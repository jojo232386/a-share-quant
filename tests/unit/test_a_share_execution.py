from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal

import backtrader as bt
import pandas as pd
import pytest

from aquant.backtest import (
    BacktestConfig,
    BacktestDataError,
    StrategyName,
    run_synthetic_backtest,
)
from aquant.backtest_cli import main as backtest_cli_main
from aquant.data.calendar_snapshot import (
    CalendarSnapshotStore,
    load_verified_calendar,
)
from aquant.data.corporate_actions import CorporateActionEvent
from aquant.rules import (
    CommissionAssumption,
    InstrumentKind,
    default_fee_policy,
    make_fee_policy,
)


def _calendar(tmp_path, *values: str):
    record = CalendarSnapshotStore(tmp_path).write(
        tuple(date.fromisoformat(value) for value in values),
        source_provider="synthetic",
        source_function="pytest_fixture",
        source_version="1",
        fetched_at_utc=datetime(2026, 7, 31, tzinfo=UTC),
    )
    return load_verified_calendar(tmp_path, record)


def _frame(
    dates: tuple[str, ...],
    *,
    opens: tuple[float, ...],
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    closes: tuple[float, ...],
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": (10_000,) * len(dates),
            "amount": (100_000.0,) * len(dates),
        }
    )


def _run_buy(
    tmp_path,
    frame: pd.DataFrame,
    *,
    calendar_dates: tuple[str, ...],
    initial_cash: float = 10_000.0,
    instrument_kind: InstrumentKind = InstrumentKind.MAIN_BOARD_STOCK,
    symbol: str = "600519",
    target_weight: Decimal = Decimal("0.95"),
    corporate_action_events: tuple[CorporateActionEvent, ...] = (),
):
    return run_synthetic_backtest(
        frame,
        symbol=symbol,
        calendar=_calendar(tmp_path, *calendar_dates),
        fee_policy=default_fee_policy(),
        instrument_kind=instrument_kind,
        corporate_action_events=corporate_action_events,
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=initial_cash,
            target_weight=target_weight,
        ),
    )


def _cash_dividend(
    *,
    ex_date: date,
    payable_date: date,
    amount: Decimal = Decimal("2"),
) -> CorporateActionEvent:
    return CorporateActionEvent.create(
        symbol="600519",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        announcement_date=date(2026, 7, 1),
        record_date=date.fromordinal(ex_date.toordinal() - 1),
        ex_date=ex_date,
        payable_date=payable_date,
        cash_dividend_per_unit=amount,
        stock_dividend_ratio=Decimal("0"),
        capitalization_ratio=Decimal("0"),
        rights_ratio=Decimal("0"),
        rights_price=None,
        source_schema="synthetic.cash.v1",
        source_url="https://example.invalid/corporate-actions",
    )


def test_dividend_registers_before_ex_open_and_pays_without_changing_equity(
    tmp_path,
):
    event = _cash_dividend(
        ex_date=date(2026, 7, 15),
        payable_date=date(2026, 7, 17),
    )
    result = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17"),
            opens=(100.0, 100.0, 98.0, 98.0, 98.0),
            highs=(100.0, 100.0, 98.0, 98.0, 98.0),
            lows=(100.0, 100.0, 98.0, 98.0, 98.0),
            closes=(100.0, 100.0, 98.0, 98.0, 98.0),
        ),
        calendar_dates=(
            "2026-07-13",
            "2026-07-14",
            "2026-07-15",
            "2026-07-16",
            "2026-07-17",
            "2026-07-20",
        ),
        initial_cash=11_000.0,
        corporate_action_events=(event,),
    )

    by_date = {row.date: row.balance for row in result.receivables}
    assert by_date[date(2026, 7, 14)] == 0
    assert by_date[date(2026, 7, 15)] == 200.0
    assert by_date[date(2026, 7, 16)] == 200.0
    assert by_date[date(2026, 7, 17)] == 0
    assert [row.event_type for row in result.corporate_action_ledger] == [
        "receivable_registered",
        "cash_paid",
    ]
    assert result.cash_ledger[1].cash == result.cash_ledger[2].cash
    assert result.cash_ledger[4].cash == pytest.approx(
        result.cash_ledger[3].cash + 200.0
    )
    assert result.equity_curve[1].equity == pytest.approx(
        result.equity_curve[2].equity
    )
    assert result.equity_curve[3].equity == pytest.approx(
        result.equity_curve[4].equity
    )


