"""Strict corporate-action contracts for audited A-share research."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

import pandas as pd

from aquant.rules import InstrumentKind

_SYMBOL_RE = re.compile(r"[0-9]{6}")
_STOCK_COLUMNS = {
    "实施方案公告日期",
    "分红类型",
    "送股比例",
    "转增比例",
    "派息比例",
    "股权登记日",
    "除权日",
    "派息日",
    "股份到账日",
    "实施方案分红说明",
    "报告时间",
}
_STOCK_SOURCE_SCHEMA = "akshare.stock_dividend_cninfo.v1"
_STOCK_SOURCE_URL = "https://webapi.cninfo.com.cn/"
_ETF_CUMULATIVE_COLUMNS = {"日期", "累计分红"}
_ETF_DETAIL_COLUMNS = {"权益登记日", "红利发放日", "每份分红(元)"}
_ETF_SOURCE_SCHEMA = "akshare.fund_etf_dividend_sina+sina.detail.v1"
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_TEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_VERIFIED_ACTIONS_TOKEN = object()


class CorporateActionError(ValueError):
    """Raised when corporate actions cannot cross the formal trust boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _event_identity(values: dict[str, Any]) -> str:
    canonical = {
        key: (
            value.isoformat()
            if isinstance(value, date)
            else value.value
            if isinstance(value, InstrumentKind)
            else _decimal_text(value)
            if isinstance(value, Decimal)
            else value
        )
        for key, value in values.items()
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CorporateActionEvent:
    event_id: str
    symbol: str
    instrument_kind: InstrumentKind
    announcement_date: date | None
    record_date: date
    ex_date: date
    payable_date: date
    cash_dividend_per_unit: Decimal
    stock_dividend_ratio: Decimal
    capitalization_ratio: Decimal
    rights_ratio: Decimal
    rights_price: Decimal | None
    source_schema: str
    source_url: str

    @classmethod
    def create(cls, **values: Any) -> CorporateActionEvent:
        expected = set(cls.__dataclass_fields__) - {"event_id"}
        if set(values) != expected:
            raise CorporateActionError(
                "invalid_event_fields",
                "corporate-action event fields are incomplete or unknown",
            )
        return cls(event_id=_event_identity(values), **values)

    def __post_init__(self) -> None:
        if _SYMBOL_RE.fullmatch(self.symbol) is None:
            raise CorporateActionError(
                "invalid_symbol", "corporate-action symbol must be six digits"
            )
        if type(self.instrument_kind) is not InstrumentKind:
            raise CorporateActionError(
                "invalid_instrument_kind", "corporate-action instrument kind is invalid"
            )
        for field in ("record_date", "ex_date", "payable_date"):
            if type(getattr(self, field)) is not date:
                raise CorporateActionError("invalid_date", f"{field} must be a date")
        if self.announcement_date is not None and type(self.announcement_date) is not date:
            raise CorporateActionError(
                "invalid_date", "announcement_date must be a date or null"
            )
        if self.payable_date < self.ex_date:
            raise CorporateActionError(
                "payment_before_ex_date",
                "cash payment date cannot precede the ex-date",
            )
        for field in (
            "cash_dividend_per_unit",
            "stock_dividend_ratio",
            "capitalization_ratio",
            "rights_ratio",
        ):
            value = getattr(self, field)
            if type(value) is not Decimal or not value.is_finite() or value < 0:
                raise CorporateActionError(
                    "invalid_numeric_value", f"{field} must be a non-negative Decimal"
                )
        if self.rights_price is not None and (
            type(self.rights_price) is not Decimal
            or not self.rights_price.is_finite()
            or self.rights_price < 0
        ):
            raise CorporateActionError(
                "invalid_numeric_value",
                "rights_price must be a non-negative Decimal or null",
            )
        expected_id = _event_identity(
            {
                key: value
                for key, value in asdict(self).items()
                if key != "event_id"
            }
        )
        if self.event_id != expected_id:
            raise CorporateActionError(
                "invalid_event_id", "event ID does not match canonical fields"
            )

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "symbol": self.symbol,
            "instrument_kind": self.instrument_kind.value,
            "announcement_date": (
                self.announcement_date.isoformat()
                if self.announcement_date is not None
                else None
            ),
            "record_date": self.record_date.isoformat(),
            "ex_date": self.ex_date.isoformat(),
            "payable_date": self.payable_date.isoformat(),
            "cash_dividend_per_unit": _decimal_text(
                self.cash_dividend_per_unit
            ),
            "stock_dividend_ratio": _decimal_text(self.stock_dividend_ratio),
            "capitalization_ratio": _decimal_text(self.capitalization_ratio),
            "rights_ratio": _decimal_text(self.rights_ratio),
            "rights_price": (
                _decimal_text(self.rights_price)
                if self.rights_price is not None
                else None
            ),
            "source_schema": self.source_schema,
            "source_url": self.source_url,
        }

    @classmethod
    def from_json_dict(cls, values: Any) -> CorporateActionEvent:
        expected = set(cls.__dataclass_fields__)
        if type(values) is not dict or set(values) != expected:
            raise CorporateActionError(
                "invalid_event_fields",
                "stored corporate-action fields are incomplete or unknown",
            )
        try:
            announcement = values["announcement_date"]
            event = cls(
                event_id=values["event_id"],
                symbol=values["symbol"],
                instrument_kind=InstrumentKind(values["instrument_kind"]),
                announcement_date=(
                    date.fromisoformat(announcement)
                    if announcement is not None
                    else None
                ),
                record_date=date.fromisoformat(values["record_date"]),
                ex_date=date.fromisoformat(values["ex_date"]),
                payable_date=date.fromisoformat(values["payable_date"]),
                cash_dividend_per_unit=Decimal(
                    values["cash_dividend_per_unit"]
                ),
                stock_dividend_ratio=Decimal(values["stock_dividend_ratio"]),
                capitalization_ratio=Decimal(
                    values["capitalization_ratio"]
                ),
                rights_ratio=Decimal(values["rights_ratio"]),
                rights_price=(
                    Decimal(values["rights_price"])
                    if values["rights_price"] is not None
                    else None
                ),
                source_schema=values["source_schema"],
                source_url=values["source_url"],
            )
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise CorporateActionError(
                "invalid_event_record", "stored corporate-action event is invalid"
            ) from exc
        if event.to_json_dict() != values:
            raise CorporateActionError(
                "noncanonical_event_record",
                "stored corporate-action event is not canonical",
            )
        return event


