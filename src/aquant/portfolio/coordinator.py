"""Deterministic official-session coordinator for v0.2 portfolios."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from aquant.data.calendar_snapshot import (
    CalendarError,
    VerifiedTradingCalendar,
    verify_trading_calendar,
)
from aquant.portfolio.accounting import (
    BuyPosting,
    CashReceivable,
    PortfolioLedger,
    SymbolValuation,
    close_session,
    create_portfolio_ledger,
    decimal_yuan_to_fen,
    notional_fen,
    pay_receivables,
    post_buy,
    register_receivable,
)
from aquant.portfolio.availability import (
    AvailabilityStatus,
    check_bar_availability,
)
from aquant.portfolio.models import (
    PortfolioConfig,
    PortfolioError,
    PortfolioInstrumentInput,
    TargetAllocation,
    allocate_equal_targets,
    validate_portfolio_inputs,
)
from aquant.rules import (
    FeeBreakdown,
    FeePolicyError,
    FeeRateTouch,
    InstrumentRule,
    OrderIntent,
    OrderSide,
    RejectionReason,
    RuleInputError,
    VerifiedFeePolicy,
    calculate_fees,
    create_buy_lot,
    evaluate_order,
    sellable_size,
    verify_fee_policy,
)
from aquant.universe import VerifiedUniverse

_SYMBOL_RE = re.compile(r"[0-9]{6}")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class TargetStatus(StrEnum):
    """Lifecycle of one fixed-notional root entry target."""

    PENDING = "pending"
    FILLED = "filled"
    EXPIRED_UNFILLED = "expired_unfilled"


class AttemptStatus(StrEnum):
    """Outcome of one official-session entry attempt."""

    FILLED = "filled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class EntryTarget:
    """One immutable root target shared by all of its attempts."""

    target_id: str
    symbol: str
    signal_date: date
    target_notional_fen: int
    attempts_used: int
    status: TargetStatus
    fill_event_id: str | None

    def __post_init__(self) -> None:
        if (
            type(self.target_id) is not str
            or _IDENTIFIER_RE.fullmatch(self.target_id) is None
            or type(self.symbol) is not str
            or _SYMBOL_RE.fullmatch(self.symbol) is None
            or type(self.signal_date) is not date
            or type(self.target_notional_fen) is not int
            or self.target_notional_fen <= 0
            or type(self.attempts_used) is not int
            or self.attempts_used < 0
            or type(self.status) is not TargetStatus
            or self.status is TargetStatus.FILLED
            and (
                type(self.fill_event_id) is not str
                or _IDENTIFIER_RE.fullmatch(self.fill_event_id) is None
            )
            or self.status is not TargetStatus.FILLED
            and self.fill_event_id is not None
        ):
            raise PortfolioError("invalid_target", "portfolio target is invalid")


@dataclass(frozen=True)
class EntryAttempt:
    """One immutable exact-session attempt against a root target."""

    attempt_id: str
    target_id: str
    symbol: str
    original_signal_date: date
    intent_session: date
    execution_session: date
    attempt_number: int
    initial_candidate_size: int
    requested_size: int
    availability_status: AvailabilityStatus
    status: AttemptStatus
    rejection_reason: RejectionReason | None
    fees: FeeBreakdown | None
    fill_event_id: str | None
    cash_available_before_fen: int | None
    initial_candidate_cash_required_fen: int | None
    requested_cash_required_fen: int | None
    quantity_adjustment_reason: str | None

    def __post_init__(self) -> None:
        identifiers = (self.attempt_id, self.target_id)
        if (
            any(
                type(item) is not str or _IDENTIFIER_RE.fullmatch(item) is None
                for item in identifiers
            )
            or type(self.symbol) is not str
            or _SYMBOL_RE.fullmatch(self.symbol) is None
            or type(self.original_signal_date) is not date
            or type(self.intent_session) is not date
            or type(self.execution_session) is not date
            or type(self.attempt_number) is not int
            or self.attempt_number <= 0
            or type(self.initial_candidate_size) is not int
            or self.initial_candidate_size < 0
            or self.initial_candidate_size % 100 != 0
            or type(self.requested_size) is not int
            or self.requested_size < 0
            or self.requested_size % 100 != 0
            or self.requested_size > self.initial_candidate_size
            or type(self.availability_status) is not AvailabilityStatus
            or type(self.status) is not AttemptStatus
        ):
            raise PortfolioError("invalid_attempt", "portfolio attempt is invalid")
        if self.status is AttemptStatus.FILLED:
            if (
                self.availability_status is not AvailabilityStatus.AVAILABLE
                or self.requested_size <= 0
                or self.rejection_reason is not None
                or type(self.fees) is not FeeBreakdown
                or type(self.fees.commission_fen) is not int
                or self.fees.commission_fen < 0
                or type(self.fees.stamp_duty_fen) is not int
                or self.fees.stamp_duty_fen < 0
                or type(self.fees.transfer_fee_fen) is not int
                or self.fees.transfer_fee_fen < 0
                or type(self.fees.touched_rates) is not tuple
                or any(
                    type(touch) is not FeeRateTouch
                    or type(touch.fee_name) is not str
                    or not touch.fee_name
                    or touch.effective_date is not None
                    and type(touch.effective_date) is not date
                    or type(touch.rate) is not Decimal
                    or not touch.rate.is_finite()
                    or touch.rate < 0
                    or touch.minimum_yuan is not None
                    and (
                        type(touch.minimum_yuan) is not Decimal
                        or not touch.minimum_yuan.is_finite()
                        or touch.minimum_yuan < 0
                    )
                    for touch in self.fees.touched_rates
                )
                or type(self.fill_event_id) is not str
                or _IDENTIFIER_RE.fullmatch(self.fill_event_id) is None
                or type(self.cash_available_before_fen) is not int
                or self.cash_available_before_fen < 0
                or type(self.initial_candidate_cash_required_fen) is not int
                or self.initial_candidate_cash_required_fen <= 0
                or type(self.requested_cash_required_fen) is not int
                or self.requested_cash_required_fen <= 0
                or self.initial_candidate_cash_required_fen
                < self.requested_cash_required_fen
                or self.cash_available_before_fen
                < self.requested_cash_required_fen
            ):
                raise PortfolioError("invalid_attempt", "filled attempt is invalid")
            if self.initial_candidate_size == self.requested_size:
                if (
                    self.quantity_adjustment_reason is not None
                    or self.initial_candidate_cash_required_fen
                    != self.requested_cash_required_fen
                ):
                    raise PortfolioError(
                        "invalid_attempt",
                        "unadjusted filled attempt evidence is invalid",
                    )
            elif (
                self.quantity_adjustment_reason
                != "insufficient_cash_including_fees"
                or self.cash_available_before_fen
                >= self.initial_candidate_cash_required_fen
                or self.initial_candidate_cash_required_fen
                <= self.requested_cash_required_fen
            ):
                raise PortfolioError(
                    "invalid_attempt",
                    "adjusted filled attempt evidence is invalid",
                )
        elif (
            type(self.rejection_reason) is not RejectionReason
            or self.fees is not None
            or self.fill_event_id is not None
            or self.cash_available_before_fen is not None
            or self.initial_candidate_cash_required_fen is not None
            or self.requested_cash_required_fen is not None
            or self.quantity_adjustment_reason is not None
        ):
            raise PortfolioError("invalid_attempt", "rejected attempt is invalid")


@dataclass(frozen=True)
class PortfolioBacktestResult:
    """Gate B's immutable in-memory result."""

    config: PortfolioConfig
    allocation: TargetAllocation
    targets: tuple[EntryTarget, ...]
    attempts: tuple[EntryAttempt, ...]
    dividends: tuple[DividendAudit, ...]
    availability: tuple[AvailabilityAudit, ...]
    ledger: PortfolioLedger


