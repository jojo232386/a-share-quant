"""Deterministic public-only inputs for the v0.1 engineering fixture.

The data generated here are artificial. They preserve the release pipeline's
provenance and replay interfaces, but are neither historical A-share prices nor
evidence about a strategy, market behaviour, profitability, or live execution.
The calendar is an artificial weekday-derived schedule, not an official exchange
calendar: a deterministic set of synthetic non-sessions preserves the v0.1
fixture's fixed session count without representing market closures.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from aquant.data.calendar_snapshot import CalendarSnapshotStore
from aquant.data.corporate_actions import (
    CorporateActionEvent,
    publish_corporate_actions,
)
from aquant.data.manifest import ManifestRecord, ManifestWriter
from aquant.data.normalize import SourceSchema
from aquant.data.snapshot import RawSnapshotStore
from aquant.rules import InstrumentKind
from aquant.universe import UniverseMember, canonical_universe_bytes

PUBLIC_FIXTURE_SCHEMA = SourceSchema.SYNTHETIC_PUBLIC_FIXTURE.value
PUBLIC_FIXTURE_VERSION = "v1"
_FIXTURE_TIMESTAMP = datetime(2026, 7, 25, tzinfo=UTC)
_START = date(2018, 1, 2)
_END = date(2026, 7, 24)
_SNAPSHOT_DATE = date(2026, 7, 25)
_SESSION_COUNT = 2074
_SYNTHETIC_NON_SESSION_COUNT = 160
_SYMBOLS = (
    "000001",
    "000858",
    "510300",
    "510500",
    "600030",
    "600036",
    "600519",
    "600900",
    "601166",
    "601318",
)
_ETF_SYMBOLS = frozenset({"510300", "510500"})


@dataclass(frozen=True)
class PublicV01Inputs:
    """Stable identities for a generated public fixture input closure."""

    symbols: tuple[str, ...]
    universe_id: str
    calendar_id: str
    calendar_first_date: date
    calendar_last_date: date
    calendar_row_count: int
    calendar_source: str
    fixture_version: str
    market_snapshot_ids: tuple[tuple[str, str], ...]
    corporate_action_snapshot_ids: tuple[tuple[str, str], ...]


def _kind(symbol: str) -> InstrumentKind:
    return (
        InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF
        if symbol in _ETF_SYMBOLS
        else InstrumentKind.MAIN_BOARD_STOCK
    )


def _weekday_sessions() -> tuple[date, ...]:
    weekdays: list[date] = []
    current = _START
    while current <= _END:
        if current.weekday() < 5:
            weekdays.append(current)
        current += timedelta(days=1)
    if len(weekdays) - _SYNTHETIC_NON_SESSION_COUNT != _SESSION_COUNT:
        raise RuntimeError("synthetic_fixture_session_count_mismatch")
    required_sessions = {
        _START,
        date(2026, 7, 23),
        _END,
    }
    candidate_indices = [
        index
        for index, value in enumerate(weekdays)
        if value not in required_sessions
    ]
    removal_positions = {
        index
        * (len(candidate_indices) - 1)
        // (_SYNTHETIC_NON_SESSION_COUNT - 1)
        for index in range(_SYNTHETIC_NON_SESSION_COUNT)
    }
    removed_indices = {
        candidate_indices[position]
        for position in removal_positions
    }
    sessions = tuple(
        value
        for index, value in enumerate(weekdays)
        if index not in removed_indices
    )
    if len(sessions) != _SESSION_COUNT:
        raise RuntimeError("synthetic_fixture_session_count_mismatch")
    return sessions


def _suspension_positions(symbol: str, session_count: int) -> frozenset[int]:
    seed = int(symbol) % 97
    positions = {250 + seed, 1100 + seed}
    if symbol not in _ETF_SYMBOLS:
        positions.add(1750 + seed)
    return frozenset(position for position in positions if position < session_count - 1)


def _market_frame(symbol: str, sessions: tuple[date, ...]) -> pd.DataFrame:
    seed = int(symbol) % 1009
    gaps = _suspension_positions(symbol, len(sessions))
    rows: list[dict[str, object]] = []
    for index, session in enumerate(sessions):
        if index in gaps:
            continue
        trend = (index * (seed % 23 + 7)) % 2200
        cycle = ((index * index + seed * 17) % 401) - 200
        open_cents = 900 + seed % 1200 + trend + cycle
        close_cents = open_cents + ((index * 11 + seed) % 71) - 35
        high_cents = max(open_cents, close_cents) + 19
        low_cents = min(open_cents, close_cents) - 17
        volume = 100_000 + ((index * 7919 + seed * 313) % 900_000)
        amount_cents = ((open_cents + close_cents) * volume) // 2
        rows.append(
            {
                "date": pd.Timestamp(session),
                "open": open_cents / 100,
                "high": high_cents / 100,
                "low": low_cents / 100,
                "close": close_cents / 100,
                "volume": volume,
                "amount": amount_cents / 100,
            }
        )
    return pd.DataFrame(rows)


def _fixture_actions(
    symbol: str,
    kind: InstrumentKind,
    sessions: tuple[date, ...],
) -> tuple[CorporateActionEvent, ...]:
    if symbol not in {"000001", "510300", "600519", "601318"}:
        return ()
    requested_ex_date = {
        "000001": date(2020, 6, 15),
        "510300": date(2021, 7, 12),
        "600519": date(2022, 8, 22),
        "601318": date(2023, 9, 18),
    }[symbol]
    ex_index = next(
        index for index, session in enumerate(sessions) if session >= requested_ex_date
    )
    ex_date = sessions[ex_index]
    return (
        CorporateActionEvent.create(
            symbol=symbol,
            instrument_kind=kind,
            announcement_date=sessions[ex_index - 2],
            record_date=sessions[ex_index - 1],
            ex_date=ex_date,
            payable_date=sessions[ex_index + 2],
            cash_dividend_per_unit=Decimal("0.10"),
            stock_dividend_ratio=Decimal("0"),
            capitalization_ratio=Decimal("0"),
            rights_ratio=Decimal("0"),
            rights_price=None,
            source_schema=PUBLIC_FIXTURE_SCHEMA,
            source_url="https://synthetic-public-fixture.invalid/",
        ),
    )


def _write_universe(inputs_root: Path) -> str:
    members = tuple(UniverseMember(symbol, _kind(symbol).value) for symbol in _SYMBOLS)
    content = canonical_universe_bytes("public-synthetic-pilot-10", members)
    digest = hashlib.sha256(content).hexdigest()
    destination = inputs_root / "configs" / "universes" / f"{digest}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return digest


def _remove_lock_files(inputs_root: Path) -> None:
    for path in sorted(inputs_root.rglob("*.lock")):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("synthetic_fixture_lock_is_unsafe")
        path.unlink()


def _input_root(release_root: Path) -> Path:
    if not isinstance(release_root, Path):
        raise TypeError("release_root must be a Path")
    inputs_root = release_root / "inputs"
    if inputs_root.exists():
        if not inputs_root.is_dir() or inputs_root.is_symlink() or any(inputs_root.iterdir()):
            raise ValueError("synthetic_fixture_inputs_must_be_empty")
    else:
        inputs_root.mkdir(parents=True)
    return inputs_root


def build_public_v01_inputs(release_root: Path) -> PublicV01Inputs:
    """Create the exact public synthetic v0.1 input closure in an empty release root."""
    inputs_root = _input_root(release_root)
    sessions = _weekday_sessions()
    calendar = CalendarSnapshotStore(inputs_root).write(
        sessions,
        source_provider=PUBLIC_FIXTURE_SCHEMA,
        source_function="deterministic_session_calendar_v1",
        source_version=PUBLIC_FIXTURE_VERSION,
        fetched_at_utc=_FIXTURE_TIMESTAMP,
    )
    universe_id = _write_universe(inputs_root)
    raw_store = RawSnapshotStore(inputs_root)
    market_records: list[ManifestRecord] = []
    action_ids: list[tuple[str, str]] = []
    for symbol in _SYMBOLS:
        kind = _kind(symbol)
        frame = _market_frame(symbol, sessions)
        artifact = raw_store.write(
            frame,
            symbol=symbol,
            source_slug=PUBLIC_FIXTURE_SCHEMA,
            snapshot_date=_SNAPSHOT_DATE,
        )
        market_records.append(
            ManifestRecord.create(
                schema_version="1.0",
                symbol=symbol,
                instrument_kind=kind.value,
                provider=PUBLIC_FIXTURE_SCHEMA,
                source_function="deterministic_ohlcv_v1",
                source_schema=PUBLIC_FIXTURE_SCHEMA,
                endpoint_host="synthetic-public-fixture.invalid",
                provider_symbol=f"fixture-{symbol}",
                fetched_at_utc=_FIXTURE_TIMESTAMP,
                requested_start=_START,
                requested_end=_END,
                actual_start=_START,
                actual_end=_END,
                row_count=len(frame),
                snapshot_relative_path=artifact.relative_path,
                file_sha256=artifact.sha256,
                adjustment="",
                factor_source=None,
                latest_market_date=_END,
                akshare_version=PUBLIC_FIXTURE_VERSION,
                raw_volume_unit="unit",
                volume_multiplier_to_canonical=1,
                full_history_download=True,
                local_date_slice=False,
                quality_issue_counts={
                    "duplicate_date": 0,
                    "empty_frame": 0,
                    "invalid_high": 0,
                    "invalid_low": 0,
                    "negative_amount": 0,
                    "negative_volume": 0,
                    "non_finite_numeric": 0,
                    "non_positive_price": 0,
                    "null": 0,
                    "out_of_order_date": 0,
                },
            )
        )
        action = publish_corporate_actions(
            inputs_root,
            _fixture_actions(symbol, kind, sessions),
            symbol=symbol,
            instrument_kind=kind,
            provider=PUBLIC_FIXTURE_SCHEMA,
            source_schema=PUBLIC_FIXTURE_SCHEMA,
            normalization_version="cash-only-v1",
            coverage_start=_START,
            coverage_end=_END,
        )
        action_ids.append((symbol, action.snapshot_id))
    ManifestWriter(inputs_root / "data" / "manifests" / "manifest.jsonl").append_batch(
        tuple(market_records)
    )
    _remove_lock_files(inputs_root)
    return PublicV01Inputs(
        symbols=_SYMBOLS,
        universe_id=universe_id,
        calendar_id=calendar.calendar_id,
        calendar_first_date=calendar.first_date,
        calendar_last_date=calendar.last_complete_date,
        calendar_row_count=calendar.row_count,
        calendar_source=calendar.source_provider,
        fixture_version=PUBLIC_FIXTURE_VERSION,
        market_snapshot_ids=tuple(
            sorted((record.symbol, record.snapshot_id) for record in market_records)
        ),
        corporate_action_snapshot_ids=tuple(sorted(action_ids)),
    )
