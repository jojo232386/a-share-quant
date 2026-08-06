"""Deterministic identity binding for verified shared-cash portfolio runs."""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from aquant.data.calendar_snapshot import (
    CalendarError,
    VerifiedTradingCalendar,
    verify_trading_calendar,
)
from aquant.portfolio.accounting import verify_portfolio_ledger
from aquant.portfolio.contracts import (
    BUDGET_MODE,
    DIVIDEND_TAX_MODE,
    NO_BAR_VALUATION_MODE,
    PORTFOLIO_ENGINE,
    PORTFOLIO_SCHEMA_VERSION,
    PRICE_STREAM_VERSION,
    RETRY_MODE,
)
from aquant.portfolio.coordinator import (
    PortfolioBacktestResult,
    run_portfolio_backtest,
)
from aquant.portfolio.models import (
    PortfolioConfig,
    PortfolioError,
    PortfolioInstrumentInput,
    validate_portfolio_inputs,
)
from aquant.rules import (
    FeePolicyError,
    VerifiedFeePolicy,
    verify_fee_policy,
)
from aquant.universe import VerifiedUniverse

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_PACKAGE_ROOT = Path(__file__).parents[1]
_IMPLEMENTATION_FILES = (
    "pyproject.toml",
    "src/aquant/backtest/data_access.py",
    "src/aquant/backtest/feed.py",
    "src/aquant/data/calendar_snapshot.py",
    "src/aquant/data/corporate_actions.py",
    "src/aquant/data/manifest.py",
    "src/aquant/gate_e/__init__.py",
    "src/aquant/gate_e/config.py",
    "src/aquant/gate_e/frozen_manifest.py",
    "src/aquant/portfolio/__init__.py",
    "src/aquant/portfolio/accounting.py",
    "src/aquant/portfolio/availability.py",
    "src/aquant/portfolio/contracts.py",
    "src/aquant/portfolio/coordinator.py",
    "src/aquant/portfolio/export.py",
    "src/aquant/portfolio/identity.py",
    "src/aquant/portfolio/metrics.py",
    "src/aquant/portfolio/models.py",
    "src/aquant/portfolio/verify.py",
    "src/aquant/portfolio_cli.py",
    "src/aquant/release_network.py",
    "src/aquant/rules/__init__.py",
    "src/aquant/rules/engine.py",
    "src/aquant/rules/fees.py",
    "src/aquant/rules/lots.py",
    "src/aquant/rules/models.py",
    "src/aquant/rules/price_limits.py",
    "src/aquant/universe.py",
)


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PortfolioError(
                "duplicate_input_closure_key",
                "portfolio input closure contains a duplicate JSON key",
            )
        result[key] = value
    return result


