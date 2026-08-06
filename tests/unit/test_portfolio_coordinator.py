from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from aquant.backtest import load_verified_snapshot
from aquant.data.calendar_snapshot import (
    CalendarSnapshotStore,
    load_verified_calendar,
)
from aquant.data.corporate_actions import (
    CorporateActionEvent,
    make_synthetic_corporate_actions,
)
from aquant.data.manifest import ManifestRecord
from aquant.data.snapshot import RawSnapshotStore
from aquant.portfolio import (
    PortfolioConfig,
    PortfolioError,
    PortfolioInstrumentInput,
    PortfolioStrategy,
)
from aquant.portfolio.coordinator import (
    AttemptStatus,
    AvailabilityStatus,
    TargetStatus,
    actual_cash_date,
    run_portfolio_backtest,
)
from aquant.rules import InstrumentKind, RejectionReason, default_fee_policy, make_fee_policy
from aquant.universe import (
    UniverseMember,
    canonical_universe_bytes,
    load_verified_universe,
)


def _calendar(root: Path, dates: tuple[date, ...]):
    record = CalendarSnapshotStore(root).write(
        dates,
        source_provider="synthetic",
        source_function="test_calendar",
        source_version="1",
        fetched_at_utc=datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
    )
    return load_verified_calendar(root, record)


def _raw_market_frame(
    dates: tuple[date, ...],
    *,
    opens: tuple[Decimal, ...] | None = None,
    closes: tuple[Decimal, ...] | None = None,
) -> pd.DataFrame:
    open_values = opens or tuple(Decimal("10.00") for _ in dates)
    close_values = closes or open_values
    return pd.DataFrame(
        {
            "日期": [item.isoformat() for item in dates],
            "开盘": [float(item) for item in open_values],
            "最高": [
                float(max(open_value, close_value) + Decimal("0.10"))
                for open_value, close_value in zip(
                    open_values,
                    close_values,
                    strict=True,
                )
            ],
            "最低": [
                float(min(open_value, close_value) - Decimal("0.10"))
                for open_value, close_value in zip(
                    open_values,
                    close_values,
                    strict=True,
                )
            ],
            "收盘": [float(item) for item in close_values],
            "成交量": [10_000 for _ in dates],
            "成交额": [100_000.0 for _ in dates],
        }
    )


def _verified_market(
    root: Path,
    symbol: str,
    dates: tuple[date, ...],
    *,
    opens: tuple[Decimal, ...] | None = None,
    closes: tuple[Decimal, ...] | None = None,
):
    artifact = RawSnapshotStore(root).write(
        _raw_market_frame(dates, opens=opens, closes=closes),
        symbol=symbol,
        source_slug="eastmoney",
        snapshot_date=date(2026, 7, 22),
    )
    record = ManifestRecord.create(
        schema_version="1.0",
        symbol=symbol,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK.value,
        provider="eastmoney",
        source_function="stock_zh_a_hist",
        source_schema="akshare.stock_zh_a_hist",
        endpoint_host="push2his.eastmoney.com",
        provider_symbol="sh" + symbol,
        fetched_at_utc=datetime(2026, 7, 22, 8, 0, tzinfo=UTC),
        requested_start=dates[0],
        requested_end=dates[-1],
        actual_start=dates[0],
        actual_end=dates[-1],
        row_count=artifact.row_count,
        snapshot_relative_path=artifact.relative_path,
        file_sha256=artifact.sha256,
        adjustment="",
        factor_source=None,
        latest_market_date=dates[-1],
        akshare_version="1.18.64",
        raw_volume_unit="lot",
        volume_multiplier_to_canonical=100,
        full_history_download=False,
        local_date_slice=False,
        quality_issue_counts={
            "empty_frame": 0,
            "null": 0,
            "duplicate_date": 0,
            "out_of_order_date": 0,
            "non_finite_numeric": 0,
            "non_positive_price": 0,
            "negative_volume": 0,
            "negative_amount": 0,
            "invalid_high": 0,
            "invalid_low": 0,
        },
    )
    return load_verified_snapshot(root, record)


def _verified_universe(root: Path, symbols: tuple[str, ...]):
    members = tuple(
        UniverseMember(symbol, InstrumentKind.MAIN_BOARD_STOCK.value)
        for symbol in symbols
    )
    content = canonical_universe_bytes("coordinator-test", members)
    universe_id = hashlib.sha256(content).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{universe_id}.json"
    path.write_bytes(content)
    return load_verified_universe(path, expected_id=universe_id)


