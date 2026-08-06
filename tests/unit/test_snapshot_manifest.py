from __future__ import annotations

import io
import json
import logging
import multiprocessing
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import aquant.data.manifest as manifest_module
import aquant.data.snapshot as snapshot_module
from aquant.data.manifest import (
    ManifestError,
    ManifestRecord,
    ManifestWriter,
)
from aquant.data.snapshot import RawSnapshotStore, SnapshotError
from aquant.logging import JsonEventFormatter, log_event


def raw_frame(*, close: float = 10.2) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": ["2026-07-20", "2026-07-21"],
            "开盘": [10.0, 10.1],
            "最高": [10.3, 10.4],
            "最低": [9.9, 10.0],
            "收盘": [10.1, close],
            "成交量": [100, 120],
            "成交额": [1000.0, 1224.0],
        }
    )


def valid_record(artifact, **changes) -> ManifestRecord:
    values = {
        "schema_version": "1.0",
        "symbol": "600519",
        "instrument_kind": "stock",
        "provider": "sina",
        "source_function": "akshare.stock_zh_a_daily",
        "source_schema": "akshare.stock_zh_a_daily",
        "endpoint_host": "money.finance.sina.com.cn",
        "provider_symbol": "sh600519",
        "fetched_at_utc": datetime(2026, 7, 22, 1, 2, 3, tzinfo=UTC),
        "requested_start": date(2026, 7, 20),
        "requested_end": date(2026, 7, 21),
        "actual_start": date(2026, 7, 20),
        "actual_end": date(2026, 7, 21),
        "row_count": artifact.row_count,
        "snapshot_relative_path": artifact.relative_path,
        "file_sha256": artifact.sha256,
        "adjustment": "",
        "factor_source": None,
        "latest_market_date": date(2026, 7, 21),
        "akshare_version": "1.18.64",
        "raw_volume_unit": "lots",
        "volume_multiplier_to_canonical": 100,
        "full_history_download": False,
        "local_date_slice": False,
        "quality_issue_counts": {"null": 0, "duplicate_date": 0},
    }
    values.update(changes)
    return ManifestRecord.create(**values)


def _append_batch_process(path: str, record_values: tuple[dict, ...], start_event) -> None:
    records = tuple(ManifestRecord.from_dict(values) for values in record_values)
    start_event.wait()
    ManifestWriter(path).append_batch(records)