def _reject_nonfinite_json_constant(_value: str) -> None:
    raise PortfolioError(
        "noncanonical_input_closure",
        "portfolio input closure must not contain non-finite numbers",
    )


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _parse_canonical_input_closure(content: object) -> dict[str, object]:
    if type(content) is not bytes:
        raise PortfolioError(
            "noncanonical_input_closure",
            "portfolio input closure must be canonical UTF-8 JSON bytes",
        )
    try:
        parsed = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except PortfolioError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PortfolioError(
            "noncanonical_input_closure",
            "portfolio input closure must be canonical UTF-8 JSON bytes",
        ) from exc
    try:
        canonical = _canonical_json_bytes(parsed)
    except (TypeError, ValueError) as exc:
        raise PortfolioError(
            "noncanonical_input_closure",
            "portfolio input closure must be canonical UTF-8 JSON bytes",
        ) from exc
    if type(parsed) is not dict or canonical != content:
        raise PortfolioError(
            "noncanonical_input_closure",
            "portfolio input closure must be canonical UTF-8 JSON bytes",
        )
    return parsed


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return {
            "__enum_type__": (f"{type(value).__module__}.{type(value).__qualname__}"),
            "value": _canonical_value(value.value),
        }
    if type(value) in {type(None), bool, int, str}:
        return value
    if type(value) is Decimal:
        if not value.is_finite():
            raise PortfolioError(
                "noncanonical_result",
                "portfolio result contains a non-finite decimal",
            )
        return str(value)
    if type(value) is date:
        return value.isoformat()
    if type(value) in {tuple, list}:
        return [_canonical_value(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise PortfolioError(
                "noncanonical_result",
                "portfolio result mapping keys must be strings",
            )
        return {key: _canonical_value(item) for key, item in sorted(value.items())}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__dataclass_type__": (f"{type(value).__module__}.{type(value).__qualname__}"),
            "fields": {
                item.name: _canonical_value(getattr(value, item.name)) for item in fields(value)
            },
        }
    raise PortfolioError(
        "noncanonical_result",
        f"portfolio result contains unsupported type {type(value).__name__}",
    )


def _read_implementation_file(filename: str) -> bytes:
    if filename == "pyproject.toml":
        bundled = _PACKAGE_ROOT / "pyproject.toml"
        if bundled.is_file():
            return bundled.read_bytes()
        return _PACKAGE_ROOT.parents[1].joinpath(filename).read_bytes()
    parts = PurePosixPath(filename).parts
    if parts[:2] != ("src", "aquant") or len(parts) <= 2:
        raise OSError("implementation path is outside the installed package")
    return _PACKAGE_ROOT.joinpath(*parts[2:]).read_bytes()


def _implementation_digest() -> str:
    digest = hashlib.sha256()
    for filename in _IMPLEMENTATION_FILES:
        try:
            content = _read_implementation_file(filename)
        except OSError as exc:
            raise PortfolioError(
                "implementation_file_missing",
                "a required portfolio implementation file is unreadable",
            ) from exc
        if type(content) is not bytes:
            raise PortfolioError(
                "implementation_file_missing",
                "a required portfolio implementation file is unreadable",
            )
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class PortfolioRunIdentity:
    """Complete deterministic identity for one verified portfolio result."""

    schema_version: str
    engine: str
    run_id: str
    implementation_digest: str
    input_closure_digest: str
    result_digest: str
    universe_id: str
    calendar_id: str
    calendar_sha256: str
    fee_policy_digest: str
    input_closure_json: bytes

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version != PORTFOLIO_SCHEMA_VERSION
            or type(self.engine) is not str
            or self.engine != PORTFOLIO_ENGINE
            or any(
                type(value) is not str or _HASH_RE.fullmatch(value) is None
                for value in (
                    self.run_id,
                    self.implementation_digest,
                    self.input_closure_digest,
                    self.result_digest,
                    self.universe_id,
                    self.calendar_id,
                    self.calendar_sha256,
                    self.fee_policy_digest,
                )
            )
            or type(self.input_closure_json) is not bytes
        ):
            raise PortfolioError(
                "invalid_portfolio_identity",
                "portfolio run identity is invalid",
            )


@dataclass(frozen=True, init=False)
class VerifiedPortfolioRun:
    """Exact registered pairing of a run identity and Gate B result."""

    identity: PortfolioRunIdentity
    result: PortfolioBacktestResult


_VERIFIED_RUN_REGISTRY: dict[
    int,
    tuple[
        weakref.ReferenceType[VerifiedPortfolioRun],
        PortfolioRunIdentity,
        PortfolioBacktestResult,
        str,
        str,
    ],
] = {}


def _validated_config(config: object) -> PortfolioConfig:
    if type(config) is not PortfolioConfig:
        raise TypeError("config must be an exact PortfolioConfig")
    return PortfolioConfig(
        strategy=config.strategy,
        initial_cash_fen=config.initial_cash_fen,
        gross_target_weight=config.gross_target_weight,
        signal_date=config.signal_date,
        end_date=config.end_date,
        max_entry_attempts=config.max_entry_attempts,
    )


def _ordered_verified_inputs(
    *,
    config: PortfolioConfig,
    inputs: tuple[PortfolioInstrumentInput, ...],
    universe: VerifiedUniverse,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
) -> tuple[PortfolioInstrumentInput, ...]:
    _validated_config(config)
    ordered = validate_portfolio_inputs(inputs, universe=universe)
    try:
        verify_trading_calendar(calendar)
    except (AttributeError, CalendarError, TypeError, ValueError) as exc:
        raise PortfolioError(
            "unverified_calendar",
            "portfolio identity requires an exact verified calendar",
        ) from exc
    try:
        verify_fee_policy(fee_policy)
    except (AttributeError, FeePolicyError, TypeError, ValueError) as exc:
        raise PortfolioError(
            "unverified_fee_policy",
            "portfolio identity requires an exact verified fee policy",
        ) from exc
    return ordered