def _run_fixture(
    root: Path,
    *,
    symbols: tuple[str, ...] = ("600001", "600000"),
):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
    )
    market_dates = official_dates[:2]
    inputs = tuple(
        PortfolioInstrumentInput(
            market_data=_verified_market(
                root / "market" / symbol,
                symbol,
                market_dates,
            ),
            corporate_actions=make_synthetic_corporate_actions(
                (),
                symbol=symbol,
                instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
                coverage_start=official_dates[0],
                coverage_end=official_dates[-1],
            ),
        )
        for symbol in symbols
    )
    return {
        "config": PortfolioConfig(
            strategy=PortfolioStrategy.BUY_AND_HOLD,
            initial_cash_fen=2_000_000,
            gross_target_weight=Decimal("0.95"),
            signal_date=official_dates[0],
            end_date=official_dates[1],
            max_entry_attempts=5,
        ),
        "inputs": inputs,
        "universe": _verified_universe(root / "universe", symbols),
        "calendar": _calendar(root / "calendar", official_dates),
        "fee_policy": default_fee_policy(),
    }


def _scenario(
    root: Path,
    *,
    symbols: tuple[str, ...],
    official_dates: tuple[date, ...],
    market_dates: tuple[date, ...],
    opens: tuple[Decimal, ...] | None = None,
    closes: tuple[Decimal, ...] | None = None,
    initial_cash_fen: int = 10_000_000,
    max_entry_attempts: int = 5,
):
    inputs = tuple(
        PortfolioInstrumentInput(
            market_data=_verified_market(
                root / "market" / symbol,
                symbol,
                market_dates,
                opens=opens,
                closes=closes,
            ),
            corporate_actions=make_synthetic_corporate_actions(
                (),
                symbol=symbol,
                instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
                coverage_start=official_dates[0],
                coverage_end=official_dates[-1],
            ),
        )
        for symbol in symbols
    )
    return {
        "config": PortfolioConfig(
            strategy=PortfolioStrategy.BUY_AND_HOLD,
            initial_cash_fen=initial_cash_fen,
            gross_target_weight=Decimal("0.95"),
            signal_date=official_dates[0],
            end_date=official_dates[-2],
            max_entry_attempts=max_entry_attempts,
        ),
        "inputs": inputs,
        "universe": _verified_universe(root / "universe", symbols),
        "calendar": _calendar(root / "calendar", official_dates),
        "fee_policy": default_fee_policy(),
    }


def _cash_dividend_event(
    *,
    symbol: str = "600000",
    record_date: date = date(2026, 7, 16),
    ex_date: date = date(2026, 7, 17),
    payable_date: date = date(2026, 7, 18),
    cash_per_unit: Decimal = Decimal("1.00"),
    stock_ratio: Decimal = Decimal("0"),
) -> CorporateActionEvent:
    return CorporateActionEvent.create(
        symbol=symbol,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        announcement_date=date(2026, 7, 1),
        record_date=record_date,
        ex_date=ex_date,
        payable_date=payable_date,
        cash_dividend_per_unit=cash_per_unit,
        stock_dividend_ratio=stock_ratio,
        capitalization_ratio=Decimal("0"),
        rights_ratio=Decimal("0"),
        rights_price=None,
        source_schema="synthetic.cash.v1",
        source_url="https://example.invalid/cash",
    )


def _dividend_scenario(
    root: Path,
    *,
    market_dates: tuple[date, ...],
    event: CorporateActionEvent | None = None,
):
    official_dates = (
        date(2026, 7, 15),
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
        date(2026, 7, 21),
    )
    action = event or _cash_dividend_event()
    prices = tuple(
        Decimal("9.00") if item >= action.ex_date else Decimal("10.00")
        for item in market_dates
    )
    portfolio_input = PortfolioInstrumentInput(
        market_data=_verified_market(
            root / "market",
            action.symbol,
            market_dates,
            opens=prices,
            closes=prices,
        ),
        corporate_actions=make_synthetic_corporate_actions(
            (action,),
            symbol=action.symbol,
            instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
            coverage_start=official_dates[0],
            coverage_end=official_dates[-1],
        ),
    )
    return {
        "config": PortfolioConfig(
            strategy=PortfolioStrategy.BUY_AND_HOLD,
            initial_cash_fen=1_000_000,
            gross_target_weight=Decimal("0.95"),
            signal_date=official_dates[0],
            end_date=official_dates[-2],
            max_entry_attempts=5,
        ),
        "inputs": (portfolio_input,),
        "universe": _verified_universe(
            root / "universe",
            (action.symbol,),
        ),
        "calendar": _calendar(root / "calendar", official_dates),
        "fee_policy": default_fee_policy(),
    }


