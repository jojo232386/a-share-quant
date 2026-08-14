"""Command-line entry point for the restricted Week 5 experiment."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aquant.backtest import load_verified_snapshot
from aquant.cli_support import make_safe_argument_parser, path_beneath, write_json
from aquant.data.calendar_snapshot import CalendarSnapshotStore, load_verified_calendar
from aquant.data.corporate_actions import (
    load_verified_corporate_actions,
    read_corporate_action_manifest,
)
from aquant.data.manifest import ManifestWriter
from aquant.planner import PlannerLimits
from aquant.research.loop import (
    STRATEGY_ABSOLUTE_MOMENTUM_252,
    STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12,
    STRATEGY_SMA,
    STRATEGY_VOLATILITY_REGIME_DEFENSE,
    ResearchLoopConfig,
    run_research_loop,
)
from aquant.research.report import build_research_report, publish_research_report
from aquant.research.week5 import (
    Week5Error,
    build_week5_report,
    load_verified_run_series,
    publish_week5_report,
)
from aquant.rules import default_fee_policy
from aquant.universe import load_verified_universe

_HASH_RE = re.compile(r"[0-9a-f]{64}")


class ExperimentCliError(RuntimeError):
    """Sanitized experiment command error."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


_SAFE_ARGUMENT_PARSER = make_safe_argument_parser(
    error_factory=ExperimentCliError,
    invalid_arguments_message="experiment arguments are invalid",
)


def _parser() -> argparse.ArgumentParser:
    parser = _SAFE_ARGUMENT_PARSER(prog="aquant-experiment")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SAFE_ARGUMENT_PARSER,
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

    research = subparsers.add_parser(
        "research-loop",
        help="run the verified Research Loop v1",
    )
    research.add_argument("--project-root", default=".")
    research.add_argument("--data-root", default=".")
    research.add_argument("--universe-id", required=True)
    research.add_argument("--calendar-id", required=True)
    research.add_argument("--snapshot-id", required=True)
    research.add_argument("--corporate-action-snapshot-id", required=True)
    research.add_argument("--secondary-snapshot-id")
    research.add_argument("--secondary-corporate-action-snapshot-id")
    research.add_argument("--preregistration", required=True)
    research.add_argument("--symbol", required=True)
    research.add_argument("--secondary-symbol")
    research.add_argument("--initial-cash-yuan", default="1000000.00")
    research.add_argument(
        "--strategy",
        choices=(
            STRATEGY_SMA,
            STRATEGY_VOLATILITY_REGIME_DEFENSE,
            STRATEGY_ABSOLUTE_MOMENTUM_252,
            STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12,
        ),
        default=STRATEGY_SMA,
    )
    research.add_argument("--sma-period", type=int, default=20)
    research.add_argument("--lookback-returns", type=int, default=20)
    research.add_argument("--lookback-sessions", type=int, default=252)
    research.add_argument("--lookback-start-month", type=int, default=2)
    research.add_argument("--lookback-end-month", type=int, default=12)
    research.add_argument("--annualization", type=int, default=252)
    research.add_argument("--volatility-threshold", default="0.25")
    research.add_argument("--active-weight", default="0.95")
    research.add_argument("--output", default="outputs/research_loop")
    return parser


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


def _exact_record(records, *, identity: str, label: str):
    matches = tuple(
        item
        for item in records
        if getattr(item, "snapshot_id", getattr(item, "calendar_id", None)) == identity
    )
    if len(matches) != 1:
        raise ExperimentCliError(
            "record_not_found",
            f"requested {label} must exist exactly once",
        )
    return matches[0]


def _parse_cash_fen(value: str) -> int:
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ExperimentCliError(
            "invalid_arguments",
            "initial cash must be an exact positive yuan amount",
        ) from exc
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise ExperimentCliError(
            "invalid_arguments",
            "initial cash must be an exact positive yuan amount with at most two decimals",
        )
    return int(amount * 100)


