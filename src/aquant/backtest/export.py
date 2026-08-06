"""Atomic, deterministic persistence for Week 2 audit bundles."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path

from aquant.backtest.models import (
    BacktestResult,
    CashRecord,
    CorporateActionRecord,
    EquityRecord,
    FillRecord,
    OrderRecord,
    PositionLotRecord,
    PositionRecord,
    ReceivableRecord,
)

_ARTIFACT_SCHEMA_VERSION = "2.1"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class BacktestExportError(RuntimeError):
    """Raised when an existing artifact conflicts with a deterministic run."""


def _json_metadata(result: BacktestResult) -> bytes:
    values = {
        "schema_version": result.schema_version,
        "run_id": result.run_id,
        "engine": result.engine,
        "implementation_digest": result.implementation_digest,
        "input_digest": result.input_digest,
        "universe_id": result.universe_id,
        "provenance": asdict(result.provenance),
        "config": {
            **asdict(result.config),
            "strategy": result.config.strategy.value,
            "target_weight": str(result.config.target_weight),
        },
        "row_counts": {
            "orders": len(result.orders),
            "fills": len(result.fills),
            "positions": len(result.positions),
            "cash": len(result.cash_ledger),
            "equity": len(result.equity_curve),
            "lots": len(result.lots),
            "missing_sessions": len(result.missing_market_sessions),
            "corporate_actions": len(result.corporate_action_ledger),
            "receivables": len(result.receivables),
        },
        "corporate_action_provenance": (
            {
                **asdict(result.corporate_action_provenance),
                "coverage_start": (
                    result.corporate_action_provenance.coverage_start.isoformat()
                ),
                "coverage_end": (
                    result.corporate_action_provenance.coverage_end.isoformat()
                ),
            }
            if result.corporate_action_provenance is not None
            else None
        ),
        "price_stream_version": result.price_stream_version,
        "dividend_tax_mode": result.dividend_tax_mode,
        "rule_provenance": (
            asdict(result.rule_provenance)
            if result.rule_provenance is not None
            else None
        ),
        "touched_fee_rates": [
            {
                **asdict(item),
                "effective_date": (
                    item.effective_date.isoformat()
                    if item.effective_date is not None
                    else None
                ),
            }
            for item in result.touched_fee_rates
        ],
        "execution_policy": {
            "signal": "after_daily_close",
            "fill": "next_official_session_open",
            "fees": (
                "verified_date_effective_policy"
                if result.rule_provenance is not None
                else "legacy_synthetic_zero"
            ),
            "cheat_on_close": False,
            "cheat_on_open": False,
            "target_sizing": "fee_aware_next_open_target_weight",
            "dividends": "ex_open_receivable_then_payable_date_cash",
        },
    }
    text = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{text}\n".encode()


def _csv_bytes(rows: tuple[object, ...], row_type: type) -> bytes:
    fieldnames = [field.name for field in fields(row_type)]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        values = asdict(row)
        for key, value in values.items():
            if hasattr(value, "isoformat"):
                values[key] = value.isoformat()
        writer.writerow(values)
    return stream.getvalue().encode()


def _payload_bytes(result: BacktestResult) -> dict[str, bytes]:
    return {
        "run.json": _json_metadata(result),
        "orders.csv": _csv_bytes(result.orders, OrderRecord),
        "fills.csv": _csv_bytes(result.fills, FillRecord),
        "positions.csv": _csv_bytes(result.positions, PositionRecord),
        "cash.csv": _csv_bytes(result.cash_ledger, CashRecord),
        "equity.csv": _csv_bytes(result.equity_curve, EquityRecord),
        "lots.csv": _csv_bytes(result.lots, PositionLotRecord),
        "corporate_actions.csv": _csv_bytes(
            result.corporate_action_ledger, CorporateActionRecord
        ),
        "receivables.csv": _csv_bytes(result.receivables, ReceivableRecord),
        "missing_sessions.json": (
            json.dumps(
                {
                    "dates": [
                        value.isoformat() for value in result.missing_market_sessions
                    ],
                    "row_count": len(result.missing_market_sessions),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode(),
    }


def _complete_bundle_bytes(result: BacktestResult) -> dict[str, bytes]:
    payload = _payload_bytes(result)
    manifest = {
        "artifact_schema_version": _ARTIFACT_SCHEMA_VERSION,
        "status": "complete",
        "run_id": result.run_id,
        "files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(payload.items())
        },
    }
    payload["artifact_manifest.json"] = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    return payload


def _verify_existing_bundle(directory: Path, expected: dict[str, bytes]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise BacktestExportError("run artifact target must be a safe directory")
    actual_names = {path.name for path in directory.iterdir()}
    if actual_names != set(expected):
        raise BacktestExportError("existing artifact bundle is incomplete or has unknown files")
    for name, content in expected.items():
        path = directory / name
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BacktestExportError(f"artifact target is not a safe regular file: {name}")
        if path.read_bytes() != content:
            raise BacktestExportError(
                f"existing artifact conflicts with deterministic run: {name}"
            )


@contextmanager
def _run_lock(root: Path, run_id: str) -> Iterator[None]:
    lock_path = root / f".{run_id}.lock"
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
        metadata = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise BacktestExportError("run bundle lock cannot be opened safely") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise BacktestExportError("run bundle lock is not a safe regular file")
    locked = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_new_bundle(directory: Path, contents: dict[str, bytes]) -> None:
    for name, content in contents.items():
        path = directory / name
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def export_backtest_result(result: BacktestResult, output_root: str | Path) -> Path:
    """Atomically publish one complete, hash-inventoried run directory."""
    if type(result) is not BacktestResult:
        raise TypeError("result must be an exact BacktestResult")
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    run_directory = root / result.run_id
    expected = _complete_bundle_bytes(result)

    with _run_lock(root, result.run_id):
        if run_directory.exists() or run_directory.is_symlink():
            _verify_existing_bundle(run_directory, expected)
            return run_directory

        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{result.run_id}.",
                suffix=".tmp",
                dir=root,
            )
        )
        try:
            _write_new_bundle(temporary, expected)
            if run_directory.exists() or run_directory.is_symlink():
                _verify_existing_bundle(run_directory, expected)
            else:
                os.rename(temporary, run_directory)
                root_descriptor = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(root_descriptor)
                finally:
                    os.close(root_descriptor)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return run_directory