def test_actual_cash_date_keeps_session_or_moves_to_next_official_session(tmp_path):
    calendar = _calendar(
        tmp_path,
        (
            date(2026, 7, 16),
            date(2026, 7, 17),
            date(2026, 7, 20),
        ),
    )

    assert actual_cash_date(calendar, date(2026, 7, 17)) == date(2026, 7, 17)
    assert actual_cash_date(calendar, date(2026, 7, 18)) == date(2026, 7, 20)


def test_target_attempt_ids_and_order_are_deterministic(tmp_path):
    result = run_portfolio_backtest(**_run_fixture(tmp_path))

    assert tuple(item.symbol for item in result.targets) == ("600000", "600001")
    assert tuple(item.attempt_number for item in result.attempts) == (1, 1)
    assert len({item.attempt_id for item in result.attempts}) == 2
    assert all(item.status is AttemptStatus.FILLED for item in result.attempts)
    assert all(item.status is TargetStatus.FILLED for item in result.targets)


def test_filled_attempt_retains_the_exact_touched_fee_rates(tmp_path):
    result = run_portfolio_backtest(
        **_run_fixture(tmp_path, symbols=("600000",))
    )

    attempt = result.attempts[0]
    assert attempt.fees is not None
    assert attempt.fees.commission_fen == 500
    assert attempt.fees.transfer_fee_fen == 19
    assert tuple(
        item.fee_name for item in attempt.fees.touched_rates
    ) == ("commission", "transfer_fee")


def test_five_no_bar_sessions_create_five_attempts_then_expire(tmp_path):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
        date(2026, 7, 21),
        date(2026, 7, 22),
        date(2026, 7, 23),
        date(2026, 7, 24),
    )
    result = run_portfolio_backtest(
        **_scenario(
            tmp_path,
            symbols=("600000",),
            official_dates=official_dates,
            market_dates=(official_dates[0],),
        )
    )

    assert [item.attempt_number for item in result.attempts] == [1, 2, 3, 4, 5]
    assert all(
        item.rejection_reason.value == "suspended_no_bar"
        for item in result.attempts
    )
    assert result.targets[0].attempts_used == 5
    assert result.targets[0].status is TargetStatus.EXPIRED_UNFILLED


def test_price_limit_rejection_retries_on_next_official_session(tmp_path):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
        date(2026, 7, 21),
    )
    result = run_portfolio_backtest(
        **_scenario(
            tmp_path,
            symbols=("600000",),
            official_dates=official_dates,
            market_dates=official_dates[:3],
            opens=(
                Decimal("10.00"),
                Decimal("11.00"),
                Decimal("10.50"),
            ),
            closes=(
                Decimal("10.00"),
                Decimal("10.00"),
                Decimal("10.50"),
            ),
        )
    )

    assert result.attempts[0].rejection_reason.value == "price_limit_open"
    assert result.attempts[1].status is AttemptStatus.FILLED
    assert result.targets[0].attempts_used == 2


def test_two_three_and_ten_member_runs_keep_shared_cash_nonnegative(tmp_path):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
    )
    for member_count in (2, 3, 10):
        symbols = tuple(f"60000{index}" for index in range(member_count))
        result = run_portfolio_backtest(
            **_scenario(
                tmp_path / str(member_count),
                symbols=tuple(reversed(symbols)),
                official_dates=official_dates,
                market_dates=official_dates[:2],
            )
        )

        assert len(result.targets) == member_count
        assert len(result.ledger.lots) == member_count
        assert result.ledger.cash_fen >= 0
        assert tuple(item.symbol for item in result.targets) == symbols
        assert all(item.original_size % 100 == 0 for item in result.ledger.lots)
        assert all(
            item.available_date == date(2026, 7, 20)
            for item in result.ledger.lots
        )


