from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from aquant.data.calendar_snapshot import (
    CalendarSnapshotStore,
    load_verified_calendar,
)
from aquant.rules import (
    CommissionAssumption,
    FeePolicyError,
    InstrumentKind,
    InstrumentRule,
    OrderIntent,
    OrderSide,
    PositionLot,
    RejectionReason,
    VerifiedFeePolicy,
    calculate_fees,
    consume_fifo,
    create_buy_lot,
    default_fee_policy,
    evaluate_order,
    make_fee_policy,
    price_limits,
    sellable_size,
    validate_sell_size,
)


def test_stock_fee_schedule_switches_on_effective_dates():
    policy = default_fee_policy()
    before = calculate_fees(
        policy,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        side=OrderSide.SELL,
        execution_date=date(2023, 8, 25),
        notional=Decimal("10000.00"),
    )
    after = calculate_fees(
        policy,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        side=OrderSide.SELL,
        execution_date=date(2023, 8, 28),
        notional=Decimal("10000.00"),
    )

    assert before.commission_fen == 500
    assert before.stamp_duty_fen == 1000
    assert before.transfer_fee_fen == 10
    assert after.stamp_duty_fen == 500
    assert after.transfer_fee_fen == 10


def test_stock_transfer_fee_is_charged_both_directions_after_2018():
    policy = default_fee_policy()

    buy = calculate_fees(
        policy,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        side=OrderSide.BUY,
        execution_date=date(2018, 1, 2),
        notional=Decimal("10000.00"),
    )
    sell = calculate_fees(
        policy,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        side=OrderSide.SELL,
        execution_date=date(2018, 1, 2),
        notional=Decimal("10000.00"),
    )

    assert buy.transfer_fee_fen == sell.transfer_fee_fen == 20


def test_etf_has_commission_but_no_stamp_or_transfer_fee():
    fees = calculate_fees(
        default_fee_policy(),
        instrument_kind=InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF,
        side=OrderSide.SELL,
        execution_date=date(2026, 7, 22),
        notional=Decimal("10000.00"),
    )

    assert fees.commission_fen == 500
    assert fees.stamp_duty_fen == 0
    assert fees.transfer_fee_fen == 0
    assert fees.total_fees_fen == 500


def test_each_fee_component_uses_decimal_half_up_to_one_fen():
    policy = make_fee_policy(
        stock_commission=CommissionAssumption(
            rate=Decimal("0.0000015"),
            minimum_yuan=Decimal("0.00"),
        ),
        etf_commission=CommissionAssumption(
            rate=Decimal("0.0000015"),
            minimum_yuan=Decimal("0.00"),
        ),
    )

    fees = calculate_fees(
        policy,
        instrument_kind=InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF,
        side=OrderSide.BUY,
        execution_date=date(2026, 7, 22),
        notional=Decimal("10000.00"),
    )

    assert fees.commission_fen == 2


@pytest.mark.parametrize(
    "schedule",
    [
        ((date(2023, 8, 28), 0.0005),),
        (
            (date(2023, 8, 28), Decimal("0.0005")),
            (date(2008, 9, 19), Decimal("0.001")),
        ),
        (
            (date(2023, 8, 28), Decimal("0.0005")),
            (date(2023, 8, 28), Decimal("0.0004")),
        ),
    ],
)
def test_fee_policy_rejects_non_decimal_unsorted_or_duplicate_schedule(schedule):
    with pytest.raises(FeePolicyError):
        make_fee_policy(stamp_duty_schedule=schedule)


def test_fee_policy_rejects_dates_before_first_effective_rate():
    with pytest.raises(FeePolicyError, match="missing fee schedule"):
        calculate_fees(
            default_fee_policy(),
            instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
            side=OrderSide.SELL,
            execution_date=date(2008, 9, 18),
            notional=Decimal("10000.00"),
        )


def test_verified_fee_policy_cannot_be_constructed_by_callers():
    with pytest.raises(TypeError):
        VerifiedFeePolicy(
            CommissionAssumption(Decimal("0.1"), Decimal("1.00")),
            CommissionAssumption(Decimal("0.1"), Decimal("1.00")),
            (),
            (),
            "a" * 64,
        )


def _calendar(tmp_path, *values: str):
    record = CalendarSnapshotStore(tmp_path).write(
        tuple(date.fromisoformat(value) for value in values),
        source_provider="synthetic",
        source_function="pytest_fixture",
        source_version="1",
        fetched_at_utc=datetime(2026, 7, 16, tzinfo=UTC),
    )
    return load_verified_calendar(tmp_path, record)


