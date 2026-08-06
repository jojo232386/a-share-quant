"""Command-line entry point for the restricted Week 5 experiment."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from aquant.data.calendar_snapshot import CalendarSnapshotStore, load_verified_calendar
from aquant.research.week5 import (
    Week5Error,
    build_week5_report,
    load_verified_run_series,
    publish_week5_report,
)
from aquant.universe import load_verified_universe

_HASH_RE = re.compile(r"[0-9a-f]{64}")


class ExperimentCliError(RuntimeError):
    """Sanitized experiment command error."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ExperimentCliError("invalid_arguments", "experiment arguments are invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="aquant-experiment")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SafeArgumentParser,
    )
    run = subparsers.add_parser("run", help="run the restricted Week 5 experiment")
    run.add_argument("--project-root", default=".")
    run.add_argument("--universe-id", required=True)
    run.add_argument("--calendar-id", required=True)
    run.add_argument("--candidate-root", default="outputs/experiments/week5/candidates")
    run.add_argument("--baseline-root", default="outputs/backtests")
    run.add_argument("--output", default="outputs/experiments/week5")
    run.add_argument("--train-end", required=True)
    run.add_argument("--holdout-start", required=True)
    run.add_argument("--periods", default="10,20,60")
    run.add_argument("--replay-days", type=int, default=10)
    return parser


def _path_beneath(root: Path, value: str, *, label: str) -> Path:
    candidate = Path(value)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ExperimentCliError("unsafe_path", f"{label} must stay beneath project root") from exc
    return path


def _write_json(stream, payload: dict[str, object]) -> None:
    stream.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _parse_periods(value: str) -> tuple[int, ...]:
    try:
        periods = tuple(sorted({int(item) for item in value.split(",")}))
    except ValueError as exc:
        raise ExperimentCliError(
            "invalid_arguments",
            "periods must be comma-separated integers",
        ) from exc
    if not periods or any(period < 2 for period in periods):
        raise ExperimentCliError("invalid_arguments", "periods must be integers of at least two")
    return periods


def _run_metadata(path: Path) -> dict[str, object]:
    try:
        metadata = json.loads((path / "run.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExperimentCliError("invalid_run_bundle", "run metadata cannot be read") from exc
    if type(metadata) is not dict or metadata.get("run_id") != path.name:
        raise ExperimentCliError("invalid_run_bundle", "run metadata identity is invalid")
    return metadata


def _index_runs(root: Path) -> tuple[tuple[Path, dict[str, object]], ...]:
    if root.is_symlink() or not root.is_dir():
        raise ExperimentCliError("run_root_not_found", "run root must be a safe directory")
    indexed: list[tuple[Path, dict[str, object]]] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.is_symlink() or _HASH_RE.fullmatch(path.name) is None:
            continue
        if (path / "run.json").is_file():
            indexed.append((path, _run_metadata(path)))
    return tuple(indexed)


def _find_one(
    indexed: tuple[tuple[Path, dict[str, object]], ...],
    *,
    symbol: str,
    strategy: str,
    sma_period: int | None,
    universe_id: str,
) -> Path:
    matches = tuple(
        path
        for path, metadata in indexed
        if metadata.get("provenance", {}).get("symbol") == symbol
        and metadata.get("config", {}).get("strategy") == strategy
        and metadata.get("config", {}).get("sma_period") == sma_period
        and metadata.get("universe_id") == universe_id
    )
    if len(matches) != 1:
        raise ExperimentCliError(
            "run_bundle_contract",
            "each requested run must exist exactly once",
        )
    return matches[0]


def main(argv: Sequence[str] | None = None) -> int:
    """Build one restricted experiment and return a process exit code."""
    try:
        args = _parser().parse_args(argv)
        if args.command != "run":
            raise ExperimentCliError("invalid_arguments", "unsupported experiment command")
        project_root = Path(args.project_root).resolve()
        if (
            _HASH_RE.fullmatch(args.universe_id) is None
            or _HASH_RE.fullmatch(args.calendar_id) is None
        ):
            raise ExperimentCliError(
                "invalid_arguments",
                "universe and calendar IDs must be SHA-256 values",
            )
        periods = _parse_periods(args.periods)
        train_end = date.fromisoformat(args.train_end)
        holdout_start = date.fromisoformat(args.holdout_start)
        candidate_root = _path_beneath(project_root, args.candidate_root, label="candidate root")
        baseline_root = _path_beneath(project_root, args.baseline_root, label="baseline root")
        output_root = _path_beneath(project_root, args.output, label="output root")
        universe = load_verified_universe(
            project_root / "configs" / "universes" / f"{args.universe_id}.json",
            expected_id=args.universe_id,
        )
        calendar_records = tuple(
            record
            for record in CalendarSnapshotStore(project_root).read_manifest()
            if record.calendar_id == args.calendar_id
        )
        if len(calendar_records) != 1:
            raise ExperimentCliError(
                "calendar_record_not_found",
                "requested calendar ID does not exist exactly once",
            )
        calendar = load_verified_calendar(project_root, calendar_records[0])
        candidate_index = _index_runs(candidate_root)
        baseline_index = _index_runs(baseline_root)
        candidate_runs = {
            member.symbol: {
                period: load_verified_run_series(
                    _find_one(
                        candidate_index,
                        symbol=member.symbol,
                        strategy="sma",
                        sma_period=period,
                        universe_id=args.universe_id,
                    )
                )
                for period in periods
            }
            for member in universe.members
        }
        baseline_runs = {
            member.symbol: {
                "buy_and_hold": load_verified_run_series(
                    _find_one(
                        baseline_index,
                        symbol=member.symbol,
                        strategy="buy_and_hold",
                        sma_period=None,
                        universe_id=args.universe_id,
                    )
                ),
                "sma20": load_verified_run_series(
                    _find_one(
                        baseline_index,
                        symbol=member.symbol,
                        strategy="sma",
                        sma_period=20,
                        universe_id=args.universe_id,
                    )
                ),
            }
            for member in universe.members
        }
        report = build_week5_report(
            candidate_runs,
            baseline_runs,
            expected_universe_id=args.universe_id,
            expected_symbols=tuple(member.symbol for member in universe.members),
            calendar_id=args.calendar_id,
            calendar_dates=calendar.dates,
            train_end=train_end,
            holdout_start=holdout_start,
            candidate_periods=periods,
            replay_days=args.replay_days,
        )
        directory = publish_week5_report(report, output_root)
        relative = directory.relative_to(project_root).as_posix()
    except (ValueError, Week5Error) as exc:
        _write_json(
            sys.stderr,
            {
                "error_code": getattr(exc, "code", "operation_failed"),
                "error_type": type(exc).__name__,
                "status": "error",
            },
        )
        return 1
    except Exception as exc:
        _write_json(
            sys.stderr,
            {
                "error_code": getattr(exc, "code", "operation_failed"),
                "error_type": type(exc).__name__,
                "status": "error",
            },
        )
        return 1
    _write_json(
        sys.stdout,
        {
            "experiment_directory": relative,
            "experiment_id": report.experiment_id,
            "status": "ok",
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