def _parse_date(value: Any, *, nullable: bool = False) -> date | None:
    if value is None or pd.isna(value):
        if nullable:
            return None
        raise CorporateActionError("invalid_date", "required corporate-action date is missing")
    if type(value) is bool:
        raise CorporateActionError("invalid_date", "corporate-action date is invalid")
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CorporateActionError(
            "invalid_date", "corporate-action date cannot be parsed"
        ) from exc
    if parsed.tzinfo is not None or parsed != parsed.normalize():
        raise CorporateActionError(
            "invalid_date", "corporate-action date must be timezone-free and date-only"
        )
    result = parsed.date()
    if isinstance(value, str) and value != result.isoformat():
        raise CorporateActionError(
            "invalid_date", "corporate-action date string must be canonical ISO"
        )
    return result


def _parse_source_date(value: Any) -> date:
    if value is None or pd.isna(value) or type(value) is bool:
        raise CorporateActionError("invalid_date", "corporate-action date is invalid")
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CorporateActionError(
            "invalid_date", "corporate-action date cannot be parsed"
        ) from exc
    if parsed.tzinfo is not None or parsed != parsed.normalize():
        raise CorporateActionError(
            "invalid_date", "corporate-action date must be timezone-free and date-only"
        )
    return parsed.date()


def _parse_decimal(value: Any, *, field: str) -> Decimal:
    if value is None or pd.isna(value):
        return Decimal("0")
    if type(value) is bool:
        raise CorporateActionError(
            "invalid_numeric_value", f"{field} must not be boolean"
        )
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CorporateActionError(
            "invalid_numeric_value", f"{field} must be numeric"
        ) from exc
    if not result.is_finite():
        raise CorporateActionError(
            "invalid_numeric_value", f"{field} must be finite"
        )
    return result


