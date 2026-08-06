"""Deterministic composition of the supported pre-trade rules."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from aquant.data.calendar_snapshot import VerifiedTradingCalendar
from aquant.rules.fees import FeePolicyError, VerifiedFeePolicy, calculate_fees
from aquant.rules.lots import sellable_size, validate_sell_size
from aquant.rules.models import (
    InstrumentRule,
    OrderIntent,
    OrderSide,
    PositionLot,
    RejectionReason,
    RuleDecision,
)
from aquant.rules.price_limits import price_limits
from aquant.universe import is_supported_instrument_identity


def _rejected(
    reason: RejectionReason, target_execution_date: date | None
) -> RuleDecision:
    return RuleDecision(False, reason, target_execution_date, None)


def evaluate_order(
    *,
    intent: OrderIntent,
    instrument: InstrumentRule,
    calendar: VerifiedTradingCalendar,
    available_bar_dates: frozenset[date],
    previous_close: Decimal,
    execution_open: Decimal,
    cash_fen: int,
    lots: tuple[PositionLot, ...],
    fee_policy: VerifiedFeePolicy,
) -> RuleDecision:
    if (
        type(intent) is not OrderIntent
        or type(instrument) is not InstrumentRule
        or not is_supported_instrument_identity(
            instrument.symbol,
            instrument.kind.value,
        )
        or intent.symbol != instrument.symbol
    ):
        return _rejected(RejectionReason.UNSUPPORTED_INSTRUMENT, None)
    if type(calendar) is not VerifiedTradingCalendar or not calendar.contains(
        intent.signal_date
    ):
        return _rejected(RejectionReason.MISSING_CALENDAR_COVERAGE, None)
    target = calendar.next_session(intent.signal_date)
    if target is None:
        return _rejected(RejectionReason.NO_NEXT_SESSION_IN_RANGE, None)
    if target not in available_bar_dates:
        return _rejected(RejectionReason.SUSPENDED_NO_BAR, target)
    if (
        intent.side is OrderSide.BUY
        and calendar.next_session(target) is None
    ):
        return _rejected(RejectionReason.NO_NEXT_SESSION_IN_RANGE, target)
    if (
        type(previous_close) is not Decimal
        or type(execution_open) is not Decimal
        or not previous_close.is_finite()
        or not execution_open.is_finite()
        or previous_close <= 0
        or execution_open <= 0
    ):
        return _rejected(RejectionReason.MISSING_PREVIOUS_CLOSE, target)
    if (
        type(intent.requested_size) is not int
        or isinstance(intent.requested_size, bool)
        or intent.requested_size <= 0
    ):
        return _rejected(RejectionReason.INVALID_LOT_SIZE, target)
    available = sellable_size(lots, target)
    if intent.side is OrderSide.BUY:
        if intent.requested_size % 100:
            return _rejected(RejectionReason.INVALID_LOT_SIZE, target)
    elif not validate_sell_size(available, intent.requested_size):
        reason = (
            RejectionReason.INSUFFICIENT_SELLABLE_POSITION
            if available < intent.requested_size
            else RejectionReason.INVALID_LOT_SIZE
        )
        return _rejected(reason, target)
    lower, upper = price_limits(previous_close, instrument.kind)
    if (
        intent.side is OrderSide.BUY
        and execution_open >= upper
        or intent.side is OrderSide.SELL
        and execution_open <= lower
    ):
        return _rejected(RejectionReason.PRICE_LIMIT_OPEN, target)
    notional = execution_open * intent.requested_size
    try:
        fees = calculate_fees(
            fee_policy,
            instrument_kind=instrument.kind,
            side=intent.side,
            execution_date=target,
            notional=notional,
        )
    except FeePolicyError as exc:
        reason = (
            RejectionReason.MISSING_FEE_SCHEDULE
            if exc.code == "missing_fee_schedule"
            else RejectionReason.INVALID_FEE_CONFIGURATION
        )
        return _rejected(reason, target)
    if intent.side is OrderSide.BUY:
        notional_fen = int(
            notional.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100
        )
        if cash_fen < notional_fen + fees.total_fees_fen:
            return _rejected(RejectionReason.INSUFFICIENT_CASH, target)
    return RuleDecision(True, None, target, fees)