def test_same_day_dividend_registers_then_pays_exactly_once(tmp_path):
    event = _cash_dividend(
        ex_date=date(2026, 7, 15),
        payable_date=date(2026, 7, 15),
    )
    result = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13", "2026-07-14", "2026-07-15"),
            opens=(100.0, 100.0, 98.0),
            highs=(100.0, 100.0, 98.0),
            lows=(100.0, 100.0, 98.0),
            closes=(100.0, 100.0, 98.0),
        ),
        calendar_dates=(
            "2026-07-13",
            "2026-07-14",
            "2026-07-15",
            "2026-07-16",
        ),
        initial_cash=11_000.0,
        corporate_action_events=(event,),
    )

    assert [row.event_type for row in result.corporate_action_ledger] == [
        "receivable_registered",
        "cash_paid",
    ]
    assert result.receivables[-1].balance_fen == 0
    assert result.cash_ledger[-1].cash == pytest.approx(
        result.cash_ledger[-2].cash + 200.0
    )


def test_purchase_at_ex_open_does_not_receive_that_dividend(tmp_path):
    event = _cash_dividend(
        ex_date=date(2026, 7, 14),
        payable_date=date(2026, 7, 14),
    )
    result = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13", "2026-07-14", "2026-07-15"),
            opens=(100.0, 98.0, 98.0),
            highs=(100.0, 98.0, 98.0),
            lows=(100.0, 98.0, 98.0),
            closes=(100.0, 98.0, 98.0),
        ),
        calendar_dates=(
            "2026-07-13",
            "2026-07-14",
            "2026-07-15",
            "2026-07-16",
        ),
        initial_cash=11_000.0,
        corporate_action_events=(event,),
    )

    assert result.fills[0].execution_date == date(2026, 7, 14)
    assert result.corporate_action_ledger == ()
    assert all(row.balance_fen == 0 for row in result.receivables)


def test_sale_at_ex_open_keeps_the_preopen_entitlement(tmp_path):
    event = _cash_dividend(
        ex_date=date(2026, 7, 16),
        payable_date=date(2026, 7, 17),
    )
    frame = _frame(
        (
            "2026-07-13",
            "2026-07-14",
            "2026-07-15",
            "2026-07-16",
            "2026-07-17",
        ),
        opens=(100.0, 101.0, 101.0, 89.0, 89.0),
        highs=(100.0, 101.0, 101.0, 89.0, 89.0),
        lows=(100.0, 101.0, 90.0, 89.0, 89.0),
        closes=(100.0, 101.0, 90.0, 89.0, 89.0),
    )
    result = run_synthetic_backtest(
        frame,
        symbol="600519",
        calendar=_calendar(
            tmp_path,
            "2026-07-13",
            "2026-07-14",
            "2026-07-15",
            "2026-07-16",
            "2026-07-17",
            "2026-07-20",
        ),
        fee_policy=default_fee_policy(),
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        corporate_action_events=(event,),
        config=BacktestConfig(
            strategy=StrategyName.SMA,
            initial_cash=11_000.0,
            target_weight=Decimal("0.95"),
            sma_period=2,
        ),
    )

    assert [(fill.side, fill.execution_date) for fill in result.fills[:2]] == [
        ("buy", date(2026, 7, 15)),
        ("sell", date(2026, 7, 16)),
    ]
    registered = result.corporate_action_ledger[0]
    assert registered.event_type == "receivable_registered"
    assert registered.entitled_size == result.fills[0].size
    assert result.receivables[3].balance == 200.0
    assert result.receivables[4].balance == 0


