"""Minimal verified Data -> Signal -> Planner -> Portfolio research loop."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from aquant.backtest import EquityRecord, FillRecord, PositionRecord, VerifiedMarketData
from aquant.backtest.price_streams import PRICE_STREAM_VERSION, derive_price_streams
from aquant.data.calendar_snapshot import (
    VerifiedTradingCalendar,
    verify_trading_calendar,
)
from aquant.data.corporate_actions import (
    VerifiedCorporateActions,
    verify_verified_corporate_actions,
)
from aquant.planner import (
    NoPreviousState,
    NoPreviousStateReason,
    PlannedTargets,
    PlannerLimits,
    PreviousTargets,
    plan_targets,
)
from aquant.portfolio import (
    CashEventKind,
    CashLedgerEvent,
    CashReceivable,
    PortfolioError,
    SymbolValuation,
    actual_cash_date,
    decimal_yuan_to_fen,
)
from aquant.research.signals import SignalInput, SignalObservation, SmaSignal
from aquant.risk.metrics import RiskMetrics, compute_risk_metrics
from aquant.rolling import (
    RebalanceAttempt,
    RollingConfig,
    RollingExecutionInput,
    RollingPortfolioLedger,
    SellFillEvent,
    close_rolling_session,
    create_rolling_ledger,
    rebalance_to_plan,
    verify_rolling_ledger,
)
from aquant.rules import (
    InstrumentKind,
    OrderSide,
    VerifiedFeePolicy,
    verify_fee_policy,
)
from aquant.universe import VerifiedUniverse, verify_universe

RESEARCH_LOOP_SCHEMA_VERSION = "1.0.0"
_IMPLEMENTATION_FILES = (
    "experiment_cli.py",
    "research/loop.py",
    "research/report.py",
    "research/signals.py",
    "planner/core.py",
    "rolling/accounting.py",
    "rolling/orchestration.py",
    "backtest/price_streams.py",
    "risk/metrics.py",
)


class ResearchLoopError(ValueError):
    """A fail-closed Research Loop v1 contract error."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ResearchLoopConfig:
    """Fixed P0 settings for one single-symbol daily research run."""

    symbol: str
    initial_cash_fen: int
    sma_period: int = 20
    active_weight: Decimal = Decimal("0.95")
    limits: PlannerLimits = PlannerLimits(
        max_single_weight=Decimal("0.95"),
        max_gross=Decimal("0.95"),
        min_cash_ratio=Decimal("0.05"),
    )

    def __post_init__(self) -> None:
        if (
            type(self.symbol) is not str
            or len(self.symbol) != 6
            or not self.symbol.isdigit()
            or type(self.initial_cash_fen) is not int
            or isinstance(self.initial_cash_fen, bool)
            or self.initial_cash_fen <= 0
            or type(self.sma_period) is not int
            or self.sma_period < 2
            or type(self.active_weight) is not Decimal
            or not self.active_weight.is_finite()
            or not Decimal("0") < self.active_weight <= Decimal("1")
            or type(self.limits) is not PlannerLimits
            or self.active_weight > self.limits.max_single_weight
            or self.active_weight > self.limits.max_gross
            or self.active_weight + self.limits.min_cash_ratio > Decimal("1")
        ):
            raise ResearchLoopError(
                "invalid_config",
                "research loop configuration is invalid",
            )


@dataclass(frozen=True)
class SignalDecision:
    session: date
    data_available: bool
    output: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True)
class DividendEventAudit:
    event_id: str
    symbol: str
    ex_date: date
    source_payable_date: date
    actual_cash_date: date
    entitled_size: int
    cash_dividend_per_unit: Decimal
    amount_fen: int


@dataclass(frozen=True)
class ResearchPathResult:
    label: str
    ledger: RollingPortfolioLedger
    plans: tuple[PlannedTargets, ...]
    decisions: tuple[SignalDecision, ...]
    attempts: tuple[RebalanceAttempt, ...]
    dividends: tuple[DividendEventAudit, ...]
    missing_market_sessions: tuple[date, ...]
    mark_prices: tuple[tuple[date, Decimal], ...]
    equity_curve: tuple[EquityRecord, ...]
    positions: tuple[PositionRecord, ...]
    fills: tuple[FillRecord, ...]
    metrics: RiskMetrics

    @property
    def transaction_count(self) -> int:
        return len(self.fills)


