"""Exact immutable contracts for the supported A-share execution rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class InstrumentKind(StrEnum):
    MAIN_BOARD_STOCK = "main_board_stock"
    DOMESTIC_EQUITY_BROAD_BASED_ETF = "domestic_equity_broad_based_etf"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class RejectionReason(StrEnum):
    UNSUPPORTED_INSTRUMENT = "unsupported_instrument"
    MISSING_CALENDAR_COVERAGE = "missing_calendar_coverage"
    NO_NEXT_SESSION_IN_RANGE = "no_next_session_in_range"
    SUSPENDED_NO_BAR = "suspended_no_bar"
    MISSING_PREVIOUS_CLOSE = "missing_previous_close"
    PRICE_LIMIT_OPEN = "price_limit_open"
    INVALID_LOT_SIZE = "invalid_lot_size"
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_SELLABLE_POSITION = "insufficient_sellable_position"
    MISSING_FEE_SCHEDULE = "missing_fee_schedule"
    INVALID_FEE_CONFIGURATION = "invalid_fee_configuration"


@dataclass(frozen=True)
class InstrumentRule:
    symbol: str
    kind: InstrumentKind


@dataclass(frozen=True)
class OrderIntent:
    order_id: str
    symbol: str
    signal_date: date
    side: OrderSide
    requested_size: int


@dataclass(frozen=True)
class CommissionAssumption:
    rate: Decimal
    minimum_yuan: Decimal

    def __post_init__(self) -> None:
        if (
            type(self.rate) is not Decimal
            or type(self.minimum_yuan) is not Decimal
            or not self.rate.is_finite()
            or not self.minimum_yuan.is_finite()
            or self.rate < 0
            or self.minimum_yuan < 0
        ):
            raise ValueError("commission assumption is invalid")


@dataclass(frozen=True)
class FeeRateTouch:
    fee_name: str
    effective_date: date | None
    rate: Decimal
    minimum_yuan: Decimal | None


@dataclass(frozen=True)
class FeeBreakdown:
    commission_fen: int
    stamp_duty_fen: int
    transfer_fee_fen: int
    touched_rates: tuple[FeeRateTouch, ...]

    @property
    def total_fees_fen(self) -> int:
        return self.commission_fen + self.stamp_duty_fen + self.transfer_fee_fen


@dataclass(frozen=True)
class PositionLot:
    lot_id: str
    symbol: str
    acquired_date: date
    available_date: date
    original_size: int
    remaining_size: int
    unit_cost: Decimal


@dataclass(frozen=True)
class RuleDecision:
    allowed: bool
    reason: RejectionReason | None
    target_execution_date: date | None
    fees: FeeBreakdown | None
