"""Deterministic one-session Planner to rolling-portfolio orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from aquant.data.calendar_snapshot import (
    CalendarError,
    VerifiedTradingCalendar,
    verify_trading_calendar,
)
from aquant.planner import PlannedTargets, PlannerLimits
from aquant.portfolio import (
    AvailabilityStatus,
    BuyPosting,
    CashLedgerEvent,
    PortfolioError,
    PortfolioLedger,
    check_bar_availability,
    verify_portfolio_ledger,
)
from aquant.rolling.accounting import (
    RollingPortfolioLedger,
    SellFillEvent,
    SellPosting,
    post_rolling_buy,
    post_rolling_sell,
    verify_rolling_ledger,
)
from aquant.rules import (
    FeeBreakdown,
    FeePolicyError,
    InstrumentKind,
    InstrumentRule,
    OrderIntent,
    OrderSide,
    RejectionReason,
    VerifiedFeePolicy,
    create_buy_lot,
    evaluate_order,
    sellable_size,
    validate_sell_size,
    verify_fee_policy,
)
from aquant.universe import is_supported_instrument_identity

_ZERO = Decimal("0")
_ALLOWED_ADJUSTMENTS = frozenset({"insufficient_cash_including_fees", "partial_sellable_position"})


class RollingAttemptStatus(StrEnum):
    FILLED = "filled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class RollingConfig:
    limits: PlannerLimits

    def __post_init__(self) -> None:
        if type(self.limits) is not PlannerLimits:
            raise PortfolioError("invalid_rolling_config", "limits must be exact")
        PlannerLimits(
            max_single_weight=self.limits.max_single_weight,
            max_gross=self.limits.max_gross,
            min_cash_ratio=self.limits.min_cash_ratio,
        )


@dataclass(frozen=True)
class RollingExecutionInput:
    symbol: str
    instrument_kind: InstrumentKind
    intent_session: date
    execution_session: date
    previous_close: Decimal | None
    execution_open: Decimal | None

    def __post_init__(self) -> None:
        no_bar = self.previous_close is None and self.execution_open is None
        has_bar = (
            type(self.previous_close) is Decimal
            and self.previous_close.is_finite()
            and self.previous_close > 0
            and type(self.execution_open) is Decimal
            and self.execution_open.is_finite()
            and self.execution_open > 0
        )
        if (
            type(self.symbol) is not str
            or not self.symbol
            or type(self.instrument_kind) is not InstrumentKind
            or not is_supported_instrument_identity(
                self.symbol,
                self.instrument_kind.value,
            )
            or type(self.intent_session) is not date
            or type(self.execution_session) is not date
            or not (no_bar or has_bar)
        ):
            raise PortfolioError("invalid_execution_input", "execution input fields are invalid")


@dataclass(frozen=True)
class RebalanceAttempt:
    attempt_id: str
    plan_as_of: date
    execution_session: date
    symbol: str
    side: OrderSide
    target_weight: Decimal
    target_notional_fen: Decimal
    target_shares: int | None
    realized_before: int
    requested_size: int
    feasible_size: int
    filled_size: int
    status: RollingAttemptStatus
    rejection_reason: RejectionReason | None
    fees: FeeBreakdown | None
    cash_before_fen: int
    cash_after_fen: int
    quantity_adjustment_reason: str | None

    def __post_init__(self) -> None:
        expected_id = (
            f"{self.plan_as_of.isoformat()}:{self.execution_session.isoformat()}:"
            f"{self.side.value}:{self.symbol}"
            if type(self.plan_as_of) is date
            and type(self.execution_session) is date
            and type(self.side) is OrderSide
            and type(self.symbol) is str
            else None
        )
        if (
            type(self.attempt_id) is not str
            or self.attempt_id != expected_id
            or not self.symbol
            or type(self.target_weight) is not Decimal
            or not self.target_weight.is_finite()
            or self.target_weight < 0
            or type(self.target_notional_fen) is not Decimal
            or not self.target_notional_fen.is_finite()
            or self.target_notional_fen < 0
            or type(self.target_shares) is not int
            or self.target_shares < 0
            or type(self.realized_before) is not int
            or self.realized_before < 0
            or type(self.requested_size) is not int
            or self.requested_size <= 0
            or type(self.feasible_size) is not int
            or not 0 <= self.feasible_size <= self.requested_size
            or type(self.filled_size) is not int
            or not 0 <= self.filled_size <= self.feasible_size
            or type(self.cash_before_fen) is not int
            or self.cash_before_fen < 0
            or type(self.cash_after_fen) is not int
            or self.cash_after_fen < 0
        ):
            raise PortfolioError("invalid_attempt", "attempt fields are invalid")
        delta = self.target_shares - self.realized_before
        if (
            self.side is OrderSide.BUY
            and delta <= 0
            or self.side is OrderSide.SELL
            and delta >= 0
            or self.requested_size != abs(delta)
        ):
            raise PortfolioError(
                "invalid_attempt",
                "attempt side and quantity do not match the target delta",
            )
        if (
            self.quantity_adjustment_reason is not None
            and self.quantity_adjustment_reason not in _ALLOWED_ADJUSTMENTS
        ):
            raise PortfolioError("invalid_attempt", "invalid quantity adjustment")
        if (
            self.quantity_adjustment_reason == "insufficient_cash_including_fees"
            and (self.side is not OrderSide.BUY or self.feasible_size >= self.requested_size)
            or self.quantity_adjustment_reason == "partial_sellable_position"
            and (self.side is not OrderSide.SELL or self.feasible_size >= self.requested_size)
        ):
            raise PortfolioError(
                "invalid_attempt",
                "quantity adjustment does not match its execution phase",
            )
        if self.status is RollingAttemptStatus.FILLED:
            valid = (
                self.filled_size == self.feasible_size > 0
                and type(self.fees) is FeeBreakdown
                and self.rejection_reason is None
            )
        elif self.status is RollingAttemptStatus.REJECTED:
            valid = (
                self.filled_size == 0
                and self.fees is None
                and type(self.rejection_reason) is RejectionReason
                and self.cash_after_fen == self.cash_before_fen
            )
        else:
            valid = False
        if not valid:
            raise PortfolioError("invalid_attempt", "attempt invariants failed")


@dataclass(frozen=True)
class TargetRealization:
    symbol: str
    desired_weight: Decimal
    target_notional_fen: Decimal
    target_shares: int | None
    realized_shares: int
    residual_shares: int | None
    is_aligned: bool

    def __post_init__(self) -> None:
        known_target = (
            type(self.target_shares) is int
            and self.target_shares >= 0
            and type(self.residual_shares) is int
            and self.residual_shares == abs(self.target_shares - self.realized_shares)
        )
        unknown_target = (
            self.target_shares is None and self.residual_shares is None and self.is_aligned is False
        )
        if (
            type(self.symbol) is not str
            or not self.symbol
            or type(self.desired_weight) is not Decimal
            or not self.desired_weight.is_finite()
            or self.desired_weight < 0
            or type(self.target_notional_fen) is not Decimal
            or not self.target_notional_fen.is_finite()
            or self.target_notional_fen < 0
            or type(self.realized_shares) is not int
            or self.realized_shares < 0
            or type(self.is_aligned) is not bool
            or not (known_target or unknown_target)
            or known_target
            and self.is_aligned is not (self.residual_shares == 0)
        ):
            raise PortfolioError("invalid_target_realization", "target realization is invalid")


@dataclass(frozen=True)
class RollingRebalanceResult:
    planned: PlannedTargets
    execution_session: date
    equity_fen: int
    attempts: tuple[RebalanceAttempt, ...]
    targets: tuple[TargetRealization, ...]
    ledger: RollingPortfolioLedger

    def __post_init__(self) -> None:
        _verify_rebalance_result(self)


def _fail(code: str, message: str) -> None:
    raise PortfolioError(code, message)


def _validate_config(config: object) -> RollingConfig:
    if type(config) is not RollingConfig:
        _fail("invalid_rolling_config", "config must be exact")
    try:
        checked = RollingConfig(config.limits)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PortfolioError("invalid_rolling_config", "config is invalid") from exc
    return checked


def _validate_plan(planned: object) -> PlannedTargets:
    if type(planned) is not PlannedTargets:
        _fail("invalid_planned_targets", "planned targets must be exact")
    try:
        checked = PlannedTargets(as_of=planned.as_of, targets=planned.targets)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PortfolioError("invalid_planned_targets", "planned targets are invalid") from exc
    if checked != planned:
        _fail("invalid_planned_targets", "planned targets are invalid")
    return planned


def _validate_runtime(
    *, calendar: object, fee_policy: object
) -> tuple[VerifiedTradingCalendar, VerifiedFeePolicy]:
    try:
        verify_trading_calendar(calendar)  # type: ignore[arg-type]
    except (AttributeError, CalendarError, TypeError, ValueError) as exc:
        raise PortfolioError("unverified_calendar", "calendar is invalid") from exc
    try:
        verify_fee_policy(fee_policy)  # type: ignore[arg-type]
    except (AttributeError, FeePolicyError, TypeError, ValueError) as exc:
        raise PortfolioError("unverified_fee_policy", "fee policy is invalid") from exc
    return calendar, fee_policy  # type: ignore[return-value]


def _execution_session(calendar: VerifiedTradingCalendar, planned: PlannedTargets) -> date:
    if not calendar.contains(planned.as_of):
        _fail("missing_calendar_coverage", "plan date is outside the calendar")
    execution_session = calendar.next_session(planned.as_of)
    if execution_session is None:
        _fail("no_next_session_in_range", "calendar has no execution session")
    return execution_session


def _equity_at_plan_close(ledger: RollingPortfolioLedger, as_of: date) -> int:
    verify_rolling_ledger(ledger)
    if (
        any(event.session > as_of for event in ledger.cash_events)
        or any(lot.acquired_date > as_of for lot in ledger.lots)
        or any(item.registered_date > as_of for item in ledger.receivables)
    ):
        _fail(
            "post_plan_ledger_state",
            "ledger contains state after the plan close",
        )
    if ledger.daily_snapshots:
        snapshot = ledger.daily_snapshots[-1]
        if snapshot.session != as_of:
            _fail("missing_plan_close_snapshot", "latest snapshot must equal plan date")
        return snapshot.equity_fen

    pristine = (
        ledger.lots == ()
        and ledger.cash_events == ()
        and ledger.receivables == ()
        and ledger.daily_snapshots == ()
        and ledger.cash_fen == ledger.initial_cash_fen
    )
    if not pristine:
        _fail("non_pristine_without_snapshot", "non-pristine ledger needs a T close")
    legacy = PortfolioLedger(
        initial_cash_fen=ledger.initial_cash_fen,
        cash_fen=ledger.cash_fen,
        lots=ledger.lots,
        cash_events=(),
        receivables=ledger.receivables,
        daily_snapshots=ledger.daily_snapshots,
    )
    verify_portfolio_ledger(legacy)
    return ledger.initial_cash_fen


def _validate_execution_inputs(
    value: object,
    *,
    planned: PlannedTargets,
    execution_session: date,
) -> tuple[RollingExecutionInput, ...]:
    if type(value) is not tuple:
        _fail("invalid_execution_inputs", "execution inputs must be an exact tuple")
    checked: list[RollingExecutionInput] = []
    for item in value:
        if type(item) is not RollingExecutionInput:
            _fail("invalid_execution_input", "execution input must be exact")
        no_bar = item.previous_close is None and item.execution_open is None
        has_bar = (
            type(item.previous_close) is Decimal
            and item.previous_close.is_finite()
            and item.previous_close > 0
            and type(item.execution_open) is Decimal
            and item.execution_open.is_finite()
            and item.execution_open > 0
        )
        if (
            type(item.symbol) is not str
            or not item.symbol
            or type(item.instrument_kind) is not InstrumentKind
            or not is_supported_instrument_identity(
                item.symbol,
                item.instrument_kind.value,
            )
            or type(item.intent_session) is not date
            or item.intent_session != planned.as_of
            or type(item.execution_session) is not date
            or item.execution_session != execution_session
            or not (no_bar or has_bar)
        ):
            _fail("invalid_execution_input", "execution input fields are invalid")
        checked.append(item)
    symbols = tuple(item.symbol for item in checked)
    if len(symbols) != len(set(symbols)):
        _fail("duplicate_execution_input", "execution input symbols must be unique")
    normalized = tuple(sorted(checked, key=lambda item: item.symbol))
    if {item.symbol for item in normalized} != set(planned.targets):
        _fail("execution_input_mismatch", "execution inputs must cover the plan")
    return normalized


def _realized_shares(ledger: RollingPortfolioLedger) -> dict[str, int]:
    realized: dict[str, int] = {}
    for lot in ledger.lots:
        if lot.remaining_size:
            realized[lot.symbol] = realized.get(lot.symbol, 0) + lot.remaining_size
    return realized


def _validate_lot_calendar_bindings(
    ledger: RollingPortfolioLedger,
    calendar: VerifiedTradingCalendar,
) -> None:
    for lot in ledger.lots:
        if lot.remaining_size == 0:
            continue
        if not calendar.contains(lot.acquired_date):
            _fail(
                "lot_acquired_date_outside_calendar",
                "remaining lot acquisition date is outside the verified calendar",
            )
        expected_available = calendar.next_session(lot.acquired_date)
        if expected_available is None:
            _fail(
                "lot_availability_calendar_end",
                "remaining lot has no official T+1 availability session",
            )
        if lot.available_date != expected_available:
            _fail(
                "lot_availability_binding_mismatch",
                "remaining lot availability is not the official T+1 session",
            )


def _decimal_times_int(value: Decimal, multiplier: int) -> Decimal:
    """Multiply finite non-negative Decimal by int without context rounding."""
    decimal_tuple = value.as_tuple()
    coefficient = int("".join(str(digit) for digit in decimal_tuple.digits))
    product = coefficient * multiplier
    digits = tuple(int(digit) for digit in str(product)) if product else (0,)
    return Decimal((decimal_tuple.sign, digits, decimal_tuple.exponent))


def _event_matches_filled_attempt(
    *,
    event: object,
    attempt: RebalanceAttempt,
    ledger: RollingPortfolioLedger,
) -> bool:
    if (
        type(event) not in (CashLedgerEvent, SellFillEvent)
        or event.session != attempt.execution_session
        or event.symbol != attempt.symbol
        or event.cash_before_fen != attempt.cash_before_fen
        or event.cash_after_fen != attempt.cash_after_fen
        or attempt.fees is None
        or event.commission_fen != attempt.fees.commission_fen
        or event.stamp_duty_fen != attempt.fees.stamp_duty_fen
        or event.transfer_fee_fen != attempt.fees.transfer_fee_fen
    ):
        return False
    if attempt.side is OrderSide.SELL:
        return type(event) is SellFillEvent and event.size == attempt.filled_size
    if type(event) is not CashLedgerEvent or event.side is not OrderSide.BUY:
        return False
    matching_lots = tuple(lot for lot in ledger.lots if lot.lot_id == event.reference_id)
    return (
        len(matching_lots) == 1
        and matching_lots[0].symbol == attempt.symbol
        and matching_lots[0].acquired_date == attempt.execution_session
        and matching_lots[0].original_size == attempt.filled_size
    )


def _verify_rebalance_result(result: RollingRebalanceResult) -> None:
    if type(result.planned) is not PlannedTargets:
        _fail("invalid_rebalance_result", "result planned targets must be exact")
    try:
        checked_plan = PlannedTargets(
            as_of=result.planned.as_of,
            targets=result.planned.targets,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PortfolioError(
            "invalid_rebalance_result",
            "result planned targets are invalid",
        ) from exc
    if checked_plan != result.planned:
        _fail("invalid_rebalance_result", "result planned targets are invalid")
    if (
        type(result.execution_session) is not date
        or result.execution_session <= result.planned.as_of
        or type(result.equity_fen) is not int
        or isinstance(result.equity_fen, bool)
        or result.equity_fen <= 0
        or type(result.attempts) is not tuple
        or any(type(item) is not RebalanceAttempt for item in result.attempts)
        or type(result.targets) is not tuple
        or any(type(item) is not TargetRealization for item in result.targets)
        or type(result.ledger) is not RollingPortfolioLedger
    ):
        _fail("invalid_rebalance_result", "result fields are invalid")
    verify_rolling_ledger(result.ledger)

    planned_symbols = tuple(result.planned.targets)
    target_symbols = tuple(item.symbol for item in result.targets)
    if target_symbols != planned_symbols:
        _fail(
            "invalid_rebalance_result",
            "result targets must exactly match planned symbol order",
        )
    realized = _realized_shares(result.ledger)
    if set(realized).difference(planned_symbols):
        _fail(
            "invalid_rebalance_result",
            "terminal ledger contains an unplanned held symbol",
        )
    targets_by_symbol = {item.symbol: item for item in result.targets}
    for target in result.targets:
        expected_notional = _decimal_times_int(
            result.planned.targets[target.symbol],
            result.equity_fen,
        )
        if (
            target.desired_weight != result.planned.targets[target.symbol]
            or target.target_notional_fen != expected_notional
            or target.realized_shares != realized.get(target.symbol, 0)
        ):
            _fail(
                "invalid_rebalance_result",
                "target evidence does not reconcile to plan and terminal ledger",
            )

    attempt_ids = tuple(item.attempt_id for item in result.attempts)
    if len(attempt_ids) != len(set(attempt_ids)):
        _fail("invalid_rebalance_result", "attempt IDs must be unique")
    phase_keys = tuple(
        (0 if item.side is OrderSide.SELL else 1, item.symbol) for item in result.attempts
    )
    if phase_keys != tuple(sorted(phase_keys)):
        _fail(
            "invalid_rebalance_result",
            "attempts must be sorted SELL phase then BUY phase",
        )
    for previous, current in zip(
        result.attempts,
        result.attempts[1:],
        strict=False,
    ):
        if previous.cash_after_fen != current.cash_before_fen:
            _fail("invalid_rebalance_result", "attempt cash chain is discontinuous")
    if result.attempts and result.attempts[-1].cash_after_fen != result.ledger.cash_fen:
        _fail(
            "invalid_rebalance_result",
            "attempt cash chain does not reach terminal ledger cash",
        )

    events_by_id = {event.event_id: event for event in result.ledger.cash_events}
    current_events = tuple(
        event for event in result.ledger.cash_events if event.session > result.planned.as_of
    )
    filled_attempt_ids = {
        attempt.attempt_id
        for attempt in result.attempts
        if attempt.status is RollingAttemptStatus.FILLED
    }
    if (
        any(event.session != result.execution_session for event in current_events)
        or {event.event_id for event in current_events} != filled_attempt_ids
    ):
        _fail(
            "invalid_rebalance_result",
            "current execution events must exactly match filled attempts",
        )
    for attempt in result.attempts:
        target = targets_by_symbol.get(attempt.symbol)
        terminal_from_attempt = (
            attempt.realized_before + attempt.filled_size
            if attempt.side is OrderSide.BUY
            else attempt.realized_before - attempt.filled_size
        )
        if (
            attempt.plan_as_of != result.planned.as_of
            or attempt.execution_session != result.execution_session
            or target is None
            or attempt.target_weight != target.desired_weight
            or attempt.target_notional_fen != target.target_notional_fen
            or attempt.target_shares != target.target_shares
            or terminal_from_attempt != target.realized_shares
        ):
            _fail(
                "invalid_rebalance_result",
                "attempt evidence does not align with its result target",
            )
        event = events_by_id.get(attempt.attempt_id)
        if attempt.status is RollingAttemptStatus.FILLED:
            if not _event_matches_filled_attempt(
                event=event,
                attempt=attempt,
                ledger=result.ledger,
            ):
                _fail(
                    "invalid_rebalance_result",
                    "filled attempt lacks a matching ledger event",
                )
        elif event is not None:
            _fail(
                "invalid_rebalance_result",
                "rejected attempt must not have a ledger event",
            )


def _exact_decimal_sum(values: tuple[Decimal, ...]) -> Decimal:
    nonzero = tuple(value for value in values if value != _ZERO)
    if not nonzero:
        return _ZERO
    exponent = min(int(value.as_tuple().exponent) for value in nonzero)
    total = 0
    for value in nonzero:
        item = value.as_tuple()
        coefficient = int("".join(str(digit) for digit in item.digits))
        total += coefficient * 10 ** (int(item.exponent) - exponent)
    return Decimal((0, tuple(int(digit) for digit in str(total)), exponent))


def _floor_target_shares(*, target_notional_fen: Decimal, execution_open: Decimal) -> int:
    exact_shares = Fraction(target_notional_fen) / (100 * Fraction(execution_open))
    return exact_shares.numerator // exact_shares.denominator // 100 * 100


def _sized_targets(
    *,
    planned: PlannedTargets,
    equity_fen: int,
    execution_inputs: tuple[RollingExecutionInput, ...],
    realized: dict[str, int],
    limits: PlannerLimits,
) -> tuple[TargetRealization, ...]:
    missing = set(realized).difference(planned.targets)
    if missing:
        _fail("held_symbol_missing_from_plan", "held symbol is absent from effective plan")
    by_symbol = {item.symbol: item for item in execution_inputs}
    if equity_fen <= 0:
        _fail("invalid_equity", "T close equity must be positive")
    notionals = {
        symbol: _decimal_times_int(weight, equity_fen) for symbol, weight in planned.targets.items()
    }
    total_notional = _exact_decimal_sum(tuple(notionals.values()))
    gross_limit_notional = _decimal_times_int(limits.max_gross, equity_fen)
    if total_notional > gross_limit_notional:
        _fail("max_gross_exceeded", "target gross exceeds rolling limit")

    targets: list[TargetRealization] = []
    for symbol, weight in planned.targets.items():
        target_notional = notionals[symbol]
        execution_input = by_symbol[symbol]
        if weight == _ZERO:
            target_shares: int | None = 0
        elif execution_input.execution_open is None:
            target_shares = None
        else:
            target_shares = _floor_target_shares(
                target_notional_fen=target_notional,
                execution_open=execution_input.execution_open,
            )
        current = realized.get(symbol, 0)
        residual = None if target_shares is None else abs(target_shares - current)
        targets.append(
            TargetRealization(
                symbol=symbol,
                desired_weight=weight,
                target_notional_fen=target_notional,
                target_shares=target_shares,
                realized_shares=current,
                residual_shares=residual,
                is_aligned=residual == 0,
            )
        )
    return tuple(targets)


def _attempt_id(*, plan_as_of: date, execution_session: date, side: OrderSide, symbol: str) -> str:
    return f"{plan_as_of.isoformat()}:{execution_session.isoformat()}:{side.value}:{symbol}"


def _rejected_attempt(
    *,
    planned: PlannedTargets,
    execution_session: date,
    target: TargetRealization,
    side: OrderSide,
    realized_before: int,
    requested_size: int,
    feasible_size: int,
    reason: RejectionReason,
    cash_fen: int,
    quantity_adjustment_reason: str | None = None,
) -> RebalanceAttempt:
    return RebalanceAttempt(
        attempt_id=_attempt_id(
            plan_as_of=planned.as_of,
            execution_session=execution_session,
            side=side,
            symbol=target.symbol,
        ),
        plan_as_of=planned.as_of,
        execution_session=execution_session,
        symbol=target.symbol,
        side=side,
        target_weight=target.desired_weight,
        target_notional_fen=target.target_notional_fen,
        target_shares=target.target_shares,
        realized_before=realized_before,
        requested_size=requested_size,
        feasible_size=feasible_size,
        filled_size=0,
        status=RollingAttemptStatus.REJECTED,
        rejection_reason=reason,
        fees=None,
        cash_before_fen=cash_fen,
        cash_after_fen=cash_fen,
        quantity_adjustment_reason=quantity_adjustment_reason,
    )


def _filled_attempt(
    *,
    planned: PlannedTargets,
    execution_session: date,
    target: TargetRealization,
    side: OrderSide,
    realized_before: int,
    requested_size: int,
    feasible_size: int,
    fees: FeeBreakdown,
    cash_before_fen: int,
    cash_after_fen: int,
    quantity_adjustment_reason: str | None = None,
) -> RebalanceAttempt:
    return RebalanceAttempt(
        attempt_id=_attempt_id(
            plan_as_of=planned.as_of,
            execution_session=execution_session,
            side=side,
            symbol=target.symbol,
        ),
        plan_as_of=planned.as_of,
        execution_session=execution_session,
        symbol=target.symbol,
        side=side,
        target_weight=target.desired_weight,
        target_notional_fen=target.target_notional_fen,
        target_shares=target.target_shares,
        realized_before=realized_before,
        requested_size=requested_size,
        feasible_size=feasible_size,
        filled_size=feasible_size,
        status=RollingAttemptStatus.FILLED,
        rejection_reason=None,
        fees=fees,
        cash_before_fen=cash_before_fen,
        cash_after_fen=cash_after_fen,
        quantity_adjustment_reason=quantity_adjustment_reason,
    )


def _available(
    *,
    execution_input: RollingExecutionInput,
    calendar: VerifiedTradingCalendar,
) -> bool:
    available_bar_dates = (
        frozenset()
        if execution_input.execution_open is None
        else frozenset({execution_input.execution_session})
    )
    decision = check_bar_availability(
        intent_session=execution_input.intent_session,
        execution_session=execution_input.execution_session,
        calendar=calendar,
        available_bar_dates=available_bar_dates,
    )
    return decision.status is AvailabilityStatus.AVAILABLE


def _decision(
    *,
    planned: PlannedTargets,
    execution_input: RollingExecutionInput,
    side: OrderSide,
    requested_size: int,
    ledger: RollingPortfolioLedger,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
):
    assert execution_input.previous_close is not None
    assert execution_input.execution_open is not None
    return evaluate_order(
        intent=OrderIntent(
            order_id=_attempt_id(
                plan_as_of=planned.as_of,
                execution_session=execution_input.execution_session,
                side=side,
                symbol=execution_input.symbol,
            ),
            symbol=execution_input.symbol,
            signal_date=planned.as_of,
            side=side,
            requested_size=requested_size,
        ),
        instrument=InstrumentRule(
            symbol=execution_input.symbol,
            kind=execution_input.instrument_kind,
        ),
        calendar=calendar,
        available_bar_dates=frozenset({execution_input.execution_session}),
        previous_close=execution_input.previous_close,
        execution_open=execution_input.execution_open,
        cash_fen=ledger.cash_fen,
        lots=tuple(lot for lot in ledger.lots if lot.symbol == execution_input.symbol),
        fee_policy=fee_policy,
    )


def _execute(
    *,
    planned: PlannedTargets,
    ledger: RollingPortfolioLedger,
    targets: tuple[TargetRealization, ...],
    execution_inputs: tuple[RollingExecutionInput, ...],
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
) -> tuple[RollingPortfolioLedger, tuple[RebalanceAttempt, ...]]:
    inputs = {item.symbol: item for item in execution_inputs}
    current = ledger
    attempts: list[RebalanceAttempt] = []
    phases: dict[OrderSide, list[tuple[TargetRealization, int, int]]] = {
        OrderSide.SELL: [],
        OrderSide.BUY: [],
    }
    for target in targets:
        if target.target_shares is None:
            continue
        delta = target.target_shares - target.realized_shares
        if delta < 0:
            phases[OrderSide.SELL].append((target, target.realized_shares, -delta))
        elif delta > 0:
            phases[OrderSide.BUY].append((target, target.realized_shares, delta))

    for side in (OrderSide.SELL, OrderSide.BUY):
        for target, realized_before, requested_size in phases[side]:
            execution_input = inputs[target.symbol]
            cash_before = current.cash_fen
            if not _available(execution_input=execution_input, calendar=calendar):
                attempts.append(
                    _rejected_attempt(
                        planned=planned,
                        execution_session=execution_input.execution_session,
                        target=target,
                        side=side,
                        realized_before=realized_before,
                        requested_size=requested_size,
                        feasible_size=0,
                        reason=RejectionReason.SUSPENDED_NO_BAR,
                        cash_fen=cash_before,
                    )
                )
                continue

            feasible_size = requested_size
            adjustment: str | None = None
            if side is OrderSide.SELL:
                symbol_lots = tuple(lot for lot in current.lots if lot.symbol == target.symbol)
                available = sellable_size(
                    symbol_lots,
                    execution_input.execution_session,
                )
                if available < requested_size:
                    feasible_size = available
                    adjustment = "partial_sellable_position"
                total = sum(lot.remaining_size for lot in symbol_lots)
                if feasible_size == 0 or not validate_sell_size(total, feasible_size):
                    attempts.append(
                        _rejected_attempt(
                            planned=planned,
                            execution_session=execution_input.execution_session,
                            target=target,
                            side=side,
                            realized_before=realized_before,
                            requested_size=requested_size,
                            feasible_size=0,
                            reason=RejectionReason.INSUFFICIENT_SELLABLE_POSITION,
                            cash_fen=cash_before,
                            quantity_adjustment_reason=adjustment,
                        )
                    )
                    continue

            decision = _decision(
                planned=planned,
                execution_input=execution_input,
                side=side,
                requested_size=feasible_size,
                ledger=current,
                calendar=calendar,
                fee_policy=fee_policy,
            )
            if side is OrderSide.BUY:
                while (
                    not decision.allowed
                    and decision.reason is RejectionReason.INSUFFICIENT_CASH
                    and feasible_size > 100
                ):
                    feasible_size -= 100
                    adjustment = "insufficient_cash_including_fees"
                    decision = _decision(
                        planned=planned,
                        execution_input=execution_input,
                        side=side,
                        requested_size=feasible_size,
                        ledger=current,
                        calendar=calendar,
                        fee_policy=fee_policy,
                    )
                if not decision.allowed and decision.reason is RejectionReason.INSUFFICIENT_CASH:
                    adjustment = "insufficient_cash_including_fees"
                    feasible_size = 0

            if not decision.allowed:
                if decision.reason not in {
                    RejectionReason.PRICE_LIMIT_OPEN,
                    RejectionReason.INSUFFICIENT_CASH,
                }:
                    _fail(
                        "rule_contract_rejected",
                        f"unexpected rule rejection: {decision.reason}",
                    )
                assert decision.reason is not None
                attempts.append(
                    _rejected_attempt(
                        planned=planned,
                        execution_session=execution_input.execution_session,
                        target=target,
                        side=side,
                        realized_before=realized_before,
                        requested_size=requested_size,
                        feasible_size=feasible_size,
                        reason=decision.reason,
                        cash_fen=cash_before,
                        quantity_adjustment_reason=adjustment,
                    )
                )
                continue

            assert decision.fees is not None
            assert execution_input.execution_open is not None
            event_id = _attempt_id(
                plan_as_of=planned.as_of,
                execution_session=execution_input.execution_session,
                side=side,
                symbol=target.symbol,
            )
            if side is OrderSide.SELL:
                current = post_rolling_sell(
                    current,
                    SellPosting(
                        event_id=event_id,
                        execution_date=execution_input.execution_session,
                        symbol=target.symbol,
                        size=feasible_size,
                        unit_price=execution_input.execution_open,
                        fees=decision.fees,
                    ),
                )
            else:
                lot = create_buy_lot(
                    lot_id=event_id,
                    symbol=target.symbol,
                    acquired_date=execution_input.execution_session,
                    size=feasible_size,
                    unit_cost=execution_input.execution_open,
                    calendar=calendar,
                )
                current = post_rolling_buy(
                    current,
                    BuyPosting(
                        event_id=event_id,
                        execution_date=execution_input.execution_session,
                        lot=lot,
                        fees=decision.fees,
                    ),
                )
            attempts.append(
                _filled_attempt(
                    planned=planned,
                    execution_session=execution_input.execution_session,
                    target=target,
                    side=side,
                    realized_before=realized_before,
                    requested_size=requested_size,
                    feasible_size=feasible_size,
                    fees=decision.fees,
                    cash_before_fen=cash_before,
                    cash_after_fen=current.cash_fen,
                    quantity_adjustment_reason=adjustment,
                )
            )
    return current, tuple(attempts)


def rebalance_to_plan(
    *,
    config: RollingConfig,
    planned: PlannedTargets,
    ledger: RollingPortfolioLedger,
    execution_inputs: tuple[RollingExecutionInput, ...],
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
) -> RollingRebalanceResult:
    """Validate and size one exact effective plan against its T close."""
    checked_config = _validate_config(config)
    checked_plan = _validate_plan(planned)
    checked_calendar, checked_fee_policy = _validate_runtime(
        calendar=calendar, fee_policy=fee_policy
    )
    next_session = _execution_session(checked_calendar, checked_plan)
    equity_fen = _equity_at_plan_close(ledger, checked_plan.as_of)
    _validate_lot_calendar_bindings(ledger, checked_calendar)
    normalized_inputs = _validate_execution_inputs(
        execution_inputs,
        planned=checked_plan,
        execution_session=next_session,
    )
    staged_targets = _sized_targets(
        planned=checked_plan,
        equity_fen=equity_fen,
        execution_inputs=normalized_inputs,
        realized=_realized_shares(ledger),
        limits=checked_config.limits,
    )
    updated_ledger, attempts = _execute(
        planned=checked_plan,
        ledger=ledger,
        targets=staged_targets,
        execution_inputs=normalized_inputs,
        calendar=checked_calendar,
        fee_policy=checked_fee_policy,
    )
    targets = _sized_targets(
        planned=checked_plan,
        equity_fen=equity_fen,
        execution_inputs=normalized_inputs,
        realized=_realized_shares(updated_ledger),
        limits=checked_config.limits,
    )
    return RollingRebalanceResult(
        planned=checked_plan,
        execution_session=next_session,
        equity_fen=equity_fen,
        attempts=attempts,
        targets=targets,
        ledger=updated_ledger,
    )