def _git_output(project_root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ("git", "-C", str(project_root), *arguments),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExperimentCliError(
            "git_identity_unavailable",
            "formal research Git identity cannot be resolved",
        ) from exc
    if completed.returncode != 0:
        raise ExperimentCliError(
            "git_identity_unavailable",
            "formal research Git identity cannot be resolved",
        )
    return completed.stdout


def _committed_preregistration(
    project_root: Path,
    value: str,
) -> tuple[str, str, bytes]:
    repository_root = Path(
        _git_output(project_root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    ).resolve()
    if repository_root != project_root:
        raise ExperimentCliError(
            "git_identity_mismatch",
            "project root must be the formal research repository root",
        )
    git_head = (
        _git_output(project_root, "rev-parse", "--verify", "HEAD^{commit}")
        .decode("ascii")
        .strip()
    )
    if re.fullmatch(r"[0-9a-f]{40}", git_head) is None:
        raise ExperimentCliError(
            "git_identity_unavailable",
            "formal research Git identity cannot be resolved",
        )
    if _git_output(project_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ExperimentCliError(
            "repository_not_clean",
            "formal research requires a clean committed repository",
        )
    path = path_beneath(
        project_root,
        value,
        label="preregistration",
        error_factory=ExperimentCliError,
    )
    if path.is_symlink() or not path.is_file():
        raise ExperimentCliError(
            "preregistration_not_found",
            "preregistration must be a regular file in the project repository",
        )
    relative = path.relative_to(project_root).as_posix()
    _git_output(project_root, "ls-files", "--error-unmatch", "--", relative)
    committed_content = _git_output(project_root, "show", f"HEAD:{relative}")
    content = path.read_bytes()
    if content != committed_content:
        raise ExperimentCliError(
            "preregistration_not_committed",
            "preregistration must match the content committed at Git HEAD",
        )
    preregistration_commit = (
        _git_output(project_root, "log", "-1", "--format=%H", "--", relative)
        .decode("ascii")
        .strip()
    )
    if re.fullmatch(r"[0-9a-f]{40}", preregistration_commit) is None:
        raise ExperimentCliError(
            "preregistration_not_committed",
            "preregistration must already be committed before the formal run",
        )
    return git_head, preregistration_commit, content


def _run_research_loop_command(args) -> dict[str, object]:
    project_root = Path(args.project_root).resolve()
    data_root = Path(args.data_root).resolve()
    git_head, preregistration_commit, preregistration_content = _committed_preregistration(
        project_root,
        args.preregistration,
    )
    is_a4_3 = args.strategy == STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12
    secondary_values = (
        args.secondary_symbol,
        args.secondary_snapshot_id,
        args.secondary_corporate_action_snapshot_id,
    )
    if is_a4_3 and any(value is None for value in secondary_values):
        raise ExperimentCliError(
            "invalid_arguments",
            "A4-3 requires the frozen secondary symbol and both secondary input IDs",
        )
    if not is_a4_3 and any(value is not None for value in secondary_values):
        raise ExperimentCliError(
            "invalid_arguments",
            "secondary inputs are reserved for the frozen A4-3 strategy",
        )
    identities = [
        args.universe_id,
        args.calendar_id,
        args.snapshot_id,
        args.corporate_action_snapshot_id,
    ]
    if is_a4_3:
        identities.extend(
            (
                args.secondary_snapshot_id,
                args.secondary_corporate_action_snapshot_id,
            )
        )
    if any(_HASH_RE.fullmatch(value) is None for value in identities):
        raise ExperimentCliError(
            "invalid_arguments",
            "all requested input IDs must be SHA-256 values",
        )
    if not data_root.is_dir() or data_root.is_symlink():
        raise ExperimentCliError(
            "invalid_data_root",
            "data root must be a safe directory",
        )
    try:
        active_weight = Decimal(args.active_weight)
        volatility_threshold = Decimal(args.volatility_threshold)
    except (InvalidOperation, ValueError) as exc:
        raise ExperimentCliError(
            "invalid_arguments",
            "active weight and volatility threshold must be exact decimals",
        ) from exc
    config = ResearchLoopConfig(
        symbol=args.symbol,
        initial_cash_fen=_parse_cash_fen(args.initial_cash_yuan),
        strategy=args.strategy,
        sma_period=args.sma_period,
        lookback_returns=args.lookback_returns,
        lookback_sessions=args.lookback_sessions,
        annualization=args.annualization,
        volatility_threshold=volatility_threshold,
        active_weight=active_weight,
        secondary_symbol=args.secondary_symbol,
        lookback_start_month=args.lookback_start_month,
        lookback_end_month=args.lookback_end_month,
        limits=PlannerLimits(
            max_single_weight=active_weight,
            max_gross=active_weight,
            min_cash_ratio=Decimal("1") - active_weight,
        ),
    )
    universe = load_verified_universe(
        project_root / "configs" / "universes" / f"{args.universe_id}.json",
        expected_id=args.universe_id,
    )
    market_records = ManifestWriter(
        data_root / "data" / "manifests" / "manifest.jsonl"
    ).read_all()
    action_records = read_corporate_action_manifest(data_root)
    market_record = _exact_record(
        market_records,
        identity=args.snapshot_id,
        label="market snapshot",
    )
    action_record = _exact_record(
        action_records,
        identity=args.corporate_action_snapshot_id,
        label="corporate-action snapshot",
    )
    calendar_record = _exact_record(
        CalendarSnapshotStore(data_root).read_manifest(),
        identity=args.calendar_id,
        label="calendar",
    )
    if market_record.symbol != args.symbol or action_record.symbol != args.symbol:
        raise ExperimentCliError(
            "input_identity_mismatch",
            "requested snapshots do not belong to the requested symbol",
        )
    if is_a4_3:
        secondary_market_record = _exact_record(
            market_records,
            identity=args.secondary_snapshot_id,
            label="secondary market snapshot",
        )
        secondary_action_record = _exact_record(
            action_records,
            identity=args.secondary_corporate_action_snapshot_id,
            label="secondary corporate-action snapshot",
        )
        if (
            secondary_market_record.symbol != args.secondary_symbol
            or secondary_action_record.symbol != args.secondary_symbol
        ):
            raise ExperimentCliError(
                "input_identity_mismatch",
                "secondary snapshots do not belong to the frozen secondary symbol",
            )
        market_input = {
            args.symbol: load_verified_snapshot(data_root, market_record),
            args.secondary_symbol: load_verified_snapshot(
                data_root, secondary_market_record
            ),
        }
        action_input = {
            args.symbol: load_verified_corporate_actions(data_root, action_record),
            args.secondary_symbol: load_verified_corporate_actions(
                data_root, secondary_action_record
            ),
        }
    else:
        market_input = load_verified_snapshot(data_root, market_record)
        action_input = load_verified_corporate_actions(data_root, action_record)
    result = run_research_loop(
        git_head=git_head,
        preregistration_commit=preregistration_commit,
        preregistration_content=preregistration_content,
        config=config,
        market_data=market_input,
        corporate_actions=action_input,
        calendar=load_verified_calendar(data_root, calendar_record),
        fee_policy=default_fee_policy(),
        universe=universe,
    )
    report = build_research_report(result)
    output_root = path_beneath(
        project_root,
        args.output,
        label="research output",
        error_factory=ExperimentCliError,
    )
    directory = publish_research_report(report, output_root)
    return {
        "assessment": report.assessment,
        "research_directory": directory.relative_to(project_root).as_posix(),
        "run_id": result.run_id,
        "status": "ok",
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Build one restricted experiment and return a process exit code."""
    try:
        args = _parser().parse_args(argv)
        if args.command == "research-loop":
            response = _run_research_loop_command(args)
            write_json(sys.stdout, response)
            return 0
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
        candidate_root = path_beneath(
            project_root,
            args.candidate_root,
            label="candidate root",
            error_factory=ExperimentCliError,
        )
        baseline_root = path_beneath(
            project_root,
            args.baseline_root,
            label="baseline root",
            error_factory=ExperimentCliError,
        )
        output_root = path_beneath(
            project_root,
            args.output,
            label="output root",
            error_factory=ExperimentCliError,
        )
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
        write_json(
            sys.stderr,
            {
                "error_code": getattr(exc, "code", "operation_failed"),
                "error_type": type(exc).__name__,
                "status": "error",
            },
        )
        return 1
    except Exception as exc:
        write_json(
            sys.stderr,
            {
                "error_code": getattr(exc, "code", "operation_failed"),
                "error_type": type(exc).__name__,
                "status": "error",
            },
        )
        return 1
    write_json(
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
