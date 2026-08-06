from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from aquant.portfolio import (
    BuyPosting,
    CashReceivable,
    DailyAccountSnapshot,
    PortfolioError,
    SymbolValuation,
    cash_after_fill,
    close_session,
    create_portfolio_ledger,
    decimal_yuan_to_fen,
    notional_fen,
    pay_receivables,
    post_buy,
    register_receivable,
    verify_portfolio_ledger,
)
from aquant.rules import (
    FeeBreakdown,
    FeeRateTouch,
    OrderSide,
    PositionLot,
)


def _fees(
    *,
    commission_fen: int = 500,
    stamp_duty_fen: int = 0,
    transfer_fee_fen: int = 0,
) -> FeeBreakdown:
    return FeeBreakdown(
        commission_fen=commission_fen,
        stamp_duty_fen=stamp_duty_fen,
        transfer_fee_fen=transfer_fee_fen,
        touched_rates=(),
    )


def _buy_posting(
    *,
    event_id: str = "fill-0001",
    lot_id: str = "lot-0001",
    symbol: str = "510300",
    execution_date: date = date(2026, 7, 14),
    available_date: date = date(2026, 7, 15),
    size: int = 100,
    unit_cost: Decimal = Decimal("95.000"),
    fees: FeeBreakdown | None = None,
) -> BuyPosting:
    return BuyPosting(
        event_id=event_id,
        execution_date=execution_date,
        lot=PositionLot(
            lot_id=lot_id,
            symbol=symbol,
            acquired_date=execution_date,
            available_date=available_date,
            original_size=size,
            remaining_size=size,
            unit_cost=unit_cost,
        ),
        fees=_fees() if fees is None else fees,
    )


def test_decimal_amounts_use_half_up_fen_without_float():
    assert decimal_yuan_to_fen(Decimal("1.004")) == 100
    assert decimal_yuan_to_fen(Decimal("1.005")) == 101
    assert notional_fen(Decimal("95.001"), 100) == 950_010


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (1.005, "invalid_decimal_amount"),
        (Decimal("NaN"), "invalid_decimal_amount"),
        (Decimal("-0.01"), "invalid_decimal_amount"),
        (Decimal("1E+1000"), "invalid_decimal_amount"),
    ],
)
def test_decimal_amount_rejects_inexact_or_invalid_values(value, code):
    with pytest.raises(PortfolioError) as captured:
        decimal_yuan_to_fen(value)

    assert captured.value.code == code


@pytest.mark.parametrize("size", [True, 0, -100, 1.0])
def test_notional_rejects_invalid_sizes(size):
    with pytest.raises(PortfolioError) as captured:
        notional_fen(Decimal("10.00"), size)

    assert captured.value.code == "invalid_position_size"


def test_cash_after_fill_uses_direction_and_all_fee_components():
    fees = _fees(commission_fen=500, stamp_duty_fen=300, transfer_fee_fen=10)

    assert (
        cash_after_fill(
            cash_before_fen=2_000_000,
            side=OrderSide.BUY,
            notional_amount_fen=1_000_000,
            fees=fees,
        )
        == 999_190
    )
    assert (
        cash_after_fill(
            cash_before_fen=2_000_000,
            side=OrderSide.SELL,
            notional_amount_fen=1_000_000,
            fees=fees,
        )
        == 2_999_190
    )


@pytest.mark.parametrize(
    ("cash", "side", "notional", "fees", "code"),
    [
        (True, OrderSide.BUY, 100, _fees(), "invalid_cash"),
        (-1, OrderSide.BUY, 100, _fees(), "invalid_cash"),
        (1000, "buy", 100, _fees(), "invalid_order_side"),
        (1000, OrderSide.BUY, True, _fees(), "invalid_notional"),
        (1000, OrderSide.BUY, 0, _fees(), "invalid_notional"),
        (1000, OrderSide.BUY, 100, object(), "invalid_fees"),
    ],
)
def test_cash_after_fill_rejects_implicit_or_invalid_inputs(
    cash,
    side,
    notional,
    fees,
    code,
):
    with pytest.raises(PortfolioError) as captured:
        cash_after_fill(
            cash_before_fen=cash,
            side=side,
            notional_amount_fen=notional,
            fees=fees,
        )

    assert captured.value.code == code


def test_cash_after_buy_fails_closed_before_becoming_negative():
    with pytest.raises(PortfolioError) as captured:
        cash_after_fill(
            cash_before_fen=100,
            side=OrderSide.BUY,
            notional_amount_fen=100,
            fees=_fees(commission_fen=1),
        )

    assert captured.value.code == "insufficient_cash"


