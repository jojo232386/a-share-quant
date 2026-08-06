"""Normalize AKShare market-data frames into one canonical schema."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import pandas as pd

REQUIRED_MARKET_COLUMNS = ("date", "open", "high", "low", "close", "volume", "amount")
NUMERIC_MARKET_COLUMNS = ("open", "high", "low", "close", "volume", "amount")
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

_ALL_COLUMN_ALIASES = {
    "date": "date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "amount": "amount",
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
}


class SourceSchema(StrEnum):
    """Supported, unit-aware market-data source schemas."""

    STOCK_EASTMONEY = "akshare.stock_zh_a_hist"
    STOCK_SINA = "akshare.stock_zh_a_daily"
    ETF_EASTMONEY = "akshare.fund_etf_hist_em"
    ETF_SINA = "akshare.fund_etf_hist_sina"
    SYNTHETIC_PUBLIC_FIXTURE = "synthetic_public_fixture"


@dataclass(frozen=True)
class _SourceProfile:
    column_aliases: Mapping[str, str]
    volume_multiplier_to_canonical_units: int


_EASTMONEY_COLUMN_ALIASES = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
}
_SINA_COLUMN_ALIASES = {column: column for column in REQUIRED_MARKET_COLUMNS}

_SOURCE_PROFILES = {
    SourceSchema.STOCK_EASTMONEY: _SourceProfile(
        column_aliases=_EASTMONEY_COLUMN_ALIASES,
        volume_multiplier_to_canonical_units=100,
    ),
    SourceSchema.STOCK_SINA: _SourceProfile(
        column_aliases=_SINA_COLUMN_ALIASES,
        volume_multiplier_to_canonical_units=1,
    ),
    SourceSchema.ETF_EASTMONEY: _SourceProfile(
        column_aliases=_EASTMONEY_COLUMN_ALIASES,
        volume_multiplier_to_canonical_units=100,
    ),
    SourceSchema.ETF_SINA: _SourceProfile(
        column_aliases=_SINA_COLUMN_ALIASES,
        volume_multiplier_to_canonical_units=1,
    ),
    SourceSchema.SYNTHETIC_PUBLIC_FIXTURE: _SourceProfile(
        column_aliases=_SINA_COLUMN_ALIASES,
        volume_multiplier_to_canonical_units=1,
    ),
}


class NormalizationError(ValueError):
    """Raised when a source frame cannot be normalized without ambiguity."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


def _resolve_source_profile(
    source_schema: SourceSchema | str | None,
) -> tuple[SourceSchema, _SourceProfile]:
    if source_schema is None:
        raise NormalizationError(
            "missing_source_profile",
            "market data normalization requires an explicit source schema",
            details={"supported": tuple(schema.value for schema in SourceSchema)},
        )
    try:
        schema = SourceSchema(source_schema)
    except (TypeError, ValueError) as exc:
        raise NormalizationError(
            "unknown_source_profile",
            "market data source schema is unsupported",
            details={"supported": tuple(schema.value for schema in SourceSchema)},
        ) from exc
    return schema, _SOURCE_PROFILES[schema]


def _schema_details(
    columns: pd.Index, profile: _SourceProfile
) -> tuple[dict[str, str], tuple[str, ...], dict[str, Any]]:
    actual = tuple(str(column) for column in columns)
    duplicate_labels = tuple(
        dict.fromkeys(str(column) for column in columns[columns.duplicated(keep=False)])
    )
    if duplicate_labels:
        raise NormalizationError(
            "duplicate_source_columns",
            f"market data has duplicate source columns: columns={duplicate_labels!r}",
            details={"source_columns": duplicate_labels},
        )

    all_canonical_sources: dict[str, list[str]] = {}
    for column in columns:
        if not isinstance(column, str):
            continue
        canonical = _ALL_COLUMN_ALIASES.get(column)
        if canonical is None:
            continue
        all_canonical_sources.setdefault(canonical, []).append(column)

    for canonical, source_columns in all_canonical_sources.items():
        if len(source_columns) > 1:
            sources = tuple(source_columns)
            raise NormalizationError(
                "duplicate_mapping",
                f"multiple source columns map to canonical column {canonical!r}: "
                f"source_columns={sources!r}",
                details={"canonical": canonical, "source_columns": sources},
            )

    rename_map: dict[str, str] = {}
    canonical_sources: dict[str, list[str]] = {}
    for column in columns:
        if not isinstance(column, str):
            continue
        canonical = profile.column_aliases.get(column)
        if canonical is not None:
            rename_map[column] = canonical
            canonical_sources.setdefault(canonical, []).append(column)

    missing = tuple(column for column in REQUIRED_MARKET_COLUMNS if column not in canonical_sources)
    details = {
        "expected": REQUIRED_MARKET_COLUMNS,
        "actual": actual,
        "missing": missing,
    }
    return rename_map, missing, details