@dataclass(frozen=True)
class ResearchLoopResult:
    schema_version: str
    run_id: str
    implementation_digest: str
    input_digest: str
    market_snapshot_id: str
    market_file_sha256: str
    corporate_action_snapshot_id: str
    corporate_action_file_sha256: str
    calendar_id: str
    calendar_file_sha256: str
    universe_id: str
    fee_policy_digest: str
    price_stream_version: str
    config: ResearchLoopConfig
    instrument_kind: InstrumentKind
    simulation_start: date
    simulation_end: date
    settlement_buffer_session: date
    strategy: ResearchPathResult
    benchmark: ResearchPathResult


@dataclass(frozen=True)
class _Bar:
    session: date
    open: Decimal
    close: Decimal
    indicator_close: float
    reference_price: Decimal | None


def _implementation_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for relative in _IMPLEMENTATION_FILES:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _decimal_price(value: object, *, allow_missing: bool = False) -> Decimal | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ResearchLoopError("invalid_market_price", "market price is invalid") from exc
    if allow_missing and math.isnan(numeric):
        return None
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ResearchLoopError("invalid_market_price", "market price is invalid")
    return result


def _bar_map(
    market_data: VerifiedMarketData,
    actions: VerifiedCorporateActions,
) -> dict[date, _Bar]:
    enriched = derive_price_streams(market_data.frame, actions)
    bars: dict[date, _Bar] = {}
    for row in enriched.itertuples(index=False):
        session = row.date.date()
        bars[session] = _Bar(
            session=session,
            open=_decimal_price(row.open),  # type: ignore[arg-type]
            close=_decimal_price(row.close),  # type: ignore[arg-type]
            indicator_close=float(row.indicator_close),
            reference_price=_decimal_price(row.reference_price, allow_missing=True),
        )
    return bars


def _validated_sessions(
    bars: dict[date, _Bar],
    calendar: VerifiedTradingCalendar,
) -> tuple[tuple[date, ...], date]:
    bar_dates = tuple(sorted(bars))
    if len(bar_dates) < 2:
        raise ResearchLoopError(
            "insufficient_history",
            "research loop requires at least two market observations",
        )
    official = tuple(
        session for session in calendar.dates if bar_dates[0] <= session <= bar_dates[-1]
    )
    if not official or official[0] != bar_dates[0] or official[-1] != bar_dates[-1]:
        raise ResearchLoopError(
            "calendar_market_mismatch",
            "market endpoints must be official verified sessions",
        )
    next_after_market = calendar.next_session(official[-1])
    if next_after_market is not None:
        simulation = official
        buffer_session = next_after_market
    else:
        if len(official) < 3:
            raise ResearchLoopError(
                "missing_settlement_buffer",
                "calendar needs one session beyond the final simulated close",
            )
        simulation = official[:-1]
        buffer_session = official[-1]
    if len(simulation) < 2:
        raise ResearchLoopError(
            "insufficient_history",
            "research loop requires two simulated sessions",
        )
    return simulation, buffer_session


def _entitled_size(
    ledger: RollingPortfolioLedger,
    *,
    symbol: str,
    record_date: date,
) -> int:
    return sum(
        lot.remaining_size
        for lot in ledger.lots
        if lot.symbol == symbol and lot.acquired_date <= record_date and lot.remaining_size > 0
    )


def _session_dividends(
    ledger: RollingPortfolioLedger,
    *,
    session: date,
    symbol: str,
    actions: VerifiedCorporateActions,
    calendar: VerifiedTradingCalendar,
) -> tuple[tuple[CashReceivable, ...], tuple[DividendEventAudit, ...]]:
    receivables: list[CashReceivable] = []
    audits: list[DividendEventAudit] = []
    events = sorted(
        (
            event
            for event in actions.events
            if event.symbol == symbol
            and event.ex_date == session
            and event.cash_dividend_per_unit > 0
        ),
        key=lambda event: event.event_id,
    )
    for event in events:
        entitled = _entitled_size(
            ledger,
            symbol=symbol,
            record_date=event.record_date,
        )
        cash_session = actual_cash_date(calendar, event.payable_date)
        amount_fen = decimal_yuan_to_fen(event.cash_dividend_per_unit * entitled)
        audits.append(
            DividendEventAudit(
                event_id=event.event_id,
                symbol=symbol,
                ex_date=session,
                source_payable_date=event.payable_date,
                actual_cash_date=cash_session,
                entitled_size=entitled,
                cash_dividend_per_unit=event.cash_dividend_per_unit,
                amount_fen=amount_fen,
            )
        )
        if amount_fen:
            receivables.append(
                CashReceivable(
                    event_id=event.event_id,
                    symbol=symbol,
                    registered_date=session,
                    source_payable_date=event.payable_date,
                    actual_cash_date=cash_session,
                    amount_fen=amount_fen,
                )
            )
    return tuple(receivables), tuple(audits)