def test_backtrader_market_hook_signature_is_the_pinned_contract():
    assert tuple(inspect.signature(bt.brokers.BackBroker._try_exec_market).parameters) == (
        "self",
        "order",
        "popen",
        "phigh",
        "plow",
    )


@pytest.mark.parametrize(
    ("price", "expected_size"),
    [
        (10.0, 95_000),
        (100.0, 9_500),
        (1_000.0, 900),
    ],
)
def test_next_open_target_sizing_replaces_placeholder_before_fill(
    tmp_path, price, expected_size
):
    result = run_synthetic_backtest(
        _frame(
            ("2026-07-13", "2026-07-14"),
            opens=(price, price),
            highs=(price, price),
            lows=(price, price),
            closes=(price, price),
        ),
        symbol="600519",
        calendar=_calendar(
            tmp_path, "2026-07-13", "2026-07-14", "2026-07-15"
        ),
        fee_policy=default_fee_policy(),
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=1_000_000.0,
            target_weight=Decimal("0.95"),
        ),
    )

    assert result.orders[0].requested_size == expected_size
    assert Decimal(result.orders[0].actual_weight) <= Decimal("0.95")
    assert result.fills[0].size == expected_size
    assert result.fills[0].value <= 950_000.0


def test_target_sizing_uses_actual_gap_open_and_shrinks_for_fees(tmp_path):
    gap_result = _run_buy(
        tmp_path / "gap",
        _frame(
            ("2026-07-13", "2026-07-14"),
            opens=(10.0, 10.8),
            highs=(10.0, 10.8),
            lows=(10.0, 10.8),
            closes=(10.0, 10.8),
        ),
        calendar_dates=("2026-07-13", "2026-07-14", "2026-07-15"),
    )
    fee_result = _run_buy(
        tmp_path / "fees",
        _frame(
            ("2026-07-13", "2026-07-14"),
            opens=(10.0, 10.0),
            highs=(10.0, 10.0),
            lows=(10.0, 10.0),
            closes=(10.0, 10.0),
        ),
        calendar_dates=("2026-07-13", "2026-07-14", "2026-07-15"),
        target_weight=Decimal("1"),
    )

    assert gap_result.fills[0].size == 800
    assert fee_result.fills[0].size == 900
    assert fee_result.cash_ledger[-1].cash > 0


def test_missing_target_session_is_rejected_and_not_filled_on_reopen(tmp_path):
    result = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13", "2026-07-15"),
            opens=(10.0, 12.0),
            highs=(10.5, 12.5),
            lows=(9.5, 11.5),
            closes=(10.0, 12.0),
        ),
        calendar_dates=("2026-07-13", "2026-07-14", "2026-07-15"),
    )

    assert result.orders[0].target_execution_date == date(2026, 7, 14)
    assert result.orders[0].final_status == "rejected"
    assert result.orders[0].rejection_reason == "suspended_no_bar"
    assert result.fills == ()
    assert result.missing_market_sessions == (date(2026, 7, 14),)


