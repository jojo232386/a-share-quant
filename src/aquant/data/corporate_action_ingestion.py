"""Explicit acquisition boundary for immutable corporate-action snapshots."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from aquant.config import ConfigError, DataConfig, validate_data_config
from aquant.data.akshare_client import AkshareClient
from aquant.data.corporate_actions import (
    CorporateActionEvent,
    CorporateActionManifestRecord,
    load_verified_corporate_actions,
    publish_corporate_actions,
)
from aquant.data.ingestion import IngestionError, _validated_trade_calendar
from aquant.rules import InstrumentKind

_SOURCE_CONTRACTS = {
    InstrumentKind.MAIN_BOARD_STOCK: (
        "cninfo",
        "akshare.stock_dividend_cninfo.v1",
    ),
    InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF: (
        "sina",
        "akshare.fund_etf_dividend_sina+sina.detail.v1",
    ),
}


@dataclass(frozen=True)
class CorporateActionIngestionResult:
    symbol: str
    requested_start: date
    requested_end: date
    event_count: int
    snapshot_id: str
    file_sha256: str
    manifest_record: CorporateActionManifestRecord


def run_corporate_action_ingestion(
    config: DataConfig,
    *,
    symbol: str,
    client: AkshareClient,
    clock: Callable[[], datetime],
    trade_calendar_provider: Callable[[], pd.DataFrame],
    project_root,
) -> CorporateActionIngestionResult:
    """Fetch, publish, and re-open one symbol without involving a backtest."""
    try:
        validate_data_config(config)
    except ConfigError as exc:
        raise IngestionError(
            "invalid_config",
            "corporate-action ingestion requires a validated config",
        ) from exc
    matches = tuple(item for item in config.instruments if item.symbol == symbol)
    if len(matches) != 1:
        raise IngestionError(
            "unsupported_instrument",
            "corporate-action symbol must be one configured instrument",
        )
    calendar = _validated_trade_calendar(
        now=clock(),
        trade_calendar_provider=trade_calendar_provider,
    )
    if calendar.latest_complete < config.start:
        raise IngestionError(
            "invalid_range",
            "complete trading day precedes configured start",
        )
    instrument = matches[0]
    kind = InstrumentKind(instrument.kind)
    provider, source_schema = _SOURCE_CONTRACTS[kind]
    events = client.fetch_corporate_actions(
        instrument,
        start=config.start,
        end=calendar.latest_complete,
    )
    if type(events) is not tuple or any(
        type(event) is not CorporateActionEvent
        or event.symbol != instrument.symbol
        or event.instrument_kind is not kind
        or event.source_schema != source_schema
        for event in events
    ):
        raise IngestionError(
            "corporate_action_contract_violation",
            "corporate-action source result violates the pinned contract",
        )
    record = publish_corporate_actions(
        project_root,
        events,
        symbol=instrument.symbol,
        instrument_kind=kind,
        provider=provider,
        source_schema=source_schema,
        normalization_version="cash-only-v1",
        coverage_start=config.start,
        coverage_end=calendar.latest_complete,
    )
    verified = load_verified_corporate_actions(project_root, record)
    if verified.events != events or verified.provenance is None:
        raise IngestionError(
            "corporate_action_postcondition_failed",
            "published corporate actions failed the verified re-open check",
        )
    return CorporateActionIngestionResult(
        symbol=instrument.symbol,
        requested_start=config.start,
        requested_end=calendar.latest_complete,
        event_count=len(events),
        snapshot_id=record.snapshot_id,
        file_sha256=record.file_sha256,
        manifest_record=record,
    )
