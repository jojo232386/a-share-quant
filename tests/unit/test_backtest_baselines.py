from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import backtrader as bt
import pandas as pd
import pytest

from aquant.backtest import (
    BacktestConfig,
    BacktestDataError,
    BacktestExportError,
    DataProvenance,
    StrategyName,
    export_backtest_result,
    load_verified_snapshot,
    run_backtest,
    run_synthetic_backtest,
)
from aquant.backtest.strategies import AuditedBaselineStrategy
from aquant.data.calendar_snapshot import (
    CalendarSnapshotStore,
    load_verified_calendar,
)
from aquant.data.corporate_actions import (
    load_verified_corporate_actions,
    publish_corporate_actions,
)
from aquant.data.manifest import ManifestRecord, ManifestWriter
from aquant.data.snapshot import RawSnapshotStore
from aquant.rules import InstrumentKind, default_fee_policy
from aquant.universe import (
    UniverseMember,
    canonical_universe_bytes,
    load_verified_universe,
)


def market_frame(
    *,
    opens: list[float] | None = None,
    closes: list[float] | None = None,
) -> pd.DataFrame:
    dates = pd.to_datetime(
        [
            "2026-07-13",
            "2026-07-14",
            "2026-07-15",
            "2026-07-16",
            "2026-07-17",
            "2026-07-20",
        ]
    )
    open_values = opens or [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    close_values = closes or [10.5, 11.5, 12.5, 13.5, 14.5, 15.5]
    return pd.DataFrame(
        {
            "date": dates,
            "open": open_values,
            "high": [
                max(open_price, close_price) + 0.5
                for open_price, close_price in zip(open_values, close_values, strict=True)
            ],
            "low": [
                min(open_price, close_price) - 0.5
                for open_price, close_price in zip(open_values, close_values, strict=True)
            ],
            "close": close_values,
            "volume": [10_000] * len(dates),
            "amount": [100_000.0] * len(dates),
        }
    )


def verified_calendar(root: Path):
    record = CalendarSnapshotStore(root).write(
        (date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15)),
        source_provider="synthetic",
        source_function="pytest_fixture",
        source_version="1",
        fetched_at_utc=datetime(2026, 7, 16, tzinfo=UTC),
    )
    return load_verified_calendar(root, record)


def verified_corporate_actions(
    root: Path,
    *,
    normalization_version: str = "cash-only-v1",
):
    root.mkdir(parents=True, exist_ok=True)
    record = publish_corporate_actions(
        root,
        (),
        symbol="600519",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        provider="synthetic",
        source_schema="synthetic.cash.v1",
        normalization_version=normalization_version,
        coverage_start=date(2026, 7, 13),
        coverage_end=date(2026, 7, 20),
    )
    return load_verified_corporate_actions(root, record)


def verified_universe(root: Path, *, symbol: str = "600519"):
    member = UniverseMember(symbol, "main_board_stock")
    content = canonical_universe_bytes("pytest-universe", (member,))
    universe_id = hashlib.sha256(content).hexdigest()
    directory = root / "configs" / "universes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{universe_id}.json"
    path.write_bytes(content)
    return load_verified_universe(path, expected_id=universe_id)


def required_rule_cli_args(
    calendar_id: str = "b" * 64,
    universe_id: str = "d" * 64,
) -> list[str]:
    return [
        "--calendar-id",
        calendar_id,
        "--universe-id",
        universe_id,
        "--stock-commission-rate",
        "0.00025",
        "--stock-minimum-commission",
        "5.00",
        "--etf-commission-rate",
        "0.00025",
        "--etf-minimum-commission",
        "5.00",
    ]


