"""T+1 position lots and conservative board-lot validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from aquant.data.calendar_snapshot import VerifiedTradingCalendar
from aquant.rules.models import PositionLot


class RuleInputError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def create_buy_lot(
    *,
    lot_id: str,
    symbol: str,
    acquired_date: date,
    size: int,
    unit_cost: Decimal,
    calendar: VerifiedTradingCalendar,
) -> PositionLot:
    if type(calendar) is not VerifiedTradingCalendar:
        raise RuleInputError("missing_calendar_coverage", "verified calendar is required")
    available = calendar.next_session(acquired_date)
    if available is None:
        raise RuleInputError("no_next_session_in_range", "calendar has no next session")
    if (
        type(size) is not int
        or isinstance(size, bool)
        or size <= 0
        or size % 100
        or type(unit_cost) is not Decimal
        or not unit_cost.is_finite()
        or unit_cost <= 0
    ):
        raise RuleInputError("invalid_lot_size", "buy lot input is invalid")
    return PositionLot(
        lot_id,
        symbol,
        acquired_date,
        available,
        size,
        size,
        unit_cost,
    )


def sellable_size(lots: tuple[PositionLot, ...], execution_date: date) -> int:
    return sum(
        lot.remaining_size
        for lot in lots
        if lot.available_date <= execution_date
    )


def validate_sell_size(position: int, requested: int) -> bool:
    return (
        type(position) is int
        and type(requested) is int
        and not isinstance(position, bool)
        and not isinstance(requested, bool)
        and position > 0
        and 0 < requested <= position
        and (requested % 100 == 0 or requested == position)
    )


def consume_fifo(
    lots: tuple[PositionLot, ...],
    *,
    execution_date: date,
    requested_size: int,
) -> tuple[PositionLot, ...]:
    if sellable_size(lots, execution_date) < requested_size:
        raise RuleInputError(
            "insufficient_sellable_position", "sellable position is insufficient"
        )
    remaining = requested_size
    updated: list[PositionLot] = []
    for lot in lots:
        take = (
            min(lot.remaining_size, remaining)
            if lot.available_date <= execution_date
            else 0
        )
        updated.append(replace(lot, remaining_size=lot.remaining_size - take))
        remaining -= take
    return tuple(updated)
