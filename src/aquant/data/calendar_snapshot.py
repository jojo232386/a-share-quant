"""Immutable, content-addressed trading-calendar evidence."""

from __future__ import annotations

import bisect
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_TEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW
_VERIFIED_CALENDAR_REGISTRY: dict[
    int, tuple[VerifiedTradingCalendar, str]
] = {}


class CalendarError(RuntimeError):
    """Raised when calendar evidence is invalid, unsafe, or damaged."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_calendar_bytes(dates: tuple[date, ...]) -> bytes:
    """Return the stable byte representation used as the calendar identity."""
    _validate_dates(dates)
    payload = {
        "dates": [value.isoformat() for value in dates],
        "schema_version": "1.0",
    }
    return (_canonical_json(payload) + "\n").encode("utf-8")


def _validate_dates(dates: object) -> None:
    invalid = (
        type(dates) is not tuple
        or not dates
        or any(type(value) is not date or value.weekday() >= 5 for value in dates)
        or any(left >= right for left, right in zip(dates, dates[1:], strict=False))
    )
    if invalid:
        raise CalendarError("invalid_dates", "calendar dates are invalid")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CalendarError("duplicate_json_key", "calendar contains a duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True)
class CalendarRecord:
    schema_version: str
    calendar_id: str
    file_sha256: str
    source_provider: str
    source_function: str
    source_version: str
    fetched_at_utc: datetime
    first_date: date
    last_complete_date: date
    row_count: int
    relative_path: Path

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != "1.0":
            raise CalendarError("invalid_record", "calendar record verification failed")
        if any(
            type(value) is not str or _HASH_RE.fullmatch(value) is None
            for value in (self.calendar_id, self.file_sha256)
        ):
            raise CalendarError("invalid_record", "calendar record verification failed")
        for value in (self.source_provider, self.source_function, self.source_version):
            if type(value) is not str or _TEXT_RE.fullmatch(value) is None:
                raise CalendarError("invalid_record", "calendar record verification failed")
        if (
            type(self.fetched_at_utc) is not datetime
            or self.fetched_at_utc.utcoffset() != UTC.utcoffset(None)
        ):
            raise CalendarError("invalid_record", "calendar record verification failed")
        if (
            type(self.first_date) is not date
            or type(self.last_complete_date) is not date
            or self.first_date > self.last_complete_date
            or type(self.row_count) is not int
            or isinstance(self.row_count, bool)
            or self.row_count <= 0
        ):
            raise CalendarError("invalid_record", "calendar record verification failed")
        path = PurePosixPath(Path(self.relative_path).as_posix())
        expected = PurePosixPath("data", "calendars", f"{self.calendar_id}.json")
        if path != expected:
            raise CalendarError("invalid_record", "calendar record verification failed")
        object.__setattr__(self, "relative_path", Path(path.as_posix()))

    def to_dict(self) -> dict[str, object]:
        return {
            "calendar_id": self.calendar_id,
            "fetched_at_utc": self.fetched_at_utc.isoformat().replace("+00:00", "Z"),
            "file_sha256": self.file_sha256,
            "first_date": self.first_date.isoformat(),
            "last_complete_date": self.last_complete_date.isoformat(),
            "relative_path": self.relative_path.as_posix(),
            "row_count": self.row_count,
            "schema_version": self.schema_version,
            "source_function": self.source_function,
            "source_provider": self.source_provider,
            "source_version": self.source_version,
        }

    @classmethod
    def from_dict(cls, values: object) -> CalendarRecord:
        if type(values) is not dict or set(values) != set(cls.__dataclass_fields__):
            raise CalendarError("invalid_record", "calendar record verification failed")
        parsed = dict(values)
        canonical_fields = (
            "calendar_id",
            "fetched_at_utc",
            "file_sha256",
            "first_date",
            "last_complete_date",
            "relative_path",
            "schema_version",
            "source_function",
            "source_provider",
            "source_version",
        )
        if any(type(parsed[name]) is not str for name in canonical_fields):
            raise CalendarError("invalid_record", "calendar record verification failed")
        try:
            timestamp = parsed["fetched_at_utc"]
            if not timestamp.endswith("Z"):
                raise ValueError
            parsed["fetched_at_utc"] = datetime.fromisoformat(timestamp[:-1] + "+00:00")
            parsed["first_date"] = date.fromisoformat(parsed["first_date"])
            parsed["last_complete_date"] = date.fromisoformat(parsed["last_complete_date"])
            parsed["relative_path"] = Path(parsed["relative_path"])
            return cls(**parsed)
        except (TypeError, ValueError) as exc:
            raise CalendarError(
                "invalid_record", "calendar record verification failed"
            ) from exc


@dataclass(frozen=True, init=False)
class VerifiedTradingCalendar:
    """Calendar data obtainable only through the verified loader."""

    dates: tuple[date, ...]
    calendar_id: str
    file_sha256: str

    def next_session(self, value: date) -> date | None:
        index = bisect.bisect_right(self.dates, value)
        return None if index == len(self.dates) else self.dates[index]

    def contains(self, value: date) -> bool:
        index = bisect.bisect_left(self.dates, value)
        return index < len(self.dates) and self.dates[index] == value


def verify_trading_calendar(calendar: VerifiedTradingCalendar) -> None:
    """Recompute identity and require the exact loader-created object."""
    if type(calendar) is not VerifiedTradingCalendar:
        raise CalendarError("invalid_verified_calendar", "verified calendar is invalid")
    registered = _VERIFIED_CALENDAR_REGISTRY.get(id(calendar))
    if registered is None or registered[0] is not calendar:
        raise CalendarError("invalid_verified_calendar", "verified calendar is invalid")
    try:
        digest = hashlib.sha256(canonical_calendar_bytes(calendar.dates)).hexdigest()
    except CalendarError as exc:
        raise CalendarError("invalid_verified_calendar", "verified calendar is invalid") from exc
    if (
        digest != registered[1]
        or calendar.calendar_id != digest
        or calendar.file_sha256 != digest
    ):
        raise CalendarError("invalid_verified_calendar", "verified calendar is invalid")


class CalendarSnapshotStore:
    """Store calendar content and its first-seen provenance beneath data/calendars."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def write(
        self,
        dates: tuple[date, ...],
        *,
        source_provider: str,
        source_function: str,
        source_version: str,
        fetched_at_utc: datetime,
    ) -> CalendarRecord:
        content = canonical_calendar_bytes(dates)
        digest = hashlib.sha256(content).hexdigest()
        candidate = CalendarRecord(
            schema_version="1.0",
            calendar_id=digest,
            file_sha256=digest,
            source_provider=source_provider,
            source_function=source_function,
            source_version=source_version,
            fetched_at_utc=fetched_at_utc,
            first_date=dates[0],
            last_complete_date=dates[-1],
            row_count=len(dates),
            relative_path=Path("data/calendars") / f"{digest}.json",
        )
        parent_fd = self._open_calendar_directory()
        lock_fd: int | None = None
        try:
            lock_fd = self._open_regular(
                parent_fd, ".manifest.lock", os.O_RDWR | os.O_CREAT, purpose="lock"
            )
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            records = self._read_manifest_from(parent_fd)
            matches = tuple(item for item in records if item.calendar_id == digest)
            if len(matches) > 1:
                raise CalendarError("duplicate_record", "calendar manifest has duplicate records")
            if matches:
                self._verify_file(parent_fd, f"{digest}.json", digest)
                return matches[0]
            self._publish_file(parent_fd, f"{digest}.json", content, digest)
            self._append_manifest(parent_fd, candidate)
            return candidate
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            os.close(parent_fd)

    def read_manifest(self) -> tuple[CalendarRecord, ...]:
        parent_fd = self._open_calendar_directory()
        try:
            records = self._read_manifest_from(parent_fd)
            ids = [record.calendar_id for record in records]
            if len(ids) != len(set(ids)):
                raise CalendarError("duplicate_record", "calendar manifest has duplicate records")
            return records
        finally:
            os.close(parent_fd)

    def _open_calendar_directory(self) -> int:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            current_fd = os.open(self.root, _DIRECTORY_FLAGS)
        except OSError as exc:
            raise CalendarError("unsafe_path", "calendar root must not be a symlink") from exc
        for component in ("data", "calendars"):
            try:
                os.mkdir(component, mode=0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            try:
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                os.close(current_fd)
                raise CalendarError(
                    "unsafe_path", "calendar path must not contain a symlink"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd

    @staticmethod
    def _open_regular(parent_fd: int, name: str, flags: int, *, purpose: str) -> int:
        try:
            descriptor = os.open(name, flags | _NOFOLLOW, 0o644, dir_fd=parent_fd)
        except OSError as exc:
            message = (
                f"calendar {purpose} must not be a symlink"
                if exc.errno == getattr(os, "ELOOP", 62)
                else f"calendar {purpose} cannot be opened"
            )
            raise CalendarError("open_failed", message) from exc
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise CalendarError("not_regular", f"calendar {purpose} is not a regular file")
        if purpose != "lock" and metadata.st_nlink != 1:
            os.close(descriptor)
            raise CalendarError("hardlink_alias", f"calendar {purpose} has a hardlink alias")
        return descriptor

    @classmethod
    def _verify_file(cls, parent_fd: int, name: str, expected_hash: str) -> bytes:
        descriptor = cls._open_regular(parent_fd, name, os.O_RDONLY, purpose="content file")
        try:
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            digest = hashlib.sha256()
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                chunks.append(block)
                digest.update(block)
            after = os.fstat(descriptor)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or digest.hexdigest() != expected_hash
            ):
                raise CalendarError(
                    "content_verification_failed", "calendar content verification failed"
                )
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @classmethod
    def _publish_file(
        cls, parent_fd: int, target_name: str, content: bytes, expected_hash: str
    ) -> None:
        temporary = f".calendar-{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise CalendarError("write_failed", "calendar content write failed")
                view = view[written:]
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o444)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(
                    temporary,
                    target_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            os.unlink(temporary, dir_fd=parent_fd)
            os.fsync(parent_fd)
            cls._verify_file(parent_fd, target_name, expected_hash)
        except OSError as exc:
            raise CalendarError("publish_failed", "calendar content publication failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass

    @classmethod
    def _read_manifest_from(cls, parent_fd: int) -> tuple[CalendarRecord, ...]:
        try:
            descriptor = cls._open_regular(
                parent_fd, "manifest.jsonl", os.O_RDONLY, purpose="manifest"
            )
        except CalendarError as exc:
            if exc.__cause__ is not None and isinstance(exc.__cause__, FileNotFoundError):
                return ()
            raise
        try:
            content = b""
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                content += block
        finally:
            os.close(descriptor)
        records: list[CalendarRecord] = []
        try:
            text = content.decode("utf-8")
            for line in text.splitlines():
                if not line:
                    raise CalendarError("invalid_manifest", "calendar manifest is invalid")
                values = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
                records.append(CalendarRecord.from_dict(values))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CalendarError("invalid_manifest", "calendar manifest is invalid") from exc
        return tuple(records)

    @classmethod
    def _append_manifest(cls, parent_fd: int, record: CalendarRecord) -> None:
        descriptor = cls._open_regular(
            parent_fd,
            "manifest.jsonl",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            purpose="manifest",
        )
        try:
            payload = (_canonical_json(record.to_dict()) + "\n").encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise CalendarError("write_failed", "calendar manifest write failed")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def load_verified_calendar(
    root: str | Path, record: CalendarRecord
) -> VerifiedTradingCalendar:
    """Load an exact record only after file, content, and metadata verification."""
    if type(record) is not CalendarRecord:
        raise CalendarError("invalid_record", "calendar record verification failed")
    try:
        checked = CalendarRecord.from_dict(record.to_dict())
    except CalendarError as exc:
        raise CalendarError("invalid_record", "calendar record verification failed") from exc
    store = CalendarSnapshotStore(root)
    parent_fd = store._open_calendar_directory()
    try:
        content = store._verify_file(parent_fd, checked.relative_path.name, checked.file_sha256)
    finally:
        os.close(parent_fd)
    try:
        payload = json.loads(content, object_pairs_hook=_reject_duplicate_json_keys)
        if type(payload) is not dict or set(payload) != {"dates", "schema_version"}:
            raise ValueError
        if payload["schema_version"] != "1.0" or type(payload["dates"]) is not list:
            raise ValueError
        dates = tuple(date.fromisoformat(value) for value in payload["dates"])
        if canonical_calendar_bytes(dates) != content:
            raise ValueError
    except (CalendarError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CalendarError(
            "content_verification_failed", "calendar content verification failed"
        ) from exc
    if (
        checked.calendar_id != hashlib.sha256(content).hexdigest()
        or checked.first_date != dates[0]
        or checked.last_complete_date != dates[-1]
        or checked.row_count != len(dates)
    ):
        raise CalendarError("invalid_record", "calendar record verification failed")
    calendar = object.__new__(VerifiedTradingCalendar)
    object.__setattr__(calendar, "dates", dates)
    object.__setattr__(calendar, "calendar_id", checked.calendar_id)
    object.__setattr__(calendar, "file_sha256", checked.file_sha256)
    _VERIFIED_CALENDAR_REGISTRY[id(calendar)] = (calendar, checked.calendar_id)
    return calendar
