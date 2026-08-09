from __future__ import annotations

from dataclasses import fields, replace
from datetime import date
from decimal import Decimal

import pytest

import aquant.rolling as rolling_accounting
from aquant.portfolio import (
    BuyPosting,
    CashLedgerEvent,
    CashReceivable,
    PortfolioError,
    PortfolioLedger,
    SymbolValuation,
    create_portfolio_ledger,
    post_buy,
    register_receivable,
    verify_portfolio_ledger,
)
from aquant.rolling import (
    RollingPortfolioLedger,
    create_rolling_ledger,
    post_rolling_buy,
    promote_portfolio_ledger,
)
from aquant.rules import FeeBreakdown, OrderSide, PositionLot


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


def test_create_rolling_ledger_delegates_to_verified_pristine_legacy_ledger():
    legacy = create_portfolio_ledger(1_000_000)

    rolling = create_rolling_ledger(1_000_000)

    assert type(rolling) is RollingPortfolioLedger
    assert rolling == RollingPortfolioLedger(
        initial_cash_fen=legacy.initial_cash_fen,
        cash_fen=legacy.cash_fen,
        lots=legacy.lots,
        cash_events=legacy.cash_events,
        receivables=legacy.receivables,
        daily_snapshots=legacy.daily_snapshots,
    )
    verify_portfolio_ledger(legacy)


def test_promote_portfolio_ledger_copies_verified_state_without_aliasing_schema():
    legacy = post_buy(create_portfolio_ledger(1_000_000), _buy_posting())

    rolling = promote_portfolio_ledger(legacy)

    assert type(rolling) is RollingPortfolioLedger
    assert type(rolling) is not PortfolioLedger
    assert (
        rolling.initial_cash_fen,
        rolling.cash_fen,
        rolling.lots,
        rolling.cash_events,
        rolling.receivables,
        rolling.daily_snapshots,
    ) == (
        legacy.initial_cash_fen,
        legacy.cash_fen,
        legacy.lots,
        legacy.cash_events,
        legacy.receivables,
        legacy.daily_snapshots,
    )


def test_rolling_buy_matches_one_event_legacy_post_buy():
    posting = _buy_posting()
    legacy = post_buy(create_portfolio_ledger(1_000_000), posting)

    rolling = post_rolling_buy(create_rolling_ledger(1_000_000), posting)

    assert rolling.cash_fen == legacy.cash_fen
    assert rolling.lots == legacy.lots
    assert rolling.cash_events == legacy.cash_events
    assert rolling.cash_events[0] is legacy.cash_events[0] or (
        rolling.cash_events[0] == legacy.cash_events[0]
    )


def test_legacy_dataclass_fields_and_sell_rejection_remain_frozen():
    assert tuple(item.name for item in fields(PortfolioLedger)) == (
        "initial_cash_fen",
        "cash_fen",
        "lots",
        "cash_events",
        "receivables",
        "daily_snapshots",
    )
    assert tuple(item.name for item in fields(CashLedgerEvent)) == (
        "event_id",
        "event_kind",
        "session",
        "side",
        "symbol",
        "reference_id",
        "notional_fen",
        "commission_fen",
        "stamp_duty_fen",
        "transfer_fee_fen",
        "cash_before_fen",
        "cash_after_fen",
    )
    legacy = post_buy(create_portfolio_ledger(1_000_000), _buy_posting())
    forged_sell = replace(
        legacy.cash_events[0],
        side=OrderSide.SELL,
        cash_after_fen=1_949_500,
    )

    with pytest.raises(PortfolioError) as captured:
        verify_portfolio_ledger(
            replace(
                legacy,
                cash_fen=forged_sell.cash_after_fen,
                cash_events=(forged_sell,),
            )
        )

    assert captured.value.code == "invalid_cash_event"


