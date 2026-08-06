"""All-or-nothing validation and provenance orchestration for market data."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from aquant.config import ConfigError, DataConfig, validate_data_config
from aquant.data.akshare_client import (
    AkshareClient,
    RawFetchResult,
    SourceContractError,
    validate_raw_fetch_result,
)
from aquant.data.calendar_snapshot import CalendarRecord, CalendarSnapshotStore
from aquant.data.manifest import ManifestRecord, ManifestWriter
from aquant.data.normalize import NormalizationError, normalize_market_frame
from aquant.data.quality import DataQualityError, QualityReport, validate_market_frame
from aquant.data.snapshot import RawSnapshotStore, SnapshotArtifact
from aquant.logging import log_event

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class IngestionError(RuntimeError):
    """Raised when the batch cannot safely reach the persistence phase."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RunItemResult:
    symbol: str
    provider: str
    source_function: str
    actual_start: date
    actual_end: date
    row_count: int
    snapshot_relative_path: Path
    snapshot_sha256: str
    snapshot_reused: bool
    manifest_status: str
    snapshot_id: str


@dataclass(frozen=True)
class RunResult:
    requested_start: date
    requested_end: date
    fetched_at_utc: datetime
    items: tuple[RunItemResult, ...]
    calendar_record: CalendarRecord
    missing_sessions: tuple[SymbolMissingSessions, ...]


@dataclass(frozen=True)
class SymbolMissingSessions:
    symbol: str
    dates: tuple[date, ...]


@dataclass(frozen=True)
class _Validated:
    raw: RawFetchResult
    sliced_raw_frame: pd.DataFrame
    report: QualityReport
    missing_sessions: tuple[date, ...]


@dataclass(frozen=True)
class _CalendarWindow:
    dates: tuple[date, ...]
    latest_complete: date


def _validated_trade_calendar(
    *, now: datetime, trade_calendar_provider: Callable[[], pd.DataFrame]
) -> _CalendarWindow:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise IngestionError("invalid_clock", "clock must return a timezone-aware datetime")
    today = now.astimezone(_SHANGHAI).date()
    try:
        calendar = trade_calendar_provider()
    except Exception as exc:
        raise IngestionError(
            "calendar_fetch_failed",
            f"trade calendar fetch failed ({type(exc).__name__})",
        ) from exc
    if not isinstance(calendar, pd.DataFrame) or "trade_date" not in calendar.columns:
        raise IngestionError("invalid_calendar", "trade calendar schema is invalid")
    try:
        values = pd.to_datetime(calendar["trade_date"], errors="raise")
    except (TypeError, ValueError) as exc:
        raise IngestionError("invalid_calendar", "trade calendar values are invalid") from exc
    if (
        values.empty
        or values.isna().any()
        or values.dt.tz is not None
        or values.ne(values.dt.normalize()).any()
        or values.dt.dayofweek.ge(5).any()
    ):
        raise IngestionError("invalid_calendar", "trade calendar values are invalid")
    calendar_dates = values.dt.date
    if calendar_dates.duplicated().any() or not calendar_dates.is_monotonic_increasing:
        raise IngestionError("invalid_calendar", "trade calendar values are invalid")
    prior = tuple(calendar_dates[calendar_dates < today])
    if not prior:
        raise IngestionError(
            "no_complete_trading_day", "trade calendar has no prior complete trading day"
        )
    return _CalendarWindow(
        dates=prior,
        latest_complete=prior[-1],
    )