def _input_closure(
    *,
    config: PortfolioConfig,
    inputs: tuple[PortfolioInstrumentInput, ...],
    universe: VerifiedUniverse,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
) -> dict[str, object]:
    market_data: list[dict[str, object]] = []
    corporate_actions: list[dict[str, object]] = []
    for item in inputs:
        market_provenance = item.market_data.provenance
        action_provenance = item.corporate_actions.provenance
        if action_provenance is None:
            raise PortfolioError(
                "input_contract_mismatch",
                "corporate-action provenance is required",
            )
        market_data.append(
            {
                "adjustment": market_provenance.adjustment,
                "file_sha256": market_provenance.file_sha256,
                "input_digest": item.market_data.input_digest,
                "instrument_kind": market_provenance.instrument_kind.value,
                "snapshot_id": market_provenance.snapshot_id,
                "symbol": market_provenance.symbol,
                "verification_method": market_provenance.verification_method,
            }
        )
        corporate_actions.append(
            {
                "coverage_end": action_provenance.coverage_end.isoformat(),
                "coverage_start": action_provenance.coverage_start.isoformat(),
                "file_sha256": action_provenance.file_sha256,
                "instrument_kind": action_provenance.instrument_kind.value,
                "normalization_version": action_provenance.normalization_version,
                "provider": action_provenance.provider,
                "row_count": action_provenance.row_count,
                "snapshot_id": action_provenance.snapshot_id,
                "source_schema": action_provenance.source_schema,
                "symbol": action_provenance.symbol,
                "verification_method": action_provenance.verification_method,
            }
        )
    return {
        "behavior_modes": {
            "budget_mode": BUDGET_MODE,
            "dividend_tax_mode": DIVIDEND_TAX_MODE,
            "no_bar_valuation_mode": NO_BAR_VALUATION_MODE,
            "price_stream_version": PRICE_STREAM_VERSION,
            "retry_mode": RETRY_MODE,
        },
        "calendar": {
            "calendar_id": calendar.calendar_id,
            "file_sha256": calendar.file_sha256,
        },
        "config": {
            "end_date": config.end_date.isoformat(),
            "gross_target_weight": str(config.gross_target_weight),
            "initial_cash_fen": config.initial_cash_fen,
            "max_entry_attempts": config.max_entry_attempts,
            "signal_date": config.signal_date.isoformat(),
            "strategy": config.strategy.value,
        },
        "corporate_actions": corporate_actions,
        "fee_policy": {"policy_digest": fee_policy.policy_digest},
        "market_data": market_data,
        "universe": {
            "members": [
                {"kind": item.kind, "symbol": item.symbol}
                for item in universe.members
            ],
            "name": universe.name,
            "universe_id": universe.universe_id,
        },
    }


