from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from aquant.backtest import (
    BacktestConfig,
    StrategyName,
    export_backtest_result,
    run_synthetic_backtest,
)
from aquant.reporting.risk_report import (
    build_independent_batch_report,
    load_audited_run_metrics,
)
from aquant.research.week5 import build_week5_report, load_verified_run_series

PROJECT_ROOT = Path(__file__).parents[2]
UNIVERSE_ID = "a" * 64
CALENDAR_ID = "b" * 64


def _market_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2023-12-27",
                    "2023-12-28",
                    "2023-12-29",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-05",
                    "2024-01-08",
                ]
            ),
            "open": [10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4],
            "high": [10.5, 10.7, 10.9, 11.1, 11.3, 11.5, 11.7, 11.9],
            "low": [9.5, 9.7, 9.9, 10.1, 10.3, 10.5, 10.7, 10.9],
            "close": [10.1, 10.3, 10.5, 10.7, 10.9, 11.1, 11.3, 11.5],
            "volume": [10_000] * 8,
            "amount": [100_000.0] * 8,
        }
    )


def _result(
    *,
    symbol: str = "600519",
    strategy: StrategyName = StrategyName.BUY_AND_HOLD,
    sma_period: int | None = None,
    random_seed: int = 7,
):
    return run_synthetic_backtest(
        _market_frame(),
        symbol=symbol,
        config=BacktestConfig(
            strategy=strategy,
            initial_cash=10_000.0,
            target_weight=Decimal("0.95"),
            sma_period=sma_period,
            random_seed=random_seed,
        ),
    )


def _payloads(directory: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def test_run_identity_ignores_wall_clock_and_pid(monkeypatch, tmp_path):
    monkeypatch.setattr("time.time", lambda: 1.0)
    monkeypatch.setattr("os.getpid", lambda: 11)
    first = _result()
    first_directory = export_backtest_result(first, tmp_path / "first")

    monkeypatch.setattr("time.time", lambda: 9_999_999.0)
    monkeypatch.setattr("os.getpid", lambda: 99_999)
    second = _result()
    second_directory = export_backtest_result(second, tmp_path / "second")

    assert first.run_id == second.run_id
    assert _payloads(first_directory) == _payloads(second_directory)
    assert _result(random_seed=8).run_id != first.run_id


def test_run_identity_is_stable_across_python_hash_seeds():
    def probe(seed: str) -> bytes:
        return subprocess.check_output(
            [sys.executable, str(PROJECT_ROOT / "tests" / "identity_probe.py")],
            cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )

    assert probe("1") == probe("98765")


def test_risk_report_identity_ignores_input_order(tmp_path):
    audited = []
    for symbol in ("600519", "000001"):
        for strategy, period in (
            (StrategyName.BUY_AND_HOLD, None),
            (StrategyName.SMA, 2),
        ):
            directory = export_backtest_result(
                _result(symbol=symbol, strategy=strategy, sma_period=period),
                tmp_path / "runs",
            )
            audited.append(
                replace(
                    load_audited_run_metrics(directory),
                    universe_id=UNIVERSE_ID,
                )
            )

    forward = build_independent_batch_report(
        tuple(audited),
        expected_universe_id=UNIVERSE_ID,
        expected_symbols=("600519", "000001"),
    )
    reverse = build_independent_batch_report(
        tuple(reversed(audited)),
        expected_universe_id=UNIVERSE_ID,
        expected_symbols=("000001", "600519"),
    )

    assert forward.report_id == reverse.report_id
    assert forward.json_bytes == reverse.json_bytes


def test_week5_identity_ignores_dictionary_insertion_order(tmp_path):
    candidate_runs: dict[str, dict[int, object]] = {}
    baseline_runs: dict[str, dict[str, object]] = {}
    for symbol in ("600519", "000001"):
        candidates: dict[int, object] = {}
        for period in (2, 3, 4):
            directory = export_backtest_result(
                _result(
                    symbol=symbol,
                    strategy=StrategyName.SMA,
                    sma_period=period,
                ),
                tmp_path / "candidates",
            )
            candidates[period] = load_verified_run_series(directory)
        candidate_runs[symbol] = candidates
        baseline_runs[symbol] = {
            "buy_and_hold": load_verified_run_series(
                export_backtest_result(
                    _result(symbol=symbol),
                    tmp_path / "baselines",
                )
            ),
            "sma20": candidates[2],
        }

    kwargs = {
        "expected_universe_id": UNIVERSE_ID,
        "expected_symbols": ("600519", "000001"),
        "calendar_id": CALENDAR_ID,
        "calendar_dates": tuple(
            value.date() for value in _market_frame()["date"]
        ),
        "train_end": date(2023, 12, 29),
        "holdout_start": date(2024, 1, 2),
        "candidate_periods": (2, 3, 4),
        "replay_days": 2,
    }
    forward = build_week5_report(candidate_runs, baseline_runs, **kwargs)
    reversed_candidates = {
        symbol: dict(reversed(tuple(candidate_runs[symbol].items())))
        for symbol in reversed(tuple(candidate_runs))
    }
    reversed_baselines = {
        symbol: dict(reversed(tuple(baseline_runs[symbol].items())))
        for symbol in reversed(tuple(baseline_runs))
    }
    reverse = build_week5_report(
        reversed_candidates,
        reversed_baselines,
        **{**kwargs, "expected_symbols": ("000001", "600519")},
    )

    assert forward.experiment_id == reverse.experiment_id
    assert forward.json_bytes == reverse.json_bytes
    assert forward.replay_bytes == reverse.replay_bytes
