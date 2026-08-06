"""Canonical payloads and no-replace atomic publication for portfolio runs."""

from __future__ import annotations

import csv
import ctypes
import errno
import fcntl
import hashlib
import io
import json
import os
import secrets
import stat
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path

from aquant.portfolio.accounting import CashEventKind
from aquant.portfolio.contracts import (
    BUDGET_MODE,
    DIVIDEND_TAX_MODE,
    NO_BAR_VALUATION_MODE,
    PORTFOLIO_SCHEMA_VERSION,
    PRICE_STREAM_VERSION,
    RETRY_MODE,
)
from aquant.portfolio.coordinator import AttemptStatus
from aquant.portfolio.identity import (
    VerifiedPortfolioRun,
    verify_portfolio_run,
)
from aquant.portfolio.metrics import PortfolioMetrics, compute_portfolio_metrics

PORTFOLIO_PAYLOAD_FILES = frozenset(
    {
        "run.json",
        "targets.csv",
        "orders.csv",
        "fills.csv",
        "positions.csv",
        "lots.csv",
        "cash.csv",
        "equity.csv",
        "receivables.csv",
        "corporate_actions.csv",
        "availability.csv",
        "metrics.json",
    }
)
PORTFOLIO_ARTIFACT_FILES = PORTFOLIO_PAYLOAD_FILES | {"artifact_manifest.json"}

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 1

_TARGET_FIELDS = (
    "run_id",
    "schema_version",
    "target_id",
    "symbol",
    "signal_date",
    "target_notional_fen",
    "attempts_used",
    "status",
    "fill_event_id",
)
_ORDER_FIELDS = (
    "run_id",
    "schema_version",
    "attempt_id",
    "target_id",
    "symbol",
    "original_signal_date",
    "intent_session",
    "execution_session",
    "attempt_number",
    "initial_candidate_size",
    "requested_size",
    "availability_status",
    "status",
    "rejection_reason",
    "cash_available_before_fen",
    "initial_candidate_cash_required_fen",
    "requested_cash_required_fen",
    "quantity_adjustment_reason",
    "fill_event_id",
)
_FILL_FIELDS = (
    "run_id",
    "schema_version",
    "fill_event_id",
    "attempt_id",
    "target_id",
    "symbol",
    "execution_session",
    "side",
    "initial_candidate_size",
    "filled_size",
    "unit_price",
    "notional_fen",
    "commission_fen",
    "stamp_duty_fen",
    "transfer_fee_fen",
    "total_fees_fen",
    "cash_before_fen",
    "cash_after_fen",
    "lot_id",
    "available_date",
    "cash_available_before_fen",
    "initial_candidate_cash_required_fen",
    "requested_cash_required_fen",
    "quantity_adjustment_reason",
)
_POSITION_FIELDS = (
    "run_id",
    "schema_version",
    "session",
    "symbol",
    "total_size",
    "available_size",
    "locked_size",
    "mark_price",
    "market_value_fen",
)
_LOT_FIELDS = (
    "run_id",
    "schema_version",
    "lot_id",
    "symbol",
    "acquired_date",
    "available_date",
    "original_size",
    "remaining_size",
    "unit_cost",
)
_CASH_FIELDS = (
    "run_id",
    "schema_version",
    "event_id",
    "event_kind",
    "session",
    "side",
    "symbol",
    "reference_id",
    "notional_fen",
    "commission_fen",
    "stamp_duty_fen",
    "transfer_fee_fen",
    "total_fees_fen",
    "cash_before_fen",
    "cash_after_fen",
)
_EQUITY_FIELDS = (
    "run_id",
    "schema_version",
    "session",
    "cash_fen",
    "position_market_value_fen",
    "receivable_fen",
    "equity_fen",
)
_RECEIVABLE_FIELDS = (
    "run_id",
    "schema_version",
    "event_id",
    "symbol",
    "registered_date",
    "source_payable_date",
    "actual_cash_date",
    "amount_fen",
    "paid_date",
)
_CORPORATE_ACTION_FIELDS = (
    "run_id",
    "schema_version",
    "event_id",
    "symbol",
    "ex_date",
    "source_payable_date",
    "actual_cash_date",
    "entitled_size",
    "cash_dividend_per_unit",
    "amount_fen",
)
_AVAILABILITY_FIELDS = (
    "run_id",
    "schema_version",
    "session",
    "symbol",
    "status",
    "mark_price",
    "carried_sessions",
    "adjustment_reason",
)


