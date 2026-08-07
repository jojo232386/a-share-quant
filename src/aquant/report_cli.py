"""Command-line entry point for deterministic audited risk reports."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from aquant.cli_support import make_safe_argument_parser, path_beneath, write_json
from aquant.reporting import (
    build_independent_batch_report,
    load_audited_run_metrics,
    publish_risk_report,
    verify_published_risk_report,
)
from aquant.universe import load_verified_universe


class ReportCliError(RuntimeError):
    """Sanitized report command error."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


_SAFE_ARGUMENT_PARSER = make_safe_argument_parser(
    error_factory=ReportCliError,
    invalid_arguments_message="report command arguments are invalid",
)


def _parser() -> argparse.ArgumentParser:
    parser = _SAFE_ARGUMENT_PARSER(prog="aquant-report")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SAFE_ARGUMENT_PARSER,
    )
    build = subparsers.add_parser(
        "build",
        help="build one verified independent-baseline risk report",
    )
    build.add_argument("--project-root", default=".")
    build.add_argument("--universe-id", required=True)
    build.add_argument("--backtests", default="outputs/backtests")
    build.add_argument("--output", default="outputs/reports")
    build.add_argument("--max-drawdown-limit", type=float, default=0.50)
    build.add_argument("--max-exposure-limit", type=float, default=1.00)
    verify = subparsers.add_parser(
        "verify",
        help="verify one published report against current source run bundles",
    )
    verify.add_argument("--project-root", default=".")
    verify.add_argument("--report-id", required=True)
    verify.add_argument("--backtests", default="outputs/backtests")
    verify.add_argument("--reports", default="outputs/reports")
    return parser


def _candidate_directories(root: Path, universe_id: str) -> tuple[Path, ...]:
    if not root.is_dir() or root.is_symlink():
        raise ReportCliError(
            "backtest_root_not_found",
            "backtest root must be a safe directory",
        )
    candidates: list[Path] = []
    for directory in root.iterdir():
        if (
            not directory.is_dir()
            or directory.is_symlink()
            or _HASH_RE.fullmatch(directory.name) is None
        ):
            continue
        run_path = directory / "run.json"
        try:
            metadata = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if type(metadata) is dict and metadata.get("universe_id") == universe_id:
            candidates.append(directory)
    return tuple(sorted(candidates))


_HASH_RE = re.compile(r"[0-9a-f]{64}")


def main(argv: Sequence[str] | None = None) -> int:
    """Build a deterministic report and return a process exit code."""
    try:
        args = _parser().parse_args(argv)
        project_root = Path(args.project_root).resolve()
        if args.command == "verify":
            if _HASH_RE.fullmatch(args.report_id) is None:
                raise ReportCliError(
                    "invalid_arguments",
                    "report ID must be a lowercase SHA-256 value",
                )
            backtest_root = path_beneath(
                project_root,
                args.backtests,
                label="backtest root",
                error_factory=ReportCliError,
            )
            report_root = path_beneath(
                project_root,
                args.reports,
                label="report root",
                error_factory=ReportCliError,
            )
            verification = verify_published_risk_report(
                report_root / args.report_id,
                backtest_root,
            )
            success_payload: dict[str, object] = {
                "report_id": verification.report_id,
                "run_count": verification.run_count,
                "status": "verified",
                "universe_id": verification.universe_id,
            }
        else:
            if _HASH_RE.fullmatch(args.universe_id) is None:
                raise ReportCliError(
                    "invalid_arguments",
                    "universe ID must be a lowercase SHA-256 value",
                )
            backtest_root = path_beneath(
                project_root,
                args.backtests,
                label="backtest root",
                error_factory=ReportCliError,
            )
            output_root = path_beneath(
                project_root,
                args.output,
                label="report output",
                error_factory=ReportCliError,
            )
            universe = load_verified_universe(
                project_root
                / "configs"
                / "universes"
                / f"{args.universe_id}.json",
                expected_id=args.universe_id,
            )
            runs = tuple(
                load_audited_run_metrics(directory)
                for directory in _candidate_directories(
                    backtest_root,
                    args.universe_id,
                )
            )
            report = build_independent_batch_report(
                runs,
                expected_universe_id=args.universe_id,
                expected_symbols=tuple(
                    item.symbol for item in universe.members
                ),
                max_drawdown_limit=args.max_drawdown_limit,
                max_exposure_limit=args.max_exposure_limit,
            )
            directory = publish_risk_report(report, output_root)
            relative = directory.relative_to(project_root).as_posix()
            success_payload = {
                "report_directory": relative,
                "report_id": report.report_id,
                "run_count": len(runs),
                "status": "ok",
                "universe_id": args.universe_id,
            }
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
    write_json(sys.stdout, success_payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