@dataclass(frozen=True)
class DividendAudit:
    """Exact entitlement and cash-date evidence for one verified event."""

    event_id: str
    symbol: str
    ex_date: date
    source_payable_date: date
    actual_cash_date: date
    entitled_size: int
    cash_dividend_per_unit: Decimal
    amount_fen: int

    def __post_init__(self) -> None:
        if (
            type(self.event_id) is not str
            or _IDENTIFIER_RE.fullmatch(self.event_id) is None
            or type(self.symbol) is not str
            or _SYMBOL_RE.fullmatch(self.symbol) is None
            or type(self.ex_date) is not date
            or type(self.source_payable_date) is not date
            or type(self.actual_cash_date) is not date
            or self.source_payable_date < self.ex_date
            or self.actual_cash_date < self.source_payable_date
            or type(self.entitled_size) is not int
            or self.entitled_size < 0
            or type(self.cash_dividend_per_unit) is not Decimal
            or not self.cash_dividend_per_unit.is_finite()
            or self.cash_dividend_per_unit <= 0
            or type(self.amount_fen) is not int
            or self.amount_fen < 0
        ):
            raise PortfolioError(
                "invalid_dividend_audit",
                "dividend audit record is invalid",
            )


@dataclass(frozen=True)
class AvailabilityAudit:
    """One symbol's same-session trusted-mark evidence."""

    session: date
    symbol: str
    status: AvailabilityStatus
    mark_price: Decimal
    carried_sessions: int
    adjustment_reason: str

    def __post_init__(self) -> None:
        allowed_reasons = {"bar_close", "cash_dividend", "no_bar_carry"}
        if (
            type(self.session) is not date
            or type(self.symbol) is not str
            or _SYMBOL_RE.fullmatch(self.symbol) is None
            or type(self.status) is not AvailabilityStatus
            or type(self.mark_price) is not Decimal
            or not self.mark_price.is_finite()
            or self.mark_price <= 0
            or type(self.carried_sessions) is not int
            or self.carried_sessions < 0
            or type(self.adjustment_reason) is not str
            or self.adjustment_reason not in allowed_reasons
            or self.status is AvailabilityStatus.AVAILABLE
            and (
                self.carried_sessions != 0
                or self.adjustment_reason != "bar_close"
            )
            or self.status is AvailabilityStatus.NO_BAR_UNAVAILABLE
            and self.adjustment_reason == "bar_close"
        ):
            raise PortfolioError(
                "invalid_availability_audit",
                "availability audit record is invalid",
            )


