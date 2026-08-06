"""Deterministic single-instrument Backtrader baseline runner."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path

import backtrader as bt
import pandas as pd

from aquant.backtest.data_access import VerifiedMarketData
from aquant.backtest.execution import RuleAwareBackBroker
from aquant.backtest.feed import (
    BacktestDataError,
    canonical_market_digest,
    make_research_feed,
)
from aquant.backtest.models import (
    BacktestConfig,
    BacktestResult,
    CorporateActionRunProvenance,
    DataProvenance,
    FeeRateRecord,
    PositionLotRecord,
    RuleProvenance,
    StrategyName,
)
from aquant.backtest.price_streams import PRICE_STREAM_VERSION, derive_price_streams
from aquant.backtest.strategies import BuyAndHoldStrategy, SmaStrategy
from aquant.data.calendar_snapshot import (
    CalendarError,
    VerifiedTradingCalendar,
    verify_trading_calendar,
)
from aquant.data.corporate_actions import (
    CorporateActionError,
    CorporateActionEvent,
    VerifiedCorporateActions,
    make_synthetic_corporate_actions,
    verify_verified_corporate_actions,
)
from aquant.rules import (
    FeePolicyError,
    InstrumentKind,
    VerifiedFeePolicy,
    verify_fee_policy,
)
from aquant.universe import (
    UniverseError,
    VerifiedUniverse,
    verify_universe,
)

_SCHEMA_VERSION = "2.1"
_DIVIDEND_TAX_MODE = "gross_before_personal_tax"
_ENGINE = f"backtrader-{bt.__version__}"
_IMPLEMENTATION_FILES = (
    "__init__.py",
    "data_access.py",
    "execution.py",
    "export.py",
    "feed.py",
    "models.py",
    "price_streams.py",
    "runner.py",
    "strategies.py",
    "../backtest_cli.py",
    "../data/akshare_client.py",
    "../data/calendar_snapshot.py",
    "../data/corporate_actions.py",
    "../data/manifest.py",
    "../data/normalize.py",
    "../data/quality.py",
    "../data/snapshot.py",
    "../rules/__init__.py",
    "../rules/engine.py",
    "../rules/fees.py",
    "../rules/lots.py",
    "../rules/models.py",
    "../rules/price_limits.py",
    "../config.py",
    "../universe.py",
)


def _implementation_digest() -> str:
    digest = hashlib.sha256()
    module_directory = Path(__file__).parent
    for filename in _IMPLEMENTATION_FILES:
        digest.update(filename.encode())
        digest.update(b"\0")
        digest.update((module_directory / filename).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _run_id(
    *,
    provenance: DataProvenance,
    config: BacktestConfig,
    input_digest: str,
    implementation_digest: str,
    universe_id: str | None,
    rule_identity: dict[str, str] | None = None,
    corporate_action_identity: dict[str, object],
) -> str:
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "engine": _ENGINE,
        "implementation_digest": implementation_digest,
        "input_digest": input_digest,
        "universe_id": universe_id,
        "provenance": asdict(provenance),
        "config": {
            **asdict(config),
            "strategy": config.strategy.value,
            "target_weight": str(config.target_weight),
        },
        "rule_identity": rule_identity,
        "corporate_action_identity": corporate_action_identity,
        "price_stream_version": PRICE_STREAM_VERSION,
        "dividend_tax_mode": _DIVIDEND_TAX_MODE,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _assert_accounting_identity(result: BacktestResult) -> None:
    if not (
        len(result.positions)
        == len(result.cash_ledger)
        == len(result.equity_curve)
        == len(result.receivables)
    ):
        raise RuntimeError("backtest ledgers have inconsistent row counts")
    for position, cash, equity, receivable in zip(
        result.positions,
        result.cash_ledger,
        result.equity_curve,
        result.receivables,
        strict=True,
    ):
        if (
            position.date != cash.date
            or cash.date != equity.date
            or equity.date != receivable.date
        ):
            raise RuntimeError("backtest ledgers have inconsistent dates")
        expected = cash.cash + position.market_value + receivable.balance
        if not math.isclose(equity.equity, expected, rel_tol=1e-12, abs_tol=1e-8):
            raise RuntimeError("cash plus position market value does not equal equity")


def _execute_frame(
    frame: pd.DataFrame,
    *,
    provenance: DataProvenance,
    config: BacktestConfig,
    expected_input_digest: str,
    calendar: VerifiedTradingCalendar | None = None,
    fee_policy: VerifiedFeePolicy | None = None,
    instrument_kind: InstrumentKind | None = None,
    corporate_actions: VerifiedCorporateActions,
    universe: VerifiedUniverse | None = None,
) -> BacktestResult:
    if type(config) is not BacktestConfig:
        raise TypeError("config must be an exact BacktestConfig")
    if provenance.adjustment != "":
        raise BacktestDataError(
            "adjusted_price_forbidden",
            "only verified unadjusted prices may enter execution and accounting",
        )
    if universe is not None:
        try:
            verify_universe(universe)
        except UniverseError as exc:
            raise BacktestDataError(
                "unverified_universe",
                "formal backtests require an exact verified universe",
            ) from exc
        if not universe.contains(
            provenance.symbol,
            provenance.instrument_kind.value,
        ):
            raise BacktestDataError(
                "universe_contract_mismatch",
                "market data is not a member of the requested universe",
            )
    input_digest = canonical_market_digest(frame)
    if input_digest != expected_input_digest:
        raise BacktestDataError(
            "verified_data_modified",
            "market data no longer matches its verified canonical digest",
        )
    try:
        verify_verified_corporate_actions(corporate_actions)
    except CorporateActionError as exc:
        raise BacktestDataError(
            "unverified_corporate_actions",
            "corporate actions failed the exact verification boundary",
        ) from exc
    action_provenance = corporate_actions.provenance
    assert action_provenance is not None
    frame_start = frame["date"].dt.date.iloc[0]
    frame_end = frame["date"].dt.date.iloc[-1]
    if (
        action_provenance.symbol != provenance.symbol
        or action_provenance.instrument_kind is not provenance.instrument_kind
        or action_provenance.coverage_start > frame_start
        or action_provenance.coverage_end < frame_end
    ):
        raise BacktestDataError(
            "corporate_action_contract_mismatch",
            "corporate-action identity or coverage does not match market data",
        )
    if (
        corporate_actions.events
        and calendar is None
        and fee_policy is None
        and instrument_kind is None
    ):
        raise BacktestDataError(
            "rules_required_for_corporate_actions",
            "non-empty corporate actions require the rule-aware broker",
        )
    if calendar is not None:
        try:
            verify_trading_calendar(calendar)
        except CalendarError as exc:
            raise BacktestDataError(
                "verified_calendar_modified",
                "verified calendar no longer matches its loader identity",
            ) from exc
    if fee_policy is not None:
        try:
            verify_fee_policy(fee_policy)
        except FeePolicyError as exc:
            raise BacktestDataError(
                "verified_fee_policy_modified",
                "verified fee policy no longer matches its factory identity",
            ) from exc

    implementation_digest = _implementation_digest()

    cerebro = bt.Cerebro(stdstats=False, cheat_on_open=False)
    if calendar is None and fee_policy is None and instrument_kind is None:
        broker = cerebro.broker
        broker.setcommission(commission=0.0)
    elif (
        type(calendar) is VerifiedTradingCalendar
        and type(fee_policy) is VerifiedFeePolicy
        and type(instrument_kind) is InstrumentKind
    ):
        broker = RuleAwareBackBroker(
            checksubmit=False,
            verified_calendar=calendar,
            verified_fee_policy=fee_policy,
            instrument_symbol=provenance.symbol,
            instrument_kind=instrument_kind,
            available_bar_dates=tuple(frame["date"].dt.date),
            verified_corporate_actions=corporate_actions,
        )
        cerebro.setbroker(broker)
    else:
        raise TypeError("calendar, fee policy, and instrument kind must be supplied together")
    broker.setcash(float(config.initial_cash))
    broker.set_coc(False)
    feed_frame = derive_price_streams(frame, corporate_actions)
    feed = make_research_feed(feed_frame, name=provenance.symbol)
    cerebro.adddata(feed)
    if isinstance(broker, RuleAwareBackBroker):
        broker.bind_market_data(feed)

    strategy_class = (
        BuyAndHoldStrategy
        if config.strategy is StrategyName.BUY_AND_HOLD
        else SmaStrategy
    )
    cerebro.addstrategy(
        strategy_class,
        target_weight=config.target_weight,
        sma_period=config.sma_period,
    )
    strategies = cerebro.run(runonce=False, preload=True)
    strategy = strategies[0]
    if isinstance(broker, RuleAwareBackBroker):
        for rejected_order in broker.finalize_pending_orders():
            strategy.notify_order(rejected_order)

    rule_identity = (
        {
            "calendar_id": calendar.calendar_id,
            "calendar_sha256": calendar.file_sha256,
            "fee_policy_digest": fee_policy.policy_digest,
            "instrument_kind": instrument_kind.value,
        }
        if calendar is not None
        and fee_policy is not None
        and instrument_kind is not None
        else None
    )
    rule_provenance = (
        RuleProvenance(**rule_identity) if rule_identity is not None else None
    )
    action_identity = {
        "snapshot_id": action_provenance.snapshot_id,
        "file_sha256": action_provenance.file_sha256,
        "symbol": action_provenance.symbol,
        "instrument_kind": action_provenance.instrument_kind.value,
        "provider": action_provenance.provider,
        "source_schema": action_provenance.source_schema,
        "normalization_version": action_provenance.normalization_version,
        "coverage_start": action_provenance.coverage_start.isoformat(),
        "coverage_end": action_provenance.coverage_end.isoformat(),
        "row_count": action_provenance.row_count,
        "verification_method": action_provenance.verification_method,
    }
    action_run_provenance = CorporateActionRunProvenance(
        snapshot_id=action_provenance.snapshot_id,
        file_sha256=action_provenance.file_sha256,
        symbol=action_provenance.symbol,
        instrument_kind=action_provenance.instrument_kind.value,
        provider=action_provenance.provider,
        source_schema=action_provenance.source_schema,
        normalization_version=action_provenance.normalization_version,
        coverage_start=action_provenance.coverage_start,
        coverage_end=action_provenance.coverage_end,
        row_count=action_provenance.row_count,
        verification_method=action_provenance.verification_method,
    )
    lots = (
        tuple(
            PositionLotRecord(
                lot_id=lot.lot_id,
                symbol=lot.symbol,
                acquired_date=lot.acquired_date,
                available_date=lot.available_date,
                original_size=lot.original_size,
                remaining_size=lot.remaining_size,
                unit_cost=str(lot.unit_cost),
            )
            for lot in broker.audited_lots()
        )
        if isinstance(broker, RuleAwareBackBroker)
        else ()
    )
    touched_fee_rates = (
        tuple(
            FeeRateRecord(
                fee_name=touch.fee_name,
                effective_date=touch.effective_date,
                rate=str(touch.rate),
                minimum_yuan=(
                    str(touch.minimum_yuan)
                    if touch.minimum_yuan is not None
                    else None
                ),
            )
            for touch in broker.audited_touched_rates()
        )
        if isinstance(broker, RuleAwareBackBroker)
        else ()
    )
    frame_dates = frozenset(frame["date"].dt.date)
    missing_market_sessions = (
        tuple(
            value
            for value in calendar.dates
            if frame["date"].dt.date.iloc[0] <= value <= frame["date"].dt.date.iloc[-1]
            and value not in frame_dates
        )
        if calendar is not None
        else ()
    )
    result = BacktestResult(
        schema_version=_SCHEMA_VERSION,
        run_id=_run_id(
            provenance=provenance,
            config=config,
            input_digest=input_digest,
            implementation_digest=implementation_digest,
            universe_id=(
                universe.universe_id
                if universe is not None
                else None
            ),
            rule_identity=rule_identity,
            corporate_action_identity=action_identity,
        ),
        engine=_ENGINE,
        implementation_digest=implementation_digest,
        input_digest=input_digest,
        provenance=provenance,
        config=config,
        orders=strategy.audited_orders(),
        fills=strategy.audited_fills(),
        positions=strategy.audited_positions(),
        cash_ledger=strategy.audited_cash(),
        equity_curve=strategy.audited_equity(),
        rule_provenance=rule_provenance,
        lots=lots,
        missing_market_sessions=missing_market_sessions,
        touched_fee_rates=touched_fee_rates,
        receivables=strategy.audited_receivables(),
        corporate_action_ledger=(
            broker.audited_corporate_actions()
            if isinstance(broker, RuleAwareBackBroker)
            else ()
        ),
        corporate_action_provenance=action_run_provenance,
        price_stream_version=PRICE_STREAM_VERSION,
        dividend_tax_mode=_DIVIDEND_TAX_MODE,
        universe_id=(
            universe.universe_id
            if universe is not None
            else None
        ),
    )
    _assert_accounting_identity(result)
    return result


def run_backtest(
    market_data: VerifiedMarketData,
    *,
    universe: VerifiedUniverse,
    corporate_actions: VerifiedCorporateActions,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
    config: BacktestConfig,
) -> BacktestResult:
    """Run only data produced by the manifest and SHA-256 verification boundary."""
    if type(market_data) is not VerifiedMarketData:
        raise TypeError("market_data must be an exact VerifiedMarketData")
    if type(universe) is not VerifiedUniverse:
        raise TypeError("universe must be an exact VerifiedUniverse")
    if market_data.provenance.verification_method != "manifest_sha256":
        raise BacktestDataError(
            "unverified_market_data",
            "formal backtests require manifest and SHA-256 verification",
        )
    if type(calendar) is not VerifiedTradingCalendar:
        raise TypeError("calendar must be an exact VerifiedTradingCalendar")
    if type(fee_policy) is not VerifiedFeePolicy:
        raise TypeError("fee_policy must be an exact VerifiedFeePolicy")
    if type(corporate_actions) is not VerifiedCorporateActions:
        raise TypeError(
            "corporate_actions must be exact VerifiedCorporateActions"
        )
    if corporate_actions.provenance is None or (
        corporate_actions.provenance.verification_method != "manifest_sha256"
    ):
        raise BacktestDataError(
            "unverified_corporate_actions",
            "formal backtests require manifest and SHA-256 verified actions",
        )
    return _execute_frame(
        market_data.frame,
        provenance=market_data.provenance,
        config=config,
        expected_input_digest=market_data.input_digest,
        calendar=calendar,
        fee_policy=fee_policy,
        instrument_kind=market_data.provenance.instrument_kind,
        corporate_actions=corporate_actions,
        universe=universe,
    )


def run_synthetic_backtest(
    frame: pd.DataFrame,
    *,
    config: BacktestConfig,
    symbol: str = "600519",
    calendar: VerifiedTradingCalendar | None = None,
    fee_policy: VerifiedFeePolicy | None = None,
    instrument_kind: InstrumentKind | None = None,
    corporate_action_events: tuple[CorporateActionEvent, ...] = (),
) -> BacktestResult:
    """Run an explicitly labelled synthetic fixture for engineering tests only."""
    input_digest = canonical_market_digest(frame)
    snapshot_id = hashlib.sha256(
        f"synthetic\0{symbol}\0{input_digest}".encode()
    ).hexdigest()
    resolved_kind = instrument_kind or (
        InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF
        if symbol.startswith("5")
        else InstrumentKind.MAIN_BOARD_STOCK
    )
    provenance = DataProvenance(
        symbol=symbol,
        snapshot_id=snapshot_id,
        file_sha256=input_digest,
        adjustment="",
        verification_method="synthetic_digest",
        instrument_kind=resolved_kind,
    )
    actions = make_synthetic_corporate_actions(
        corporate_action_events,
        symbol=symbol,
        instrument_kind=resolved_kind,
        coverage_start=frame["date"].dt.date.iloc[0],
        coverage_end=frame["date"].dt.date.iloc[-1],
    )
    return _execute_frame(
        frame,
        provenance=provenance,
        config=config,
        expected_input_digest=input_digest,
        calendar=calendar,
        fee_policy=fee_policy,
        instrument_kind=instrument_kind,
        corporate_actions=actions,
    )