def test_snapshot_is_deterministic_idempotent_and_roundtrips_raw_frame(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    frame = raw_frame()

    first = store.write(
        frame, symbol="600519", source_slug="akshare-stock-sina", snapshot_date=date(2026, 7, 22)
    )
    second = store.write(
        frame.copy(),
        symbol="600519",
        source_slug="akshare-stock-sina",
        snapshot_date=date(2026, 7, 22),
    )

    assert first.sha256 == second.sha256
    assert first.relative_path == second.relative_path
    assert first.reused is False
    assert second.reused is True
    assert len(list((tmp_path / "data/raw").rglob("*.parquet"))) == 1
    loaded = pd.read_parquet(tmp_path / first.relative_path)
    pd.testing.assert_frame_equal(loaded, frame)


def test_snapshot_changed_content_is_preserved_as_a_new_file(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    first = store.write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    second = store.write(
        raw_frame(close=10.25),
        symbol="600519",
        source_slug="sina",
        snapshot_date=date(2026, 7, 22),
    )

    assert first.relative_path != second.relative_path
    assert (tmp_path / first.relative_path).exists()
    assert (tmp_path / second.relative_path).exists()


def test_snapshot_rejects_custom_string_index(tmp_path: Path) -> None:
    frame = raw_frame()
    frame.index = pd.Index(["first", "second"])

    with pytest.raises(SnapshotError, match="index"):
        RawSnapshotStore(tmp_path).write(
            frame, symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )


def test_snapshot_rejects_nonzero_range_index(tmp_path: Path) -> None:
    frame = raw_frame()
    frame.index = pd.RangeIndex(start=1, stop=3)

    with pytest.raises(SnapshotError, match="index"):
        RawSnapshotStore(tmp_path).write(
            frame, symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )


def test_snapshot_rejects_named_default_range_index(tmp_path: Path) -> None:
    frame = raw_frame()
    frame.index.name = "upstream_row"

    with pytest.raises(SnapshotError, match="index"):
        RawSnapshotStore(tmp_path).write(
            frame, symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )


@pytest.mark.parametrize(
    ("symbol", "source", "snapshot_date"),
    [
        ("../519", "sina", date(2026, 7, 22)),
        ("600519", "../sina", date(2026, 7, 22)),
        ("600519", "sina/query", date(2026, 7, 22)),
        ("600519", "sina", "../../2026-07-22"),
        ("600519", "sina", datetime(2026, 7, 22, tzinfo=UTC)),
    ],
)
def test_snapshot_rejects_unsafe_path_components(tmp_path, symbol, source, snapshot_date) -> None:
    with pytest.raises(SnapshotError):
        RawSnapshotStore(tmp_path).write(
            raw_frame(), symbol=symbol, source_slug=source, snapshot_date=snapshot_date
        )


def test_snapshot_rejects_source_slug_str_subclass(tmp_path: Path) -> None:
    class FormattableSource(str):
        def __format__(self, format_spec: str) -> str:
            return "../../escaped"

    with pytest.raises(SnapshotError, match="source"):
        RawSnapshotStore(tmp_path).write(
            raw_frame(),
            symbol="600519",
            source_slug=FormattableSource("sina"),
            snapshot_date=date(2026, 7, 22),
        )

    assert not (tmp_path / "data/raw").exists()


def test_snapshot_rejects_empty_frame(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError, match="empty"):
        RawSnapshotStore(tmp_path).write(
            pd.DataFrame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )


def test_snapshot_fails_closed_if_existing_target_is_corrupt(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    artifact = store.write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    target = tmp_path / artifact.relative_path
    target.chmod(0o644)
    target.write_bytes(b"corrupt")

    with pytest.raises(SnapshotError, match="hash"):
        store.write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )


def test_snapshot_rejects_symlinked_storage_parent(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    date_parent = tmp_path / "data/raw"
    date_parent.mkdir(parents=True)
    (date_parent / "2026-07-22").symlink_to(external, target_is_directory=True)

    with pytest.raises(SnapshotError, match="symlink"):
        RawSnapshotStore(tmp_path).write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )
    assert list(external.iterdir()) == []


def test_snapshot_rejects_existing_symlink_target(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    artifact = store.write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    target = tmp_path / artifact.relative_path
    external = tmp_path / "external.parquet"
    external.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(external)

    with pytest.raises(SnapshotError, match="symlink"):
        store.write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )


def test_snapshot_rejects_existing_hardlink_alias_before_reuse(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    artifact = store.write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    target = tmp_path / artifact.relative_path
    alias = tmp_path / "snapshot-alias.parquet"
    os.link(target, alias)

    with pytest.raises(SnapshotError, match="hardlink"):
        store.write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )


def test_new_snapshot_has_no_write_permission_bits(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )

    assert (tmp_path / artifact.relative_path).stat().st_mode & 0o222 == 0


def test_snapshot_verify_checks_hash_and_hardlink_before_consumption(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    artifact = store.write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )

    store.verify(artifact.relative_path, expected_hash=artifact.sha256)
    os.link(tmp_path / artifact.relative_path, tmp_path / "consumer-alias.parquet")

    with pytest.raises(SnapshotError, match="hardlink"):
        store.verify(artifact.relative_path, expected_hash=artifact.sha256)


def test_snapshot_wraps_write_oserror_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_write(descriptor: int, content: bytes) -> int:
        raise OSError("simulated snapshot disk failure")

    monkeypatch.setattr(snapshot_module.os, "write", fail_write)

    with pytest.raises(SnapshotError, match="write"):
        RawSnapshotStore(tmp_path).write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )

    raw_root = tmp_path / "data/raw"
    assert not list(raw_root.rglob("*.parquet"))
    assert not list(raw_root.rglob(".snapshot-*"))


def test_snapshot_retry_recovers_reserved_temporary_hardlink_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSnapshotStore(tmp_path)
    real_unlink = os.unlink

    def fail_reserved_temporary_unlink(path, *args, **kwargs):
        if str(path).startswith(".snapshot-"):
            raise OSError("simulated temporary alias cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(snapshot_module.os, "unlink", fail_reserved_temporary_unlink)
    with pytest.raises(SnapshotError, match="cleanup"):
        store.write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )

    target = next((tmp_path / "data/raw").rglob("*.parquet"))
    assert target.stat().st_nlink == 2
    assert len(list(target.parent.glob(".snapshot-*.tmp"))) == 1

    monkeypatch.setattr(snapshot_module.os, "unlink", real_unlink)
    artifact = store.write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )

    assert artifact.reused is True
    assert target.stat().st_nlink == 1
    assert not list(target.parent.glob(".snapshot-*.tmp"))


