"""Machine-readable entry point for offline v0.1 release verification."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from aquant.release_replay import ProgressEvent, verify_release


class ReleaseCliError(RuntimeError):
    """Sanitized command-line failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ReleaseCliError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="aquant-release")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SafeArgumentParser,
    )
    verify = subparsers.add_parser(
        "verify",
        help="rebuild the frozen v0.1 research release offline",
    )
    verify.add_argument("--project-root", default=".")
    return parser


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


def _progress(event: ProgressEvent) -> None:
    _write_json(
        sys.stderr,
        {
            "completed": event.completed,
            "stage": event.stage,
            "total": event.total,
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Verify the fixed release and return a process exit code."""
    started = time.monotonic()
    try:
        args = _parser().parse_args(argv)
        if args.command != "verify":
            raise ReleaseCliError("invalid_arguments")
        project_root = Path(args.project_root).resolve()
        release_root = (project_root / "release" / "v0.1-research").resolve()
        try:
            release_root.relative_to(project_root)
        except ValueError as exc:
            raise ReleaseCliError("unsafe_path") from exc
        summary = verify_release(
            project_root=project_root,
            release_root=release_root,
            progress=_progress,
        )
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
            "baseline_run_count": summary.baseline_run_count,
            "candidate_run_count": summary.candidate_run_count,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "release_name": summary.release_name,
            "replay_row_count": summary.replay_row_count,
            "risk_report_id": summary.risk_report_id,
            "status": "verified",
            "week5_experiment_id": summary.week5_experiment_id,
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