def test_buy_posting_uses_shared_cash_and_preserves_original_ledger():
    original = create_portfolio_ledger(1_000_000)
    posting = _buy_posting()

    updated = post_buy(original, posting)

    assert original.cash_fen == 1_000_000
    assert original.lots == ()
    assert updated.cash_fen == 49_500
    assert updated.lots == (posting.lot,)
    assert len(updated.cash_events) == 1
    assert updated.cash_events[0].cash_before_fen == 1_000_000
    assert updated.cash_events[0].cash_after_fen == 49_500
    verify_portfolio_ledger(updated)


def test_target_notional_does_not_treat_commission_as_part_of_weight_budget():
    ledger = create_portfolio_ledger(1_000_000)

    updated = post_buy(ledger, _buy_posting())

    assert notional_fen(updated.lots[0].unit_cost, 100) == 950_000
    assert updated.cash_events[0].commission_fen == 500
    assert updated.cash_fen == 49_500


@pytest.mark.parametrize(
    "posting",
    [
        _buy_posting(execution_date=date(2026, 7, 14)).__class__(
            event_id="fill-0001",
            execution_date=date(2026, 7, 15),
            lot=_buy_posting().lot,
            fees=_fees(),
        ),
        _buy_posting(size=0),
        _buy_posting(size=50),
        _buy_posting(available_date=date(2026, 7, 13)),
        _buy_posting(unit_cost=Decimal("0")),
    ],
)
def test_buy_posting_rejects_invalid_lot_contract(posting):
    with pytest.raises(PortfolioError):
        post_buy(create_portfolio_ledger(1_000_000), posting)


def test_buy_posting_rejects_duplicate_event_and_lot_ids():
    first = _buy_posting()
    ledger = post_buy(create_portfolio_ledger(2_000_000), first)

    with pytest.raises(PortfolioError, match="event"):
        post_buy(
            ledger,
            _buy_posting(event_id=first.event_id, lot_id="lot-0002"),
        )
    with pytest.raises(PortfolioError, match="lot"):
        post_buy(
            ledger,
            _buy_posting(event_id="fill-0002", lot_id=first.lot.lot_id),
        )


def test_failed_buy_does_not_mutate_shared_ledger():
    ledger = create_portfolio_ledger(950_000)

    with pytest.raises(PortfolioError) as captured:
        post_buy(ledger, _buy_posting())

    assert captured.value.code == "insufficient_cash"
    assert ledger.cash_fen == 950_000
    assert ledger.lots == ()
    assert ledger.cash_events == ()


def test_fee_breakdown_rejects_boolean_or_negative_components():
    for bad in (
        _fees(commission_fen=True),
        _fees(commission_fen=-1),
        _fees(stamp_duty_fen=-1),
        _fees(transfer_fee_fen=-1),
    ):
        with pytest.raises(PortfolioError) as captured:
            cash_after_fill(
                cash_before_fen=1_000_000,
                side=OrderSide.BUY,
                notional_amount_fen=100,
                fees=bad,
            )
        assert captured.value.code == "invalid_fees"


def test_fee_breakdown_requires_exact_rate_touch_records():
    damaged = FeeBreakdown(
        commission_fen=0,
        stamp_duty_fen=0,
        transfer_fee_fen=0,
        touched_rates=(FeeRateTouch("commission", None, Decimal("0"), None), object()),
    )

    with pytest.raises(PortfolioError) as captured:
        cash_after_fill(
            cash_before_fen=1_000,
            side=OrderSide.BUY,
            notional_amount_fen=100,
            fees=damaged,
        )

    assert captured.value.code == "invalid_fees"


def _receivable(
    *,
    event_id: str = "action-0001",
    symbol: str = "510300",
    registered_date: date = date(2026, 7, 14),
    source_payable_date: date = date(2026, 7, 19),
    actual_cash_date: date = date(2026, 7, 20),
    amount_fen: int = 10_000,
) -> CashReceivable:
    return CashReceivable(
        event_id=event_id,
        symbol=symbol,
        registered_date=registered_date,
        source_payable_date=source_payable_date,
        actual_cash_date=actual_cash_date,
        amount_fen=amount_fen,
        paid_date=None,
    )