def test_snapshot_recovery_removes_only_same_inode_reserved_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSnapshotStore(tmp_path)
    real_unlink = os.unlink

    def fail_reserved_temporary_unlink(path, *args, **kwargs):
        if str(path).startswith(".snapshot-"):
            raise OSError("simulated temporary alias cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(snapshot_module.os, "unlink", fail_reserved_temporary_unlink)
    with pytest.raises(SnapshotError, match="cleanup"):
        store.write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )

    target = next((tmp_path / "data/raw").rglob("*.parquet"))
    controlled_alias = next(target.parent.glob(".snapshot-*.tmp"))
    external_alias = target.parent / "external-hardlink.parquet"
    os.link(target, external_alias)
    unrelated_reserved = target.parent / f".snapshot-{'f' * 32}.tmp"
    unrelated_reserved.write_bytes(b"unrelated")

    monkeypatch.setattr(snapshot_module.os, "unlink", real_unlink)
    with pytest.raises(SnapshotError) as error:
        store.write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )

    assert error.value.code == "existing_hardlink_alias"
    assert not controlled_alias.exists()
    assert external_alias.exists()
    assert unrelated_reserved.read_bytes() == b"unrelated"
    assert target.stat().st_nlink == 2


def test_snapshot_recovery_fsync_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSnapshotStore(tmp_path)
    real_unlink = os.unlink
    real_fsync = os.fsync

    def fail_reserved_temporary_unlink(path, *args, **kwargs):
        if str(path).startswith(".snapshot-"):
            raise OSError("simulated temporary alias cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(snapshot_module.os, "unlink", fail_reserved_temporary_unlink)
    with pytest.raises(SnapshotError, match="cleanup"):
        store.write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )

    monkeypatch.setattr(snapshot_module.os, "unlink", real_unlink)

    def fail_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated recovery directory fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(snapshot_module.os, "fsync", fail_directory_fsync)
    with pytest.raises(SnapshotError) as error:
        store.write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )

    assert error.value.code == "existing_hardlink_alias"

    monkeypatch.setattr(snapshot_module.os, "fsync", real_fsync)
    artifact = store.write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    target = tmp_path / artifact.relative_path
    assert artifact.reused is True
    assert target.stat().st_nlink == 1
    assert not list(target.parent.glob(".snapshot-*.tmp"))


def test_manifest_roundtrip_preserves_canonical_types(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)
    writer = ManifestWriter(tmp_path / "data/manifests/manifest.jsonl")

    result = writer.append(record)
    loaded = writer.read_all()

    assert result.status == "appended"
    assert loaded == (record,)
    line = (tmp_path / "data/manifests/manifest.jsonl").read_text().strip()
    assert json.loads(line)["fetched_at_utc"] == "2026-07-22T01:02:03Z"
    assert line == json.dumps(
        record.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fetched_at_utc", datetime(2026, 7, 22, 9, tzinfo=timezone(timedelta(hours=8)))),
        ("requested_end", date(2026, 7, 19)),
        ("actual_end", date(2026, 7, 19)),
        ("file_sha256", "not-a-hash"),
        ("snapshot_relative_path", Path("../escape.parquet")),
        ("snapshot_relative_path", ["not", "a", "path"]),
        ("endpoint_host", "https://example.com/a?token=secret"),
        ("row_count", 0),
        ("quality_issue_counts", {"null": -1}),
    ],
)
def test_manifest_rejects_invalid_fields(tmp_path: Path, field: str, value) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    with pytest.raises(ManifestError):
        valid_record(artifact, **{field: value})