def test_limit_open_is_rejected_but_intraday_touch_after_open_is_allowed(tmp_path):
    rejected = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13", "2026-07-14"),
            opens=(10.0, 11.0),
            highs=(10.0, 11.0),
            lows=(10.0, 11.0),
            closes=(10.0, 11.0),
        ),
        calendar_dates=("2026-07-13", "2026-07-14", "2026-07-15"),
    )
    allowed = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13", "2026-07-14"),
            opens=(10.0, 10.5),
            highs=(10.0, 11.0),
            lows=(10.0, 10.0),
            closes=(10.0, 10.8),
        ),
        calendar_dates=("2026-07-13", "2026-07-14", "2026-07-15"),
    )

    assert rejected.orders[0].rejection_reason == "price_limit_open"
    assert rejected.orders[0].requested_size == 0
    assert rejected.fills == ()
    assert allowed.fills[0].price == pytest.approx(10.5)
    assert allowed.fills[0].commission_fen == 500
    assert allowed.fills[0].transfer_fee_fen == 9
    assert allowed.fills[0].stamp_duty_fen == 0
    assert allowed.fills[0].total_fees_fen == 509
    assert allowed.fills[0].commission == pytest.approx(5.09)
    assert allowed.positions[-1].size == 900
    assert allowed.positions[-1].available_size == 0
    assert allowed.positions[-1].locked_size == 900
    assert allowed.lots[0].available_date == date(2026, 7, 15)
    assert {item.fee_name for item in allowed.touched_fee_rates} == {
        "commission",
        "transfer_fee",
    }


def test_fill_value_is_rounded_to_currency_precision(tmp_path):
    result = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13", "2026-07-14"),
            opens=(70.0, 74.6),
            highs=(70.0, 75.0),
            lows=(70.0, 74.0),
            closes=(70.0, 74.8),
        ),
        calendar_dates=("2026-07-13", "2026-07-14", "2026-07-15"),
    )

    assert result.fills[0].value == 7_460.0


def test_rule_engine_accepts_new_universe_member_without_symbol_whitelist(tmp_path):
    result = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13", "2026-07-14"),
            opens=(40.0, 40.5),
            highs=(40.0, 41.0),
            lows=(40.0, 40.0),
            closes=(40.0, 40.8),
        ),
        calendar_dates=("2026-07-13", "2026-07-14", "2026-07-15"),
        symbol="600036",
    )

    assert result.orders[0].final_status == "completed"
    assert result.fills[0].execution_date == date(2026, 7, 14)


def test_fill_price_does_not_export_binary_float_artifacts(tmp_path):
    result = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13", "2026-07-14"),
            opens=(3.1, 3.3510000000000004),
            highs=(3.2, 3.4),
            lows=(3.0, 3.3),
            closes=(3.1, 3.36),
        ),
        calendar_dates=("2026-07-13", "2026-07-14", "2026-07-15"),
        instrument_kind=InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF,
        symbol="510300",
    )

    assert result.fills[0].price == 3.351
    assert str(result.fills[0].price) == "3.351"


def test_cash_including_fees_rejects_when_one_lot_is_unaffordable(tmp_path):
    result = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13", "2026-07-14"),
            opens=(100.0, 100.0),
            highs=(100.0, 100.0),
            lows=(100.0, 100.0),
            closes=(100.0, 100.0),
        ),
        calendar_dates=("2026-07-13", "2026-07-14", "2026-07-15"),
        initial_cash=10_004.99,
        target_weight=Decimal("1"),
    )

    assert result.orders[0].rejection_reason == "insufficient_cash"
    assert result.orders[0].requested_size == 0
    assert result.fills == ()


def test_order_at_dataset_end_is_closed_as_missing_target_bar(tmp_path):
    result = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13",),
            opens=(10.0,),
            highs=(10.5,),
            lows=(9.5,),
            closes=(10.0,),
        ),
        calendar_dates=("2026-07-13", "2026-07-14"),
    )

    assert result.orders[0].target_execution_date == date(2026, 7, 14)
    assert result.orders[0].final_status == "rejected"
    assert result.orders[0].rejection_reason == "suspended_no_bar"
    assert result.fills == ()


def test_buy_is_rejected_before_fill_when_t_plus_one_calendar_is_uncovered(tmp_path):
    result = _run_buy(
        tmp_path,
        _frame(
            ("2026-07-13", "2026-07-14"),
            opens=(10.0, 10.5),
            highs=(10.5, 10.8),
            lows=(9.5, 10.0),
            closes=(10.0, 10.5),
        ),
        calendar_dates=("2026-07-13", "2026-07-14"),
    )

    assert result.orders[0].final_status == "rejected"
    assert result.orders[0].rejection_reason == "no_next_session_in_range"
    assert result.fills == ()


