from dataclasses import replace
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from aquant.backtest import (
    BacktestConfig,
    StrategyName,
    export_backtest_result,
    run_synthetic_backtest,
)
from aquant.backtest.models import EquityRecord


def _run_directory(tmp_path):
    dates = pd.to_datetime(
        ["2023-12-28", "2023-12-29", "2024-01-02", "2024-01-03"]
    )
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0, 10.5, 11.0, 11.5],
            "high": [10.5, 11.0, 11.5, 12.0],
            "low": [9.5, 10.0, 10.5, 11.0],
            "close": [10.0, 10.5, 11.0, 11.5],
            "volume": [10000] * 4,
            "amount": [100000.0] * 4,
        }
    )
    result = run_synthetic_backtest(
        frame,
        config=BacktestConfig(
            strategy=StrategyName.SMA,
            initial_cash=10000.0,
            target_weight=Decimal("0.95"),
            sma_period=2,
        ),
    )
    return export_backtest_result(result, tmp_path)


def test_split_run_uses_training_and_holdout_boundaries(tmp_path):
    from aquant.research.week5 import load_verified_run_series, split_series

    series = load_verified_run_series(_run_directory(tmp_path))

    split = split_series(
        series,
        train_end=date(2023, 12, 29),
        holdout_start=date(2024, 1, 2),
    )

    assert split.training[-1].date == date(2023, 12, 29)
    assert split.holdout[0].date == date(2024, 1, 2)
    assert all(item.date <= date(2023, 12, 29) for item in split.training)
    assert all(item.date >= date(2024, 1, 2) for item in split.holdout)


def test_selection_score_does_not_read_holdout(tmp_path):
    from aquant.research.week5 import load_verified_run_series, select_training_period

    source = load_verified_run_series(_run_directory(tmp_path))
    dates = tuple(item.date for item in source.equity)
    candidates = {
        10: replace(
            source,
            equity=tuple(
                EquityRecord(date=value, equity=amount)
                for value, amount in zip(
                    dates,
                    (10000.0, 11000.0, 11000.0, 11000.0),
                    strict=True,
                )
            ),
        ),
        20: replace(
            source,
            equity=tuple(
                EquityRecord(date=value, equity=amount)
                for value, amount in zip(
                    dates,
                    (10000.0, 10050.0, 10050.0, 20000.0),
                    strict=True,
                )
            ),
        ),
        60: replace(
            source,
            equity=tuple(
                EquityRecord(date=value, equity=amount)
                for value, amount in zip(
                    dates,
                    (10000.0, 10200.0, 10200.0, 10200.0),
                    strict=True,
                )
            ),
        ),
    }

    selected = select_training_period(
        candidates,
        train_end=date(2023, 12, 29),
        holdout_start=date(2024, 1, 2),
    )

    assert selected.period == 10
    assert selected.selection_basis == (
        "training_calmar_then_return_then_smaller_period"
    )


def test_replay_has_ten_calendar_rows_and_machine_readable_orders(tmp_path):
    from aquant.research.week5 import build_week5_replay, load_verified_run_series

    series = load_verified_run_series(_run_directory(tmp_path))
    calendar_dates = tuple(
        date.fromordinal(date(2024, 1, 2).toordinal() + offset)
        for offset in range(14)
        if date.fromordinal(date(2024, 1, 2).toordinal() + offset).weekday() < 5
    )

    rows = build_week5_replay(
        series,
        calendar_dates=calendar_dates,
        replay_start=date(2024, 1, 2),
        replay_days=10,
    )

    assert len(rows) == 10
    assert {"date", "data_available", "orders", "fills"} <= rows[0].keys()
    assert rows[0]["data_available"] is True
    assert isinstance(rows[0]["orders"], list)
    assert isinstance(rows[0]["fills"], list)


def test_week5_report_publishes_training_and_holdout_evidence(tmp_path):
    from aquant.research.week5 import (
        build_week5_report,
        load_verified_run_series,
        publish_week5_report,
    )

    series = load_verified_run_series(_run_directory(tmp_path))
    calendar_dates = tuple(
        date.fromordinal(date(2024, 1, 2).toordinal() + offset)
        for offset in range(14)
        if date.fromordinal(date(2024, 1, 2).toordinal() + offset).weekday() < 5
    )
    report = build_week5_report(
        {"600519": {10: series, 20: series, 60: series}},
        {"600519": {"buy_and_hold": series, "sma20": series}},
        expected_universe_id="a" * 64,
        expected_symbols=("600519",),
        calendar_id="b" * 64,
        calendar_dates=calendar_dates,
        train_end=date(2023, 12, 29),
        holdout_start=date(2024, 1, 2),
    )

    directory = publish_week5_report(report, tmp_path / "experiments")

    assert len(report.experiment_id) == 64
    assert "不证明 Alpha" in report.markdown
    assert (directory / "replay.json").is_file()
    assert (directory / "artifact_manifest.json").is_file()


