from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd

from aquant.backtest.data_access import load_verified_snapshot
from aquant.data import SourceSchema
from aquant.data.corporate_actions import (
    load_verified_corporate_actions,
    read_corporate_action_manifest,
)
from aquant.data.manifest import ManifestWriter
from aquant.release_synthetic import (
    PUBLIC_FIXTURE_SCHEMA,
    PUBLIC_FIXTURE_VERSION,
    build_public_v01_inputs,
)


def _input_digests(root: Path) -> dict[str, str]:
    inputs = root / "inputs"
    return {
        path.relative_to(inputs).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(inputs.rglob("*"))
        if path.is_file()
    }


def test_explicit_public_fixture_schema_is_supported_at_execution_boundary(tmp_path):
    result = build_public_v01_inputs(tmp_path)
    manifest = tmp_path / "inputs" / "data" / "manifests" / "manifest.jsonl"
    records = ManifestWriter(manifest).read_all()

    assert PUBLIC_FIXTURE_SCHEMA == "synthetic_public_fixture"
    assert SourceSchema(PUBLIC_FIXTURE_SCHEMA) is SourceSchema.SYNTHETIC_PUBLIC_FIXTURE
    assert {record.provider for record in records} == {PUBLIC_FIXTURE_SCHEMA}
    assert {record.source_schema for record in records} == {PUBLIC_FIXTURE_SCHEMA}
    verified_symbols = {
        load_verified_snapshot(tmp_path / "inputs", record).provenance.symbol
        for record in records
    }
    assert verified_symbols == set(result.symbols)
    assert result.symbols == (
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


def test_public_fixture_inputs_are_deterministic_auditable_and_not_market_data(tmp_path):
    left = build_public_v01_inputs(tmp_path / "left")
    right = build_public_v01_inputs(tmp_path / "right")

    assert left == right
    assert _input_digests(tmp_path / "left") == _input_digests(tmp_path / "right")
    assert len(left.market_snapshot_ids) == 10
    assert len(left.corporate_action_snapshot_ids) == 10
    assert left.calendar_first_date == date(2018, 1, 2)
    assert left.calendar_last_date == date(2026, 7, 24)
    assert left.calendar_row_count == 2074
    assert left.calendar_source == PUBLIC_FIXTURE_SCHEMA
    assert left.fixture_version == PUBLIC_FIXTURE_VERSION

    records = ManifestWriter(
        tmp_path / "left" / "inputs" / "data" / "manifests" / "manifest.jsonl"
    ).read_all()
    assert {record.provider for record in records} == {PUBLIC_FIXTURE_SCHEMA}
    assert {record.source_schema for record in records} == {PUBLIC_FIXTURE_SCHEMA}
    assert {record.endpoint_host for record in records} == {"synthetic-public-fixture.invalid"}
    assert all(record.actual_start == date(2018, 1, 2) for record in records)
    assert all(record.actual_end == date(2026, 7, 24) for record in records)
    assert all(record.row_count < left.calendar_row_count for record in records)
    assert sum(left.calendar_row_count - record.row_count for record in records) == 28

    calendar_document = next(
        (tmp_path / "left" / "inputs" / "data" / "calendars").glob("*.json")
    )
    calendar_dates = {
        date.fromisoformat(value)
        for value in json.loads(calendar_document.read_text(encoding="utf-8"))["dates"]
    }
    action_records = read_corporate_action_manifest(tmp_path / "left" / "inputs")
    assert all(
        event.payable_date in calendar_dates
        for record in action_records
        for event in load_verified_corporate_actions(tmp_path / "left" / "inputs", record).events
    )

    first = records[0]
    frame = pd.read_parquet(tmp_path / "left" / "inputs" / first.snapshot_relative_path)
    assert tuple(frame.columns) == ("date", "open", "high", "low", "close", "volume", "amount")
    assert frame["date"].min() == pd.Timestamp("2018-01-02")
    assert frame["date"].max() == pd.Timestamp("2026-07-24")
    assert not (tmp_path / "left" / "inputs" / "data" / "calendars" / ".manifest.lock").exists()
    action_lock = (
        tmp_path
        / "left"
        / "inputs"
        / "data"
        / "corporate_actions"
        / "manifest.jsonl.lock"
    )
    assert not action_lock.exists()
