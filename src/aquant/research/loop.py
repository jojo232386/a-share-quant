"""Minimal verified Data -> Signal -> Planner -> Portfolio research loop."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
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
from aquant.research.signals import (
    AbsoluteMomentumSignal,
    MonthlyRelativeMomentumSignal,
    SignalInput,
    SignalObservation,
    SmaSignal,
    VolatilityRegimeDefenseSignal,
)
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

RESEARCH_LOOP_SCHEMA_VERSION = "1.4.0"
STRATEGY_SMA = "sma"
STRATEGY_VOLATILITY_REGIME_DEFENSE = "volatility_regime_defense"
STRATEGY_ABSOLUTE_MOMENTUM_252 = "absolute_momentum_252"
STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12 = "monthly_relative_momentum_2_12"
PREREGISTRATION_PRIMARY_METRICS = (
    "total_return",
    "sharpe_zero_rate",
    "max_drawdown",
)
PREREGISTRATION_PASS_CRITERIA = {
    "strategy_total_return": "> benchmark_total_return",
    "strategy_sharpe_zero_rate": "> benchmark_sharpe_zero_rate",
    "strategy_max_drawdown": "<= benchmark_max_drawdown",
}
A4_PRIMARY_METRICS = (
    "annualized_return",
    "sharpe_zero_rate",
    "max_drawdown",
    "gross_turnover",
)
A4_SECONDARY_METRICS = ("total_return", "trade_count")
A4_PASS_CRITERIA = {
    "strategy_annualized_return": ">= 0.70 * benchmark_annualized_return",
    "strategy_gross_turnover": "<= 100.0",
    "strategy_max_drawdown": "<= 0.80 * benchmark_max_drawdown",
    "strategy_sharpe_zero_rate": ">= benchmark_sharpe_zero_rate + 0.10",
}
A4_INVALID_HANDLING = {
    "non_finite_input": "SignalInput fails closed before producing a decision",
    "non_finite_return_or_volatility": "NO_DECISION",
    "non_positive_close": "NO_DECISION",
}
A4_RESEARCH_SEMANTICS = {
    "annualized_volatility": (
        "sample_standard_deviation(latest_20_simple_returns, ddof=1) * sqrt(252)"
    ),
    "flat": "explicit Decimal('0') target passed through the existing Planner",
    "no_decision": "symbol omitted so the existing Planner preserves the previous target",
    "numeric_convention": (
        "float arithmetic with math.fsum for mean and squared deviations, "
        "matching aquant.risk.metrics"
    ),
    "price_series": "causal indicator_close available as of the session close",
    "return_definition": "indicator_close[t] / indicator_close[t-1] - 1.0",
    "threshold_boundary": (
        "annualized_volatility <= 0.25 is ACTIVE; annualized_volatility > 0.25 is FLAT"
    ),
    "window_semantics": (
        "latest 20 consecutive close-to-close returns ending at as_of, "
        "requiring 21 valid closes"
    ),
    "warm_up": "fewer than 20 returns produces NO_DECISION",
}
A4_2_INVALID_HANDLING = {
    "non_finite_input": "SignalInput fails closed before producing a decision",
    "non_finite_trailing_return": "NO_DECISION",
    "non_positive_close": "NO_DECISION",
}
A4_2_RESEARCH_SEMANTICS = {
    "flat": "explicit Decimal('0') target passed through the existing Planner",
    "no_decision": "symbol omitted so the existing Planner preserves the previous target",
    "numeric_convention": "float division on the existing indicator_close stream",
    "price_series": "causal indicator_close available as of the session close",
    "return_definition": "indicator_close[t] / indicator_close[t-252] - 1.0",
    "threshold_boundary": (
        "trailing_252_session_return > 0 is ACTIVE; "
        "trailing_252_session_return <= 0 is FLAT"
    ),
    "window_semantics": (
        "252 trading-session intervals ending at as_of, requiring 253 valid closes"
    ),
    "warm_up": "fewer than 253 closes produces NO_DECISION",
}
A4_3_SECONDARY_METRICS = (
    "total_return",
    "trade_count",
    "annualized_gross_turnover",
)
A4_3_INVALID_HANDLING = {
    "invalid_monthly_endpoint": "whole strategy NO_DECISION",
    "missing_one_symbol": "whole strategy NO_DECISION",
    "non_finite_input": "SignalInput fails closed before producing a decision",
    "non_finite_momentum": "whole strategy NO_DECISION",
    "non_positive_close": "whole strategy NO_DECISION",
}
A4_3_RESEARCH_SEMANTICS = {
    "benchmark": (
        "initial 510300=0.475, 510500=0.475, cash=0.05 targets; "
        "no later active rebalancing"
    ),
    "cash_target": "0.05",
    "decision_schedule": (
        "only the final official trading session of each natural month; all other "
        "sessions are NO_NEW_DECISION and preserve the previous effective target"
    ),
    "missing_ranking_input": (
        "if either eligible symbol lacks one required endpoint or has an invalid "
        "endpoint, emit whole-strategy NO_DECISION; never shrink the ranking universe"
    ),
    "negative_momentum": (
        "always hold the relative winner even when both momentum values are negative; "
        "no absolute filter"
    ),
    "no_decision": (
        "both symbols omitted so the existing Planner preserves the complete previous "
        "target state"
    ),
    "numeric_convention": "float division on each existing causal indicator_close stream",
    "price_series": (
        "causal indicator_close available as of each official session close"
    ),
    "ranking": (
        "higher momentum wins; exact ties break by ascending symbol, so 510300 wins"
    ),
    "return_definition": (
        "at the final session of month t-1, indicator_close(end month t-2) / "
        "indicator_close(end month t-13) - 1.0"
    ),
    "target": "winner Decimal('0.95'), loser Decimal('0'), cash Decimal('0.05')",
    "window_semantics": (
        "11 complete calendar-month cumulative return skipping the latest complete "
        "month, using verified official month-end sessions"
    ),
    "warm_up": (
        "first decision requires valid month-end endpoints for both symbols; "
        "mechanically fixed first signal is 2019-01-31 and next-session execution is "
        "2019-02-01"
    ),
}
A4_3_BENCHMARK = {
    "active_rebalancing": False,
    "cash_weight": "0.05",
    "initial_targets": {"510300": "0.475", "510500": "0.475"},
    "type": "static_buy_and_hold",
}
A4_3_FORMAL_RUN_CONTROLS = {
    "formal_rerun": False,
    "max_formal_runs": 1,
    "parameter_rescue": False,
    "parameter_sweep": False,
}
A4_3_TURNOVER_UNIT = {
    "annualized_gross_turnover": "secondary diagnostic only",
    "gross_turnover": "raw ratio",
    "threshold_raw_ratio": 100.0,
    "threshold_percent_equivalent": "10000%",
}
A4_3_VALIDITY_CRITERIA = {
    "execution_consistency": "required",
    "invalid_data_behavior": "none",
    "preregistration_implementation_binding": "required",
    "provenance": "required",
    "static_benchmark_consistency": "required",
}
A4_INSUFFICIENT_EVIDENCE_CRITERIA = (
    "incomplete_data",
    "provenance_failure",
    "formal_runner_failure",
    "strategy_cannot_be_expressed_by_existing_execution_semantics",
)
A4_VALIDITY_CRITERIA = {
    "execution_consistency": "required",
    "invalid_data_behavior": "none",
    "provenance": "required",
}
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
    strategy: str = STRATEGY_SMA
    sma_period: int = 20
    lookback_returns: int = 20
    lookback_sessions: int = 252
    annualization: int = 252
    volatility_threshold: Decimal = Decimal("0.25")
    active_weight: Decimal = Decimal("0.95")
    secondary_symbol: str | None = None
    lookback_start_month: int = 2
    lookback_end_month: int = 12
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
            or self.strategy not in {
                STRATEGY_SMA,
                STRATEGY_VOLATILITY_REGIME_DEFENSE,
                STRATEGY_ABSOLUTE_MOMENTUM_252,
                STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12,
            }
            or (self.strategy == STRATEGY_SMA and (
                type(self.sma_period) is not int or self.sma_period < 2
            ))
            or (self.strategy == STRATEGY_VOLATILITY_REGIME_DEFENSE and (
                self.symbol != "510300"
                or type(self.lookback_returns) is not int
                or self.lookback_returns != 20
                or type(self.annualization) is not int
                or self.annualization != 252
                or type(self.volatility_threshold) is not Decimal
                or self.volatility_threshold != Decimal("0.25")
                or self.active_weight != Decimal("0.95")
                or self.limits
                != PlannerLimits(
                    max_single_weight=Decimal("0.95"),
                    max_gross=Decimal("0.95"),
                    min_cash_ratio=Decimal("0.05"),
                )
            ))
            or (self.strategy == STRATEGY_ABSOLUTE_MOMENTUM_252 and (
                self.symbol != "510300"
                or type(self.lookback_sessions) is not int
                or self.lookback_sessions != 252
                or self.active_weight != Decimal("0.95")
                or self.limits
                != PlannerLimits(
                    max_single_weight=Decimal("0.95"),
                    max_gross=Decimal("0.95"),
                    min_cash_ratio=Decimal("0.05"),
                )
            ))
            or (self.strategy == STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12 and (
                self.symbol != "510300"
                or self.secondary_symbol != "510500"
                or self.lookback_start_month != 2
                or self.lookback_end_month != 12
                or self.active_weight != Decimal("0.95")
                or self.limits
                != PlannerLimits(
                    max_single_weight=Decimal("0.95"),
                    max_gross=Decimal("0.95"),
                    min_cash_ratio=Decimal("0.05"),
                )
            ))
            or (
                self.strategy != STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12
                and self.secondary_symbol is not None
            )
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

    @property
    def symbols(self) -> tuple[str, ...]:
        return (
            (self.symbol, self.secondary_symbol)
            if self.secondary_symbol is not None
            else (self.symbol,)
        )


def research_config_payload(config: ResearchLoopConfig) -> dict[str, object]:
    """Return the exact strategy/parameter identity bound into formal artifacts."""
    common: dict[str, object] = {
        "symbol": config.symbol,
        "initial_cash_fen": config.initial_cash_fen,
        "active_weight": str(config.active_weight),
        "limits": {key: str(value) for key, value in asdict(config.limits).items()},
    }
    if config.strategy == STRATEGY_SMA:
        return {**common, "sma_period": config.sma_period}
    if config.strategy == STRATEGY_VOLATILITY_REGIME_DEFENSE:
        return {
            **common,
            "strategy": STRATEGY_VOLATILITY_REGIME_DEFENSE,
            "lookback_returns": config.lookback_returns,
            "annualization": config.annualization,
            "volatility_threshold": str(config.volatility_threshold),
        }
    if config.strategy == STRATEGY_ABSOLUTE_MOMENTUM_252:
        return {
            **common,
            "strategy": STRATEGY_ABSOLUTE_MOMENTUM_252,
            "lookback_sessions": config.lookback_sessions,
        }
    return {
        "active_weight": str(config.active_weight),
        "initial_cash_fen": config.initial_cash_fen,
        "limits": {key: str(value) for key, value in asdict(config.limits).items()},
        "lookback_end_month": config.lookback_end_month,
        "lookback_start_month": config.lookback_start_month,
        "strategy": STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12,
        "symbols": list(config.symbols),
    }


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
    git_head: str
    preregistration_commit: str
    preregistration_sha256: str
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
    market_snapshot_ids: tuple[tuple[str, str], ...] = ()
    market_file_sha256s: tuple[tuple[str, str], ...] = ()
    corporate_action_snapshot_ids: tuple[tuple[str, str], ...] = ()
    corporate_action_file_sha256s: tuple[tuple[str, str], ...] = ()


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


def _month_end_sessions(sessions: tuple[date, ...]) -> tuple[date, ...]:
    month_ends: list[date] = []
    for session in sessions:
        if month_ends and (
            month_ends[-1].year == session.year
            and month_ends[-1].month == session.month
        ):
            month_ends[-1] = session
        else:
            month_ends.append(session)
    return tuple(month_ends)


def _validated_a4_3_sessions(
    bars_by_symbol: Mapping[str, dict[date, _Bar]],
    calendar: VerifiedTradingCalendar,
) -> tuple[tuple[date, ...], date, tuple[date, ...]]:
    if tuple(sorted(bars_by_symbol)) != ("510300", "510500"):
        raise ResearchLoopError(
            "input_identity_mismatch",
            "A4-3 requires exactly the two frozen market-data symbols",
        )
    primary_sessions, buffer_session = _validated_sessions(
        bars_by_symbol["510300"], calendar
    )
    for symbol in ("510300", "510500"):
        dates = tuple(sorted(bars_by_symbol[symbol]))
        if dates[0] != min(bars_by_symbol["510300"]) or dates[-1] != max(
            bars_by_symbol["510300"]
        ):
            raise ResearchLoopError(
                "calendar_market_mismatch",
                "A4-3 market snapshots must have identical verified endpoints",
            )
    official_with_buffer = tuple(
        session
        for session in calendar.dates
        if min(bars_by_symbol["510300"])
        <= session
        <= max(bars_by_symbol["510300"])
    )
    month_ends = _month_end_sessions(official_with_buffer)
    first_signal: date | None = None
    for position, decision_session in enumerate(month_ends):
        if position < 12 or decision_session not in primary_sessions:
            continue
        numerator_session = month_ends[position - 1]
        denominator_session = month_ends[position - 12]
        valid = True
        for symbol in ("510300", "510500"):
            numerator = bars_by_symbol[symbol].get(numerator_session)
            denominator = bars_by_symbol[symbol].get(denominator_session)
            decision = bars_by_symbol[symbol].get(decision_session)
            if (
                numerator is None
                or denominator is None
                or decision is None
                or numerator.indicator_close <= 0
                or denominator.indicator_close <= 0
            ):
                valid = False
                break
        if valid:
            first_signal = decision_session
            break
    if first_signal is None:
        raise ResearchLoopError(
            "insufficient_history",
            "A4-3 has no uniquely valid first monthly signal session",
        )
    first_execution = calendar.next_session(first_signal)
    if first_execution is None or first_execution not in primary_sessions:
        raise ResearchLoopError(
            "missing_settlement_buffer",
            "A4-3 first signal has no simulated next-session execution",
        )
    simulation = tuple(session for session in primary_sessions if session >= first_signal)
    if len(simulation) < 2:
        raise ResearchLoopError(
            "insufficient_history",
            "A4-3 requires a signal baseline and at least one execution session",
        )
    return simulation, buffer_session, month_ends


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


def _valuations_multi(
    ledger: RollingPortfolioLedger,
    *,
    session: date,
    marks: Mapping[str, Decimal],
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
            mark_price=marks[symbol],
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


def _execution_inputs_multi(
    planned: PlannedTargets,
    *,
    execution_session: date,
    bars: Mapping[str, _Bar | None],
    instrument_kinds: Mapping[str, InstrumentKind],
) -> tuple[RollingExecutionInput, ...]:
    return tuple(
        RollingExecutionInput(
            symbol=symbol,
            instrument_kind=instrument_kinds[symbol],
            intent_session=planned.as_of,
            execution_session=execution_session,
            previous_close=(
                bars[symbol].reference_price if bars[symbol] is not None else None
            ),
            execution_open=(bars[symbol].open if bars[symbol] is not None else None),
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


MultiPlanFactory = Callable[
    [date, bool, Mapping[str, tuple[SignalObservation, ...]], PlannedTargets | None],
    tuple[PlannedTargets | None, SignalDecision | None],
]


def _a4_3_strategy_plan_factory(
    config: ResearchLoopConfig,
    *,
    month_end_sessions: tuple[date, ...],
) -> MultiPlanFactory:
    signal = MonthlyRelativeMomentumSignal(month_end_sessions=month_end_sessions)
    eligible = frozenset(config.symbols)

    def factory(
        session: date,
        data_available: bool,
        observations: Mapping[str, tuple[SignalObservation, ...]],
        previous: PlannedTargets | None,
    ) -> tuple[PlannedTargets, SignalDecision]:
        data = SignalInput(as_of=session, per_symbol=observations)
        output = signal.compute(session, data) if data_available else {}
        planned = plan_targets(
            as_of=session,
            signal_output=output,
            previous=(
                NoPreviousState(NoPreviousStateReason.FIRST_PERIOD)
                if previous is None
                else PreviousTargets(as_of=previous.as_of, targets=previous.targets)
            ),
            eligible_symbols=eligible,
            limits=config.limits,
        )
        return planned, SignalDecision(
            session=session,
            data_available=data_available,
            output=tuple(sorted(output.items())),
        )

    return factory


def _a4_3_benchmark_plan_factory(config: ResearchLoopConfig) -> MultiPlanFactory:
    emitted = False
    eligible = frozenset(config.symbols)

    def factory(
        session: date,
        data_available: bool,
        _observations: Mapping[str, tuple[SignalObservation, ...]],
        previous: PlannedTargets | None,
    ) -> tuple[PlannedTargets, None]:
        nonlocal emitted
        if not emitted and not data_available:
            raise ResearchLoopError(
                "benchmark_initial_bar_missing",
                "A4-3 static benchmark requires both initial signal-session bars",
            )
        output = (
            {"510300": Decimal("0.475"), "510500": Decimal("0.475")}
            if not emitted
            else {}
        )
        planned = plan_targets(
            as_of=session,
            signal_output=output,
            previous=(
                NoPreviousState(NoPreviousStateReason.FIRST_PERIOD)
                if previous is None
                else PreviousTargets(as_of=previous.as_of, targets=previous.targets)
            ),
            eligible_symbols=eligible,
            limits=config.limits,
        )
        emitted = True
        return planned, None

    return factory


def _simulate_a4_3_path(
    *,
    label: str,
    config: ResearchLoopConfig,
    sessions: tuple[date, ...],
    bars_by_symbol: Mapping[str, dict[date, _Bar]],
    actions_by_symbol: Mapping[str, VerifiedCorporateActions],
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
    instrument_kinds: Mapping[str, InstrumentKind],
    plan_factory: MultiPlanFactory,
) -> ResearchPathResult:
    ledger = create_rolling_ledger(config.initial_cash_fen)
    pending_plan: PlannedTargets | None = None
    previous_plan: PlannedTargets | None = None
    observations: dict[str, list[SignalObservation]] = {
        symbol: [
            SignalObservation(session=session, indicator_close=bar.indicator_close)
            for session, bar in sorted(bars_by_symbol[symbol].items())
            if session < sessions[0]
        ]
        for symbol in config.symbols
    }
    plans: list[PlannedTargets] = []
    decisions: list[SignalDecision] = []
    attempts: list[RebalanceAttempt] = []
    dividend_audits: list[DividendEventAudit] = []
    missing: list[date] = []
    marks: list[tuple[date, Decimal]] = []
    current_marks = {
        symbol: bars_by_symbol[symbol][sessions[0]].close for symbol in config.symbols
    }
    retry_required = False

    for position, session in enumerate(sessions):
        session_bars = {
            symbol: bars_by_symbol[symbol].get(session) for symbol in config.symbols
        }
        data_available = all(bar is not None for bar in session_bars.values())
        if not data_available:
            missing.append(session)
        for symbol, bar in session_bars.items():
            if bar is not None:
                current_marks[symbol] = bar.close
                observations[symbol].append(
                    SignalObservation(
                        session=session,
                        indicator_close=bar.indicator_close,
                    )
                )

        receivables: list[CashReceivable] = []
        for symbol in config.symbols:
            symbol_receivables, session_audits = _session_dividends(
                ledger,
                session=session,
                symbol=symbol,
                actions=actions_by_symbol[symbol],
                calendar=calendar,
            )
            receivables.extend(symbol_receivables)
            dividend_audits.extend(session_audits)

        if pending_plan is not None:
            result = rebalance_to_plan(
                config=RollingConfig(limits=config.limits),
                planned=pending_plan,
                ledger=ledger,
                execution_inputs=_execution_inputs_multi(
                    pending_plan,
                    execution_session=session,
                    bars=session_bars,
                    instrument_kinds=instrument_kinds,
                ),
                calendar=calendar,
                fee_policy=fee_policy,
            )
            ledger = result.ledger
            attempts.extend(result.attempts)
            retry_required = any(not target.is_aligned for target in result.targets)
        else:
            retry_required = False

        ledger = _register_receivables(ledger, tuple(receivables))
        ledger = _pay_receivables(ledger, session)
        ledger = close_rolling_session(
            ledger,
            session,
            _valuations_multi(ledger, session=session, marks=current_marks),
        )
        marks.append((session, current_marks[config.symbol]))

        terminal = position == len(sessions) - 1
        generated_plan, decision = plan_factory(
            session,
            data_available,
            {symbol: tuple(values) for symbol, values in observations.items()},
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
    if config.strategy == STRATEGY_SMA:
        signal = SmaSignal(config.sma_period, config.active_weight)
    elif config.strategy == STRATEGY_VOLATILITY_REGIME_DEFENSE:
        signal = VolatilityRegimeDefenseSignal(
            lookback_returns=config.lookback_returns,
            annualization=config.annualization,
            volatility_threshold=config.volatility_threshold,
            active_weight=config.active_weight,
        )
    else:
        signal = AbsoluteMomentumSignal(
            lookback_sessions=config.lookback_sessions,
            active_weight=config.active_weight,
        )

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


def _validate_a4_3_inputs(
    *,
    config: ResearchLoopConfig,
    market_data: Mapping[str, VerifiedMarketData],
    corporate_actions: Mapping[str, VerifiedCorporateActions],
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
    universe: VerifiedUniverse,
) -> tuple[dict[str, InstrumentKind], dict[str, object]]:
    if type(config) is not ResearchLoopConfig:
        raise TypeError("config must be an exact ResearchLoopConfig")
    if not isinstance(market_data, Mapping) or not isinstance(
        corporate_actions, Mapping
    ):
        raise TypeError("A4-3 market data and corporate actions must be mappings")
    if tuple(sorted(market_data)) != config.symbols or tuple(
        sorted(corporate_actions)
    ) != config.symbols:
        raise ResearchLoopError(
            "input_identity_mismatch",
            "A4-3 requires exact market and corporate-action inputs for both symbols",
        )
    verify_trading_calendar(calendar)
    verify_fee_policy(fee_policy)
    verify_universe(universe)
    kinds: dict[str, InstrumentKind] = {}
    action_provenance: dict[str, object] = {}
    coverage: list[tuple[date, date]] = []
    for symbol in config.symbols:
        market = market_data[symbol]
        actions = corporate_actions[symbol]
        if type(market) is not VerifiedMarketData:
            raise TypeError("A4-3 market data values must be exact VerifiedMarketData")
        verify_verified_corporate_actions(actions)
        provenance = market.provenance
        action = actions.provenance
        assert action is not None
        if (
            provenance.symbol != symbol
            or action.symbol != symbol
            or action.instrument_kind is not provenance.instrument_kind
            or not universe.contains(symbol, provenance.instrument_kind.value)
        ):
            raise ResearchLoopError(
                "input_identity_mismatch",
                "A4-3 input identities must match the frozen two-symbol eligibility",
            )
        frame = market.frame
        start = frame["date"].dt.date.iloc[0]
        end = frame["date"].dt.date.iloc[-1]
        if action.coverage_start > start or action.coverage_end < end:
            raise ResearchLoopError(
                "corporate_action_coverage_mismatch",
                "A4-3 corporate-action coverage must span each market snapshot",
            )
        kinds[symbol] = provenance.instrument_kind
        action_provenance[symbol] = action
        coverage.append((start, end))
    if len(set(coverage)) != 1 or len(set(kinds.values())) != 1:
        raise ResearchLoopError(
            "input_identity_mismatch",
            "A4-3 inputs must share exact coverage and instrument kind",
        )
    return kinds, action_provenance


def _a4_3_input_digest(market_data: Mapping[str, VerifiedMarketData]) -> str:
    payload = {
        symbol: market_data[symbol].input_digest for symbol in sorted(market_data)
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _identity_payload(
    *,
    git_head: str,
    preregistration_commit: str,
    preregistration_sha256: str,
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
        "git_head": git_head,
        "preregistration_commit": preregistration_commit,
        "preregistration_sha256": preregistration_sha256,
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
        "config": research_config_payload(config),
    }


def _a4_3_identity_payload(
    *,
    git_head: str,
    preregistration_commit: str,
    preregistration_sha256: str,
    implementation_digest: str,
    market_data: Mapping[str, VerifiedMarketData],
    corporate_actions: Mapping[str, VerifiedCorporateActions],
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
    universe: VerifiedUniverse,
    config: ResearchLoopConfig,
    simulation_start: date,
    simulation_end: date,
    settlement_buffer_session: date,
) -> dict[str, object]:
    return {
        "schema_version": RESEARCH_LOOP_SCHEMA_VERSION,
        "git_head": git_head,
        "preregistration_commit": preregistration_commit,
        "preregistration_sha256": preregistration_sha256,
        "implementation_digest": implementation_digest,
        "input_digest": _a4_3_input_digest(market_data),
        "market_snapshot_ids": {
            symbol: market_data[symbol].provenance.snapshot_id
            for symbol in config.symbols
        },
        "market_file_sha256s": {
            symbol: market_data[symbol].provenance.file_sha256
            for symbol in config.symbols
        },
        "corporate_action_snapshot_ids": {
            symbol: corporate_actions[symbol].provenance.snapshot_id
            for symbol in config.symbols
        },
        "corporate_action_file_sha256s": {
            symbol: corporate_actions[symbol].provenance.file_sha256
            for symbol in config.symbols
        },
        "calendar_id": calendar.calendar_id,
        "calendar_file_sha256": calendar.file_sha256,
        "universe_id": universe.universe_id,
        "fee_policy_digest": fee_policy.policy_digest,
        "price_stream_version": PRICE_STREAM_VERSION,
        "simulation_start": simulation_start.isoformat(),
        "simulation_end": simulation_end.isoformat(),
        "settlement_buffer_session": settlement_buffer_session.isoformat(),
        "config": research_config_payload(config),
    }


def _validate_preregistration(
    content: bytes,
    *,
    config: ResearchLoopConfig,
    simulation_start: date,
    simulation_end: date,
    market_data: VerifiedMarketData | Mapping[str, VerifiedMarketData],
    corporate_actions: VerifiedCorporateActions
    | Mapping[str, VerifiedCorporateActions],
    calendar: VerifiedTradingCalendar,
    universe: VerifiedUniverse,
) -> str:
    try:
        preregistration = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchLoopError(
            "invalid_preregistration",
            "preregistration must be valid UTF-8 JSON",
        ) from exc
    expected_parameters = research_config_payload(config)
    expected_period = {
        "start": simulation_start.isoformat(),
        "end": simulation_end.isoformat(),
    }
    if config.strategy == STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12:
        if not isinstance(market_data, Mapping) or not isinstance(
            corporate_actions, Mapping
        ):
            raise ResearchLoopError(
                "preregistration_mismatch",
                "A4-3 preregistration requires both input identity mappings",
            )
        expected_inputs = {
            "calendar_id": calendar.calendar_id,
            "corporate_action_snapshot_ids": {
                symbol: corporate_actions[symbol].provenance.snapshot_id
                for symbol in config.symbols
            },
            "market_snapshot_ids": {
                symbol: market_data[symbol].provenance.snapshot_id
                for symbol in config.symbols
            },
            "universe_id": universe.universe_id,
        }
        first_execution = calendar.next_session(simulation_start)
        mismatch = (
            type(preregistration) is not dict
            or type(preregistration.get("hypothesis")) is not str
            or not preregistration["hypothesis"].strip()
            or preregistration.get("hypothesis_id")
            != "A4_3_510300_510500_MONTHLY_RELATIVE_MOMENTUM_2_12"
            or preregistration.get("subject") != "510300,510500"
            or preregistration.get("universe") != list(config.symbols)
            or preregistration.get("evaluation_period") != expected_period
            or preregistration.get("first_signal_session")
            != simulation_start.isoformat()
            or preregistration.get("first_execution_session")
            != (first_execution.isoformat() if first_execution is not None else None)
            or preregistration.get("benchmark") != A4_3_BENCHMARK
            or preregistration.get("strategy_parameters") != expected_parameters
            or preregistration.get("input_identities") != expected_inputs
            or preregistration.get("primary_metrics") != list(A4_PRIMARY_METRICS)
            or preregistration.get("secondary_metrics")
            != list(A4_3_SECONDARY_METRICS)
            or preregistration.get("pass_criteria") != A4_PASS_CRITERIA
            or preregistration.get("reject_criteria")
            != "any_core_threshold_failure"
            or preregistration.get("invalid_handling") != A4_3_INVALID_HANDLING
            or preregistration.get("research_semantics")
            != A4_3_RESEARCH_SEMANTICS
            or preregistration.get("insufficient_evidence_criteria")
            != list(A4_INSUFFICIENT_EVIDENCE_CRITERIA)
            or preregistration.get("validity_criteria")
            != A4_3_VALIDITY_CRITERIA
            or preregistration.get("formal_run_controls")
            != A4_3_FORMAL_RUN_CONTROLS
            or preregistration.get("turnover_unit") != A4_3_TURNOVER_UNIT
        )
        if mismatch:
            raise ResearchLoopError(
                "preregistration_mismatch",
                "A4-3 preregistration must match every frozen research field",
            )
        return hashlib.sha256(content).hexdigest()
    common_mismatch = (
        type(preregistration) is not dict
        or type(preregistration.get("hypothesis")) is not str
        or not preregistration["hypothesis"].strip()
        or preregistration.get("universe") != [config.symbol]
        or preregistration.get("evaluation_period") != expected_period
        or preregistration.get("benchmark") != "buy_and_hold"
        or preregistration.get("strategy_parameters") != expected_parameters
    )
    if config.strategy == STRATEGY_SMA:
        strategy_mismatch = (
            preregistration.get("primary_metrics")
            != list(PREREGISTRATION_PRIMARY_METRICS)
            or preregistration.get("pass_criteria") != PREREGISTRATION_PASS_CRITERIA
            or preregistration.get("reject_criteria") != "otherwise"
        )
    else:
        action = corporate_actions.provenance
        assert action is not None
        expected_inputs = {
            "calendar_id": calendar.calendar_id,
            "corporate_action_snapshot_id": action.snapshot_id,
            "market_snapshot_id": market_data.provenance.snapshot_id,
            "universe_id": universe.universe_id,
        }
        if config.strategy == STRATEGY_VOLATILITY_REGIME_DEFENSE:
            expected_hypothesis_id = "A4_1_510300_VOLATILITY_REGIME_DEFENSE"
            expected_invalid_handling = A4_INVALID_HANDLING
            expected_research_semantics = A4_RESEARCH_SEMANTICS
        else:
            expected_hypothesis_id = "A4_2_510300_ABSOLUTE_MOMENTUM_252"
            expected_invalid_handling = A4_2_INVALID_HANDLING
            expected_research_semantics = A4_2_RESEARCH_SEMANTICS
        strategy_mismatch = (
            preregistration.get("hypothesis_id") != expected_hypothesis_id
            or preregistration.get("subject") != config.symbol
            or preregistration.get("input_identities") != expected_inputs
            or preregistration.get("primary_metrics") != list(A4_PRIMARY_METRICS)
            or preregistration.get("secondary_metrics") != list(A4_SECONDARY_METRICS)
            or preregistration.get("pass_criteria") != A4_PASS_CRITERIA
            or preregistration.get("reject_criteria") != "any_core_threshold_failure"
            or preregistration.get("invalid_handling") != expected_invalid_handling
            or preregistration.get("research_semantics") != expected_research_semantics
            or preregistration.get("insufficient_evidence_criteria")
            != list(A4_INSUFFICIENT_EVIDENCE_CRITERIA)
            or preregistration.get("validity_criteria") != A4_VALIDITY_CRITERIA
        )
    if common_mismatch or strategy_mismatch:
        raise ResearchLoopError(
            "preregistration_mismatch",
            "preregistration must match the formal research definition and parameters",
        )
    return hashlib.sha256(content).hexdigest()


def run_research_loop(
    *,
    git_head: str,
    preregistration_commit: str,
    preregistration_content: bytes,
    config: ResearchLoopConfig,
    market_data: VerifiedMarketData | Mapping[str, VerifiedMarketData],
    corporate_actions: VerifiedCorporateActions
    | Mapping[str, VerifiedCorporateActions],
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
    universe: VerifiedUniverse,
) -> ResearchLoopResult:
    """Run one deterministic verified strategy and one buy-and-hold benchmark."""
    if (
        type(git_head) is not str
        or len(git_head) != 40
        or any(character not in "0123456789abcdef" for character in git_head)
        or type(preregistration_commit) is not str
        or len(preregistration_commit) != 40
        or any(character not in "0123456789abcdef" for character in preregistration_commit)
        or type(preregistration_content) is not bytes
    ):
        raise ResearchLoopError(
            "invalid_provenance_identity",
            "formal research provenance identity is invalid",
        )
    if config.strategy == STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12:
        if not isinstance(market_data, Mapping) or not isinstance(
            corporate_actions, Mapping
        ):
            raise ResearchLoopError(
                "input_identity_mismatch",
                "A4-3 requires two-symbol verified input mappings",
            )
        instrument_kinds, _ = _validate_a4_3_inputs(
            config=config,
            market_data=market_data,
            corporate_actions=corporate_actions,
            calendar=calendar,
            fee_policy=fee_policy,
            universe=universe,
        )
        bars_by_symbol = {
            symbol: _bar_map(market_data[symbol], corporate_actions[symbol])
            for symbol in config.symbols
        }
        sessions, buffer_session, month_ends = _validated_a4_3_sessions(
            bars_by_symbol, calendar
        )
        preregistration_sha256 = _validate_preregistration(
            preregistration_content,
            config=config,
            simulation_start=sessions[0],
            simulation_end=sessions[-1],
            market_data=market_data,
            corporate_actions=corporate_actions,
            calendar=calendar,
            universe=universe,
        )
        strategy = _simulate_a4_3_path(
            label=f"{config.strategy}_planner",
            config=config,
            sessions=sessions,
            bars_by_symbol=bars_by_symbol,
            actions_by_symbol=corporate_actions,
            calendar=calendar,
            fee_policy=fee_policy,
            instrument_kinds=instrument_kinds,
            plan_factory=_a4_3_strategy_plan_factory(
                config, month_end_sessions=month_ends
            ),
        )
        benchmark = _simulate_a4_3_path(
            label="static_buy_and_hold",
            config=config,
            sessions=sessions,
            bars_by_symbol=bars_by_symbol,
            actions_by_symbol=corporate_actions,
            calendar=calendar,
            fee_policy=fee_policy,
            instrument_kinds=instrument_kinds,
            plan_factory=_a4_3_benchmark_plan_factory(config),
        )
        implementation_digest = _implementation_digest()
        identity = _a4_3_identity_payload(
            git_head=git_head,
            preregistration_commit=preregistration_commit,
            preregistration_sha256=preregistration_sha256,
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
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        primary_market = market_data[config.symbol]
        primary_action = corporate_actions[config.symbol].provenance
        assert primary_action is not None
        return ResearchLoopResult(
            schema_version=RESEARCH_LOOP_SCHEMA_VERSION,
            run_id=run_id,
            git_head=git_head,
            preregistration_commit=preregistration_commit,
            preregistration_sha256=preregistration_sha256,
            implementation_digest=implementation_digest,
            input_digest=_a4_3_input_digest(market_data),
            market_snapshot_id=primary_market.provenance.snapshot_id,
            market_file_sha256=primary_market.provenance.file_sha256,
            corporate_action_snapshot_id=primary_action.snapshot_id,
            corporate_action_file_sha256=primary_action.file_sha256,
            calendar_id=calendar.calendar_id,
            calendar_file_sha256=calendar.file_sha256,
            universe_id=universe.universe_id,
            fee_policy_digest=fee_policy.policy_digest,
            price_stream_version=PRICE_STREAM_VERSION,
            config=config,
            instrument_kind=instrument_kinds[config.symbol],
            simulation_start=sessions[0],
            simulation_end=sessions[-1],
            settlement_buffer_session=buffer_session,
            strategy=strategy,
            benchmark=benchmark,
            market_snapshot_ids=tuple(
                (symbol, market_data[symbol].provenance.snapshot_id)
                for symbol in config.symbols
            ),
            market_file_sha256s=tuple(
                (symbol, market_data[symbol].provenance.file_sha256)
                for symbol in config.symbols
            ),
            corporate_action_snapshot_ids=tuple(
                (symbol, corporate_actions[symbol].provenance.snapshot_id)
                for symbol in config.symbols
            ),
            corporate_action_file_sha256s=tuple(
                (symbol, corporate_actions[symbol].provenance.file_sha256)
                for symbol in config.symbols
            ),
        )
    if type(market_data) is not VerifiedMarketData or type(
        corporate_actions
    ) is not VerifiedCorporateActions:
        raise ResearchLoopError(
            "input_identity_mismatch",
            "single-symbol research requires exact single-symbol inputs",
        )
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
    preregistration_sha256 = _validate_preregistration(
        preregistration_content,
        config=config,
        simulation_start=sessions[0],
        simulation_end=sessions[-1],
        market_data=market_data,
        corporate_actions=corporate_actions,
        calendar=calendar,
        universe=universe,
    )
    strategy = _simulate_path(
        label=f"{config.strategy}_planner",
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
        git_head=git_head,
        preregistration_commit=preregistration_commit,
        preregistration_sha256=preregistration_sha256,
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
        git_head=git_head,
        preregistration_commit=preregistration_commit,
        preregistration_sha256=preregistration_sha256,
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