def actual_cash_date(
    calendar: VerifiedTradingCalendar,
    source_payable_date: date,
) -> date:
    """Return the first official session not earlier than the source date."""
    try:
        verify_trading_calendar(calendar)
    except (AttributeError, CalendarError, TypeError, ValueError) as exc:
        raise PortfolioError(
            "unverified_calendar",
            "cash-date calculation requires an exact verified calendar",
        ) from exc
    if type(source_payable_date) is not date:
        raise PortfolioError("invalid_payable_date", "payable date is invalid")
    result = (
        source_payable_date
        if calendar.contains(source_payable_date)
        else calendar.next_session(source_payable_date)
    )
    if result is None:
        raise PortfolioError(
            "missing_calendar_coverage",
            "calendar does not cover the dividend cash date",
        )
    return result


def _decimal_price(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError) as exc:
        raise PortfolioError("invalid_market_price", "market price is invalid") from exc
    if not result.is_finite() or result <= 0:
        raise PortfolioError("invalid_market_price", "market price is invalid")
    return result


def _bar_map(item: PortfolioInstrumentInput) -> dict[date, dict[str, Decimal]]:
    frame = item.market_data.frame
    return {
        row.date.date(): {
            "open": _decimal_price(row.open),
            "close": _decimal_price(row.close),
        }
        for row in frame.itertuples(index=False)
    }


def _initial_target(
    symbol: str,
    config: PortfolioConfig,
    allocation: TargetAllocation,
) -> EntryTarget:
    return EntryTarget(
        target_id=f"target:{symbol}:{config.signal_date.isoformat()}",
        symbol=symbol,
        signal_date=config.signal_date,
        target_notional_fen=allocation.per_symbol_target_notional_fen,
        attempts_used=0,
        status=TargetStatus.PENDING,
        fill_event_id=None,
    )


