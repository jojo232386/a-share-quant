"""Offline, isolated reconstruction of one frozen research release."""

from __future__ import annotations

import hashlib
import io
import json
import platform
import shutil
import stat
import tempfile
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import backtrader as bt

from aquant.backtest_cli import main as backtest_main
from aquant.data.calendar_snapshot import CalendarSnapshotStore
from aquant.data.corporate_actions import read_corporate_action_manifest
from aquant.data.manifest import ManifestWriter
from aquant.experiment_cli import main as experiment_main
from aquant.release_manifest import (
    ReleaseManifest,
    ReleaseVerificationError,
    load_release_manifest,
    verify_release_inputs,
)
from aquant.release_network import offline_network_guard
from aquant.report_cli import main as report_main
from aquant.reporting.risk_report import (
    load_audited_run_metrics,
    verify_published_risk_report,
)

_STOCK_COMMISSION_RATE = "0.00025"
_STOCK_MINIMUM_COMMISSION = "5.00"
_ETF_COMMISSION_RATE = "0.00025"
_ETF_MINIMUM_COMMISSION = "5.00"
_TRAIN_END = "2023-12-29"
_HOLDOUT_START = "2024-01-02"


@dataclass(frozen=True)
class ProgressEvent:
    """One sanitized stage marker for a long release reconstruction."""

    stage: str
    completed: int
    total: int = 5


@dataclass(frozen=True)
class ReleaseSummary:
    """Identity-bearing result of a successful reconstruction."""

    release_name: str
    baseline_run_count: int
    candidate_run_count: int
    replay_row_count: int
    risk_report_id: str
    week5_experiment_id: str


