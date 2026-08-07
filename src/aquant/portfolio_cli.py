"""Safe, explicit, offline CLI for shared-cash portfolio audit runs."""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from aquant.backtest import load_verified_snapshot
from aquant.cli_support import make_safe_argument_parser, write_json
from aquant.data.calendar_snapshot import (
    CalendarSnapshotStore,
    load_verified_calendar,
)
from aquant.data.corporate_actions import (
    load_verified_corporate_actions,
    read_corporate_action_manifest,
)
from aquant.data.manifest import ManifestWriter
from aquant.gate_e.config import (
    GateEConfig,
    load_gate_e_config,
    verify_gate_e_config,
)
from aquant.gate_e.frozen_manifest import read_frozen_manifest
from aquant.portfolio import (
    PortfolioConfig,
    PortfolioInstrumentInput,
    PortfolioStrategy,
    export_portfolio_run,
    run_verified_portfolio,
    verify_portfolio_artifact,
)
from aquant.release_network import offline_network_guard
from aquant.rules import (
    CommissionAssumption,
    FeePolicyError,
    VerifiedFeePolicy,
    make_fee_policy,
    verify_fee_policy,
)
from aquant.universe import load_verified_universe

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SYMBOL_RE = re.compile(r"[0-9]{6}")
_UNSIGNED_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*")
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_ACTION_MANIFEST = PurePosixPath("data/corporate_actions/manifest.jsonl")


