"""Fail-closed quality checks for normalized daily market data."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from aquant.data.normalize import NUMERIC_MARKET_COLUMNS, REQUIRED_MARKET_COLUMNS


@dataclass(frozen=True)
class QualityReport:
    """Structured counts produced by the daily market-data quality gate."""

    row_count: int
    start_date: date | None
    end_date: date | None
    empty_frame_count: int = 0
    null_count: int = 0
    duplicate_date_count: int = 0
    out_of_order_date_count: int = 0
    non_finite_numeric_count: int = 0
    non_positive_price_count: int = 0
    negative_volume_count: int = 0
    negative_amount_count: int = 0
    invalid_high_count: int = 0
    invalid_low_count: int = 0

    @property
    def issue_counts(self) -> dict[str, int]:
        """Return every anomaly counter with a stable machine-readable key."""
        return {
            "empty_frame": self.empty_frame_count,
            "null": self.null_count,
            "duplicate_date": self.duplicate_date_count,
            "out_of_order_date": self.out_of_order_date_count,
            "non_finite_numeric": self.non_finite_numeric_count,
            "non_positive_price": self.non_positive_price_count,
            "negative_volume": self.negative_volume_count,
            "negative_amount": self.negative_amount_count,
            "invalid_high": self.invalid_high_count,
            "invalid_low": self.invalid_low_count,
        }

    @property
    def anomaly_count(self) -> int:
        """Return the sum of all anomaly counters."""
        return sum(self.issue_counts.values())


class DataQualityError(ValueError):
    """Raised when a market frame cannot pass the quality gate."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        report: QualityReport | None = None,
    ):
        self.code = code
        self.details = dict(details or {})
        self.report = report
        super().__init__(message)


def _validate_schema(frame: pd.DataFrame) -> None:
    actual = tuple(str(column) for column in frame.columns)
    missing = tuple(column for column in REQUIRED_MARKET_COLUMNS if column not in frame.columns)
    if missing:
        details = {
            "expected": REQUIRED_MARKET_COLUMNS,
            "actual": actual,
            "missing": missing,
        }
        raise DataQualityError(
            "missing_columns",
            "market data schema is incomplete: "
            f"expected={REQUIRED_MARKET_COLUMNS!r}; actual={actual!r}; missing={missing!r}",
            details=details,
        )

    duplicate_columns = tuple(
        dict.fromkeys(str(column) for column in frame.columns[frame.columns.duplicated(keep=False)])
    )
    if duplicate_columns:
        raise DataQualityError(
            "duplicate_columns",
            f"market data schema contains duplicate columns: columns={duplicate_columns!r}",
            details={"columns": duplicate_columns},
        )


def _validate_dtypes(frame: pd.DataFrame) -> None:
    invalid: list[str] = []
    if not pd.api.types.is_datetime64_any_dtype(frame["date"].dtype):
        invalid.append("date")
    elif frame["date"].dt.tz is not None:
        invalid.append("date")

    for column in NUMERIC_MARKET_COLUMNS:
        dtype = frame[column].dtype
        if (
            not pd.api.types.is_numeric_dtype(dtype)
            or pd.api.types.is_bool_dtype(dtype)
            or pd.api.types.is_complex_dtype(dtype)
        ):
            invalid.append(column)
    if invalid:
        columns = tuple(invalid)
        raise DataQualityError(
            "invalid_dtypes",
            f"market data must be normalized before validation: columns={columns!r}",
            details={"columns": columns},
        )

    non_midnight_count = int(
        (frame["date"].notna() & frame["date"].ne(frame["date"].dt.normalize())).sum()
    )
    if non_midnight_count:
        raise DataQualityError(
            "invalid_date_granularity",
            "daily market data contains non-midnight timestamps",
            details={"count": non_midnight_count},
        )


def _finite_mask(series: pd.Series) -> pd.Series:
    return series.map(lambda value: False if pd.isna(value) else math.isfinite(value))


def validate_market_frame(frame: pd.DataFrame) -> QualityReport:
    """Validate a normalized daily market frame and return its quality report.

    Validation never reorders or repairs rows. Any anomaly raises a
    :class:`DataQualityError` carrying the full counter-only report.
    """
    if not isinstance(frame, pd.DataFrame):
        raise DataQualityError(
            "invalid_frame_type",
            "market data validation requires a pandas DataFrame",
            details={"expected_type": "DataFrame", "actual_type": type(frame).__name__},
        )

    _validate_schema(frame)
    _validate_dtypes(frame)

    required = frame.loc[:, REQUIRED_MARKET_COLUMNS]
    null_count = int(required.isna().sum().sum())

    valid_dates = frame["date"].dropna()
    duplicate_date_count = int(valid_dates.duplicated(keep="first").sum())
    out_of_order_date_count = int(valid_dates.diff().lt(pd.Timedelta(0)).sum())

    finite_masks = {column: _finite_mask(frame[column]) for column in NUMERIC_MARKET_COLUMNS}
    non_finite_numeric_count = sum(
        int((frame[column].notna() & ~finite_masks[column]).sum())
        for column in NUMERIC_MARKET_COLUMNS
    )

    prices = ("open", "high", "low", "close")
    non_positive_price_count = sum(
        int((frame[column].notna() & finite_masks[column] & frame[column].le(0)).sum())
        for column in prices
    )
    negative_volume_count = int(
        (frame["volume"].notna() & finite_masks["volume"] & frame["volume"].lt(0)).sum()
    )
    negative_amount_count = int(
        (frame["amount"].notna() & finite_masks["amount"] & frame["amount"].lt(0)).sum()
    )

    finite_price_rows = pd.concat([finite_masks[column] for column in prices], axis=1).all(axis=1)
    invalid_high_count = int(
        (
            finite_price_rows & frame["high"].lt(frame.loc[:, ["open", "close", "low"]].max(axis=1))
        ).sum()
    )
    invalid_low_count = int(
        (
            finite_price_rows & frame["low"].gt(frame.loc[:, ["open", "close", "high"]].min(axis=1))
        ).sum()
    )

    start_date = valid_dates.min().date() if not valid_dates.empty else None
    end_date = valid_dates.max().date() if not valid_dates.empty else None
    report = QualityReport(
        row_count=len(frame),
        start_date=start_date,
        end_date=end_date,
        empty_frame_count=int(frame.empty),
        null_count=null_count,
        duplicate_date_count=duplicate_date_count,
        out_of_order_date_count=out_of_order_date_count,
        non_finite_numeric_count=non_finite_numeric_count,
        non_positive_price_count=non_positive_price_count,
        negative_volume_count=negative_volume_count,
        negative_amount_count=negative_amount_count,
        invalid_high_count=invalid_high_count,
        invalid_low_count=invalid_low_count,
    )
    if report.anomaly_count:
        failures = {key: count for key, count in report.issue_counts.items() if count}
        raise DataQualityError(
            "quality_violations",
            f"market data failed quality checks: counts={failures!r}",
            details={"counts": failures},
            report=report,
        )
    return report