def _register_receivables(
    ledger: RollingPortfolioLedger,
    receivables: tuple[CashReceivable, ...],
) -> RollingPortfolioLedger:
    current = ledger
    for receivable in receivables:
        if receivable.event_id in {item.event_id for item in current.receivables}:
            raise PortfolioError(
                "duplicate_receivable",
                "corporate-action event is already registered",
            )
        current = replace(
            current,
            receivables=current.receivables + (receivable,),
        )
        verify_rolling_ledger(current)
    return current


def _pay_receivables(
    ledger: RollingPortfolioLedger,
    session: date,
) -> RollingPortfolioLedger:
    verify_rolling_ledger(ledger)
    if any(
        item.paid_date is None and item.actual_cash_date < session for item in ledger.receivables
    ):
        raise PortfolioError(
            "overdue_receivable",
            "an earlier receivable payment session was skipped",
        )
    due = tuple(
        sorted(
            (
                item
                for item in ledger.receivables
                if item.paid_date is None and item.actual_cash_date == session
            ),
            key=lambda item: (item.symbol, item.event_id),
        )
    )
    if not due:
        return ledger
    events = list(ledger.cash_events)
    cash = ledger.cash_fen
    paid_ids: set[str] = set()
    existing = {item.event_id for item in events}
    for item in due:
        event_id = f"cash:{item.event_id}"
        if event_id in existing:
            raise PortfolioError("duplicate_event", "dividend cash event already exists")
        event = CashLedgerEvent(
            event_id=event_id,
            event_kind=CashEventKind.DIVIDEND_PAYMENT,
            session=session,
            side=None,
            symbol=item.symbol,
            reference_id=item.event_id,
            notional_fen=item.amount_fen,
            commission_fen=0,
            stamp_duty_fen=0,
            transfer_fee_fen=0,
            cash_before_fen=cash,
            cash_after_fen=cash + item.amount_fen,
        )
        events.append(event)
        existing.add(event_id)
        paid_ids.add(item.event_id)
        cash = event.cash_after_fen
    updated = replace(
        ledger,
        cash_fen=cash,
        cash_events=tuple(events),
        receivables=tuple(
            replace(item, paid_date=session) if item.event_id in paid_ids else item
            for item in ledger.receivables
        ),
    )
    verify_rolling_ledger(updated)
    return updated


def _valuations(
    ledger: RollingPortfolioLedger,
    *,
    session: date,
    mark: Decimal,
) -> tuple[SymbolValuation, ...]:
    sizes: dict[str, list[int]] = {}
    for lot in ledger.lots:
        if lot.remaining_size == 0 or lot.acquired_date > session:
            continue
        values = sizes.setdefault(lot.symbol, [0, 0])
        values[0] += lot.remaining_size
        if lot.available_date <= session:
            values[1] += lot.remaining_size
    return tuple(
        SymbolValuation(
            symbol=symbol,
            total_size=values[0],
            available_size=values[1],
            locked_size=values[0] - values[1],
            mark_price=mark,
        )
        for symbol, values in sorted(sizes.items())
    )


def _execution_inputs(
    planned: PlannedTargets,
    *,
    execution_session: date,
    bar: _Bar | None,
    instrument_kind: InstrumentKind,
) -> tuple[RollingExecutionInput, ...]:
    return tuple(
        RollingExecutionInput(
            symbol=symbol,
            instrument_kind=instrument_kind,
            intent_session=planned.as_of,
            execution_session=execution_session,
            previous_close=bar.reference_price if bar is not None else None,
            execution_open=bar.open if bar is not None else None,
        )
        for symbol in planned.targets
    )