def _rejected_attempt(
    *,
    target: EntryTarget,
    session: date,
    intent_session: date,
    availability_status: AvailabilityStatus,
    initial_candidate_size: int,
    requested_size: int,
    reason: RejectionReason,
) -> EntryAttempt:
    number = target.attempts_used + 1
    return EntryAttempt(
        attempt_id=f"attempt:{target.symbol}:{target.signal_date.isoformat()}:{number}",
        target_id=target.target_id,
        symbol=target.symbol,
        original_signal_date=target.signal_date,
        intent_session=intent_session,
        execution_session=session,
        attempt_number=number,
        initial_candidate_size=initial_candidate_size,
        requested_size=requested_size,
        availability_status=availability_status,
        status=AttemptStatus.REJECTED,
        rejection_reason=reason,
        fees=None,
        fill_event_id=None,
        cash_available_before_fen=None,
        initial_candidate_cash_required_fen=None,
        requested_cash_required_fen=None,
        quantity_adjustment_reason=None,
    )


def _record_rejection(
    target: EntryTarget,
    attempt: EntryAttempt,
    *,
    max_attempts: int,
) -> EntryTarget:
    attempts_used = target.attempts_used + 1
    return replace(
        target,
        attempts_used=attempts_used,
        status=(
            TargetStatus.EXPIRED_UNFILLED
            if attempts_used == max_attempts
            else TargetStatus.PENDING
        ),
    )


def _position_valuations(
    ledger: PortfolioLedger,
    *,
    session: date,
    marks: dict[str, Decimal],
) -> tuple[SymbolValuation, ...]:
    symbols = sorted(
        {
            lot.symbol
            for lot in ledger.lots
            if lot.acquired_date <= session and lot.remaining_size > 0
        }
    )
    values: list[SymbolValuation] = []
    for symbol in symbols:
        lots = tuple(
            item
            for item in ledger.lots
            if item.symbol == symbol and item.remaining_size > 0
        )
        total = sum(item.remaining_size for item in lots)
        available = sellable_size(lots, session)
        values.append(
            SymbolValuation(
                symbol=symbol,
                total_size=total,
                available_size=available,
                locked_size=total - available,
                mark_price=marks[symbol],
            )
        )
    return tuple(values)


def _verify_preflight(
    *,
    config: PortfolioConfig,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
) -> None:
    if type(config) is not PortfolioConfig:
        raise TypeError("config must be an exact PortfolioConfig")
    try:
        verify_trading_calendar(calendar)
    except (AttributeError, CalendarError, TypeError, ValueError) as exc:
        raise PortfolioError(
            "unverified_calendar",
            "portfolio requires an exact verified calendar",
        ) from exc
    try:
        verify_fee_policy(fee_policy)
    except (AttributeError, FeePolicyError, TypeError, ValueError) as exc:
        raise PortfolioError(
            "invalid_fee_policy",
            "portfolio requires an exact verified fee policy",
        ) from exc
    if (
        not calendar.contains(config.signal_date)
        or not calendar.contains(config.end_date)
        or calendar.next_session(config.end_date) is None
    ):
        raise PortfolioError(
            "missing_calendar_coverage",
            "calendar must cover signal, end, and one post-end session",
        )


def _verify_supported_actions(
    ordered_inputs: tuple[PortfolioInstrumentInput, ...],
    *,
    config: PortfolioConfig,
    calendar: VerifiedTradingCalendar,
) -> None:
    for item in ordered_inputs:
        provenance = item.corporate_actions.provenance
        if (
            provenance is None
            or provenance.coverage_start > config.signal_date
            or provenance.coverage_end < config.end_date
        ):
            raise PortfolioError(
                "corporate_action_coverage_gap",
                "corporate actions must cover the complete portfolio run",
            )
        for event in item.corporate_actions.events:
            if event.record_date >= event.ex_date:
                raise PortfolioError(
                    "invalid_corporate_action_dates",
                    "corporate-action record date must precede its ex-date",
                )
            if (
                event.stock_dividend_ratio != 0
                or event.capitalization_ratio != 0
                or event.rights_ratio != 0
                or event.rights_price is not None
            ):
                raise PortfolioError(
                    "unsupported_corporate_action",
                    "portfolio v0.2 supports verified cash dividends only",
                )
            if (
                config.signal_date < event.ex_date <= config.end_date
                and not calendar.contains(event.ex_date)
            ):
                raise PortfolioError(
                    "invalid_corporate_action_session",
                    "in-range ex-date must be an official session",
                )


