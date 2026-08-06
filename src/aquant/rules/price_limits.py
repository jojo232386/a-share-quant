"""Tick-aware 10 percent price limits for supported v0.1 instrument classes."""

from decimal import ROUND_HALF_UP, Decimal

from aquant.rules.models import InstrumentKind


def price_limits(
    previous_close: Decimal, instrument_kind: InstrumentKind
) -> tuple[Decimal, Decimal]:
    if (
        type(previous_close) is not Decimal
        or not previous_close.is_finite()
        or previous_close <= 0
        or type(instrument_kind) is not InstrumentKind
    ):
        raise ValueError("price-limit input is invalid")
    tick = (
        Decimal("0.01")
        if instrument_kind is InstrumentKind.MAIN_BOARD_STOCK
        else Decimal("0.001")
    )
    lower = (previous_close * Decimal("0.90")).quantize(
        tick, rounding=ROUND_HALF_UP
    )
    upper = (previous_close * Decimal("1.10")).quantize(
        tick, rounding=ROUND_HALF_UP
    )
    return lower, upper