def test_sell_cash_uses_notional_and_all_fee_components():
    ledger = post_rolling_buy(
        create_rolling_ledger(3_000_000),
        _buy_posting(size=200, unit_cost=Decimal("10"), fees=_fees(commission_fen=100)),
    )
    posting = rolling_accounting.SellPosting(
        event_id="sell-0001",
        execution_date=date(2026, 7, 15),
        symbol="510300",
        size=100,
        unit_price=Decimal("12"),
        fees=_fees(
            commission_fen=500,
            stamp_duty_fen=300,
            transfer_fee_fen=10,
        ),
    )

    updated = rolling_accounting.post_rolling_sell(ledger, posting)

    event = updated.cash_events[-1]
    assert type(event) is rolling_accounting.SellFillEvent
    assert event.notional_fen == 120_000
    assert event.total_fees_fen == 810
    assert event.cash_before_fen == 2_799_900
    assert event.cash_after_fen == 2_919_090
    assert updated.cash_fen == event.cash_after_fen


def test_sell_consumes_only_same_symbol_fifo_and_retains_zero_lots():
    ledger = create_rolling_ledger(10_000_000)
    ledger = post_rolling_buy(
        ledger,
        _buy_posting(
            event_id="buy-a1",
            lot_id="lot-a1",
            symbol="600519",
            execution_date=date(2026, 7, 13),
            available_date=date(2026, 7, 14),
            unit_cost=Decimal("10"),
            fees=_fees(commission_fen=0),
        ),
    )
    ledger = post_rolling_buy(
        ledger,
        _buy_posting(
            event_id="buy-b1",
            lot_id="lot-b1",
            symbol="601318",
            execution_date=date(2026, 7, 13),
            available_date=date(2026, 7, 14),
            unit_cost=Decimal("20"),
            fees=_fees(commission_fen=0),
        ),
    )
    ledger = post_rolling_buy(
        ledger,
        _buy_posting(
            event_id="buy-a2",
            lot_id="lot-a2",
            symbol="600519",
            execution_date=date(2026, 7, 14),
            available_date=date(2026, 7, 15),
            size=200,
            unit_cost=Decimal("11"),
            fees=_fees(commission_fen=0),
        ),
    )

    first = rolling_accounting.post_rolling_sell(
        ledger,
        rolling_accounting.SellPosting(
            "sell-a1",
            date(2026, 7, 15),
            "600519",
            100,
            Decimal("12"),
            _fees(commission_fen=0),
        ),
    )
    second = rolling_accounting.post_rolling_sell(
        first,
        rolling_accounting.SellPosting(
            "sell-a2",
            date(2026, 7, 15),
            "600519",
            100,
            Decimal("12"),
            _fees(commission_fen=0),
        ),
    )

    assert tuple(lot.lot_id for lot in second.lots) == (
        "lot-a1",
        "lot-b1",
        "lot-a2",
    )
    assert tuple(lot.remaining_size for lot in second.lots) == (0, 100, 100)
    assert first.cash_events[-1].consumptions == (rolling_accounting.LotConsumption("lot-a1", 100),)
    assert second.cash_events[-1].consumptions == (
        rolling_accounting.LotConsumption("lot-a2", 100),
    )


def test_sell_preserves_locked_lots_and_rejects_insufficient_sellable_size():
    ledger = create_rolling_ledger(5_000_000)
    ledger = post_rolling_buy(
        ledger,
        _buy_posting(
            event_id="buy-available",
            lot_id="lot-available",
            symbol="600519",
            execution_date=date(2026, 7, 13),
            available_date=date(2026, 7, 14),
            unit_cost=Decimal("10"),
            fees=_fees(commission_fen=0),
        ),
    )
    ledger = post_rolling_buy(
        ledger,
        _buy_posting(
            event_id="buy-locked",
            lot_id="lot-locked",
            symbol="600519",
            execution_date=date(2026, 7, 14),
            available_date=date(2026, 7, 15),
            unit_cost=Decimal("11"),
            fees=_fees(commission_fen=0),
        ),
    )
    before = ledger
    too_large = rolling_accounting.SellPosting(
        "sell-too-large",
        date(2026, 7, 14),
        "600519",
        200,
        Decimal("12"),
        _fees(commission_fen=0),
    )

    with pytest.raises(PortfolioError) as captured:
        rolling_accounting.post_rolling_sell(ledger, too_large)

    assert captured.value.code == "insufficient_sellable_size"
    assert ledger == before

    sold = rolling_accounting.post_rolling_sell(
        ledger,
        replace(too_large, event_id="sell-available", size=100),
    )
    assert tuple(lot.remaining_size for lot in sold.lots) == (0, 100)
    assert sold.lots[1] == ledger.lots[1]
    rolling_accounting.verify_rolling_ledger(sold)