def test_manifest_rejects_wrong_snapshot_id(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)
    with pytest.raises(ManifestError, match="snapshot_id"):
        replace(record, snapshot_id="0" * 64)


def test_manifest_snapshot_filename_requires_nonempty_valid_source_slug(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    empty_source_path = artifact.relative_path.with_name(f"-{artifact.sha256}.parquet")

    with pytest.raises(ManifestError, match="source"):
        valid_record(artifact, snapshot_relative_path=empty_source_path)


def test_manifest_snapshot_path_requires_hyphenated_iso_date(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    compact_date_path = Path("data/raw/20260722") / Path(*artifact.relative_path.parts[3:])

    with pytest.raises(ManifestError, match="date"):
        valid_record(artifact, snapshot_relative_path=compact_date_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_start", 20260720),
        ("requested_start", "2026-7-20"),
        ("snapshot_relative_path", 123),
        ("fetched_at_utc", "2026-07-22T01:02:03+00:00"),
    ],
)
def test_manifest_from_dict_rejects_noncanonical_json_scalars(
    tmp_path: Path, field: str, value
) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    values = valid_record(artifact).to_dict()
    values[field] = value

    with pytest.raises(ManifestError) as error:
        ManifestRecord.from_dict(values)

    assert error.value.code == "invalid_record"


def test_manifest_duplicate_is_idempotent_and_conflict_fails(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)
    writer = ManifestWriter(tmp_path / "data/manifests/manifest.jsonl")

    assert writer.append(record).status == "appended"
    assert writer.append(record).status == "duplicate"
    assert len(writer.read_all()) == 1

    conflicting = replace(record, endpoint_host="quotes.sina.com.cn")
    with pytest.raises(ManifestError, match="conflict"):
        writer.append(conflicting)


@pytest.mark.parametrize("operation", ["read", "append"])
def test_manifest_rejects_existing_hardlink_alias(tmp_path: Path, operation: str) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)
    writer = ManifestWriter(tmp_path / "data/manifests/manifest.jsonl")
    writer.append(record)
    os.link(writer.path, writer.path.with_name("manifest-alias.jsonl"))

    with pytest.raises(ManifestError) as error:
        if operation == "read":
            writer.read_all()
        else:
            writer.append(record)

    assert error.value.code == "hardlink_alias"


def test_manifest_append_batch_publishes_all_records_with_one_operation(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    first = valid_record(
        store.write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )
    )
    second = valid_record(
        store.write(
            raw_frame(close=10.25),
            symbol="600519",
            source_slug="sina",
            snapshot_date=date(2026, 7, 22),
        )
    )
    writer = ManifestWriter(tmp_path / "data/manifests/manifest.jsonl")

    results = writer.append_batch((first, second))

    assert tuple(result.status for result in results) == ("appended", "appended")
    assert writer.read_all() == (first, second)


def test_manifest_batch_conflict_publishes_none_of_the_new_records(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    first = valid_record(
        store.write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )
    )
    new = valid_record(
        store.write(
            raw_frame(close=10.25),
            symbol="600519",
            source_slug="sina",
            snapshot_date=date(2026, 7, 22),
        )
    )
    conflicting = replace(first, endpoint_host="quotes.sina.com.cn")
    writer = ManifestWriter(tmp_path / "data/manifests/manifest.jsonl")
    writer.append(first)

    with pytest.raises(ManifestError, match="conflict"):
        writer.append_batch((new, conflicting))

    assert writer.read_all() == (first,)


