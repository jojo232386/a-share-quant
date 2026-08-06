from __future__ import annotations

import hashlib
import io
import json
import platform
import shutil
import socket
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime
from pathlib import Path

import backtrader as bt
import pandas as pd
import pytest

from aquant.backtest_cli import main as backtest_main
from aquant.data.calendar_snapshot import CalendarSnapshotStore
from aquant.data.corporate_actions import publish_corporate_actions
from aquant.data.manifest import ManifestRecord, ManifestWriter
from aquant.data.snapshot import RawSnapshotStore
from aquant.experiment_cli import main as experiment_main
from aquant.release_cli import main as release_cli_main
from aquant.release_network import ReleaseNetworkError
from aquant.release_replay import ProgressEvent, verify_release
from aquant.report_cli import main as report_main
from aquant.rules import InstrumentKind
from aquant.universe import (
    UniverseMember,
    canonical_universe_bytes,
)

SYMBOL = "600519"
HASH_REPLACEMENT = "f" * 64


def _invoke(main, arguments: list[str]) -> dict[str, object]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(arguments)
    assert exit_code == 0, stderr.getvalue()
    assert stderr.getvalue() == ""
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert type(value) is dict
    return value


def _market_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2023-07-03", "2024-01-15")
    closes = [10.0 + index * 0.01 for index in range(len(dates))]
    return pd.DataFrame(
        {
            "日期": [value.date().isoformat() for value in dates],
            "开盘": closes,
            "最高": [value + 0.2 for value in closes],
            "最低": [value - 0.2 for value in closes],
            "收盘": [value + 0.05 for value in closes],
            "成交量": [10_000 + index for index in range(len(dates))],
            "成交额": [
                (10_000 + index) * 100 * closes[index]
                for index in range(len(dates))
            ],
        }
    )