def test_week5_identity_binds_calendar_and_implementation(tmp_path):
    from aquant.research.week5 import build_week5_report, load_verified_run_series

    series = load_verified_run_series(_run_directory(tmp_path))
    calendar_dates = tuple(
        date.fromordinal(date(2024, 1, 2).toordinal() + offset)
        for offset in range(14)
        if date.fromordinal(date(2024, 1, 2).toordinal() + offset).weekday() < 5
    )
    kwargs = {
        "candidate_runs": {"600519": {10: series, 20: series, 60: series}},
        "baseline_runs": {"600519": {"buy_and_hold": series, "sma20": series}},
        "expected_universe_id": "a" * 64,
        "expected_symbols": ("600519",),
        "calendar_dates": calendar_dates,
        "train_end": date(2023, 12, 29),
        "holdout_start": date(2024, 1, 2),
    }
    first = build_week5_report(calendar_id="b" * 64, **kwargs)
    second = build_week5_report(calendar_id="c" * 64, **kwargs)

    assert first.experiment_id != second.experiment_id


def test_report_rejects_mixed_implementation_digests(tmp_path):
    from aquant.research.week5 import Week5Error, build_week5_report, load_verified_run_series

    series = load_verified_run_series(_run_directory(tmp_path))
    mixed = replace(series, implementation_digest="f" * 64)
    calendar_dates = tuple(
        date.fromordinal(date(2024, 1, 2).toordinal() + offset)
        for offset in range(14)
        if date.fromordinal(date(2024, 1, 2).toordinal() + offset).weekday() < 5
    )

    with pytest.raises(Week5Error, match="mixes implementation fingerprints"):
        build_week5_report(
            {"600519": {10: series, 20: series, 60: mixed}},
            {"600519": {"buy_and_hold": series, "sma20": series}},
            expected_universe_id="a" * 64,
            expected_symbols=("600519",),
            calendar_id="b" * 64,
            calendar_dates=calendar_dates,
            train_end=date(2023, 12, 29),
            holdout_start=date(2024, 1, 2),
        )


def test_split_rejects_unsorted_run_series(tmp_path):
    from aquant.research.week5 import Week5Error, load_verified_run_series, split_series

    source = load_verified_run_series(_run_directory(tmp_path))
    dates = (
        date(2023, 12, 27),
        date(2023, 12, 29),
        date(2023, 12, 28),
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
    )
    malformed = replace(
        source,
        equity=tuple(
            EquityRecord(date=item, equity=10000.0 + index)
            for index, item in enumerate(dates)
        ),
        positions=tuple(
            replace(source.positions[0], date=item) for item in dates
        ),
    )

    with pytest.raises(Week5Error, match="strictly increasing"):
        split_series(
            malformed,
            train_end=date(2023, 12, 29),
            holdout_start=date(2024, 1, 2),
        )


def test_report_rejects_selection_without_training_evidence(tmp_path, monkeypatch):
    from aquant.research import week5

    series = week5.load_verified_run_series(_run_directory(tmp_path))
    calendar_dates = tuple(
        date.fromordinal(date(2024, 1, 2).toordinal() + offset)
        for offset in range(14)
        if date.fromordinal(date(2024, 1, 2).toordinal() + offset).weekday() < 5
    )

    def empty_selection(*_args, **_kwargs):
        return week5.TrainingSelection(
            period=10,
            selection_basis="training_calmar_then_return_then_smaller_period",
            training_metrics=(),
        )

    monkeypatch.setattr(week5, "select_training_period", empty_selection)
    with pytest.raises(week5.Week5Error, match="selected period has no training evidence"):
        week5.build_week5_report(
            {"600519": {10: series, 20: series, 60: series}},
            {"600519": {"buy_and_hold": series, "sma20": series}},
            expected_universe_id="a" * 64,
            expected_symbols=("600519",),
            calendar_id="b" * 64,
            calendar_dates=calendar_dates,
            train_end=date(2023, 12, 29),
            holdout_start=date(2024, 1, 2),
        )


def test_experiment_cli_rejects_candidate_path_escape(tmp_path, capsys):
    from aquant.experiment_cli import main

    exit_code = main(
        [
            "run",
            "--project-root",
            str(tmp_path),
            "--universe-id",
            "a" * 64,
            "--calendar-id",
            "b" * 64,
            "--candidate-root",
            "../outside",
            "--train-end",
            "2023-12-29",
            "--holdout-start",
            "2024-01-02",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert '"error_code":"unsafe_path"' in captured.err
    assert captured.out == ""