def _fill_records(ledger: RollingPortfolioLedger) -> tuple[FillRecord, ...]:
    lots = {lot.lot_id: lot for lot in ledger.lots}
    records: list[FillRecord] = []
    for event in ledger.cash_events:
        if type(event) is CashLedgerEvent and event.event_kind is CashEventKind.FILL:
            lot = lots[event.reference_id]
            side = OrderSide.BUY.value
            size = lot.original_size
            price = lot.unit_cost
        elif type(event) is SellFillEvent:
            side = OrderSide.SELL.value
            size = event.size
            price = event.unit_price
        else:
            continue
        records.append(
            FillRecord(
                order_id=event.event_id,
                execution_date=event.session,
                side=side,
                size=size,
                price=float(price),
                value=event.notional_fen / 100.0,
                commission=event.total_fees_fen / 100.0,
                commission_fen=event.commission_fen,
                stamp_duty_fen=event.stamp_duty_fen,
                transfer_fee_fen=event.transfer_fee_fen,
                total_fees_fen=event.total_fees_fen,
            )
        )
    return tuple(records)


def _metric_ledgers(
    ledger: RollingPortfolioLedger,
    marks: tuple[tuple[date, Decimal], ...],
) -> tuple[tuple[EquityRecord, ...], tuple[PositionRecord, ...], tuple[FillRecord, ...]]:
    mark_by_session = dict(marks)
    equity = tuple(
        EquityRecord(date=item.session, equity=item.equity_fen / 100.0)
        for item in ledger.daily_snapshots
    )
    positions = tuple(
        PositionRecord(
            date=item.session,
            size=sum(value.total_size for value in item.valuations),
            close=float(mark_by_session[item.session]),
            market_value=item.position_market_value_fen / 100.0,
            available_size=sum(value.available_size for value in item.valuations),
            locked_size=sum(value.locked_size for value in item.valuations),
        )
        for item in ledger.daily_snapshots
    )
    return equity, positions, _fill_records(ledger)


PlanFactory = Callable[
    [date, bool, tuple[SignalObservation, ...], PlannedTargets | None],
    tuple[PlannedTargets | None, SignalDecision | None],
]


def _simulate_path(
    *,
    label: str,
    config: ResearchLoopConfig,
    sessions: tuple[date, ...],
    bars: dict[date, _Bar],
    actions: VerifiedCorporateActions,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
    instrument_kind: InstrumentKind,
    plan_factory: PlanFactory,
) -> ResearchPathResult:
    ledger = create_rolling_ledger(config.initial_cash_fen)
    pending_plan: PlannedTargets | None = None
    previous_plan: PlannedTargets | None = None
    observations: list[SignalObservation] = []
    plans: list[PlannedTargets] = []
    decisions: list[SignalDecision] = []
    attempts: list[RebalanceAttempt] = []
    dividend_audits: list[DividendEventAudit] = []
    missing: list[date] = []
    marks: list[tuple[date, Decimal]] = []
    current_mark = bars[sessions[0]].close
    retry_required = False

    for position, session in enumerate(sessions):
        bar = bars.get(session)
        if bar is None:
            missing.append(session)
        else:
            current_mark = bar.close
            observations.append(
                SignalObservation(
                    session=session,
                    indicator_close=bar.indicator_close,
                )
            )

        receivables, session_audits = _session_dividends(
            ledger,
            session=session,
            symbol=config.symbol,
            actions=actions,
            calendar=calendar,
        )
        dividend_audits.extend(session_audits)

        if pending_plan is not None:
            result = rebalance_to_plan(
                config=RollingConfig(limits=config.limits),
                planned=pending_plan,
                ledger=ledger,
                execution_inputs=_execution_inputs(
                    pending_plan,
                    execution_session=session,
                    bar=bar,
                    instrument_kind=instrument_kind,
                ),
                calendar=calendar,
                fee_policy=fee_policy,
            )
            ledger = result.ledger
            attempts.extend(result.attempts)
            retry_required = any(not target.is_aligned for target in result.targets)
        else:
            retry_required = False

        # A2 validates an uninterrupted T-close -> T+1-open transition. Dividend
        # cash is therefore posted after the same-session open, conservatively
        # preventing that cash from funding the opening rebalance.
        ledger = _register_receivables(ledger, receivables)
        ledger = _pay_receivables(ledger, session)
        ledger = close_rolling_session(
            ledger,
            session,
            _valuations(ledger, session=session, mark=current_mark),
        )
        marks.append((session, current_mark))

        terminal = position == len(sessions) - 1
        generated_plan, decision = plan_factory(
            session,
            bar is not None,
            tuple(observations),
            previous_plan,
        )
        if decision is not None:
            decisions.append(decision)
        if generated_plan is not None:
            plans.append(generated_plan)
            target_changed = previous_plan is None or dict(generated_plan.targets) != dict(
                previous_plan.targets
            )
            previous_plan = generated_plan
            pending_plan = generated_plan if target_changed or retry_required else None
        else:
            pending_plan = None
        if terminal:
            pending_plan = None

    verify_rolling_ledger(ledger)
    equity, positions, fills = _metric_ledgers(ledger, tuple(marks))
    metrics = compute_risk_metrics(
        equity_curve=equity,
        positions=positions,
        fills=fills,
    )
    return ResearchPathResult(
        label=label,
        ledger=ledger,
        plans=tuple(plans),
        decisions=tuple(decisions),
        attempts=tuple(attempts),
        dividends=tuple(dividend_audits),
        missing_market_sessions=tuple(missing),
        mark_prices=tuple(marks),
        equity_curve=equity,
        positions=positions,
        fills=fills,
        metrics=metrics,
    )