def _entitled_size(
    ledger: PortfolioLedger,
    *,
    symbol: str,
    record_date: date,
) -> int:
    return sum(
        item.remaining_size
        for item in ledger.lots
        if item.symbol == symbol
        and item.acquired_date <= record_date
        and item.remaining_size > 0
    )


def _register_session_dividends(
    ledger: PortfolioLedger,
    *,
    session: date,
    ordered_inputs: tuple[PortfolioInstrumentInput, ...],
    calendar: VerifiedTradingCalendar,
    marks: dict[str, Decimal],
) -> tuple[PortfolioLedger, tuple[DividendAudit, ...], frozenset[str]]:
    audits: list[DividendAudit] = []
    adjusted_symbols: set[str] = set()
    for item in ordered_inputs:
        events = tuple(
            sorted(
                (
                    event
                    for event in item.corporate_actions.events
                    if event.ex_date == session
                    and event.cash_dividend_per_unit > 0
                ),
                key=lambda event: event.event_id,
            )
        )
        if not events:
            continue
        total_cash_per_unit = sum(
            (
                event.cash_dividend_per_unit
                for event in events
            ),
            start=Decimal("0"),
        )
        adjusted_mark = marks[item.symbol] - total_cash_per_unit
        if adjusted_mark <= 0:
            raise PortfolioError(
                "nonpositive_reference_price",
                "cash dividend produced a non-positive trusted mark",
            )
        marks[item.symbol] = adjusted_mark
        adjusted_symbols.add(item.symbol)
        for event in events:
            entitled = _entitled_size(
                ledger,
                symbol=item.symbol,
                record_date=event.record_date,
            )
            cash_session = actual_cash_date(calendar, event.payable_date)
            amount_fen = decimal_yuan_to_fen(
                event.cash_dividend_per_unit * entitled
            )
            audit = DividendAudit(
                event_id=event.event_id,
                symbol=item.symbol,
                ex_date=session,
                source_payable_date=event.payable_date,
                actual_cash_date=cash_session,
                entitled_size=entitled,
                cash_dividend_per_unit=event.cash_dividend_per_unit,
                amount_fen=amount_fen,
            )
            audits.append(audit)
            if amount_fen == 0:
                continue
            ledger = register_receivable(
                ledger,
                CashReceivable(
                    event_id=event.event_id,
                    symbol=item.symbol,
                    registered_date=session,
                    source_payable_date=event.payable_date,
                    actual_cash_date=cash_session,
                    amount_fen=amount_fen,
                ),
            )
    return ledger, tuple(audits), frozenset(adjusted_symbols)