def _identity_digest(identity: PortfolioRunIdentity) -> str:
    payload = {
        "calendar_id": identity.calendar_id,
        "calendar_sha256": identity.calendar_sha256,
        "engine": identity.engine,
        "fee_policy_digest": identity.fee_policy_digest,
        "implementation_digest": identity.implementation_digest,
        "input_closure_digest": identity.input_closure_digest,
        "input_closure_json": identity.input_closure_json.decode("utf-8"),
        "result_digest": identity.result_digest,
        "run_id": identity.run_id,
        "schema_version": identity.schema_version,
        "universe_id": identity.universe_id,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _result_digest(result: PortfolioBacktestResult) -> str:
    if type(result) is not PortfolioBacktestResult:
        raise PortfolioError(
            "noncanonical_result",
            "portfolio result must be an exact PortfolioBacktestResult",
        )
    return hashlib.sha256(_canonical_json_bytes(_canonical_value(result))).hexdigest()


def _semantic_decimal(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise PortfolioError(
            "noncanonical_result",
            "portfolio semantic result contains an invalid decimal",
        )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _semantic_result_payload(
    result: PortfolioBacktestResult,
) -> dict[str, object]:
    """Return the persisted, order-normalized result contract for Gate C."""
    config = result.config
    allocation = result.allocation
    targets = sorted(result.targets, key=lambda item: item.symbol)
    attempts = sorted(
        result.attempts,
        key=lambda item: (
            item.execution_session,
            item.symbol,
            item.attempt_number,
        ),
    )
    dividends = sorted(
        result.dividends,
        key=lambda item: (item.ex_date, item.symbol, item.event_id),
    )
    availability = sorted(
        result.availability,
        key=lambda item: (item.session, item.symbol),
    )
    ledger = result.ledger
    lots = sorted(
        ledger.lots,
        key=lambda item: (item.symbol, item.acquired_date, item.lot_id),
    )
    cash_events = sorted(
        ledger.cash_events,
        key=lambda item: (item.session, item.event_id),
    )
    receivables = sorted(
        ledger.receivables,
        key=lambda item: (item.actual_cash_date, item.event_id),
    )
    snapshots = sorted(
        ledger.daily_snapshots,
        key=lambda item: item.session,
    )
    return {
        "allocation": {
            "allocation_rounding_remainder_fen": (allocation.allocation_rounding_remainder_fen),
            "gross_target_notional_fen": (allocation.gross_target_notional_fen),
            "member_count": allocation.member_count,
            "per_symbol_target_notional_fen": (allocation.per_symbol_target_notional_fen),
            "planned_cash_reserve_fen": (allocation.planned_cash_reserve_fen),
        },
        "attempts": [
            {
                "attempt_id": item.attempt_id,
                "attempt_number": item.attempt_number,
                "availability_status": item.availability_status.value,
                "cash_available_before_fen": (item.cash_available_before_fen),
                "execution_session": item.execution_session.isoformat(),
                "fees": (
                    None
                    if item.fees is None
                    else {
                        "commission_fen": item.fees.commission_fen,
                        "stamp_duty_fen": item.fees.stamp_duty_fen,
                        "touched_rates": [
                            {
                                "effective_date": (
                                    None
                                    if touch.effective_date is None
                                    else touch.effective_date.isoformat()
                                ),
                                "fee_name": touch.fee_name,
                                "minimum_yuan": (
                                    None
                                    if touch.minimum_yuan is None
                                    else _semantic_decimal(touch.minimum_yuan)
                                ),
                                "rate": _semantic_decimal(touch.rate),
                            }
                            for touch in sorted(
                                item.fees.touched_rates,
                                key=lambda touch: (
                                    touch.fee_name,
                                    touch.effective_date or date.min,
                                ),
                            )
                        ],
                        "transfer_fee_fen": item.fees.transfer_fee_fen,
                    }
                ),
                "fill_event_id": item.fill_event_id,
                "initial_candidate_cash_required_fen": (item.initial_candidate_cash_required_fen),
                "initial_candidate_size": item.initial_candidate_size,
                "intent_session": item.intent_session.isoformat(),
                "original_signal_date": (item.original_signal_date.isoformat()),
                "quantity_adjustment_reason": (item.quantity_adjustment_reason),
                "rejection_reason": (
                    None if item.rejection_reason is None else item.rejection_reason.value
                ),
                "requested_cash_required_fen": (item.requested_cash_required_fen),
                "requested_size": item.requested_size,
                "status": item.status.value,
                "symbol": item.symbol,
                "target_id": item.target_id,
            }
            for item in attempts
        ],
        "availability": [
            {
                "adjustment_reason": item.adjustment_reason,
                "carried_sessions": item.carried_sessions,
                "mark_price": _semantic_decimal(item.mark_price),
                "session": item.session.isoformat(),
                "status": item.status.value,
                "symbol": item.symbol,
            }
            for item in availability
        ],
        "config": {
            "end_date": config.end_date.isoformat(),
            "gross_target_weight": _semantic_decimal(config.gross_target_weight),
            "initial_cash_fen": config.initial_cash_fen,
            "max_entry_attempts": config.max_entry_attempts,
            "signal_date": config.signal_date.isoformat(),
            "strategy": config.strategy.value,
        },
        "dividends": [
            {
                "actual_cash_date": item.actual_cash_date.isoformat(),
                "amount_fen": item.amount_fen,
                "cash_dividend_per_unit": _semantic_decimal(item.cash_dividend_per_unit),
                "entitled_size": item.entitled_size,
                "event_id": item.event_id,
                "ex_date": item.ex_date.isoformat(),
                "source_payable_date": (item.source_payable_date.isoformat()),
                "symbol": item.symbol,
            }
            for item in dividends
        ],
        "ledger": {
            "cash_events": [
                {
                    "cash_after_fen": item.cash_after_fen,
                    "cash_before_fen": item.cash_before_fen,
                    "commission_fen": item.commission_fen,
                    "event_id": item.event_id,
                    "event_kind": item.event_kind.value,
                    "notional_fen": item.notional_fen,
                    "reference_id": item.reference_id,
                    "session": item.session.isoformat(),
                    "side": (None if item.side is None else item.side.value),
                    "stamp_duty_fen": item.stamp_duty_fen,
                    "symbol": item.symbol,
                    "transfer_fee_fen": item.transfer_fee_fen,
                }
                for item in cash_events
            ],
            "cash_fen": ledger.cash_fen,
            "daily_snapshots": [
                {
                    "cash_fen": item.cash_fen,
                    "equity_fen": item.equity_fen,
                    "position_market_value_fen": (item.position_market_value_fen),
                    "receivable_fen": item.receivable_fen,
                    "session": item.session.isoformat(),
                    "valuations": [
                        {
                            "available_size": value.available_size,
                            "locked_size": value.locked_size,
                            "mark_price": _semantic_decimal(value.mark_price),
                            "market_value_fen": (value.market_value_fen),
                            "symbol": value.symbol,
                            "total_size": value.total_size,
                        }
                        for value in sorted(
                            item.valuations,
                            key=lambda value: value.symbol,
                        )
                    ],
                }
                for item in snapshots
            ],
            "initial_cash_fen": ledger.initial_cash_fen,
            "lots": [
                {
                    "acquired_date": item.acquired_date.isoformat(),
                    "available_date": item.available_date.isoformat(),
                    "lot_id": item.lot_id,
                    "original_size": item.original_size,
                    "remaining_size": item.remaining_size,
                    "symbol": item.symbol,
                    "unit_cost": _semantic_decimal(item.unit_cost),
                }
                for item in lots
            ],
            "receivables": [
                {
                    "actual_cash_date": item.actual_cash_date.isoformat(),
                    "amount_fen": item.amount_fen,
                    "event_id": item.event_id,
                    "paid_date": (None if item.paid_date is None else item.paid_date.isoformat()),
                    "registered_date": item.registered_date.isoformat(),
                    "source_payable_date": (item.source_payable_date.isoformat()),
                    "symbol": item.symbol,
                }
                for item in receivables
            ],
        },
        "semantic_result_schema": "portfolio-semantic-result-v1",
        "targets": [
            {
                "attempts_used": item.attempts_used,
                "fill_event_id": item.fill_event_id,
                "signal_date": item.signal_date.isoformat(),
                "status": item.status.value,
                "symbol": item.symbol,
                "target_id": item.target_id,
                "target_notional_fen": item.target_notional_fen,
            }
            for item in targets
        ],
    }


def _semantic_result_digest(result: PortfolioBacktestResult) -> str:
    if type(result) is not PortfolioBacktestResult:
        raise PortfolioError(
            "noncanonical_result",
            "portfolio result must be an exact PortfolioBacktestResult",
        )
    return hashlib.sha256(_canonical_json_bytes(_semantic_result_payload(result))).hexdigest()


def run_verified_portfolio(
    *,
    config: PortfolioConfig,
    inputs: tuple[PortfolioInstrumentInput, ...],
    universe: VerifiedUniverse,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
) -> VerifiedPortfolioRun:
    """Run Gate B once and bind its exact input, code, and result identities."""
    ordered_inputs = _ordered_verified_inputs(
        config=config,
        inputs=inputs,
        universe=universe,
        calendar=calendar,
        fee_policy=fee_policy,
    )
    input_closure_json = _canonical_json_bytes(
        _input_closure(
            config=config,
            inputs=ordered_inputs,
            universe=universe,
            calendar=calendar,
            fee_policy=fee_policy,
        )
    )
    input_closure_digest = hashlib.sha256(input_closure_json).hexdigest()
    implementation_digest = _implementation_digest()
    result = run_portfolio_backtest(
        config=config,
        inputs=ordered_inputs,
        universe=universe,
        calendar=calendar,
        fee_policy=fee_policy,
    )
    post_run_inputs = _ordered_verified_inputs(
        config=config,
        inputs=inputs,
        universe=universe,
        calendar=calendar,
        fee_policy=fee_policy,
    )
    post_run_closure_json = _canonical_json_bytes(
        _input_closure(
            config=config,
            inputs=post_run_inputs,
            universe=universe,
            calendar=calendar,
            fee_policy=fee_policy,
        )
    )
    if post_run_closure_json != input_closure_json:
        raise PortfolioError(
            "portfolio_inputs_changed_during_run",
            "portfolio inputs changed while the engine was running",
        )
    if _implementation_digest() != implementation_digest:
        raise PortfolioError(
            "implementation_changed_during_run",
            "portfolio implementation changed while the engine was running",
        )
    verify_portfolio_ledger(result.ledger)
    result_digest = _semantic_result_digest(result)
    identity_payload = {
        "engine": PORTFOLIO_ENGINE,
        "implementation_digest": implementation_digest,
        "input_closure_digest": input_closure_digest,
        "result_digest": result_digest,
        "schema_version": PORTFOLIO_SCHEMA_VERSION,
    }
    run_id = hashlib.sha256(_canonical_json_bytes(identity_payload)).hexdigest()
    identity = PortfolioRunIdentity(
        schema_version=PORTFOLIO_SCHEMA_VERSION,
        engine=PORTFOLIO_ENGINE,
        run_id=run_id,
        implementation_digest=implementation_digest,
        input_closure_digest=input_closure_digest,
        result_digest=result_digest,
        universe_id=universe.universe_id,
        calendar_id=calendar.calendar_id,
        calendar_sha256=calendar.file_sha256,
        fee_policy_digest=fee_policy.policy_digest,
        input_closure_json=input_closure_json,
    )
    run = object.__new__(VerifiedPortfolioRun)
    object.__setattr__(run, "identity", identity)
    object.__setattr__(run, "result", result)
    registry_key = id(run)

    def discard_registered_run(
        _reference: weakref.ReferenceType[VerifiedPortfolioRun],
        *,
        key: int = registry_key,
    ) -> None:
        _VERIFIED_RUN_REGISTRY.pop(key, None)

    _VERIFIED_RUN_REGISTRY[registry_key] = (
        weakref.ref(run, discard_registered_run),
        identity,
        result,
        _identity_digest(identity),
        _result_digest(result),
    )
    return run


def verify_portfolio_run(run: VerifiedPortfolioRun) -> None:
    """Recompute the exact registered wrapper, identity, closure, and result."""
    if type(run) is not VerifiedPortfolioRun:
        raise TypeError("run must be an exact VerifiedPortfolioRun")
    registered = _VERIFIED_RUN_REGISTRY.get(id(run))
    if registered is None or registered[0]() is not run:
        raise PortfolioError(
            "unverified_portfolio_run",
            "portfolio run is not registered by the verified runner",
        )
    if (
        type(run.identity) is not PortfolioRunIdentity
        or type(run.result) is not PortfolioBacktestResult
    ):
        raise PortfolioError(
            "verified_portfolio_run_modified",
            "verified portfolio run changed after construction",
        )
    _parse_canonical_input_closure(run.identity.input_closure_json)
    if (
        hashlib.sha256(run.identity.input_closure_json).hexdigest()
        != run.identity.input_closure_digest
        or registered[1] is not run.identity
        or registered[2] is not run.result
    ):
        raise PortfolioError(
            "verified_portfolio_run_modified",
            "verified portfolio run changed after construction",
        )
    try:
        identity_digest = _identity_digest(run.identity)
        result_digest = _result_digest(run.result)
    except (AttributeError, PortfolioError, TypeError, UnicodeError, ValueError) as exc:
        raise PortfolioError(
            "verified_portfolio_run_modified",
            "verified portfolio run changed after construction",
        ) from exc
    if (
        identity_digest != registered[3]
        or result_digest != registered[4]
        or _semantic_result_digest(run.result) != run.identity.result_digest
    ):
        raise PortfolioError(
            "verified_portfolio_run_modified",
            "verified portfolio run changed after construction",
        )
    try:
        verify_portfolio_ledger(run.result.ledger)
    except (AttributeError, PortfolioError, TypeError, ValueError) as exc:
        raise PortfolioError(
            "verified_portfolio_run_modified",
            "verified portfolio run changed after construction",
        ) from exc