class PortfolioCliError(RuntimeError):
    """Machine-readable command error that never contains raw arguments."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


_SAFE_ARGUMENT_PARSER = make_safe_argument_parser(
    error_factory=PortfolioCliError,
    invalid_arguments_message="portfolio command arguments are invalid",
)


def _parser() -> argparse.ArgumentParser:
    parser = _SAFE_ARGUMENT_PARSER(prog="aquant-portfolio")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SAFE_ARGUMENT_PARSER,
    )
    run = subparsers.add_parser(
        "run",
        help="run one explicit offline shared-cash portfolio",
    )
    run.add_argument("--project-root", default=".")
    run.add_argument("--manifest", required=True)
    run.add_argument("--corporate-action-manifest", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--calendar-id", required=True)
    run.add_argument("--universe-id", required=True)
    run.add_argument(
        "--market-snapshot",
        action="append",
        required=True,
    )
    run.add_argument(
        "--corporate-action-snapshot",
        action="append",
        required=True,
    )
    run.add_argument("--initial-cash-fen", required=True)
    run.add_argument("--gross-target-weight", required=True)
    run.add_argument("--signal-date", required=True)
    run.add_argument("--end-date", required=True)
    run.add_argument("--max-entry-attempts", required=True)
    run.add_argument("--stock-commission-rate", required=True)
    run.add_argument("--stock-minimum-commission", required=True)
    run.add_argument("--etf-commission-rate", required=True)
    run.add_argument("--etf-minimum-commission", required=True)

    run_config = subparsers.add_parser(
        "run-config",
        help="run the immutable a-share-quant v0.2 Gate E config",
    )
    run_config.add_argument("--config", required=True)

    verify = subparsers.add_parser(
        "verify",
        help="independently verify one exported portfolio artifact",
    )
    verify.add_argument("--project-root", default=".")
    verify.add_argument("--artifact", required=True)
    verify.add_argument("--expected-run-id")
    return parser


def _relative_parts(value: object) -> tuple[str, ...]:
    if type(value) is not str or not value or "\\" in value:
        raise PortfolioCliError("unsafe_path", "path must be a safe relative path")
    path = PurePosixPath(value)
    parts = path.parts
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise PortfolioCliError("unsafe_path", "path must be a safe relative path")
    return parts


def _absolute_root_parts(value: object) -> tuple[Path, tuple[str, ...]]:
    if type(value) is not str or not value or "\x00" in value:
        raise PortfolioCliError("unsafe_path", "project root is unsafe")
    candidate = Path(value)
    if any(part == ".." for part in candidate.parts):
        raise PortfolioCliError("unsafe_path", "project root is unsafe")
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    parts = candidate.parts
    if not parts or parts[0] != os.sep:
        raise PortfolioCliError("unsafe_path", "project root is unsafe")
    return candidate, tuple(part for part in parts[1:] if part != ".")


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


@contextmanager
def _safe_project_root(
    value: object,
) -> Iterator[tuple[Path, int, os.stat_result]]:
    root, components = _absolute_root_parts(value)
    current = -1
    try:
        current = os.open(os.sep, _DIRECTORY_FLAGS | _NOFOLLOW)
        for component in components:
            next_descriptor = os.open(
                component,
                _DIRECTORY_FLAGS | _NOFOLLOW,
                dir_fd=current,
            )
            metadata = os.fstat(next_descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                _close_quietly(next_descriptor)
                raise PortfolioCliError(
                    "unsafe_path",
                    "project root is not a safe directory",
                )
            os.close(current)
            current = next_descriptor
        metadata = os.fstat(current)
        yield root, current, metadata
        _assert_root_binding(root, current, metadata)
    except PortfolioCliError:
        raise
    except OSError as exc:
        raise PortfolioCliError(
            "unsafe_path",
            "project root cannot be opened safely",
        ) from exc
    finally:
        if current >= 0:
            _close_quietly(current)


def _assert_root_binding(
    root: Path,
    descriptor: int,
    expected: os.stat_result,
) -> None:
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise PortfolioCliError(
            "unsafe_path",
            "project root binding changed",
        ) from exc
    if (
        not stat.S_ISDIR(path_metadata.st_mode)
        or not _same_inode(expected, descriptor_metadata)
        or not _same_inode(expected, path_metadata)
    ):
        raise PortfolioCliError(
            "unsafe_path",
            "project root binding changed",
        )


def _safe_relative_path(
    root: Path,
    root_descriptor: int,
    value: object,
    *,
    kind: str,
) -> tuple[Path, PurePosixPath]:
    parts = _relative_parts(value)
    relative = PurePosixPath(*parts)
    current = os.dup(root_descriptor)
    try:
        for index, component in enumerate(parts):
            final = index == len(parts) - 1
            flags = os.O_RDONLY
            if not final or kind in {"directory", "output"}:
                flags |= getattr(os, "O_DIRECTORY", 0)
            try:
                next_descriptor = os.open(
                    component,
                    flags | _NOFOLLOW,
                    dir_fd=current,
                )
            except FileNotFoundError:
                if kind == "output":
                    return root.joinpath(*parts), relative
                raise PortfolioCliError(
                    "unsafe_path",
                    "required path is missing",
                ) from None
            except OSError as exc:
                raise PortfolioCliError(
                    "unsafe_path",
                    "path cannot be opened safely",
                ) from exc
            metadata = os.fstat(next_descriptor)
            if final and kind == "file":
                safe = stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
            else:
                safe = stat.S_ISDIR(metadata.st_mode)
            if not safe:
                _close_quietly(next_descriptor)
                raise PortfolioCliError(
                    "unsafe_path",
                    "path is not a safe single-owner object",
                )
            os.close(current)
            current = next_descriptor
        return root.joinpath(*parts), relative
    finally:
        _close_quietly(current)


def _sha256(value: object) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise PortfolioCliError(
            "invalid_arguments",
            "identity must be a lowercase SHA-256 value",
        )
    return value


def _snapshot_mapping(values: object) -> dict[str, str]:
    if type(values) not in {list, tuple} or not values:
        raise PortfolioCliError(
            "invalid_snapshot_mapping",
            "snapshot mappings are invalid",
        )
    result: dict[str, str] = {}
    for raw in values:
        if type(raw) is not str or raw.count("=") != 1:
            raise PortfolioCliError(
                "invalid_snapshot_mapping",
                "snapshot mappings are invalid",
            )
        symbol, snapshot_id = raw.split("=", 1)
        if (
            _SYMBOL_RE.fullmatch(symbol) is None
            or _HASH_RE.fullmatch(snapshot_id) is None
            or symbol in result
        ):
            raise PortfolioCliError(
                "invalid_snapshot_mapping",
                "snapshot mappings are invalid",
            )
        result[symbol] = snapshot_id
    return result


def _positive_integer(value: object) -> int:
    if type(value) is not str or _POSITIVE_INTEGER_RE.fullmatch(value) is None:
        raise PortfolioCliError(
            "invalid_arguments",
            "positive integer argument is invalid",
        )
    return int(value)


def _decimal(value: object) -> Decimal:
    if type(value) is not str or _UNSIGNED_DECIMAL_RE.fullmatch(value) is None:
        raise PortfolioCliError(
            "invalid_arguments",
            "decimal argument is invalid",
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise PortfolioCliError(
            "invalid_arguments",
            "decimal argument is invalid",
        ) from exc
    if not parsed.is_finite():
        raise PortfolioCliError(
            "invalid_arguments",
            "decimal argument is invalid",
        )
    return parsed


def _date(value: object) -> date:
    if type(value) is not str:
        raise PortfolioCliError("invalid_arguments", "date argument is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PortfolioCliError(
            "invalid_arguments",
            "date argument is invalid",
        ) from exc
    if parsed.isoformat() != value:
        raise PortfolioCliError("invalid_arguments", "date argument is invalid")
    return parsed


def _one_record(records, *, symbol: str, snapshot_id: str, code: str):
    matches = tuple(
        record
        for record in records
        if record.symbol == symbol and record.snapshot_id == snapshot_id
    )
    if len(matches) != 1:
        raise PortfolioCliError(
            code,
            "one exact requested manifest record is required",
        )
    return matches[0]


def _load_run_arguments(
    args,
    *,
    root: Path,
    root_descriptor: int,
    root_metadata: os.stat_result,
    gate_e_config: GateEConfig | None = None,
):
    if gate_e_config is None:
        if args is None:
            raise PortfolioCliError(
                "invalid_arguments",
                "legacy portfolio arguments are required",
            )
        fee_policy_override: VerifiedFeePolicy | None = None
        post_end_validation_date: date | None = None
    else:
        if args is not None:
            raise PortfolioCliError(
                "invalid_gate_e_contract",
                "Gate E accepts only its verified config object",
            )
        verify_gate_e_config(gate_e_config)
        args = gate_e_config.to_portfolio_namespace(
            project_root=str(root)
        )
        fee_policy_override = gate_e_config.to_fee_policy()
        post_end_validation_date = (
            gate_e_config.post_end_validation_date
        )
    if args is None:  # pragma: no cover - narrowed above
        raise PortfolioCliError(
            "invalid_arguments",
            "portfolio arguments are required",
        )
    manifest_path, _ = _safe_relative_path(
        root,
        root_descriptor,
        args.manifest,
        kind="file",
    )
    action_manifest_path, action_manifest_relative = _safe_relative_path(
        root,
        root_descriptor,
        args.corporate_action_manifest,
        kind="file",
    )
    if action_manifest_relative != _ACTION_MANIFEST:
        raise PortfolioCliError(
            "unsupported_manifest_path",
            "corporate-action manifest path is not supported",
        )
    output_root, output_relative = _safe_relative_path(
        root,
        root_descriptor,
        args.output,
        kind="output",
    )
    universe_id = _sha256(args.universe_id)
    calendar_id = _sha256(args.calendar_id)
    universe_path, _ = _safe_relative_path(
        root,
        root_descriptor,
        f"configs/universes/{universe_id}.json",
        kind="file",
    )
    _safe_relative_path(
        root,
        root_descriptor,
        "data/calendars/manifest.jsonl",
        kind="file",
    )
    _assert_root_binding(root, root_descriptor, root_metadata)

    universe = load_verified_universe(
        universe_path,
        expected_id=universe_id,
    )
    symbols = tuple(member.symbol for member in universe.members)
    expected_symbols = set(symbols)
    market_mappings = _snapshot_mapping(args.market_snapshot)
    action_mappings = _snapshot_mapping(args.corporate_action_snapshot)
    if set(market_mappings) != expected_symbols or set(action_mappings) != expected_symbols:
        raise PortfolioCliError(
            "snapshot_mapping_mismatch",
            "snapshot mapping symbols must equal the verified universe",
        )

    market_records = (
        read_frozen_manifest(
            manifest_path,
            expected_sha256=gate_e_config.payload["input_files"][
                args.manifest
            ],
        )
        if gate_e_config is not None
        else ManifestWriter(manifest_path).read_all()
    )
    action_records = read_corporate_action_manifest(root)
    if action_manifest_path != root / _ACTION_MANIFEST.as_posix():
        raise PortfolioCliError(
            "unsafe_path",
            "corporate-action manifest binding is invalid",
        )
    calendar_matches = tuple(
        record
        for record in CalendarSnapshotStore(root).read_manifest()
        if record.calendar_id == calendar_id
    )
    if len(calendar_matches) != 1:
        raise PortfolioCliError(
            "calendar_record_not_found",
            "one exact calendar record is required",
        )
    calendar_record = calendar_matches[0]
    _safe_relative_path(
        root,
        root_descriptor,
        calendar_record.relative_path.as_posix(),
        kind="file",
    )
    calendar = load_verified_calendar(root, calendar_record)

    inputs: list[PortfolioInstrumentInput] = []
    for symbol in sorted(symbols):
        market_record = _one_record(
            market_records,
            symbol=symbol,
            snapshot_id=market_mappings[symbol],
            code="manifest_record_not_found",
        )
        action_record = _one_record(
            action_records,
            symbol=symbol,
            snapshot_id=action_mappings[symbol],
            code="corporate_action_record_not_found",
        )
        _safe_relative_path(
            root,
            root_descriptor,
            market_record.snapshot_relative_path.as_posix(),
            kind="file",
        )
        _safe_relative_path(
            root,
            root_descriptor,
            action_record.snapshot_relative_path.as_posix(),
            kind="file",
        )
        inputs.append(
            PortfolioInstrumentInput(
                market_data=load_verified_snapshot(root, market_record),
                corporate_actions=load_verified_corporate_actions(
                    root,
                    action_record,
                ),
            )
        )

    config = PortfolioConfig(
        strategy=PortfolioStrategy.BUY_AND_HOLD,
        initial_cash_fen=_positive_integer(args.initial_cash_fen),
        gross_target_weight=_decimal(args.gross_target_weight),
        signal_date=_date(args.signal_date),
        end_date=_date(args.end_date),
        max_entry_attempts=_positive_integer(args.max_entry_attempts),
    )
    if post_end_validation_date is not None:
        if (
            type(post_end_validation_date) is not date
            or calendar.next_session(config.end_date)
            != post_end_validation_date
        ):
            raise PortfolioCliError(
                "post_end_validation_mismatch",
                "Gate E post-end validation date is not the next session",
            )
    if fee_policy_override is None:
        fee_policy = make_fee_policy(
            stock_commission=CommissionAssumption(
                rate=_decimal(args.stock_commission_rate),
                minimum_yuan=_decimal(args.stock_minimum_commission),
            ),
            etf_commission=CommissionAssumption(
                rate=_decimal(args.etf_commission_rate),
                minimum_yuan=_decimal(args.etf_minimum_commission),
            ),
        )
    else:
        try:
            verify_fee_policy(fee_policy_override)
        except (AttributeError, FeePolicyError, TypeError, ValueError) as exc:
            raise PortfolioCliError(
                "unverified_fee_policy",
                "Gate E fee policy is not verified",
            ) from exc
        fee_policy = fee_policy_override
    _assert_root_binding(root, root_descriptor, root_metadata)
    run = run_verified_portfolio(
        config=config,
        inputs=tuple(inputs),
        universe=universe,
        calendar=calendar,
        fee_policy=fee_policy,
    )
    if gate_e_config is not None:
        verify_gate_e_config(gate_e_config)
    directory = export_portfolio_run(run, output_root)
    expected_directory = output_root / run.identity.run_id
    if directory != expected_directory:
        raise PortfolioCliError(
            "artifact_identity_mismatch",
            "portfolio artifact path is invalid",
        )
    if gate_e_config is not None:
        verify_gate_e_config(gate_e_config)
    _assert_root_binding(root, root_descriptor, root_metadata)
    return run, output_relative / run.identity.run_id


def _run_command(args) -> dict[str, object]:
    with _safe_project_root(args.project_root) as (
        root,
        root_descriptor,
        root_metadata,
    ):
        run, relative_directory = _load_run_arguments(
            args,
            root=root,
            root_descriptor=root_descriptor,
            root_metadata=root_metadata,
        )
        return {
            "artifact_directory": relative_directory.as_posix(),
            "run_id": run.identity.run_id,
            "status": "ok",
            "symbol_count": len(run.result.targets),
        }


def _run_config_command(args) -> dict[str, object]:
    with _safe_project_root(".") as (
        root,
        root_descriptor,
        root_metadata,
    ):
        config_path, _relative = _safe_relative_path(
            root,
            root_descriptor,
            args.config,
            kind="file",
        )
        with offline_network_guard():
            config = load_gate_e_config(config_path)
            run, relative_directory = _load_run_arguments(
                None,
                root=root,
                root_descriptor=root_descriptor,
                root_metadata=root_metadata,
                gate_e_config=config,
            )
        return {
            "artifact_directory": relative_directory.as_posix(),
            "run_id": run.identity.run_id,
            "status": "ok",
            "symbol_count": len(run.result.targets),
        }


def _verify_command(args) -> dict[str, object]:
    expected_run_id = None if args.expected_run_id is None else _sha256(args.expected_run_id)
    with _safe_project_root(args.project_root) as (
        root,
        root_descriptor,
        root_metadata,
    ):
        artifact, _ = _safe_relative_path(
            root,
            root_descriptor,
            args.artifact,
            kind="directory",
        )
        with offline_network_guard():
            verified = verify_portfolio_artifact(
                artifact,
                expected_run_id=expected_run_id,
            )
        _assert_root_binding(root, root_descriptor, root_metadata)
        return {
            "artifact_manifest_sha256": (verified.artifact_manifest_sha256),
            "artifact_file_count": verified.artifact_file_count,
            "file_count": verified.file_count,
            "payload_file_count": verified.payload_file_count,
            "run_id": verified.run_id,
            "status": verified.status,
            "trade_count": verified.trade_count,
        }


def main(argv: Sequence[str] | None = None) -> int:
    """Run or verify one explicit offline portfolio audit artifact."""
    try:
        args = _parser().parse_args(argv)
        if args.command == "run":
            payload = _run_command(args)
        elif args.command == "run-config":
            payload = _run_config_command(args)
        elif args.command == "verify":
            payload = _verify_command(args)
        else:
            raise PortfolioCliError(
                "invalid_arguments",
                "portfolio command is unsupported",
            )
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
    write_json(sys.stdout, payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
