from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime

import pytest

from aquant.data.calendar_snapshot import (
    CalendarError,
    CalendarSnapshotStore,
    VerifiedTradingCalendar,
    load_verified_calendar,
)


def _write_calendar(tmp_path, dates=(date(2026, 7, 22), date(2026, 7, 23))):
    return CalendarSnapshotStore(tmp_path).write(
        dates,
        source_provider="sina",
        source_function="tool_trade_date_hist_sina",
        source_version="1.18.64",
        fetched_at_utc=datetime(2026, 7, 24, tzinfo=UTC),
    )


def test_calendar_id_is_canonical_content_sha256(tmp_path):
    store = CalendarSnapshotStore(tmp_path)
    first = _write_calendar(tmp_path)
    second = store.write(
        (date(2026, 7, 22), date(2026, 7, 23)),
        source_provider="sina",
        source_function="tool_trade_date_hist_sina",
        source_version="1.18.65",
        fetched_at_utc=datetime(2026, 7, 25, tzinfo=UTC),
    )

    assert first.calendar_id == second.calendar_id
    assert first.relative_path == second.relative_path
    assert len(list((tmp_path / "data/calendars").glob("*.json"))) == 1
    assert len(store.read_manifest()) == 1


@pytest.mark.parametrize(
    "dates",
    [
        (date(2026, 7, 23), date(2026, 7, 22)),
        (date(2026, 7, 22), date(2026, 7, 22)),
        (date(2026, 7, 25),),
        (datetime(2026, 7, 22, tzinfo=UTC),),
    ],
)
def test_calendar_rejects_unsorted_duplicate_weekend_or_non_date_values(tmp_path, dates):
    with pytest.raises(CalendarError, match="calendar dates are invalid"):
        _write_calendar(tmp_path, dates)


def test_calendar_roundtrip_supports_next_session_and_coverage(tmp_path):
    record = _write_calendar(tmp_path)
    calendar = load_verified_calendar(tmp_path, record)

    assert calendar.dates == (date(2026, 7, 22), date(2026, 7, 23))
    assert calendar.next_session(date(2026, 7, 21)) == date(2026, 7, 22)
    assert calendar.next_session(date(2026, 7, 22)) == date(2026, 7, 23)
    assert calendar.next_session(date(2026, 7, 23)) is None
    assert calendar.contains(date(2026, 7, 22)) is True


def test_calendar_loader_rejects_content_tampering(tmp_path):
    record = _write_calendar(tmp_path)
    path = tmp_path / record.relative_path
    path.chmod(0o644)
    path.write_text('{"dates":[],"schema_version":"1.0"}\n', encoding="utf-8")

    with pytest.raises(CalendarError, match="content verification"):
        load_verified_calendar(tmp_path, record)


def test_calendar_loader_rejects_forged_record_metadata(tmp_path):
    record = _write_calendar(tmp_path)
    object.__setattr__(record, "row_count", 99)

    with pytest.raises(CalendarError, match="record verification"):
        load_verified_calendar(tmp_path, record)


def test_verified_calendar_cannot_be_constructed_by_callers():
    with pytest.raises(TypeError):
        VerifiedTradingCalendar((), "a" * 64, "a" * 64)


def test_calendar_rejects_existing_symlink_target(tmp_path):
    record = _write_calendar(tmp_path)
    target = tmp_path / record.relative_path
    external = tmp_path / "external.json"
    external.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(external)

    with pytest.raises(CalendarError, match="symlink"):
        _write_calendar(tmp_path)


def test_calendar_loader_rejects_hardlink_alias(tmp_path):
    record = _write_calendar(tmp_path)
    target = tmp_path / record.relative_path
    os.link(target, tmp_path / "calendar-alias.json")

    with pytest.raises(CalendarError, match="hardlink"):
        load_verified_calendar(tmp_path, record)


def test_calendar_manifest_rejects_duplicate_json_keys(tmp_path):
    record = _write_calendar(tmp_path)
    manifest = tmp_path / "data/calendars/manifest.jsonl"
    payload = record.to_dict()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    manifest.write_text(
        encoded[:-1] + ',"calendar_id":"' + record.calendar_id + '"}\n',
        encoding="utf-8",
    )

    with pytest.raises(CalendarError, match="duplicate JSON key"):
        CalendarSnapshotStore(tmp_path).read_manifest()