def test_receivable_registration_changes_no_cash_and_preserves_dates():
    original = create_portfolio_ledger(1_000_000)
    receivable = _receivable()

    updated = register_receivable(original, receivable)

    assert original.receivables == ()
    assert updated.cash_fen == original.cash_fen
    assert updated.receivables == (receivable,)
    assert updated.receivables[0].source_payable_date == date(2026, 7, 19)
    assert updated.receivables[0].actual_cash_date == date(2026, 7, 20)
    verify_portfolio_ledger(updated)


def test_receivable_pays_once_on_exact_actual_cash_date():
    ledger = register_receivable(create_portfolio_ledger(1_000_000), _receivable())

    before_due = pay_receivables(ledger, date(2026, 7, 17))
    paid = pay_receivables(before_due, date(2026, 7, 20))
    paid_again = pay_receivables(paid, date(2026, 7, 20))

    assert before_due == ledger
    assert paid.cash_fen == 1_010_000
    assert paid.receivables[0].paid_date == date(2026, 7, 20)
    assert len(paid.cash_events) == 1
    assert paid.cash_events[0].cash_before_fen == 1_000_000
    assert paid.cash_events[0].cash_after_fen == 1_010_000
    assert paid_again == paid
    verify_portfolio_ledger(paid)


def test_multiple_receivables_pay_in_symbol_then_event_order():
    ledger = create_portfolio_ledger(1_000_000)
    ledger = register_receivable(
        ledger,
        _receivable(event_id="action-b", symbol="601318", amount_fen=20_000),
    )
    ledger = register_receivable(
        ledger,
        _receivable(event_id="action-a", symbol="600519", amount_fen=10_000),
    )

    paid = pay_receivables(ledger, date(2026, 7, 20))

    assert tuple(event.symbol for event in paid.cash_events) == ("600519", "601318")
    assert paid.cash_fen == 1_030_000


def test_receivable_rejects_duplicate_or_invalid_date_contract():
    ledger = register_receivable(create_portfolio_ledger(1_000_000), _receivable())

    with pytest.raises(PortfolioError, match="event"):
        register_receivable(ledger, _receivable())
    with pytest.raises(PortfolioError) as captured:
        register_receivable(
            create_portfolio_ledger(1_000_000),
            _receivable(
                source_payable_date=date(2026, 7, 20),
                actual_cash_date=date(2026, 7, 19),
            ),
        )
    assert captured.value.code == "invalid_receivable"


def test_close_session_uses_one_date_for_cash_mark_receivable_and_equity():
    ledger = post_buy(create_portfolio_ledger(1_000_000), _buy_posting())
    ledger = register_receivable(
        ledger,
        _receivable(
            registered_date=date(2026, 7, 14),
            source_payable_date=date(2026, 7, 20),
            actual_cash_date=date(2026, 7, 20),
        ),
    )
    valuation = SymbolValuation(
        symbol="510300",
        total_size=100,
        available_size=0,
        locked_size=100,
        mark_price=Decimal("96.000"),
    )

    updated = close_session(ledger, date(2026, 7, 14), (valuation,))

    snapshot = updated.daily_snapshots[0]
    assert type(snapshot) is DailyAccountSnapshot
    assert snapshot.session == date(2026, 7, 14)
    assert snapshot.cash_fen == 49_500
    assert snapshot.position_market_value_fen == 960_000
    assert snapshot.receivable_fen == 10_000
    assert snapshot.equity_fen == 1_019_500
    verify_portfolio_ledger(updated)


def test_close_session_tracks_t_plus_one_available_and_locked_sizes():
    ledger = post_buy(create_portfolio_ledger(1_000_000), _buy_posting())
    first = close_session(
        ledger,
        date(2026, 7, 14),
        (
            SymbolValuation(
                "510300",
                100,
                0,
                100,
                Decimal("95.000"),
            ),
        ),
    )
    second = close_session(
        first,
        date(2026, 7, 15),
        (
            SymbolValuation(
                "510300",
                100,
                100,
                0,
                Decimal("96.000"),
            ),
        ),
    )

    assert first.daily_snapshots[0].valuations[0].available_size == 0
    assert second.daily_snapshots[-1].valuations[0].available_size == 100
    verify_portfolio_ledger(second)


