"""Immutable, content-addressed storage for unmodified upstream frames."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from aquant.logging import log_event

_SYMBOL_RE = re.compile(r"[0-9]{6}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_TEMPORARY_RE = re.compile(r"\.snapshot-[0-9a-f]{32}\.tmp")
SOURCE_SLUG_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def is_valid_source_slug(value: object) -> bool:
    """Return whether a value is a safe source component for snapshot filenames."""
    return (
        type(value) is str
        and SOURCE_SLUG_PATTERN.fullmatch(value) is not None
        and ".." not in value
    )


class SnapshotError(RuntimeError):
    """Raised when an immutable raw snapshot cannot be safely stored."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class SnapshotArtifact:
    relative_path: Path
    sha256: str
    size_bytes: int
    row_count: int
    reused: bool


def _parquet_bytes(frame: pd.DataFrame) -> bytes:
    table = pa.Table.from_pandas(frame, preserve_index=False)
    sink = pa.BufferOutputStream()
    pq.write_table(
        table,
        sink,
        compression="zstd",
        compression_level=3,
        use_dictionary=False,
        write_statistics=True,
        data_page_version="2.0",
        version="2.6",
    )
    return sink.getvalue().to_pybytes()


class RawSnapshotStore:
    """Store exact input frames beneath ``data/raw`` without normalization."""

    def __init__(self, root: str | Path, *, logger: logging.Logger | None = None):
        self.root = Path(root)
        self.logger = logger

    def write(
        self,
        frame: pd.DataFrame,
        *,
        symbol: str,
        source_slug: str,
        snapshot_date: date,
    ) -> SnapshotArtifact:
        if not isinstance(frame, pd.DataFrame):
            raise SnapshotError("invalid_frame", "raw snapshot input must be a DataFrame")
        if frame.empty:
            raise SnapshotError("empty_frame", "raw snapshot input must not be empty")
        expected_index = pd.RangeIndex(start=0, stop=len(frame), step=1)
        if (
            not isinstance(frame.index, pd.RangeIndex)
            or not frame.index.equals(expected_index)
            or frame.index.name is not None
        ):
            raise SnapshotError(
                "invalid_index",
                "raw snapshot index must be the default zero-based RangeIndex",
            )
        if not isinstance(symbol, str) or _SYMBOL_RE.fullmatch(symbol) is None:
            raise SnapshotError("invalid_symbol", "snapshot symbol must be exactly six digits")
        if not is_valid_source_slug(source_slug):
            raise SnapshotError("invalid_source", "snapshot source slug is unsafe")
        if type(snapshot_date) is not date:
            raise SnapshotError("invalid_date", "snapshot date must be a date value")

        try:
            content = _parquet_bytes(frame)
        except (pa.ArrowException, TypeError, ValueError) as exc:
            raise SnapshotError(
                "serialization_failed", "raw snapshot serialization failed"
            ) from exc
        digest = hashlib.sha256(content).hexdigest()
        relative = PurePosixPath(
            "data",
            "raw",
            snapshot_date.isoformat(),
            symbol,
            f"{source_slug}-{digest}.parquet",
        )
        parent = self.root.joinpath(*relative.parts[:-1])
        parent_fd = self._open_safe_parent(parent)
        target_name = relative.name
        temporary_name: str | None = None
        main_error: BaseException | None = None
        reused = False
        try:
            temporary_name, temporary_fd = self._create_temporary(parent_fd)
            temporary_error: BaseException | None = None
            try:
                self._write_all(temporary_fd, content)
                self._fsync(temporary_fd, "snapshot file fsync failed")
                try:
                    os.fchmod(temporary_fd, 0o444)
                except OSError as exc:
                    raise SnapshotError(
                        "permission_failed", "snapshot read-only permission update failed"
                    ) from exc
            except BaseException as exc:
                temporary_error = exc
                raise
            finally:
                try:
                    os.close(temporary_fd)
                except OSError as exc:
                    if temporary_error is None:
                        raise SnapshotError(
                            "close_failed", "snapshot temporary file close failed"
                        ) from exc
            try:
                os.link(
                    temporary_name,
                    target_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                self._verify_existing(parent_fd, target_name, digest)
                reused = True
            except OSError as exc:
                raise SnapshotError("publish_failed", "snapshot hardlink publish failed") from exc
            self._fsync(parent_fd, "snapshot directory fsync failed")
        except BaseException as exc:
            main_error = exc
            raise
        finally:
            cleanup_error: SnapshotError | None = None
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                    self._fsync(parent_fd, "snapshot cleanup directory fsync failed")
                except FileNotFoundError:
                    pass
                except SnapshotError as exc:
                    cleanup_error = exc
                except OSError as exc:
                    cleanup_error = SnapshotError(
                        "cleanup_failed", "snapshot temporary file cleanup failed"
                    )
                    cleanup_error.__cause__ = exc
            try:
                os.close(parent_fd)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = SnapshotError("close_failed", "snapshot directory close failed")
                    cleanup_error.__cause__ = exc
            if main_error is None and cleanup_error is not None:
                raise cleanup_error

        artifact = SnapshotArtifact(
            relative_path=Path(relative.as_posix()),
            sha256=digest,
            size_bytes=len(content),
            row_count=len(frame),
            reused=reused,
        )
        log_event(
            self.logger,
            logging.INFO,
            "raw_snapshot_reused" if reused else "raw_snapshot_written",
            symbol=symbol,
            path=artifact.relative_path,
            sha256=digest,
            row_count=len(frame),
        )
        return artifact

    def verify(self, relative_path: str | Path, *, expected_hash: str) -> None:
        """Verify one manifest-referenced snapshot before downstream consumption."""
        if not isinstance(relative_path, (str, os.PathLike)):
            raise SnapshotError("invalid_path", "snapshot verification path is invalid")
        if not isinstance(expected_hash, str) or _HASH_RE.fullmatch(expected_hash) is None:
            raise SnapshotError("invalid_hash", "snapshot verification hash is invalid")
        path = PurePosixPath(Path(relative_path).as_posix())
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.parts) != 5
            or path.parts[:2] != ("data", "raw")
            or not path.name.endswith(f"-{expected_hash}.parquet")
        ):
            raise SnapshotError("invalid_path", "snapshot verification path is unsafe")

        parent = self.root.joinpath(*path.parts[:-1])
        parent_fd = self._open_safe_parent(parent)
        main_error: BaseException | None = None
        try:
            self._verify_existing(parent_fd, path.name, expected_hash)
        except BaseException as exc:
            main_error = exc
            raise
        finally:
            try:
                os.close(parent_fd)
            except OSError as exc:
                if main_error is None:
                    raise SnapshotError("close_failed", "snapshot directory close failed") from exc

    @staticmethod
    def _verify_existing(parent_fd: int, name: str, expected_hash: str) -> None:
        try:
            descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            message = (
                "existing snapshot is a symlink"
                if exc.errno == getattr(os, "ELOOP", 62)
                else "existing snapshot cannot be opened"
            )
            raise SnapshotError("existing_open_failed", message) from exc
        read_error: BaseException | None = None
        try:
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise SnapshotError(
                        "existing_not_file", "existing snapshot is not a regular file"
                    )
                if metadata.st_nlink != 1:
                    RawSnapshotStore._recover_reserved_temporary_aliases(parent_fd, metadata)
                    metadata = os.fstat(descriptor)
                    if metadata.st_nlink != 1:
                        raise SnapshotError(
                            "existing_hardlink_alias",
                            "existing snapshot has a hardlink alias",
                        )
                digest = hashlib.sha256()
                while True:
                    block = os.read(descriptor, 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                actual_hash = digest.hexdigest()
                if actual_hash != expected_hash:
                    raise SnapshotError(
                        "existing_hash_mismatch",
                        "existing snapshot hash does not match its content-addressed path",
                    )
                os.fchmod(descriptor, 0o444)
            except OSError as exc:
                raise SnapshotError(
                    "existing_read_failed", "existing snapshot cannot be read"
                ) from exc
        except BaseException as exc:
            read_error = exc
            raise
        finally:
            try:
                os.close(descriptor)
            except OSError as exc:
                if read_error is None:
                    raise SnapshotError("close_failed", "existing snapshot close failed") from exc

    @staticmethod
    def _recover_reserved_temporary_aliases(parent_fd: int, target: os.stat_result) -> None:
        """Remove only same-inode aliases from this store's reserved temp namespace."""
        removed = False
        try:
            names = os.listdir(parent_fd)
            for name in names:
                if _TEMPORARY_RE.fullmatch(name) is None:
                    continue
                candidate = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    stat.S_ISREG(candidate.st_mode)
                    and candidate.st_dev == target.st_dev
                    and candidate.st_ino == target.st_ino
                ):
                    os.unlink(name, dir_fd=parent_fd)
                    removed = True
            if removed:
                os.fsync(parent_fd)
        except OSError as exc:
            raise SnapshotError(
                "existing_hardlink_alias",
                "existing snapshot hardlink recovery failed",
            ) from exc

    def _open_safe_parent(self, parent: Path) -> int:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SnapshotError("directory_failed", "snapshot root cannot be created") from exc
        try:
            current_fd = os.open(self.root, _DIRECTORY_FLAGS)
        except OSError as exc:
            message = (
                "snapshot root must not be a symlink"
                if exc.errno == getattr(os, "ELOOP", 62) or self.root.is_symlink()
                else "snapshot root cannot be opened"
            )
            raise SnapshotError("directory_failed", message) from exc
        relative_parent = parent.relative_to(self.root)
        for component in relative_parent.parts:
            try:
                os.mkdir(component, mode=0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                _close_quietly(current_fd)
                raise SnapshotError(
                    "directory_failed", "snapshot storage parent cannot be created"
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
                    "snapshot storage parent must not contain a symlink"
                    if exc.errno == getattr(os, "ELOOP", 62) or is_symlink
                    else "snapshot storage parent is not a safe directory"
                )
                raise SnapshotError("unsafe_directory", message) from exc
            try:
                os.close(current_fd)
            except OSError as exc:
                _close_quietly(next_fd)
                raise SnapshotError("close_failed", "snapshot directory close failed") from exc
            current_fd = next_fd
        return current_fd

    @staticmethod
    def _create_temporary(parent_fd: int) -> tuple[str, int]:
        for _ in range(10):
            name = f".snapshot-{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
                return name, descriptor
            except FileExistsError:
                continue
            except OSError as exc:
                raise SnapshotError(
                    "temporary_open_failed", "snapshot temporary file open failed"
                ) from exc
        raise SnapshotError("temporary_collision", "snapshot temporary filename allocation failed")

    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        offset = 0
        try:
            while offset < len(content):
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise OSError("snapshot write made no progress")
                offset += written
        except OSError as exc:
            raise SnapshotError("write_failed", "snapshot write failed") from exc

    @staticmethod
    def _fsync(descriptor: int, message: str) -> None:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise SnapshotError("fsync_failed", message) from exc
