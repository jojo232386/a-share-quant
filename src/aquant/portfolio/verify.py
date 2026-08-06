"""Independent, read-only verification of portfolio audit bundles."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
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

from aquant.portfolio.contracts import (
    BUDGET_MODE,
    DIVIDEND_TAX_MODE,
    NO_BAR_VALUATION_MODE,
    PORTFOLIO_ENGINE,
    PORTFOLIO_SCHEMA_VERSION,
    PRICE_STREAM_VERSION,
    RETRY_MODE,
)

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SYMBOL_RE = re.compile(r"[0-9]{6}")
_UNIVERSE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MAIN_BOARD_SYMBOL_RE = re.compile(r"(?:60[0135][0-9]{3}|00[0123][0-9]{3})")
_ETF_SYMBOL_RE = re.compile(r"5[0-9]{5}")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_INTEGER_RE = re.compile(r"0|[1-9][0-9]*")
_ANNUAL_SESSIONS = 252
_METRIC_QUANTUM = Decimal("0.000000000001")
_ZERO = Decimal(0)
_ONE = Decimal(1)

_BEHAVIOR_MODES = {
    "budget_mode": BUDGET_MODE,
    "dividend_tax_mode": DIVIDEND_TAX_MODE,
    "no_bar_valuation_mode": NO_BAR_VALUATION_MODE,
    "price_stream_version": PRICE_STREAM_VERSION,
    "retry_mode": RETRY_MODE,
}

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


class PortfolioArtifactError(RuntimeError):
    """Stable fail-closed error for a persisted portfolio artifact."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class VerifiedPortfolioArtifact:
    """Small verified summary reconstructed only from persisted bytes."""

    run_id: str
    status: str
    artifact_manifest_sha256: str
    artifact_file_count: int
    payload_file_count: int
    file_count: int
    trade_count: int
    row_counts: tuple[tuple[str, int], ...]


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