def test_buy_after_sell_uses_the_updated_shared_cash():
    ledger = post_rolling_buy(
        create_rolling_ledger(5_000_000),
        _buy_posting(
            event_id="buy-first",
            lot_id="lot-first",
            symbol="600519",
            execution_date=date(2026, 7, 13),
            available_date=date(2026, 7, 14),
            size=200,
            unit_cost=Decimal("10"),
            fees=_fees(commission_fen=0),
        ),
    )
    ledger = rolling_accounting.post_rolling_sell(
        ledger,
        rolling_accounting.SellPosting(
            "sell-first",
            date(2026, 7, 14),
            "600519",
            100,
            Decimal("12"),
            _fees(commission_fen=0),
        ),
    )
    cash_after_sell = ledger.cash_fen

    updated = post_rolling_buy(
        ledger,
        _buy_posting(
            event_id="buy-second",
            lot_id="lot-second",
            symbol="601318",
            execution_date=date(2026, 7, 14),
            available_date=date(2026, 7, 15),
            unit_cost=Decimal("5"),
            fees=_fees(commission_fen=100),
        ),
    )

    buy_event = updated.cash_events[-1]
    assert buy_event.cash_before_fen == cash_after_sell
    assert buy_event.cash_after_fen == cash_after_sell - 50_000 - 100
    assert updated.cash_fen == buy_event.cash_after_fen
    rolling_accounting.verify_rolling_ledger(updated)


def test_failed_sell_is_atomic():
    ledger = post_rolling_buy(
        create_rolling_ledger(2_000_000),
        _buy_posting(
            event_id="buy-atomic",
            lot_id="lot-atomic",
            symbol="600519",
            execution_date=date(2026, 7, 13),
            available_date=date(2026, 7, 14),
            size=200,
            unit_cost=Decimal("10"),
            fees=_fees(commission_fen=0),
        ),
    )
    before = ledger
    posting = rolling_accounting.SellPosting(
        "sell-atomic",
        date(2026, 7, 14),
        "600519",
        100,
        Decimal("12"),
        _fees(commission_fen=2_000_001),
    )

    with pytest.raises(PortfolioError) as captured:
        rolling_accounting.post_rolling_sell(ledger, posting)

    assert captured.value.code == "insufficient_cash"
    assert ledger is before
    assert ledger == before
    assert tuple(lot.remaining_size for lot in ledger.lots) == (200,)
    assert tuple(event.event_id for event in ledger.cash_events) == ("buy-atomic",)
    rolling_accounting.verify_rolling_ledger(ledger)


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        pytest.param("event_id", [], "invalid_sell_posting", id="event-id-list"),
        pytest.param(
            "execution_date",
            None,
            "invalid_sell_posting",
            id="execution-date-none",
        ),
        pytest.param("symbol", "bad", "invalid_sell_posting", id="invalid-symbol"),
        pytest.param("size", "100", "invalid_sell_posting", id="size-string"),
        pytest.param("unit_price", 12, "invalid_sell_posting", id="price-int"),
        pytest.param(
            "unit_price",
            Decimal("0"),
            "invalid_sell_posting",
            id="price-nonpositive",
        ),
        pytest.param("fees", object(), "invalid_fees", id="fees-nonexact"),
    ],
)
def test_post_rolling_sell_rejects_malformed_fields_with_stable_error_and_is_atomic(
    field,
    value,
    expected_code,
):
    ledger = post_rolling_buy(
        create_rolling_ledger(3_000_000),
        _buy_posting(
            event_id="buy-malformed",
            lot_id="lot-malformed",
            symbol="600519",
            execution_date=date(2026, 7, 13),
            available_date=date(2026, 7, 14),
            unit_cost=Decimal("10"),
            fees=_fees(commission_fen=0),
        ),
    )
    before = ledger
    posting = replace(
        rolling_accounting.SellPosting(
            "sell-malformed",
            date(2026, 7, 14),
            "600519",
            100,
            Decimal("12"),
            _fees(commission_fen=0),
        ),
        **{field: value},
    )

    with pytest.raises(PortfolioError) as captured:
        rolling_accounting.post_rolling_sell(ledger, posting)

    assert captured.value.code == expected_code
    assert ledger is before
    assert ledger == before
    assert tuple(lot.remaining_size for lot in ledger.lots) == (100,)
    assert tuple(event.event_id for event in ledger.cash_events) == ("buy-malformed",)
    rolling_accounting.verify_rolling_ledger(ledger)