def test_buy_and_hold_signals_after_first_close_and_fills_at_next_open():
    result = run_synthetic_backtest(
        market_frame(),
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=10_000.0,
            target_weight=Decimal("0.95"),
        ),
    )

    assert len(result.orders) == 1
    assert result.orders[0].order_id == "order-0001"
    assert result.orders[0].signal_date.isoformat() == "2026-07-13"
    assert result.orders[0].side == "buy"
    assert result.orders[0].final_status == "completed"

    assert len(result.fills) == 1
    assert result.fills[0].order_id == "order-0001"
    assert result.fills[0].execution_date.isoformat() == "2026-07-14"
    assert result.fills[0].price == pytest.approx(11.0)
    assert result.fills[0].size == 100
    assert result.fills[0].value == pytest.approx(1_100.0)
    assert result.fills[0].commission == 0.0


def test_sma_uses_close_only_and_both_orders_fill_on_following_open():
    frame = market_frame(
        opens=[10.0, 9.0, 11.0, 12.0, 8.0, 7.0],
        closes=[10.0, 9.0, 11.0, 12.0, 8.0, 7.0],
    )
    result = run_synthetic_backtest(
        frame,
        config=BacktestConfig(
            strategy=StrategyName.SMA,
            initial_cash=10_000.0,
            target_weight=Decimal("0.95"),
            sma_period=2,
        ),
    )

    assert [(order.signal_date.isoformat(), order.side) for order in result.orders] == [
        ("2026-07-15", "buy"),
        ("2026-07-17", "sell"),
    ]
    assert [
        (fill.execution_date.isoformat(), fill.side, fill.price) for fill in result.fills
    ] == [
        ("2026-07-16", "buy", 12.0),
        ("2026-07-20", "sell", 7.0),
    ]
    assert result.fills[0].size == result.fills[1].size


def test_daily_ledgers_cover_every_bar_and_satisfy_accounting_identity():
    result = run_synthetic_backtest(
        market_frame(),
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=10_000.0,
            target_weight=Decimal("0.95"),
        ),
    )

    assert len(result.positions) == len(result.cash_ledger) == len(result.equity_curve) == 6
    for position, cash, equity in zip(
        result.positions, result.cash_ledger, result.equity_curve, strict=True
    ):
        assert position.date == cash.date == equity.date
        assert position.market_value == pytest.approx(position.size * position.close)
        assert equity.equity == pytest.approx(cash.cash + position.market_value)


def test_repeat_run_is_structurally_identical_including_local_order_ids():
    config = BacktestConfig(
        strategy=StrategyName.BUY_AND_HOLD,
        initial_cash=10_000.0,
        target_weight=Decimal("0.95"),
    )
    frame = market_frame()

    first = run_synthetic_backtest(frame, config=config)
    second = run_synthetic_backtest(frame.copy(), config=config)

    assert first == second
    assert first.run_id == second.run_id
    assert len(first.implementation_digest) == 64


def test_repeated_completed_callback_does_not_duplicate_fill():
    class CompletedBuyOrder:
        ref = 7
        status = bt.Order.Completed
        Completed = bt.Order.Completed
        Canceled = bt.Order.Canceled
        Margin = bt.Order.Margin
        Rejected = bt.Order.Rejected
        executed = type(
            "Executed",
            (),
            {
                "size": 100,
                "price": 11.0,
                "dt": bt.date2num(datetime(2026, 7, 14)),
                "comm": 0.0,
            },
        )()

        @staticmethod
        def getstatusname() -> str:
            return "Completed"

        @staticmethod
        def isbuy() -> bool:
            return True

    strategy = object.__new__(AuditedBaselineStrategy)
    strategy._pending_order = CompletedBuyOrder()
    strategy._engine_ref_to_local_id = {7: "order-0001"}
    strategy._order_state = {
        "order-0001": {
            "order_id": "order-0001",
            "signal_date": date(2026, 7, 13),
            "side": "buy",
            "requested_size": 100,
            "final_status": "submitted",
        }
    }
    strategy._fills = []
    order = CompletedBuyOrder()

    strategy.notify_order(order)
    strategy.notify_order(order)

    assert strategy._order_state["order-0001"]["final_status"] == "completed"
    assert len(strategy._fills) == 1
    assert strategy._fills[0].order_id == "order-0001"
    assert strategy._pending_order is None