def normalize_stock_dividends(
    frame: pd.DataFrame,
    *,
    symbol: str,
    instrument_kind: InstrumentKind,
    coverage_start: date,
    coverage_end: date,
) -> tuple[CorporateActionEvent, ...]:
    """Normalize CNInfo per-ten-share rows into exact per-share cash events."""
    if not isinstance(frame, pd.DataFrame):
        raise CorporateActionError(
            "invalid_source_frame", "stock dividend input must be a DataFrame"
        )
    if set(frame.columns) != _STOCK_COLUMNS:
        raise CorporateActionError(
            "source_schema_changed",
            "stock dividend source columns do not match the pinned schema",
        )
    if type(symbol) is not str or _SYMBOL_RE.fullmatch(symbol) is None:
        raise CorporateActionError(
            "invalid_symbol", "stock dividend symbol must be six digits"
        )
    if instrument_kind is not InstrumentKind.MAIN_BOARD_STOCK:
        raise CorporateActionError(
            "invalid_instrument_kind",
            "stock dividend source requires a main-board stock",
        )
    if (
        type(coverage_start) is not date
        or type(coverage_end) is not date
        or coverage_start > coverage_end
    ):
        raise CorporateActionError(
            "invalid_coverage", "corporate-action coverage is invalid"
        )

    grouped: dict[date, list[dict[str, Any]]] = {}
    for row in frame.to_dict(orient="records"):
        ex_date = _parse_date(row["除权日"], nullable=True)
        if ex_date is None:
            record_date = _parse_date(row["股权登记日"], nullable=True)
            payable_date = _parse_date(row["派息日"], nullable=True)
            if (
                record_date is not None
                and payable_date is not None
                and record_date < coverage_start
                and payable_date < coverage_start
            ):
                continue
            raise CorporateActionError(
                "missing_required_date",
                "in-scope corporate action is missing its ex-date",
            )
        if ex_date < coverage_start or ex_date > coverage_end:
            continue
        cash_per_ten = _parse_decimal(row["派息比例"], field="派息比例")
        if cash_per_ten < 0:
            raise CorporateActionError(
                "invalid_cash_dividend", "cash dividend cannot be negative"
            )
        stock_per_ten = _parse_decimal(row["送股比例"], field="送股比例")
        capitalization_per_ten = _parse_decimal(row["转增比例"], field="转增比例")
        if stock_per_ten != 0 or capitalization_per_ten != 0:
            raise CorporateActionError(
                "unsupported_corporate_action",
                "v0.1 rejects stock dividends and capitalization issues",
            )
        payable_date = _parse_date(row["派息日"])
        assert payable_date is not None
        if payable_date < ex_date:
            raise CorporateActionError(
                "payment_before_ex_date",
                "cash payment date cannot precede the ex-date",
            )
        grouped.setdefault(ex_date, []).append(
            {
                "announcement_date": _parse_date(
                    row["实施方案公告日期"], nullable=True
                ),
                "record_date": _parse_date(row["股权登记日"]),
                "payable_date": payable_date,
                "cash": cash_per_ten / Decimal("10"),
            }
        )

    events: list[CorporateActionEvent] = []
    for ex_date, rows in sorted(grouped.items()):
        metadata = {
            (
                row["announcement_date"],
                row["record_date"],
                row["payable_date"],
            )
            for row in rows
        }
        if len(metadata) != 1:
            raise CorporateActionError(
                "conflicting_same_day_events",
                "same-day dividend rows disagree on dates",
            )
        announcement_date, record_date, payable_date = metadata.pop()
        assert type(record_date) is date
        assert type(payable_date) is date
        events.append(
            CorporateActionEvent.create(
                symbol=symbol,
                instrument_kind=instrument_kind,
                announcement_date=announcement_date,
                record_date=record_date,
                ex_date=ex_date,
                payable_date=payable_date,
                cash_dividend_per_unit=sum(
                    (row["cash"] for row in rows), start=Decimal("0")
                ),
                stock_dividend_ratio=Decimal("0"),
                capitalization_ratio=Decimal("0"),
                rights_ratio=Decimal("0"),
                rights_price=None,
                source_schema=_STOCK_SOURCE_SCHEMA,
                source_url=_STOCK_SOURCE_URL,
            )
        )
    return tuple(events)


def parse_sina_etf_dividend_detail(content: bytes) -> pd.DataFrame:
    """Parse the pinned Sina ETF detail table without accepting schema drift."""
    if type(content) is not bytes:
        raise CorporateActionError(
            "invalid_etf_detail", "ETF dividend detail must be raw bytes"
        )
    try:
        text = content.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise CorporateActionError(
            "etf_detail_decode_failed", "ETF dividend detail decoding failed"
        ) from exc
    try:
        tables = pd.read_html(io.StringIO(text))
    except (ImportError, TypeError, ValueError) as exc:
        raise CorporateActionError(
            "etf_detail_schema_changed", "ETF dividend detail table is unavailable"
        ) from exc
    ordered_columns = ["权益登记日", "红利发放日", "每份分红(元)"]
    matches: list[pd.DataFrame] = []
    for table in tables:
        if _ETF_DETAIL_COLUMNS.issubset(set(table.columns)):
            candidate = table.loc[:, ordered_columns].copy()
        elif len(table.columns) >= 3 and not table.empty:
            first = table.iloc[0]
            first_three = [str(value).strip() for value in first.iloc[:3]]
            trailing = tuple(first.iloc[3:])
            if first_three != ordered_columns or any(
                not pd.isna(value) and str(value).strip() for value in trailing
            ):
                continue
            candidate = table.iloc[1:, :3].copy()
            candidate.columns = ordered_columns
        else:
            continue

        populated = candidate.notna()
        complete = populated.all(axis=1)
        footer = (
            candidate["权益登记日"].isna()
            & candidate["红利发放日"].isna()
            & candidate["每份分红(元)"]
            .astype("string")
            .str.startswith("合计:", na=False)
        )
        zero_dividend_placeholder = (
            candidate["权益登记日"].notna()
            & candidate["红利发放日"]
            .astype("string")
            .str.strip()
            .eq("1970/1/1")
            & candidate["每份分红(元)"].isna()
        )
        invalid_partial = (
            populated.any(axis=1)
            & ~complete
            & ~footer
            & ~zero_dividend_placeholder
        )
        if invalid_partial.any():
            raise CorporateActionError(
                "etf_detail_schema_changed",
                "ETF dividend detail contains a partial data row",
            )
        matches.append(candidate.loc[complete].reset_index(drop=True))
    if len(matches) != 1:
        raise CorporateActionError(
            "etf_detail_schema_changed",
            "ETF dividend detail must contain exactly one pinned table",
        )
    result = matches[0]
    if result.empty:
        raise CorporateActionError(
            "etf_detail_schema_changed", "ETF dividend detail table must not be empty"
        )
    result.columns = ordered_columns
    return result.reset_index(drop=True)