def test_run_id_binds_calendar_and_fee_policy(tmp_path):
    frame = _frame(
        ("2026-07-13", "2026-07-14"),
        opens=(10.0, 10.5),
        highs=(10.5, 10.8),
        lows=(9.5, 10.0),
        closes=(10.0, 10.5),
    )
    config = BacktestConfig(
        strategy=StrategyName.BUY_AND_HOLD,
        initial_cash=10_000.0,
        target_weight=Decimal("0.95"),
    )
    first = run_synthetic_backtest(
        frame,
        calendar=_calendar(
            tmp_path / "first", "2026-07-13", "2026-07-14", "2026-07-15"
        ),
        fee_policy=default_fee_policy(),
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        config=config,
    )
    second = run_synthetic_backtest(
        frame,
        calendar=_calendar(
            tmp_path / "second",
            "2026-07-13",
            "2026-07-14",
            "2026-07-15",
            "2026-07-16",
        ),
        fee_policy=default_fee_policy(),
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        config=config,
    )
    third = run_synthetic_backtest(
        frame,
        calendar=_calendar(
            tmp_path / "third", "2026-07-13", "2026-07-14", "2026-07-15"
        ),
        fee_policy=make_fee_policy(
            stock_commission=CommissionAssumption(
                Decimal("0.00030"), Decimal("5.00")
            )
        ),
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        config=config,
    )

    assert len({first.run_id, second.run_id, third.run_id}) == 3


def test_cli_requires_calendar_id_and_all_commission_assumptions(capsys):
    exit_code = backtest_cli_main(
        [
            "run",
            "--project-root",
            ".",
            "--symbol",
            "600519",
            "--snapshot-id",
            "a" * 64,
            "--strategy",
            "buy_and_hold",
        ]
    )

    assert exit_code == 1
    assert '"error_code":"invalid_arguments"' in capsys.readouterr().err


def test_runner_recomputes_calendar_and_fee_policy_digests_after_tampering(tmp_path):
    frame = _frame(
        ("2026-07-13", "2026-07-14"),
        opens=(10.0, 10.5),
        highs=(10.5, 10.8),
        lows=(9.5, 10.0),
        closes=(10.0, 10.5),
    )
    calendar = _calendar(
        tmp_path / "calendar", "2026-07-13", "2026-07-14", "2026-07-15"
    )
    policy = default_fee_policy()
    object.__setattr__(
        calendar,
        "dates",
        (date(2026, 7, 13), date(2026, 7, 15)),
    )
    with pytest.raises(BacktestDataError) as calendar_error:
        run_synthetic_backtest(
            frame,
            calendar=calendar,
            fee_policy=policy,
            instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
            config=BacktestConfig(
                strategy=StrategyName.BUY_AND_HOLD,
                initial_cash=10_000.0,
                target_weight=Decimal("0.95"),
            ),
        )
    assert calendar_error.value.code == "verified_calendar_modified"

    clean_calendar = _calendar(
        tmp_path / "clean", "2026-07-13", "2026-07-14", "2026-07-15"
    )
    object.__setattr__(
        policy,
        "stock_commission",
        CommissionAssumption(Decimal("0.00001"), Decimal("0.00")),
    )
    with pytest.raises(BacktestDataError) as fee_error:
        run_synthetic_backtest(
            frame,
            calendar=clean_calendar,
            fee_policy=policy,
            instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
            config=BacktestConfig(
                strategy=StrategyName.BUY_AND_HOLD,
                initial_cash=10_000.0,
                target_weight=Decimal("0.95"),
            ),
        )
    assert fee_error.value.code == "verified_fee_policy_modified"