def _validate_batch(
    raw_results: tuple[RawFetchResult, ...],
    *,
    config: DataConfig,
    requested_end: date,
    calendar_dates: tuple[date, ...],
) -> tuple[_Validated, ...]:
    calendar_date_set = frozenset(calendar_dates)
    expected_calendar_dates = frozenset(
        value for value in calendar_dates if config.start <= value <= requested_end
    )
    expected = tuple(item.symbol for item in config.instruments)
    actual = tuple(item.symbol for item in raw_results)
    if (
        len(actual) != len(expected)
        or set(actual) != set(expected)
        or len(set(actual)) != len(actual)
    ):
        raise IngestionError("batch_mismatch", "fetched batch does not match configured universe")
    by_symbol = {item.symbol: item for item in raw_results}
    validated: list[_Validated] = []
    for instrument in config.instruments:
        raw = by_symbol[instrument.symbol]
        try:
            validate_raw_fetch_result(raw)
        except SourceContractError as exc:
            raise IngestionError(
                "source_contract_violation", "fetched source contract is invalid"
            ) from exc
        if raw.instrument_kind != instrument.kind:
            raise IngestionError("batch_mismatch", "fetched instrument kind is inconsistent")
        if raw.local_date_slice and not raw.full_history_download:
            raise IngestionError("batch_mismatch", "source download flags are inconsistent")
        try:
            normalized = normalize_market_frame(raw.frame, source_schema=raw.source_schema)
            if normalized["date"].isna().any():
                raise IngestionError(
                    "batch_validation_failed",
                    f"batch validation failed for symbol {instrument.symbol} (invalid date)",
                )
            in_range = normalized["date"].dt.date.between(config.start, requested_end)
            if not raw.local_date_slice and not bool(in_range.all()):
                raise IngestionError(
                    "source_range_violation",
                    f"explicit-range source returned out-of-range rows for {instrument.symbol}",
                )
            sliced_normalized = normalized.loc[in_range.to_numpy()].reset_index(drop=True)
            sliced_raw = raw.frame.loc[in_range.to_numpy()].reset_index(drop=True)
            if sliced_normalized.empty:
                raise IngestionError(
                    "empty_requested_interval", "market data has an empty requested interval"
                )
            if not bool(sliced_normalized["date"].dt.date.isin(calendar_date_set).all()):
                raise IngestionError(
                    "non_trading_date",
                    f"market data contains a non-trading date for {instrument.symbol}",
                )
            report = validate_market_frame(sliced_normalized)
        except IngestionError:
            raise
        except (NormalizationError, DataQualityError) as exc:
            raise IngestionError(
                "batch_validation_failed",
                f"batch validation failed for symbol {instrument.symbol} ({type(exc).__name__})",
            ) from exc
        if len(sliced_raw) != report.row_count:
            raise IngestionError(
                "slice_mismatch", "raw and normalized date slices are inconsistent"
            )
        if report.end_date != requested_end:
            raise IngestionError(
                "stale_market_data",
                f"market data does not reach the requested end for {instrument.symbol}",
            )
        actual_dates = frozenset(sliced_normalized["date"].dt.date)
        missing_sessions = tuple(sorted(expected_calendar_dates - actual_dates))
        validated.append(
            _Validated(
                raw=raw,
                sliced_raw_frame=sliced_raw,
                report=report,
                missing_sessions=missing_sessions,
            )
        )
    return tuple(validated)


