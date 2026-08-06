from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

import aquant.gate_e.frozen_manifest as frozen_manifest_module
from aquant.data.manifest import ManifestError, ManifestRecord, ManifestWriter
from aquant.data.snapshot import RawSnapshotStore
from aquant.gate_e.frozen_manifest import read_frozen_manifest


def _record(tmp_path: Path) -> ManifestRecord:
    artifact = RawSnapshotStore(tmp_path).write(
        pd.DataFrame(
            {
                "日期": ["2026-07-20", "2026-07-21"],
                "开盘": [10.0, 10.1],
                "最高": [10.3, 10.4],
                "最低": [9.9, 10.0],
                "收盘": [10.1, 10.2],
                "成交量": [100, 120],
                "成交额": [1000.0, 1224.0],
            }
        ),
        symbol="600519",
        source_slug="sina",
        snapshot_date=date(2026, 7, 22),
    )
    return ManifestRecord.create(
        schema_version="1.0",
        symbol="600519",
        instrument_kind="stock",
        provider="sina",
        source_function="akshare.stock_zh_a_daily",
        source_schema="akshare.stock_zh_a_daily",
        endpoint_host="money.finance.sina.com.cn",
        provider_symbol="sh600519",
        fetched_at_utc=datetime(2026, 7, 22, 1, 2, 3, tzinfo=UTC),
        requested_start=date(2026, 7, 20),
        requested_end=date(2026, 7, 21),
        actual_start=date(2026, 7, 20),
        actual_end=date(2026, 7, 21),
        row_count=artifact.row_count,
        snapshot_relative_path=artifact.relative_path,
        file_sha256=artifact.sha256,
        adjustment="",
        factor_source=None,
        latest_market_date=date(2026, 7, 21),
        akshare_version="1.18.64",
        raw_volume_unit="lots",
        volume_multiplier_to_canonical=100,
        full_history_download=False,
        local_date_slice=False,
        quality_issue_counts={"null": 0, "duplicate_date": 0},
    )


def test_frozen_manifest_read_preserves_lock_contract(tmp_path: Path) -> None:
    record = _record(tmp_path)
    writer = ManifestWriter(tmp_path / "data/manifests/manifest.jsonl")
    writer.append(record)
    writer.lock_path.unlink()
    expected_sha256 = hashlib.sha256(writer.path.read_bytes()).hexdigest()
    original_open = frozen_manifest_module.os.open
    manifest_open_flags: list[int] = []

    def observing_open(value, flags, *args, **kwargs):
        if value == writer.path.name:
            manifest_open_flags.append(flags)
        return original_open(value, flags, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(frozen_manifest_module.os, "open", observing_open)
        assert read_frozen_manifest(
            writer.path,
            expected_sha256=expected_sha256,
        ) == (record,)
        assert not writer.lock_path.exists()
        assert writer.read_all() == (record,)

    assert manifest_open_flags[0] & getattr(os, "O_NONBLOCK", 0)
    assert writer.lock_path.is_file()


@pytest.mark.parametrize(
    ("parent_exists", "error_code"),
    (
        (False, "parent_open_failed"),
        (True, "open_failed"),
    ),
)
def test_frozen_manifest_read_does_not_create_missing_paths(
    tmp_path: Path,
    parent_exists: bool,
    error_code: str,
) -> None:
    root = tmp_path / "missing"
    path = root / "data/manifests/manifest.jsonl"
    if parent_exists:
        path.parent.mkdir(parents=True)

    with pytest.raises(ManifestError) as captured:
        read_frozen_manifest(path, expected_sha256="0" * 64)

    assert captured.value.code == error_code
    assert not path.exists()
    assert not path.with_suffix(".jsonl.lock").exists()
    assert root.exists() is parent_exists


def test_frozen_manifest_read_hashes_consumed_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    writer = ManifestWriter(tmp_path / "data/manifests/manifest.jsonl")
    writer.append(_record(tmp_path))
    writer.lock_path.unlink()
    expected_sha256 = hashlib.sha256(writer.path.read_bytes()).hexdigest()
    original_read = frozen_manifest_module.os.read
    changed = False

    def changed_read(descriptor, size):
        nonlocal changed
        block = original_read(descriptor, size)
        if block and not changed:
            changed = True
            return block.replace(
                b"2026-07-22T01:02:03Z",
                b"2026-07-22T01:02:04Z",
                1,
            )
        return block

    monkeypatch.setattr(frozen_manifest_module.os, "read", changed_read)

    with pytest.raises(ManifestError) as captured:
        read_frozen_manifest(writer.path, expected_sha256=expected_sha256)

    assert captured.value.code == "manifest_hash_mismatch"
    assert changed is True
    assert not writer.lock_path.exists()


def test_frozen_manifest_rejects_invalid_hash_before_filesystem_access(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-not-be-created"

    with pytest.raises(ManifestError) as captured:
        read_frozen_manifest(
            root / "data/manifests/manifest.jsonl",
            expected_sha256="A" * 64,
        )

    assert captured.value.code == "invalid_expected_hash"
    assert not root.exists()


@pytest.mark.parametrize(
    ("alias_kind", "error_code"),
    (
        ("symlink", "open_failed"),
        ("hardlink", "hardlink_alias"),
    ),
)
def test_frozen_manifest_rejects_aliases(
    tmp_path: Path,
    alias_kind: str,
    error_code: str,
) -> None:
    writer = ManifestWriter(tmp_path / "data/manifests/manifest.jsonl")
    writer.append(_record(tmp_path))
    writer.lock_path.unlink()
    expected_sha256 = hashlib.sha256(writer.path.read_bytes()).hexdigest()
    alias = writer.path.with_name("manifest-alias.jsonl")
    if alias_kind == "symlink":
        writer.path.rename(alias)
        writer.path.symlink_to(alias.name)
    else:
        os.link(writer.path, alias)

    with pytest.raises(ManifestError) as captured:
        read_frozen_manifest(
            writer.path,
            expected_sha256=expected_sha256,
        )

    assert captured.value.code == error_code
    assert not writer.lock_path.exists()