def run_portfolio_backtest(
    *,
    config: PortfolioConfig,
    inputs: tuple[PortfolioInstrumentInput, ...],
    universe: VerifiedUniverse,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
) -> PortfolioBacktestResult:
    """Run deterministic buy-and-hold entries over official sessions."""
    _verify_preflight(
        config=config,
        calendar=calendar,
        fee_policy=fee_policy,
    )
    ordered_inputs = validate_portfolio_inputs(inputs, universe=universe)
    _verify_supported_actions(
        ordered_inputs,
        config=config,
        calendar=calendar,
    )
    allocation = allocate_equal_targets(config, len(ordered_inputs))
    bars = {item.symbol: _bar_map(item) for item in ordered_inputs}
    available_dates = {
        symbol: frozenset(values)
        for symbol, values in bars.items()
    }
    if any(config.signal_date not in bars[item.symbol] for item in ordered_inputs):
        raise PortfolioError(
            "missing_signal_bar",
            "every portfolio member requires a signal-date bar",
        )
    marks = {
        item.symbol: bars[item.symbol][config.signal_date]["close"]
        for item in ordered_inputs
    }
    targets = [
        _initial_target(item.symbol, config, allocation)
        for item in ordered_inputs
    ]
    attempts: list[EntryAttempt] = []
    dividend_audits: list[DividendAudit] = []
    availability_audits: list[AvailabilityAudit] = []
    carried_sessions = {item.symbol: 0 for item in ordered_inputs}
    ledger = create_portfolio_ledger(config.initial_cash_fen)
    official_sessions = tuple(
        item
        for item in calendar.dates
        if config.signal_date < item <= config.end_date
    )
    previous_session = config.signal_date
    for session in official_sessions:
        ledger, session_dividends, adjusted_symbols = (
            _register_session_dividends(
                ledger,
                session=session,
                ordered_inputs=ordered_inputs,
                calendar=calendar,
                marks=marks,
            )
        )
        dividend_audits.extend(session_dividends)
        ledger = pay_receivables(ledger, session)
        for position, target in enumerate(targets):
            if target.status is not TargetStatus.PENDING:
                continue
            availability = check_bar_availability(
                intent_session=previous_session,
                execution_session=session,
                calendar=calendar,
                available_bar_dates=available_dates[target.symbol],
            )
            if availability.status is AvailabilityStatus.NO_BAR_UNAVAILABLE:
                assert availability.source_rule_reason is not None
                attempt = _rejected_attempt(
                    target=target,
                    session=session,
                    intent_session=previous_session,
                    availability_status=availability.status,
                    initial_candidate_size=0,
                    requested_size=0,
                    reason=availability.source_rule_reason,
                )
                attempts.append(attempt)
                targets[position] = _record_rejection(
                    target,
                    attempt,
                    max_attempts=config.max_entry_attempts,
                )
                continue
            current_bar = bars[target.symbol][session]
            execution_open = current_bar["open"]
            target_yuan = (
                Decimal(target.target_notional_fen) / Decimal(100)
            )
            units = int(
                (target_yuan / execution_open).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            initial_candidate = units // 100 * 100
            if initial_candidate == 0:
                attempt = _rejected_attempt(
                    target=target,
                    session=session,
                    intent_session=previous_session,
                    availability_status=availability.status,
                    initial_candidate_size=0,
                    requested_size=0,
                    reason=RejectionReason.INVALID_LOT_SIZE,
                )
                attempts.append(attempt)
                targets[position] = _record_rejection(
                    target,
                    attempt,
                    max_attempts=config.max_entry_attempts,
                )
                continue
            cash_available_before_fen = ledger.cash_fen
            instrument_kind = next(
                item.instrument_kind
                for item in ordered_inputs
                if item.symbol == target.symbol
            )
            candidate = initial_candidate
            decision = None
            while candidate > 0:
                decision = evaluate_order(
                    intent=OrderIntent(
                        order_id=(
                            f"attempt:{target.symbol}:"
                            f"{target.signal_date.isoformat()}:"
                            f"{target.attempts_used + 1}"
                        ),
                        symbol=target.symbol,
                        signal_date=previous_session,
                        side=OrderSide.BUY,
                        requested_size=candidate,
                    ),
                    instrument=InstrumentRule(
                        target.symbol,
                        instrument_kind,
                    ),
                    calendar=calendar,
                    available_bar_dates=available_dates[target.symbol],
                    previous_close=marks[target.symbol],
                    execution_open=execution_open,
                    cash_fen=ledger.cash_fen,
                    lots=ledger.lots,
                    fee_policy=fee_policy,
                )
                if (
                    decision.allowed
                    or decision.reason is not RejectionReason.INSUFFICIENT_CASH
                ):
                    break
                candidate -= 100
            assert decision is not None
            if not decision.allowed:
                if decision.reason in {
                    RejectionReason.MISSING_CALENDAR_COVERAGE,
                    RejectionReason.NO_NEXT_SESSION_IN_RANGE,
                    RejectionReason.MISSING_FEE_SCHEDULE,
                    RejectionReason.INVALID_FEE_CONFIGURATION,
                }:
                    raise PortfolioError(
                        "rule_contract_failure",
                        "portfolio rule inputs are incomplete or invalid",
                    )
                assert decision.reason is not None
                attempt = _rejected_attempt(
                    target=target,
                    session=session,
                    intent_session=previous_session,
                    availability_status=availability.status,
                    initial_candidate_size=initial_candidate,
                    requested_size=max(candidate, 0),
                    reason=decision.reason,
                )
                attempts.append(attempt)
                targets[position] = _record_rejection(
                    target,
                    attempt,
                    max_attempts=config.max_entry_attempts,
                )
                continue
            assert decision.fees is not None
            try:
                initial_candidate_fees = calculate_fees(
                    fee_policy,
                    instrument_kind=instrument_kind,
                    side=OrderSide.BUY,
                    execution_date=session,
                    notional=execution_open * initial_candidate,
                )
            except FeePolicyError as exc:
                raise PortfolioError(
                    "rule_contract_failure",
                    "portfolio rule inputs are incomplete or invalid",
                ) from exc
            initial_candidate_cash_required_fen = (
                notional_fen(execution_open, initial_candidate)
                + initial_candidate_fees.total_fees_fen
            )
            requested_cash_required_fen = (
                notional_fen(execution_open, candidate)
                + decision.fees.total_fees_fen
            )
            number = target.attempts_used + 1
            fill_event_id = (
                f"fill:{target.symbol}:{target.signal_date.isoformat()}:{number}"
            )
            try:
                lot = create_buy_lot(
                    lot_id=(
                        f"lot:{target.symbol}:"
                        f"{target.signal_date.isoformat()}:{number}"
                    ),
                    symbol=target.symbol,
                    acquired_date=session,
                    size=candidate,
                    unit_cost=execution_open,
                    calendar=calendar,
                )
            except RuleInputError as exc:
                raise PortfolioError(
                    "rule_contract_failure",
                    "authorized buy could not create a T+1 lot",
                ) from exc
            ledger = post_buy(
                ledger,
                BuyPosting(
                    event_id=fill_event_id,
                    execution_date=session,
                    lot=lot,
                    fees=decision.fees,
                ),
            )
            attempt = EntryAttempt(
                attempt_id=(
                    f"attempt:{target.symbol}:"
                    f"{target.signal_date.isoformat()}:{number}"
                ),
                target_id=target.target_id,
                symbol=target.symbol,
                original_signal_date=target.signal_date,
                intent_session=previous_session,
                execution_session=session,
                attempt_number=number,
                initial_candidate_size=initial_candidate,
                requested_size=candidate,
                availability_status=availability.status,
                status=AttemptStatus.FILLED,
                rejection_reason=None,
                fees=decision.fees,
                fill_event_id=fill_event_id,
                cash_available_before_fen=cash_available_before_fen,
                initial_candidate_cash_required_fen=(
                    initial_candidate_cash_required_fen
                ),
                requested_cash_required_fen=requested_cash_required_fen,
                quantity_adjustment_reason=(
                    "insufficient_cash_including_fees"
                    if initial_candidate != candidate
                    else None
                ),
            )
            attempts.append(attempt)
            targets[position] = replace(
                target,
                attempts_used=number,
                status=TargetStatus.FILLED,
                fill_event_id=fill_event_id,
            )
        for item in ordered_inputs:
            if session in bars[item.symbol]:
                marks[item.symbol] = bars[item.symbol][session]["close"]
                carried_sessions[item.symbol] = 0
                availability_audits.append(
                    AvailabilityAudit(
                        session=session,
                        symbol=item.symbol,
                        status=AvailabilityStatus.AVAILABLE,
                        mark_price=marks[item.symbol],
                        carried_sessions=0,
                        adjustment_reason="bar_close",
                    )
                )
            else:
                carried_sessions[item.symbol] += 1
                availability_audits.append(
                    AvailabilityAudit(
                        session=session,
                        symbol=item.symbol,
                        status=AvailabilityStatus.NO_BAR_UNAVAILABLE,
                        mark_price=marks[item.symbol],
                        carried_sessions=carried_sessions[item.symbol],
                        adjustment_reason=(
                            "cash_dividend"
                            if item.symbol in adjusted_symbols
                            else "no_bar_carry"
                        ),
                    )
                )
        ledger = close_session(
            ledger,
            session,
            _position_valuations(
                ledger,
                session=session,
                marks=marks,
            ),
        )
        previous_session = session
    return PortfolioBacktestResult(
        config=config,
        allocation=allocation,
        targets=tuple(targets),
        attempts=tuple(attempts),
        dividends=tuple(dividend_audits),
        availability=tuple(availability_audits),
        ledger=ledger,
    )