def test_t_plus_one_uses_calendar_and_does_not_wait_for_symbol_bar(tmp_path):
    calendar = _calendar(tmp_path, "2026-07-13", "2026-07-14", "2026-07-15")
    lot = create_buy_lot(
        lot_id="lot-0001",
        symbol="600519",
        acquired_date=date(2026, 7, 13),
        size=100,
        unit_cost=Decimal("10.00"),
        calendar=calendar,
    )

    assert lot.available_date == date(2026, 7, 14)
    assert sellable_size((lot,), date(2026, 7, 13)) == 0
    assert sellable_size((lot,), date(2026, 7, 14)) == 100


@pytest.mark.parametrize(
    ("position", "requested", "expected"),
    [(250, 100, True), (250, 150, False), (250, 250, True), (50, 50, True)],
)
def test_sell_lot_policy_allows_only_board_lots_or_full_liquidation(
    position, requested, expected
):
    assert validate_sell_size(position, requested) is expected


def test_fifo_consumption_preserves_locked_lots():
    lots = (
        PositionLot(
            "lot-0001",
            "600519",
            date(2026, 7, 13),
            date(2026, 7, 14),
            100,
            100,
            Decimal("10.00"),
        ),
        PositionLot(
            "lot-0002",
            "600519",
            date(2026, 7, 14),
            date(2026, 7, 15),
            100,
            100,
            Decimal("11.00"),
        ),
    )

    updated = consume_fifo(
        lots,
        execution_date=date(2026, 7, 14),
        requested_size=100,
    )

    assert [lot.remaining_size for lot in updated] == [0, 100]


def test_stock_and_etf_use_different_ticks_for_limit_rounding():
    assert price_limits(Decimal("10.01"), InstrumentKind.MAIN_BOARD_STOCK) == (
        Decimal("9.01"),
        Decimal("11.01"),
    )
    assert price_limits(
        Decimal("3.333"),
        InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF,
    ) == (Decimal("3.000"), Decimal("3.666"))


def test_order_for_missing_target_bar_is_rejected_without_carrying_forward(tmp_path):
    calendar = _calendar(tmp_path, "2026-07-13", "2026-07-14", "2026-07-15")
    decision = evaluate_order(
        intent=OrderIntent(
            "order-0001",
            "600519",
            date(2026, 7, 13),
            OrderSide.BUY,
            100,
        ),
        instrument=InstrumentRule("600519", InstrumentKind.MAIN_BOARD_STOCK),
        calendar=calendar,
        available_bar_dates=frozenset({date(2026, 7, 13), date(2026, 7, 15)}),
        previous_close=Decimal("10.00"),
        execution_open=Decimal("10.20"),
        cash_fen=1_000_000,
        lots=(),
        fee_policy=default_fee_policy(),
    )

    assert decision.allowed is False
    assert decision.reason is RejectionReason.SUSPENDED_NO_BAR
    assert decision.target_execution_date == date(2026, 7, 14)


def test_buy_at_upper_limit_and_same_day_sale_are_rejected(tmp_path):
    calendar = _calendar(tmp_path, "2026-07-13", "2026-07-14", "2026-07-15")
    common = {
        "instrument": InstrumentRule("600519", InstrumentKind.MAIN_BOARD_STOCK),
        "calendar": calendar,
        "available_bar_dates": frozenset({date(2026, 7, 13), date(2026, 7, 14)}),
        "previous_close": Decimal("10.00"),
        "cash_fen": 1_000_000,
        "fee_policy": default_fee_policy(),
    }
    limit_buy = evaluate_order(
        intent=OrderIntent(
            "order-0001", "600519", date(2026, 7, 13), OrderSide.BUY, 100
        ),
        execution_open=Decimal("11.00"),
        lots=(),
        **common,
    )
    locked_lot = PositionLot(
        "lot-0001",
        "600519",
        date(2026, 7, 14),
        date(2026, 7, 15),
        100,
        100,
        Decimal("10.00"),
    )
    same_day_sell = evaluate_order(
        intent=OrderIntent(
            "order-0002", "600519", date(2026, 7, 13), OrderSide.SELL, 100
        ),
        execution_open=Decimal("10.00"),
        lots=(locked_lot,),
        **common,
    )

    assert limit_buy.reason is RejectionReason.PRICE_LIMIT_OPEN
    assert same_day_sell.reason is RejectionReason.INSUFFICIENT_SELLABLE_POSITION