@contextmanager
def _open_artifact_directory(
    directory: str | Path,
) -> Iterator[tuple[Path, int, int, os.stat_result, ExitStack]]:
    if not isinstance(directory, (str, os.PathLike)):
        raise TypeError("directory must be a string or path-like object")
    path = Path(os.path.abspath(os.fspath(directory)))
    if path == Path(path.anchor):
        raise PortfolioArtifactError(
            "unsafe_artifact",
            "portfolio artifact must be a named real directory",
        )
    parent_descriptor: int | None = None
    artifact_descriptor: int | None = None
    payload_stack = ExitStack()
    try:
        parent_descriptor = os.open(
            path.anchor,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
        )
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                dir_fd=parent_descriptor,
            )
            entry = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            opened = os.fstat(child)
            if not stat.S_ISDIR(entry.st_mode) or not _same_object(entry, opened):
                os.close(child)
                raise PortfolioArtifactError(
                    "unsafe_artifact",
                    "portfolio artifact path contains an unsafe directory",
                )
            os.close(parent_descriptor)
            parent_descriptor = child
        entry = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(entry.st_mode):
            raise PortfolioArtifactError(
                "unsafe_artifact",
                "portfolio artifact must be a real directory",
            )
        artifact_descriptor = os.open(
            path.name,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(artifact_descriptor)
        if not _same_object(entry, opened):
            raise PortfolioArtifactError(
                "unsafe_artifact",
                "portfolio artifact directory binding changed",
            )
    except PortfolioArtifactError:
        if artifact_descriptor is not None:
            os.close(artifact_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        raise
    except OSError as exc:
        if artifact_descriptor is not None:
            os.close(artifact_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        raise PortfolioArtifactError(
            "unsafe_artifact",
            "portfolio artifact cannot be opened safely",
        ) from exc
    try:
        yield (
            path,
            parent_descriptor,
            artifact_descriptor,
            opened,
            payload_stack,
        )
    finally:
        payload_stack.close()
        os.close(artifact_descriptor)
        os.close(parent_descriptor)


def _verify_artifact_binding(
    path: Path,
    parent_descriptor: int,
    opened: os.stat_result,
) -> None:
    try:
        current = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PortfolioArtifactError(
            "unsafe_artifact",
            "portfolio artifact directory binding changed",
        ) from exc
    if resolved != path or not stat.S_ISDIR(current.st_mode) or not _same_object(current, opened):
        raise PortfolioArtifactError(
            "unsafe_artifact",
            "portfolio artifact directory binding changed",
        )


@contextmanager
def _open_safe_file(
    directory_descriptor: int,
    name: str,
) -> Iterator[tuple[bytes, os.stat_result, int]]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | _NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PortfolioArtifactError(
                "unsafe_artifact",
                "portfolio artifact payload must be a single-link regular file",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        reopened = os.fstat(descriptor)
        current = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(reopened.st_mode)
            or reopened.st_nlink != 1
            or not _same_object(metadata, reopened)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or not _same_object(metadata, current)
        ):
            raise PortfolioArtifactError(
                "unsafe_artifact",
                "portfolio artifact payload binding changed",
            )
        yield b"".join(chunks), metadata, descriptor
    except PortfolioArtifactError:
        raise
    except OSError as exc:
        raise PortfolioArtifactError(
            "unsafe_artifact",
            "portfolio artifact payload cannot be read safely",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_file_binding(
    directory_descriptor: int,
    name: str,
    opened: os.stat_result,
    descriptor: int,
    expected_content: bytes,
) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        reopened = os.fstat(descriptor)
        current = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise PortfolioArtifactError(
            "unsafe_artifact",
            "portfolio artifact payload binding changed",
        ) from exc
    if (
        b"".join(chunks) != expected_content
        or not stat.S_ISREG(reopened.st_mode)
        or reopened.st_nlink != 1
        or not _same_object(reopened, opened)
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or not _same_object(current, opened)
    ):
        raise PortfolioArtifactError(
            "unsafe_artifact",
            "portfolio artifact payload binding changed",
        )


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PortfolioArtifactError(
                "noncanonical_json",
                "portfolio JSON contains a duplicate key",
            )
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise PortfolioArtifactError(
        "noncanonical_json",
        "portfolio JSON contains a non-finite number",
    )


def _canonical_json_bytes(value: object) -> bytes:
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
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PortfolioArtifactError(
            "noncanonical_json",
            "portfolio JSON cannot be canonicalized",
        ) from exc


def _parse_json(content: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except PortfolioArtifactError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PortfolioArtifactError(
            "noncanonical_json",
            "portfolio JSON is not canonical UTF-8",
        ) from exc
    if type(parsed) is not dict or _canonical_json_bytes(parsed) != content:
        raise PortfolioArtifactError(
            "noncanonical_json",
            "portfolio JSON is not in canonical form",
        )
    return parsed


def _parse_csv(
    filename: str,
    content: bytes,
) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8")
        reader = csv.DictReader(
            io.StringIO(text, newline=""),
            strict=True,
        )
        rows = list(reader)
    except (UnicodeError, csv.Error) as exc:
        raise PortfolioArtifactError(
            "noncanonical_csv",
            "portfolio CSV is malformed",
        ) from exc
    fields = _CSV_SCHEMAS[filename]
    if tuple(reader.fieldnames or ()) != fields or any(
        set(row) != set(fields) or None in row for row in rows
    ):
        raise PortfolioArtifactError(
            "invalid_artifact_schema",
            "portfolio CSV header or row shape is invalid",
        )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=fields,
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    if stream.getvalue().encode("utf-8") != content:
        raise PortfolioArtifactError(
            "noncanonical_csv",
            "portfolio CSV is not in canonical form",
        )
    return rows


def _exact_keys(
    value: object,
    expected: set[str],
    *,
    code: str,
    message: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise PortfolioArtifactError(code, message)
    return value


def _hash(value: object, *, code: str = "invalid_artifact_value") -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise PortfolioArtifactError(code, "portfolio hash value is invalid")
    return value


def _text(
    value: object,
    *,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        type(value) is not str
        or not value
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise PortfolioArtifactError(
            "invalid_artifact_value",
            "portfolio text value is invalid",
        )
    return value


def _int(
    value: object,
    *,
    positive: bool = False,
) -> int:
    if type(value) is int:
        result = value
    elif type(value) is str and _INTEGER_RE.fullmatch(value) is not None:
        result = int(value)
    else:
        raise PortfolioArtifactError(
            "invalid_artifact_value",
            "portfolio integer value is invalid",
        )
    if result < 0 or positive and result <= 0:
        raise PortfolioArtifactError(
            "invalid_artifact_value",
            "portfolio integer range is invalid",
        )
    return result


def _optional_int(value: str) -> int | None:
    return None if value == "" else _int(value)


def _date(value: object) -> date:
    if type(value) is not str:
        raise PortfolioArtifactError(
            "invalid_artifact_value",
            "portfolio date value is invalid",
        )
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise PortfolioArtifactError(
            "invalid_artifact_value",
            "portfolio date value is invalid",
        ) from exc
    if result.isoformat() != value:
        raise PortfolioArtifactError(
            "invalid_artifact_value",
            "portfolio date is not canonical",
        )
    return result


def _optional_date(value: object) -> date | None:
    return None if value in {"", None} else _date(value)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _decimal(
    value: object,
    *,
    nonnegative: bool = False,
    positive: bool = False,
) -> Decimal:
    if type(value) is not str or not value:
        raise PortfolioArtifactError(
            "invalid_artifact_value",
            "portfolio decimal value is invalid",
        )
    try:
        result = Decimal(value)
    except DecimalException as exc:
        raise PortfolioArtifactError(
            "invalid_artifact_value",
            "portfolio decimal value is invalid",
        ) from exc
    if (
        not result.is_finite()
        or _decimal_text(result) != value
        or nonnegative
        and result < 0
        or positive
        and result <= 0
    ):
        raise PortfolioArtifactError(
            "invalid_artifact_value",
            "portfolio decimal value is not canonical",
        )
    return result


def _notional_fen(unit_price: Decimal, size: int) -> int:
    try:
        rounded = (unit_price * size).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except DecimalException as exc:
        raise PortfolioArtifactError(
            "invalid_artifact_value",
            "portfolio notional cannot be represented in fen",
        ) from exc
    return int(rounded * 100)


def _published_decimal(value: Decimal) -> Decimal:
    try:
        with localcontext() as context:
            context.prec = max(80, len(value.as_tuple().digits) + 20)
            return value.quantize(
                _METRIC_QUANTUM,
                rounding=ROUND_HALF_UP,
            )
    except DecimalException as exc:
        raise PortfolioArtifactError(
            "metric_recomputation_failed",
            "portfolio metric cannot be published",
        ) from exc


def _identity_row(
    row: Mapping[str, str],
    *,
    run_id: str,
) -> None:
    if row["run_id"] != run_id or row["schema_version"] != PORTFOLIO_SCHEMA_VERSION:
        raise PortfolioArtifactError(
            "artifact_identity_mismatch",
            "portfolio payload identity is inconsistent",
        )


def _ordered_unique(
    keys: Sequence[tuple[object, ...]],
) -> None:
    if list(keys) != sorted(keys):
        raise PortfolioArtifactError(
            "invalid_artifact_order",
            "portfolio payload primary order is invalid",
        )
    if len(keys) != len(set(keys)):
        raise PortfolioArtifactError(
            "duplicate_artifact_key",
            "portfolio payload primary key is duplicated",
        )


def _validate_manifest(
    content: bytes,
) -> tuple[dict[str, object], str, dict[str, int]]:
    manifest = _exact_keys(
        _parse_json(content),
        {"artifact_schema_version", "files", "run_id", "status"},
        code="invalid_artifact_manifest",
        message="portfolio artifact manifest shape is invalid",
    )
    run_id = _hash(
        manifest["run_id"],
        code="invalid_artifact_manifest",
    )
    if (
        manifest["artifact_schema_version"] != PORTFOLIO_SCHEMA_VERSION
        or manifest["status"] != "complete"
    ):
        raise PortfolioArtifactError(
            "invalid_artifact_manifest",
            "portfolio artifact manifest contract is invalid",
        )
    files = manifest["files"]
    if type(files) is not dict or set(files) != _PAYLOAD_FILES:
        raise PortfolioArtifactError(
            "invalid_artifact_manifest",
            "portfolio artifact manifest file set is invalid",
        )
    row_counts: dict[str, int] = {}
    for filename in sorted(files):
        entry = _exact_keys(
            files[filename],
            {"row_count", "run_id", "schema_version", "sha256"},
            code="invalid_artifact_manifest",
            message="portfolio artifact manifest entry is invalid",
        )
        if entry["run_id"] != run_id or entry["schema_version"] != PORTFOLIO_SCHEMA_VERSION:
            raise PortfolioArtifactError(
                "invalid_artifact_manifest",
                "portfolio artifact manifest identity is inconsistent",
            )
        _hash(entry["sha256"], code="invalid_artifact_manifest")
        row_counts[filename] = _int(entry["row_count"])
    if row_counts["run.json"] != 1 or row_counts["metrics.json"] != 1:
        raise PortfolioArtifactError(
            "invalid_artifact_manifest",
            "portfolio JSON row counts must equal one",
        )
    return manifest, run_id, row_counts


def _validate_closure(
    run: Mapping[str, object],
    config: Mapping[str, object],
) -> tuple[str, ...]:
    closure = _exact_keys(
        run["input_closure"],
        {
            "behavior_modes",
            "calendar",
            "config",
            "corporate_actions",
            "fee_policy",
            "market_data",
            "universe",
        },
        code="invalid_run_payload",
        message="portfolio input closure shape is invalid",
    )
    if closure["behavior_modes"] != run["behavior_modes"] or closure["config"] != run["config"]:
        raise PortfolioArtifactError(
            "artifact_identity_mismatch",
            "portfolio input closure mirrors are inconsistent",
        )
    calendar = _exact_keys(
        closure["calendar"],
        {"calendar_id", "file_sha256"},
        code="invalid_run_payload",
        message="portfolio calendar closure is invalid",
    )
    if (
        _hash(calendar["calendar_id"]) != run["calendar_id"]
        or _hash(calendar["file_sha256"]) != run["calendar_sha256"]
        or calendar["calendar_id"] != calendar["file_sha256"]
    ):
        raise PortfolioArtifactError(
            "artifact_identity_mismatch",
            "portfolio calendar identity is inconsistent",
        )
    fee_policy = _exact_keys(
        closure["fee_policy"],
        {"policy_digest"},
        code="invalid_run_payload",
        message="portfolio fee closure is invalid",
    )
    if _hash(fee_policy["policy_digest"]) != run["fee_policy_digest"]:
        raise PortfolioArtifactError(
            "artifact_identity_mismatch",
            "portfolio fee identity is inconsistent",
        )
    universe = _exact_keys(
        closure["universe"],
        {"members", "name", "universe_id"},
        code="invalid_run_payload",
        message="portfolio universe closure is invalid",
    )
    if _hash(universe["universe_id"]) != run["universe_id"]:
        raise PortfolioArtifactError(
            "artifact_identity_mismatch",
            "portfolio universe identity is inconsistent",
        )
    name = _text(universe["name"], pattern=_UNIVERSE_NAME_RE)
    if type(universe["members"]) is not list or not 1 <= len(universe["members"]) <= 100:
        raise PortfolioArtifactError(
            "invalid_run_payload",
            "portfolio universe members are invalid",
        )
    members: list[tuple[str, str]] = []
    for item in universe["members"]:
        member = _exact_keys(
            item,
            {"kind", "symbol"},
            code="invalid_run_payload",
            message="portfolio universe member is invalid",
        )
        symbol = _text(member["symbol"], pattern=_SYMBOL_RE)
        kind = _enum(
            member["kind"],
            {
                "domestic_equity_broad_based_etf",
                "main_board_stock",
            },
        )
        pattern = (
            _ETF_SYMBOL_RE if kind == "domestic_equity_broad_based_etf" else _MAIN_BOARD_SYMBOL_RE
        )
        if pattern.fullmatch(symbol) is None:
            raise PortfolioArtifactError(
                "invalid_run_payload",
                "portfolio universe member identity is unsupported",
            )
        members.append((symbol, kind))
    if len(members) != len(set(members)):
        raise PortfolioArtifactError(
            "invalid_run_payload",
            "portfolio universe members are duplicated",
        )
    canonical_universe = {
        "members": [{"kind": kind, "symbol": symbol} for symbol, kind in members],
        "name": name,
        "schema_version": "1.0",
    }
    if (
        hashlib.sha256(_canonical_json_bytes(canonical_universe)).hexdigest()
        != universe["universe_id"]
    ):
        raise PortfolioArtifactError(
            "artifact_identity_mismatch",
            "portfolio universe ID cannot be reconstructed",
        )
    symbols = tuple(sorted(symbol for symbol, _kind in members))
    kinds = dict(members)
    for field, expected_keys in (
        (
            "market_data",
            {
                "adjustment",
                "file_sha256",
                "input_digest",
                "instrument_kind",
                "snapshot_id",
                "symbol",
                "verification_method",
            },
        ),
        (
            "corporate_actions",
            {
                "coverage_end",
                "coverage_start",
                "file_sha256",
                "instrument_kind",
                "normalization_version",
                "provider",
                "row_count",
                "snapshot_id",
                "source_schema",
                "symbol",
                "verification_method",
            },
        ),
    ):
        values = closure[field]
        if type(values) is not list:
            raise PortfolioArtifactError(
                "invalid_run_payload",
                "portfolio source closure is invalid",
            )
        source_symbols: list[str] = []
        for value in values:
            source = _exact_keys(
                value,
                expected_keys,
                code="invalid_run_payload",
                message="portfolio source closure entry is invalid",
            )
            symbol = _text(source["symbol"], pattern=_SYMBOL_RE)
            source_symbols.append(symbol)
            _hash(source["file_sha256"])
            _hash(source["snapshot_id"])
            kind = _text(source["instrument_kind"])
            verification_method = _text(source["verification_method"])
            if kind != kinds[symbol]:
                raise PortfolioArtifactError(
                    "artifact_identity_mismatch",
                    "portfolio source kind does not match the universe",
                )
            if field == "market_data":
                _hash(source["input_digest"])
                if source["adjustment"] != "" or verification_method != "manifest_sha256":
                    raise PortfolioArtifactError(
                        "invalid_run_payload",
                        "portfolio market source contract is invalid",
                    )
            else:
                coverage_start = _date(source["coverage_start"])
                coverage_end = _date(source["coverage_end"])
                _int(source["row_count"])
                for text_field in (
                    "normalization_version",
                    "provider",
                    "source_schema",
                ):
                    _text(source[text_field])
                if (
                    coverage_start > config["signal_date"]
                    or coverage_end < config["end_date"]
                    or source["normalization_version"] != "cash-only-v1"
                    or verification_method not in {"manifest_sha256", "synthetic_digest"}
                ):
                    raise PortfolioArtifactError(
                        "invalid_run_payload",
                        "portfolio corporate-action coverage is invalid",
                    )
        if tuple(source_symbols) != symbols:
            raise PortfolioArtifactError(
                "artifact_identity_mismatch",
                "portfolio source symbols do not match the universe",
            )
    closure_digest = hashlib.sha256(_canonical_json_bytes(closure)).hexdigest()
    if closure_digest != run["input_closure_digest"]:
        raise PortfolioArtifactError(
            "artifact_identity_mismatch",
            "portfolio input closure digest is inconsistent",
        )
    return symbols


def _validate_run(
    content: bytes,
    *,
    manifest_run_id: str,
    manifest_row_counts: Mapping[str, int],
    directory_name: str,
    expected_run_id: str | None,
) -> tuple[dict[str, object], dict[str, object], tuple[str, ...]]:
    run = _exact_keys(
        _parse_json(content),
        {
            "behavior_modes",
            "calendar_id",
            "calendar_sha256",
            "config",
            "engine",
            "fee_policy_digest",
            "implementation_digest",
            "input_closure",
            "input_closure_digest",
            "result_digest",
            "row_counts",
            "run_id",
            "schema_version",
            "touched_fee_rates",
            "universe_id",
        },
        code="invalid_run_payload",
        message="portfolio run payload shape is invalid",
    )
    run_id = _hash(run["run_id"])
    for field in (
        "calendar_id",
        "calendar_sha256",
        "fee_policy_digest",
        "implementation_digest",
        "input_closure_digest",
        "result_digest",
        "universe_id",
    ):
        _hash(run[field])
    if (
        run["schema_version"] != PORTFOLIO_SCHEMA_VERSION
        or run["engine"] != PORTFOLIO_ENGINE
        or run["behavior_modes"] != _BEHAVIOR_MODES
        or run_id != manifest_run_id
        or directory_name != run_id
        or expected_run_id is not None
        and expected_run_id != run_id
    ):
        raise PortfolioArtifactError(
            "artifact_identity_mismatch",
            "portfolio run identity is inconsistent",
        )
    row_counts = run["row_counts"]
    if (
        type(row_counts) is not dict
        or set(row_counts) != _PAYLOAD_FILES
        or any(type(value) is not int or value < 0 for value in row_counts.values())
        or row_counts != dict(manifest_row_counts)
    ):
        raise PortfolioArtifactError(
            "artifact_row_count_mismatch",
            "portfolio run row counts are inconsistent",
        )
    config = _exact_keys(
        run["config"],
        {
            "end_date",
            "gross_target_weight",
            "initial_cash_fen",
            "max_entry_attempts",
            "signal_date",
            "strategy",
        },
        code="invalid_run_payload",
        message="portfolio run config is invalid",
    )
    parsed_config: dict[str, object] = {
        "end_date": _date(config["end_date"]),
        "gross_target_weight": _decimal(config["gross_target_weight"], positive=True),
        "initial_cash_fen": _int(config["initial_cash_fen"], positive=True),
        "max_entry_attempts": _int(config["max_entry_attempts"], positive=True),
        "signal_date": _date(config["signal_date"]),
        "strategy": config["strategy"],
    }
    if (
        parsed_config["strategy"] != "buy_and_hold"
        or parsed_config["gross_target_weight"] > _ONE
        or parsed_config["max_entry_attempts"] > 20
        or parsed_config["end_date"] <= parsed_config["signal_date"]
    ):
        raise PortfolioArtifactError(
            "invalid_run_payload",
            "portfolio run config values are invalid",
        )
    symbols = _validate_closure(run, parsed_config)
    identity_payload = {
        "engine": PORTFOLIO_ENGINE,
        "implementation_digest": run["implementation_digest"],
        "input_closure_digest": run["input_closure_digest"],
        "result_digest": run["result_digest"],
        "schema_version": PORTFOLIO_SCHEMA_VERSION,
    }
    recomputed_run_id = hashlib.sha256(_canonical_json_bytes(identity_payload)).hexdigest()
    if recomputed_run_id != run_id:
        raise PortfolioArtifactError(
            "artifact_identity_mismatch",
            "portfolio run ID cannot be reconstructed",
        )
    touched = run["touched_fee_rates"]
    if type(touched) is not list:
        raise PortfolioArtifactError(
            "invalid_run_payload",
            "portfolio touched fee records are invalid",
        )
    previous_key: tuple[str, str, str, str] | None = None
    for item in touched:
        record = _exact_keys(
            item,
            {
                "attempt_id",
                "effective_date",
                "execution_session",
                "fee_name",
                "minimum_yuan",
                "rate",
                "symbol",
            },
            code="invalid_run_payload",
            message="portfolio touched fee record is invalid",
        )
        attempt_id = _text(record["attempt_id"], pattern=_IDENTIFIER_RE)
        symbol = _text(record["symbol"], pattern=_SYMBOL_RE)
        execution = _date(record["execution_session"])
        fee_name = _text(record["fee_name"])
        _optional_date(record["effective_date"])
        _decimal(record["rate"], nonnegative=True)
        if record["minimum_yuan"] is not None:
            _decimal(record["minimum_yuan"], nonnegative=True)
        key = (
            attempt_id,
            symbol,
            execution.isoformat(),
            fee_name,
        )
        if previous_key is not None and key < previous_key:
            raise PortfolioArtifactError(
                "invalid_artifact_order",
                "portfolio touched fee order is invalid",
            )
        previous_key = key
    return run, parsed_config, symbols


def _optional_text(
    value: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> str | None:
    return None if value == "" else _text(value, pattern=pattern)


def _enum(value: str, allowed: set[str]) -> str:
    if value not in allowed:
        raise PortfolioArtifactError(
            "invalid_artifact_value",
            "portfolio enum value is invalid",
        )
    return value


def _parse_targets(
    rows: Sequence[Mapping[str, str]],
    *,
    run_id: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        _identity_row(row, run_id=run_id)
        status = _enum(
            row["status"],
            {"pending", "filled", "expired_unfilled"},
        )
        fill_event_id = _optional_text(
            row["fill_event_id"],
            pattern=_IDENTIFIER_RE,
        )
        if (status == "filled") != (fill_event_id is not None):
            raise PortfolioArtifactError(
                "invalid_artifact_value",
                "portfolio target fill state is invalid",
            )
        result.append(
            {
                "attempts_used": _int(row["attempts_used"]),
                "fill_event_id": fill_event_id,
                "signal_date": _date(row["signal_date"]),
                "status": status,
                "symbol": _text(row["symbol"], pattern=_SYMBOL_RE),
                "target_id": _text(
                    row["target_id"],
                    pattern=_IDENTIFIER_RE,
                ),
                "target_notional_fen": _int(
                    row["target_notional_fen"],
                    positive=True,
                ),
            }
        )
    _ordered_unique([(item["symbol"],) for item in result])
    target_ids = [item["target_id"] for item in result]
    if len(target_ids) != len(set(target_ids)):
        raise PortfolioArtifactError(
            "duplicate_artifact_key",
            "portfolio target ID is duplicated",
        )
    return result


def _parse_orders(
    rows: Sequence[Mapping[str, str]],
    *,
    run_id: str,
) -> list[dict[str, object]]:
    rejection_reasons = {
        "unsupported_instrument",
        "missing_calendar_coverage",
        "no_next_session_in_range",
        "suspended_no_bar",
        "missing_previous_close",
        "price_limit_open",
        "invalid_lot_size",
        "insufficient_cash",
        "insufficient_sellable_position",
        "missing_fee_schedule",
        "invalid_fee_configuration",
    }
    result: list[dict[str, object]] = []
    for row in rows:
        _identity_row(row, run_id=run_id)
        status = _enum(row["status"], {"filled", "rejected"})
        initial_size = _int(row["initial_candidate_size"])
        requested_size = _int(row["requested_size"])
        if initial_size % 100 != 0 or requested_size % 100 != 0 or requested_size > initial_size:
            raise PortfolioArtifactError(
                "invalid_artifact_value",
                "portfolio order size is invalid",
            )
        rejection = _optional_text(row["rejection_reason"])
        fill_event_id = _optional_text(
            row["fill_event_id"],
            pattern=_IDENTIFIER_RE,
        )
        cash_before = _optional_int(row["cash_available_before_fen"])
        initial_required = _optional_int(row["initial_candidate_cash_required_fen"])
        requested_required = _optional_int(row["requested_cash_required_fen"])
        adjustment = _optional_text(row["quantity_adjustment_reason"])
        availability_status = _enum(
            row["availability_status"],
            {"available", "no_bar_unavailable"},
        )
        if status == "filled":
            if (
                availability_status != "available"
                or requested_size <= 0
                or rejection is not None
                or fill_event_id is None
                or cash_before is None
                or initial_required is None
                or requested_required is None
                or initial_required < requested_required
                or cash_before < requested_required
            ):
                raise PortfolioArtifactError(
                    "invalid_artifact_value",
                    "filled portfolio order evidence is invalid",
                )
            if initial_size == requested_size:
                if adjustment is not None or initial_required != requested_required:
                    raise PortfolioArtifactError(
                        "invalid_artifact_value",
                        "unadjusted portfolio order evidence is invalid",
                    )
            elif (
                adjustment != "insufficient_cash_including_fees"
                or cash_before >= initial_required
                or initial_required <= requested_required
            ):
                raise PortfolioArtifactError(
                    "invalid_artifact_value",
                    "fee-reduced portfolio order evidence is invalid",
                )
        elif (
            rejection not in rejection_reasons
            or fill_event_id is not None
            or cash_before is not None
            or initial_required is not None
            or requested_required is not None
            or adjustment is not None
        ):
            raise PortfolioArtifactError(
                "invalid_artifact_value",
                "rejected portfolio order evidence is invalid",
            )
        result.append(
            {
                "attempt_id": _text(
                    row["attempt_id"],
                    pattern=_IDENTIFIER_RE,
                ),
                "attempt_number": _int(
                    row["attempt_number"],
                    positive=True,
                ),
                "availability_status": availability_status,
                "cash_available_before_fen": cash_before,
                "execution_session": _date(row["execution_session"]),
                "fill_event_id": fill_event_id,
                "initial_candidate_cash_required_fen": initial_required,
                "initial_candidate_size": initial_size,
                "intent_session": _date(row["intent_session"]),
                "original_signal_date": _date(row["original_signal_date"]),
                "quantity_adjustment_reason": adjustment,
                "rejection_reason": rejection,
                "requested_cash_required_fen": requested_required,
                "requested_size": requested_size,
                "status": status,
                "symbol": _text(row["symbol"], pattern=_SYMBOL_RE),
                "target_id": _text(
                    row["target_id"],
                    pattern=_IDENTIFIER_RE,
                ),
            }
        )
    _ordered_unique(
        [
            (
                item["execution_session"],
                item["symbol"],
                item["attempt_number"],
            )
            for item in result
        ]
    )
    attempt_ids = [item["attempt_id"] for item in result]
    if len(attempt_ids) != len(set(attempt_ids)):
        raise PortfolioArtifactError(
            "duplicate_artifact_key",
            "portfolio attempt ID is duplicated",
        )
    return result


def _parse_fills(
    rows: Sequence[Mapping[str, str]],
    *,
    run_id: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        _identity_row(row, run_id=run_id)
        initial_size = _int(row["initial_candidate_size"], positive=True)
        filled_size = _int(row["filled_size"], positive=True)
        unit_price = _decimal(row["unit_price"], positive=True)
        notional = _int(row["notional_fen"], positive=True)
        commission = _int(row["commission_fen"])
        stamp_duty = _int(row["stamp_duty_fen"])
        transfer_fee = _int(row["transfer_fee_fen"])
        total_fees = _int(row["total_fees_fen"])
        cash_before = _int(row["cash_before_fen"])
        cash_after = _int(row["cash_after_fen"])
        evidence_cash = _int(row["cash_available_before_fen"])
        initial_required = _int(
            row["initial_candidate_cash_required_fen"],
            positive=True,
        )
        requested_required = _int(
            row["requested_cash_required_fen"],
            positive=True,
        )
        adjustment = _optional_text(row["quantity_adjustment_reason"])
        if (
            row["side"] != "buy"
            or initial_size % 100 != 0
            or filled_size % 100 != 0
            or filled_size > initial_size
            or notional != _notional_fen(unit_price, filled_size)
            or total_fees != commission + stamp_duty + transfer_fee
            or cash_after != cash_before - notional - total_fees
            or evidence_cash != cash_before
            or requested_required != notional + total_fees
            or initial_required < requested_required
        ):
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "portfolio fill arithmetic is inconsistent",
            )
        if initial_size == filled_size:
            if adjustment is not None or initial_required != requested_required:
                raise PortfolioArtifactError(
                    "fill_reconciliation_failed",
                    "portfolio fill adjustment evidence is inconsistent",
                )
        elif (
            adjustment != "insufficient_cash_including_fees"
            or evidence_cash >= initial_required
            or evidence_cash < requested_required
        ):
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "portfolio fee-reduction evidence is inconsistent",
            )
        result.append(
            {
                "attempt_id": _text(
                    row["attempt_id"],
                    pattern=_IDENTIFIER_RE,
                ),
                "available_date": _date(row["available_date"]),
                "cash_after_fen": cash_after,
                "cash_available_before_fen": evidence_cash,
                "cash_before_fen": cash_before,
                "commission_fen": commission,
                "execution_session": _date(row["execution_session"]),
                "fill_event_id": _text(
                    row["fill_event_id"],
                    pattern=_IDENTIFIER_RE,
                ),
                "filled_size": filled_size,
                "initial_candidate_cash_required_fen": initial_required,
                "initial_candidate_size": initial_size,
                "lot_id": _text(row["lot_id"], pattern=_IDENTIFIER_RE),
                "notional_fen": notional,
                "quantity_adjustment_reason": adjustment,
                "requested_cash_required_fen": requested_required,
                "side": "buy",
                "stamp_duty_fen": stamp_duty,
                "symbol": _text(row["symbol"], pattern=_SYMBOL_RE),
                "target_id": _text(
                    row["target_id"],
                    pattern=_IDENTIFIER_RE,
                ),
                "total_fees_fen": total_fees,
                "transfer_fee_fen": transfer_fee,
                "unit_price": unit_price,
            }
        )
    _ordered_unique(
        [
            (
                item["execution_session"],
                item["symbol"],
                item["attempt_id"],
            )
            for item in result
        ]
    )
    fill_ids = [item["fill_event_id"] for item in result]
    if len(fill_ids) != len(set(fill_ids)):
        raise PortfolioArtifactError(
            "duplicate_artifact_key",
            "portfolio fill ID is duplicated",
        )
    return result


def _parse_positions(
    rows: Sequence[Mapping[str, str]],
    *,
    run_id: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        _identity_row(row, run_id=run_id)
        total = _int(row["total_size"])
        available = _int(row["available_size"])
        locked = _int(row["locked_size"])
        mark_price = _decimal(row["mark_price"], positive=True)
        market_value = _int(row["market_value_fen"])
        if available + locked != total or market_value != (
            0 if total == 0 else _notional_fen(mark_price, total)
        ):
            raise PortfolioArtifactError(
                "position_reconciliation_failed",
                "portfolio position arithmetic is inconsistent",
            )
        result.append(
            {
                "available_size": available,
                "locked_size": locked,
                "mark_price": mark_price,
                "market_value_fen": market_value,
                "session": _date(row["session"]),
                "symbol": _text(row["symbol"], pattern=_SYMBOL_RE),
                "total_size": total,
            }
        )
    _ordered_unique([(item["session"], item["symbol"]) for item in result])
    return result


def _parse_lots(
    rows: Sequence[Mapping[str, str]],
    *,
    run_id: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        _identity_row(row, run_id=run_id)
        acquired = _date(row["acquired_date"])
        available = _date(row["available_date"])
        original = _int(row["original_size"], positive=True)
        remaining = _int(row["remaining_size"])
        if available <= acquired or original % 100 != 0 or remaining != original:
            raise PortfolioArtifactError(
                "position_reconciliation_failed",
                "portfolio lot contract is invalid",
            )
        result.append(
            {
                "acquired_date": acquired,
                "available_date": available,
                "lot_id": _text(row["lot_id"], pattern=_IDENTIFIER_RE),
                "original_size": original,
                "remaining_size": remaining,
                "symbol": _text(row["symbol"], pattern=_SYMBOL_RE),
                "unit_cost": _decimal(row["unit_cost"], positive=True),
            }
        )
    _ordered_unique([(item["symbol"], item["acquired_date"], item["lot_id"]) for item in result])
    return result


def _parse_cash(
    rows: Sequence[Mapping[str, str]],
    *,
    run_id: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        _identity_row(row, run_id=run_id)
        kind = _enum(row["event_kind"], {"fill", "dividend_payment"})
        side = _optional_text(row["side"])
        notional = _int(row["notional_fen"], positive=True)
        commission = _int(row["commission_fen"])
        stamp_duty = _int(row["stamp_duty_fen"])
        transfer_fee = _int(row["transfer_fee_fen"])
        total_fees = _int(row["total_fees_fen"])
        before = _int(row["cash_before_fen"])
        after = _int(row["cash_after_fen"])
        if total_fees != commission + stamp_duty + transfer_fee:
            raise PortfolioArtifactError(
                "cash_reconciliation_failed",
                "portfolio cash fee total is inconsistent",
            )
        if kind == "fill":
            if side != "buy" or after != before - notional - total_fees:
                raise PortfolioArtifactError(
                    "cash_reconciliation_failed",
                    "portfolio fill cash transition is inconsistent",
                )
        elif side is not None or total_fees != 0 or after != before + notional:
            raise PortfolioArtifactError(
                "cash_reconciliation_failed",
                "portfolio dividend cash transition is inconsistent",
            )
        result.append(
            {
                "cash_after_fen": after,
                "cash_before_fen": before,
                "commission_fen": commission,
                "event_id": _text(
                    row["event_id"],
                    pattern=_IDENTIFIER_RE,
                ),
                "event_kind": kind,
                "notional_fen": notional,
                "reference_id": _text(
                    row["reference_id"],
                    pattern=_IDENTIFIER_RE,
                ),
                "session": _date(row["session"]),
                "side": side,
                "stamp_duty_fen": stamp_duty,
                "symbol": _text(row["symbol"], pattern=_SYMBOL_RE),
                "total_fees_fen": total_fees,
                "transfer_fee_fen": transfer_fee,
            }
        )
    _ordered_unique([(item["session"], item["event_id"]) for item in result])
    return result


def _parse_equity(
    rows: Sequence[Mapping[str, str]],
    *,
    run_id: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        _identity_row(row, run_id=run_id)
        cash = _int(row["cash_fen"])
        market = _int(row["position_market_value_fen"])
        receivable = _int(row["receivable_fen"])
        equity = _int(row["equity_fen"], positive=True)
        if equity != cash + market + receivable:
            raise PortfolioArtifactError(
                "daily_accounting_identity_failed",
                "portfolio daily equity identity is inconsistent",
            )
        result.append(
            {
                "cash_fen": cash,
                "equity_fen": equity,
                "position_market_value_fen": market,
                "receivable_fen": receivable,
                "session": _date(row["session"]),
            }
        )
    _ordered_unique([(item["session"],) for item in result])
    if not result:
        raise PortfolioArtifactError(
            "daily_accounting_identity_failed",
            "portfolio artifact has no daily equity observations",
        )
    return result


def _parse_receivables(
    rows: Sequence[Mapping[str, str]],
    *,
    run_id: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        _identity_row(row, run_id=run_id)
        registered = _date(row["registered_date"])
        source_payable = _date(row["source_payable_date"])
        actual_cash = _date(row["actual_cash_date"])
        paid = _optional_date(row["paid_date"])
        if (
            registered > source_payable
            or actual_cash < source_payable
            or paid is not None
            and paid != actual_cash
        ):
            raise PortfolioArtifactError(
                "receivable_reconciliation_failed",
                "portfolio receivable dates are inconsistent",
            )
        result.append(
            {
                "actual_cash_date": actual_cash,
                "amount_fen": _int(row["amount_fen"], positive=True),
                "event_id": _text(
                    row["event_id"],
                    pattern=_IDENTIFIER_RE,
                ),
                "paid_date": paid,
                "registered_date": registered,
                "source_payable_date": source_payable,
                "symbol": _text(row["symbol"], pattern=_SYMBOL_RE),
            }
        )
    _ordered_unique([(item["actual_cash_date"], item["event_id"]) for item in result])
    return result


def _parse_dividends(
    rows: Sequence[Mapping[str, str]],
    *,
    run_id: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        _identity_row(row, run_id=run_id)
        ex_date = _date(row["ex_date"])
        source_payable = _date(row["source_payable_date"])
        actual_cash = _date(row["actual_cash_date"])
        entitled = _int(row["entitled_size"])
        per_unit = _decimal(
            row["cash_dividend_per_unit"],
            positive=True,
        )
        amount = _int(row["amount_fen"])
        if (
            source_payable < ex_date
            or actual_cash < source_payable
            or amount != _notional_fen(per_unit, entitled)
        ):
            raise PortfolioArtifactError(
                "receivable_reconciliation_failed",
                "portfolio dividend arithmetic is inconsistent",
            )
        result.append(
            {
                "actual_cash_date": actual_cash,
                "amount_fen": amount,
                "cash_dividend_per_unit": per_unit,
                "entitled_size": entitled,
                "event_id": _text(
                    row["event_id"],
                    pattern=_IDENTIFIER_RE,
                ),
                "ex_date": ex_date,
                "source_payable_date": source_payable,
                "symbol": _text(row["symbol"], pattern=_SYMBOL_RE),
            }
        )
    _ordered_unique([(item["ex_date"], item["symbol"], item["event_id"]) for item in result])
    return result


def _parse_availability(
    rows: Sequence[Mapping[str, str]],
    *,
    run_id: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        _identity_row(row, run_id=run_id)
        status = _enum(
            row["status"],
            {"available", "no_bar_unavailable"},
        )
        carried = _int(row["carried_sessions"])
        reason = _enum(
            row["adjustment_reason"],
            {"bar_close", "cash_dividend", "no_bar_carry"},
        )
        if (
            status == "available"
            and (carried != 0 or reason != "bar_close")
            or status == "no_bar_unavailable"
            and reason == "bar_close"
        ):
            raise PortfolioArtifactError(
                "position_reconciliation_failed",
                "portfolio availability evidence is inconsistent",
            )
        result.append(
            {
                "adjustment_reason": reason,
                "carried_sessions": carried,
                "mark_price": _decimal(
                    row["mark_price"],
                    positive=True,
                ),
                "session": _date(row["session"]),
                "status": status,
                "symbol": _text(row["symbol"], pattern=_SYMBOL_RE),
            }
        )
    _ordered_unique([(item["session"], item["symbol"]) for item in result])
    return result


def _to_fen(value: Decimal) -> int:
    try:
        rounded = value.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except DecimalException as exc:
        raise PortfolioArtifactError(
            "fill_reconciliation_failed",
            "portfolio fee cannot be represented in fen",
        ) from exc
    return int(rounded * 100)


def _normalized_touched_rates(
    run: Mapping[str, object],
    *,
    orders: Sequence[Mapping[str, object]],
    fills: Sequence[Mapping[str, object]],
) -> dict[str, list[dict[str, object]]]:
    orders_by_id = {item["attempt_id"]: item for item in orders}
    fills_by_attempt = {item["attempt_id"]: item for item in fills}
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for raw in run["touched_fee_rates"]:
        attempt_id = raw["attempt_id"]
        order = orders_by_id.get(attempt_id)
        if (
            order is None
            or order["status"] != "filled"
            or raw["symbol"] != order["symbol"]
            or _date(raw["execution_session"]) != order["execution_session"]
        ):
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "portfolio touched fee record has no matching fill",
            )
        grouped[attempt_id].append(
            {
                "effective_date": _optional_date(raw["effective_date"]),
                "fee_name": _text(raw["fee_name"]),
                "minimum_yuan": (
                    None
                    if raw["minimum_yuan"] is None
                    else _decimal(
                        raw["minimum_yuan"],
                        nonnegative=True,
                    )
                ),
                "rate": _decimal(raw["rate"], nonnegative=True),
            }
        )
    if set(grouped) != set(fills_by_attempt):
        raise PortfolioArtifactError(
            "fill_reconciliation_failed",
            "portfolio touched rates and fills are not bijective",
        )
    kinds = {item["symbol"]: item["kind"] for item in run["input_closure"]["universe"]["members"]}
    for attempt_id, touches in grouped.items():
        keys = [
            (
                item["fee_name"],
                item["effective_date"] or date.min,
            )
            for item in touches
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "portfolio touched fee records are duplicated or unsorted",
            )
        by_name = {item["fee_name"]: item for item in touches}
        fill = fills_by_attempt[attempt_id]
        notional_yuan = Decimal(fill["notional_fen"]) / 100
        commission = by_name.get("commission")
        if (
            commission is None
            or commission["effective_date"] is not None
            or commission["minimum_yuan"] is None
        ):
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "portfolio commission evidence is incomplete",
            )
        expected_commission = _to_fen(
            max(
                notional_yuan * commission["rate"],
                commission["minimum_yuan"],
            )
        )
        if fill["commission_fen"] != expected_commission:
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "portfolio commission cannot be recomputed",
            )
        if kinds[fill["symbol"]] == "main_board_stock":
            transfer = by_name.get("transfer_fee")
            if (
                set(by_name) != {"commission", "transfer_fee"}
                or transfer is None
                or transfer["effective_date"] is None
                or transfer["minimum_yuan"] is not None
                or fill["transfer_fee_fen"] != _to_fen(notional_yuan * transfer["rate"])
                or fill["stamp_duty_fen"] != 0
            ):
                raise PortfolioArtifactError(
                    "fill_reconciliation_failed",
                    "portfolio stock fee evidence is inconsistent",
                )
        elif (
            set(by_name) != {"commission"}
            or fill["transfer_fee_fen"] != 0
            or fill["stamp_duty_fen"] != 0
        ):
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "portfolio ETF fee evidence is inconsistent",
            )
    return grouped


def _replay_cash(
    events: Sequence[Mapping[str, object]],
    *,
    initial_cash_fen: int,
) -> tuple[list[Mapping[str, object]], int]:
    identifiers = [item["event_id"] for item in events]
    if len(identifiers) != len(set(identifiers)):
        raise PortfolioArtifactError(
            "duplicate_artifact_key",
            "portfolio cash event ID is duplicated",
        )
    by_session: dict[date, list[Mapping[str, object]]] = defaultdict(list)
    for event in events:
        by_session[event["session"]].append(event)
    running = initial_cash_fen
    ordered: list[Mapping[str, object]] = []
    for session in sorted(by_session):
        pending = list(by_session[session])
        while pending:
            candidates = [event for event in pending if event["cash_before_fen"] == running]
            if len(candidates) != 1:
                raise PortfolioArtifactError(
                    "cash_reconciliation_failed",
                    "portfolio cash chain is broken or ambiguous",
                )
            event = candidates[0]
            pending.remove(event)
            ordered.append(event)
            running = event["cash_after_fen"]
    return ordered, running


def _semantic_payload_from_rows(
    *,
    config: Mapping[str, object],
    allocation: Mapping[str, object],
    targets: Sequence[Mapping[str, object]],
    attempts: Sequence[Mapping[str, object]],
    dividends: Sequence[Mapping[str, object]],
    availability: Sequence[Mapping[str, object]],
    initial_cash_fen: int,
    final_cash_fen: int,
    lots: Sequence[Mapping[str, object]],
    cash_events: Sequence[Mapping[str, object]],
    receivables: Sequence[Mapping[str, object]],
    snapshots: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Build the persisted semantic contract without producer imports."""
    return {
        "allocation": dict(allocation),
        "attempts": [
            {
                "attempt_id": item["attempt_id"],
                "attempt_number": item["attempt_number"],
                "availability_status": item["availability_status"],
                "cash_available_before_fen": (item["cash_available_before_fen"]),
                "execution_session": item["execution_session"].isoformat(),
                "fees": (
                    None
                    if item["fees"] is None
                    else {
                        "commission_fen": item["fees"]["commission_fen"],
                        "stamp_duty_fen": item["fees"]["stamp_duty_fen"],
                        "touched_rates": [
                            {
                                "effective_date": (
                                    None
                                    if touch["effective_date"] is None
                                    else touch["effective_date"].isoformat()
                                ),
                                "fee_name": touch["fee_name"],
                                "minimum_yuan": (
                                    None
                                    if touch["minimum_yuan"] is None
                                    else _decimal_text(touch["minimum_yuan"])
                                ),
                                "rate": _decimal_text(touch["rate"]),
                            }
                            for touch in sorted(
                                item["fees"]["touched_rates"],
                                key=lambda touch: (
                                    touch["fee_name"],
                                    touch["effective_date"] or date.min,
                                ),
                            )
                        ],
                        "transfer_fee_fen": (item["fees"]["transfer_fee_fen"]),
                    }
                ),
                "fill_event_id": item["fill_event_id"],
                "initial_candidate_cash_required_fen": (
                    item["initial_candidate_cash_required_fen"]
                ),
                "initial_candidate_size": item["initial_candidate_size"],
                "intent_session": item["intent_session"].isoformat(),
                "original_signal_date": (item["original_signal_date"].isoformat()),
                "quantity_adjustment_reason": (item["quantity_adjustment_reason"]),
                "rejection_reason": item["rejection_reason"],
                "requested_cash_required_fen": (item["requested_cash_required_fen"]),
                "requested_size": item["requested_size"],
                "status": item["status"],
                "symbol": item["symbol"],
                "target_id": item["target_id"],
            }
            for item in sorted(
                attempts,
                key=lambda item: (
                    item["execution_session"],
                    item["symbol"],
                    item["attempt_number"],
                ),
            )
        ],
        "availability": [
            {
                "adjustment_reason": item["adjustment_reason"],
                "carried_sessions": item["carried_sessions"],
                "mark_price": _decimal_text(item["mark_price"]),
                "session": item["session"].isoformat(),
                "status": item["status"],
                "symbol": item["symbol"],
            }
            for item in sorted(
                availability,
                key=lambda item: (item["session"], item["symbol"]),
            )
        ],
        "config": {
            "end_date": config["end_date"].isoformat(),
            "gross_target_weight": _decimal_text(config["gross_target_weight"]),
            "initial_cash_fen": config["initial_cash_fen"],
            "max_entry_attempts": config["max_entry_attempts"],
            "signal_date": config["signal_date"].isoformat(),
            "strategy": config["strategy"],
        },
        "dividends": [
            {
                "actual_cash_date": item["actual_cash_date"].isoformat(),
                "amount_fen": item["amount_fen"],
                "cash_dividend_per_unit": _decimal_text(item["cash_dividend_per_unit"]),
                "entitled_size": item["entitled_size"],
                "event_id": item["event_id"],
                "ex_date": item["ex_date"].isoformat(),
                "source_payable_date": (item["source_payable_date"].isoformat()),
                "symbol": item["symbol"],
            }
            for item in sorted(
                dividends,
                key=lambda item: (
                    item["ex_date"],
                    item["symbol"],
                    item["event_id"],
                ),
            )
        ],
        "ledger": {
            "cash_events": [
                {
                    "cash_after_fen": item["cash_after_fen"],
                    "cash_before_fen": item["cash_before_fen"],
                    "commission_fen": item["commission_fen"],
                    "event_id": item["event_id"],
                    "event_kind": item["event_kind"],
                    "notional_fen": item["notional_fen"],
                    "reference_id": item["reference_id"],
                    "session": item["session"].isoformat(),
                    "side": item["side"],
                    "stamp_duty_fen": item["stamp_duty_fen"],
                    "symbol": item["symbol"],
                    "transfer_fee_fen": item["transfer_fee_fen"],
                }
                for item in sorted(
                    cash_events,
                    key=lambda item: (item["session"], item["event_id"]),
                )
            ],
            "cash_fen": final_cash_fen,
            "daily_snapshots": [
                {
                    "cash_fen": item["cash_fen"],
                    "equity_fen": item["equity_fen"],
                    "position_market_value_fen": (item["position_market_value_fen"]),
                    "receivable_fen": item["receivable_fen"],
                    "session": item["session"].isoformat(),
                    "valuations": [
                        {
                            "available_size": value["available_size"],
                            "locked_size": value["locked_size"],
                            "mark_price": _decimal_text(value["mark_price"]),
                            "market_value_fen": value["market_value_fen"],
                            "symbol": value["symbol"],
                            "total_size": value["total_size"],
                        }
                        for value in sorted(
                            item["valuations"],
                            key=lambda value: value["symbol"],
                        )
                    ],
                }
                for item in sorted(
                    snapshots,
                    key=lambda item: item["session"],
                )
            ],
            "initial_cash_fen": initial_cash_fen,
            "lots": [
                {
                    "acquired_date": item["acquired_date"].isoformat(),
                    "available_date": item["available_date"].isoformat(),
                    "lot_id": item["lot_id"],
                    "original_size": item["original_size"],
                    "remaining_size": item["remaining_size"],
                    "symbol": item["symbol"],
                    "unit_cost": _decimal_text(item["unit_cost"]),
                }
                for item in sorted(
                    lots,
                    key=lambda item: (
                        item["symbol"],
                        item["acquired_date"],
                        item["lot_id"],
                    ),
                )
            ],
            "receivables": [
                {
                    "actual_cash_date": (item["actual_cash_date"].isoformat()),
                    "amount_fen": item["amount_fen"],
                    "event_id": item["event_id"],
                    "paid_date": (
                        None if item["paid_date"] is None else item["paid_date"].isoformat()
                    ),
                    "registered_date": item["registered_date"].isoformat(),
                    "source_payable_date": (item["source_payable_date"].isoformat()),
                    "symbol": item["symbol"],
                }
                for item in sorted(
                    receivables,
                    key=lambda item: (
                        item["actual_cash_date"],
                        item["event_id"],
                    ),
                )
            ],
        },
        "semantic_result_schema": "portfolio-semantic-result-v1",
        "targets": [
            {
                "attempts_used": item["attempts_used"],
                "fill_event_id": item["fill_event_id"],
                "signal_date": item["signal_date"].isoformat(),
                "status": item["status"],
                "symbol": item["symbol"],
                "target_id": item["target_id"],
                "target_notional_fen": item["target_notional_fen"],
            }
            for item in sorted(targets, key=lambda item: item["symbol"])
        ],
    }


def _risk_metrics(
    returns: Sequence[Decimal],
) -> tuple[Decimal | None, Decimal | None]:
    if len(returns) < 2:
        return None, None
    mean = sum(returns, start=_ZERO) / len(returns)
    variance = sum(((value - mean) ** 2 for value in returns), start=_ZERO) / (len(returns) - 1)
    deviation = variance.sqrt()
    if deviation == 0:
        return _published_decimal(_ZERO), None
    annualizer = Decimal(_ANNUAL_SESSIONS).sqrt()
    return (
        _published_decimal(deviation * annualizer),
        _published_decimal(mean / deviation * annualizer),
    )


def _metric_payload_from_rows(
    *,
    run_id: str,
    config: Mapping[str, object],
    allocation: Mapping[str, object],
    targets: Sequence[Mapping[str, object]],
    orders: Sequence[Mapping[str, object]],
    fills: Sequence[Mapping[str, object]],
    positions: Sequence[Mapping[str, object]],
    equity: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Recompute the formal metrics directly from persisted rows."""
    daily_equities = [item["equity_fen"] for item in equity]
    previous = Decimal(config["initial_cash_fen"])
    returns: list[Decimal] = []
    for value in daily_equities:
        current = Decimal(value)
        if current <= 0 or previous <= 0:
            raise PortfolioArtifactError(
                "metric_recomputation_failed",
                "portfolio metric equity must remain positive",
            )
        returns.append(current / previous - _ONE)
        previous = current
    total_return = Decimal(daily_equities[-1]) / Decimal(config["initial_cash_fen"]) - _ONE
    try:
        annualized_return = (_ONE + total_return) ** (
            Decimal(_ANNUAL_SESSIONS) / len(returns)
        ) - _ONE
    except DecimalException as exc:
        raise PortfolioArtifactError(
            "metric_recomputation_failed",
            "portfolio annualized return cannot be recomputed",
        ) from exc
    volatility, sharpe = _risk_metrics(returns)

    peak = Decimal(config["initial_cash_fen"])
    worst_drawdown = _ZERO
    for value in daily_equities:
        current = Decimal(value)
        peak = max(peak, current)
        worst_drawdown = min(worst_drawdown, current / peak - _ONE)

    target_weights = {
        item["symbol"]: (Decimal(item["target_notional_fen"]) / Decimal(config["initial_cash_fen"]))
        for item in targets
    }
    symbols = tuple(sorted(target_weights))
    positions_by_session: dict[date, dict[str, int]] = defaultdict(dict)
    for item in positions:
        positions_by_session[item["session"]][item["symbol"]] = item["market_value_fen"]
    daily_exposure: list[dict[str, str]] = []
    max_symbol_weight = _ZERO
    max_target_deviation = _ZERO
    final_deviations: list[dict[str, str]] = []
    for snapshot in equity:
        equity_value = Decimal(snapshot["equity_fen"])
        gross = Decimal(snapshot["position_market_value_fen"]) / equity_value
        daily_exposure.append(
            {
                "session": snapshot["session"].isoformat(),
                "value": _decimal_text(_published_decimal(gross)),
            }
        )
        values = positions_by_session[snapshot["session"]]
        deviations: list[dict[str, str]] = []
        for symbol in symbols:
            weight = Decimal(values.get(symbol, 0)) / equity_value
            deviation = weight - target_weights[symbol]
            max_symbol_weight = max(max_symbol_weight, abs(weight))
            max_target_deviation = max(
                max_target_deviation,
                abs(deviation),
            )
            deviations.append(
                {
                    "symbol": symbol,
                    "value": _decimal_text(_published_decimal(deviation)),
                }
            )
        final_deviations = deviations

    orders_by_target: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    fills_by_attempt = {item["attempt_id"]: item for item in fills}
    for order in orders:
        orders_by_target[order["target_id"]].append(order)
    invested = 0
    ordinary_rounding = 0
    fee_reduction = 0
    expired = 0
    rejected = 0
    for target in targets:
        if target["status"] == "expired_unfilled":
            expired += target["target_notional_fen"]
            continue
        if target["status"] == "pending":
            rejected += target["target_notional_fen"]
            continue
        filled_attempts = [
            item for item in orders_by_target[target["target_id"]] if item["status"] == "filled"
        ]
        if len(filled_attempts) != 1:
            raise PortfolioArtifactError(
                "metric_recomputation_failed",
                "portfolio filled target cannot be recomputed",
            )
        attempt = filled_attempts[0]
        fill = fills_by_attempt[attempt["attempt_id"]]
        initial_notional = _notional_fen(
            fill["unit_price"],
            attempt["initial_candidate_size"],
        )
        reduced_notional = initial_notional - fill["notional_fen"]
        ordinary = target["target_notional_fen"] - initial_notional
        if ordinary < 0 or reduced_notional < 0:
            raise PortfolioArtifactError(
                "metric_recomputation_failed",
                "portfolio allocation categories cannot be recomputed",
            )
        invested += fill["notional_fen"]
        ordinary_rounding += ordinary
        fee_reduction += reduced_notional
    conservation = (
        allocation["allocation_rounding_remainder_fen"]
        + invested
        + ordinary_rounding
        + fee_reduction
        + expired
        + rejected
    )
    if conservation != allocation["gross_target_notional_fen"]:
        raise PortfolioArtifactError(
            "metric_recomputation_failed",
            "portfolio allocation categories do not conserve",
        )

    average_equity = sum((Decimal(value) for value in daily_equities), start=_ZERO) / len(
        daily_equities
    )
    turnover = Decimal(sum(item["notional_fen"] for item in fills)) / average_equity
    return {
        "allocation_rounding_remainder_fen": (allocation["allocation_rounding_remainder_fen"]),
        "annual_sessions": _ANNUAL_SESSIONS,
        "annualized_return": _decimal_text(_published_decimal(annualized_return)),
        "annualized_volatility": (None if volatility is None else _decimal_text(volatility)),
        "daily_gross_exposure": daily_exposure,
        "expired_uninvested_fen": expired,
        "fee_lot_reduction_fen": fee_reduction,
        "final_symbol_weight_deviations": final_deviations,
        "gross_target_notional_fen": (allocation["gross_target_notional_fen"]),
        "invested_notional_fen": invested,
        "live_trading": False,
        "max_drawdown": _decimal_text(_published_decimal(worst_drawdown)),
        "max_gross_exposure": _decimal_text(
            max(
                (Decimal(item["value"]) for item in daily_exposure),
                default=_ZERO,
            )
        ),
        "max_symbol_weight": _decimal_text(_published_decimal(max_symbol_weight)),
        "max_target_weight_deviation": _decimal_text(_published_decimal(max_target_deviation)),
        "observation_count": len(equity),
        "observed_return_count": len(returns),
        "ordinary_lot_rounding_fen": ordinary_rounding,
        "planned_cash_reserve_fen": (allocation["planned_cash_reserve_fen"]),
        "profit_claim": False,
        "rejected_attempt_count": sum(item["status"] == "rejected" for item in orders),
        "rejected_uninvested_fen": rejected,
        "research_only": True,
        "risk_free_rate": "0",
        "run_id": run_id,
        "schema_version": PORTFOLIO_SCHEMA_VERSION,
        "sharpe_zero_rate": (None if sharpe is None else _decimal_text(sharpe)),
        "total_paid_fees_fen": sum(item["total_fees_fen"] for item in fills),
        "total_return": _decimal_text(_published_decimal(total_return)),
        "trade_count": len(fills),
        "turnover": _decimal_text(_published_decimal(turnover)),
    }


def _reconcile_result(
    *,
    run: Mapping[str, object],
    config: Mapping[str, object],
    symbols: tuple[str, ...],
    targets: list[dict[str, object]],
    orders: list[dict[str, object]],
    fills: list[dict[str, object]],
    positions: list[dict[str, object]],
    lots: list[dict[str, object]],
    cash: list[dict[str, object]],
    equity: list[dict[str, object]],
    receivables: list[dict[str, object]],
    dividends: list[dict[str, object]],
    availability: list[dict[str, object]],
) -> tuple[dict[str, object], dict[str, object]]:
    if tuple(item["symbol"] for item in targets) != symbols:
        raise PortfolioArtifactError(
            "artifact_identity_mismatch",
            "portfolio targets do not match the universe",
        )
    member_count = len(symbols)
    gross_target = int(
        (Decimal(config["initial_cash_fen"]) * config["gross_target_weight"]).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    per_symbol = gross_target // member_count
    allocation = {
        "allocation_rounding_remainder_fen": (gross_target - per_symbol * member_count),
        "gross_target_notional_fen": gross_target,
        "member_count": member_count,
        "per_symbol_target_notional_fen": per_symbol,
        "planned_cash_reserve_fen": (config["initial_cash_fen"] - gross_target),
    }
    if any(
        item["signal_date"] != config["signal_date"] or item["target_notional_fen"] != per_symbol
        for item in targets
    ):
        raise PortfolioArtifactError(
            "fill_reconciliation_failed",
            "portfolio target allocation is inconsistent",
        )

    targets_by_id = {item["target_id"]: item for item in targets}
    orders_by_id = {item["attempt_id"]: item for item in orders}
    attempts_by_target: dict[str, list[dict[str, object]]] = defaultdict(list)
    for order in orders:
        target = targets_by_id.get(order["target_id"])
        if (
            target is None
            or order["symbol"] != target["symbol"]
            or order["original_signal_date"] != target["signal_date"]
            or order["intent_session"] < target["signal_date"]
            or order["execution_session"] <= order["intent_session"]
        ):
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "portfolio attempt does not match its root target",
            )
        attempts_by_target[order["target_id"]].append(order)
    for target in targets:
        attempts = attempts_by_target.get(target["target_id"], [])
        if (
            len(attempts) != target["attempts_used"]
            or [item["attempt_number"] for item in attempts] != list(range(1, len(attempts) + 1))
            or len(attempts) > config["max_entry_attempts"]
            or any(item["execution_session"] > config["end_date"] for item in attempts)
        ):
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "portfolio target attempt sequence is inconsistent",
            )
        if attempts and (
            attempts[0]["intent_session"] != target["signal_date"]
            or any(
                current["intent_session"] != previous["execution_session"]
                for previous, current in zip(
                    attempts,
                    attempts[1:],
                    strict=False,
                )
            )
        ):
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "portfolio retry chain is inconsistent",
            )
        for attempt in attempts:
            if (
                attempt["availability_status"] == "no_bar_unavailable"
                and (
                    attempt["status"] != "rejected"
                    or attempt["rejection_reason"] != "suspended_no_bar"
                )
                or attempt["availability_status"] == "available"
                and attempt["rejection_reason"] == "suspended_no_bar"
            ):
                raise PortfolioArtifactError(
                    "fill_reconciliation_failed",
                    "portfolio rejection lacks matching availability evidence",
                )
        filled = [item for item in attempts if item["status"] == "filled"]
        if target["status"] == "filled":
            if (
                len(filled) != 1
                or filled[0]["fill_event_id"] != target["fill_event_id"]
                or filled[0] is not attempts[-1]
            ):
                raise PortfolioArtifactError(
                    "fill_reconciliation_failed",
                    "filled portfolio target is not bijective",
                )
        elif (
            filled
            or not attempts
            or target["status"] == "expired_unfilled"
            and len(attempts) != config["max_entry_attempts"]
            or target["status"] == "pending"
            and (
                len(attempts) >= config["max_entry_attempts"]
                or attempts[-1]["execution_session"] != config["end_date"]
            )
        ):
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "unfilled portfolio target status is inconsistent",
            )

    fills_by_id = {item["fill_event_id"]: item for item in fills}
    fills_by_attempt = {item["attempt_id"]: item for item in fills}
    if len(fills_by_attempt) != len(fills):
        raise PortfolioArtifactError(
            "duplicate_artifact_key",
            "portfolio filled attempt is duplicated",
        )
    expected_fill_ids = {item["fill_event_id"] for item in orders if item["status"] == "filled"}
    if set(fills_by_id) != expected_fill_ids:
        raise PortfolioArtifactError(
            "fill_reconciliation_failed",
            "portfolio orders and fills are not bijective",
        )
    lots_by_id = {item["lot_id"]: item for item in lots}
    if len(lots_by_id) != len(lots):
        raise PortfolioArtifactError(
            "duplicate_artifact_key",
            "portfolio lot ID is duplicated",
        )
    cash_by_id = {item["event_id"]: item for item in cash}
    if len(cash_by_id) != len(cash):
        raise PortfolioArtifactError(
            "duplicate_artifact_key",
            "portfolio cash event ID is duplicated",
        )
    for fill in fills:
        order = orders_by_id.get(fill["attempt_id"])
        event = cash_by_id.get(fill["fill_event_id"])
        lot = lots_by_id.get(fill["lot_id"])
        if (
            order is None
            or event is None
            or lot is None
            or order["status"] != "filled"
            or order["target_id"] != fill["target_id"]
            or order["symbol"] != fill["symbol"]
            or order["execution_session"] != fill["execution_session"]
            or order["initial_candidate_size"] != fill["initial_candidate_size"]
            or order["requested_size"] != fill["filled_size"]
            or order["fill_event_id"] != fill["fill_event_id"]
            or order["cash_available_before_fen"] != fill["cash_available_before_fen"]
            or order["initial_candidate_cash_required_fen"]
            != fill["initial_candidate_cash_required_fen"]
            or order["requested_cash_required_fen"] != fill["requested_cash_required_fen"]
            or order["quantity_adjustment_reason"] != fill["quantity_adjustment_reason"]
            or event["event_kind"] != "fill"
            or event["symbol"] != fill["symbol"]
            or event["reference_id"] != fill["lot_id"]
            or event["session"] != fill["execution_session"]
            or event["notional_fen"] != fill["notional_fen"]
            or event["commission_fen"] != fill["commission_fen"]
            or event["stamp_duty_fen"] != fill["stamp_duty_fen"]
            or event["transfer_fee_fen"] != fill["transfer_fee_fen"]
            or event["cash_before_fen"] != fill["cash_before_fen"]
            or event["cash_after_fen"] != fill["cash_after_fen"]
            or lot["symbol"] != fill["symbol"]
            or lot["acquired_date"] != fill["execution_session"]
            or lot["available_date"] != fill["available_date"]
            or lot["original_size"] != fill["filled_size"]
            or lot["unit_cost"] != fill["unit_price"]
        ):
            raise PortfolioArtifactError(
                "fill_reconciliation_failed",
                "portfolio fill linkage is inconsistent",
            )
    if {item["reference_id"] for item in cash if item["event_kind"] == "fill"} != set(lots_by_id):
        raise PortfolioArtifactError(
            "fill_reconciliation_failed",
            "portfolio fill cash events and lots are not bijective",
        )

    touched = _normalized_touched_rates(
        run,
        orders=orders,
        fills=fills,
    )
    ordered_cash, final_cash = _replay_cash(
        cash,
        initial_cash_fen=config["initial_cash_fen"],
    )

    receivables_by_id = {item["event_id"]: item for item in receivables}
    dividends_by_id = {item["event_id"]: item for item in dividends}
    if len(receivables_by_id) != len(receivables) or len(dividends_by_id) != len(dividends):
        raise PortfolioArtifactError(
            "duplicate_artifact_key",
            "portfolio dividend or receivable ID is duplicated",
        )
    positive_dividends = {
        event_id: item for event_id, item in dividends_by_id.items() if item["amount_fen"] > 0
    }
    if set(positive_dividends) != set(receivables_by_id):
        raise PortfolioArtifactError(
            "receivable_reconciliation_failed",
            "portfolio dividends and receivables are not bijective",
        )
    dividend_cash = {
        item["reference_id"]: item for item in cash if item["event_kind"] == "dividend_payment"
    }
    for event_id, receivable in receivables_by_id.items():
        dividend = positive_dividends[event_id]
        if (
            receivable["symbol"] != dividend["symbol"]
            or receivable["registered_date"] != dividend["ex_date"]
            or receivable["source_payable_date"] != dividend["source_payable_date"]
            or receivable["actual_cash_date"] != dividend["actual_cash_date"]
            or receivable["amount_fen"] != dividend["amount_fen"]
        ):
            raise PortfolioArtifactError(
                "receivable_reconciliation_failed",
                "portfolio dividend and receivable values differ",
            )
        payment = dividend_cash.get(event_id)
        if receivable["paid_date"] is None:
            if payment is not None:
                raise PortfolioArtifactError(
                    "receivable_reconciliation_failed",
                    "unpaid receivable has a cash event",
                )
        elif (
            payment is None
            or payment["symbol"] != receivable["symbol"]
            or payment["session"] != receivable["actual_cash_date"]
            or payment["notional_fen"] != receivable["amount_fen"]
        ):
            raise PortfolioArtifactError(
                "receivable_reconciliation_failed",
                "paid receivable has no matching cash event",
            )
    if set(dividend_cash) != {
        event_id for event_id, item in receivables_by_id.items() if item["paid_date"] is not None
    }:
        raise PortfolioArtifactError(
            "receivable_reconciliation_failed",
            "portfolio receivable payments are not bijective",
        )

    positions_by_session: dict[date, list[dict[str, object]]] = defaultdict(list)
    for item in positions:
        positions_by_session[item["session"]].append(item)
    availability_by_key = {(item["session"], item["symbol"]): item for item in availability}
    expected_availability_keys = {
        (snapshot["session"], symbol) for snapshot in equity for symbol in symbols
    }
    if set(availability_by_key) != expected_availability_keys:
        raise PortfolioArtifactError(
            "position_reconciliation_failed",
            "portfolio availability matrix is incomplete",
        )
    dividend_adjustments: dict[tuple[date, str], Decimal] = defaultdict(lambda: _ZERO)
    for item in dividends:
        dividend_adjustments[(item["ex_date"], item["symbol"])] += item["cash_dividend_per_unit"]
    for symbol in symbols:
        previous_mark: Decimal | None = None
        carried = 0
        for snapshot in equity:
            audit = availability_by_key[(snapshot["session"], symbol)]
            adjustment = dividend_adjustments[(snapshot["session"], symbol)]
            if audit["status"] == "available":
                carried = 0
            else:
                carried += 1
                expected_reason = "cash_dividend" if adjustment > 0 else "no_bar_carry"
                if (
                    audit["carried_sessions"] != carried
                    or audit["adjustment_reason"] != expected_reason
                    or previous_mark is not None
                    and audit["mark_price"] != previous_mark - adjustment
                ):
                    raise PortfolioArtifactError(
                        "position_reconciliation_failed",
                        "portfolio no-bar valuation chain is inconsistent",
                    )
            previous_mark = audit["mark_price"]
    cash_index = 0
    running_cash = config["initial_cash_fen"]
    for snapshot in equity:
        while (
            cash_index < len(ordered_cash)
            and ordered_cash[cash_index]["session"] <= snapshot["session"]
        ):
            running_cash = ordered_cash[cash_index]["cash_after_fen"]
            cash_index += 1
        session_positions = positions_by_session.get(
            snapshot["session"],
            [],
        )
        expected_sizes: dict[str, tuple[int, int, int]] = {}
        for lot in lots:
            if lot["acquired_date"] > snapshot["session"]:
                continue
            total, available_size, locked_size = expected_sizes.get(
                lot["symbol"],
                (0, 0, 0),
            )
            total += lot["remaining_size"]
            if lot["available_date"] <= snapshot["session"]:
                available_size += lot["remaining_size"]
            else:
                locked_size += lot["remaining_size"]
            expected_sizes[lot["symbol"]] = (
                total,
                available_size,
                locked_size,
            )
        actual_sizes = {
            item["symbol"]: (
                item["total_size"],
                item["available_size"],
                item["locked_size"],
            )
            for item in session_positions
        }
        if actual_sizes != expected_sizes:
            raise PortfolioArtifactError(
                "position_reconciliation_failed",
                "portfolio positions do not reconcile to lots",
            )
        for item in session_positions:
            audit = availability_by_key[(snapshot["session"], item["symbol"])]
            if item["mark_price"] != audit["mark_price"]:
                raise PortfolioArtifactError(
                    "position_reconciliation_failed",
                    "portfolio valuation mark lacks availability evidence",
                )
        expected_market = sum(item["market_value_fen"] for item in session_positions)
        expected_receivable = sum(
            item["amount_fen"]
            for item in receivables
            if item["registered_date"] <= snapshot["session"]
            and (item["paid_date"] is None or item["paid_date"] > snapshot["session"])
        )
        if (
            snapshot["cash_fen"] != running_cash
            or snapshot["position_market_value_fen"] != expected_market
            or snapshot["receivable_fen"] != expected_receivable
            or snapshot["equity_fen"] != running_cash + expected_market + expected_receivable
        ):
            raise PortfolioArtifactError(
                "daily_accounting_identity_failed",
                "portfolio daily account cannot be replayed",
            )
    if final_cash != equity[-1]["cash_fen"]:
        raise PortfolioArtifactError(
            "cash_reconciliation_failed",
            "portfolio final cash differs from the final daily account",
        )

    semantic_attempts: list[dict[str, object]] = []
    for order in orders:
        fill = fills_by_attempt.get(order["attempt_id"])
        semantic_attempts.append(
            {
                **order,
                "fees": (
                    None
                    if fill is None
                    else {
                        "commission_fen": fill["commission_fen"],
                        "stamp_duty_fen": fill["stamp_duty_fen"],
                        "touched_rates": touched[order["attempt_id"]],
                        "transfer_fee_fen": fill["transfer_fee_fen"],
                    }
                ),
            }
        )
    snapshots = [
        {
            **snapshot,
            "valuations": positions_by_session.get(
                snapshot["session"],
                [],
            ),
        }
        for snapshot in equity
    ]
    semantic_result = _semantic_payload_from_rows(
        config=config,
        allocation=allocation,
        targets=targets,
        attempts=semantic_attempts,
        dividends=dividends,
        availability=availability,
        initial_cash_fen=config["initial_cash_fen"],
        final_cash_fen=final_cash,
        lots=lots,
        cash_events=cash,
        receivables=receivables,
        snapshots=snapshots,
    )
    semantic_digest = hashlib.sha256(_canonical_json_bytes(semantic_result)).hexdigest()
    if semantic_digest != run["result_digest"]:
        raise PortfolioArtifactError(
            "result_digest_mismatch",
            "portfolio semantic result does not match the bound result digest",
        )
    return semantic_result, allocation


def _verify_portfolio_artifact(
    directory: str | Path,
    *,
    expected_run_id: str | None = None,
) -> VerifiedPortfolioArtifact:
    if expected_run_id is not None:
        expected_run_id = _hash(
            expected_run_id,
            code="artifact_identity_mismatch",
        )
    with _open_artifact_directory(directory) as (
        path,
        parent_descriptor,
        artifact_descriptor,
        opened,
        payload_stack,
    ):
        try:
            names = set(os.listdir(artifact_descriptor))
        except OSError as exc:
            raise PortfolioArtifactError(
                "unsafe_artifact",
                "portfolio artifact contents cannot be listed safely",
            ) from exc
        if names != _ARTIFACT_FILES:
            raise PortfolioArtifactError(
                "artifact_file_set_mismatch",
                "portfolio artifact file set is incomplete or contains extras",
            )
        opened_files = {
            name: payload_stack.enter_context(_open_safe_file(artifact_descriptor, name))
            for name in sorted(names)
        }
        content = {name: value[0] for name, value in opened_files.items()}
        (
            manifest,
            manifest_run_id,
            manifest_row_counts,
        ) = _validate_manifest(content["artifact_manifest.json"])
        manifest_files = manifest["files"]
        for filename in sorted(_PAYLOAD_FILES):
            if hashlib.sha256(content[filename]).hexdigest() != manifest_files[filename]["sha256"]:
                raise PortfolioArtifactError(
                    "artifact_hash_mismatch",
                    "portfolio artifact payload hash is inconsistent",
                )

        run, config, symbols = _validate_run(
            content["run.json"],
            manifest_run_id=manifest_run_id,
            manifest_row_counts=manifest_row_counts,
            directory_name=path.name,
            expected_run_id=expected_run_id,
        )
        raw_csv = {name: _parse_csv(name, content[name]) for name in sorted(_CSV_SCHEMAS)}
        actual_counts = {
            "metrics.json": 1,
            "run.json": 1,
            **{name: len(rows) for name, rows in raw_csv.items()},
        }
        if actual_counts != manifest_row_counts:
            raise PortfolioArtifactError(
                "artifact_row_count_mismatch",
                "portfolio artifact row counts cannot be reconstructed",
            )
        metrics = _parse_json(content["metrics.json"])
        run_id = run["run_id"]
        targets = _parse_targets(raw_csv["targets.csv"], run_id=run_id)
        orders = _parse_orders(raw_csv["orders.csv"], run_id=run_id)
        fills = _parse_fills(raw_csv["fills.csv"], run_id=run_id)
        positions = _parse_positions(
            raw_csv["positions.csv"],
            run_id=run_id,
        )
        lots = _parse_lots(raw_csv["lots.csv"], run_id=run_id)
        cash = _parse_cash(raw_csv["cash.csv"], run_id=run_id)
        equity = _parse_equity(raw_csv["equity.csv"], run_id=run_id)
        receivables = _parse_receivables(
            raw_csv["receivables.csv"],
            run_id=run_id,
        )
        dividends = _parse_dividends(
            raw_csv["corporate_actions.csv"],
            run_id=run_id,
        )
        availability = _parse_availability(
            raw_csv["availability.csv"],
            run_id=run_id,
        )
        _semantic_result, allocation = _reconcile_result(
            run=run,
            config=config,
            symbols=symbols,
            targets=targets,
            orders=orders,
            fills=fills,
            positions=positions,
            lots=lots,
            cash=cash,
            equity=equity,
            receivables=receivables,
            dividends=dividends,
            availability=availability,
        )
        with localcontext(Context(prec=80, rounding=ROUND_HALF_UP)):
            recomputed_metrics = _metric_payload_from_rows(
                run_id=run_id,
                config=config,
                allocation=allocation,
                targets=targets,
                orders=orders,
                fills=fills,
                positions=positions,
                equity=equity,
            )
        if metrics != recomputed_metrics:
            raise PortfolioArtifactError(
                "metric_recomputation_failed",
                "portfolio metrics cannot be independently recomputed",
            )
        for name, (original_bytes, metadata, descriptor) in opened_files.items():
            _verify_file_binding(
                artifact_descriptor,
                name,
                metadata,
                descriptor,
                original_bytes,
            )
        try:
            current_names = set(os.listdir(artifact_descriptor))
        except OSError as exc:
            raise PortfolioArtifactError(
                "unsafe_artifact",
                "portfolio artifact contents cannot be relisted safely",
            ) from exc
        if current_names != names:
            raise PortfolioArtifactError(
                "unsafe_artifact",
                "portfolio artifact file set changed during verification",
            )
        _verify_artifact_binding(path, parent_descriptor, opened)
        return VerifiedPortfolioArtifact(
            run_id=run_id,
            status="verified",
            artifact_manifest_sha256=hashlib.sha256(content["artifact_manifest.json"]).hexdigest(),
            artifact_file_count=len(_ARTIFACT_FILES),
            payload_file_count=len(_PAYLOAD_FILES),
            file_count=len(_ARTIFACT_FILES),
            trade_count=len(fills),
            row_counts=tuple(sorted(actual_counts.items())),
        )


def verify_portfolio_artifact(
    directory: str | Path,
    *,
    expected_run_id: str | None = None,
) -> VerifiedPortfolioArtifact:
    """Verify a complete portfolio bundle without producer runtime imports."""
    with localcontext(Context(prec=80, rounding=ROUND_HALF_UP)):
        return _verify_portfolio_artifact(
            directory,
            expected_run_id=expected_run_id,
        )