def test_manifest_failed_batch_rewrite_keeps_old_complete_state_and_retry_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSnapshotStore(tmp_path)
    artifacts = [
        store.write(
            raw_frame(close=10.2 + index / 100),
            symbol="600519",
            source_slug="sina",
            snapshot_date=date(2026, 7, 22),
        )
        for index in range(3)
    ]
    first, second, third = (valid_record(artifact) for artifact in artifacts)
    path = tmp_path / "data/manifests/manifest.jsonl"
    writer = ManifestWriter(path)
    writer.append(first)
    original = path.read_bytes()
    real_write = os.write
    calls = 0

    def partial_then_fail(descriptor: int, content: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(descriptor, content[:23])
        raise OSError("simulated batch rewrite failure")

    monkeypatch.setattr(manifest_module.os, "write", partial_then_fail)
    with pytest.raises(ManifestError, match="write"):
        writer.append_batch((second, third))

    assert path.read_bytes() == original
    assert ManifestWriter(path).read_all() == (first,)

    monkeypatch.setattr(manifest_module.os, "write", real_write)
    results = writer.append_batch((second, third))
    assert tuple(result.status for result in results) == ("appended", "appended")
    assert ManifestWriter(path).read_all() == (first, second, third)


def test_manifest_batch_replace_failure_keeps_old_complete_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSnapshotStore(tmp_path)
    first = valid_record(
        store.write(
            raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
        )
    )
    second = valid_record(
        store.write(
            raw_frame(close=10.25),
            symbol="600519",
            source_slug="sina",
            snapshot_date=date(2026, 7, 22),
        )
    )
    path = tmp_path / "data/manifests/manifest.jsonl"
    writer = ManifestWriter(path)
    writer.append(first)

    def fail_replace(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(manifest_module.os, "replace", fail_replace)
    with pytest.raises(ManifestError, match="atomic replace"):
        writer.append_batch((second,))

    assert ManifestWriter(path).read_all() == (first,)


def test_manifest_parent_fsync_failure_never_exposes_partial_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSnapshotStore(tmp_path)
    records = tuple(
        valid_record(
            store.write(
                raw_frame(close=10.2 + index / 100),
                symbol="600519",
                source_slug="sina",
                snapshot_date=date(2026, 7, 22),
            )
        )
        for index in range(3)
    )
    first, second, third = records
    path = tmp_path / "data/manifests/manifest.jsonl"
    writer = ManifestWriter(path)
    writer.append(first)
    real_fsync = os.fsync
    calls = 0

    def fail_parent_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated parent fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(manifest_module.os, "fsync", fail_parent_fsync)
    with pytest.raises(ManifestError, match="parent fsync"):
        writer.append_batch((second, third))

    loaded = ManifestWriter(path).read_all()
    assert loaded == (first,) or loaded == (first, second, third)


def test_manifest_batch_lock_serializes_two_processes(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    records = tuple(
        valid_record(
            store.write(
                raw_frame(close=10.2 + index / 100),
                symbol="600519",
                source_slug="sina",
                snapshot_date=date(2026, 7, 22),
            )
        )
        for index in range(4)
    )
    path = tmp_path / "data/manifests/manifest.jsonl"
    context = multiprocessing.get_context("fork")
    start_event = context.Event()
    batches = (records[:2], records[2:])
    processes = [
        context.Process(
            target=_append_batch_process,
            args=(
                str(path),
                tuple(record.to_dict() for record in batch),
                start_event,
            ),
        )
        for batch in batches
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    loaded = ManifestWriter(path).read_all()
    assert len(loaded) == 4
    assert {record.snapshot_id for record in loaded} == {record.snapshot_id for record in records}


def test_manifest_refetch_time_only_is_duplicate_and_preserves_first_time(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    first = valid_record(artifact)
    refetch = replace(first, fetched_at_utc=datetime(2026, 7, 22, 2, tzinfo=UTC))
    writer = ManifestWriter(tmp_path / "data/manifests/manifest.jsonl")

    assert writer.append(first).status == "appended"
    assert writer.append(refetch).status == "duplicate"

    loaded = writer.read_all()
    assert loaded == (first,)
    assert len(writer.path.read_text().splitlines()) == 1


def test_manifest_reader_deduplicates_refetch_time_only_records(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    first = valid_record(artifact)
    refetch = replace(first, fetched_at_utc=datetime(2026, 7, 22, 2, tzinfo=UTC))
    path = tmp_path / "data/manifests/manifest.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(item.to_dict()) for item in (first, refetch)) + "\n")

    assert ManifestWriter(path).read_all() == (first,)


def test_manifest_bad_json_fails_closed_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "data/manifests/manifest.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"broken":\n')
    writer = ManifestWriter(path)

    with pytest.raises(ManifestError, match="JSON"):
        writer.read_all()
    assert path.read_text() == '{"broken":\n'


def test_manifest_missing_field_fails_closed(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    values = valid_record(artifact).to_dict()
    del values["provider"]
    path = tmp_path / "data/manifests/manifest.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(values) + "\n")

    with pytest.raises(ManifestError, match="fields"):
        ManifestWriter(path).read_all()


def test_manifest_duplicate_conflict_on_disk_fails_closed(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)
    conflict = replace(record, endpoint_host="quotes.sina.com.cn")
    path = tmp_path / "data/manifests/manifest.jsonl"
    path.parent.mkdir(parents=True)
    lines = [json.dumps(record.to_dict()), json.dumps(conflict.to_dict())]
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ManifestError, match="duplicate"):
        ManifestWriter(path).read_all()


def test_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)
    canonical = json.dumps(record.to_dict(), separators=(",", ":"))
    duplicate = canonical.replace('"provider":"sina"', '"provider":"sina","provider":"shadow"', 1)
    path = tmp_path / "data/manifests/manifest.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(duplicate + "\n")

    with pytest.raises(ManifestError, match="duplicate JSON key"):
        ManifestWriter(path).read_all()


def test_manifest_file_symlink_never_reads_or_appends_external_target(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)
    external = tmp_path / "external-manifest.txt"
    external.write_text("external sentinel\n")
    path = tmp_path / "data/manifests/manifest.jsonl"
    path.parent.mkdir(parents=True)
    path.symlink_to(external)
    writer = ManifestWriter(path)

    with pytest.raises(ManifestError, match="symlink"):
        writer.read_all()
    with pytest.raises(ManifestError, match="symlink"):
        writer.append(record)
    assert external.read_text() == "external sentinel\n"


def test_manifest_lock_symlink_never_reads_or_appends_external_target(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)
    external = tmp_path / "external-lock.txt"
    external.write_text("external lock sentinel\n")
    path = tmp_path / "data/manifests/manifest.jsonl"
    path.parent.mkdir(parents=True)
    path.with_suffix(".jsonl.lock").symlink_to(external)
    writer = ManifestWriter(path)

    with pytest.raises(ManifestError, match="symlink"):
        writer.read_all()
    with pytest.raises(ManifestError, match="symlink"):
        writer.append(record)
    assert external.read_text() == "external lock sentinel\n"


def test_manifest_rejects_symlinked_parent_below_configured_root(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path / "artifact-root").write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)
    config_root = tmp_path / "config-root"
    config_root.mkdir()
    external = tmp_path / "external-parent"
    external.mkdir()
    (config_root / "data").symlink_to(external, target_is_directory=True)
    writer = ManifestWriter(config_root / "data/manifests/manifest.jsonl")

    with pytest.raises(ManifestError, match="symlink"):
        writer.read_all()
    with pytest.raises(ManifestError, match="symlink"):
        writer.append(record)
    assert list(external.iterdir()) == []


def test_manifest_wraps_fsync_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)

    def fail_fsync(descriptor: int) -> None:
        raise OSError("simulated manifest fsync failure")

    monkeypatch.setattr(manifest_module.os, "fsync", fail_fsync)

    with pytest.raises(ManifestError, match="fsync"):
        ManifestWriter(tmp_path / "data/manifests/manifest.jsonl").append(record)


def test_manifest_missing_terminal_newline_fails_closed(tmp_path: Path) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)
    path = tmp_path / "data/manifests/manifest.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record.to_dict()))

    with pytest.raises(ManifestError, match="newline"):
        ManifestWriter(path).append(record)


def test_manifest_concurrent_writers_produce_complete_unique_lines(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    path = tmp_path / "data/manifests/manifest.jsonl"
    records = []
    for index in range(12):
        artifact = store.write(
            raw_frame(close=10.2 + index / 1000),
            symbol="600519",
            source_slug="sina",
            snapshot_date=date(2026, 7, 22),
        )
        records.append(valid_record(artifact))

    calls = records + records
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda item: ManifestWriter(path).append(item), calls))

    loaded = ManifestWriter(path).read_all()
    assert len(loaded) == len(records)
    assert len({item.snapshot_id for item in loaded}) == len(records)
    for line in path.read_text().splitlines():
        assert json.loads(line)


def test_manifest_retries_short_os_writes_until_line_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = RawSnapshotStore(tmp_path).write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    record = valid_record(artifact)
    path = tmp_path / "data/manifests/manifest.jsonl"
    real_write = os.write

    def short_write(descriptor: int, content: bytes) -> int:
        return real_write(descriptor, content[: max(1, len(content) // 3)])

    monkeypatch.setattr(manifest_module.os, "write", short_write)
    ManifestWriter(path).append(record)

    assert ManifestWriter(path).read_all() == (record,)
    assert len(path.read_text().splitlines()) == 1


def test_manifest_failed_rewrite_never_leaves_partial_official_line_and_retry_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSnapshotStore(tmp_path)
    first_artifact = store.write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    second_artifact = store.write(
        raw_frame(close=10.25),
        symbol="600519",
        source_slug="sina",
        snapshot_date=date(2026, 7, 22),
    )
    first = valid_record(first_artifact)
    second = valid_record(second_artifact)
    path = tmp_path / "data/manifests/manifest.jsonl"
    writer = ManifestWriter(path)
    writer.append(first)
    original = path.read_bytes()
    real_write = os.write
    calls = 0

    def partial_then_fail(descriptor: int, content: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(descriptor, content[:17])
        raise OSError("simulated manifest rewrite failure")

    monkeypatch.setattr(manifest_module.os, "write", partial_then_fail)
    with pytest.raises(ManifestError, match="write"):
        writer.append(second)

    assert path.read_bytes() == original
    assert ManifestWriter(path).read_all() == (first,)
    assert not list(path.parent.glob(".manifest-*.tmp"))

    monkeypatch.setattr(manifest_module.os, "write", real_write)
    assert writer.append(second).status == "appended"
    assert ManifestWriter(path).read_all() == (first, second)


def test_manifest_parent_fsync_failure_leaves_complete_file_and_retry_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSnapshotStore(tmp_path)
    first_artifact = store.write(
        raw_frame(), symbol="600519", source_slug="sina", snapshot_date=date(2026, 7, 22)
    )
    second_artifact = store.write(
        raw_frame(close=10.25),
        symbol="600519",
        source_slug="sina",
        snapshot_date=date(2026, 7, 22),
    )
    first = valid_record(first_artifact)
    second = valid_record(second_artifact)
    path = tmp_path / "data/manifests/manifest.jsonl"
    writer = ManifestWriter(path)
    writer.append(first)
    real_fsync = os.fsync
    calls = 0

    def fail_parent_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated parent fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(manifest_module.os, "fsync", fail_parent_fsync)
    with pytest.raises(ManifestError, match="parent fsync"):
        writer.append(second)

    loaded_after_error = ManifestWriter(path).read_all()
    assert loaded_after_error == (first,) or loaded_after_error == (first, second)
    assert not list(path.parent.glob(".manifest-*.tmp"))

    monkeypatch.setattr(manifest_module.os, "fsync", real_fsync)
    assert writer.append(second).status in {"appended", "duplicate"}
    assert ManifestWriter(path).read_all() == (first, second)


def test_json_event_log_is_parseable_and_redacts_sensitive_values() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonEventFormatter())
    logger = logging.getLogger("aquant-test-json-event")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    log_event(
        logger,
        logging.ERROR,
        "fetch_failed",
        url="https://user:password@example.com:8443/prices?token=top-secret",
        error=RuntimeError("private exception body"),
        symbol="600519",
    )

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "fetch_failed"
    assert payload["symbol"] == "600519"
    assert payload["url"] == "https://example.com:8443/prices"
    assert payload["error"] == {"exception_type": "RuntimeError"}
    assert "top-secret" not in stream.getvalue()
    assert "password" not in stream.getvalue()
    assert "private exception body" not in stream.getvalue()


def test_json_event_log_redacts_relative_and_scheme_less_url_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonEventFormatter())
    logger = logging.getLogger("aquant-test-url-field-semantics")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    log_event(
        logger,
        logging.INFO,
        "url_shapes",
        callback_url="/prices?token=relative-secret#fragment",
        nested={
            "source_url": "user:password@example.com/prices?token=host-secret#fragment",
            "note": "https://user:password@example.com/keep?ordinary=text#fragment",
        },
    )

    payload = json.loads(stream.getvalue())
    assert payload["callback_url"] == "/prices"
    assert payload["nested"]["source_url"] == "example.com/prices"
    assert payload["nested"]["note"] == "https://example.com/keep"
    assert "relative-secret" not in stream.getvalue()
    assert "host-secret" not in stream.getvalue()
    assert "password" not in stream.getvalue()


def test_json_event_log_redacts_nested_secrets_and_endpoint_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonEventFormatter())
    logger = logging.getLogger("aquant-test-secret-fields")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    log_event(
        logger,
        logging.INFO,
        "secret_fields",
        nested={
            "Authorization": "Bearer top-secret",
            "API_KEY": "api-secret",
            "provider_endpoint": "user:password@example.com/path?token=query-secret#fragment",
        },
    )

    payload = json.loads(stream.getvalue())
    assert payload["nested"]["Authorization"] == "[REDACTED]"
    assert payload["nested"]["API_KEY"] == "[REDACTED]"
    assert payload["nested"]["provider_endpoint"] == "example.com/path"
    for secret in ("top-secret", "api-secret", "password", "query-secret"):
        assert secret not in stream.getvalue()


def test_json_event_log_redacts_prefixed_and_punctuated_secret_keys() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonEventFormatter())
    logger = logging.getLogger("aquant-test-secret-key-variants")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    log_event(
        logger,
        logging.INFO,
        "secret_key_variants",
        nested={
            "deepseek_api_key": "deepseek-secret",
            "auth_token": "auth-secret",
            "deeper": {"client_secret": "client-secret", "X-API-Key": "header-secret"},
            "camel": {
                "accessToken": "access-secret",
                "clientSecret": "camel-client-secret",
                "refreshToken": "refresh-secret",
            },
            "token_count": 17,
        },
    )

    payload = json.loads(stream.getvalue())
    assert payload["nested"]["deepseek_api_key"] == "[REDACTED]"
    assert payload["nested"]["auth_token"] == "[REDACTED]"
    assert payload["nested"]["deeper"]["client_secret"] == "[REDACTED]"
    assert payload["nested"]["deeper"]["X-API-Key"] == "[REDACTED]"
    assert payload["nested"]["camel"]["accessToken"] == "[REDACTED]"
    assert payload["nested"]["camel"]["clientSecret"] == "[REDACTED]"
    assert payload["nested"]["camel"]["refreshToken"] == "[REDACTED]"
    assert payload["nested"]["token_count"] == 17
    for secret in (
        "deepseek-secret",
        "auth-secret",
        "client-secret",
        "header-secret",
        "access-secret",
        "camel-client-secret",
        "refresh-secret",
    ):
        assert secret not in stream.getvalue()


def test_json_event_log_rejects_dataframe_fields() -> None:
    logger = logging.getLogger("aquant-test-no-dataframe")
    with pytest.raises(TypeError, match="DataFrame"):
        log_event(logger, logging.INFO, "bad", frame=raw_frame())


def test_json_event_log_rejects_reserved_field_names() -> None:
    logger = logging.getLogger("aquant-test-reserved-fields")
    with pytest.raises(ValueError, match="reserved"):
        log_event(logger, logging.INFO, "fetch_started", **{"level": "overwritten"})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_json_event_log_rejects_non_finite_numbers(value: float) -> None:
    logger = logging.getLogger("aquant-test-finite-numbers")
    with pytest.raises(ValueError, match="finite"):
        log_event(logger, logging.INFO, "bad_number", value=value)