def normalize_etf_dividends(
    cumulative_frame: pd.DataFrame,
    detail_frame: pd.DataFrame,
    *,
    symbol: str,
    instrument_kind: InstrumentKind,
    coverage_start: date,
    coverage_end: date,
) -> tuple[CorporateActionEvent, ...]:
    """Match Sina cumulative ETF dividends to record and payment dates."""
    if not isinstance(cumulative_frame, pd.DataFrame) or not isinstance(
        detail_frame, pd.DataFrame
    ):
        raise CorporateActionError(
            "invalid_source_frame", "ETF dividend inputs must be DataFrames"
        )
    if set(cumulative_frame.columns) != _ETF_CUMULATIVE_COLUMNS:
        raise CorporateActionError(
            "source_schema_changed",
            "ETF cumulative dividend columns do not match the pinned schema",
        )
    if set(detail_frame.columns) != _ETF_DETAIL_COLUMNS:
        raise CorporateActionError(
            "etf_detail_schema_changed",
            "ETF detail dividend columns do not match the pinned schema",
        )
    if (
        type(symbol) is not str
        or _SYMBOL_RE.fullmatch(symbol) is None
        or instrument_kind
        is not InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF
    ):
        raise CorporateActionError(
            "invalid_instrument_kind", "ETF dividend identity is invalid"
        )
    if (
        type(coverage_start) is not date
        or type(coverage_end) is not date
        or coverage_start > coverage_end
    ):
        raise CorporateActionError(
            "invalid_coverage", "corporate-action coverage is invalid"
        )

    cumulative_rows: list[tuple[date, Decimal]] = []
    for row in cumulative_frame.to_dict(orient="records"):
        cumulative_rows.append(
            (
                _parse_source_date(row["日期"]),
                _parse_decimal(row["累计分红"], field="累计分红"),
            )
        )
    cumulative_rows.sort()
    if len({row[0] for row in cumulative_rows}) != len(cumulative_rows):
        raise CorporateActionError(
            "duplicate_etf_cumulative_date",
            "ETF cumulative dividend dates must be unique",
        )

    details: list[tuple[date, date, Decimal]] = []
    for row in detail_frame.to_dict(orient="records"):
        amount = _parse_decimal(row["每份分红(元)"], field="每份分红(元)")
        detail = (
            _parse_source_date(row["权益登记日"]),
            _parse_source_date(row["红利发放日"]),
            amount,
        )
        if detail in details:
            raise CorporateActionError(
                "duplicate_etf_dividend_detail",
                "ETF dividend detail rows must be unique",
            )
        details.append(detail)

    previous = Decimal("0")
    deltas: list[tuple[date, Decimal]] = []
    for ex_date, cumulative in cumulative_rows:
        delta = cumulative - previous
        previous = cumulative
        if delta < 0:
            raise CorporateActionError(
                "negative_cumulative_difference",
                "ETF cumulative dividend cannot decrease",
            )
        if delta == 0:
            continue
        deltas.append((ex_date, delta))

    events: list[CorporateActionEvent] = []
    for ex_date, delta in deltas:
        if not (coverage_start <= ex_date <= coverage_end):
            continue
        candidates = [
            detail
            for detail in details
            if 0 <= (ex_date - detail[0]).days <= 10 and detail[1] >= ex_date
        ]
        if not candidates:
            raise CorporateActionError(
                "missing_etf_dividend_detail",
                "ETF cumulative dividend has no matching detail row",
            )
        if len(candidates) != 1:
            raise CorporateActionError(
                "ambiguous_etf_dividend_detail",
                "ETF cumulative dividend matches multiple detail rows",
            )
        record_date, payable_date, detail_amount = candidates[0]
        if delta != detail_amount:
            raise CorporateActionError(
                "dividend_amount_mismatch",
                "ETF cumulative and detail dividend amounts differ",
            )
        events.append(
            CorporateActionEvent.create(
                symbol=symbol,
                instrument_kind=instrument_kind,
                announcement_date=None,
                record_date=record_date,
                ex_date=ex_date,
                payable_date=payable_date,
                cash_dividend_per_unit=delta,
                stock_dividend_ratio=Decimal("0"),
                capitalization_ratio=Decimal("0"),
                rights_ratio=Decimal("0"),
                rights_price=None,
                source_schema=_ETF_SOURCE_SCHEMA,
                source_url=(
                    "https://stock.finance.sina.com.cn/fundInfo/view/"
                    f"FundInfo_JJFH.php?symbol={symbol}"
                ),
            )
        )
    return tuple(events)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CorporateActionError(
                "duplicate_json_key",
                "corporate-action JSON contains a duplicate key",
            )
        result[key] = value
    return result