def test_partial_callback_is_not_exported_as_a_completed_fill():
    class PartialBuyOrder:
        ref = 7
        status = bt.Order.Partial
        Completed = bt.Order.Completed
        Canceled = bt.Order.Canceled
        Margin = bt.Order.Margin
        Rejected = bt.Order.Rejected

        @staticmethod
        def getstatusname() -> str:
            return "Partial"

    pending_order = PartialBuyOrder()
    strategy = object.__new__(AuditedBaselineStrategy)
    strategy._pending_order = pending_order
    strategy._engine_ref_to_local_id = {7: "order-0001"}
    strategy._order_state = {
        "order-0001": {
            "order_id": "order-0001",
            "signal_date": date(2026, 7, 13),
            "side": "buy",
            "requested_size": 100,
            "final_status": "accepted",
        }
    }
    strategy._fills = []

    strategy.notify_order(pending_order)

    assert strategy._order_state["order-0001"]["final_status"] == "partial"
    assert strategy._fills == []
    assert strategy._pending_order is pending_order


def test_public_core_runner_rejects_an_unverified_dataframe(tmp_path):
    with pytest.raises(TypeError, match="VerifiedMarketData"):
        run_backtest(
            market_frame(),
            universe=verified_universe(tmp_path),
            corporate_actions=verified_corporate_actions(tmp_path),
            calendar=verified_calendar(tmp_path),
            fee_policy=default_fee_policy(),
            config=BacktestConfig(
                strategy=StrategyName.BUY_AND_HOLD,
                initial_cash=10_000.0,
                target_weight=Decimal("0.95"),
            ),
        )


def test_bad_market_data_fails_closed_without_reordering_or_repair():
    frame = market_frame().iloc[::-1].reset_index(drop=True)

    with pytest.raises(BacktestDataError) as error:
        run_synthetic_backtest(
            frame,
            config=BacktestConfig(
                strategy=StrategyName.BUY_AND_HOLD,
                initial_cash=10_000.0,
                target_weight=Decimal("0.95"),
            ),
        )

    assert error.value.code == "invalid_market_data"


def test_formal_run_id_binds_corporate_action_snapshot_and_normalization(tmp_path):
    market = load_verified_snapshot(tmp_path, stored_manifest_record(tmp_path))
    calendar = verified_calendar(tmp_path)
    config = BacktestConfig(
        strategy=StrategyName.BUY_AND_HOLD,
        initial_cash=10_000.0,
        target_weight=Decimal("0.95"),
    )
    first = run_backtest(
        market,
        universe=verified_universe(tmp_path),
        corporate_actions=verified_corporate_actions(
            tmp_path / "actions-v1",
            normalization_version="cash-only-v1",
        ),
        calendar=calendar,
        fee_policy=default_fee_policy(),
        config=config,
    )
    second = run_backtest(
        market,
        universe=verified_universe(tmp_path),
        corporate_actions=verified_corporate_actions(
            tmp_path / "actions-v2",
            normalization_version="cash-only-v2",
        ),
        calendar=calendar,
        fee_policy=default_fee_policy(),
        config=config,
    )

    assert first.run_id != second.run_id
    assert first.corporate_action_provenance.snapshot_id != (
        second.corporate_action_provenance.snapshot_id
    )


def test_formal_runner_requires_membership_in_verified_universe(tmp_path):
    market = load_verified_snapshot(tmp_path, stored_manifest_record(tmp_path))

    with pytest.raises(BacktestDataError) as captured:
        run_backtest(
            market,
            universe=verified_universe(tmp_path, symbol="601318"),
            corporate_actions=verified_corporate_actions(tmp_path),
            calendar=verified_calendar(tmp_path),
            fee_policy=default_fee_policy(),
            config=BacktestConfig(
                strategy=StrategyName.BUY_AND_HOLD,
                initial_cash=10_000.0,
                target_weight=Decimal("0.95"),
            ),
        )

    assert captured.value.code == "universe_contract_mismatch"