def _strategy_plan_factory(config: ResearchLoopConfig) -> PlanFactory:
    signal = SmaSignal(config.sma_period, config.active_weight)

    def factory(
        session: date,
        data_available: bool,
        observations: tuple[SignalObservation, ...],
        previous: PlannedTargets | None,
    ) -> tuple[PlannedTargets, SignalDecision]:
        data = SignalInput(
            as_of=session,
            per_symbol={config.symbol: observations},
        )
        output = signal.compute(session, data) if data_available else {}
        planned = plan_targets(
            as_of=session,
            signal_output=output,
            previous=(
                NoPreviousState(NoPreviousStateReason.FIRST_PERIOD)
                if previous is None
                else PreviousTargets(as_of=previous.as_of, targets=previous.targets)
            ),
            eligible_symbols=frozenset({config.symbol}),
            limits=config.limits,
        )
        return planned, SignalDecision(
            session=session,
            data_available=data_available,
            output=tuple(sorted(output.items())),
        )

    return factory


def _benchmark_plan_factory(config: ResearchLoopConfig) -> PlanFactory:
    emitted = False

    def factory(
        session: date,
        data_available: bool,
        _observations: tuple[SignalObservation, ...],
        previous: PlannedTargets | None,
    ) -> tuple[PlannedTargets | None, SignalDecision | None]:
        nonlocal emitted
        if not emitted and not data_available:
            raise ResearchLoopError(
                "benchmark_initial_bar_missing",
                "benchmark requires the first simulated close",
            )
        output = {config.symbol: config.active_weight} if not emitted else {}
        planned = plan_targets(
            as_of=session,
            signal_output=output,
            previous=(
                NoPreviousState(NoPreviousStateReason.FIRST_PERIOD)
                if previous is None
                else PreviousTargets(as_of=previous.as_of, targets=previous.targets)
            ),
            eligible_symbols=frozenset({config.symbol}),
            limits=config.limits,
        )
        emitted = True
        return planned, None

    return factory


def _validate_inputs(
    *,
    config: ResearchLoopConfig,
    market_data: VerifiedMarketData,
    corporate_actions: VerifiedCorporateActions,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
    universe: VerifiedUniverse,
) -> tuple[InstrumentKind, object]:
    if type(config) is not ResearchLoopConfig:
        raise TypeError("config must be an exact ResearchLoopConfig")
    if type(market_data) is not VerifiedMarketData:
        raise TypeError("market_data must be exact VerifiedMarketData")
    verify_verified_corporate_actions(corporate_actions)
    verify_trading_calendar(calendar)
    verify_fee_policy(fee_policy)
    verify_universe(universe)
    provenance = market_data.provenance
    action_provenance = corporate_actions.provenance
    assert action_provenance is not None
    if (
        provenance.symbol != config.symbol
        or action_provenance.symbol != config.symbol
        or action_provenance.instrument_kind is not provenance.instrument_kind
        or not universe.contains(config.symbol, provenance.instrument_kind.value)
    ):
        raise ResearchLoopError(
            "input_identity_mismatch",
            "market, corporate action, universe, and config identities must agree",
        )
    frame = market_data.frame
    start = frame["date"].dt.date.iloc[0]
    end = frame["date"].dt.date.iloc[-1]
    if action_provenance.coverage_start > start or action_provenance.coverage_end < end:
        raise ResearchLoopError(
            "corporate_action_coverage_mismatch",
            "corporate-action coverage must span the market snapshot",
        )
    return provenance.instrument_kind, action_provenance


