"""Lock-free, hash-bound reads of an existing frozen market manifest."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from aquant.data.manifest import ManifestError, ManifestRecord, ManifestWriter

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _open_existing_parent(path: Path) -> int:
    parent = path.parent
    if parent.name == "manifests" and parent.parent.name == "data":
        root = parent.parent.parent
        components = ("data", "manifests")
    else:
        root = parent
        components = ()
    try:
        current_fd = os.open(root, _DIRECTORY_FLAGS)
    except OSError as exc:
        message = (
            "manifest parent must not be a symlink"
            if exc.errno == getattr(os, "ELOOP", 62) or root.is_symlink()
            else "manifest parent cannot be opened"
        )
        raise ManifestError("parent_open_failed", message) from exc
    for component in components:
        try:
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
        except OSError as exc:
            is_symlink = False
            try:
                is_symlink = stat.S_ISLNK(
                    os.stat(
                        component,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    ).st_mode
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
            raise ManifestError(
                "close_failed",
                "manifest parent close failed",
            ) from exc
        current_fd = next_fd
    return current_fd


def _open_frozen_file(parent_fd: int, name: str) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | _NONBLOCK | _NOFOLLOW | _CLOEXEC,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        raise ManifestError(
            "open_failed",
            "manifest data file is missing",
        ) from None
    except OSError as exc:
        message = (
            "manifest data file must not be a symlink"
            if exc.errno == getattr(os, "ELOOP", 62)
            else "manifest data file open failed"
        )
        raise ManifestError("open_failed", message) from exc
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        _close_quietly(descriptor)
        raise ManifestError(
            "stat_failed",
            "manifest data file stat failed",
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        _close_quietly(descriptor)
        raise ManifestError(
            "not_regular",
            "manifest data file is not a regular file",
        )
    if metadata.st_nlink != 1:
        _close_quietly(descriptor)
        raise ManifestError(
            "hardlink_alias",
            "manifest data file has a hardlink alias",
        )
    return descriptor, metadata


def _read_bound_bytes(
    parent_fd: int,
    path: Path,
    *,
    expected_sha256: str,
) -> bytes:
    descriptor, initial = _open_frozen_file(parent_fd, path.name)
    main_error: BaseException | None = None
    try:
        content = bytearray()
        while True:
            try:
                block = os.read(descriptor, 1024 * 1024)
            except OSError as exc:
                raise ManifestError(
                    "read_failed",
                    "manifest data file read failed",
                ) from exc
            if not block:
                break
            content.extend(block)
        try:
            final = os.fstat(descriptor)
            path_metadata = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ManifestError(
                "manifest_binding_changed",
                "frozen manifest binding cannot be verified",
            ) from exc
        if (
            not stat.S_ISREG(final.st_mode)
            or not stat.S_ISREG(path_metadata.st_mode)
            or final.st_nlink != 1
            or path_metadata.st_nlink != 1
            or (initial.st_dev, initial.st_ino) != (final.st_dev, final.st_ino)
            or (initial.st_dev, initial.st_ino) != (path_metadata.st_dev, path_metadata.st_ino)
            or initial.st_size != final.st_size
            or initial.st_mtime_ns != final.st_mtime_ns
            or initial.st_ctime_ns != final.st_ctime_ns
            or len(content) != final.st_size
        ):
            raise ManifestError(
                "manifest_binding_changed",
                "frozen manifest binding changed while read",
            )
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ManifestError(
                "manifest_hash_mismatch",
                "frozen manifest bytes do not match the expected hash",
            )
        return bytes(content)
    except BaseException as exc:
        main_error = exc
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            if main_error is None:
                raise ManifestError(
                    "close_failed",
                    "manifest data file close failed",
                ) from exc


def read_frozen_manifest(
    path: str | Path,
    *,
    expected_sha256: str,
) -> tuple[ManifestRecord, ...]:
    """Read frozen JSONL bytes without creating a directory or lock file."""
    if type(expected_sha256) is not str or _HASH_RE.fullmatch(expected_sha256) is None:
        raise ManifestError(
            "invalid_expected_hash",
            "expected manifest hash is invalid",
        )
    manifest_path = Path(path)
    parent_fd = _open_existing_parent(manifest_path)
    main_error: BaseException | None = None
    try:
        content = _read_bound_bytes(
            parent_fd,
            manifest_path,
            expected_sha256=expected_sha256,
        )
        return ManifestWriter._parse_bytes(content)
    except BaseException as exc:
        main_error = exc
        raise
    finally:
        try:
            os.close(parent_fd)
        except OSError as exc:
            if main_error is None:
                raise ManifestError(
                    "close_failed",
                    "manifest parent close failed",
                ) from exc