def test_rolling_verifier_rejects_tampered_consumption_cash_fee_and_order():
    ledger = post_rolling_buy(
        create_rolling_ledger(3_000_000),
        _buy_posting(
            event_id="buy-replay",
            lot_id="lot-replay",
            symbol="600519",
            execution_date=date(2026, 7, 13),
            available_date=date(2026, 7, 14),
            size=200,
            unit_cost=Decimal("10"),
            fees=_fees(commission_fen=100),
        ),
    )
    ledger = rolling_accounting.post_rolling_sell(
        ledger,
        rolling_accounting.SellPosting(
            "sell-replay",
            date(2026, 7, 14),
            "600519",
            100,
            Decimal("12"),
            _fees(commission_fen=500, stamp_duty_fen=300, transfer_fee_fen=10),
        ),
    )
    buy_event, sell_event = ledger.cash_events
    assert type(sell_event) is rolling_accounting.SellFillEvent
    corrupted = (
        replace(
            ledger,
            cash_events=(
                buy_event,
                replace(
                    sell_event,
                    consumptions=(rolling_accounting.LotConsumption("lot-replay", 200),),
                ),
            ),
        ),
        replace(ledger, cash_fen=ledger.cash_fen + 1),
        replace(
            ledger,
            cash_events=(
                buy_event,
                replace(sell_event, commission_fen=sell_event.commission_fen + 1),
            ),
        ),
        replace(ledger, cash_events=(sell_event, buy_event)),
    )

    for damaged in corrupted:
        with pytest.raises(PortfolioError):
            rolling_accounting.verify_rolling_ledger(damaged)


def test_rolling_verifier_rejects_reordered_lots_even_when_consumptions_and_terminal_sizes_are_rewritten():  # noqa: E501
    ledger = create_rolling_ledger(5_000_000)
    ledger = post_rolling_buy(
        ledger,
        _buy_posting(
            event_id="buy-canonical-1",
            lot_id="lot-canonical-1",
            symbol="600519",
            execution_date=date(2026, 7, 13),
            available_date=date(2026, 7, 14),
            unit_cost=Decimal("10"),
            fees=_fees(commission_fen=0),
        ),
    )
    ledger = post_rolling_buy(
        ledger,
        _buy_posting(
            event_id="buy-canonical-2",
            lot_id="lot-canonical-2",
            symbol="600519",
            execution_date=date(2026, 7, 13),
            available_date=date(2026, 7, 14),
            unit_cost=Decimal("11"),
            fees=_fees(commission_fen=0),
        ),
    )
    ledger = rolling_accounting.post_rolling_sell(
        ledger,
        rolling_accounting.SellPosting(
            "sell-canonical",
            date(2026, 7, 14),
            "600519",
            100,
            Decimal("12"),
            _fees(commission_fen=0),
        ),
    )
    buy_one, buy_two, sell_event = ledger.cash_events
    lot_one, lot_two = ledger.lots
    assert type(sell_event) is rolling_accounting.SellFillEvent
    damaged = replace(
        ledger,
        lots=(
            replace(lot_two, remaining_size=0),
            replace(lot_one, remaining_size=100),
        ),
        cash_events=(
            buy_one,
            buy_two,
            replace(
                sell_event,
                consumptions=(rolling_accounting.LotConsumption("lot-canonical-2", 100),),
            ),
        ),
    )

    with pytest.raises(PortfolioError):
        rolling_accounting.verify_rolling_ledger(damaged)


