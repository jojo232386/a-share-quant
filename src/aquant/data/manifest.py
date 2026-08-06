"""Strict append-only provenance manifest for raw market-data snapshots."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from aquant.data.snapshot import is_valid_source_slug
from aquant.logging import log_event

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_TEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


class ManifestError(RuntimeError):
    """Raised when provenance is invalid, damaged, or conflicting."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _require_date(name: str, value) -> None:
    if type(value) is not date:
        raise ManifestError("invalid_field", f"{name} must be a date")


def _stable_id_key(values: Mapping[str, object]) -> dict[str, object]:
    return {
        key: values[key]
        for key in (
            "schema_version",
            "symbol",
            "instrument_kind",
            "provider",
            "source_function",
            "provider_symbol",
            "requested_start",
            "requested_end",
            "snapshot_relative_path",
            "file_sha256",
        )
    }


def _json_value(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _canonical_json(values: Mapping[str, object]) -> str:
    return json.dumps(
        _json_value(values), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _record_identity(record: ManifestRecord) -> str:
    values = record.to_dict()
    del values["fetched_at_utc"]
    return _canonical_json(values)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(
                "duplicate_json_key", f"manifest contains duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


@dataclass(frozen=True)
class ManifestRecord:
    schema_version: str
    snapshot_id: str
    symbol: str
    instrument_kind: str
    provider: str
    source_function: str
    source_schema: str
    endpoint_host: str
    provider_symbol: str
    fetched_at_utc: datetime
    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    row_count: int
    snapshot_relative_path: Path
    file_sha256: str
    adjustment: str
    factor_source: str | None
    latest_market_date: date
    akshare_version: str
    raw_volume_unit: str
    volume_multiplier_to_canonical: int
    full_history_download: bool
    local_date_slice: bool
    quality_issue_counts: Mapping[str, int]

    @classmethod
    def create(cls, **values) -> ManifestRecord:
        missing = set(cls.__dataclass_fields__) - {"snapshot_id"} - set(values)
        extra = set(values) - set(cls.__dataclass_fields__)
        if missing or extra:
            message = (
                f"manifest fields mismatch: missing={sorted(missing)!r}; extra={sorted(extra)!r}"
            )
            raise ManifestError("invalid_fields", message)
        stable = _canonical_json(_stable_id_key(values))
        values["snapshot_id"] = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        return cls(**values)

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, str) or not _VERSION_RE.fullmatch(
            self.schema_version
        ):
            raise ManifestError("invalid_field", "schema_version is invalid")
        if not isinstance(self.symbol, str) or re.fullmatch(r"[0-9]{6}", self.symbol) is None:
            raise ManifestError("invalid_field", "symbol must be exactly six digits")
        for name in (
            "instrument_kind",
            "provider",
            "source_function",
            "source_schema",
            "provider_symbol",
            "akshare_version",
            "raw_volume_unit",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or _TEXT_RE.fullmatch(value) is None:
                raise ManifestError("invalid_field", f"{name} is invalid")
        if (
            not isinstance(self.endpoint_host, str)
            or _HOST_RE.fullmatch(self.endpoint_host) is None
        ):
            raise ManifestError(
                "invalid_field", "endpoint_host must be a bare host without URL parameters"
            )
        if not isinstance(
            self.fetched_at_utc, datetime
        ) or self.fetched_at_utc.utcoffset() != UTC.utcoffset(None):
            raise ManifestError("invalid_field", "fetched_at_utc must be timezone-aware UTC")
        date_fields = (
            "requested_start",
            "requested_end",
            "actual_start",
            "actual_end",
            "latest_market_date",
        )
        for name in date_fields:
            _require_date(name, getattr(self, name))
        if self.requested_start > self.requested_end or self.actual_start > self.actual_end:
            raise ManifestError("invalid_range", "manifest date range is reversed")
        if self.actual_start < self.requested_start or self.actual_end > self.requested_end:
            raise ManifestError("invalid_range", "actual date range must be within requested range")
        if self.latest_market_date != self.actual_end:
            raise ManifestError("invalid_range", "latest_market_date must equal actual_end")
        if (
            not isinstance(self.row_count, int)
            or isinstance(self.row_count, bool)
            or self.row_count <= 0
        ):
            raise ManifestError("invalid_field", "row_count must be a positive integer")
        if not isinstance(self.file_sha256, str) or _HASH_RE.fullmatch(self.file_sha256) is None:
            raise ManifestError("invalid_field", "file_sha256 must be lowercase SHA-256")
        if not isinstance(self.snapshot_relative_path, (str, os.PathLike)):
            raise ManifestError("invalid_field", "snapshot_relative_path must be path-like")
        try:
            path = PurePosixPath(Path(self.snapshot_relative_path).as_posix())
        except (TypeError, ValueError, OSError) as exc:
            raise ManifestError(
                "invalid_field", "snapshot_relative_path must be path-like"
            ) from exc
        if path.is_absolute() or ".." in path.parts or len(path.parts) != 5:
            raise ManifestError("invalid_field", "snapshot_relative_path is unsafe")
        if path.parts[:2] != ("data", "raw") or path.suffix != ".parquet":
            raise ManifestError(
                "invalid_field", "snapshot_relative_path must point beneath data/raw"
            )
        hash_suffix = f"-{self.file_sha256}.parquet"
        if path.parts[3] != self.symbol or not path.name.endswith(hash_suffix):
            raise ManifestError(
                "invalid_field", "snapshot path does not match symbol and file hash"
            )
        source_slug = path.name[: -len(hash_suffix)]
        if not is_valid_source_slug(source_slug):
            raise ManifestError("invalid_field", "snapshot path source slug is missing or invalid")
        path_date = path.parts[2]
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", path_date) is None:
            raise ManifestError("invalid_field", "snapshot path date is invalid")
        try:
            date.fromisoformat(path_date)
        except ValueError as exc:
            raise ManifestError("invalid_field", "snapshot path date is invalid") from exc
        object.__setattr__(self, "snapshot_relative_path", Path(path.as_posix()))
        if self.adjustment not in {"", "qfq", "hfq"}:
            raise ManifestError("invalid_field", "adjustment is unsupported")
        if self.factor_source is not None and (
            not isinstance(self.factor_source, str) or not self.factor_source.strip()
        ):
            raise ManifestError("invalid_field", "factor_source is invalid")
        if self.adjustment == "" and self.factor_source is not None:
            raise ManifestError("invalid_field", "unadjusted data must not name a factor source")
        if (
            not isinstance(self.volume_multiplier_to_canonical, int)
            or isinstance(self.volume_multiplier_to_canonical, bool)
            or self.volume_multiplier_to_canonical <= 0
        ):
            raise ManifestError("invalid_field", "volume multiplier must be a positive integer")
        if type(self.full_history_download) is not bool or type(self.local_date_slice) is not bool:
            raise ManifestError("invalid_field", "download flags must be booleans")
        if self.local_date_slice and not self.full_history_download:
            raise ManifestError(
                "invalid_field", "local date slicing requires a full-history download"
            )
        if not isinstance(self.quality_issue_counts, Mapping):
            raise ManifestError("invalid_field", "quality_issue_counts must be a mapping")
        checked_counts: dict[str, int] = {}
        for key, value in self.quality_issue_counts.items():
            invalid_count = (
                not isinstance(key, str)
                or not key
                or not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            )
            if invalid_count:
                raise ManifestError(
                    "invalid_field",
                    "quality issue counts must be named non-negative integers",
                )
            checked_counts[key] = value
        immutable_counts = MappingProxyType(dict(sorted(checked_counts.items())))
        object.__setattr__(self, "quality_issue_counts", immutable_counts)
        stable_key = _canonical_json(_stable_id_key(self.to_dict())).encode("utf-8")
        expected_id = hashlib.sha256(stable_key).hexdigest()
        if self.snapshot_id != expected_id:
            raise ManifestError(
                "invalid_snapshot_id", "snapshot_id does not match the stable manifest key"
            )

    def to_dict(self) -> dict[str, object]:
        return {name: _json_value(getattr(self, name)) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, values: Mapping[str, object]) -> ManifestRecord:
        if not isinstance(values, Mapping):
            raise ManifestError("invalid_record", "manifest line must be a JSON object")
        expected = set(cls.__dataclass_fields__)
        if set(values) != expected:
            raise ManifestError(
                "invalid_fields", "manifest record fields are incomplete or unknown"
            )
        parsed = dict(values)
        date_fields = (
            "requested_start",
            "requested_end",
            "actual_start",
            "actual_end",
            "latest_market_date",
        )
        canonical_string_fields = ("fetched_at_utc", "snapshot_relative_path", *date_fields)
        if any(type(parsed[name]) is not str for name in canonical_string_fields):
            raise ManifestError(
                "invalid_record",
                "manifest canonical date, timestamp, and path values must be strings",
            )
        try:
            timestamp_text = parsed["fetched_at_utc"]
            if not timestamp_text.endswith("Z"):
                raise ValueError("timestamp is not canonical UTC")
            timestamp = datetime.fromisoformat(timestamp_text[:-1] + "+00:00")
            if _json_value(timestamp) != timestamp_text:
                raise ValueError("timestamp is not canonical UTC")
            parsed["fetched_at_utc"] = timestamp
            for name in date_fields:
                date_text = parsed[name]
                if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date_text) is None:
                    raise ValueError("date is not canonical ISO format")
                parsed[name] = date.fromisoformat(date_text)
            parsed["snapshot_relative_path"] = Path(parsed["snapshot_relative_path"])
            return cls(**parsed)
        except (TypeError, ValueError) as exc:
            raise ManifestError(
                "invalid_record", "manifest record contains invalid canonical types"
            ) from exc


@dataclass(frozen=True)
class ManifestAppendResult:
    status: str
    snapshot_id: str
    manifest_path: Path


class ManifestWriter:
    def __init__(self, path: str | Path, *, logger: logging.Logger | None = None):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.logger = logger

    def _open_parent(self) -> int:
        parent = self.path.parent
        if parent.name == "manifests" and parent.parent.name == "data":
            root = parent.parent.parent
            components = ("data", "manifests")
        else:
            root = parent
            components = ()
        try:
            root.mkdir(parents=True, exist_ok=True)
            current_fd = os.open(root, _DIRECTORY_FLAGS)
        except OSError as exc:
            message = (
                "manifest parent must not be a symlink"
                if exc.errno == getattr(os, "ELOOP", 62) or root.is_symlink()
                else "manifest parent cannot be created or opened"
            )
            raise ManifestError("parent_open_failed", message) from exc
        for component in components:
            try:
                os.mkdir(component, mode=0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                _close_quietly(current_fd)
                raise ManifestError(
                    "parent_create_failed", "manifest parent cannot be created"
                ) from exc
            try:
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                is_symlink = False
                try:
                    is_symlink = stat.S_ISLNK(
                        os.stat(component, dir_fd=current_fd, follow_symlinks=False).st_mode
                    )
                except OSError:
                    pass
                _close_quietly(current_fd)
                message = (
                    "manifest parent must not contain a symlink"
                    if exc.errno == getattr(os, "ELOOP", 62) or is_symlink
                    else "manifest parent is not a safe directory"
                )
                raise ManifestError("parent_open_failed", message) from exc
            try:
                os.close(current_fd)
            except OSError as exc:
                _close_quietly(next_fd)
                raise ManifestError("close_failed", "manifest parent close failed") from exc
            current_fd = next_fd
        return current_fd

    @staticmethod
    def _open_regular(
        parent_fd: int,
        name: str,
        flags: int,
        *,
        purpose: str,
        allow_missing: bool = False,
    ) -> int | None:
        descriptor: int | None = None
        last_error: OSError | None = None
        attempts = 5 if flags & os.O_CREAT else 1
        for _ in range(attempts):
            try:
                descriptor = os.open(name, flags | _NOFOLLOW, 0o644, dir_fd=parent_fd)
                break
            except FileNotFoundError as exc:
                last_error = exc
                if not flags & os.O_CREAT:
                    if allow_missing:
                        return None
                    raise ManifestError("open_failed", f"manifest {purpose} is missing") from None
            except OSError as exc:
                last_error = exc
                break
        if descriptor is None:
            assert last_error is not None
            message = (
                f"manifest {purpose} must not be a symlink"
                if last_error.errno == getattr(os, "ELOOP", 62)
                else f"manifest {purpose} open failed"
            )
            raise ManifestError("open_failed", message) from last_error
        try:
            metadata = os.fstat(descriptor)
        except OSError as exc:
            _close_quietly(descriptor)
            raise ManifestError("stat_failed", f"manifest {purpose} stat failed") from exc
        if not stat.S_ISREG(metadata.st_mode):
            _close_quietly(descriptor)
            raise ManifestError("not_regular", f"manifest {purpose} is not a regular file")
        if purpose == "data file" and metadata.st_nlink != 1:
            _close_quietly(descriptor)
            raise ManifestError("hardlink_alias", "manifest data file has a hardlink alias")
        return descriptor

    @contextmanager
    def _locked(self):
        parent_fd = self._open_parent()
        lock_fd: int | None = None
        main_error: BaseException | None = None
        try:
            lock_fd = self._open_regular(
                parent_fd,
                self.lock_path.name,
                os.O_RDWR | os.O_CREAT,
                purpose="lock",
            )
            assert lock_fd is not None
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise ManifestError("lock_failed", "manifest file lock failed") from exc
            yield parent_fd
        except BaseException as exc:
            main_error = exc
            raise
        finally:
            cleanup_error: ManifestError | None = None
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError as exc:
                    cleanup_error = ManifestError("unlock_failed", "manifest file unlock failed")
                    cleanup_error.__cause__ = exc
                try:
                    os.close(lock_fd)
                except OSError as exc:
                    if cleanup_error is None:
                        cleanup_error = ManifestError("close_failed", "manifest lock close failed")
                        cleanup_error.__cause__ = exc
            try:
                os.close(parent_fd)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = ManifestError("close_failed", "manifest parent close failed")
                    cleanup_error.__cause__ = exc
            if main_error is None and cleanup_error is not None:
                raise cleanup_error

    def _read_bytes_unlocked(self, parent_fd: int) -> bytes:
        descriptor = self._open_regular(
            parent_fd,
            self.path.name,
            os.O_RDONLY,
            purpose="data file",
            allow_missing=True,
        )
        if descriptor is None:
            return b""
        main_error: BaseException | None = None
        try:
            content = bytearray()
            while True:
                try:
                    block = os.read(descriptor, 1024 * 1024)
                except OSError as exc:
                    raise ManifestError("read_failed", "manifest data file read failed") from exc
                if not block:
                    break
                content.extend(block)
        except BaseException as exc:
            main_error = exc
            raise
        finally:
            try:
                os.close(descriptor)
            except OSError as exc:
                if main_error is None:
                    raise ManifestError("close_failed", "manifest data file close failed") from exc
        return bytes(content)

    @staticmethod
    def _parse_bytes(content: bytes) -> tuple[ManifestRecord, ...]:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestError("unreadable_manifest", "manifest is not readable UTF-8") from exc
        if text and not text.endswith("\n"):
            raise ManifestError(
                "incomplete_manifest", "manifest must end with a complete newline-delimited record"
            )
        records: list[ManifestRecord] = []
        seen: dict[str, str] = {}
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line:
                raise ManifestError("invalid_json", f"manifest JSON line {line_number} is empty")
            try:
                values = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
            except json.JSONDecodeError as exc:
                raise ManifestError(
                    "invalid_json", f"manifest JSON line {line_number} is invalid JSON"
                ) from exc
            record = ManifestRecord.from_dict(values)
            identity = _record_identity(record)
            previous = seen.get(record.snapshot_id)
            if previous is not None:
                if previous != identity:
                    raise ManifestError(
                        "duplicate_conflict",
                        "manifest contains a duplicate snapshot_id conflict",
                    )
                continue
            seen[record.snapshot_id] = identity
            records.append(record)
        return tuple(records)

    def _read_unlocked(self, parent_fd: int) -> tuple[ManifestRecord, ...]:
        return self._parse_bytes(self._read_bytes_unlocked(parent_fd))

    def read_all(self) -> tuple[ManifestRecord, ...]:
        with self._locked() as parent_fd:
            return self._read_unlocked(parent_fd)

    @staticmethod
    def _create_temporary(parent_fd: int) -> tuple[str, int]:
        for _ in range(10):
            name = f".manifest-{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o644,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise ManifestError(
                    "temporary_open_failed", "manifest temporary file open failed"
                ) from exc
            try:
                metadata = os.fstat(descriptor)
            except OSError as exc:
                _close_quietly(descriptor)
                try:
                    os.unlink(name, dir_fd=parent_fd)
                except OSError:
                    pass
                raise ManifestError(
                    "temporary_stat_failed", "manifest temporary file stat failed"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode):
                _close_quietly(descriptor)
                try:
                    os.unlink(name, dir_fd=parent_fd)
                except OSError:
                    pass
                raise ManifestError(
                    "temporary_not_regular", "manifest temporary path is not a regular file"
                )
            return name, descriptor
        raise ManifestError("temporary_collision", "manifest temporary filename allocation failed")

    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        offset = 0
        while offset < len(content):
            try:
                written = os.write(descriptor, content[offset:])
            except OSError as exc:
                raise ManifestError("write_failed", "manifest temporary file write failed") from exc
            if written <= 0:
                raise ManifestError("short_write", "manifest rewrite did not make progress")
            offset += written

    def _replace_unlocked(self, parent_fd: int, content: bytes) -> None:
        temporary_name, descriptor = self._create_temporary(parent_fd)
        main_error: BaseException | None = None
        descriptor_open = True
        try:
            self._write_all(descriptor, content)
            try:
                os.fsync(descriptor)
            except OSError as exc:
                raise ManifestError("fsync_failed", "manifest temporary file fsync failed") from exc
            try:
                os.close(descriptor)
                descriptor_open = False
            except OSError as exc:
                raise ManifestError("close_failed", "manifest temporary file close failed") from exc
            try:
                os.replace(
                    temporary_name,
                    self.path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_name = ""
            except OSError as exc:
                raise ManifestError("replace_failed", "manifest atomic replace failed") from exc
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise ManifestError("fsync_failed", "manifest parent fsync failed") from exc
        except BaseException as exc:
            main_error = exc
            raise
        finally:
            cleanup_error: ManifestError | None = None
            if descriptor_open:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    cleanup_error = ManifestError(
                        "close_failed", "manifest temporary file close failed"
                    )
                    cleanup_error.__cause__ = exc
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    if cleanup_error is None:
                        cleanup_error = ManifestError(
                            "cleanup_failed", "manifest temporary file cleanup failed"
                        )
                        cleanup_error.__cause__ = exc
            if main_error is None and cleanup_error is not None:
                raise cleanup_error

    def append_batch(self, records: tuple[ManifestRecord, ...]) -> tuple[ManifestAppendResult, ...]:
        if not isinstance(records, tuple) or not records:
            raise ManifestError("invalid_batch", "append_batch requires a non-empty tuple")
        if not all(isinstance(record, ManifestRecord) for record in records):
            raise ManifestError("invalid_record", "append_batch requires ManifestRecord items")

        batch_identities: dict[str, str] = {}
        for record in records:
            identity = _record_identity(record)
            if record.snapshot_id in batch_identities:
                raise ManifestError(
                    "duplicate_batch_member",
                    "manifest batch contains a duplicate snapshot_id",
                )
            batch_identities[record.snapshot_id] = identity

        with self._locked() as parent_fd:
            existing_bytes = self._read_bytes_unlocked(parent_fd)
            existing_records = self._parse_bytes(existing_bytes)
            existing = {item.snapshot_id: _record_identity(item) for item in existing_records}
            statuses: list[str] = []
            new_lines: list[bytes] = []
            for record in records:
                identity = batch_identities[record.snapshot_id]
                if record.snapshot_id in existing:
                    if existing[record.snapshot_id] != identity:
                        raise ManifestError(
                            "snapshot_conflict",
                            "snapshot_id conflicts with existing manifest content",
                        )
                    statuses.append("duplicate")
                else:
                    new_lines.append((_canonical_json(record.to_dict()) + "\n").encode("utf-8"))
                    statuses.append("appended")
            if new_lines:
                self._replace_unlocked(parent_fd, existing_bytes + b"".join(new_lines))

        results = tuple(
            ManifestAppendResult(status, record.snapshot_id, self.path)
            for record, status in zip(records, statuses, strict=True)
        )
        for result in results:
            log_event(
                self.logger,
                logging.INFO,
                "manifest_duplicate" if result.status == "duplicate" else "manifest_appended",
                snapshot_id=result.snapshot_id,
                path=self.path,
            )
        return results

    def append(self, record: ManifestRecord) -> ManifestAppendResult:
        if not isinstance(record, ManifestRecord):
            raise ManifestError("invalid_record", "append requires a ManifestRecord")
        return self.append_batch((record,))[0]
