"""Command-line entry point for the research-only market-data pipeline."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import pandas as pd

from aquant.cli_support import write_json
from aquant.config import load_data_config
from aquant.data.akshare_client import AkshareClient
from aquant.data.calendar_snapshot import CalendarSnapshotStore
from aquant.data.corporate_action_ingestion import (
    CorporateActionIngestionResult,
    run_corporate_action_ingestion,
)
from aquant.data.ingestion import RunResult, run_ingestion
from aquant.data.manifest import ManifestWriter
from aquant.data.snapshot import RawSnapshotStore


@dataclass(frozen=True)
class CliServices:
    """Injectable external boundary services for deterministic CLI tests."""

    client: AkshareClient
    clock: Callable[[], datetime]
    trade_calendar_provider: Callable[[], pd.DataFrame]
    akshare_version: str


def _real_services() -> CliServices:
    import akshare as ak

    return CliServices(
        client=AkshareClient(ak),
        clock=lambda: datetime.now(UTC),
        trade_calendar_provider=ak.tool_trade_date_hist_sina,
        akshare_version=version("akshare"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aquant-data")
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser(
        "fetch",
        help="fetch the content-addressed v0.1 research universe",
    )
    fetch.add_argument("--config", default="configs/data.yaml")
    fetch.add_argument("--project-root", default=".")
    actions = subparsers.add_parser(
        "corporate-actions",
        help="fetch one immutable corporate-action snapshot",
    )
    actions.add_argument("--config", default="configs/data.yaml")
    actions.add_argument("--project-root", default=".")
    actions.add_argument("--symbol", required=True)
    return parser


def _summary(result: RunResult) -> dict[str, object]:
    return {
        "status": "ok",
        "requested_start": result.requested_start.isoformat(),
        "requested_end": result.requested_end.isoformat(),
        "calendar_id": result.calendar_record.calendar_id,
        "missing_session_count": sum(len(item.dates) for item in result.missing_sessions),
        "instruments": [
            {
                "symbol": item.symbol,
                "provider": item.provider,
                "source_function": item.source_function,
                "actual_start": item.actual_start.isoformat(),
                "actual_end": item.actual_end.isoformat(),
                "row_count": item.row_count,
                "snapshot_path": item.snapshot_relative_path.as_posix(),
                "snapshot_reused": item.snapshot_reused,
                "manifest_status": item.manifest_status,
            }
            for item in result.items
        ],
    }


def _corporate_action_summary(
    result: CorporateActionIngestionResult,
) -> dict[str, object]:
    return {
        "status": "ok",
        "symbol": result.symbol,
        "requested_start": result.requested_start.isoformat(),
        "requested_end": result.requested_end.isoformat(),
        "event_count": result.event_count,
        "snapshot_id": result.snapshot_id,
        "file_sha256": result.file_sha256,
    }


def main(argv: Sequence[str] | None = None, *, services: CliServices | None = None) -> int:
    """Run the CLI and return a process exit code without exposing exception text."""
    args = _parser().parse_args(argv)
    try:
        project_root = Path(args.project_root).resolve()
        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = project_root / config_path
        config = load_data_config(config_path)
        active = services or _real_services()
        if args.command == "fetch":
            result = run_ingestion(
                config,
                client=active.client,
                clock=active.clock,
                trade_calendar_provider=active.trade_calendar_provider,
                snapshot_store=RawSnapshotStore(project_root),
                manifest_writer=ManifestWriter(
                    project_root / "data" / "manifests" / "manifest.jsonl"
                ),
                calendar_store=CalendarSnapshotStore(project_root),
                akshare_version=active.akshare_version,
            )
            summary = _summary(result)
        else:
            result = run_corporate_action_ingestion(
                config,
                symbol=args.symbol,
                client=active.client,
                clock=active.clock,
                trade_calendar_provider=active.trade_calendar_provider,
                project_root=project_root,
            )
            summary = _corporate_action_summary(result)
    except Exception as exc:
        write_json(
            sys.stderr,
            {
                "status": "error",
                "error_code": getattr(exc, "code", "operation_failed"),
                "error_type": type(exc).__name__,
            },
        )
        return 1
    write_json(sys.stdout, summary)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the installed script
    raise SystemExit(main())