class PortfolioExportError(RuntimeError):
    """Stable fail-closed error for portfolio payload publication."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise PortfolioExportError(
            "noncanonical_decimal",
            "portfolio payload contains a non-finite decimal",
        )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        return "0"
    return text


def _json_bytes(value: object) -> bytes:
    try:
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
    except (TypeError, ValueError) as exc:
        raise PortfolioExportError(
            "noncanonical_payload",
            "portfolio JSON payload is not canonical",
        ) from exc


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, Enum):
        return value.value
    if type(value) is date:
        return value.isoformat()
    if type(value) is Decimal:
        return _decimal_text(value)
    if type(value) in {str, int}:
        return value
    raise PortfolioExportError(
        "noncanonical_payload",
        f"unsupported portfolio CSV value type: {type(value).__name__}",
    )


def _csv_bytes(
    fieldnames: tuple[str, ...],
    rows: Sequence[Mapping[str, object]],
) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=fieldnames,
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        if set(row) != set(fieldnames):
            raise PortfolioExportError(
                "noncanonical_payload",
                "portfolio CSV row does not match its explicit schema",
            )
        writer.writerow({name: _csv_value(row[name]) for name in fieldnames})
    return stream.getvalue().encode("utf-8")


def _base_row(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "schema_version": PORTFOLIO_SCHEMA_VERSION,
    }


def _metric_payload(
    *,
    run_id: str,
    metrics: PortfolioMetrics,
) -> dict[str, object]:
    return {
        "allocation_rounding_remainder_fen": (metrics.allocation_rounding_remainder_fen),
        "annual_sessions": metrics.annual_sessions,
        "annualized_return": _decimal_text(metrics.annualized_return),
        "annualized_volatility": (
            None
            if metrics.annualized_volatility is None
            else _decimal_text(metrics.annualized_volatility)
        ),
        "daily_gross_exposure": [
            {
                "session": session.isoformat(),
                "value": _decimal_text(value),
            }
            for session, value in metrics.daily_gross_exposure
        ],
        "expired_uninvested_fen": metrics.expired_uninvested_fen,
        "fee_lot_reduction_fen": metrics.fee_lot_reduction_fen,
        "final_symbol_weight_deviations": [
            {"symbol": symbol, "value": _decimal_text(value)}
            for symbol, value in metrics.final_symbol_weight_deviations
        ],
        "gross_target_notional_fen": metrics.gross_target_notional_fen,
        "invested_notional_fen": metrics.invested_notional_fen,
        "live_trading": metrics.live_trading,
        "max_drawdown": _decimal_text(metrics.max_drawdown),
        "max_gross_exposure": _decimal_text(metrics.max_gross_exposure),
        "max_symbol_weight": _decimal_text(metrics.max_symbol_weight),
        "max_target_weight_deviation": _decimal_text(metrics.max_target_weight_deviation),
        "observation_count": metrics.observation_count,
        "observed_return_count": metrics.observed_return_count,
        "ordinary_lot_rounding_fen": metrics.ordinary_lot_rounding_fen,
        "planned_cash_reserve_fen": metrics.planned_cash_reserve_fen,
        "profit_claim": metrics.profit_claim,
        "rejected_attempt_count": metrics.rejected_attempt_count,
        "rejected_uninvested_fen": metrics.rejected_uninvested_fen,
        "research_only": metrics.research_only,
        "risk_free_rate": _decimal_text(metrics.risk_free_rate),
        "run_id": run_id,
        "schema_version": PORTFOLIO_SCHEMA_VERSION,
        "sharpe_zero_rate": (
            None if metrics.sharpe_zero_rate is None else _decimal_text(metrics.sharpe_zero_rate)
        ),
        "total_paid_fees_fen": metrics.total_paid_fees_fen,
        "total_return": _decimal_text(metrics.total_return),
        "trade_count": metrics.trade_count,
        "turnover": _decimal_text(metrics.turnover),
    }


def portfolio_payload_bytes(
    run: VerifiedPortfolioRun,
) -> dict[str, tuple[bytes, int]]:
    """Return all twelve canonical payloads and their exact row counts."""
    verify_portfolio_run(run)
    metrics = compute_portfolio_metrics(run)
    identity = run.identity
    result = run.result
    run_id = identity.run_id

    targets = tuple(sorted(result.targets, key=lambda item: item.symbol))
    attempts = tuple(
        sorted(
            result.attempts,
            key=lambda item: (
                item.execution_session,
                item.symbol,
                item.attempt_number,
            ),
        )
    )
    fill_events = {
        item.event_id: item
        for item in result.ledger.cash_events
        if item.event_kind is CashEventKind.FILL
    }
    lots_by_id = {item.lot_id: item for item in result.ledger.lots}
    fills = tuple(item for item in attempts if item.status is AttemptStatus.FILLED)
    positions = tuple(
        (snapshot.session, valuation)
        for snapshot in result.ledger.daily_snapshots
        for valuation in snapshot.valuations
    )
    lots = tuple(
        sorted(
            result.ledger.lots,
            key=lambda item: (item.symbol, item.acquired_date, item.lot_id),
        )
    )
    cash_events = tuple(
        sorted(
            result.ledger.cash_events,
            key=lambda item: (item.session, item.event_id),
        )
    )
    snapshots = tuple(
        sorted(
            result.ledger.daily_snapshots,
            key=lambda item: item.session,
        )
    )
    receivables = tuple(
        sorted(
            result.ledger.receivables,
            key=lambda item: (item.actual_cash_date, item.event_id),
        )
    )
    dividends = tuple(
        sorted(
            result.dividends,
            key=lambda item: (item.ex_date, item.symbol, item.event_id),
        )
    )
    availability = tuple(
        sorted(
            result.availability,
            key=lambda item: (item.session, item.symbol),
        )
    )

    rows: dict[str, list[dict[str, object]]] = {
        "targets.csv": [
            {
                **_base_row(run_id),
                "target_id": item.target_id,
                "symbol": item.symbol,
                "signal_date": item.signal_date,
                "target_notional_fen": item.target_notional_fen,
                "attempts_used": item.attempts_used,
                "status": item.status,
                "fill_event_id": item.fill_event_id,
            }
            for item in targets
        ],
        "orders.csv": [
            {
                **_base_row(run_id),
                "attempt_id": item.attempt_id,
                "target_id": item.target_id,
                "symbol": item.symbol,
                "original_signal_date": item.original_signal_date,
                "intent_session": item.intent_session,
                "execution_session": item.execution_session,
                "attempt_number": item.attempt_number,
                "initial_candidate_size": item.initial_candidate_size,
                "requested_size": item.requested_size,
                "availability_status": item.availability_status,
                "status": item.status,
                "rejection_reason": item.rejection_reason,
                "cash_available_before_fen": item.cash_available_before_fen,
                "initial_candidate_cash_required_fen": (item.initial_candidate_cash_required_fen),
                "requested_cash_required_fen": (item.requested_cash_required_fen),
                "quantity_adjustment_reason": item.quantity_adjustment_reason,
                "fill_event_id": item.fill_event_id,
            }
            for item in attempts
        ],
        "fills.csv": [],
        "positions.csv": [
            {
                **_base_row(run_id),
                "session": session,
                "symbol": item.symbol,
                "total_size": item.total_size,
                "available_size": item.available_size,
                "locked_size": item.locked_size,
                "mark_price": item.mark_price,
                "market_value_fen": item.market_value_fen,
            }
            for session, item in positions
        ],
        "lots.csv": [
            {
                **_base_row(run_id),
                "lot_id": item.lot_id,
                "symbol": item.symbol,
                "acquired_date": item.acquired_date,
                "available_date": item.available_date,
                "original_size": item.original_size,
                "remaining_size": item.remaining_size,
                "unit_cost": item.unit_cost,
            }
            for item in lots
        ],
        "cash.csv": [
            {
                **_base_row(run_id),
                "event_id": item.event_id,
                "event_kind": item.event_kind,
                "session": item.session,
                "side": item.side,
                "symbol": item.symbol,
                "reference_id": item.reference_id,
                "notional_fen": item.notional_fen,
                "commission_fen": item.commission_fen,
                "stamp_duty_fen": item.stamp_duty_fen,
                "transfer_fee_fen": item.transfer_fee_fen,
                "total_fees_fen": item.total_fees_fen,
                "cash_before_fen": item.cash_before_fen,
                "cash_after_fen": item.cash_after_fen,
            }
            for item in cash_events
        ],
        "equity.csv": [
            {
                **_base_row(run_id),
                "session": item.session,
                "cash_fen": item.cash_fen,
                "position_market_value_fen": (item.position_market_value_fen),
                "receivable_fen": item.receivable_fen,
                "equity_fen": item.equity_fen,
            }
            for item in snapshots
        ],
        "receivables.csv": [
            {
                **_base_row(run_id),
                "event_id": item.event_id,
                "symbol": item.symbol,
                "registered_date": item.registered_date,
                "source_payable_date": item.source_payable_date,
                "actual_cash_date": item.actual_cash_date,
                "amount_fen": item.amount_fen,
                "paid_date": item.paid_date,
            }
            for item in receivables
        ],
        "corporate_actions.csv": [
            {
                **_base_row(run_id),
                "event_id": item.event_id,
                "symbol": item.symbol,
                "ex_date": item.ex_date,
                "source_payable_date": item.source_payable_date,
                "actual_cash_date": item.actual_cash_date,
                "entitled_size": item.entitled_size,
                "cash_dividend_per_unit": item.cash_dividend_per_unit,
                "amount_fen": item.amount_fen,
            }
            for item in dividends
        ],
        "availability.csv": [
            {
                **_base_row(run_id),
                "session": item.session,
                "symbol": item.symbol,
                "status": item.status,
                "mark_price": item.mark_price,
                "carried_sessions": item.carried_sessions,
                "adjustment_reason": item.adjustment_reason,
            }
            for item in availability
        ],
    }
    for attempt in fills:
        if attempt.fill_event_id is None:
            raise PortfolioExportError(
                "fill_reconciliation_failed",
                "filled attempt is missing its cash event",
            )
        event = fill_events.get(attempt.fill_event_id)
        if event is None:
            raise PortfolioExportError(
                "fill_reconciliation_failed",
                "filled attempt has no cash event",
            )
        lot = lots_by_id.get(event.reference_id)
        if lot is None:
            raise PortfolioExportError(
                "fill_reconciliation_failed",
                "fill cash event has no position lot",
            )
        rows["fills.csv"].append(
            {
                **_base_row(run_id),
                "fill_event_id": event.event_id,
                "attempt_id": attempt.attempt_id,
                "target_id": attempt.target_id,
                "symbol": attempt.symbol,
                "execution_session": attempt.execution_session,
                "side": event.side,
                "initial_candidate_size": attempt.initial_candidate_size,
                "filled_size": attempt.requested_size,
                "unit_price": lot.unit_cost,
                "notional_fen": event.notional_fen,
                "commission_fen": event.commission_fen,
                "stamp_duty_fen": event.stamp_duty_fen,
                "transfer_fee_fen": event.transfer_fee_fen,
                "total_fees_fen": event.total_fees_fen,
                "cash_before_fen": event.cash_before_fen,
                "cash_after_fen": event.cash_after_fen,
                "lot_id": lot.lot_id,
                "available_date": lot.available_date,
                "cash_available_before_fen": (attempt.cash_available_before_fen),
                "initial_candidate_cash_required_fen": (
                    attempt.initial_candidate_cash_required_fen
                ),
                "requested_cash_required_fen": (attempt.requested_cash_required_fen),
                "quantity_adjustment_reason": (attempt.quantity_adjustment_reason),
            }
        )

    headers = {
        "targets.csv": _TARGET_FIELDS,
        "orders.csv": _ORDER_FIELDS,
        "fills.csv": _FILL_FIELDS,
        "positions.csv": _POSITION_FIELDS,
        "lots.csv": _LOT_FIELDS,
        "cash.csv": _CASH_FIELDS,
        "equity.csv": _EQUITY_FIELDS,
        "receivables.csv": _RECEIVABLE_FIELDS,
        "corporate_actions.csv": _CORPORATE_ACTION_FIELDS,
        "availability.csv": _AVAILABILITY_FIELDS,
    }
    row_counts = {
        "run.json": 1,
        **{name: len(value) for name, value in rows.items()},
        "metrics.json": 1,
    }
    touched_rates = [
        {
            "attempt_id": attempt.attempt_id,
            "effective_date": (
                touch.effective_date.isoformat() if touch.effective_date is not None else None
            ),
            "fee_name": touch.fee_name,
            "minimum_yuan": (
                None if touch.minimum_yuan is None else _decimal_text(touch.minimum_yuan)
            ),
            "rate": _decimal_text(touch.rate),
            "symbol": attempt.symbol,
            "execution_session": attempt.execution_session.isoformat(),
        }
        for attempt in fills
        if attempt.fees is not None
        for touch in sorted(
            attempt.fees.touched_rates,
            key=lambda item: (
                item.fee_name,
                item.effective_date or date.min,
            ),
        )
    ]
    closure = json.loads(identity.input_closure_json)
    run_payload = {
        "behavior_modes": {
            "budget_mode": BUDGET_MODE,
            "dividend_tax_mode": DIVIDEND_TAX_MODE,
            "no_bar_valuation_mode": NO_BAR_VALUATION_MODE,
            "price_stream_version": PRICE_STREAM_VERSION,
            "retry_mode": RETRY_MODE,
        },
        "calendar_id": identity.calendar_id,
        "calendar_sha256": identity.calendar_sha256,
        "config": {
            "end_date": result.config.end_date.isoformat(),
            "gross_target_weight": _decimal_text(result.config.gross_target_weight),
            "initial_cash_fen": result.config.initial_cash_fen,
            "max_entry_attempts": result.config.max_entry_attempts,
            "signal_date": result.config.signal_date.isoformat(),
            "strategy": result.config.strategy.value,
        },
        "engine": identity.engine,
        "fee_policy_digest": identity.fee_policy_digest,
        "implementation_digest": identity.implementation_digest,
        "input_closure": closure,
        "input_closure_digest": identity.input_closure_digest,
        "result_digest": identity.result_digest,
        "row_counts": {name: row_counts[name] for name in sorted(row_counts)},
        "run_id": run_id,
        "schema_version": identity.schema_version,
        "touched_fee_rates": touched_rates,
        "universe_id": identity.universe_id,
    }
    payloads: dict[str, tuple[bytes, int]] = {
        name: (_csv_bytes(headers[name], rows[name]), row_counts[name]) for name in sorted(headers)
    }
    payloads["metrics.json"] = (
        _json_bytes(_metric_payload(run_id=run_id, metrics=metrics)),
        1,
    )
    payloads["run.json"] = (_json_bytes(run_payload), 1)
    if set(payloads) != PORTFOLIO_PAYLOAD_FILES:
        raise PortfolioExportError(
            "incomplete_payload",
            "portfolio payload file set is incomplete",
        )
    return {name: payloads[name] for name in sorted(payloads)}


def _complete_bundle_bytes(
    run: VerifiedPortfolioRun,
) -> dict[str, bytes]:
    payloads = portfolio_payload_bytes(run)
    manifest = {
        "artifact_schema_version": PORTFOLIO_SCHEMA_VERSION,
        "files": {
            name: {
                "row_count": row_count,
                "run_id": run.identity.run_id,
                "schema_version": PORTFOLIO_SCHEMA_VERSION,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, (content, row_count) in sorted(payloads.items())
        },
        "run_id": run.identity.run_id,
        "status": "complete",
    }
    result = {name: content for name, (content, _row_count) in payloads.items()}
    result["artifact_manifest.json"] = _json_bytes(manifest)
    return {name: result[name] for name in sorted(result)}


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


@contextmanager
def _open_safe_output_root(
    output_root: str | Path,
) -> Iterator[tuple[Path, int, os.stat_result]]:
    if not isinstance(output_root, (str, os.PathLike)):
        raise TypeError("output_root must be a string or path-like object")
    root = Path(os.path.abspath(os.fspath(output_root)))
    descriptor: int | None = None
    try:
        descriptor = os.open(
            root.anchor,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
        )
        for component in root.parts[1:]:
            child: int | None = None
            try:
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(
                        component,
                        os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                        dir_fd=descriptor,
                    )
                entry_metadata = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                opened_metadata = os.fstat(child)
                if (
                    not stat.S_ISDIR(entry_metadata.st_mode)
                    or not stat.S_ISDIR(opened_metadata.st_mode)
                    or not _same_object(entry_metadata, opened_metadata)
                ):
                    raise PortfolioExportError(
                        "unsafe_output_root",
                        "portfolio output path components must be real directories",
                    )
            except (OSError, PortfolioExportError):
                if child is not None:
                    os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
    except PortfolioExportError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, RuntimeError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise PortfolioExportError(
            "unsafe_output_root",
            "portfolio output root cannot be opened safely",
        ) from exc
    try:
        yield root, descriptor, metadata
    finally:
        os.close(descriptor)


def _verify_output_root_binding(
    root: Path,
    root_descriptor: int,
    expected: os.stat_result,
) -> None:
    try:
        opened = os.fstat(root_descriptor)
        path_metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PortfolioExportError(
            "output_root_changed",
            "portfolio output root binding changed during export",
        ) from exc
    if (
        resolved != root
        or not stat.S_ISDIR(path_metadata.st_mode)
        or not _same_object(opened, expected)
        or not _same_object(path_metadata, expected)
    ):
        raise PortfolioExportError(
            "output_root_changed",
            "portfolio output root binding changed during export",
        )


@contextmanager
def _run_lock(root_descriptor: int, run_id: str) -> Iterator[None]:
    lock_name = f".{run_id}.lock"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            lock_name,
            os.O_RDWR | os.O_CREAT | _NOFOLLOW,
            0o600,
            dir_fd=root_descriptor,
        )
        metadata = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise PortfolioExportError(
            "unsafe_lock",
            "portfolio export lock cannot be opened safely",
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise PortfolioExportError(
            "unsafe_lock",
            "portfolio export lock must be a single-link regular file",
        )
    locked = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        current = os.stat(
            lock_name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if not _same_object(metadata, current):
            raise PortfolioExportError(
                "unsafe_lock",
                "portfolio export lock binding changed",
            )
        yield
    except PortfolioExportError:
        raise
    except OSError as exc:
        raise PortfolioExportError(
            "unsafe_lock",
            "portfolio export lock cannot be verified safely",
        ) from exc
    finally:
        unlock_error: OSError | None = None
        close_error: OSError | None = None
        if locked:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError as exc:
                unlock_error = exc
        try:
            os.close(descriptor)
        except OSError as exc:
            close_error = exc
        release_error = unlock_error or close_error
        if release_error is not None:
            raise PortfolioExportError(
                "unsafe_lock",
                "portfolio export lock could not be released safely",
            ) from release_error


def _safe_file_bytes(directory_descriptor: int, name: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | _NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PortfolioExportError(
                "artifact_conflict",
                "portfolio artifact contains an unsafe payload file",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        current = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if not _same_object(metadata, current):
            raise PortfolioExportError(
                "artifact_conflict",
                "portfolio artifact payload binding changed",
            )
        return b"".join(chunks)
    except PortfolioExportError:
        raise
    except OSError as exc:
        raise PortfolioExportError(
            "artifact_conflict",
            "portfolio artifact payload cannot be read safely",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_existing_bundle(
    root_descriptor: int,
    directory_name: str,
    expected: Mapping[str, bytes],
) -> None:
    descriptor: int | None = None
    try:
        entry_metadata = os.stat(
            directory_name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(entry_metadata.st_mode):
            raise PortfolioExportError(
                "artifact_conflict",
                "portfolio artifact target must be a real directory",
            )
        descriptor = os.open(
            directory_name,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
            dir_fd=root_descriptor,
        )
        opened_metadata = os.fstat(descriptor)
    except PortfolioExportError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise PortfolioExportError(
            "artifact_conflict",
            "portfolio artifact target cannot be opened safely",
        ) from exc
    if not _same_object(entry_metadata, opened_metadata):
        os.close(descriptor)
        raise PortfolioExportError(
            "artifact_conflict",
            "portfolio artifact target binding changed",
        )
    try:
        try:
            actual_names = set(os.listdir(descriptor))
        except OSError as exc:
            raise PortfolioExportError(
                "artifact_conflict",
                "portfolio artifact target cannot be listed safely",
            ) from exc
        if actual_names != set(expected):
            raise PortfolioExportError(
                "artifact_conflict",
                "portfolio artifact file set conflicts with expected output",
            )
        for name, content in expected.items():
            if _safe_file_bytes(descriptor, name) != content:
                raise PortfolioExportError(
                    "artifact_conflict",
                    "portfolio artifact bytes conflict with expected output",
                )
        current = os.stat(
            directory_name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if not _same_object(opened_metadata, current):
            raise PortfolioExportError(
                "artifact_conflict",
                "portfolio artifact target binding changed",
            )
    except PortfolioExportError:
        raise
    except OSError as exc:
        raise PortfolioExportError(
            "artifact_conflict",
            "portfolio artifact target cannot be verified safely",
        ) from exc
    finally:
        os.close(descriptor)


def _write_bundle(
    directory_descriptor: int,
    contents: Mapping[str, bytes],
    created_payloads: dict[str, os.stat_result],
) -> None:
    for name, content in sorted(contents.items()):
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=directory_descriptor,
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PortfolioExportError(
                    "unsafe_temporary_payload",
                    "temporary portfolio payload is unsafe",
                )
            created_payloads[name] = metadata
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short portfolio payload write")
                view = view[written:]
            os.fsync(descriptor)
        except PortfolioExportError:
            raise
        except OSError as exc:
            raise PortfolioExportError(
                "payload_write_failed",
                "portfolio payload could not be written durably",
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
    try:
        os.fsync(directory_descriptor)
    except OSError as exc:
        raise PortfolioExportError(
            "payload_fsync_failed",
            "portfolio temporary directory could not be synced",
        ) from exc


def _rename_no_replace(
    source_directory_descriptor: int,
    source_name: str,
    destination_directory_descriptor: int,
    destination_name: str,
) -> None:
    source_bytes = os.fsencode(source_name)
    destination_bytes = os.fsencode(destination_name)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            function = library.renameatx_np
        except AttributeError as exc:
            raise PortfolioExportError(
                "atomic_publish_unavailable",
                "macOS renameatx_np is unavailable",
            ) from exc
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            source_directory_descriptor,
            source_bytes,
            destination_directory_descriptor,
            destination_bytes,
            _RENAME_EXCL,
        )
    elif sys.platform.startswith("linux"):
        try:
            function = library.renameat2
        except AttributeError as exc:
            raise PortfolioExportError(
                "atomic_publish_unavailable",
                "Linux renameat2 is unavailable",
            ) from exc
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            source_directory_descriptor,
            source_bytes,
            destination_directory_descriptor,
            destination_bytes,
            _RENAME_NOREPLACE,
        )
    else:
        raise PortfolioExportError(
            "atomic_publish_unavailable",
            "no supported no-replace rename primitive is available",
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            destination_name,
        )
    if error_number in {
        errno.ENOSYS,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }:
        raise PortfolioExportError(
            "atomic_publish_unavailable",
            "filesystem does not support no-replace atomic publication",
        )
    raise OSError(error_number, os.strerror(error_number), destination_name)


def _create_owned_temporary(
    root_descriptor: int,
    run_id: str,
) -> tuple[str, int, os.stat_result]:
    for _attempt in range(128):
        name = f".{run_id}.{secrets.token_hex(8)}.tmp"
        descriptor: int | None = None
        try:
            os.mkdir(name, 0o700, dir_fd=root_descriptor)
        except FileExistsError:
            continue
        try:
            entry_metadata = os.stat(
                name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            descriptor = os.open(
                name,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                dir_fd=root_descriptor,
            )
            opened_metadata = os.fstat(descriptor)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise PortfolioExportError(
                "unsafe_temporary_directory",
                "portfolio temporary directory cannot be opened safely",
            ) from exc
        if not stat.S_ISDIR(entry_metadata.st_mode) or not _same_object(
            entry_metadata, opened_metadata
        ):
            os.close(descriptor)
            raise PortfolioExportError(
                "unsafe_temporary_directory",
                "portfolio temporary directory binding changed",
            )
        return name, descriptor, opened_metadata
    raise PortfolioExportError(
        "temporary_name_exhausted",
        "portfolio temporary directory name attempts were exhausted",
    )


def _verify_directory_entry(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result,
    *,
    code: str,
    message: str,
) -> None:
    try:
        current = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise PortfolioExportError(code, message) from exc
    if not stat.S_ISDIR(current.st_mode) or not _same_object(current, expected):
        raise PortfolioExportError(code, message)


def _remove_owned_temporary(
    root_descriptor: int,
    temporary_name: str,
    temporary_descriptor: int,
    *,
    metadata: os.stat_result,
    expected_payloads: Mapping[str, os.stat_result],
) -> None:
    try:
        current = os.stat(
            temporary_name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PortfolioExportError(
            "unsafe_temporary_cleanup",
            "portfolio temporary directory cannot be inspected safely",
        ) from exc
    if not stat.S_ISDIR(current.st_mode) or not _same_object(current, metadata):
        raise PortfolioExportError(
            "unsafe_temporary_cleanup",
            "portfolio temporary directory ownership changed",
        )
    try:
        names = set(os.listdir(temporary_descriptor))
    except OSError as exc:
        raise PortfolioExportError(
            "unsafe_temporary_cleanup",
            "portfolio temporary directory cannot be listed safely",
        ) from exc
    if not names.issubset(expected_payloads):
        raise PortfolioExportError(
            "unsafe_temporary_cleanup",
            "portfolio temporary directory contains unexpected entries",
        )
    for name in sorted(names):
        try:
            entry = os.stat(
                name,
                dir_fd=temporary_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(entry.st_mode)
                or entry.st_nlink != 1
                or not _same_object(entry, expected_payloads[name])
            ):
                raise PortfolioExportError(
                    "unsafe_temporary_cleanup",
                    "portfolio temporary payload ownership changed",
                )
        except PortfolioExportError:
            raise
        except OSError as exc:
            raise PortfolioExportError(
                "unsafe_temporary_cleanup",
                "portfolio temporary payload cannot be verified safely",
            ) from exc
    for name in sorted(names):
        try:
            os.unlink(name, dir_fd=temporary_descriptor)
        except OSError as exc:
            raise PortfolioExportError(
                "unsafe_temporary_cleanup",
                "portfolio temporary payload cannot be removed safely",
            ) from exc
    try:
        os.fsync(temporary_descriptor)
    except OSError as exc:
        raise PortfolioExportError(
            "unsafe_temporary_cleanup",
            "portfolio temporary directory cleanup cannot be synced",
        ) from exc
    _verify_directory_entry(
        root_descriptor,
        temporary_name,
        metadata,
        code="unsafe_temporary_cleanup",
        message="portfolio temporary directory ownership changed",
    )
    # POSIX has no portable rmdir-by-fd primitive. Removing the final empty
    # directory by name would reopen an inode-check/use race, so failures
    # conservatively retain this validated, empty, hidden directory.


def _entry_exists(parent_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PortfolioExportError(
            "artifact_conflict",
            "portfolio artifact target cannot be inspected safely",
        ) from exc
    return True


def export_portfolio_run(
    run: VerifiedPortfolioRun,
    output_root: str | Path,
) -> Path:
    """Atomically publish or byte-verify one deterministic audit bundle."""
    verify_portfolio_run(run)
    with _open_safe_output_root(output_root) as (
        root,
        root_descriptor,
        root_metadata,
    ):
        expected = _complete_bundle_bytes(run)
        _verify_output_root_binding(root, root_descriptor, root_metadata)
        target_name = run.identity.run_id
        target = root / target_name

        with _run_lock(root_descriptor, run.identity.run_id):
            _verify_output_root_binding(
                root,
                root_descriptor,
                root_metadata,
            )
            if _entry_exists(root_descriptor, target_name):
                _verify_existing_bundle(
                    root_descriptor,
                    target_name,
                    expected,
                )
                _verify_output_root_binding(
                    root,
                    root_descriptor,
                    root_metadata,
                )
                return target
            (
                temporary_name,
                temporary_descriptor,
                temporary_metadata,
            ) = _create_owned_temporary(
                root_descriptor,
                run.identity.run_id,
            )
            temporary_payloads: dict[str, os.stat_result] = {}
            try:
                _write_bundle(
                    temporary_descriptor,
                    expected,
                    temporary_payloads,
                )
                _verify_directory_entry(
                    root_descriptor,
                    temporary_name,
                    temporary_metadata,
                    code="unsafe_temporary_directory",
                    message="portfolio temporary directory binding changed",
                )
                try:
                    _rename_no_replace(
                        root_descriptor,
                        temporary_name,
                        root_descriptor,
                        target_name,
                    )
                except FileExistsError:
                    _verify_existing_bundle(
                        root_descriptor,
                        target_name,
                        expected,
                    )
                    _verify_output_root_binding(
                        root,
                        root_descriptor,
                        root_metadata,
                    )
                    return target
                except PortfolioExportError:
                    raise
                except OSError as exc:
                    raise PortfolioExportError(
                        "atomic_publish_failed",
                        "portfolio bundle could not be atomically published",
                    ) from exc
                _verify_directory_entry(
                    root_descriptor,
                    target_name,
                    temporary_metadata,
                    code="atomic_publish_failed",
                    message="published portfolio bundle binding changed",
                )
                try:
                    os.fsync(root_descriptor)
                except OSError as exc:
                    raise PortfolioExportError(
                        "parent_fsync_failed",
                        "portfolio output root could not be synced",
                    ) from exc
                _verify_existing_bundle(
                    root_descriptor,
                    target_name,
                    expected,
                )
                _verify_output_root_binding(
                    root,
                    root_descriptor,
                    root_metadata,
                )
                return target
            finally:
                try:
                    _remove_owned_temporary(
                        root_descriptor,
                        temporary_name,
                        temporary_descriptor,
                        metadata=temporary_metadata,
                        expected_payloads=temporary_payloads,
                    )
                finally:
                    os.close(temporary_descriptor)