def run_ingestion(
    config: DataConfig,
    *,
    client: AkshareClient,
    clock: Callable[[], datetime],
    trade_calendar_provider: Callable[[], pd.DataFrame],
    snapshot_store: RawSnapshotStore,
    manifest_writer: ManifestWriter,
    calendar_store: CalendarSnapshotStore | None = None,
    akshare_version: str,
    logger: logging.Logger | None = None,
) -> RunResult:
    """Fetch and validate the full verified universe before writing artifacts."""
    try:
        validate_data_config(config)
    except ConfigError as exc:
        raise IngestionError("invalid_config", "run requires a validated DataConfig") from exc
    now = clock()
    calendar = _validated_trade_calendar(now=now, trade_calendar_provider=trade_calendar_provider)
    requested_end = calendar.latest_complete
    if requested_end < config.start:
        raise IngestionError("invalid_range", "complete trading day precedes configured start")
    fetched_at_utc = now.astimezone(UTC)
    raw_results = client.fetch_batch(
        config.instruments,
        start=config.start,
        end=requested_end,
    )
    validated = _validate_batch(
        raw_results,
        config=config,
        requested_end=requested_end,
        calendar_dates=calendar.dates,
    )
    active_calendar_store = calendar_store or CalendarSnapshotStore(snapshot_store.root)
    calendar_record = active_calendar_store.write(
        calendar.dates,
        source_provider="sina",
        source_function="tool_trade_date_hist_sina",
        source_version=akshare_version,
        fetched_at_utc=fetched_at_utc,
    )
    snapshot_date = now.astimezone(_SHANGHAI).date()

    pending: list[tuple[RawFetchResult, QualityReport, SnapshotArtifact, ManifestRecord]] = []
    for prepared in validated:
        raw = prepared.raw
        report = prepared.report
        artifact = snapshot_store.write(
            prepared.sliced_raw_frame,
            symbol=raw.symbol,
            source_slug=raw.provider,
            snapshot_date=snapshot_date,
        )
        if report.start_date is None or report.end_date is None:
            raise IngestionError("empty_report", "validated quality report has no date range")
        record = ManifestRecord.create(
            schema_version="1.0",
            symbol=raw.symbol,
            instrument_kind=raw.instrument_kind,
            provider=raw.provider,
            source_function=raw.source_function,
            source_schema=raw.source_schema.value,
            endpoint_host=raw.endpoint_host,
            provider_symbol=raw.provider_symbol,
            fetched_at_utc=fetched_at_utc,
            requested_start=config.start,
            requested_end=requested_end,
            actual_start=report.start_date,
            actual_end=report.end_date,
            row_count=report.row_count,
            snapshot_relative_path=artifact.relative_path,
            file_sha256=artifact.sha256,
            adjustment=config.adjust,
            factor_source=None,
            latest_market_date=report.end_date,
            akshare_version=akshare_version,
            raw_volume_unit=raw.raw_volume_unit,
            volume_multiplier_to_canonical=raw.volume_multiplier_to_canonical,
            full_history_download=raw.full_history_download,
            local_date_slice=raw.local_date_slice,
            quality_issue_counts=report.issue_counts,
        )
        pending.append((raw, report, artifact, record))

    append_results = manifest_writer.append_batch(tuple(item[3] for item in pending))
    if len(append_results) != len(pending):
        raise IngestionError("manifest_batch_mismatch", "manifest batch result count is invalid")

    items: list[RunItemResult] = []
    for (raw, report, artifact, _record), append_result in zip(
        pending, append_results, strict=True
    ):
        items.append(
            RunItemResult(
                symbol=raw.symbol,
                provider=raw.provider,
                source_function=raw.source_function,
                actual_start=report.start_date,
                actual_end=report.end_date,
                row_count=report.row_count,
                snapshot_relative_path=artifact.relative_path,
                snapshot_sha256=artifact.sha256,
                snapshot_reused=artifact.reused,
                manifest_status=append_result.status,
                snapshot_id=append_result.snapshot_id,
            )
        )
    result = RunResult(
        requested_start=config.start,
        requested_end=requested_end,
        fetched_at_utc=fetched_at_utc,
        items=tuple(items),
        calendar_record=calendar_record,
        missing_sessions=tuple(
            SymbolMissingSessions(prepared.raw.symbol, prepared.missing_sessions)
            for prepared in validated
            if prepared.missing_sessions
        ),
    )
    log_event(
        logger,
        logging.INFO,
        "market_data_ingestion_completed",
        requested_start=result.requested_start,
        requested_end=result.requested_end,
        instrument_count=len(result.items),
        calendar_id=result.calendar_record.calendar_id,
        missing_session_count=sum(len(item.dates) for item in result.missing_sessions),
    )
    return result