def _normalize_date_column(series: pd.Series) -> pd.Series:
    normalized: list[pd.Timestamp | pd.NaT] = []
    for position, value in enumerate(series):
        if not pd.api.types.is_scalar(value):
            raise NormalizationError(
                "invalid_date",
                f"date normalization failed at row position {position}",
                details={"column": "date", "position": position},
            )
        if pd.isna(value):
            normalized.append(pd.NaT)
            continue
        if pd.api.types.is_number(value) and not isinstance(value, (date, datetime)):
            raise NormalizationError(
                "invalid_date",
                f"date normalization failed at row position {position}",
                details={"column": "date", "position": position},
            )
        try:
            timestamp = pd.Timestamp(value)
            if timestamp.tzinfo is not None:
                raise NormalizationError(
                    "invalid_date_timezone",
                    f"date normalization requires a timezone-free value at row position {position}",
                    details={"column": "date", "position": position},
                )
            if timestamp != timestamp.normalize():
                raise NormalizationError(
                    "invalid_date_granularity",
                    f"date normalization requires a midnight value at row position {position}",
                    details={"column": "date", "position": position},
                )
            normalized.append(timestamp)
        except NormalizationError:
            raise
        except (TypeError, ValueError, OverflowError) as exc:
            raise NormalizationError(
                "invalid_date",
                f"date normalization failed at row position {position}",
                details={"column": "date", "position": position},
            ) from exc

    try:
        return pd.Series(normalized, index=series.index, dtype="datetime64[ns]")
    except (TypeError, ValueError, OverflowError) as exc:
        raise NormalizationError(
            "invalid_date",
            "date normalization failed because a value is outside the supported range",
            details={"column": "date"},
        ) from exc


def _normalize_numeric_column(series: pd.Series, column: str) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series.dtype) or pd.api.types.is_timedelta64_dtype(
        series.dtype
    ):
        raise NormalizationError(
            "invalid_numeric",
            f"numeric normalization failed for column {column!r}",
            details={"column": column},
        )
    boolean_positions = [
        position for position, value in enumerate(series) if pd.api.types.is_bool(value)
    ]
    if boolean_positions:
        position = boolean_positions[0]
        raise NormalizationError(
            "invalid_numeric",
            f"numeric normalization failed for column {column!r} at row position {position}",
            details={"column": column, "position": position},
        )
    try:
        converted = pd.to_numeric(series, errors="raise")
    except (TypeError, ValueError) as exc:
        raise NormalizationError(
            "invalid_numeric",
            f"numeric normalization failed for column {column!r}",
            details={"column": column},
        ) from exc
    if pd.api.types.is_complex_dtype(converted.dtype):
        raise NormalizationError(
            "invalid_numeric",
            f"numeric normalization failed for column {column!r}: complex values are unsupported",
            details={"column": column},
        )
    return converted


def _convert_volume_to_canonical_units(series: pd.Series, multiplier: int) -> pd.Series:
    if multiplier == 1:
        return series

    minimum_source_value = -((-_INT64_MIN) // multiplier)
    maximum_source_value = _INT64_MAX // multiplier
    overflow = series.lt(minimum_source_value) | series.gt(maximum_source_value)
    if bool(overflow.fillna(False).any()):
        raise NormalizationError(
            "numeric_overflow",
            "numeric normalization failed for column 'volume' during unit conversion",
            details={"column": "volume", "multiplier": multiplier},
        )
    return series * multiplier


def normalize_market_frame(
    frame: pd.DataFrame, *, source_schema: SourceSchema | str | None = None
) -> pd.DataFrame:
    """Return a copy with canonical market columns first and source extras preserved.

    The function accepts the Chinese column names emitted by AKShare's Eastmoney
    endpoint and the English names emitted by its Sina endpoint. The source schema
    is mandatory because Eastmoney reports stock and ETF volume in lots while Sina
    reports shares or ETF units. Canonical ``volume`` is shares for stocks and units
    for ETFs; ``amount`` is yuan. The function is deliberately fail-closed when the
    source is unknown, a required field is absent, or two source fields would map to
    one canonical field.
    """
    if not isinstance(frame, pd.DataFrame):
        raise NormalizationError(
            "invalid_frame_type",
            "market data normalization requires a pandas DataFrame",
            details={"expected_type": "DataFrame", "actual_type": type(frame).__name__},
        )

    _, profile = _resolve_source_profile(source_schema)
    rename_map, missing, details = _schema_details(frame.columns, profile)
    if missing:
        raise NormalizationError(
            "missing_columns",
            "market data schema is incomplete: "
            f"expected={details['expected']!r}; actual={details['actual']!r}; "
            f"missing={missing!r}",
            details=details,
        )

    result = frame.rename(columns=rename_map).copy()
    extras = [column for column in result.columns if column not in REQUIRED_MARKET_COLUMNS]
    result = result.loc[:, [*REQUIRED_MARKET_COLUMNS, *extras]]
    result["date"] = _normalize_date_column(result["date"])
    for column in NUMERIC_MARKET_COLUMNS:
        result[column] = _normalize_numeric_column(result[column], column)
    result["volume"] = _convert_volume_to_canonical_units(
        result["volume"], profile.volume_multiplier_to_canonical_units
    )
    return result