def _mapping(entries: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return dict(entries)


def _check_runtime_versions(manifest: ReleaseManifest) -> None:
    if (
        platform.python_version() != manifest.python_version
        or version("akshare") != manifest.akshare_version
        or bt.__version__ != manifest.backtrader_version
    ):
        raise ReleaseVerificationError("runtime_version_mismatch")


def _copy_inputs(
    manifest: ReleaseManifest,
    release_root: Path,
    temporary_root: Path,
) -> None:
    for relative_path, _digest in manifest.input_files:
        source = release_root / "inputs" / relative_path
        destination = temporary_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise ReleaseVerificationError("temporary_input_conflict")
        try:
            shutil.copyfile(source, destination)
        except OSError as exc:
            raise ReleaseVerificationError("temporary_input_copy_failed") from exc


def _resolve_input_closure(
    manifest: ReleaseManifest,
    temporary_root: Path,
) -> None:
    market_ids = _mapping(manifest.market_snapshots)
    action_ids = _mapping(manifest.corporate_action_snapshots)
    expected_paths = {
        f"configs/universes/{manifest.universe_id}.json",
        "data/manifests/manifest.jsonl",
        "data/calendars/manifest.jsonl",
        "data/corporate_actions/manifest.jsonl",
    }

    try:
        market_records = ManifestWriter(
            temporary_root / "data/manifests/manifest.jsonl"
        ).read_all()
        calendar_records = CalendarSnapshotStore(temporary_root).read_manifest()
        action_records = read_corporate_action_manifest(temporary_root)
    except Exception as exc:
        raise ReleaseVerificationError("input_manifest_unreadable") from exc

    for symbol in manifest.symbols:
        market_matches = tuple(
            record
            for record in market_records
            if record.symbol == symbol
            and record.snapshot_id == market_ids[symbol]
        )
        action_matches = tuple(
            record
            for record in action_records
            if record.symbol == symbol
            and record.snapshot_id == action_ids[symbol]
        )
        if len(market_matches) != 1 or len(action_matches) != 1:
            raise ReleaseVerificationError("input_identity_not_found")
        expected_paths.add(
            market_matches[0].snapshot_relative_path.as_posix()
        )
        expected_paths.add(
            action_matches[0].snapshot_relative_path.as_posix()
        )

    calendar_matches = tuple(
        record
        for record in calendar_records
        if record.calendar_id == manifest.calendar_id
    )
    if len(calendar_matches) != 1:
        raise ReleaseVerificationError("input_identity_not_found")
    expected_paths.add(calendar_matches[0].relative_path.as_posix())

    if expected_paths != set(_mapping(manifest.input_files)):
        raise ReleaseVerificationError("input_read_closure_mismatch")


def _invoke_cli(
    main: Callable[[Sequence[str] | None], int],
    arguments: list[str],
) -> dict[str, object]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(arguments)
    stdout_lines = stdout.getvalue().splitlines()
    if exit_code != 0 or stderr.getvalue() or len(stdout_lines) != 1:
        raise ReleaseVerificationError("replay_entry_failed")
    try:
        payload = json.loads(stdout_lines[0])
    except json.JSONDecodeError as exc:
        raise ReleaseVerificationError("replay_output_invalid") from exc
    if type(payload) is not dict or payload.get("status") not in {
        "ok",
        "verified",
    }:
        raise ReleaseVerificationError("replay_output_invalid")
    return payload


def _backtest_arguments(
    temporary_root: Path,
    *,
    manifest: ReleaseManifest,
    symbol: str,
    strategy: str,
    period: int | None,
    output: str,
) -> list[str]:
    market_ids = _mapping(manifest.market_snapshots)
    action_ids = _mapping(manifest.corporate_action_snapshots)
    arguments = [
        "run",
        "--project-root",
        str(temporary_root),
        "--output",
        output,
        "--symbol",
        symbol,
        "--snapshot-id",
        market_ids[symbol],
        "--corporate-action-snapshot-id",
        action_ids[symbol],
        "--calendar-id",
        manifest.calendar_id,
        "--universe-id",
        manifest.universe_id,
        "--strategy",
        strategy,
        "--initial-cash",
        "1000000",
        "--target-weight",
        "0.95",
        "--random-seed",
        "0",
        "--stock-commission-rate",
        _STOCK_COMMISSION_RATE,
        "--stock-minimum-commission",
        _STOCK_MINIMUM_COMMISSION,
        "--etf-commission-rate",
        _ETF_COMMISSION_RATE,
        "--etf-minimum-commission",
        _ETF_MINIMUM_COMMISSION,
    ]
    if period is not None:
        arguments.extend(("--sma-period", str(period)))
    return arguments


def _verify_week5_bundle(directory: Path, experiment_id: str) -> int:
    try:
        metadata = directory.lstat()
        entries = {path.name: path for path in directory.iterdir()}
    except OSError as exc:
        raise ReleaseVerificationError("experiment_bundle_invalid") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or directory.is_symlink()
        or set(entries)
        != {
            "artifact_manifest.json",
            "experiment.json",
            "replay.json",
            "report.md",
        }
        or any(
            not path.is_file()
            or path.is_symlink()
            or path.lstat().st_nlink != 1
            for path in entries.values()
        )
    ):
        raise ReleaseVerificationError("experiment_bundle_invalid")
    try:
        artifact = json.loads(
            entries["artifact_manifest.json"].read_text(encoding="utf-8")
        )
        experiment = json.loads(
            entries["experiment.json"].read_text(encoding="utf-8")
        )
        replay = json.loads(entries["replay.json"].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError("experiment_bundle_invalid") from exc
    expected_files = {"experiment.json", "replay.json", "report.md"}
    if (
        type(artifact) is not dict
        or artifact.get("schema_version") != "1.0"
        or artifact.get("status") != "complete"
        or artifact.get("experiment_id") != experiment_id
        or type(artifact.get("files")) is not dict
        or set(artifact["files"]) != expected_files
        or type(experiment) is not dict
        or experiment.get("experiment_id") != experiment_id
        or type(replay) is not dict
        or type(replay.get("rows")) is not list
    ):
        raise ReleaseVerificationError("experiment_bundle_invalid")
    for name in expected_files:
        expected_hash = artifact["files"].get(name)
        actual_hash = hashlib.sha256(entries[name].read_bytes()).hexdigest()
        if expected_hash != actual_hash:
            raise ReleaseVerificationError("experiment_bundle_invalid")
    return len(replay["rows"])


def _rebuild_release(
    manifest: ReleaseManifest,
    temporary_root: Path,
    progress: Callable[[ProgressEvent], None],
) -> ReleaseSummary:
    expected_baselines = _mapping(manifest.baseline_run_ids)
    expected_candidates = _mapping(manifest.candidate_run_ids)
    actual_baselines: dict[str, str] = {}
    actual_candidates: dict[str, str] = {}

    for symbol in manifest.symbols:
        for strategy, period, label in (
            ("buy_and_hold", None, "buy_and_hold"),
            ("sma", 20, "sma20"),
        ):
            result = _invoke_cli(
                backtest_main,
                _backtest_arguments(
                    temporary_root,
                    manifest=manifest,
                    symbol=symbol,
                    strategy=strategy,
                    period=period,
                    output="outputs/backtests",
                ),
            )
            run_id = result.get("run_id")
            if type(run_id) is not str:
                raise ReleaseVerificationError("replay_output_invalid")
            actual_baselines[f"{symbol}|{label}"] = run_id
            load_audited_run_metrics(
                temporary_root / "outputs" / "backtests" / run_id
            )
    if actual_baselines != expected_baselines:
        raise ReleaseVerificationError("baseline_identity_mismatch")
    progress(ProgressEvent("baselines_rebuilt", 2))

    risk = _invoke_cli(
        report_main,
        [
            "build",
            "--project-root",
            str(temporary_root),
            "--universe-id",
            manifest.universe_id,
        ],
    )
    report_id = risk.get("report_id")
    if type(report_id) is not str:
        raise ReleaseVerificationError("replay_output_invalid")
    verification = verify_published_risk_report(
        temporary_root / "outputs" / "reports" / report_id,
        temporary_root / "outputs" / "backtests",
    )
    if (
        report_id != manifest.risk_report_id
        or verification.report_id != manifest.risk_report_id
    ):
        raise ReleaseVerificationError("risk_report_identity_mismatch")
    progress(ProgressEvent("risk_report_rebuilt", 3))

    for symbol in manifest.symbols:
        for period in (10, 20, 60):
            result = _invoke_cli(
                backtest_main,
                _backtest_arguments(
                    temporary_root,
                    manifest=manifest,
                    symbol=symbol,
                    strategy="sma",
                    period=period,
                    output="outputs/experiments/week5/candidates",
                ),
            )
            run_id = result.get("run_id")
            if type(run_id) is not str:
                raise ReleaseVerificationError("replay_output_invalid")
            actual_candidates[f"{symbol}|sma{period}"] = run_id
            load_audited_run_metrics(
                temporary_root
                / "outputs"
                / "experiments"
                / "week5"
                / "candidates"
                / run_id
            )
    if actual_candidates != expected_candidates:
        raise ReleaseVerificationError("candidate_identity_mismatch")

    experiment = _invoke_cli(
        experiment_main,
        [
            "run",
            "--project-root",
            str(temporary_root),
            "--universe-id",
            manifest.universe_id,
            "--calendar-id",
            manifest.calendar_id,
            "--train-end",
            _TRAIN_END,
            "--holdout-start",
            _HOLDOUT_START,
            "--periods",
            "10,20,60",
            "--replay-days",
            "10",
        ],
    )
    experiment_id = experiment.get("experiment_id")
    if type(experiment_id) is not str:
        raise ReleaseVerificationError("replay_output_invalid")
    replay_count = _verify_week5_bundle(
        temporary_root / "outputs" / "experiments" / "week5" / experiment_id,
        experiment_id,
    )
    if experiment_id != manifest.week5_experiment_id:
        raise ReleaseVerificationError("experiment_identity_mismatch")
    progress(ProgressEvent("experiment_rebuilt", 4))

    if (
        len(actual_baselines) != manifest.expected_counts.baseline_runs
        or len(actual_candidates) != manifest.expected_counts.candidate_runs
        or replay_count != manifest.expected_counts.replay_rows
    ):
        raise ReleaseVerificationError("release_count_mismatch")
    progress(ProgressEvent("identities_verified", 5))
    return ReleaseSummary(
        release_name=manifest.release_name,
        baseline_run_count=len(actual_baselines),
        candidate_run_count=len(actual_candidates),
        replay_row_count=replay_count,
        risk_report_id=report_id,
        week5_experiment_id=experiment_id,
    )


def verify_release(
    *,
    project_root: Path,
    release_root: Path,
    progress: Callable[[ProgressEvent], None],
) -> ReleaseSummary:
    """Rebuild a frozen release without reading caller data or outputs."""
    if (
        not isinstance(project_root, Path)
        or not isinstance(release_root, Path)
        or not callable(progress)
    ):
        raise TypeError("release verification arguments are invalid")
    manifest = load_release_manifest(release_root / "release_manifest.json")
    _check_runtime_versions(manifest)
    verify_release_inputs(manifest, release_root)
    with tempfile.TemporaryDirectory(prefix="aquant-v01-") as temporary:
        temporary_root = Path(temporary)
        _copy_inputs(manifest, release_root, temporary_root)
        _resolve_input_closure(manifest, temporary_root)
        progress(ProgressEvent("inputs_verified", 1))
        with offline_network_guard():
            return _rebuild_release(manifest, temporary_root, progress)
