"""Exact frozen-input staging and lock quarantine for Gate E."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from aquant.portfolio import PORTFOLIO_ARTIFACT_FILES
from aquant.release_manifest import (
    ReleaseManifest,
    ReleaseVerificationError,
    load_release_manifest,
    verify_release_inputs,
)

TRUSTED_RELEASE_MANIFEST_SHA256 = "9d9ad2ed7c351a9e06d86de6b3edea2221ba6b256de072e3744b478b65ca7422"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_EXPECTED_INPUT_COUNT = 25
_RUN_ID_RE = re.compile(r"[0-9a-f]{64}")
_LOCK_RELATIVE = PurePosixPath("data/manifests/manifest.jsonl.lock")
_RUNTIME_CONFIG_RELATIVE = PurePosixPath(
    "configs/releases/v0.2_gate_e.json"
)
_RELEASE_ROOT_ARGUMENT = PurePosixPath("release/v0.1-research")
_QUARANTINE_ARGUMENT = PurePosixPath(
    "release/v0.2-gate-e/deviations/manifest.jsonl.lock.quarantined"
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 1
_PUBLICATION_STATES = frozenset(
    {
        "staging_retained",
        "destination_published_unverified",
        "candidate_a_published_candidate_b_incomplete",
    }
)


class GateEInputError(RuntimeError):
    """Stable, sanitized failure for the Gate E input boundary."""

    def __init__(
        self,
        code: str,
        *,
        cause_code: str | None = None,
        evidence_name: str | None = None,
        publication_state: str | None = None,
    ):
        if evidence_name is not None and (
            PurePosixPath(evidence_name).name != evidence_name or evidence_name in {"", ".", ".."}
        ):
            raise ValueError("evidence_name must be one basename")
        if publication_state is not None and publication_state not in _PUBLICATION_STATES:
            raise ValueError("invalid publication_state")
        self.code = code
        self.cause_code = cause_code
        self.evidence_name = evidence_name
        self.publication_state = publication_state
        super().__init__(code)


@dataclass(frozen=True)
class GateEDeviation:
    """Recoverable evidence for the one approved frozen-input deviation."""

    original_path: str
    quarantine_path: str
    size: int
    sha256: str
    device: int
    inode: int
    link_count_before: int
    link_count_after: int
    birth_at_utc: str
    modified_at_utc: str
    recorded_at_utc: str
    move_reason: str
    research_semantics: str


@dataclass(frozen=True)
class GateEInputCopies:
    """Evidence that two independent exact input roots were staged."""

    manifest_sha256: str
    file_count: int
    destination_roots: tuple[Path, Path]


@dataclass(frozen=True)
class GateEInputCopy:
    """Evidence for one independently staged frozen-input root."""

    manifest_sha256: str
    file_count: int
    destination_root: Path


@dataclass(frozen=True)
class GateEPostRunInputs:
    """Evidence for the unchanged post-run input-root state."""

    manifest_sha256: str
    file_count: int


@dataclass(frozen=True)
class _DirectoryHandle:
    descriptor: int
    metadata: os.stat_result
    parent_descriptor: int | None
    name: str | None
    created: bool
    absolute_path: Path | None


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _stable_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_flags() -> int:
    if (
        _NOFOLLOW == 0
        or _DIRECTORY == 0
        or os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
    ):
        raise GateEInputError("atomic_path_boundary_unavailable")
    return os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC


def _open_directory_path(path: Path, *, code: str) -> _DirectoryHandle:
    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    absolute = Path(os.path.abspath(path))
    descriptor: int | None = None
    try:
        before = absolute.lstat()
        descriptor = os.open(absolute, _directory_flags())
        opened = os.fstat(descriptor)
        after = absolute.lstat()
    except GateEInputError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise GateEInputError(code) from exc
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or not _same_object(before, opened)
        or not _same_object(opened, after)
    ):
        os.close(descriptor)
        raise GateEInputError(code)
    return _DirectoryHandle(
        descriptor=descriptor,
        metadata=opened,
        parent_descriptor=None,
        name=None,
        created=False,
        absolute_path=absolute,
    )


def _open_directory_at(
    parent_descriptor: int,
    *,
    name: str,
    create: bool,
    code: str,
    conflict_code: str | None = None,
) -> _DirectoryHandle:
    if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name or "\x00" in name:
        raise GateEInputError(code)
    created = False
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            _directory_flags(),
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError as exc:
        if not create:
            raise GateEInputError(code) from exc
        try:
            os.mkdir(name, mode=0o755, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
            created = True
        except FileExistsError as mkdir_exc:
            raise GateEInputError(conflict_code or code) from mkdir_exc
        except OSError as mkdir_exc:
            raise GateEInputError(code) from mkdir_exc
        try:
            descriptor = os.open(
                name,
                _directory_flags(),
                dir_fd=parent_descriptor,
            )
        except OSError as open_exc:
            raise GateEInputError(code) from open_exc
    except GateEInputError:
        raise
    except OSError as exc:
        raise GateEInputError(code) from exc

    try:
        opened = os.fstat(descriptor)
        named = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        os.close(descriptor)
        raise GateEInputError(code) from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or not _same_object(opened, named)
    ):
        os.close(descriptor)
        raise GateEInputError(code)
    return _DirectoryHandle(
        descriptor=descriptor,
        metadata=opened,
        parent_descriptor=parent_descriptor,
        name=name,
        created=created,
        absolute_path=None,
    )


def _assert_directory_bound(
    handle: _DirectoryHandle,
    *,
    code: str,
) -> None:
    if handle.parent_descriptor is None:
        if handle.absolute_path is None:
            raise GateEInputError(code)
        try:
            opened = os.fstat(handle.descriptor)
            named = handle.absolute_path.lstat()
        except OSError as exc:
            raise GateEInputError(code) from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or not _same_object(handle.metadata, opened)
            or not _same_object(opened, named)
        ):
            raise GateEInputError(code)
        return
    if handle.name is None:
        raise GateEInputError(code)
    try:
        opened = os.fstat(handle.descriptor)
        named = os.stat(
            handle.name,
            dir_fd=handle.parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise GateEInputError(code) from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or not _same_object(handle.metadata, opened)
        or not _same_object(opened, named)
    ):
        raise GateEInputError(code)


def _assert_directories_bound(
    handles: Sequence[_DirectoryHandle],
    *,
    code: str,
) -> None:
    for handle in handles:
        _assert_directory_bound(handle, code=code)


def _safe_existing_directory(path: Path, *, code: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    absolute = Path(os.path.abspath(path))
    try:
        metadata = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GateEInputError(code) from exc
    if resolved != absolute or not stat.S_ISDIR(metadata.st_mode) or absolute.is_symlink():
        raise GateEInputError(code)
    return absolute


def _read_regular_single_link(
    path: Path,
    *,
    expected_sha256: str | None = None,
    error_code: str,
) -> tuple[bytes, os.stat_result]:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or path.is_symlink():
            raise GateEInputError(error_code)
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
        opened = os.fstat(descriptor)
        if not _same_object(before, opened) or _stable_metadata(before) != _stable_metadata(opened):
            raise GateEInputError(error_code)
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        content = b"".join(chunks)
        after_fd = os.fstat(descriptor)
        after_path = path.lstat()
        if _stable_metadata(opened) != _stable_metadata(after_fd) or _stable_metadata(
            opened
        ) != _stable_metadata(after_path):
            raise GateEInputError(error_code)
    except GateEInputError:
        raise
    except OSError as exc:
        raise GateEInputError(error_code) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise GateEInputError(error_code)
    return content, opened


def _scan_tree(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()

    def visit(directory: Path) -> None:
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise GateEInputError("input_unreadable") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise GateEInputError("input_unreadable") from exc
            if entry.is_symlink():
                raise GateEInputError("unsafe_input_link")
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(relative)
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise GateEInputError("unsafe_input_link")
                files.add(relative)
            else:
                raise GateEInputError("unsafe_input_link")

    visit(root)
    return files, directories


def _expected_directories(
    paths: set[str],
) -> set[str]:
    directories: set[str] = set()
    for relative in paths:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _trusted_manifest(
    release_root: Path,
) -> tuple[Path, ReleaseManifest]:
    root = _safe_existing_directory(
        release_root,
        code="unsafe_release_root",
    )
    manifest_path = root / "release_manifest.json"
    raw, initial = _read_regular_single_link(
        manifest_path,
        expected_sha256=TRUSTED_RELEASE_MANIFEST_SHA256,
        error_code="untrusted_release_manifest",
    )
    try:
        manifest = load_release_manifest(manifest_path)
    except ReleaseVerificationError as exc:
        raise GateEInputError(exc.code) from exc
    current, final = _read_regular_single_link(
        manifest_path,
        expected_sha256=TRUSTED_RELEASE_MANIFEST_SHA256,
        error_code="untrusted_release_manifest",
    )
    if (
        raw != current
        or _stable_metadata(initial) != _stable_metadata(final)
        or len(manifest.input_files) != _EXPECTED_INPUT_COUNT
    ):
        raise GateEInputError("untrusted_release_manifest")
    try:
        trusted_payload = json.loads(raw)
        trusted_input_files = tuple(sorted(trusted_payload["input_files"].items()))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise GateEInputError("untrusted_release_manifest") from exc
    if manifest.input_files != trusted_input_files:
        raise GateEInputError("untrusted_release_manifest")
    return root, manifest


def _verify_input_tree(
    input_root: Path,
    manifest: ReleaseManifest,
    *,
    post_run: bool,
    expected_lock: bool = False,
    allowed_runtime_files: frozenset[str] = frozenset(),
) -> dict[str, os.stat_result]:
    root = _safe_existing_directory(
        input_root,
        code=("post_run_input_set_mismatch" if post_run else "input_file_set_mismatch"),
    )
    expected_hashes = dict(manifest.input_files)
    expected_files = set(expected_hashes)
    if expected_files.intersection(allowed_runtime_files):
        raise GateEInputError("post_run_input_set_mismatch")
    expected_files.update(allowed_runtime_files)
    if expected_lock:
        expected_files.add(_LOCK_RELATIVE.as_posix())
    files, directories = _scan_tree(root)
    if files != expected_files:
        raise GateEInputError(
            "post_run_input_set_mismatch" if post_run else "input_file_set_mismatch"
        )
    if directories != _expected_directories(expected_files):
        raise GateEInputError("input_directory_set_mismatch")

    metadata_by_path: dict[str, os.stat_result] = {}
    for relative, expected_digest in manifest.input_files:
        _content, metadata = _read_regular_single_link(
            root / relative,
            expected_sha256=expected_digest,
            error_code="input_hash_mismatch",
        )
        metadata_by_path[relative] = metadata
    if expected_lock:
        lock = root / _LOCK_RELATIVE.as_posix()
        content, metadata = _read_regular_single_link(
            lock,
            expected_sha256=EMPTY_SHA256,
            error_code="unexpected_post_run_lock",
        )
        if content != b"" or metadata.st_size != 0:
            raise GateEInputError("unexpected_post_run_lock")
    return metadata_by_path


def verify_gate_e_release_inputs(
    release_root: Path,
) -> tuple[Path, ...]:
    """Verify the trusted manifest and exact 25-file directory closure."""
    root, manifest = _trusted_manifest(release_root)
    try:
        verified = verify_release_inputs(manifest, root)
    except ReleaseVerificationError as exc:
        raise GateEInputError(exc.code) from exc
    _verify_input_tree(root / "inputs", manifest, post_run=False)
    return verified


def _inspect_lock(path: Path) -> tuple[bytes, os.stat_result]:
    try:
        content, metadata = _read_regular_single_link(
            path,
            expected_sha256=EMPTY_SHA256,
            error_code="unexpected_lock_file",
        )
    except GateEInputError as exc:
        raise GateEInputError("unexpected_lock_file") from exc
    if content != b"" or metadata.st_size != 0:
        raise GateEInputError("unexpected_lock_file")
    return content, metadata


def _utc_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")


def _recorded_at(value: datetime | None) -> str:
    current = datetime.now(UTC) if value is None else value
    if (
        type(current) is not datetime
        or current.tzinfo is None
        or current.utcoffset() != UTC.utcoffset(None)
    ):
        raise GateEInputError("invalid_recorded_at")
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _named_metadata(
    directory_descriptor: int,
    name: str,
    *,
    code: str,
) -> os.stat_result:
    try:
        return os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise GateEInputError(code) from exc


def _unlock_file(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass


def quarantine_manifest_lock(
    release_root: Path,
    quarantine_path: Path,
    *,
    recorded_at_utc: datetime | None = None,
) -> GateEDeviation:
    """Move the one exact stale lock without overwriting any target."""
    root, manifest = _trusted_manifest(release_root)
    source = root / "inputs" / _LOCK_RELATIVE.as_posix()
    _inspect_lock(source)
    _verify_input_tree(
        root / "inputs",
        manifest,
        post_run=True,
        expected_lock=True,
    )

    project_root = root.parent.parent
    expected_destination = project_root / _QUARANTINE_ARGUMENT
    if not isinstance(quarantine_path, Path):
        raise TypeError("quarantine_path must be a Path")
    destination = Path(os.path.abspath(quarantine_path))
    if destination != expected_destination:
        raise GateEInputError("invalid_quarantine_path")
    if os.path.lexists(destination):
        raise GateEInputError("quarantine_destination_conflict")
    recorded = _recorded_at(recorded_at_utc)

    opened: os.stat_result
    final_metadata: os.stat_result
    with ExitStack() as stack:
        project = _open_directory_path(
            project_root,
            code="invalid_quarantine_path",
        )
        stack.callback(os.close, project.descriptor)
        release = _open_directory_at(
            project.descriptor,
            name="release",
            create=False,
            code="invalid_quarantine_path",
        )
        stack.callback(os.close, release.descriptor)
        v01 = _open_directory_at(
            release.descriptor,
            name="v0.1-research",
            create=False,
            code="invalid_quarantine_path",
        )
        stack.callback(os.close, v01.descriptor)
        try:
            root_metadata = root.lstat()
        except OSError as exc:
            raise GateEInputError("invalid_quarantine_path") from exc
        if not _same_object(root_metadata, v01.metadata):
            raise GateEInputError("invalid_quarantine_path")

        inputs = _open_directory_at(
            v01.descriptor,
            name="inputs",
            create=False,
            code="unexpected_lock_file",
        )
        stack.callback(os.close, inputs.descriptor)
        data = _open_directory_at(
            inputs.descriptor,
            name="data",
            create=False,
            code="unexpected_lock_file",
        )
        stack.callback(os.close, data.descriptor)
        manifests = _open_directory_at(
            data.descriptor,
            name="manifests",
            create=False,
            code="unexpected_lock_file",
        )
        stack.callback(os.close, manifests.descriptor)

        gate = _open_directory_at(
            release.descriptor,
            name="v0.2-gate-e",
            create=True,
            code="invalid_quarantine_path",
            conflict_code="quarantine_destination_conflict",
        )
        stack.callback(os.close, gate.descriptor)
        deviations = _open_directory_at(
            gate.descriptor,
            name="deviations",
            create=True,
            code="invalid_quarantine_path",
            conflict_code="quarantine_destination_conflict",
        )
        stack.callback(os.close, deviations.descriptor)
        handles = (
            project,
            release,
            v01,
            inputs,
            data,
            manifests,
            gate,
            deviations,
        )
        _assert_directories_bound(
            handles,
            code="invalid_quarantine_path",
        )

        descriptor: int | None = None
        try:
            descriptor = os.open(
                _LOCK_RELATIVE.name,
                os.O_RDWR | _NOFOLLOW | _CLOEXEC,
                dir_fd=manifests.descriptor,
            )
            stack.callback(os.close, descriptor)
        except OSError as exc:
            raise GateEInputError("unexpected_lock_file") from exc
        try:
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except OSError as exc:
            raise GateEInputError("lock_file_busy") from exc
        stack.callback(_unlock_file, descriptor)

        opened = os.fstat(descriptor)
        content = os.read(descriptor, 1)
        named_source = _named_metadata(
            manifests.descriptor,
            _LOCK_RELATIVE.name,
            code="unexpected_lock_file",
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != 0
            or content != b""
            or not _same_object(opened, named_source)
        ):
            raise GateEInputError("unexpected_lock_file")

        _assert_directories_bound(
            handles,
            code="invalid_quarantine_path",
        )
        try:
            _rename_no_replace(
                manifests.descriptor,
                _LOCK_RELATIVE.name,
                deviations.descriptor,
                destination.name,
            )
        except FileExistsError as exc:
            raise GateEInputError("quarantine_destination_conflict") from exc
        except GateEInputError:
            raise
        except OSError as exc:
            raise GateEInputError("quarantine_move_failed") from exc

        try:
            os.stat(
                _LOCK_RELATIVE.name,
                dir_fd=manifests.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise GateEInputError("quarantine_move_failed") from exc
        else:
            raise GateEInputError("quarantine_move_failed")
        moved_destination = _named_metadata(
            deviations.descriptor,
            destination.name,
            code="quarantine_move_failed",
        )
        moved_opened = os.fstat(descriptor)
        if (
            not _same_object(opened, moved_destination)
            or not _same_object(opened, moved_opened)
            or moved_opened.st_nlink != 1
        ):
            raise GateEInputError("quarantine_move_failed")
        _assert_directories_bound(
            handles,
            code="invalid_quarantine_path",
        )

        try:
            os.fsync(manifests.descriptor)
            os.fsync(deviations.descriptor)
        except OSError as exc:
            raise GateEInputError("quarantine_move_failed") from exc

        final_metadata = _named_metadata(
            deviations.descriptor,
            destination.name,
            code="quarantine_move_failed",
        )
        final_opened = os.fstat(descriptor)
        if (
            not _same_object(opened, final_metadata)
            or not _same_object(opened, final_opened)
            or final_opened.st_nlink != 1
            or final_opened.st_size != 0
        ):
            raise GateEInputError("quarantine_move_failed")
        _assert_directories_bound(
            handles,
            code="invalid_quarantine_path",
        )

    verify_gate_e_release_inputs(root)
    return GateEDeviation(
        original_path=source.relative_to(project_root).as_posix(),
        quarantine_path=destination.relative_to(project_root).as_posix(),
        size=final_metadata.st_size,
        sha256=EMPTY_SHA256,
        device=final_metadata.st_dev,
        inode=final_metadata.st_ino,
        link_count_before=opened.st_nlink,
        link_count_after=final_metadata.st_nlink,
        birth_at_utc=_utc_timestamp(getattr(opened, "st_birthtime", opened.st_ctime)),
        modified_at_utc=_utc_timestamp(opened.st_mtime),
        recorded_at_utc=recorded,
        move_reason="stale_untracked_reader_lock",
        research_semantics="declared_25_input_bytes_unchanged",
    )


def _validated_destination(
    path: Path,
    *,
    release_root: Path,
) -> Path:
    if not isinstance(path, Path):
        raise TypeError("destination must be a Path")
    absolute = Path(os.path.abspath(path))
    if os.path.lexists(absolute):
        raise GateEInputError("copy_destination_conflict")
    parent = _safe_existing_directory(
        absolute.parent,
        code="copy_destination_unsafe",
    )
    if (
        absolute.is_relative_to(release_root)
        or release_root.is_relative_to(absolute)
        or parent.is_relative_to(release_root)
    ):
        raise GateEInputError("copy_destination_overlap")
    return absolute


def _rename_no_replace(
    source_directory_descriptor: int,
    source_name: str,
    destination_directory_descriptor: int,
    destination_name: str,
) -> None:
    source_bytes = os.fsencode(source_name)
    destination_bytes = os.fsencode(destination_name)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            function = library.renameatx_np
        except AttributeError as exc:
            raise GateEInputError("atomic_publish_unavailable") from exc
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            source_directory_descriptor,
            source_bytes,
            destination_directory_descriptor,
            destination_bytes,
            _RENAME_EXCL,
        )
    elif sys.platform.startswith("linux"):
        try:
            function = library.renameat2
        except AttributeError as exc:
            raise GateEInputError("atomic_publish_unavailable") from exc
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            source_directory_descriptor,
            source_bytes,
            destination_directory_descriptor,
            destination_bytes,
            _RENAME_NOREPLACE,
        )
    else:
        raise GateEInputError("atomic_publish_unavailable")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            destination_name,
        )
    if error_number in {
        errno.ENOSYS,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }:
        raise GateEInputError("atomic_publish_unavailable")
    raise OSError(
        error_number,
        os.strerror(error_number),
        destination_name,
    )


def _create_staging_root(
    destination: Path,
    parent: _DirectoryHandle,
) -> tuple[Path, _DirectoryHandle]:
    _assert_directory_bound(
        parent,
        code="copy_destination_unsafe",
    )
    for _attempt in range(128):
        name = f".{destination.name}.gate-e-staging-{secrets.token_hex(16)}"
        try:
            os.mkdir(
                name,
                mode=0o700,
                dir_fd=parent.descriptor,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise GateEInputError("copy_destination_conflict") from exc
        handle: _DirectoryHandle | None = None
        try:
            os.fsync(parent.descriptor)
            handle = _open_directory_at(
                parent.descriptor,
                name=name,
                create=False,
                code="copy_destination_unsafe",
                conflict_code="copy_destination_conflict",
            )
            _assert_directory_bound(
                parent,
                code="copy_destination_unsafe",
            )
        except (OSError, GateEInputError) as exc:
            if handle is not None:
                os.close(handle.descriptor)
            cause_code = (
                exc.cause_code or exc.code
                if isinstance(exc, GateEInputError)
                else "input_copy_failed"
            )
            raise GateEInputError(
                (exc.code if isinstance(exc, GateEInputError) else "input_copy_failed"),
                cause_code=cause_code,
                evidence_name=name,
                publication_state="staging_retained",
            ) from exc
        return destination.parent / name, handle
    raise GateEInputError("copy_destination_conflict")


def _relative_parts(relative: str) -> tuple[str, ...]:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise GateEInputError("untrusted_release_manifest")
    return path.parts


def _copy_verified_file_at(
    source: Path,
    staging: _DirectoryHandle,
    relative: str,
    expected_sha256: str,
) -> None:
    content, _metadata = _read_regular_single_link(
        source,
        expected_sha256=expected_sha256,
        error_code="input_hash_mismatch",
    )
    parts = _relative_parts(relative)
    with ExitStack() as stack:
        current = staging
        for part in parts[:-1]:
            child = _open_directory_at(
                current.descriptor,
                name=part,
                create=True,
                code="copy_destination_unsafe",
                conflict_code="copy_destination_conflict",
            )
            stack.callback(os.close, child.descriptor)
            current = child
        try:
            descriptor = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=current.descriptor,
            )
        except FileExistsError as exc:
            raise GateEInputError("copy_destination_conflict") from exc
        except OSError as exc:
            raise GateEInputError("input_copy_failed") from exc
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise GateEInputError("input_copy_failed")
                view = view[written:]
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
            os.fsync(current.descriptor)
        except GateEInputError:
            raise
        except OSError as exc:
            raise GateEInputError("input_copy_failed") from exc
        finally:
            os.close(descriptor)
    _assert_directory_bound(
        staging,
        code="copy_destination_unsafe",
    )


def _publish_staged_root(
    staging: _DirectoryHandle,
    destination: Path,
    parent: _DirectoryHandle,
) -> os.stat_result:
    if staging.name is None:
        raise GateEInputError("copy_atomic_publish_failed")
    _assert_directory_bound(
        parent,
        code="copy_destination_unsafe",
    )
    _assert_directory_bound(
        staging,
        code="copy_atomic_publish_failed",
    )
    try:
        _rename_no_replace(
            parent.descriptor,
            staging.name,
            parent.descriptor,
            destination.name,
        )
    except FileExistsError as exc:
        raise GateEInputError("copy_destination_conflict") from exc
    except GateEInputError:
        raise
    except OSError as exc:
        raise GateEInputError("copy_atomic_publish_failed") from exc
    try:
        published = os.stat(
            destination.name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        os.fsync(parent.descriptor)
        _assert_directory_bound(
            parent,
            code="copy_partial_publication",
        )
    except (OSError, GateEInputError) as exc:
        raise GateEInputError(
            "copy_partial_publication",
            cause_code="copy_post_publish_verification_failed",
            evidence_name=destination.name,
            publication_state="destination_published_unverified",
        ) from exc
    if not _same_object(staging.metadata, published):
        raise GateEInputError(
            "copy_partial_publication",
            cause_code="copy_post_publish_binding_mismatch",
            evidence_name=destination.name,
            publication_state="destination_published_unverified",
        )
    return published


def stage_gate_e_input_root(
    release_root: Path,
    destination: Path,
) -> GateEInputCopy:
    """Stage one candidate root without creating any later candidate."""
    root, manifest = _trusted_manifest(release_root)
    verify_gate_e_release_inputs(root)
    target = _validated_destination(
        destination,
        release_root=root,
    )
    published = False
    with ExitStack() as stack:
        parent = _open_directory_path(
            target.parent,
            code="copy_destination_unsafe",
        )
        stack.callback(os.close, parent.descriptor)
        staging_path, staging = _create_staging_root(
            target,
            parent,
        )
        stack.callback(os.close, staging.descriptor)
        try:
            for relative, digest in manifest.input_files:
                _copy_verified_file_at(
                    root / "inputs" / relative,
                    staging,
                    relative,
                    digest,
                )
            _assert_directory_bound(
                parent,
                code="copy_destination_unsafe",
            )
            _assert_directory_bound(
                staging,
                code="copy_destination_unsafe",
            )
            source_metadata = _verify_input_tree(
                root / "inputs",
                manifest,
                post_run=False,
            )
            staging_metadata = _verify_input_tree(
                staging_path,
                manifest,
                post_run=False,
            )
            for relative, _digest in manifest.input_files:
                if _same_object(
                    source_metadata[relative],
                    staging_metadata[relative],
                ):
                    raise GateEInputError("copy_inode_not_independent")
            verify_gate_e_release_inputs(root)
            _assert_directory_bound(
                parent,
                code="copy_destination_unsafe",
            )
            _assert_directory_bound(
                staging,
                code="copy_destination_unsafe",
            )
            _publish_staged_root(
                staging,
                target,
                parent,
            )
            published = True
            _verify_input_tree(
                target,
                manifest,
                post_run=False,
            )
            _assert_directory_bound(
                parent,
                code="copy_post_publish_parent_binding_failed",
            )
        except GateEInputError as original:
            if published or original.code == "copy_partial_publication":
                raise GateEInputError(
                    "copy_partial_publication",
                    cause_code=(
                        original.cause_code
                        or (
                            original.code
                            if original.code != "copy_partial_publication"
                            else "copy_post_publish_validation_failed"
                        )
                    ),
                    evidence_name=target.name,
                    publication_state=("destination_published_unverified"),
                ) from original
            raise GateEInputError(
                original.code,
                cause_code=original.cause_code or original.code,
                evidence_name=staging.name,
                publication_state="staging_retained",
            ) from original
    return GateEInputCopy(
        manifest_sha256=TRUSTED_RELEASE_MANIFEST_SHA256,
        file_count=len(manifest.input_files),
        destination_root=target,
    )


def verify_gate_e_input_roots_independent(
    release_root: Path,
    destination_a: Path,
    destination_b: Path,
) -> GateEInputCopies:
    """Verify exact A/B roots and three-way inode independence."""
    root, manifest = _trusted_manifest(release_root)
    verify_gate_e_release_inputs(root)
    first = Path(os.path.abspath(destination_a))
    second = Path(os.path.abspath(destination_b))
    if (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
        or first.is_relative_to(root)
        or second.is_relative_to(root)
        or root.is_relative_to(first)
        or root.is_relative_to(second)
    ):
        raise GateEInputError("copy_destination_overlap")
    source_metadata = _verify_input_tree(
        root / "inputs",
        manifest,
        post_run=False,
    )
    first_metadata = _verify_input_tree(
        first,
        manifest,
        post_run=False,
    )
    second_metadata = _verify_input_tree(
        second,
        manifest,
        post_run=False,
    )
    for relative, _digest in manifest.input_files:
        identities = {
            (
                source_metadata[relative].st_dev,
                source_metadata[relative].st_ino,
            ),
            (
                first_metadata[relative].st_dev,
                first_metadata[relative].st_ino,
            ),
            (
                second_metadata[relative].st_dev,
                second_metadata[relative].st_ino,
            ),
        }
        if len(identities) != 3:
            raise GateEInputError("copy_inode_not_independent")
    return GateEInputCopies(
        manifest_sha256=TRUSTED_RELEASE_MANIFEST_SHA256,
        file_count=len(manifest.input_files),
        destination_roots=(first, second),
    )


def verify_and_copy_gate_e_inputs(
    release_root: Path,
    destination_a: Path,
    destination_b: Path,
) -> GateEInputCopies:
    """Compatibility wrapper; formal E1 stages A and B separately."""
    root, _manifest = _trusted_manifest(release_root)
    verify_gate_e_release_inputs(root)
    first_candidate = Path(os.path.abspath(destination_a))
    second_candidate = Path(os.path.abspath(destination_b))
    if (
        first_candidate == second_candidate
        or first_candidate.is_relative_to(second_candidate)
        or second_candidate.is_relative_to(first_candidate)
    ):
        raise GateEInputError("copy_destination_overlap")
    if os.path.lexists(first_candidate) or os.path.lexists(second_candidate):
        raise GateEInputError("copy_destination_conflict")

    stage_gate_e_input_root(root, first_candidate)
    try:
        stage_gate_e_input_root(root, second_candidate)
    except GateEInputError as original:
        raise GateEInputError(
            "copy_partial_publication",
            cause_code=original.cause_code or original.code,
            evidence_name=(original.evidence_name or first_candidate.name),
            publication_state=("candidate_a_published_candidate_b_incomplete"),
        ) from original
    return verify_gate_e_input_roots_independent(
        root,
        first_candidate,
        second_candidate,
    )


def verify_post_run_input_root(
    release_root: Path,
    input_root: Path,
    *,
    expected_run_id: str | None = None,
) -> GateEPostRunInputs:
    """Verify the frozen inputs plus, when requested, the fixed run closure.

    Without ``expected_run_id`` the root must contain only the 25 frozen input
    files.  Runtime mode admits only Gate E's fixed config, lock, and exact
    artifact filenames for that run; callers cannot supply an arbitrary
    allowlist.
    """
    root, manifest = _trusted_manifest(release_root)
    verify_gate_e_release_inputs(root)
    runtime_files: frozenset[str] = frozenset()
    if expected_run_id is not None:
        if (
            type(expected_run_id) is not str
            or _RUN_ID_RE.fullmatch(expected_run_id) is None
        ):
            raise GateEInputError("invalid_post_run_id")
        runtime_files = frozenset(
            {
                _RUNTIME_CONFIG_RELATIVE.as_posix(),
                (
                    "outputs/portfolios/"
                    f".{expected_run_id}.lock"
                ),
                *(
                    "outputs/portfolios/"
                    f"{expected_run_id}/{name}"
                    for name in PORTFOLIO_ARTIFACT_FILES
                ),
            }
        )
    _verify_input_tree(
        input_root,
        manifest,
        post_run=True,
        allowed_runtime_files=runtime_files,
    )
    return GateEPostRunInputs(
        manifest_sha256=TRUSTED_RELEASE_MANIFEST_SHA256,
        file_count=len(manifest.input_files),
    )


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise GateEInputError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="aquant-gate-e-inputs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    quarantine = subparsers.add_parser("quarantine")
    quarantine.add_argument("--release-root", required=True)
    quarantine.add_argument("--destination", required=True)
    return parser


def _write_json(stream, payload: dict[str, object]) -> None:
    stream.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Quarantine only the approved release lock with sanitized evidence."""
    try:
        args = _parser().parse_args(argv)
        if (
            args.command != "quarantine"
            or args.release_root != _RELEASE_ROOT_ARGUMENT.as_posix()
            or args.destination != _QUARANTINE_ARGUMENT.as_posix()
        ):
            raise GateEInputError("invalid_quarantine_path")
        deviation = quarantine_manifest_lock(
            Path(args.release_root),
            Path(args.destination),
        )
    except (GateEInputError, TypeError, ValueError, OSError) as exc:
        _write_json(
            sys.stderr,
            {
                "error_code": getattr(
                    exc,
                    "code",
                    "operation_failed",
                ),
                "status": "error",
            },
        )
        return 1
    _write_json(
        sys.stdout,
        {
            "device": deviation.device,
            "birth_at_utc": deviation.birth_at_utc,
            "inode": deviation.inode,
            "link_count_after": deviation.link_count_after,
            "link_count_before": deviation.link_count_before,
            "modified_at_utc": deviation.modified_at_utc,
            "move_reason": deviation.move_reason,
            "original_path": deviation.original_path,
            "quarantine_path": deviation.quarantine_path,
            "recorded_at_utc": deviation.recorded_at_utc,
            "research_semantics": deviation.research_semantics,
            "sha256": deviation.sha256,
            "size": deviation.size,
            "status": "quarantined",
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