def test_fee_aware_shared_cash_reduces_only_current_symbol_by_whole_lots(
    tmp_path,
):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
    )
    signatures = []
    for index, symbols in enumerate(
        (
            ("600000", "600001"),
            ("600001", "600000"),
        )
    ):
        arguments = _scenario(
            tmp_path / str(index),
            symbols=symbols,
            official_dates=official_dates,
            market_dates=official_dates[:2],
            initial_cash_fen=2_000_000,
        )
        arguments["config"] = PortfolioConfig(
            strategy=PortfolioStrategy.BUY_AND_HOLD,
            initial_cash_fen=2_000_000,
            gross_target_weight=Decimal("1"),
            signal_date=official_dates[0],
            end_date=official_dates[1],
            max_entry_attempts=5,
        )

        result = run_portfolio_backtest(**arguments)
        attempts = tuple(
            (
                item.symbol,
                item.initial_candidate_size,
                item.requested_size,
                item.status,
                item.fees.total_fees_fen if item.fees is not None else None,
                item.cash_available_before_fen,
                item.initial_candidate_cash_required_fen,
                item.requested_cash_required_fen,
                item.quantity_adjustment_reason,
            )
            for item in result.attempts
        )
        lots = tuple(
            (item.symbol, item.original_size) for item in result.ledger.lots
        )
        signatures.append((attempts, lots, result.ledger.cash_fen))

    assert signatures[0] == signatures[1]
    attempts, lots, cash_fen = signatures[0]
    assert attempts == (
        (
            "600000",
            1000,
            1000,
            AttemptStatus.FILLED,
            510,
            2_000_000,
            1_000_510,
            1_000_510,
            None,
        ),
        (
            "600001",
            1000,
            900,
            AttemptStatus.FILLED,
            509,
            999_490,
            1_000_510,
            900_509,
            "insufficient_cash_including_fees",
        ),
    )
    assert lots == (("600000", 1000), ("600001", 900))
    assert cash_fen == 98_981
    assert cash_fen >= 0


def test_attempt_cash_counterfactual_evidence_fails_closed(tmp_path):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
    )
    arguments = _scenario(
        tmp_path / "fills",
        symbols=("600000", "600001"),
        official_dates=official_dates,
        market_dates=official_dates[:2],
        initial_cash_fen=2_000_000,
    )
    arguments["config"] = PortfolioConfig(
        strategy=PortfolioStrategy.BUY_AND_HOLD,
        initial_cash_fen=2_000_000,
        gross_target_weight=Decimal("1"),
        signal_date=official_dates[0],
        end_date=official_dates[1],
        max_entry_attempts=5,
    )
    first, adjusted = run_portfolio_backtest(**arguments).attempts

    with pytest.raises(PortfolioError) as unadjusted_error:
        replace(
            first,
            quantity_adjustment_reason="insufficient_cash_including_fees",
        )
    assert unadjusted_error.value.code == "invalid_attempt"

    with pytest.raises(PortfolioError) as adjusted_error:
        replace(adjusted, quantity_adjustment_reason=None)
    assert adjusted_error.value.code == "invalid_attempt"

    rejected_arguments = _scenario(
        tmp_path / "rejected",
        symbols=("600000",),
        official_dates=official_dates,
        market_dates=official_dates[:1],
    )
    rejected = run_portfolio_backtest(**rejected_arguments).attempts[0]
    with pytest.raises(PortfolioError) as rejected_error:
        replace(rejected, cash_available_before_fen=0)
    assert rejected_error.value.code == "invalid_attempt"


def test_price_limit_rejection_precedes_fee_counterfactual_evidence(tmp_path):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
    )
    arguments = _scenario(
        tmp_path,
        symbols=("600000",),
        official_dates=official_dates,
        market_dates=official_dates[:2],
        opens=(Decimal("10"), Decimal("11")),
        closes=(Decimal("10"), Decimal("11")),
    )
    arguments["fee_policy"] = make_fee_policy(
        transfer_fee_schedule=(
            (date(2027, 1, 1), Decimal("0.00001")),
        )
    )

    result = run_portfolio_backtest(**arguments)

    attempt = result.attempts[0]
    assert attempt.status is AttemptStatus.REJECTED
    assert attempt.rejection_reason is RejectionReason.PRICE_LIMIT_OPEN
    assert attempt.cash_available_before_fen is None
    assert attempt.initial_candidate_cash_required_fen is None
    assert attempt.requested_cash_required_fen is None
    assert attempt.quantity_adjustment_reason is None


