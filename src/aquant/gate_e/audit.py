"""Independent Gate E audits for dates, no-bar evidence and accounting."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import (
    ROUND_FLOOR,
    ROUND_HALF_UP,
    Context,
    Decimal,
    DecimalException,
    localcontext,
)
from pathlib import Path

import pandas as pd
import pyarrow as pa

from aquant.data import (
    DataQualityError,
    NormalizationError,
    SourceSchema,
    normalize_market_frame,
    validate_market_frame,
)
from aquant.data.akshare_client import (
    SourceContractError,
    validate_source_contract,
)
from aquant.data.manifest import ManifestError, ManifestRecord
from aquant.gate_e.config import (
    GateEConfig,
    GateEConfigError,
    verify_gate_e_config,
)
from aquant.gate_e.frozen_manifest import read_frozen_manifest
from aquant.portfolio.verify import (
    PortfolioArtifactError,
    verify_portfolio_artifact,
)

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_INTEGER_RE = re.compile(r"0|[1-9][0-9]*")
_DECIMAL_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_METRIC_QUANTUM = Decimal("0.000000000001")
_ANNUAL_SESSIONS = 252
_ZERO = Decimal(0)
_ONE = Decimal(1)

_CSV_SCHEMAS = {
    "targets.csv": (
        "run_id",
        "schema_version",
        "target_id",
        "symbol",
        "signal_date",
        "target_notional_fen",
        "attempts_used",
        "status",
        "fill_event_id",
    ),
    "orders.csv": (
        "run_id",
        "schema_version",
        "attempt_id",
        "target_id",
        "symbol",
        "original_signal_date",
        "intent_session",
        "execution_session",
        "attempt_number",
        "initial_candidate_size",
        "requested_size",
        "availability_status",
        "status",
        "rejection_reason",
        "cash_available_before_fen",
        "initial_candidate_cash_required_fen",
        "requested_cash_required_fen",
        "quantity_adjustment_reason",
        "fill_event_id",
    ),
    "fills.csv": (
        "run_id",
        "schema_version",
        "fill_event_id",
        "attempt_id",
        "target_id",
        "symbol",
        "execution_session",
        "side",
        "initial_candidate_size",
        "filled_size",
        "unit_price",
        "notional_fen",
        "commission_fen",
        "stamp_duty_fen",
        "transfer_fee_fen",
        "total_fees_fen",
        "cash_before_fen",
        "cash_after_fen",
        "lot_id",
        "available_date",
        "cash_available_before_fen",
        "initial_candidate_cash_required_fen",
        "requested_cash_required_fen",
        "quantity_adjustment_reason",
    ),
    "positions.csv": (
        "run_id",
        "schema_version",
        "session",
        "symbol",
        "total_size",
        "available_size",
        "locked_size",
        "mark_price",
        "market_value_fen",
    ),
    "lots.csv": (
        "run_id",
        "schema_version",
        "lot_id",
        "symbol",
        "acquired_date",
        "available_date",
        "original_size",
        "remaining_size",
        "unit_cost",
    ),
    "cash.csv": (
        "run_id",
        "schema_version",
        "event_id",
        "event_kind",
        "session",
        "side",
        "symbol",
        "reference_id",
        "notional_fen",
        "commission_fen",
        "stamp_duty_fen",
        "transfer_fee_fen",
        "total_fees_fen",
        "cash_before_fen",
        "cash_after_fen",
    ),
    "equity.csv": (
        "run_id",
        "schema_version",
        "session",
        "cash_fen",
        "position_market_value_fen",
        "receivable_fen",
        "equity_fen",
    ),
    "receivables.csv": (
        "run_id",
        "schema_version",
        "event_id",
        "symbol",
        "registered_date",
        "source_payable_date",
        "actual_cash_date",
        "amount_fen",
        "paid_date",
    ),
    "corporate_actions.csv": (
        "run_id",
        "schema_version",
        "event_id",
        "symbol",
        "ex_date",
        "source_payable_date",
        "actual_cash_date",
        "entitled_size",
        "cash_dividend_per_unit",
        "amount_fen",
    ),
    "availability.csv": (
        "run_id",
        "schema_version",
        "session",
        "symbol",
        "status",
        "mark_price",
        "carried_sessions",
        "adjustment_reason",
    ),
}
_PAYLOAD_FILES = frozenset({*_CSV_SCHEMAS, "run.json", "metrics.json"})
_ARTIFACT_FILES = _PAYLOAD_FILES | {"artifact_manifest.json"}

_ECONOMIC_DATE_FIELDS = {
    "targets.csv": ("signal_date",),
    "orders.csv": (
        "original_signal_date",
        "intent_session",
        "execution_session",
    ),
    "fills.csv": ("execution_session",),
    "positions.csv": ("session",),
    "lots.csv": ("acquired_date",),
    "cash.csv": ("session",),
    "equity.csv": ("session",),
    "receivables.csv": ("registered_date", "paid_date"),
    "corporate_actions.csv": ("ex_date",),
    "availability.csv": ("session",),
}


class GateEAuditError(RuntimeError):
    """Stable, sanitized failure at the independent Gate E audit boundary."""

    def __init__(self, code: str, *, cause_code: str | None = None):
        self.code = code
        self.cause_code = cause_code
        super().__init__(code)


@dataclass(frozen=True)
class GateEAccountingAudit:
    """Independent result reconstructed from the persisted 13-file bundle."""

    run_id: str
    end_date: date
    initial_cash_fen: int
    invested_notional_fen: int
    paid_fees_fen: int
    dividend_cash_paid_fen: int
    ending_cash_fen: int
    gross_target_notional_fen: int
    allocation_rounding_fen: int
    ordinary_lot_rounding_fen: int
    fee_lot_reduction_fen: int
    pending_uninvested_fen: int
    expired_uninvested_fen: int
    ending_position_market_value_fen: int
    ending_receivable_fen: int
    ending_equity_fen: int
    observation_count: int
    latest_plan_date: date | None
    no_bar_dates: tuple[tuple[str, tuple[date, ...]], ...]
    no_bar_carried_sessions: tuple[tuple[str, date, int], ...]

    @property
    def no_bar_counts(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (symbol, len(dates))
            for symbol, dates in self.no_bar_dates
            if dates
        )

    @property
    def no_bar_total(self) -> int:
        return sum(count for _symbol, count in self.no_bar_counts)


@dataclass(frozen=True)
class GateEInputAudit:
    """Calendar-minus-symbol-bars evidence reconstructed from frozen inputs."""

    session_count: int
    no_bar_dates: tuple[tuple[str, tuple[date, ...]], ...]
    no_bar_carried_sessions: tuple[tuple[str, date, int], ...]

    @property
    def no_bar_counts(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (symbol, len(dates))
            for symbol, dates in self.no_bar_dates
            if dates
        )

    @property
    def no_bar_total(self) -> int:
        return sum(count for _symbol, count in self.no_bar_counts)


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _safe_directory(path: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if absolute == Path(absolute.anchor):
        raise GateEAuditError("unsafe_audit_path")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            absolute.anchor,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
        )
        for component in absolute.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                dir_fd=descriptor,
            )
            named = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            opened = os.fstat(child)
            if (
                not stat.S_ISDIR(named.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or not _same_object(named, opened)
            ):
                os.close(child)
                raise GateEAuditError("unsafe_audit_path")
            os.close(descriptor)
            descriptor = child
        if absolute.resolve(strict=True) != absolute:
            raise GateEAuditError("unsafe_audit_path")
        return descriptor
    except GateEAuditError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, RuntimeError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise GateEAuditError("unsafe_audit_path") from exc


def _read_named_file(
    directory_descriptor: int,
    name: str,
    *,
    expected_sha256: str | None = None,
) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
            dir_fd=directory_descriptor,
        )
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
            raise GateEAuditError("unsafe_audit_file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)
        final = os.fstat(descriptor)
        named = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(final.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or final.st_nlink != 1
            or named.st_nlink != 1
            or not _same_object(initial, final)
            or not _same_object(initial, named)
            or initial.st_size != final.st_size
            or initial.st_mtime_ns != final.st_mtime_ns
            or initial.st_ctime_ns != final.st_ctime_ns
            or len(content) != final.st_size
        ):
            raise GateEAuditError("unsafe_audit_file")
        if (
            expected_sha256 is not None
            and hashlib.sha256(content).hexdigest() != expected_sha256
        ):
            raise GateEAuditError("audit_hash_mismatch")
        return content
    except GateEAuditError:
        raise
    except OSError as exc:
        raise GateEAuditError("unsafe_audit_file") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_bundle(directory: Path) -> dict[str, bytes]:
    descriptor = _safe_directory(directory)
    try:
        try:
            names = frozenset(os.listdir(descriptor))
        except OSError as exc:
            raise GateEAuditError("unsafe_audit_path") from exc
        if names != _ARTIFACT_FILES:
            raise GateEAuditError("artifact_file_set_mismatch")
        result = {
            name: _read_named_file(descriptor, name)
            for name in sorted(_ARTIFACT_FILES)
        }
        if frozenset(os.listdir(descriptor)) != names:
            raise GateEAuditError("artifact_file_set_mismatch")
        return result
    finally:
        os.close(descriptor)


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GateEAuditError("noncanonical_audit_json")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise GateEAuditError("noncanonical_audit_json")


def _json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode()
    except (TypeError, UnicodeError, ValueError) as exc:
        raise GateEAuditError("noncanonical_audit_json") from exc


def _parse_json(content: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except GateEAuditError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GateEAuditError("noncanonical_audit_json") from exc
    if type(parsed) is not dict or _json_bytes(parsed) != content:
        raise GateEAuditError("noncanonical_audit_json")
    return parsed


def _parse_csv(name: str, content: bytes) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        rows = list(reader)
    except (UnicodeError, csv.Error) as exc:
        raise GateEAuditError("noncanonical_audit_csv") from exc
    fields = _CSV_SCHEMAS[name]
    if tuple(reader.fieldnames or ()) != fields or any(
        set(row) != set(fields) or None in row for row in rows
    ):
        raise GateEAuditError("noncanonical_audit_csv")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=fields,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    if stream.getvalue().encode() != content:
        raise GateEAuditError("noncanonical_audit_csv")
    return rows


def _date(value: object) -> date:
    if type(value) is not str:
        raise GateEAuditError("invalid_audit_date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise GateEAuditError("invalid_audit_date") from exc
    if parsed.isoformat() != value:
        raise GateEAuditError("invalid_audit_date")
    return parsed


def _integer(value: object) -> int:
    if type(value) is not str or _INTEGER_RE.fullmatch(value) is None:
        raise GateEAuditError("invalid_audit_integer")
    return int(value)


def _json_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise GateEAuditError("invalid_audit_integer")
    return value


def _decimal(value: object) -> Decimal:
    if type(value) is not str or _DECIMAL_RE.fullmatch(value) is None:
        raise GateEAuditError("invalid_audit_decimal")
    try:
        parsed = Decimal(value)
    except DecimalException as exc:
        raise GateEAuditError("invalid_audit_decimal") from exc
    if not parsed.is_finite():
        raise GateEAuditError("invalid_audit_decimal")
    return parsed


def _published(value: Decimal) -> Decimal:
    try:
        with localcontext() as context:
            context.prec = max(80, len(value.as_tuple().digits) + 20)
            return value.quantize(
                _METRIC_QUANTUM,
                rounding=ROUND_HALF_UP,
            )
    except DecimalException as exc:
        raise GateEAuditError("metric_recomputation_failed") from exc


def _notional(unit_price: str, size: str) -> int:
    value = _decimal(unit_price)
    units = _integer(size)
    if value <= 0 or units <= 0:
        raise GateEAuditError("allocation_reconciliation_failed")
    try:
        rounded = (value * units).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except DecimalException as exc:
        raise GateEAuditError("allocation_reconciliation_failed") from exc
    return int(rounded * 100)


def _verify_manifest(
    contents: Mapping[str, bytes],
    rows: Mapping[str, Sequence[Mapping[str, str]]],
    run: Mapping[str, object],
    metrics: Mapping[str, object],
    *,
    expected_run_id: str | None,
) -> str:
    manifest = _parse_json(contents["artifact_manifest.json"])
    if (
        set(manifest) != {
            "artifact_schema_version",
            "files",
            "run_id",
            "status",
        }
        or manifest["status"] != "complete"
        or manifest["artifact_schema_version"] != "0.2.0"
        or type(manifest["files"]) is not dict
        or set(manifest["files"]) != _PAYLOAD_FILES
    ):
        raise GateEAuditError("artifact_manifest_mismatch")
    run_id = manifest["run_id"]
    if (
        type(run_id) is not str
        or _HASH_RE.fullmatch(run_id) is None
        or expected_run_id is not None
        and run_id != expected_run_id
        or run.get("run_id") != run_id
        or metrics.get("run_id") != run_id
    ):
        raise GateEAuditError("run_id_mismatch")
    for name in sorted(_PAYLOAD_FILES):
        entry = manifest["files"][name]
        row_count = (
            1
            if name.endswith(".json")
            else len(rows[name])
        )
        if (
            type(entry) is not dict
            or set(entry)
            != {"row_count", "run_id", "schema_version", "sha256"}
            or entry["run_id"] != run_id
            or entry["schema_version"] != "0.2.0"
            or entry["row_count"] != row_count
            or entry["sha256"]
            != hashlib.sha256(contents[name]).hexdigest()
        ):
            raise GateEAuditError("artifact_manifest_mismatch")
    for values in rows.values():
        if any(
            row["run_id"] != run_id
            or row["schema_version"] != "0.2.0"
            for row in values
        ):
            raise GateEAuditError("run_id_mismatch")
    return run_id


def _check_date_boundary(
    rows: Mapping[str, Sequence[Mapping[str, str]]],
    run: Mapping[str, object],
    metrics: Mapping[str, object],
    *,
    end_date: date,
) -> date | None:
    latest_plan: date | None = None
    for name, fields in _ECONOMIC_DATE_FIELDS.items():
        for row in rows[name]:
            for field in fields:
                value = row[field]
                if value == "" and name == "receivables.csv" and field == "paid_date":
                    continue
                if _date(value) > end_date:
                    raise GateEAuditError("post_end_economic_event")
    for row in rows["lots.csv"]:
        planned = _date(row["available_date"])
        latest_plan = planned if latest_plan is None else max(latest_plan, planned)
    for row in rows["fills.csv"]:
        planned = _date(row["available_date"])
        latest_plan = planned if latest_plan is None else max(latest_plan, planned)
    for row in rows["receivables.csv"]:
        source = _date(row["source_payable_date"])
        actual = _date(row["actual_cash_date"])
        latest_plan = max(
            item
            for item in (latest_plan, source, actual)
            if item is not None
        )
        if row["paid_date"] != "" and actual > end_date:
            raise GateEAuditError("post_end_economic_event")
    for row in rows["corporate_actions.csv"]:
        source = _date(row["source_payable_date"])
        actual = _date(row["actual_cash_date"])
        latest_plan = max(
            item
            for item in (latest_plan, source, actual)
            if item is not None
        )
    touched = run.get("touched_fee_rates")
    if type(touched) is not list:
        raise GateEAuditError("invalid_run_contract")
    for item in touched:
        if type(item) is not dict or _date(item.get("execution_session")) > end_date:
            raise GateEAuditError("post_end_economic_event")
    exposure = metrics.get("daily_gross_exposure")
    if type(exposure) is not list:
        raise GateEAuditError("metric_recomputation_failed")
    for item in exposure:
        if type(item) is not dict or _date(item.get("session")) > end_date:
            raise GateEAuditError("post_end_economic_event")
    return latest_plan


def _audit_no_bar_rows(
    availability: Sequence[Mapping[str, str]],
) -> tuple[
    tuple[tuple[str, tuple[date, ...]], ...],
    tuple[tuple[str, date, int], ...],
]:
    grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in availability:
        grouped[row["symbol"]].append(row)
    dates_by_symbol: list[tuple[str, tuple[date, ...]]] = []
    carried_evidence: list[tuple[str, date, int]] = []
    for symbol in sorted(grouped):
        ordered = sorted(grouped[symbol], key=lambda row: row["session"])
        carried = 0
        missing: list[date] = []
        previous: date | None = None
        for row in ordered:
            session = _date(row["session"])
            if previous is not None and session <= previous:
                raise GateEAuditError("no_bar_reconciliation_failed")
            previous = session
            persisted = _integer(row["carried_sessions"])
            if row["status"] == "available":
                carried = 0
                if persisted != 0 or row["adjustment_reason"] != "bar_close":
                    raise GateEAuditError("no_bar_reconciliation_failed")
            elif row["status"] == "no_bar_unavailable":
                carried += 1
                if (
                    persisted != carried
                    or row["adjustment_reason"]
                    not in {"no_bar_carry", "cash_dividend"}
                ):
                    raise GateEAuditError("no_bar_reconciliation_failed")
                missing.append(session)
                carried_evidence.append((symbol, session, carried))
            else:
                raise GateEAuditError("no_bar_reconciliation_failed")
        dates_by_symbol.append((symbol, tuple(missing)))
    return tuple(dates_by_symbol), tuple(carried_evidence)


def _cash_events_in_execution_order(
    events: Sequence[Mapping[str, str]],
    *,
    initial_cash_fen: int,
) -> tuple[Mapping[str, str], ...]:
    by_session: dict[date, list[Mapping[str, str]]] = defaultdict(list)
    for event in events:
        by_session[_date(event["session"])].append(event)
    running_cash = initial_cash_fen
    ordered: list[Mapping[str, str]] = []
    for session in sorted(by_session):
        pending = list(by_session[session])
        while pending:
            candidates = [
                event
                for event in pending
                if _integer(event["cash_before_fen"]) == running_cash
            ]
            if len(candidates) != 1:
                raise GateEAuditError("cash_reconciliation_failed")
            event = candidates[0]
            pending.remove(event)
            ordered.append(event)
            running_cash = _integer(event["cash_after_fen"])
    return tuple(ordered)


def _cash_and_accounting(
    rows: Mapping[str, Sequence[Mapping[str, str]]],
    run: Mapping[str, object],
    metrics: Mapping[str, object],
) -> dict[str, int]:
    config = run.get("config")
    if type(config) is not dict:
        raise GateEAuditError("invalid_run_contract")
    initial_cash = _json_integer(config.get("initial_cash_fen"))
    equity = rows["equity.csv"]
    if not equity:
        raise GateEAuditError("equity_reconciliation_failed")

    running_cash = initial_cash
    invested_from_cash = 0
    fees_from_cash = 0
    dividend_cash = 0
    for event in _cash_events_in_execution_order(
        rows["cash.csv"],
        initial_cash_fen=initial_cash,
    ):
        before = _integer(event["cash_before_fen"])
        after = _integer(event["cash_after_fen"])
        notional = _integer(event["notional_fen"])
        fees = _integer(event["total_fees_fen"])
        if before != running_cash:
            raise GateEAuditError("cash_reconciliation_failed")
        if event["event_kind"] == "fill":
            if event["side"] != "buy":
                raise GateEAuditError("cash_reconciliation_failed")
            expected_after = before - notional - fees
            invested_from_cash += notional
            fees_from_cash += fees
        elif event["event_kind"] == "dividend_payment":
            if event["side"] != "" or fees != 0:
                raise GateEAuditError("cash_reconciliation_failed")
            expected_after = before + notional
            dividend_cash += notional
        else:
            raise GateEAuditError("cash_reconciliation_failed")
        if after != expected_after or after < 0:
            raise GateEAuditError("cash_reconciliation_failed")
        running_cash = after

    fills = rows["fills.csv"]
    invested = sum(_integer(row["notional_fen"]) for row in fills)
    paid_fees = sum(_integer(row["total_fees_fen"]) for row in fills)
    if invested != invested_from_cash or paid_fees != fees_from_cash:
        raise GateEAuditError("cash_reconciliation_failed")

    parsed_equity: list[tuple[date, int, int, int, int]] = []
    for row in equity:
        session = _date(row["session"])
        cash = _integer(row["cash_fen"])
        position = _integer(row["position_market_value_fen"])
        receivable = _integer(row["receivable_fen"])
        total = _integer(row["equity_fen"])
        parsed_equity.append((session, cash, position, receivable, total))
    if any(
        left[0] >= right[0]
        for left, right in zip(parsed_equity, parsed_equity[1:], strict=False)
    ):
        raise GateEAuditError("equity_reconciliation_failed")
    ending = parsed_equity[-1]
    if (
        ending[1] != running_cash
        or ending[1]
        != initial_cash - invested - paid_fees + dividend_cash
    ):
        raise GateEAuditError("cash_reconciliation_failed")
    if any(
        total != cash + position + receivable
        for _session, cash, position, receivable, total in parsed_equity
    ):
        raise GateEAuditError("equity_reconciliation_failed")

    ending_positions = sum(
        _integer(row["market_value_fen"])
        for row in rows["positions.csv"]
        if _date(row["session"]) == ending[0]
    )
    ending_receivables = sum(
        _integer(row["amount_fen"])
        for row in rows["receivables.csv"]
        if row["paid_date"] == ""
    )
    if ending[2] != ending_positions or ending[3] != ending_receivables:
        raise GateEAuditError("equity_reconciliation_failed")
    if ending[4] != ending[1] + ending_positions + ending_receivables:
        raise GateEAuditError("equity_reconciliation_failed")

    weight = _decimal(config.get("gross_target_weight"))
    if not _ZERO < weight <= _ONE:
        raise GateEAuditError("allocation_reconciliation_failed")
    try:
        gross_target = int(
            (Decimal(initial_cash) * weight).to_integral_value(
                rounding=ROUND_FLOOR,
            )
        )
    except DecimalException as exc:
        raise GateEAuditError("allocation_reconciliation_failed") from exc
    targets = rows["targets.csv"]
    if not targets:
        raise GateEAuditError("allocation_reconciliation_failed")
    per_symbol = gross_target // len(targets)
    allocation_rounding = gross_target - per_symbol * len(targets)
    if any(_integer(row["target_notional_fen"]) != per_symbol for row in targets):
        raise GateEAuditError("allocation_reconciliation_failed")

    orders_by_target: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows["orders.csv"]:
        orders_by_target[row["target_id"]].append(row)
    fills_by_attempt = {row["attempt_id"]: row for row in fills}
    ordinary_rounding = 0
    fee_reduction = 0
    pending = 0
    expired = 0
    invested_by_target = 0
    for target in targets:
        target_notional = _integer(target["target_notional_fen"])
        if target["status"] == "expired_unfilled":
            expired += target_notional
            continue
        if target["status"] == "pending":
            pending += target_notional
            continue
        filled = [
            row
            for row in orders_by_target[target["target_id"]]
            if row["status"] == "filled"
        ]
        if target["status"] != "filled" or len(filled) != 1:
            raise GateEAuditError("allocation_reconciliation_failed")
        attempt = filled[0]
        fill = fills_by_attempt.get(attempt["attempt_id"])
        if fill is None:
            raise GateEAuditError("allocation_reconciliation_failed")
        initial_notional = _notional(
            fill["unit_price"],
            attempt["initial_candidate_size"],
        )
        fill_notional = _integer(fill["notional_fen"])
        ordinary = target_notional - initial_notional
        reduction = initial_notional - fill_notional
        if ordinary < 0 or reduction < 0:
            raise GateEAuditError("allocation_reconciliation_failed")
        ordinary_rounding += ordinary
        fee_reduction += reduction
        invested_by_target += fill_notional
    if invested_by_target != invested:
        raise GateEAuditError("allocation_reconciliation_failed")
    if gross_target != (
        invested
        + allocation_rounding
        + ordinary_rounding
        + fee_reduction
        + pending
        + expired
    ):
        raise GateEAuditError("allocation_reconciliation_failed")

    metric_terms = {
        "gross_target_notional_fen": gross_target,
        "allocation_rounding_remainder_fen": allocation_rounding,
        "invested_notional_fen": invested,
        "ordinary_lot_rounding_fen": ordinary_rounding,
        "fee_lot_reduction_fen": fee_reduction,
        "rejected_uninvested_fen": pending,
        "expired_uninvested_fen": expired,
        "planned_cash_reserve_fen": initial_cash - gross_target,
        "total_paid_fees_fen": paid_fees,
        "trade_count": len(fills),
        "rejected_attempt_count": sum(
            row["status"] == "rejected" for row in rows["orders.csv"]
        ),
    }
    for name, expected in metric_terms.items():
        if _json_integer(metrics.get(name)) != expected:
            raise GateEAuditError("allocation_reconciliation_failed")
    return {
        "allocation_rounding": allocation_rounding,
        "dividend_cash": dividend_cash,
        "ending_cash": ending[1],
        "ending_equity": ending[4],
        "ending_position": ending_positions,
        "ending_receivable": ending_receivables,
        "expired": expired,
        "fee_reduction": fee_reduction,
        "gross_target": gross_target,
        "initial_cash": initial_cash,
        "invested": invested,
        "ordinary_rounding": ordinary_rounding,
        "paid_fees": paid_fees,
        "pending": pending,
    }


def _risk_metrics(
    returns: tuple[Decimal, ...],
) -> tuple[Decimal | None, Decimal | None]:
    if len(returns) < 2:
        return None, None
    mean = sum(returns, start=_ZERO) / len(returns)
    variance = (
        sum(((item - mean) ** 2 for item in returns), start=_ZERO)
        / (len(returns) - 1)
    )
    standard_deviation = variance.sqrt()
    if standard_deviation == 0:
        return _published(_ZERO), None
    annualizer = Decimal(_ANNUAL_SESSIONS).sqrt()
    return (
        _published(standard_deviation * annualizer),
        _published(mean / standard_deviation * annualizer),
    )


def _audit_metrics(
    rows: Mapping[str, Sequence[Mapping[str, str]]],
    run_id: str,
    metrics: Mapping[str, object],
    accounting: Mapping[str, int],
) -> None:
    equity = [
        {
            "session": _date(row["session"]),
            "equity": _integer(row["equity_fen"]),
            "position": _integer(row["position_market_value_fen"]),
        }
        for row in rows["equity.csv"]
    ]
    initial = accounting["initial_cash"]
    daily_equities = tuple(item["equity"] for item in equity)
    previous = Decimal(initial)
    returns: list[Decimal] = []
    for value in daily_equities:
        current = Decimal(value)
        if current <= 0:
            raise GateEAuditError("metric_recomputation_failed")
        returns.append(current / previous - _ONE)
        previous = current
    total_return = Decimal(daily_equities[-1]) / Decimal(initial) - _ONE
    try:
        with localcontext(Context(prec=80, rounding=ROUND_HALF_UP)):
            annualized_return = (
                (_ONE + total_return)
                ** (Decimal(_ANNUAL_SESSIONS) / len(returns))
                - _ONE
            )
            volatility, sharpe = _risk_metrics(tuple(returns))
    except DecimalException as exc:
        raise GateEAuditError("metric_recomputation_failed") from exc
    peak = Decimal(initial)
    worst = _ZERO
    for value in daily_equities:
        current = Decimal(value)
        peak = max(peak, current)
        worst = min(worst, current / peak - _ONE)

    targets = rows["targets.csv"]
    symbols = tuple(sorted(row["symbol"] for row in targets))
    target_weights = {
        row["symbol"]: Decimal(_integer(row["target_notional_fen"]))
        / Decimal(initial)
        for row in targets
    }
    positions_by_session: dict[date, dict[str, int]] = defaultdict(dict)
    for row in rows["positions.csv"]:
        positions_by_session[_date(row["session"])][row["symbol"]] = _integer(
            row["market_value_fen"]
        )
    exposures: list[tuple[date, Decimal]] = []
    max_symbol_weight = _ZERO
    max_deviation = _ZERO
    final_deviations: list[tuple[str, Decimal]] = []
    for item in equity:
        total = Decimal(item["equity"])
        exposure = Decimal(item["position"]) / total
        exposures.append((item["session"], _published(exposure)))
        deviations: list[tuple[str, Decimal]] = []
        for symbol in symbols:
            actual = Decimal(
                positions_by_session[item["session"]].get(symbol, 0)
            ) / total
            deviation = actual - target_weights[symbol]
            max_symbol_weight = max(max_symbol_weight, abs(actual))
            max_deviation = max(max_deviation, abs(deviation))
            deviations.append((symbol, _published(deviation)))
        final_deviations = deviations
    average_equity = (
        sum((Decimal(value) for value in daily_equities), start=_ZERO)
        / len(daily_equities)
    )
    turnover = Decimal(accounting["invested"]) / average_equity

    decimal_metrics = {
        "annualized_return": _published(annualized_return),
        "max_drawdown": _published(worst),
        "max_gross_exposure": max(
            (value for _session, value in exposures),
            default=_ZERO,
        ),
        "max_symbol_weight": _published(max_symbol_weight),
        "max_target_weight_deviation": _published(max_deviation),
        "risk_free_rate": _ZERO,
        "total_return": _published(total_return),
        "turnover": _published(turnover),
    }
    for name, expected in decimal_metrics.items():
        if _decimal(metrics.get(name)) != expected:
            raise GateEAuditError("metric_recomputation_failed")
    nullable_metrics = {
        "annualized_volatility": volatility,
        "sharpe_zero_rate": sharpe,
    }
    for name, expected in nullable_metrics.items():
        value = metrics.get(name)
        if (
            expected is None
            and value is not None
            or expected is not None
            and _decimal(value) != expected
        ):
            raise GateEAuditError("metric_recomputation_failed")
    if (
        metrics.get("run_id") != run_id
        or metrics.get("schema_version") != "0.2.0"
        or metrics.get("research_only") is not True
        or metrics.get("live_trading") is not False
        or metrics.get("profit_claim") is not False
        or _json_integer(metrics.get("annual_sessions"))
        != _ANNUAL_SESSIONS
        or _json_integer(metrics.get("observation_count"))
        != len(equity)
        or _json_integer(metrics.get("observed_return_count"))
        != len(returns)
    ):
        raise GateEAuditError("metric_recomputation_failed")
    persisted_exposure = metrics.get("daily_gross_exposure")
    if type(persisted_exposure) is not list or len(persisted_exposure) != len(
        exposures
    ):
        raise GateEAuditError("metric_recomputation_failed")
    for persisted, expected in zip(
        persisted_exposure,
        exposures,
        strict=True,
    ):
        if (
            type(persisted) is not dict
            or set(persisted) != {"session", "value"}
            or _date(persisted["session"]) != expected[0]
            or _decimal(persisted["value"]) != expected[1]
        ):
            raise GateEAuditError("metric_recomputation_failed")
    persisted_deviations = metrics.get("final_symbol_weight_deviations")
    if (
        type(persisted_deviations) is not list
        or len(persisted_deviations) != len(final_deviations)
    ):
        raise GateEAuditError("metric_recomputation_failed")
    for persisted, expected in zip(
        persisted_deviations,
        final_deviations,
        strict=True,
    ):
        if (
            type(persisted) is not dict
            or set(persisted) != {"symbol", "value"}
            or persisted["symbol"] != expected[0]
            or _decimal(persisted["value"]) != expected[1]
        ):
            raise GateEAuditError("metric_recomputation_failed")


def audit_gate_e_bundle(
    directory: Path,
    *,
    expected_run_id: str | None,
) -> GateEAccountingAudit:
    """Audit one candidate without accepting its persisted metrics as proof."""
    if expected_run_id is not None and (
        type(expected_run_id) is not str
        or _HASH_RE.fullmatch(expected_run_id) is None
    ):
        raise GateEAuditError("invalid_expected_run_id")
    contents = _read_bundle(directory)
    run = _parse_json(contents["run.json"])
    metrics = _parse_json(contents["metrics.json"])
    rows = {
        name: _parse_csv(name, contents[name])
        for name in sorted(_CSV_SCHEMAS)
    }
    run_id = _verify_manifest(
        contents,
        rows,
        run,
        metrics,
        expected_run_id=expected_run_id,
    )
    config = run.get("config")
    if type(config) is not dict:
        raise GateEAuditError("invalid_run_contract")
    end = _date(config.get("end_date"))
    latest_plan = _check_date_boundary(
        rows,
        run,
        metrics,
        end_date=end,
    )
    equity = rows["equity.csv"]
    if not equity or _date(equity[-1]["session"]) != end:
        raise GateEAuditError("equity_reconciliation_failed")
    no_bar_dates, carried = _audit_no_bar_rows(rows["availability.csv"])
    accounting = _cash_and_accounting(rows, run, metrics)
    _audit_metrics(rows, run_id, metrics, accounting)
    try:
        verify_portfolio_artifact(
            directory,
            expected_run_id=expected_run_id,
        )
    except PortfolioArtifactError as exc:
        raise GateEAuditError(
            "portfolio_artifact_invalid",
            cause_code=exc.code,
        ) from exc
    return GateEAccountingAudit(
        run_id=run_id,
        end_date=end,
        initial_cash_fen=accounting["initial_cash"],
        invested_notional_fen=accounting["invested"],
        paid_fees_fen=accounting["paid_fees"],
        dividend_cash_paid_fen=accounting["dividend_cash"],
        ending_cash_fen=accounting["ending_cash"],
        gross_target_notional_fen=accounting["gross_target"],
        allocation_rounding_fen=accounting["allocation_rounding"],
        ordinary_lot_rounding_fen=accounting["ordinary_rounding"],
        fee_lot_reduction_fen=accounting["fee_reduction"],
        pending_uninvested_fen=accounting["pending"],
        expired_uninvested_fen=accounting["expired"],
        ending_position_market_value_fen=accounting["ending_position"],
        ending_receivable_fen=accounting["ending_receivable"],
        ending_equity_fen=accounting["ending_equity"],
        observation_count=len(equity),
        latest_plan_date=latest_plan,
        no_bar_dates=no_bar_dates,
        no_bar_carried_sessions=carried,
    )


def _safe_input_file(
    project_root: Path,
    relative: str,
    expected_sha256: str,
) -> bytes:
    root = Path(os.path.abspath(project_root))
    path = root / relative
    if path.parent == path:
        raise GateEAuditError("unsafe_input_root")
    descriptor = _safe_directory(path.parent)
    try:
        return _read_named_file(
            descriptor,
            path.name,
            expected_sha256=expected_sha256,
        )
    finally:
        os.close(descriptor)


def _read_gate_e_market_dates(
    project_root: Path,
    record: ManifestRecord,
    input_files: Mapping[str, str],
) -> frozenset[date]:
    """Independently validate one frozen market snapshot without writes."""
    relative = record.snapshot_relative_path.as_posix()
    expected_sha256 = input_files.get(relative)
    if (
        type(expected_sha256) is not str
        or expected_sha256 != record.file_sha256
        or record.adjustment != ""
        or record.factor_source is not None
        or any(record.quality_issue_counts.values())
    ):
        raise GateEAuditError("market_snapshot_mismatch")
    try:
        source_schema = SourceSchema(record.source_schema)
        validate_source_contract(
            symbol=record.symbol,
            instrument_kind=record.instrument_kind,
            provider=record.provider,
            source_function=record.source_function,
            source_schema=source_schema,
            endpoint_host=record.endpoint_host,
            provider_symbol=record.provider_symbol,
            raw_volume_unit=record.raw_volume_unit,
            volume_multiplier_to_canonical=(
                record.volume_multiplier_to_canonical
            ),
            full_history_download=record.full_history_download,
            local_date_slice=record.local_date_slice,
        )
        content = _safe_input_file(
            project_root,
            relative,
            expected_sha256,
        )
        raw = pd.read_parquet(io.BytesIO(content))
        canonical = normalize_market_frame(
            raw,
            source_schema=source_schema,
        )
        report = validate_market_frame(canonical)
    except (
        DataQualityError,
        NormalizationError,
        OSError,
        SourceContractError,
        TypeError,
        ValueError,
        pa.ArrowException,
    ) as exc:
        raise GateEAuditError("market_snapshot_mismatch") from exc
    if (
        report.row_count != record.row_count
        or report.start_date != record.actual_start
        or report.end_date != record.actual_end
    ):
        raise GateEAuditError("market_snapshot_mismatch")
    return frozenset(canonical["date"].dt.date)


def audit_gate_e_inputs(
    config: GateEConfig,
    project_root: Path,
) -> GateEInputAudit:
    """Reconstruct exact missing sessions from frozen calendar and market bars."""
    try:
        verify_gate_e_config(config)
    except (GateEConfigError, TypeError) as exc:
        raise GateEAuditError("unverified_gate_e_config") from exc
    if not isinstance(project_root, Path):
        raise TypeError("project_root must be a Path")
    payload = config.payload
    input_files = payload["input_files"]
    if not isinstance(input_files, Mapping):
        raise GateEAuditError("input_closure_mismatch")
    for relative, digest in input_files.items():
        _safe_input_file(project_root, relative, digest)

    calendar_relative = (
        f"data/calendars/{payload['calendar_id']}.json"
    )
    calendar_content = _safe_input_file(
        project_root,
        calendar_relative,
        input_files[calendar_relative],
    )
    try:
        calendar_payload = json.loads(
            calendar_content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        raw_dates = calendar_payload["dates"]
        if (
            type(calendar_payload) is not dict
            or set(calendar_payload) != {"dates", "schema_version"}
            or calendar_payload["schema_version"] != "1.0"
            or type(raw_dates) is not list
        ):
            raise ValueError
        calendar_dates = tuple(_date(value) for value in raw_dates)
    except (GateEAuditError, KeyError, TypeError, ValueError) as exc:
        raise GateEAuditError("invalid_calendar_input") from exc
    if (
        hashlib.sha256(calendar_content).hexdigest()
        != payload["calendar_id"]
        or any(
            left >= right
            for left, right in zip(
                calendar_dates,
                calendar_dates[1:],
                strict=False,
            )
        )
    ):
        raise GateEAuditError("invalid_calendar_input")
    sessions = tuple(
        item
        for item in calendar_dates
        if config.signal_date < item <= config.end_date
    )
    if not sessions or sessions[-1] != config.end_date:
        raise GateEAuditError("calendar_scope_mismatch")

    manifest_path = payload["manifest"]
    expected_manifest_hash = input_files[manifest_path]
    try:
        records = read_frozen_manifest(
            project_root / manifest_path,
            expected_sha256=expected_manifest_hash,
        )
    except ManifestError as exc:
        raise GateEAuditError(
            "invalid_market_manifest",
            cause_code=exc.code,
        ) from exc
    by_id = {record.snapshot_id: record for record in records}
    if len(by_id) != len(records):
        raise GateEAuditError("invalid_market_manifest")

    missing_by_symbol: list[tuple[str, tuple[date, ...]]] = []
    carried_evidence: list[tuple[str, date, int]] = []
    for symbol in payload["symbols"]:
        snapshot_id = payload["market_snapshots"][symbol]
        record = by_id.get(snapshot_id)
        if record is None or record.symbol != symbol:
            raise GateEAuditError("market_snapshot_mismatch")
        frame_dates = _read_gate_e_market_dates(
            project_root,
            record,
            input_files,
        )
        missing: list[date] = []
        carried = 0
        for session in sessions:
            if session in frame_dates:
                carried = 0
            else:
                carried += 1
                missing.append(session)
                carried_evidence.append((symbol, session, carried))
        missing_by_symbol.append((symbol, tuple(missing)))
    return GateEInputAudit(
        session_count=len(sessions),
        no_bar_dates=tuple(missing_by_symbol),
        no_bar_carried_sessions=tuple(carried_evidence),
    )


def reconcile_gate_e_no_bar(
    actual_dates: tuple[tuple[str, tuple[date, ...]], ...],
    actual_carried: tuple[tuple[str, date, int], ...],
    expected_dates: tuple[tuple[str, tuple[date, ...]], ...],
    expected_carried: tuple[tuple[str, date, int], ...],
) -> None:
    """Require exact calendar-minus-bars and persisted availability equality."""
    if (
        type(actual_dates) is not tuple
        or type(actual_carried) is not tuple
        or type(expected_dates) is not tuple
        or type(expected_carried) is not tuple
        or actual_dates != expected_dates
        or actual_carried != expected_carried
    ):
        raise GateEAuditError("no_bar_input_output_mismatch")
