"""Canonical market-data types and validation gates."""

from aquant.data.calendar_snapshot import (
    CalendarError,
    CalendarRecord,
    CalendarSnapshotStore,
    VerifiedTradingCalendar,
    load_verified_calendar,
)
from aquant.data.normalize import NormalizationError, SourceSchema, normalize_market_frame
from aquant.data.quality import DataQualityError, QualityReport, validate_market_frame

__all__ = [
    "CalendarError",
    "CalendarRecord",
    "CalendarSnapshotStore",
    "DataQualityError",
    "NormalizationError",
    "QualityReport",
    "SourceSchema",
    "VerifiedTradingCalendar",
    "load_verified_calendar",
    "normalize_market_frame",
    "validate_market_frame",
]