def test_rolling_verifier_rejects_non_exact_lot_consumption_even_if_equality_is_spoofed():
    class EqualitySpoof:
        def __eq__(self, other: object) -> bool:
            return True

    ledger = post_rolling_buy(
        create_rolling_ledger(3_000_000),
        _buy_posting(
            event_id="buy-spoof",
            lot_id="lot-spoof",
            symbol="600519",
            execution_date=date(2026, 7, 13),
            available_date=date(2026, 7, 14),
            unit_cost=Decimal("10"),
            fees=_fees(commission_fen=0),
        ),
    )
    ledger = rolling_accounting.post_rolling_sell(
        ledger,
        rolling_accounting.SellPosting(
            "sell-spoof",
            date(2026, 7, 14),
            "600519",
            100,
            Decimal("12"),
            _fees(commission_fen=0),
        ),
    )
    buy_event, sell_event = ledger.cash_events
    assert type(sell_event) is rolling_accounting.SellFillEvent
    damaged = replace(
        ledger,
        cash_events=(
            buy_event,
            replace(sell_event, consumptions=(EqualitySpoof(),)),
        ),
    )

    with pytest.raises(PortfolioError):
        rolling_accounting.verify_rolling_ledger(damaged)


def test_close_rolling_session_recomputes_cash_market_receivable_and_equity():
    legacy = post_buy(create_portfolio_ledger(1_000_000), _buy_posting())
    receivable = CashReceivable(
        event_id="action-rolling",
        symbol="510300",
        registered_date=date(2026, 7, 14),
        source_payable_date=date(2026, 7, 20),
        actual_cash_date=date(2026, 7, 20),
        amount_fen=10_000,
    )
    legacy = register_receivable(legacy, receivable)
    ledger = promote_portfolio_ledger(legacy)
    valuation = SymbolValuation(
        symbol="510300",
        total_size=100,
        available_size=0,
        locked_size=100,
        mark_price=Decimal("96"),
    )

    updated = rolling_accounting.close_rolling_session(
        ledger,
        date(2026, 7, 14),
        (valuation,),
    )

    snapshot = updated.daily_snapshots[-1]
    assert snapshot.cash_fen == 49_500
    assert snapshot.position_market_value_fen == 960_000
    assert snapshot.receivable_fen == 10_000
    assert snapshot.equity_fen == 1_019_500
    rolling_accounting.verify_rolling_ledger(updated)


def test_historical_snapshot_remains_valid_after_a_later_sell():
    ledger = post_rolling_buy(
        create_rolling_ledger(3_000_000),
        _buy_posting(
            event_id="buy-history",
            lot_id="lot-history",
            symbol="600519",
            execution_date=date(2026, 7, 13),
            available_date=date(2026, 7, 14),
            size=200,
            unit_cost=Decimal("10"),
            fees=_fees(commission_fen=0),
        ),
    )
    ledger = rolling_accounting.close_rolling_session(
        ledger,
        date(2026, 7, 14),
        (SymbolValuation("600519", 200, 200, 0, Decimal("11")),),
    )
    historical = ledger.daily_snapshots[0]

    ledger = rolling_accounting.post_rolling_sell(
        ledger,
        rolling_accounting.SellPosting(
            "sell-history",
            date(2026, 7, 15),
            "600519",
            100,
            Decimal("12"),
            _fees(commission_fen=0),
        ),
    )
    ledger = rolling_accounting.close_rolling_session(
        ledger,
        date(2026, 7, 15),
        (SymbolValuation("600519", 100, 100, 0, Decimal("12")),),
    )

    assert ledger.daily_snapshots[0] == historical
    assert historical.valuations[0].total_size == 200
    assert ledger.daily_snapshots[-1].valuations[0].total_size == 100
    rolling_accounting.verify_rolling_ledger(ledger)


def test_pristine_conditions_cannot_be_inferred_from_missing_snapshots_only():
    legacy = post_buy(
        create_portfolio_ledger(1_000_000),
        _buy_posting(
            event_id="buy-no-snapshot",
            lot_id="lot-no-snapshot",
            unit_cost=Decimal("10"),
            fees=_fees(commission_fen=0),
        ),
    )
    assert legacy.daily_snapshots == ()
    verify_portfolio_ledger(legacy)

    rolling = promote_portfolio_ledger(legacy)

    assert rolling.daily_snapshots == ()
    assert rolling.lots == legacy.lots
    assert rolling.cash_events == legacy.cash_events
    assert rolling.cash_fen != rolling.initial_cash_fen
    rolling_accounting.verify_rolling_ledger(rolling)