def _create_market_snapshot(project_root: Path) -> ManifestRecord:
    frame = _market_frame()
    artifact = RawSnapshotStore(project_root).write(
        frame,
        symbol=SYMBOL,
        source_slug="eastmoney",
        snapshot_date=date(2024, 1, 16),
    )
    record = ManifestRecord.create(
        schema_version="1.0",
        symbol=SYMBOL,
        instrument_kind="main_board_stock",
        provider="eastmoney",
        source_function="stock_zh_a_hist",
        source_schema="akshare.stock_zh_a_hist",
        endpoint_host="push2his.eastmoney.com",
        provider_symbol="sh600519",
        fetched_at_utc=datetime(2024, 1, 16, 8, 0, tzinfo=UTC),
        requested_start=date(2023, 7, 3),
        requested_end=date(2024, 1, 15),
        actual_start=date(2023, 7, 3),
        actual_end=date(2024, 1, 15),
        row_count=artifact.row_count,
        snapshot_relative_path=artifact.relative_path,
        file_sha256=artifact.sha256,
        adjustment="",
        factor_source=None,
        latest_market_date=date(2024, 1, 15),
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
    ManifestWriter(project_root / "data/manifests/manifest.jsonl").append(record)
    return record


def _backtest_arguments(
    project_root: Path,
    *,
    snapshot_id: str,
    action_snapshot_id: str,
    calendar_id: str,
    universe_id: str,
    strategy: str,
    period: int | None,
    output: str,
) -> list[str]:
    arguments = [
        "run",
        "--project-root",
        str(project_root),
        "--output",
        output,
        "--symbol",
        SYMBOL,
        "--snapshot-id",
        snapshot_id,
        "--corporate-action-snapshot-id",
        action_snapshot_id,
        "--calendar-id",
        calendar_id,
        "--universe-id",
        universe_id,
        "--strategy",
        strategy,
        "--initial-cash",
        "1000000",
        "--target-weight",
        "0.95",
        "--random-seed",
        "0",
        "--stock-commission-rate",
        "0.00025",
        "--stock-minimum-commission",
        "5.00",
        "--etf-commission-rate",
        "0.00025",
        "--etf-minimum-commission",
        "5.00",
    ]
    if period is not None:
        arguments.extend(("--sma-period", str(period)))
    return arguments


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_release_manifest(
    release_root: Path,
    *,
    universe_id: str,
    calendar_id: str,
    snapshot_id: str,
    action_snapshot_id: str,
    input_files: dict[str, str],
    baseline_ids: dict[str, str],
    candidate_ids: dict[str, str],
    report_id: str,
    experiment_id: str,
) -> None:
    payload = {
        "schema_version": "1.0",
        "release_name": "v0.1-research",
        "implementation_commit": "1" * 40,
        "python_version": platform.python_version(),
        "akshare_version": "1.18.64",
        "backtrader_version": bt.__version__,
        "universe_id": universe_id,
        "calendar_id": calendar_id,
        "market_snapshots": {SYMBOL: snapshot_id},
        "corporate_action_snapshots": {SYMBOL: action_snapshot_id},
        "input_files": input_files,
        "baseline_run_ids": baseline_ids,
        "candidate_run_ids": candidate_ids,
        "risk_report_id": report_id,
        "week5_experiment_id": experiment_id,
        "expected_counts": {
            "symbols": 1,
            "baseline_runs": 2,
            "candidate_runs": 3,
            "replay_rows": 10,
        },
        "research_boundary": {
            "live_trading": False,
            "profit_claim": False,
            "research_only": True,
            "simulation_only": True,
        },
    }
    (release_root / "release_manifest.json").write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _build_mini_release(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "source"
    source_root.mkdir()
    market = _create_market_snapshot(source_root)
    dates = tuple(value.date() for value in pd.bdate_range("2023-07-03", "2024-01-15"))
    calendar = CalendarSnapshotStore(source_root).write(
        dates,
        source_provider="synthetic",
        source_function="pytest_calendar",
        source_version="1",
        fetched_at_utc=datetime(2024, 1, 16, tzinfo=UTC),
    )
    actions = publish_corporate_actions(
        source_root,
        (),
        symbol=SYMBOL,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        provider="synthetic",
        source_schema="synthetic.cash.v1",
        normalization_version="cash-only-v1",
        coverage_start=dates[0],
        coverage_end=dates[-1],
    )
    universe_content = canonical_universe_bytes(
        "pytest-release",
        (UniverseMember(SYMBOL, "main_board_stock"),),
    )
    universe_id = hashlib.sha256(universe_content).hexdigest()
    universe_path = source_root / "configs" / "universes" / f"{universe_id}.json"
    universe_path.parent.mkdir(parents=True)
    universe_path.write_bytes(universe_content)

    baseline_ids: dict[str, str] = {}
    for strategy, period, label in (
        ("buy_and_hold", None, "buy_and_hold"),
        ("sma", 20, "sma20"),
    ):
        result = _invoke(
            backtest_main,
            _backtest_arguments(
                source_root,
                snapshot_id=market.snapshot_id,
                action_snapshot_id=actions.snapshot_id,
                calendar_id=calendar.calendar_id,
                universe_id=universe_id,
                strategy=strategy,
                period=period,
                output="outputs/backtests",
            ),
        )
        baseline_ids[f"{SYMBOL}|{label}"] = result["run_id"]

    candidate_ids: dict[str, str] = {}
    for period in (10, 20, 60):
        result = _invoke(
            backtest_main,
            _backtest_arguments(
                source_root,
                snapshot_id=market.snapshot_id,
                action_snapshot_id=actions.snapshot_id,
                calendar_id=calendar.calendar_id,
                universe_id=universe_id,
                strategy="sma",
                period=period,
                output="outputs/experiments/week5/candidates",
            ),
        )
        candidate_ids[f"{SYMBOL}|sma{period}"] = result["run_id"]

    risk = _invoke(
        report_main,
        [
            "build",
            "--project-root",
            str(source_root),
            "--universe-id",
            universe_id,
        ],
    )
    experiment = _invoke(
        experiment_main,
        [
            "run",
            "--project-root",
            str(source_root),
            "--universe-id",
            universe_id,
            "--calendar-id",
            calendar.calendar_id,
            "--train-end",
            "2023-12-29",
            "--holdout-start",
            "2024-01-02",
            "--periods",
            "10,20,60",
            "--replay-days",
            "10",
        ],
    )

    release_root = tmp_path / "release"
    input_paths = (
        universe_path.relative_to(source_root),
        Path("data/manifests/manifest.jsonl"),
        market.snapshot_relative_path,
        Path("data/calendars/manifest.jsonl"),
        calendar.relative_path,
        Path("data/corporate_actions/manifest.jsonl"),
        actions.snapshot_relative_path,
    )
    input_hashes: dict[str, str] = {}
    for relative in input_paths:
        source = source_root / relative
        destination = release_root / "inputs" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        input_hashes[relative.as_posix()] = _sha256(destination)

    _write_release_manifest(
        release_root,
        universe_id=universe_id,
        calendar_id=calendar.calendar_id,
        snapshot_id=market.snapshot_id,
        action_snapshot_id=actions.snapshot_id,
        input_files=input_hashes,
        baseline_ids=baseline_ids,
        candidate_ids=candidate_ids,
        report_id=risk["report_id"],
        experiment_id=experiment["experiment_id"],
    )
    return source_root, release_root


def test_rebuilds_in_isolated_root_and_ignores_existing_data_and_outputs(tmp_path):
    _source_root, release_root = _build_mini_release(tmp_path)
    caller_root = tmp_path / "caller"
    conflict = caller_root / "data" / "raw" / "conflict.parquet"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"must remain untouched")
    old_output = caller_root / "outputs" / "backtests" / HASH_REPLACEMENT
    old_output.mkdir(parents=True)
    (old_output / "run.json").write_text('{"conflict":true}\n', encoding="utf-8")
    progress: list[ProgressEvent] = []

    summary = verify_release(
        project_root=caller_root,
        release_root=release_root,
        progress=progress.append,
    )

    assert summary.release_name == "v0.1-research"
    assert summary.baseline_run_count == 2
    assert summary.candidate_run_count == 3
    assert summary.replay_row_count == 10
    assert conflict.read_bytes() == b"must remain untouched"
    assert (old_output / "run.json").read_text(encoding="utf-8") == (
        '{"conflict":true}\n'
    )
    assert [event.stage for event in progress] == [
        "inputs_verified",
        "baselines_rebuilt",
        "risk_report_rebuilt",
        "experiment_rebuilt",
        "identities_verified",
    ]


def test_rejects_changed_expected_identity_after_rebuilding(tmp_path):
    _source_root, release_root = _build_mini_release(tmp_path)
    path = release_root / "release_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["risk_report_id"] = HASH_REPLACEMENT
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    from aquant.release_manifest import ReleaseVerificationError

    try:
        verify_release(
            project_root=tmp_path / "caller",
            release_root=release_root,
            progress=lambda _event: None,
        )
    except ReleaseVerificationError as error:
        assert error.code == "risk_report_identity_mismatch"
    else:
        raise AssertionError("changed report identity was accepted")


def test_rejects_declared_but_unreachable_frozen_input(tmp_path):
    _source_root, release_root = _build_mini_release(tmp_path)
    extra = release_root / "inputs" / "data" / "raw" / "unused.parquet"
    extra.write_bytes(b"declared but unreachable")
    path = release_root / "release_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["input_files"]["data/raw/unused.parquet"] = _sha256(extra)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    from aquant.release_manifest import ReleaseVerificationError

    with pytest.raises(ReleaseVerificationError) as error:
        verify_release(
            project_root=tmp_path / "caller",
            release_root=release_root,
            progress=lambda _event: None,
        )

    assert error.value.code == "input_read_closure_mismatch"


def test_verifier_blocks_network_attempt_inside_public_entry(
    tmp_path,
    monkeypatch,
):
    _source_root, release_root = _build_mini_release(tmp_path)

    def network_attempt(_arguments=None):
        socket.create_connection(("127.0.0.1", 9))
        return 0

    monkeypatch.setattr(
        "aquant.release_replay.backtest_main",
        network_attempt,
    )

    with pytest.raises(ReleaseNetworkError) as error:
        verify_release(
            project_root=tmp_path / "caller",
            release_root=release_root,
            progress=lambda _event: None,
        )

    assert error.value.code == "network_access_forbidden"


def test_release_cli_emits_one_success_and_sanitized_progress_lines(
    tmp_path,
    capsys,
):
    _source_root, release_fixture = _build_mini_release(tmp_path)
    project_root = tmp_path / "project"
    target = project_root / "release" / "v0.1-research"
    target.parent.mkdir(parents=True)
    shutil.copytree(release_fixture, target)

    exit_code = release_cli_main(
        ["verify", "--project-root", str(project_root)]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    output_lines = captured.out.splitlines()
    assert len(output_lines) == 1
    payload = json.loads(output_lines[0])
    assert payload["status"] == "verified"
    assert payload["release_name"] == "v0.1-research"
    assert payload["baseline_run_count"] == 2
    assert payload["candidate_run_count"] == 3
    assert payload["replay_row_count"] == 10
    assert type(payload["elapsed_seconds"]) is float
    assert payload["elapsed_seconds"] >= 0

    progress = [json.loads(line) for line in captured.err.splitlines()]
    assert [item["stage"] for item in progress] == [
        "inputs_verified",
        "baselines_rebuilt",
        "risk_report_rebuilt",
        "experiment_rebuilt",
        "identities_verified",
    ]
    assert all(set(item) == {"completed", "stage", "total"} for item in progress)
    assert all("path" not in line and str(tmp_path) not in line for line in progress)


def test_release_cli_failure_is_one_sanitized_json_line(tmp_path, capsys):
    project_root = tmp_path / "project"
    project_root.mkdir()

    exit_code = release_cli_main(
        ["verify", "--project-root", str(project_root)]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "error_code": "manifest_unreadable",
        "error_type": "ReleaseVerificationError",
        "status": "error",
    }
    assert str(tmp_path) not in captured.err


def test_release_cli_invalid_arguments_do_not_echo_raw_values(capsys):
    exit_code = release_cli_main(
        ["verify", "--project-root", "must-not-echo", "--unexpected"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "must-not-echo" not in captured.err
    assert json.loads(captured.err) == {
        "error_code": "invalid_arguments",
        "error_type": "ReleaseCliError",
        "status": "error",
    }