@dataclass(frozen=True)
class CorporateActionManifestRecord:
    schema_version: str
    snapshot_id: str
    symbol: str
    instrument_kind: str
    provider: str
    source_schema: str
    normalization_version: str
    coverage_start: date
    coverage_end: date
    row_count: int
    snapshot_relative_path: Path
    file_sha256: str

    @classmethod
    def create(cls, **values: Any) -> CorporateActionManifestRecord:
        expected = set(cls.__dataclass_fields__) - {"snapshot_id"}
        if set(values) != expected:
            raise CorporateActionError(
                "invalid_manifest_fields",
                "corporate-action manifest fields are incomplete or unknown",
            )
        identity = _manifest_identity(values)
        return cls(snapshot_id=identity, **values)

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise CorporateActionError(
                "invalid_manifest_field",
                "corporate-action manifest schema version is unsupported",
            )
        if _HASH_RE.fullmatch(self.snapshot_id) is None or _HASH_RE.fullmatch(
            self.file_sha256
        ) is None:
            raise CorporateActionError(
                "invalid_manifest_field",
                "corporate-action hashes must be lowercase SHA-256",
            )
        if _SYMBOL_RE.fullmatch(self.symbol) is None:
            raise CorporateActionError(
                "invalid_manifest_field",
                "corporate-action manifest symbol is invalid",
            )
        for field in (
            "instrument_kind",
            "provider",
            "source_schema",
            "normalization_version",
        ):
            value = getattr(self, field)
            if type(value) is not str or _TEXT_RE.fullmatch(value) is None:
                raise CorporateActionError(
                    "invalid_manifest_field", f"{field} is invalid"
                )
        try:
            InstrumentKind(self.instrument_kind)
        except ValueError as exc:
            raise CorporateActionError(
                "invalid_manifest_field", "instrument kind is unsupported"
            ) from exc
        if (
            type(self.coverage_start) is not date
            or type(self.coverage_end) is not date
            or self.coverage_start > self.coverage_end
        ):
            raise CorporateActionError(
                "invalid_manifest_field",
                "corporate-action manifest coverage is invalid",
            )
        if (
            type(self.row_count) is not int
            or isinstance(self.row_count, bool)
            or self.row_count < 0
        ):
            raise CorporateActionError(
                "invalid_manifest_field",
                "corporate-action row count must be non-negative",
            )
        path = PurePosixPath(Path(self.snapshot_relative_path).as_posix())
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.parts) != 4
            or path.parts[:2] != ("data", "corporate_actions")
            or path.parts[2] != self.symbol
            or path.name != f"{self.file_sha256}.json"
        ):
            raise CorporateActionError(
                "invalid_manifest_path",
                "corporate-action snapshot path is unsafe or inconsistent",
            )
        object.__setattr__(self, "snapshot_relative_path", Path(path.as_posix()))
        if self.snapshot_id != _manifest_identity(self.to_dict()):
            raise CorporateActionError(
                "invalid_snapshot_id",
                "corporate-action snapshot ID does not match manifest fields",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field) for field in self.__dataclass_fields__
        }

    def to_json_dict(self) -> dict[str, Any]:
        values = self.to_dict()
        values["coverage_start"] = self.coverage_start.isoformat()
        values["coverage_end"] = self.coverage_end.isoformat()
        values["snapshot_relative_path"] = self.snapshot_relative_path.as_posix()
        return values

    @classmethod
    def from_json_dict(cls, values: Any) -> CorporateActionManifestRecord:
        expected = set(cls.__dataclass_fields__)
        if type(values) is not dict or set(values) != expected:
            raise CorporateActionError(
                "invalid_manifest_fields",
                "stored manifest fields are incomplete or unknown",
            )
        try:
            parsed = dict(values)
            parsed["coverage_start"] = date.fromisoformat(
                parsed["coverage_start"]
            )
            parsed["coverage_end"] = date.fromisoformat(parsed["coverage_end"])
            parsed["snapshot_relative_path"] = Path(
                parsed["snapshot_relative_path"]
            )
            record = cls(**parsed)
        except (TypeError, ValueError) as exc:
            raise CorporateActionError(
                "invalid_manifest_record",
                "stored corporate-action manifest is invalid",
            ) from exc
        if record.to_json_dict() != values:
            raise CorporateActionError(
                "noncanonical_manifest_record",
                "stored corporate-action manifest is not canonical",
            )
        return record