def test_fixed_target_notional_is_not_reallocated_after_high_price_rejection(
    tmp_path,
):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
    )
    first = PortfolioInstrumentInput(
        market_data=_verified_market(
            tmp_path / "first-market",
            "600000",
            official_dates[:2],
            opens=(Decimal("10.00"), Decimal("100000.00")),
            closes=(Decimal("10.00"), Decimal("100000.00")),
        ),
        corporate_actions=make_synthetic_corporate_actions(
            (),
            symbol="600000",
            instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
            coverage_start=official_dates[0],
            coverage_end=official_dates[-1],
        ),
    )
    second = PortfolioInstrumentInput(
        market_data=_verified_market(
            tmp_path / "second-market",
            "600001",
            official_dates[:2],
        ),
        corporate_actions=make_synthetic_corporate_actions(
            (),
            symbol="600001",
            instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
            coverage_start=official_dates[0],
            coverage_end=official_dates[-1],
        ),
    )
    result = run_portfolio_backtest(
        config=PortfolioConfig(
            strategy=PortfolioStrategy.BUY_AND_HOLD,
            initial_cash_fen=2_000_000,
            gross_target_weight=Decimal("0.95"),
            signal_date=official_dates[0],
            end_date=official_dates[1],
            max_entry_attempts=5,
        ),
        inputs=(second, first),
        universe=_verified_universe(
            tmp_path / "universe",
            ("600001", "600000"),
        ),
        calendar=_calendar(tmp_path / "calendar", official_dates),
        fee_policy=default_fee_policy(),
    )

    rejected = next(item for item in result.attempts if item.symbol == "600000")
    filled_lot = next(item for item in result.ledger.lots if item.symbol == "600001")
    assert rejected.initial_candidate_size == 0
    assert rejected.rejection_reason.value == "invalid_lot_size"
    assert filled_lot.original_size == 900


def test_nontrading_payable_date_pays_on_next_session_without_symbol_bar(
    tmp_path,
):
    result = run_portfolio_backtest(
        **_dividend_scenario(
            tmp_path,
            market_dates=(
                date(2026, 7, 15),
                date(2026, 7, 16),
                date(2026, 7, 17),
            ),
        )
    )

    receivable = result.ledger.receivables[0]
    assert receivable.source_payable_date == date(2026, 7, 18)
    assert receivable.actual_cash_date == date(2026, 7, 20)
    assert receivable.paid_date == date(2026, 7, 20)
    snapshot = result.ledger.daily_snapshots[-1]
    assert snapshot.session == date(2026, 7, 20)
    assert snapshot.receivable_fen == 0
    assert result.dividends[0].entitled_size == 900
    assert result.dividends[0].amount_fen == 90_000
    assert result.ledger.cash_fen == 189_491
    assert snapshot.equity_fen == 999_491


def test_no_bar_ex_date_adjusts_mark_and_receivable_without_double_count(
    tmp_path,
):
    result = run_portfolio_backtest(
        **_dividend_scenario(
            tmp_path,
            market_dates=(
                date(2026, 7, 15),
                date(2026, 7, 16),
                date(2026, 7, 20),
            ),
        )
    )

    snapshot = next(
        item
        for item in result.ledger.daily_snapshots
        if item.session == date(2026, 7, 17)
    )
    valuation = snapshot.valuations[0]
    assert valuation.mark_price == Decimal("9.00")
    assert snapshot.receivable_fen == 90_000
    assert snapshot.equity_fen == (
        snapshot.cash_fen
        + snapshot.position_market_value_fen
        + snapshot.receivable_fen
    )
    audit = next(
        item
        for item in result.availability
        if item.session == date(2026, 7, 17)
    )
    assert audit.status is AvailabilityStatus.NO_BAR_UNAVAILABLE
    assert audit.adjustment_reason == "cash_dividend"


def test_buy_after_record_date_is_not_entitled_to_cash_dividend(tmp_path):
    event = _cash_dividend_event(
        ex_date=date(2026, 7, 20),
        payable_date=date(2026, 7, 21),
    )
    result = run_portfolio_backtest(
        **_dividend_scenario(
            tmp_path,
            market_dates=(
                date(2026, 7, 15),
                date(2026, 7, 17),
                date(2026, 7, 20),
            ),
            event=event,
        )
    )

    assert result.ledger.lots[0].acquired_date == date(2026, 7, 17)
    assert event.record_date == date(2026, 7, 16)
    assert result.dividends[0].entitled_size == 0
    assert result.dividends[0].amount_fen == 0
    assert result.ledger.receivables == ()