def test_close_session_rejects_missing_position_wrong_size_or_wrong_order():
    first_posting = _buy_posting(
        event_id="fill-1",
        lot_id="lot-1",
        symbol="600519",
        unit_cost=Decimal("10"),
    )
    second_posting = _buy_posting(
        event_id="fill-2",
        lot_id="lot-2",
        symbol="601318",
        unit_cost=Decimal("10"),
    )
    ledger = post_buy(create_portfolio_ledger(3_000_000), first_posting)
    ledger = post_buy(ledger, second_posting)
    first_value = SymbolValuation("600519", 100, 0, 100, Decimal("10"))
    second_value = SymbolValuation("601318", 100, 0, 100, Decimal("10"))

    with pytest.raises(PortfolioError, match="positions"):
        close_session(ledger, date(2026, 7, 14), (first_value,))
    with pytest.raises(PortfolioError, match="size"):
        close_session(
            ledger,
            date(2026, 7, 14),
            (
                SymbolValuation("600519", 200, 0, 200, Decimal("10")),
                second_value,
            ),
        )
    with pytest.raises(PortfolioError, match="sorted"):
        close_session(
            ledger,
            date(2026, 7, 14),
            (second_value, first_value),
        )


def test_close_session_requires_strictly_increasing_dates():
    ledger = post_buy(create_portfolio_ledger(1_000_000), _buy_posting())
    valuation = SymbolValuation("510300", 100, 0, 100, Decimal("95"))
    ledger = close_session(ledger, date(2026, 7, 14), (valuation,))

    with pytest.raises(PortfolioError) as captured:
        close_session(ledger, date(2026, 7, 14), (valuation,))

    assert captured.value.code == "invalid_snapshot_order"


@pytest.mark.parametrize(
    "valuation",
    [
        SymbolValuation("510300", 100, 90, 10, Decimal("95")),
        SymbolValuation("510300", 100, 0, 100, Decimal("95")),
    ],
)
def test_symbol_valuation_market_value_is_exact_integer_fen(valuation):
    assert valuation.market_value_fen == 950_000


def test_daily_identity_verifier_detects_independently_corrupted_equity():
    ledger = post_buy(create_portfolio_ledger(1_000_000), _buy_posting())
    ledger = close_session(
        ledger,
        date(2026, 7, 14),
        (SymbolValuation("510300", 100, 0, 100, Decimal("95")),),
    )
    object.__setattr__(
        ledger.daily_snapshots[0],
        "equity_fen",
        ledger.daily_snapshots[0].equity_fen + 1,
    )

    with pytest.raises(PortfolioError) as captured:
        verify_portfolio_ledger(ledger)

    assert captured.value.code == "daily_accounting_identity_failed"


def test_ledger_replay_binds_fill_amount_symbol_and_date_to_position_lot():
    ledger = post_buy(create_portfolio_ledger(1_000_000), _buy_posting())
    forged_event = replace(
        ledger.cash_events[0],
        notional_fen=900_000,
        cash_after_fen=99_500,
    )
    forged = replace(
        ledger,
        cash_fen=99_500,
        cash_events=(forged_event,),
    )

    with pytest.raises(PortfolioError) as captured:
        verify_portfolio_ledger(forged)

    assert captured.value.code == "lot_reconciliation_failed"


def test_ledger_replay_rejects_unsupported_sell_state_transition():
    ledger = post_buy(create_portfolio_ledger(1_000_000), _buy_posting())
    forged_event = replace(
        ledger.cash_events[0],
        side=OrderSide.SELL,
        cash_after_fen=1_949_500,
    )
    forged = replace(
        ledger,
        cash_fen=1_949_500,
        cash_events=(forged_event,),
    )

    with pytest.raises(PortfolioError) as captured:
        verify_portfolio_ledger(forged)

    assert captured.value.code == "invalid_cash_event"


def test_ledger_replay_binds_dividend_cash_to_receivable_amount_and_date():
    ledger = register_receivable(create_portfolio_ledger(1_000_000), _receivable())
    ledger = pay_receivables(ledger, date(2026, 7, 20))
    forged_event = replace(
        ledger.cash_events[0],
        session=date(2026, 7, 21),
        notional_fen=20_000,
        cash_after_fen=1_020_000,
    )
    forged = replace(
        ledger,
        cash_fen=1_020_000,
        cash_events=(forged_event,),
    )

    with pytest.raises(PortfolioError) as captured:
        verify_portfolio_ledger(forged)

    assert captured.value.code == "receivable_reconciliation_failed"


def test_overdue_receivable_cannot_be_silently_skipped_or_closed():
    ledger = register_receivable(create_portfolio_ledger(1_000_000), _receivable())

    with pytest.raises(PortfolioError) as captured:
        pay_receivables(ledger, date(2026, 7, 21))
    assert captured.value.code == "overdue_receivable"

    with pytest.raises(PortfolioError) as captured:
        close_session(ledger, date(2026, 7, 20), ())
    assert captured.value.code == "overdue_receivable"