def _manifest_identity(values: dict[str, Any]) -> str:
    canonical = {
        key: (
            value.isoformat()
            if isinstance(value, date)
            else value.as_posix()
            if isinstance(value, Path)
            else value
        )
        for key, value in values.items()
        if key != "snapshot_id"
    }
    return hashlib.sha256(_canonical_json_bytes(canonical)).hexdigest()


@dataclass(frozen=True)
class CorporateActionSnapshotArtifact:
    relative_path: Path
    sha256: str
    row_count: int
    reused: bool


class CorporateActionSnapshotStore:
    """Content-addressed canonical event storage beneath the project root."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _safe_parent(self, symbol: str) -> Path:
        current = self.root
        if current.is_symlink():
            raise CorporateActionError(
                "unsafe_snapshot_parent", "project root must not be a symlink"
            )
        for component in ("data", "corporate_actions", symbol):
            current = current / component
            if current.exists() and current.is_symlink():
                raise CorporateActionError(
                    "unsafe_snapshot_parent",
                    "corporate-action parent must not be a symlink",
                )
            try:
                current.mkdir(exist_ok=True)
            except OSError as exc:
                raise CorporateActionError(
                    "unsafe_snapshot_parent",
                    "corporate-action parent cannot be created",
                ) from exc
            metadata = current.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise CorporateActionError(
                    "unsafe_snapshot_parent",
                    "corporate-action parent must be a directory",
                )
        return current

    def write(
        self,
        events: tuple[CorporateActionEvent, ...],
        *,
        symbol: str,
    ) -> CorporateActionSnapshotArtifact:
        if type(events) is not tuple or any(
            type(event) is not CorporateActionEvent
            or event.symbol != symbol
            for event in events
        ):
            raise CorporateActionError(
                "invalid_event_collection",
                "corporate-action events must be an exact same-symbol tuple",
            )
        ordered = tuple(sorted(events, key=lambda item: (item.ex_date, item.event_id)))
        if ordered != events or len({event.event_id for event in events}) != len(
            events
        ):
            raise CorporateActionError(
                "invalid_event_collection",
                "corporate-action events must be sorted and unique",
            )
        content = _canonical_json_bytes(
            {
                "events": [event.to_json_dict() for event in events],
                "schema_version": "1.0",
            }
        )
        digest = hashlib.sha256(content).hexdigest()
        relative = Path("data", "corporate_actions", symbol, f"{digest}.json")
        parent = self._safe_parent(symbol)
        target = parent / relative.name
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise CorporateActionError(
                    "snapshot_conflict",
                    "corporate-action snapshot target is unsafe",
                )
            if target.read_bytes() != content:
                raise CorporateActionError(
                    "snapshot_conflict",
                    "corporate-action snapshot content conflicts",
                )
            return CorporateActionSnapshotArtifact(
                relative, digest, len(events), True
            )
        temporary = parent / f".snapshot-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o444)
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != content:
                    raise CorporateActionError(
                        "snapshot_conflict",
                        "concurrent corporate-action snapshot conflicts",
                    ) from None
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except CorporateActionError:
            raise
        except OSError as exc:
            raise CorporateActionError(
                "snapshot_write_failed",
                "corporate-action snapshot could not be published",
            ) from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return CorporateActionSnapshotArtifact(relative, digest, len(events), False)

    def verify(self, relative_path: Path, expected_hash: str) -> bytes:
        path = self.root / relative_path
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise CorporateActionError(
                "snapshot_verification_failed",
                "corporate-action snapshot cannot be inspected",
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or path.is_symlink()
        ):
            raise CorporateActionError(
                "snapshot_verification_failed",
                "corporate-action snapshot is not a safe regular file",
            )
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise CorporateActionError(
                "snapshot_verification_failed",
                "corporate-action snapshot hash does not match",
            )
        return content


class CorporateActionManifestWriter:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "data/corporate_actions/manifest.jsonl"
        self.lock_path = self.path.with_suffix(".jsonl.lock")

    def append(self, record: CorporateActionManifestRecord) -> None:
        if type(record) is not CorporateActionManifestRecord:
            raise CorporateActionError(
                "invalid_manifest_record",
                "manifest append requires an exact record",
            )
        CorporateActionManifestRecord(**record.to_dict())
        parent = CorporateActionSnapshotStore(self.root)._safe_parent(
            record.symbol
        ).parent
        if self.path.exists() and self.path.is_symlink():
            raise CorporateActionError(
                "unsafe_manifest", "corporate-action manifest must not be a symlink"
            )
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT | nofollow,
                0o600,
            )
        except OSError as exc:
            raise CorporateActionError(
                "unsafe_manifest", "corporate-action manifest lock is unsafe"
            ) from exc
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            existing = read_corporate_action_manifest(self.root)
            same_id = [
                item for item in existing if item.snapshot_id == record.snapshot_id
            ]
            if same_id:
                if same_id != [record]:
                    raise CorporateActionError(
                        "manifest_conflict",
                        "corporate-action manifest identity conflicts",
                    )
                return
            line = _canonical_json_bytes(record.to_json_dict())
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow,
                0o644,
            )
            try:
                os.write(descriptor, line)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            parent_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def read_corporate_action_manifest(
    root: str | Path,
) -> tuple[CorporateActionManifestRecord, ...]:
    path = Path(root) / "data/corporate_actions/manifest.jsonl"
    if not path.exists():
        return ()
    if path.is_symlink() or not path.is_file():
        raise CorporateActionError(
            "unsafe_manifest", "corporate-action manifest is unsafe"
        )
    records: list[CorporateActionManifestRecord] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                raise CorporateActionError(
                    "invalid_manifest_record",
                    "corporate-action manifest contains an empty line",
                )
            values = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            record = CorporateActionManifestRecord.from_json_dict(values)
            if _canonical_json_bytes(record.to_json_dict()).decode().strip() != line:
                raise CorporateActionError(
                    "noncanonical_manifest_record",
                    "corporate-action manifest line is not canonical",
                )
            records.append(record)
    except UnicodeError as exc:
        raise CorporateActionError(
            "invalid_manifest_record",
            "corporate-action manifest is not UTF-8",
        ) from exc
    if len({record.snapshot_id for record in records}) != len(records):
        raise CorporateActionError(
            "manifest_conflict", "corporate-action manifest IDs are duplicated"
        )
    return tuple(records)


@dataclass(frozen=True)
class CorporateActionProvenance:
    snapshot_id: str
    file_sha256: str
    symbol: str
    instrument_kind: InstrumentKind
    provider: str
    source_schema: str
    normalization_version: str
    coverage_start: date
    coverage_end: date
    row_count: int
    verification_method: str


class VerifiedCorporateActions:
    """Exact-loader product required by formal backtests."""

    def __init__(
        self,
        *,
        events: tuple[CorporateActionEvent, ...],
        provenance: CorporateActionProvenance | None,
        _token: object,
    ):
        if _token is not _VERIFIED_ACTIONS_TOKEN:
            raise CorporateActionError(
                "unverified_corporate_actions",
                "corporate actions must come from the exact verified loader",
            )
        self._events = events
        self.provenance = provenance
        self._identity_digest = _verified_actions_digest(events, provenance)

    @property
    def events(self) -> tuple[CorporateActionEvent, ...]:
        return self._events


def _verified_actions_digest(
    events: tuple[CorporateActionEvent, ...],
    provenance: CorporateActionProvenance | None,
) -> str:
    if provenance is None:
        provenance_values = None
    else:
        provenance_values = {
            key: (
                value.isoformat()
                if isinstance(value, date)
                else value.value
                if isinstance(value, InstrumentKind)
                else value
            )
            for key, value in asdict(provenance).items()
        }
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "events": [event.to_json_dict() for event in events],
                "provenance": provenance_values,
            }
        )
    ).hexdigest()


def verify_verified_corporate_actions(actions: object) -> None:
    if type(actions) is not VerifiedCorporateActions:
        raise CorporateActionError(
            "unverified_corporate_actions",
            "formal price streams require exact verified corporate actions",
        )
    if (
        type(actions._events) is not tuple
        or actions.provenance is None
        or type(actions.provenance) is not CorporateActionProvenance
        or actions._identity_digest
        != _verified_actions_digest(actions._events, actions.provenance)
    ):
        raise CorporateActionError(
            "verified_corporate_actions_modified",
            "verified corporate actions were modified after loading",
        )


def make_synthetic_corporate_actions(
    events: tuple[CorporateActionEvent, ...],
    *,
    symbol: str,
    instrument_kind: InstrumentKind,
    coverage_start: date,
    coverage_end: date,
) -> VerifiedCorporateActions:
    """Create an explicitly synthetic verified fixture for engineering tests."""
    if (
        type(events) is not tuple
        or type(symbol) is not str
        or _SYMBOL_RE.fullmatch(symbol) is None
        or type(instrument_kind) is not InstrumentKind
        or type(coverage_start) is not date
        or type(coverage_end) is not date
        or coverage_start > coverage_end
        or any(
            type(event) is not CorporateActionEvent
            or event.symbol != symbol
            or event.instrument_kind is not instrument_kind
            or not coverage_start <= event.ex_date <= coverage_end
            or not coverage_start <= event.payable_date <= coverage_end
            for event in events
        )
    ):
        raise CorporateActionError(
            "invalid_synthetic_corporate_actions",
            "synthetic corporate-action fixture is invalid",
        )
    content_digest = hashlib.sha256(
        _canonical_json_bytes([event.to_json_dict() for event in events])
    ).hexdigest()
    provenance = CorporateActionProvenance(
        snapshot_id=content_digest,
        file_sha256=content_digest,
        symbol=symbol,
        instrument_kind=instrument_kind,
        provider="synthetic",
        source_schema="synthetic.cash.v1",
        normalization_version="cash-only-v1",
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        row_count=len(events),
        verification_method="synthetic_digest",
    )
    return VerifiedCorporateActions(
        events=events,
        provenance=provenance,
        _token=_VERIFIED_ACTIONS_TOKEN,
    )


def publish_corporate_actions(
    root: str | Path,
    events: tuple[CorporateActionEvent, ...],
    *,
    symbol: str,
    instrument_kind: InstrumentKind,
    provider: str,
    source_schema: str,
    normalization_version: str,
    coverage_start: date,
    coverage_end: date,
) -> CorporateActionManifestRecord:
    if type(instrument_kind) is not InstrumentKind or any(
        event.instrument_kind is not instrument_kind for event in events
    ):
        raise CorporateActionError(
            "invalid_event_collection",
            "event instrument kind does not match publication",
        )
    artifact = CorporateActionSnapshotStore(root).write(events, symbol=symbol)
    record = CorporateActionManifestRecord.create(
        schema_version="1.0",
        symbol=symbol,
        instrument_kind=instrument_kind.value,
        provider=provider,
        source_schema=source_schema,
        normalization_version=normalization_version,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        row_count=artifact.row_count,
        snapshot_relative_path=artifact.relative_path,
        file_sha256=artifact.sha256,
    )
    CorporateActionManifestWriter(root).append(record)
    return record


def load_verified_corporate_actions(
    root: str | Path,
    record: CorporateActionManifestRecord,
) -> VerifiedCorporateActions:
    if type(record) is not CorporateActionManifestRecord:
        raise TypeError("record must be an exact CorporateActionManifestRecord")
    CorporateActionManifestRecord(**record.to_dict())
    store = CorporateActionSnapshotStore(root)
    content = store.verify(record.snapshot_relative_path, record.file_sha256)
    try:
        values = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CorporateActionError(
            "invalid_snapshot_content",
            "corporate-action snapshot JSON is invalid",
        ) from exc
    if (
        type(values) is not dict
        or set(values) != {"events", "schema_version"}
        or values["schema_version"] != "1.0"
        or type(values["events"]) is not list
    ):
        raise CorporateActionError(
            "invalid_snapshot_content",
            "corporate-action snapshot shape is invalid",
        )
    events = tuple(
        CorporateActionEvent.from_json_dict(item) for item in values["events"]
    )
    if len(events) != record.row_count:
        raise CorporateActionError(
            "manifest_content_mismatch",
            "corporate-action row count does not match manifest",
        )
    kind = InstrumentKind(record.instrument_kind)
    if any(
        event.symbol != record.symbol
        or event.instrument_kind is not kind
        or not (record.coverage_start <= event.ex_date <= record.coverage_end)
        for event in events
    ):
        raise CorporateActionError(
            "manifest_content_mismatch",
            "corporate-action identity or coverage does not match manifest",
        )
    if _canonical_json_bytes(
        {
            "events": [event.to_json_dict() for event in events],
            "schema_version": "1.0",
        }
    ) != content:
        raise CorporateActionError(
            "noncanonical_snapshot_content",
            "corporate-action snapshot is not canonical",
        )
    store.verify(record.snapshot_relative_path, record.file_sha256)
    return VerifiedCorporateActions(
        events=events,
        provenance=CorporateActionProvenance(
            snapshot_id=record.snapshot_id,
            file_sha256=record.file_sha256,
            symbol=record.symbol,
            instrument_kind=kind,
            provider=record.provider,
            source_schema=record.source_schema,
            normalization_version=record.normalization_version,
            coverage_start=record.coverage_start,
            coverage_end=record.coverage_end,
            row_count=record.row_count,
            verification_method="manifest_sha256",
        ),
        _token=_VERIFIED_ACTIONS_TOKEN,
    )