def test_buy_on_record_date_is_entitled_to_cash_dividend(tmp_path):
    event = _cash_dividend_event()
    result = run_portfolio_backtest(
        **_dividend_scenario(
            tmp_path,
            market_dates=(
                date(2026, 7, 15),
                date(2026, 7, 16),
                date(2026, 7, 17),
            ),
            event=event,
        )
    )

    assert result.ledger.lots[0].acquired_date == event.record_date
    assert result.dividends[0].entitled_size == 900
    assert result.dividends[0].amount_fen == 90_000


def test_record_date_must_precede_ex_date_before_any_ledger_change(tmp_path):
    event = _cash_dividend_event(
        record_date=date(2026, 7, 17),
        ex_date=date(2026, 7, 17),
    )

    with pytest.raises(PortfolioError) as captured:
        run_portfolio_backtest(
            **_dividend_scenario(
                tmp_path,
                market_dates=(
                    date(2026, 7, 15),
                    date(2026, 7, 16),
                    date(2026, 7, 17),
                ),
                event=event,
            )
        )

    assert captured.value.code == "invalid_corporate_action_dates"


def test_non_cash_corporate_action_fails_before_any_result(tmp_path):
    event = _cash_dividend_event(
        cash_per_unit=Decimal("0"),
        stock_ratio=Decimal("0.10"),
    )

    with pytest.raises(PortfolioError) as captured:
        run_portfolio_backtest(
            **_dividend_scenario(
                tmp_path,
                market_dates=(
                    date(2026, 7, 15),
                    date(2026, 7, 16),
                    date(2026, 7, 17),
                    date(2026, 7, 20),
                ),
                event=event,
            )
        )

    assert getattr(captured.value, "code", None) == "unsupported_corporate_action"


def test_in_range_corporate_action_ex_date_must_be_an_official_session(
    tmp_path,
):
    event = _cash_dividend_event(
        ex_date=date(2026, 7, 19),
        payable_date=date(2026, 7, 20),
    )

    with pytest.raises(PortfolioError) as captured:
        run_portfolio_backtest(
            **_dividend_scenario(
                tmp_path,
                market_dates=(
                    date(2026, 7, 15),
                    date(2026, 7, 16),
                    date(2026, 7, 17),
                    date(2026, 7, 20),
                ),
                event=event,
            )
        )

    assert captured.value.code == "invalid_corporate_action_session"


def test_portfolio_requires_one_post_end_session_for_t_plus_one(tmp_path):
    arguments = _run_fixture(tmp_path / "fixture", symbols=("600000",))
    arguments["calendar"] = _calendar(
        tmp_path / "short-calendar",
        (
            date(2026, 7, 16),
            date(2026, 7, 17),
        ),
    )

    with pytest.raises(PortfolioError) as captured:
        run_portfolio_backtest(**arguments)

    assert captured.value.code == "missing_calendar_coverage"


def test_portfolio_rechecks_fee_policy_identity_before_running(tmp_path):
    arguments = _run_fixture(tmp_path, symbols=("600000",))
    object.__setattr__(arguments["fee_policy"], "policy_digest", "0" * 64)

    with pytest.raises(PortfolioError) as captured:
        run_portfolio_backtest(**arguments)

    assert captured.value.code == "invalid_fee_policy"


def test_corporate_action_snapshot_must_cover_the_whole_run_range(tmp_path):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
    )
    portfolio_input = PortfolioInstrumentInput(
        market_data=_verified_market(
            tmp_path / "market",
            "600000",
            (official_dates[0],),
        ),
        corporate_actions=make_synthetic_corporate_actions(
            (),
            symbol="600000",
            instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
            coverage_start=official_dates[0],
            coverage_end=official_dates[0],
        ),
    )

    with pytest.raises(PortfolioError) as captured:
        run_portfolio_backtest(
            config=PortfolioConfig(
                strategy=PortfolioStrategy.BUY_AND_HOLD,
                initial_cash_fen=1_000_000,
                gross_target_weight=Decimal("0.95"),
                signal_date=official_dates[0],
                end_date=official_dates[1],
                max_entry_attempts=5,
            ),
            inputs=(portfolio_input,),
            universe=_verified_universe(
                tmp_path / "universe",
                ("600000",),
            ),
            calendar=_calendar(tmp_path / "calendar", official_dates),
            fee_policy=default_fee_policy(),
        )

    assert captured.value.code == "corporate_action_coverage_gap"
