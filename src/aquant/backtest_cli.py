"""Command-line entry point for explicit, research-only baseline backtests."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aquant.backtest import (
    BacktestConfig,
    StrategyName,
    export_backtest_result,
    load_verified_snapshot,
    run_backtest,
)
from aquant.cli_support import make_safe_argument_parser, path_beneath, write_json
from aquant.data.calendar_snapshot import CalendarSnapshotStore, load_verified_calendar
from aquant.data.corporate_actions import (
    load_verified_corporate_actions,
    read_corporate_action_manifest,
)
from aquant.data.manifest import ManifestWriter
from aquant.rules import CommissionAssumption, make_fee_policy
from aquant.universe import load_verified_universe


class BacktestCliError(RuntimeError):
    """Machine-readable command error that never includes raw input values."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


_SAFE_ARGUMENT_PARSER = make_safe_argument_parser(
    error_factory=BacktestCliError,
    invalid_arguments_message="command arguments are invalid",
)


def _parser() -> argparse.ArgumentParser:
    parser = _SAFE_ARGUMENT_PARSER(prog="aquant-backtest")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SAFE_ARGUMENT_PARSER,
    )
    run = subparsers.add_parser("run", help="run one explicit immutable data snapshot")
    run.add_argument("--project-root", default=".")
    run.add_argument("--manifest", default="data/manifests/manifest.jsonl")
    run.add_argument("--output", default="outputs/backtests")
    run.add_argument("--symbol", required=True)
    run.add_argument("--snapshot-id", required=True)
    run.add_argument("--corporate-action-snapshot-id", required=True)
    run.add_argument("--calendar-id", required=True)
    run.add_argument("--universe-id", required=True)
    run.add_argument(
        "--strategy",
        required=True,
        choices=[strategy.value for strategy in StrategyName],
    )
    run.add_argument("--initial-cash", type=float, default=1_000_000.0)
    run.add_argument("--target-weight", default="0.95")
    run.add_argument("--sma-period", type=int)
    run.add_argument("--random-seed", type=int, default=0)
    run.add_argument("--stock-commission-rate", required=True)
    run.add_argument("--stock-minimum-commission", required=True)
    run.add_argument("--etf-commission-rate", required=True)
    run.add_argument("--etf-minimum-commission", required=True)
    return parser


def _decimal_argument(value: str) -> Decimal:
    if type(value) is not str or re.fullmatch(r"(?:0|[1-9][0-9]*)\.[0-9]+", value) is None:
        raise BacktestCliError("invalid_arguments", "decimal command argument is invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BacktestCliError(
            "invalid_arguments", "decimal command argument is invalid"
        ) from exc
    if not parsed.is_finite():
        raise BacktestCliError("invalid_arguments", "decimal command argument is invalid")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    """Run one manifest-pinned baseline and return a process exit code."""
    try:
        args = _parser().parse_args(argv)
        project_root = Path(args.project_root).resolve()
        if re.fullmatch(r"[0-9a-f]{64}", args.universe_id) is None:
            raise BacktestCliError(
                "invalid_arguments",
                "universe ID must be a lowercase SHA-256 value",
            )
        manifest_path = path_beneath(
            project_root,
            args.manifest,
            label="manifest",
            error_factory=BacktestCliError,
        )
        output_root = path_beneath(
            project_root,
            args.output,
            label="output",
            error_factory=BacktestCliError,
        )
        matches = tuple(
            record
            for record in ManifestWriter(manifest_path).read_all()
            if record.symbol == args.symbol and record.snapshot_id == args.snapshot_id
        )
        if len(matches) != 1:
            raise BacktestCliError(
                "manifest_record_not_found",
                "exactly one requested symbol and snapshot ID must exist in the manifest",
            )
        calendar_matches = tuple(
            record
            for record in CalendarSnapshotStore(project_root).read_manifest()
            if record.calendar_id == args.calendar_id
        )
        if len(calendar_matches) != 1:
            raise BacktestCliError(
                "calendar_record_not_found",
                "exactly one requested calendar ID must exist in the calendar manifest",
            )
        action_matches = tuple(
            record
            for record in read_corporate_action_manifest(project_root)
            if record.symbol == args.symbol
            and record.snapshot_id == args.corporate_action_snapshot_id
        )
        if len(action_matches) != 1:
            raise BacktestCliError(
                "corporate_action_record_not_found",
                "exactly one action snapshot must exist for the requested symbol",
            )

        config = BacktestConfig(
            strategy=StrategyName(args.strategy),
            initial_cash=args.initial_cash,
            target_weight=_decimal_argument(args.target_weight),
            sma_period=args.sma_period,
            random_seed=args.random_seed,
        )
        market_data = load_verified_snapshot(project_root, matches[0])
        calendar = load_verified_calendar(project_root, calendar_matches[0])
        corporate_actions = load_verified_corporate_actions(
            project_root, action_matches[0]
        )
        universe = load_verified_universe(
            project_root
            / "configs"
            / "universes"
            / f"{args.universe_id}.json",
            expected_id=args.universe_id,
        )
        fee_policy = make_fee_policy(
            stock_commission=CommissionAssumption(
                _decimal_argument(args.stock_commission_rate),
                _decimal_argument(args.stock_minimum_commission),
            ),
            etf_commission=CommissionAssumption(
                _decimal_argument(args.etf_commission_rate),
                _decimal_argument(args.etf_minimum_commission),
            ),
        )
        result = run_backtest(
            market_data,
            universe=universe,
            corporate_actions=corporate_actions,
            calendar=calendar,
            fee_policy=fee_policy,
            config=config,
        )
        artifact_directory = export_backtest_result(result, output_root)
        relative_artifact = artifact_directory.relative_to(project_root).as_posix()
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

    write_json(
        sys.stdout,
        {
            "status": "ok",
            "run_id": result.run_id,
            "symbol": result.provenance.symbol,
            "snapshot_id": result.provenance.snapshot_id,
            "universe_id": result.universe_id,
            "strategy": result.config.strategy.value,
            "orders": len(result.orders),
            "fills": len(result.fills),
            "artifact_directory": relative_artifact,
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the installed script
    raise SystemExit(main())