def test_formal_runner_rejects_action_identity_or_coverage_mismatch(tmp_path):
    market = load_verified_snapshot(tmp_path, stored_manifest_record(tmp_path))
    action_root = tmp_path / "bad-actions"
    action_root.mkdir()
    record = publish_corporate_actions(
        action_root,
        (),
        symbol="601318",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        provider="synthetic",
        source_schema="synthetic.cash.v1",
        normalization_version="cash-only-v1",
        coverage_start=date(2026, 7, 13),
        coverage_end=date(2026, 7, 14),
    )

    with pytest.raises(BacktestDataError) as captured:
        run_backtest(
            market,
            universe=verified_universe(tmp_path),
            corporate_actions=load_verified_corporate_actions(
                action_root, record
            ),
            calendar=verified_calendar(tmp_path),
            fee_policy=default_fee_policy(),
            config=BacktestConfig(
                strategy=StrategyName.BUY_AND_HOLD,
                initial_cash=10_000.0,
                target_weight=Decimal("0.95"),
            ),
        )

    assert captured.value.code == "corporate_action_contract_mismatch"


@pytest.mark.parametrize(
    "values",
    [
        {
            "strategy": StrategyName.BUY_AND_HOLD,
            "initial_cash": 10_000.0,
            "target_weight": Decimal("0.95"),
            "sma_period": 2,
        },
        {
            "strategy": StrategyName.SMA,
            "initial_cash": 10_000.0,
            "target_weight": Decimal("0.95"),
        },
    ],
)
def test_strategy_config_rejects_hidden_or_missing_sma_parameter(values):
    with pytest.raises(ValueError):
        BacktestConfig(**values)


@pytest.mark.parametrize(
    "target_weight",
    [0.95, Decimal("0"), Decimal("1.01"), Decimal("NaN")],
)
def test_strategy_config_requires_exact_bounded_decimal_target_weight(
    target_weight,
):
    with pytest.raises(ValueError):
        BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=10_000.0,
            target_weight=target_weight,
        )


def raw_eastmoney_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": ["2026-07-13", "2026-07-14"],
            "开盘": [10.0, 11.0],
            "最高": [10.8, 11.8],
            "最低": [9.8, 10.8],
            "收盘": [10.5, 11.5],
            "成交量": [100, 120],
            "成交额": [105_000.0, 138_000.0],
        }
    )