def _identity_payload(
    *,
    implementation_digest: str,
    market_data: VerifiedMarketData,
    corporate_actions: VerifiedCorporateActions,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
    universe: VerifiedUniverse,
    config: ResearchLoopConfig,
    simulation_start: date,
    simulation_end: date,
    settlement_buffer_session: date,
) -> dict[str, object]:
    action = corporate_actions.provenance
    assert action is not None
    return {
        "schema_version": RESEARCH_LOOP_SCHEMA_VERSION,
        "implementation_digest": implementation_digest,
        "input_digest": market_data.input_digest,
        "market_snapshot_id": market_data.provenance.snapshot_id,
        "market_file_sha256": market_data.provenance.file_sha256,
        "corporate_action_snapshot_id": action.snapshot_id,
        "corporate_action_file_sha256": action.file_sha256,
        "calendar_id": calendar.calendar_id,
        "calendar_file_sha256": calendar.file_sha256,
        "universe_id": universe.universe_id,
        "fee_policy_digest": fee_policy.policy_digest,
        "price_stream_version": PRICE_STREAM_VERSION,
        "simulation_start": simulation_start.isoformat(),
        "simulation_end": simulation_end.isoformat(),
        "settlement_buffer_session": settlement_buffer_session.isoformat(),
        "config": {
            "symbol": config.symbol,
            "initial_cash_fen": config.initial_cash_fen,
            "sma_period": config.sma_period,
            "active_weight": str(config.active_weight),
            "limits": {key: str(value) for key, value in asdict(config.limits).items()},
        },
    }


def run_research_loop(
    *,
    config: ResearchLoopConfig,
    market_data: VerifiedMarketData,
    corporate_actions: VerifiedCorporateActions,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
    universe: VerifiedUniverse,
) -> ResearchLoopResult:
    """Run one deterministic verified SMA path and one buy-and-hold benchmark."""
    instrument_kind, action_provenance = _validate_inputs(
        config=config,
        market_data=market_data,
        corporate_actions=corporate_actions,
        calendar=calendar,
        fee_policy=fee_policy,
        universe=universe,
    )
    bars = _bar_map(market_data, corporate_actions)
    sessions, buffer_session = _validated_sessions(bars, calendar)
    strategy = _simulate_path(
        label="sma_planner",
        config=config,
        sessions=sessions,
        bars=bars,
        actions=corporate_actions,
        calendar=calendar,
        fee_policy=fee_policy,
        instrument_kind=instrument_kind,
        plan_factory=_strategy_plan_factory(config),
    )
    benchmark = _simulate_path(
        label="buy_and_hold",
        config=config,
        sessions=sessions,
        bars=bars,
        actions=corporate_actions,
        calendar=calendar,
        fee_policy=fee_policy,
        instrument_kind=instrument_kind,
        plan_factory=_benchmark_plan_factory(config),
    )
    implementation_digest = _implementation_digest()
    identity = _identity_payload(
        implementation_digest=implementation_digest,
        market_data=market_data,
        corporate_actions=corporate_actions,
        calendar=calendar,
        fee_policy=fee_policy,
        universe=universe,
        config=config,
        simulation_start=sessions[0],
        simulation_end=sessions[-1],
        settlement_buffer_session=buffer_session,
    )
    run_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ResearchLoopResult(
        schema_version=RESEARCH_LOOP_SCHEMA_VERSION,
        run_id=run_id,
        implementation_digest=implementation_digest,
        input_digest=market_data.input_digest,
        market_snapshot_id=market_data.provenance.snapshot_id,
        market_file_sha256=market_data.provenance.file_sha256,
        corporate_action_snapshot_id=action_provenance.snapshot_id,
        corporate_action_file_sha256=action_provenance.file_sha256,
        calendar_id=calendar.calendar_id,
        calendar_file_sha256=calendar.file_sha256,
        universe_id=universe.universe_id,
        fee_policy_digest=fee_policy.policy_digest,
        price_stream_version=PRICE_STREAM_VERSION,
        config=config,
        instrument_kind=instrument_kind,
        simulation_start=sessions[0],
        simulation_end=sessions[-1],
        settlement_buffer_session=buffer_session,
        strategy=strategy,
        benchmark=benchmark,
    )