def stored_manifest_record(
    root: Path,
    *,
    symbol: str = "600519",
    instrument_kind: str = "main_board_stock",
    **changes,
) -> ManifestRecord:
    artifact = RawSnapshotStore(root).write(
        raw_eastmoney_frame(),
        symbol=symbol,
        source_slug="eastmoney",
        snapshot_date=date(2026, 7, 15),
    )
    values = {
        "schema_version": "1.0",
        "symbol": symbol,
        "instrument_kind": instrument_kind,
        "provider": "eastmoney",
        "source_function": "stock_zh_a_hist",
        "source_schema": "akshare.stock_zh_a_hist",
        "endpoint_host": "push2his.eastmoney.com",
        "provider_symbol": ("sh" if symbol.startswith(("5", "6")) else "sz") + symbol,
        "fetched_at_utc": datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
        "requested_start": date(2026, 7, 13),
        "requested_end": date(2026, 7, 14),
        "actual_start": date(2026, 7, 13),
        "actual_end": date(2026, 7, 14),
        "row_count": artifact.row_count,
        "snapshot_relative_path": artifact.relative_path,
        "file_sha256": artifact.sha256,
        "adjustment": "",
        "factor_source": None,
        "latest_market_date": date(2026, 7, 14),
        "akshare_version": "1.18.64",
        "raw_volume_unit": "lot",
        "volume_multiplier_to_canonical": 100,
        "full_history_download": False,
        "local_date_slice": False,
        "quality_issue_counts": {
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
    }
    values.update(changes)
    return ManifestRecord.create(**values)


def test_verified_snapshot_loader_checks_hash_then_normalizes_raw_prices(tmp_path):
    record = stored_manifest_record(tmp_path)

    loaded = load_verified_snapshot(tmp_path, record)

    assert loaded.provenance == DataProvenance(
        symbol="600519",
        snapshot_id=record.snapshot_id,
        file_sha256=record.file_sha256,
        adjustment="",
        verification_method="manifest_sha256",
    )
    assert loaded.frame.columns.tolist() == [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    assert loaded.frame["volume"].tolist() == [10_000, 12_000]


def test_verified_snapshot_loader_rejects_corrupt_content(tmp_path):
    record = stored_manifest_record(tmp_path)
    snapshot = tmp_path / record.snapshot_relative_path
    snapshot.chmod(0o644)
    snapshot.write_bytes(b"corrupt")

    with pytest.raises(BacktestDataError) as error:
        load_verified_snapshot(tmp_path, record)

    assert error.value.code == "snapshot_verification_failed"


def test_verified_snapshot_loader_rejects_forged_adjustment_or_unit_metadata(tmp_path):
    record = stored_manifest_record(tmp_path)

    with pytest.raises(BacktestDataError) as adjusted:
        load_verified_snapshot(
            tmp_path,
            replace(record, adjustment="qfq", factor_source="forged"),
        )
    with pytest.raises(BacktestDataError) as wrong_unit:
        load_verified_snapshot(
            tmp_path,
            replace(record, volume_multiplier_to_canonical=1),
        )

    assert adjusted.value.code == "adjusted_price_forbidden"
    assert wrong_unit.value.code == "source_contract_violation"


def test_verified_snapshot_loader_rechecks_manifest_row_count_and_dates(tmp_path):
    record = stored_manifest_record(tmp_path)

    with pytest.raises(BacktestDataError) as error:
        load_verified_snapshot(tmp_path, replace(record, row_count=record.row_count + 1))

    assert error.value.code == "manifest_content_mismatch"


def test_verified_snapshot_loader_rejects_mixed_source_contract_and_out_of_scope_symbol(
    tmp_path,
):
    record = stored_manifest_record(tmp_path)
    out_of_scope = stored_manifest_record(
        tmp_path,
        symbol="300001",
        instrument_kind="main_board_stock",
    )

    with pytest.raises(BacktestDataError) as mixed:
        load_verified_snapshot(
            tmp_path,
            replace(record, endpoint_host="finance.sina.com.cn"),
        )
    with pytest.raises(BacktestDataError) as unsupported:
        load_verified_snapshot(tmp_path, out_of_scope)

    assert mixed.value.code == "source_contract_violation"
    assert unsupported.value.code == "source_contract_violation"


def test_verified_market_data_returns_copies_and_is_the_only_formal_runner_input(tmp_path):
    loaded = load_verified_snapshot(tmp_path, stored_manifest_record(tmp_path))
    universe = verified_universe(tmp_path)
    changed = loaded.frame
    changed.loc[0, "open"] = 999.0

    assert loaded.frame.loc[0, "open"] == 10.0
    result = run_backtest(
        loaded,
        universe=universe,
        corporate_actions=verified_corporate_actions(tmp_path),
        calendar=verified_calendar(tmp_path),
        fee_policy=default_fee_policy(),
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=10_000.0,
            target_weight=Decimal("0.95"),
        ),
    )
    assert result.fills[0].price == 11.0
    assert result.universe_id == universe.universe_id


def test_formal_runner_rechecks_digest_if_internal_verified_frame_is_tampered(tmp_path):
    loaded = load_verified_snapshot(tmp_path, stored_manifest_record(tmp_path))
    loaded._frame.loc[0, "open"] = 10.1

    with pytest.raises(BacktestDataError) as error:
        run_backtest(
            loaded,
            universe=verified_universe(tmp_path),
            corporate_actions=verified_corporate_actions(tmp_path),
            calendar=verified_calendar(tmp_path),
            fee_policy=default_fee_policy(),
            config=BacktestConfig(
                strategy=StrategyName.BUY_AND_HOLD,
                initial_cash=10_000.0,
                target_weight=Decimal("0.95"),
            ),
        )

    assert error.value.code == "verified_data_modified"


def test_export_is_idempotent_and_persists_config_snapshot_and_all_ledgers(tmp_path):
    result = run_synthetic_backtest(
        market_frame(),
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=10_000.0,
            target_weight=Decimal("0.95"),
        ),
    )

    first = export_backtest_result(result, tmp_path)
    before = {path.name: path.read_bytes() for path in first.iterdir()}
    second = export_backtest_result(result, tmp_path)
    after = {path.name: path.read_bytes() for path in second.iterdir()}

    assert first == second == tmp_path / result.run_id
    assert set(before) == {
        "run.json",
        "orders.csv",
        "fills.csv",
        "positions.csv",
        "cash.csv",
        "equity.csv",
        "lots.csv",
        "corporate_actions.csv",
        "receivables.csv",
        "missing_sessions.json",
        "artifact_manifest.json",
    }
    assert before == after
    metadata = json.loads((first / "run.json").read_text(encoding="utf-8"))
    assert metadata["run_id"] == result.run_id
    assert metadata["provenance"]["snapshot_id"] == result.provenance.snapshot_id
    assert metadata["provenance"]["adjustment"] == ""
    assert metadata["price_stream_version"] == "causal-cash-v1"
    assert metadata["dividend_tax_mode"] == "gross_before_personal_tax"
    assert metadata["universe_id"] is None
    assert metadata["corporate_action_provenance"]["verification_method"] == (
        "synthetic_digest"
    )
    assert metadata["config"] == {
        "initial_cash": 10_000.0,
        "random_seed": 0,
        "sma_period": None,
        "target_weight": "0.95",
        "strategy": "buy_and_hold",
    }


def test_export_never_overwrites_a_conflicting_existing_artifact(tmp_path):
    result = run_synthetic_backtest(
        market_frame(),
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=10_000.0,
            target_weight=Decimal("0.95"),
        ),
    )
    directory = export_backtest_result(result, tmp_path)
    orders = directory / "orders.csv"
    orders.write_text("conflict\n", encoding="utf-8")

    with pytest.raises(BacktestExportError, match="conflicts"):
        export_backtest_result(result, tmp_path)

    assert orders.read_text(encoding="utf-8") == "conflict\n"


def test_export_does_not_publish_any_new_file_when_existing_bundle_is_partial(
    tmp_path,
):
    result = run_synthetic_backtest(
        market_frame(),
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=10_000.0,
            target_weight=Decimal("0.95"),
        ),
    )
    directory = tmp_path / result.run_id
    directory.mkdir()
    (directory / "equity.csv").write_text("conflict\n", encoding="utf-8")

    with pytest.raises(BacktestExportError):
        export_backtest_result(result, tmp_path)

    assert {path.name for path in directory.iterdir()} == {"equity.csv"}


def test_artifact_manifest_hashes_every_completed_payload_file(tmp_path):
    result = run_synthetic_backtest(
        market_frame(),
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=10_000.0,
            target_weight=Decimal("0.95"),
        ),
    )
    directory = export_backtest_result(result, tmp_path)
    manifest = json.loads((directory / "artifact_manifest.json").read_text(encoding="utf-8"))

    assert manifest["status"] == "complete"
    assert manifest["run_id"] == result.run_id
    assert manifest["files"] == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.iterdir()
        if path.name != "artifact_manifest.json"
    }


def test_backtest_cli_runs_only_an_explicit_snapshot_and_prints_stable_summary(
    tmp_path, capsys
):
    from aquant.backtest_cli import main

    record = stored_manifest_record(tmp_path)
    ManifestWriter(tmp_path / "data/manifests/manifest.jsonl").append(record)
    calendar = verified_calendar(tmp_path)
    actions = verified_corporate_actions(tmp_path)
    universe = verified_universe(tmp_path)

    exit_code = main(
        [
            "run",
            "--project-root",
            str(tmp_path),
            "--symbol",
            record.symbol,
            "--snapshot-id",
            record.snapshot_id,
            "--corporate-action-snapshot-id",
            actions.provenance.snapshot_id,
            "--strategy",
            "buy_and_hold",
            "--initial-cash",
            "10000",
            "--target-weight",
            "0.95",
            *required_rule_cli_args(
                calendar.calendar_id,
                universe.universe_id,
            ),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "artifact_directory": f"outputs/backtests/{payload['run_id']}",
        "fills": 1,
        "orders": 1,
        "run_id": payload["run_id"],
        "snapshot_id": record.snapshot_id,
        "status": "ok",
        "strategy": "buy_and_hold",
        "symbol": "600519",
        "universe_id": universe.universe_id,
    }
    assert (tmp_path / payload["artifact_directory"] / "run.json").is_file()


def test_backtest_cli_fails_closed_when_snapshot_id_is_not_in_manifest(tmp_path, capsys):
    from aquant.backtest_cli import main

    exit_code = main(
        [
            "run",
            "--project-root",
            str(tmp_path),
            "--symbol",
            "600519",
            "--snapshot-id",
            "a" * 64,
            "--corporate-action-snapshot-id",
            "c" * 64,
            "--strategy",
            "buy_and_hold",
            *required_rule_cli_args(),
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().err == (
        '{"error_code":"manifest_record_not_found",'
        '"error_type":"BacktestCliError","status":"error"}\n'
    )


def test_backtest_cli_reports_invalid_arguments_as_sanitized_json(capsys):
    from aquant.backtest_cli import main

    exit_code = main(["run", "--strategy", "must-not-echo"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == (
        '{"error_code":"invalid_arguments",'
        '"error_type":"BacktestCliError","status":"error"}\n'
    )
    assert "must-not-echo" not in captured.err


def test_backtest_cli_rejects_parent_path_escape(tmp_path, capsys):
    from aquant.backtest_cli import main

    exit_code = main(
        [
            "run",
            "--project-root",
            str(tmp_path),
            "--manifest",
            "../outside.jsonl",
            "--symbol",
            "600519",
            "--snapshot-id",
            "a" * 64,
            "--corporate-action-snapshot-id",
            "c" * 64,
            "--strategy",
            "buy_and_hold",
            *required_rule_cli_args(),
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().err == (
        '{"error_code":"unsafe_path",'
        '"error_type":"BacktestCliError","status":"error"}\n'
    )


def test_backtest_cli_rejects_absolute_path_outside_project(tmp_path, capsys):
    from aquant.backtest_cli import main

    project_root = tmp_path / "project"
    project_root.mkdir()
    exit_code = main(
        [
            "run",
            "--project-root",
            str(project_root),
            "--output",
            str(tmp_path / "outside"),
            "--symbol",
            "600519",
            "--snapshot-id",
            "a" * 64,
            "--corporate-action-snapshot-id",
            "c" * 64,
            "--strategy",
            "buy_and_hold",
            *required_rule_cli_args(),
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().err == (
        '{"error_code":"unsafe_path",'
        '"error_type":"BacktestCliError","status":"error"}\n'
    )


def test_backtest_cli_rejects_symlink_escape(tmp_path, capsys):
    from aquant.backtest_cli import main

    project_root = tmp_path / "project"
    external = tmp_path / "external"
    project_root.mkdir()
    external.mkdir()
    (project_root / "linked-output").symlink_to(external, target_is_directory=True)

    exit_code = main(
        [
            "run",
            "--project-root",
            str(project_root),
            "--output",
            "linked-output",
            "--symbol",
            "600519",
            "--snapshot-id",
            "a" * 64,
            "--corporate-action-snapshot-id",
            "c" * 64,
            "--strategy",
            "buy_and_hold",
            *required_rule_cli_args(),
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().err == (
        '{"error_code":"unsafe_path",'
        '"error_type":"BacktestCliError","status":"error"}\n'
    )
