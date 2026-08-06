"""Sealed wheel and wheelhouse evidence for the Gate E replay."""

from __future__ import annotations

import configparser
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath

from aquant.gate_e.inputs import GateEInputCopy, stage_gate_e_input_root

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}")
_DISTRIBUTION_NAME_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?"
)
_NORMALIZE_NAME_RE = re.compile(r"[-_.]+")
_PROJECT_NAME = "a-share-quant"
_PROJECT_VERSION = "0.2.0"
_PORTFOLIO_ENTRY = "aquant-portfolio = aquant.portfolio_cli:main"
_WHEELHOUSE_MANIFEST_SCHEMA = "1.0"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_MAX_WHEEL_BYTES = 1024 * 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_READ_ONLY_WRITE_OPERATION = "file-write*"
_VERIFICATION_MODE_WRITE_OPERATIONS = (
    "file-write-acl",
    "file-write-create",
    "file-write-data",
    "file-write-flags",
    "file-write-owner",
    "file-write-setugid",
    "file-write-unlink",
    "file-write-xattr",
)


class GateEEnvironmentError(RuntimeError):
    """Stable, sanitized failure for Gate E release environments."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class WheelEvidence:
    """Verified identity of the disposable or formal project wheel."""

    path: Path
    size: int
    sha256: str
    distribution_version: str
    portfolio_cli_present: bool
    entry_point: str


@dataclass(frozen=True)
class WheelhouseEntry:
    """One verified dependency wheel."""

    filename: str
    normalized_name: str
    version: str
    size: int
    sha256: str


@dataclass(frozen=True)
class WheelhouseEvidence:
    """Verified exact wheelhouse closure."""

    root: Path
    entries: tuple[WheelhouseEntry, ...]
    manifest_sha256: str | None
    install_lock_sha256: str | None = None


@dataclass(frozen=True)
class GateEEnvironmentLayout:
    """One isolated mutable root used by exactly one Gate E replay."""

    root: Path
    home: Path
    xdg_cache: Path
    uv_cache: Path
    venv: Path
    python: Path
    project_root: Path
    input_root: Path
    output_root: Path
    config_path: Path
    repository_root: Path
    base_python: Path


@dataclass(frozen=True)
class InstalledEnvironmentEvidence:
    """Installed-package and import evidence from one isolated venv."""

    project_version: str
    aquant_file: Path
    sys_path: tuple[str, ...]
    packages: tuple[tuple[str, str], ...]
    portfolio_cli: Path
    gate_e_cli: Path | None


def _normalized_name(value: str) -> str:
    return _NORMALIZE_NAME_RE.sub("-", value).lower()


def _regular_single_link(path: Path, *, code: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GateEEnvironmentError(code, "required release file is missing") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or path.is_symlink()
    ):
        raise GateEEnvironmentError(
            code,
            "release files must be regular single-link files",
        )
    return metadata


def _resolved_runtime_executable(value: str, *, code: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise GateEEnvironmentError(
            code,
            "runtime executable must be an absolute path",
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GateEEnvironmentError(
            code,
            "runtime executable is unavailable",
        ) from exc
    metadata = _regular_single_link(resolved, code=code)
    if not metadata.st_mode & 0o111:
        raise GateEEnvironmentError(
            code,
            "runtime executable is not executable",
        )
    return resolved


def canonical_python_executable() -> Path:
    """Return the real base CPython 3.11.15 executable for this controller."""
    raw = getattr(sys, "_base_executable", None) or sys.executable
    if type(raw) is not str or not raw:
        raise GateEEnvironmentError(
            "unsafe_base_python",
            "base Python executable is unavailable",
        )
    executable = _resolved_runtime_executable(
        raw,
        code="unsafe_base_python",
    )
    try:
        completed = subprocess.run(
            [
                str(executable),
                "-c",
                (
                    "import platform,sys;"
                    "print(platform.python_implementation());"
                    "print('.'.join(str(value) for value in "
                    "sys.version_info[:3]))"
                ),
            ],
            cwd=executable.parent,
            env={
                "HOME": "/private/var/empty",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": _SYSTEM_PATH,
                "PYTHONNOUSERSITE": "1",
                "TZ": "Asia/Shanghai",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateEEnvironmentError(
            "unsafe_base_python",
            "base Python identity could not be verified",
        ) from exc
    if (
        completed.returncode != 0
        or completed.stdout.splitlines() != ["CPython", "3.11.15"]
    ):
        raise GateEEnvironmentError(
            "unsafe_base_python",
            "Gate E requires CPython 3.11.15",
        )
    return executable


def canonical_uv_executable() -> Path:
    """Return the real uv executable discovered from the controlled PATH."""
    discovered = shutil.which("uv")
    if discovered is None:
        raise GateEEnvironmentError(
            "unsafe_uv_executable",
            "uv executable is unavailable",
        )
    executable = _resolved_runtime_executable(
        discovered,
        code="unsafe_uv_executable",
    )
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            cwd=executable.parent,
            env={
                "HOME": "/private/var/empty",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": _SYSTEM_PATH,
                "TZ": "Asia/Shanghai",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateEEnvironmentError(
            "unsafe_uv_executable",
            "uv identity could not be verified",
        ) from exc
    fields = completed.stdout.strip().split()
    if (
        completed.returncode != 0
        or len(fields) < 2
        or fields[0] != "uv"
        or _VERSION_RE.fullmatch(fields[1]) is None
    ):
        raise GateEEnvironmentError(
            "unsafe_uv_executable",
            "uv identity could not be verified",
        )
    return executable


def _read_regular_single_link(
    path: Path,
    *,
    code: str,
    maximum_bytes: int,
) -> tuple[bytes, os.stat_result]:
    initial = _regular_single_link(path, code=code)
    if initial.st_size > maximum_bytes:
        raise GateEEnvironmentError(code, "release file exceeds its size limit")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (initial.st_dev, initial.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise GateEEnvironmentError(
                code,
                "release file identity changed during inspection",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise GateEEnvironmentError(
                    code,
                    "release file exceeds its size limit",
                )
        final = os.fstat(descriptor)
        path_metadata = path.lstat()
        if (
            not stat.S_ISREG(final.st_mode)
            or not stat.S_ISREG(path_metadata.st_mode)
            or final.st_nlink != 1
            or path_metadata.st_nlink != 1
            or (opened.st_dev, opened.st_ino)
            != (final.st_dev, final.st_ino)
            or (opened.st_dev, opened.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
            or opened.st_size != final.st_size
            or opened.st_mtime_ns != final.st_mtime_ns
            or opened.st_ctime_ns != final.st_ctime_ns
            or total != final.st_size
        ):
            raise GateEEnvironmentError(
                code,
                "release file changed during inspection",
            )
        return b"".join(chunks), final
    except GateEEnvironmentError:
        raise
    except OSError as exc:
        raise GateEEnvironmentError(
            code,
            "release file cannot be inspected safely",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _safe_zip_names(archive: zipfile.ZipFile) -> tuple[str, ...]:
    infos = archive.infolist()
    names = tuple(info.filename for info in infos)
    if len(names) != len(set(names)):
        raise GateEEnvironmentError(
            "wheel_structure_invalid",
            "wheel ZIP members must be unique",
        )
    for info in infos:
        path = PurePosixPath(info.filename)
        mode = (info.external_attr >> 16) & 0xFFFF
        if (
            not info.filename
            or "\\" in info.filename
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or stat.S_ISLNK(mode)
        ):
            raise GateEEnvironmentError(
                "wheel_structure_invalid",
                "wheel ZIP members must use safe regular paths",
            )
    if archive.testzip() is not None:
        raise GateEEnvironmentError(
            "wheel_structure_invalid",
            "wheel ZIP integrity verification failed",
        )
    return names


def _wheel_identity(path: Path) -> tuple[str, str, int, str, zipfile.ZipFile]:
    if path.suffix != ".whl":
        raise GateEEnvironmentError("unsafe_wheel", "wheel filename is invalid")
    content, metadata = _read_regular_single_link(
        path,
        code="unsafe_wheel",
        maximum_bytes=_MAX_WHEEL_BYTES,
    )
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        names = _safe_zip_names(archive)
        metadata_names = tuple(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        if len(metadata_names) != 1:
            raise GateEEnvironmentError(
                "wheel_structure_invalid",
                "wheel must contain one distribution metadata file",
            )
        message = Parser().parsestr(
            archive.read(metadata_names[0]).decode("utf-8")
        )
        distribution_names = message.get_all("Name", [])
        versions = message.get_all("Version", [])
        if (
            len(distribution_names) != 1
            or len(versions) != 1
            or type(distribution_names[0]) is not str
            or type(versions[0]) is not str
            or _DISTRIBUTION_NAME_RE.fullmatch(distribution_names[0]) is None
            or _VERSION_RE.fullmatch(versions[0]) is None
        ):
            raise GateEEnvironmentError(
                "wheel_structure_invalid",
                "wheel distribution metadata is invalid",
            )
        distribution_name = distribution_names[0]
        version = versions[0]
        normalized = _normalized_name(distribution_name)
        filename_parts = path.name.split("-")
        if (
            len(filename_parts) < 5
            or _normalized_name(filename_parts[0]) != normalized
            or filename_parts[1] != version
        ):
            raise GateEEnvironmentError(
                "wheel_identity_mismatch",
                "wheel filename and metadata identities differ",
            )
        digest = hashlib.sha256(content).hexdigest()
        return normalized, version, metadata.st_size, digest, archive
    except GateEEnvironmentError:
        if "archive" in locals():
            archive.close()
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        if "archive" in locals():
            archive.close()
        raise GateEEnvironmentError(
            "wheel_structure_invalid",
            "wheel cannot be inspected safely",
        ) from exc


def _build_environment(home: Path, cache: Path) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PIP_CONFIG_FILE": "/dev/null",
        "PYTHONNOUSERSITE": "1",
        "TZ": "Asia/Shanghai",
        "UV_CACHE_DIR": str(cache),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    return environment


def build_project_wheel(project_root: Path, dist: Path) -> Path:
    """Build one disposable offline v0.2 wheel from the current source tree."""
    if not isinstance(project_root, Path) or not isinstance(dist, Path):
        raise TypeError("project_root and dist must be Path objects")
    try:
        root_metadata = project_root.lstat()
    except OSError as exc:
        raise GateEEnvironmentError(
            "wheel_build_failed",
            "project root cannot be inspected",
        ) from exc
    if not stat.S_ISDIR(root_metadata.st_mode) or project_root.is_symlink():
        raise GateEEnvironmentError(
            "wheel_build_failed",
            "project root must be a safe directory",
        )
    try:
        dist.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        if dist.is_symlink() or not dist.is_dir() or any(dist.iterdir()):
            raise GateEEnvironmentError(
                "wheel_output_conflict",
                "wheel output directory must be empty",
            ) from None
    except OSError as exc:
        raise GateEEnvironmentError(
            "wheel_build_failed",
            "wheel output directory cannot be created",
        ) from exc
    uv = shutil.which("uv")
    if uv is None:
        raise GateEEnvironmentError(
            "wheel_build_failed",
            "uv executable is unavailable",
        )
    cache = Path(
        os.environ.get(
            "UV_CACHE_DIR",
            str(Path.home() / ".cache" / "uv"),
        )
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix=".gate-e-wheel-build-",
            dir=dist.parent,
        ) as temporary:
            home = Path(temporary)
            completed = subprocess.run(
                [
                    uv,
                    "build",
                    "--wheel",
                    "--no-sources",
                    "--out-dir",
                    str(dist),
                ],
                cwd=project_root,
                env=_build_environment(home, cache),
                capture_output=True,
                text=True,
                check=False,
            )
    except OSError as exc:
        raise GateEEnvironmentError(
            "wheel_build_failed",
            "the Gate E wheel could not be built",
        ) from exc
    if completed.returncode != 0:
        raise GateEEnvironmentError(
            "wheel_build_failed",
            "the Gate E wheel could not be built",
        )
    matches = tuple(sorted(dist.glob("a_share_quant-0.2.0-*.whl")))
    children = tuple(sorted(dist.iterdir(), key=lambda item: item.name))
    uv_ignore = dist / ".gitignore"
    expected_children = (
        tuple(sorted((*matches, uv_ignore), key=lambda item: item.name))
        if uv_ignore.exists()
        else matches
    )
    if (
        len(matches) != 1
        or children != expected_children
        or (
            uv_ignore.exists()
            and (
                _regular_single_link(
                    uv_ignore,
                    code="wheel_output_conflict",
                ).st_size
                != 1
                or uv_ignore.read_bytes() != b"*"
            )
        )
    ):
        raise GateEEnvironmentError(
            "wheel_identity_mismatch",
            "exactly one v0.2.0 wheel is required",
        )
    return matches[0]


def inspect_project_wheel(path: Path) -> WheelEvidence:
    """Verify project distribution metadata, code and console entry point."""
    normalized, version, size, digest, archive = _wheel_identity(path)
    try:
        if normalized != _PROJECT_NAME or version != _PROJECT_VERSION:
            raise GateEEnvironmentError(
                "wheel_identity_mismatch",
                "project wheel identity is not a-share-quant 0.2.0",
            )
        names = tuple(info.filename for info in archive.infolist())
        entry_names = tuple(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        if len(entry_names) != 1:
            raise GateEEnvironmentError(
                "wheel_entry_point_missing",
                "project wheel must contain one entry-point file",
            )
        parser = configparser.ConfigParser(
            interpolation=None,
            strict=True,
        )
        parser.optionxform = str
        parser.read_string(archive.read(entry_names[0]).decode("utf-8"))
        value = parser.get(
            "console_scripts",
            "aquant-portfolio",
            fallback=None,
        )
        if value != "aquant.portfolio_cli:main":
            raise GateEEnvironmentError(
                "wheel_entry_point_missing",
                "project wheel is missing the portfolio CLI",
            )
        portfolio_present = "aquant/portfolio_cli.py" in names
        if not portfolio_present:
            raise GateEEnvironmentError(
                "wheel_entry_point_missing",
                "project wheel is missing the portfolio implementation",
            )
        return WheelEvidence(
            path=path,
            size=size,
            sha256=digest,
            distribution_version=version,
            portfolio_cli_present=True,
            entry_point=_PORTFOLIO_ENTRY,
        )
    except (
        configparser.Error,
        KeyError,
        UnicodeError,
    ) as exc:
        raise GateEEnvironmentError(
            "wheel_entry_point_missing",
            "project wheel entry points are invalid",
        ) from exc
    finally:
        archive.close()


def _expected_requirements(
    values: Mapping[str, str],
) -> dict[str, str]:
    if not isinstance(values, Mapping) or not values:
        raise GateEEnvironmentError(
            "invalid_wheelhouse_contract",
            "expected requirements must be a non-empty mapping",
        )
    result: dict[str, str] = {}
    for raw_name, version in values.items():
        if (
            type(raw_name) is not str
            or type(version) is not str
            or _DISTRIBUTION_NAME_RE.fullmatch(raw_name) is None
            or _VERSION_RE.fullmatch(version) is None
        ):
            raise GateEEnvironmentError(
                "invalid_wheelhouse_contract",
                "expected wheelhouse requirement is invalid",
            )
        name = _normalized_name(raw_name)
        if name in result and result[name] != version:
            raise GateEEnvironmentError(
                "invalid_wheelhouse_contract",
                "expected wheelhouse requirements conflict",
            )
        result[name] = version
    return result


def _collect_wheelhouse(root: Path) -> tuple[WheelhouseEntry, ...]:
    try:
        metadata = root.lstat()
        children = tuple(sorted(root.iterdir(), key=lambda item: item.name))
    except OSError as exc:
        raise GateEEnvironmentError(
            "unsafe_wheelhouse",
            "wheelhouse cannot be inspected",
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
        raise GateEEnvironmentError(
            "unsafe_wheelhouse",
            "wheelhouse must be a safe directory",
        )
    entries: list[WheelhouseEntry] = []
    seen: set[tuple[str, str]] = set()
    for path in children:
        if path.suffix != ".whl":
            raise GateEEnvironmentError(
                "wheelhouse_unexpected_file",
                "wheelhouse contains a non-wheel file",
            )
        normalized, version, size, digest, archive = _wheel_identity(path)
        archive.close()
        identity = (normalized, version)
        if identity in seen:
            raise GateEEnvironmentError(
                "wheelhouse_duplicate",
                "wheelhouse contains a duplicate distribution version",
            )
        seen.add(identity)
        entries.append(
            WheelhouseEntry(
                filename=path.name,
                normalized_name=normalized,
                version=version,
                size=size,
                sha256=digest,
            )
        )
    return tuple(entries)


def _verify_exact_requirements(
    entries: tuple[WheelhouseEntry, ...],
    expected_requirements: Mapping[str, str],
) -> None:
    expected = _expected_requirements(expected_requirements)
    actual = {entry.normalized_name: entry.version for entry in entries}
    if any(actual.get(name) != version for name, version in expected.items()):
        raise GateEEnvironmentError(
            "wheelhouse_incomplete",
            "wheelhouse is missing a locked dependency",
        )
    if set(actual) != set(expected):
        raise GateEEnvironmentError(
            "wheelhouse_unexpected_dependency",
            "wheelhouse contains an unlocked dependency",
        )


def _wheelhouse_manifest_payload(
    entries: tuple[WheelhouseEntry, ...],
) -> dict[str, object]:
    return {
        "schema_version": _WHEELHOUSE_MANIFEST_SCHEMA,
        "wheels": [
            {
                "filename": entry.filename,
                "normalized_name": entry.normalized_name,
                "sha256": entry.sha256,
                "size": entry.size,
                "version": entry.version,
            }
            for entry in sorted(entries, key=lambda item: item.filename)
        ],
    }


def _canonical_json_bytes(payload: object) -> bytes:
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise GateEEnvironmentError(
            "wheelhouse_manifest_invalid",
            "wheelhouse manifest cannot be serialized canonically",
        ) from exc
    return f"{serialized}\n".encode()


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GateEEnvironmentError(
                "wheelhouse_manifest_invalid",
                "wheelhouse manifest contains duplicate keys",
            )
        result[key] = value
    return result


def _parse_wheelhouse_manifest(content: bytes) -> dict[str, object]:
    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except GateEEnvironmentError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GateEEnvironmentError(
            "wheelhouse_manifest_invalid",
            "wheelhouse manifest JSON is invalid",
        ) from exc
    if (
        type(payload) is not dict
        or set(payload) != {"schema_version", "wheels"}
        or payload["schema_version"] != _WHEELHOUSE_MANIFEST_SCHEMA
        or type(payload["wheels"]) is not list
        or _canonical_json_bytes(payload) != content
    ):
        raise GateEEnvironmentError(
            "wheelhouse_manifest_invalid",
            "wheelhouse manifest is not canonical",
        )
    for item in payload["wheels"]:
        if (
            type(item) is not dict
            or set(item)
            != {
                "filename",
                "normalized_name",
                "sha256",
                "size",
                "version",
            }
            or type(item["filename"]) is not str
            or type(item["normalized_name"]) is not str
            or type(item["sha256"]) is not str
            or _HASH_RE.fullmatch(item["sha256"]) is None
            or type(item["size"]) is not int
            or item["size"] < 1
            or type(item["version"]) is not str
            or _VERSION_RE.fullmatch(item["version"]) is None
            or Path(item["filename"]).name != item["filename"]
            or not item["filename"].endswith(".whl")
            or _normalized_name(item["normalized_name"])
            != item["normalized_name"]
        ):
            raise GateEEnvironmentError(
                "wheelhouse_manifest_invalid",
                "wheelhouse manifest entry is invalid",
            )
    return payload


def wheelhouse_requirements_from_manifest(
    manifest: Path,
) -> dict[str, str]:
    """Return the exact distribution/version mapping from a sealed manifest."""
    if not isinstance(manifest, Path):
        raise TypeError("manifest must be a Path")
    content, _metadata = _read_regular_single_link(
        manifest,
        code="unsafe_wheelhouse_manifest",
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )
    payload = _parse_wheelhouse_manifest(content)
    requirements = {
        item["normalized_name"]: item["version"]
        for item in payload["wheels"]
    }
    if len(requirements) != len(payload["wheels"]):
        raise GateEEnvironmentError(
            "wheelhouse_manifest_invalid",
            "wheelhouse manifest contains duplicate distributions",
        )
    return requirements


def _control_outside_wheelhouse(
    wheelhouse: Path,
    control: Path,
    *,
    code: str,
    label: str,
) -> None:
    try:
        root = wheelhouse.resolve(strict=True)
        parent = control.parent.resolve(strict=True)
        parent_metadata = control.parent.lstat()
    except OSError as exc:
        raise GateEEnvironmentError(
            code,
            f"wheelhouse {label} parent is unsafe",
        ) from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or control.parent.is_symlink()
        or parent == root
        or root in parent.parents
        or control.name in {"", ".", ".."}
    ):
        raise GateEEnvironmentError(
            code,
            f"wheelhouse {label} must be outside the wheelhouse",
        )


def _manifest_outside_wheelhouse(wheelhouse: Path, manifest: Path) -> None:
    _control_outside_wheelhouse(
        wheelhouse,
        manifest,
        code="unsafe_wheelhouse_manifest",
        label="manifest",
    )


def _write_immutable_control(
    target: Path,
    content: bytes,
    *,
    unsafe_code: str,
    conflict_code: str,
    failure_code: str,
    label: str,
) -> None:
    parent_descriptor = -1
    temporary_descriptor = -1
    temporary_name = (
        f".{target.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    )
    temporary_created = False
    try:
        parent_descriptor = os.open(
            target.parent,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
        )
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_created = True
        view = memoryview(content)
        while view:
            written = os.write(temporary_descriptor, view)
            if written <= 0:
                raise GateEEnvironmentError(
                    failure_code,
                    f"wheelhouse {label} could not be written",
                )
            view = view[written:]
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        os.link(
            temporary_name,
            target.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        temporary_created = False
        os.fsync(parent_descriptor)
    except FileExistsError as exc:
        raise GateEEnvironmentError(
            conflict_code,
            f"wheelhouse {label} already exists",
        ) from exc
    except GateEEnvironmentError:
        raise
    except OSError as exc:
        raise GateEEnvironmentError(
            failure_code,
            f"wheelhouse {label} could not be published",
        ) from exc
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_created and parent_descriptor >= 0:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except OSError:
                pass
        if parent_descriptor >= 0:
            os.close(parent_descriptor)

    written, _metadata = _read_regular_single_link(
        target,
        code=unsafe_code,
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )
    if written != content:
        raise GateEEnvironmentError(
            failure_code,
            f"published wheelhouse {label} differs from intended bytes",
        )


def write_wheelhouse_manifest(
    wheelhouse: Path,
    manifest: Path,
    *,
    expected_requirements: Mapping[str, str],
) -> Path:
    """Write one immutable, canonical manifest outside the wheelhouse."""
    if not isinstance(wheelhouse, Path) or not isinstance(manifest, Path):
        raise TypeError("wheelhouse and manifest must be Path objects")
    _manifest_outside_wheelhouse(wheelhouse, manifest)
    entries = _collect_wheelhouse(wheelhouse)
    _verify_exact_requirements(entries, expected_requirements)
    content = _canonical_json_bytes(_wheelhouse_manifest_payload(entries))
    _write_immutable_control(
        manifest,
        content,
        unsafe_code="unsafe_wheelhouse_manifest",
        conflict_code="wheelhouse_manifest_conflict",
        failure_code="wheelhouse_manifest_write_failed",
        label="manifest",
    )
    return manifest


def _install_lock_bytes(entries: tuple[WheelhouseEntry, ...]) -> bytes:
    lines = [
        (
            f"{entry.normalized_name}=={entry.version} "
            f"--hash=sha256:{entry.sha256}\n"
        )
        for entry in sorted(
            entries,
            key=lambda item: (item.normalized_name, item.version),
        )
    ]
    return "".join(lines).encode()


def write_wheelhouse_install_lock(
    wheelhouse: Path,
    install_lock: Path,
    *,
    expected_requirements: Mapping[str, str],
    manifest: Path,
) -> Path:
    """Write the platform wheel hashes used by strict offline installation."""
    if (
        not isinstance(wheelhouse, Path)
        or not isinstance(install_lock, Path)
        or not isinstance(manifest, Path)
    ):
        raise TypeError(
            "wheelhouse, install_lock and manifest must be Path objects"
        )
    _control_outside_wheelhouse(
        wheelhouse,
        install_lock,
        code="unsafe_wheelhouse_install_lock",
        label="install lock",
    )
    evidence = verify_wheelhouse(
        wheelhouse,
        expected_requirements=expected_requirements,
        manifest=manifest,
    )
    _write_immutable_control(
        install_lock,
        _install_lock_bytes(evidence.entries),
        unsafe_code="unsafe_wheelhouse_install_lock",
        conflict_code="wheelhouse_install_lock_conflict",
        failure_code="wheelhouse_install_lock_write_failed",
        label="install lock",
    )
    return install_lock


def verify_wheelhouse(
    wheelhouse: Path,
    *,
    expected_requirements: Mapping[str, str],
    manifest: Path | None = None,
    install_lock: Path | None = None,
) -> WheelhouseEvidence:
    """Verify exact dependency identities in one wheel-only directory."""
    if (
        not isinstance(wheelhouse, Path)
        or (manifest is not None and not isinstance(manifest, Path))
        or (
            install_lock is not None
            and not isinstance(install_lock, Path)
        )
    ):
        raise TypeError(
            "wheelhouse, manifest and install_lock must be Path objects"
        )
    entries = _collect_wheelhouse(wheelhouse)
    _verify_exact_requirements(entries, expected_requirements)
    manifest_sha256: str | None = None
    if manifest is not None:
        _manifest_outside_wheelhouse(wheelhouse, manifest)
        content, _metadata = _read_regular_single_link(
            manifest,
            code="unsafe_wheelhouse_manifest",
            maximum_bytes=_MAX_MANIFEST_BYTES,
        )
        payload = _parse_wheelhouse_manifest(content)
        expected_payload = _wheelhouse_manifest_payload(entries)
        if payload != expected_payload:
            raise GateEEnvironmentError(
                "wheelhouse_manifest_mismatch",
                "wheelhouse differs from its sealed manifest",
            )
        manifest_sha256 = hashlib.sha256(content).hexdigest()
    install_lock_sha256: str | None = None
    if install_lock is not None:
        if manifest is None:
            raise GateEEnvironmentError(
                "wheelhouse_install_lock_unbound",
                "wheelhouse install lock requires a verified manifest",
            )
        _control_outside_wheelhouse(
            wheelhouse,
            install_lock,
            code="unsafe_wheelhouse_install_lock",
            label="install lock",
        )
        lock_content, _metadata = _read_regular_single_link(
            install_lock,
            code="unsafe_wheelhouse_install_lock",
            maximum_bytes=_MAX_MANIFEST_BYTES,
        )
        if lock_content != _install_lock_bytes(entries):
            raise GateEEnvironmentError(
                "wheelhouse_install_lock_mismatch",
                "wheelhouse differs from its platform install lock",
            )
        install_lock_sha256 = hashlib.sha256(lock_content).hexdigest()
    return WheelhouseEvidence(
        root=wheelhouse,
        entries=entries,
        manifest_sha256=manifest_sha256,
        install_lock_sha256=install_lock_sha256,
    )


def _safe_directory(path: Path, *, code: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GateEEnvironmentError(
            code,
            "Gate E environment directory is unavailable",
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise GateEEnvironmentError(
            code,
            "Gate E environment directory is unsafe",
        )
    return metadata


def _validated_bootstrap_read_only_paths(
    read_only_paths: Sequence[Path],
) -> tuple[Path, ...]:
    if isinstance(read_only_paths, (str, bytes)) or not isinstance(
        read_only_paths,
        Sequence,
    ):
        raise GateEEnvironmentError(
            "invalid_read_only_path",
            "Gate E read-only sandbox paths are invalid",
        )
    validated: list[Path] = []
    for path in read_only_paths:
        if not isinstance(path, Path) or not path.is_absolute():
            raise GateEEnvironmentError(
                "invalid_read_only_path",
                "Gate E read-only sandbox path is invalid",
            )
        try:
            resolved = path.resolve(strict=True)
            metadata = path.lstat()
        except OSError as exc:
            raise GateEEnvironmentError(
                "invalid_read_only_path",
                "Gate E read-only sandbox path is unavailable",
            ) from exc
        if (
            path != resolved
            or path.is_symlink()
            or not (
                stat.S_ISREG(metadata.st_mode)
                or stat.S_ISDIR(metadata.st_mode)
            )
        ):
            raise GateEEnvironmentError(
                "invalid_read_only_path",
                "Gate E read-only sandbox path is unsafe",
            )
        validated.append(path)
    return tuple(validated)


def _bootstrap_sandboxed_command(
    command: Sequence[str],
    read_only_paths: Sequence[Path],
) -> list[str]:
    values = list(command)
    if not read_only_paths:
        return values
    profile_lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
    ]
    for path in read_only_paths:
        quoted = json.dumps(str(path), ensure_ascii=False)
        filter_name = "subpath" if path.is_dir() else "literal"
        profile_lines.append(
            f"(deny {_READ_ONLY_WRITE_OPERATION} "
            f"({filter_name} {quoted}))"
        )
    return [
        "/usr/bin/sandbox-exec",
        "-p",
        "".join(profile_lines),
        *values,
    ]


def make_environment_layout(
    root: Path,
    *,
    repository_root: Path,
    base_python: Path | None = None,
    read_only_paths: Sequence[Path] = (),
) -> GateEEnvironmentLayout:
    """Create one fresh venv, HOME, cache, project and output hierarchy."""
    if base_python is None:
        base_python = canonical_python_executable()
    if (
        not isinstance(root, Path)
        or not isinstance(repository_root, Path)
        or not isinstance(base_python, Path)
    ):
        raise TypeError(
            "root, repository_root and base_python must be Path objects"
        )
    if (
        not root.is_absolute()
        or not repository_root.is_absolute()
        or not base_python.is_absolute()
    ):
        raise GateEEnvironmentError(
            "unsafe_environment_root",
            "Gate E environment paths must be absolute",
        )
    repository = repository_root.resolve(strict=True)
    repository_metadata = _safe_directory(
        repository,
        code="unsafe_repository_root",
    )
    del repository_metadata
    _regular_single_link(base_python, code="unsafe_base_python")
    parent = root.parent.resolve(strict=True)
    _safe_directory(parent, code="unsafe_environment_root")
    intended = parent / root.name
    if (
        root.name in {"", ".", ".."}
        or root.parent != parent
        or repository_root != repository
        or intended == repository
        or repository in intended.parents
        or intended in repository.parents
    ):
        raise GateEEnvironmentError(
            "unsafe_environment_root",
            "Gate E environment must be outside the repository",
        )
    bootstrap_read_only = _validated_bootstrap_read_only_paths(
        read_only_paths
    )
    try:
        root.mkdir(mode=0o700, exist_ok=False)
        home = root / "home"
        xdg_cache = root / "xdg-cache"
        uv_cache = root / "uv-cache"
        project_root = root / "project"
        output_root = project_root / "outputs"
        config_parent = project_root / "configs" / "releases"
        for directory in (
            home,
            xdg_cache,
            uv_cache,
            project_root,
            output_root,
        ):
            directory.mkdir(mode=0o700)
        config_parent.mkdir(mode=0o700, parents=True)
    except OSError as exc:
        raise GateEEnvironmentError(
            "environment_layout_failed",
            "Gate E environment layout could not be created",
        ) from exc

    venv = root / "venv"
    python = venv / "bin" / "python"
    layout = GateEEnvironmentLayout(
        root=root,
        home=home,
        xdg_cache=xdg_cache,
        uv_cache=uv_cache,
        venv=venv,
        python=python,
        project_root=project_root,
        input_root=project_root,
        output_root=output_root,
        config_path=config_parent / "v0.2_gate_e.json",
        repository_root=repository,
        base_python=base_python,
    )
    try:
        completed = subprocess.run(
            _bootstrap_sandboxed_command(
                [str(base_python), "-m", "venv", str(venv)],
                bootstrap_read_only,
            ),
            cwd=root,
            env=_environment_values(layout, hash_seed="0"),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise GateEEnvironmentError(
            "environment_venv_failed",
            "Gate E venv could not be created",
        ) from exc
    if completed.returncode != 0 or not python.exists():
        raise GateEEnvironmentError(
            "environment_venv_failed",
            "Gate E venv could not be created",
        )
    try:
        version_probe = subprocess.run(
            _bootstrap_sandboxed_command(
                [
                    str(python),
                    "-c",
                    (
                        "import sys;"
                        "print('.'.join(str(value) for value in "
                        "sys.version_info[:3]))"
                    ),
                ],
                bootstrap_read_only,
            ),
            cwd=root,
            env=_environment_values(layout, hash_seed="0"),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise GateEEnvironmentError(
            "environment_python_mismatch",
            "Gate E venv Python version could not be verified",
        ) from exc
    if (
        version_probe.returncode != 0
        or version_probe.stdout.strip() != "3.11.15"
    ):
        raise GateEEnvironmentError(
            "environment_python_mismatch",
            "Gate E venv must use Python 3.11.15",
        )
    return layout


def _verify_layout(layout: GateEEnvironmentLayout) -> None:
    if type(layout) is not GateEEnvironmentLayout:
        raise GateEEnvironmentError(
            "invalid_environment_layout",
            "Gate E environment layout is invalid",
        )
    expected_paths = {
        "home": layout.root / "home",
        "xdg_cache": layout.root / "xdg-cache",
        "uv_cache": layout.root / "uv-cache",
        "venv": layout.root / "venv",
        "python": layout.root / "venv" / "bin" / "python",
        "project_root": layout.root / "project",
        "input_root": layout.root / "project",
        "output_root": layout.root / "project" / "outputs",
        "config_path": (
            layout.root
            / "project"
            / "configs"
            / "releases"
            / "v0.2_gate_e.json"
        ),
    }
    if any(
        getattr(layout, field) != expected
        for field, expected in expected_paths.items()
    ):
        raise GateEEnvironmentError(
            "invalid_environment_layout",
            "Gate E environment layout is invalid",
        )
    try:
        if (
            layout.root.resolve(strict=True) != layout.root
            or layout.repository_root.resolve(strict=True)
            != layout.repository_root
        ):
            raise GateEEnvironmentError(
                "invalid_environment_layout",
                "Gate E environment layout is invalid",
            )
    except OSError as exc:
        raise GateEEnvironmentError(
            "invalid_environment_layout",
            "Gate E environment layout is invalid",
        ) from exc
    for directory in (
        layout.root,
        layout.home,
        layout.xdg_cache,
        layout.uv_cache,
        layout.venv,
        layout.venv / "bin",
        layout.project_root,
        layout.project_root / "configs",
        layout.project_root / "configs" / "releases",
        layout.output_root,
        layout.repository_root,
    ):
        _safe_directory(directory, code="invalid_environment_layout")
    if (
        layout.repository_root == layout.root
        or layout.repository_root in layout.root.parents
        or layout.root in layout.repository_root.parents
    ):
        raise GateEEnvironmentError(
            "invalid_environment_layout",
            "Gate E environment layout is invalid",
        )
    try:
        _regular_single_link(
            layout.base_python,
            code="invalid_environment_layout",
        )
        resolved_python = layout.python.resolve(strict=True)
        resolved_base = layout.base_python.resolve(strict=True)
    except OSError as exc:
        raise GateEEnvironmentError(
            "invalid_environment_layout",
            "Gate E environment Python is unavailable",
        ) from exc
    if resolved_python != resolved_base:
        raise GateEEnvironmentError(
            "invalid_environment_layout",
            "Gate E environment Python is not the fixed interpreter",
        )


def _environment_values(
    layout: GateEEnvironmentLayout,
    *,
    hash_seed: str,
) -> dict[str, str]:
    if type(layout) is not GateEEnvironmentLayout:
        raise GateEEnvironmentError(
            "invalid_environment_layout",
            "Gate E environment layout is invalid",
        )
    if (
        type(hash_seed) is not str
        or not hash_seed.isascii()
        or not hash_seed.isdecimal()
        or int(hash_seed) > 4_294_967_295
    ):
        raise GateEEnvironmentError(
            "invalid_hash_seed",
            "Gate E hash seed is invalid",
        )
    return {
        "HOME": str(layout.home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": _SYSTEM_PATH,
        "PIP_CONFIG_FILE": "/dev/null",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": hash_seed,
        "PYTHONNOUSERSITE": "1",
        "TZ": "Asia/Shanghai",
        "UV_CACHE_DIR": str(layout.uv_cache),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "XDG_CACHE_HOME": str(layout.xdg_cache),
    }


def execution_environment(
    layout: GateEEnvironmentLayout,
    *,
    hash_seed: str,
) -> dict[str, str]:
    """Return the exact environment allowlist for one replay process."""
    _verify_layout(layout)
    return _environment_values(layout, hash_seed=hash_seed)


def _validated_command(command: Sequence[str]) -> list[str]:
    if (
        isinstance(command, (str, bytes))
        or not isinstance(command, Sequence)
        or not command
    ):
        raise GateEEnvironmentError(
            "invalid_environment_command",
            "Gate E command is invalid",
        )
    values = list(command)
    if any(
        type(value) is not str or not value or "\x00" in value
        for value in values
    ):
        raise GateEEnvironmentError(
            "invalid_environment_command",
            "Gate E command is invalid",
        )
    executable = Path(values[0])
    if not executable.is_absolute():
        raise GateEEnvironmentError(
            "invalid_environment_command",
            "Gate E executable must be absolute",
        )
    return values


def _run_environment_command(
    layout: GateEEnvironmentLayout,
    command: Sequence[str],
    *,
    hash_seed: str,
    sandboxed: bool,
    timeout_seconds: int,
    read_only_paths: Sequence[Path],
    verification_mode_files: Sequence[Path],
) -> subprocess.CompletedProcess[str]:
    _verify_layout(layout)
    values = _validated_command(command)
    if (
        type(timeout_seconds) is not int
        or timeout_seconds < 1
        or timeout_seconds > 3600
    ):
        raise GateEEnvironmentError(
            "invalid_environment_timeout",
            "Gate E command timeout is invalid",
        )
    if sandboxed:
        denied = (
            layout.repository_root,
            Path.home() / ".cache",
            Path.home() / ".config",
            Path.home() / ".local" / "lib",
            Path.home() / "Library" / "Caches",
            Path.home() / "Library" / "Python",
        )
        profile_lines = [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
        ]
        for path in denied:
            quoted = json.dumps(str(path), ensure_ascii=False)
            profile_lines.append(
                f"(deny file-read* (subpath {quoted}))"
            )
            profile_lines.append(
                f"(deny file-write* (subpath {quoted}))"
            )
        if isinstance(read_only_paths, (str, bytes)) or not isinstance(
            read_only_paths,
            Sequence,
        ):
            raise GateEEnvironmentError(
                "invalid_read_only_path",
                "Gate E read-only sandbox paths are invalid",
            )
        for path in read_only_paths:
            if not isinstance(path, Path) or not path.is_absolute():
                raise GateEEnvironmentError(
                    "invalid_read_only_path",
                    "Gate E read-only sandbox path is invalid",
                )
            try:
                resolved = path.resolve(strict=True)
                metadata = path.lstat()
            except OSError as exc:
                raise GateEEnvironmentError(
                    "invalid_read_only_path",
                    "Gate E read-only sandbox path is unavailable",
                ) from exc
            if (
                path != resolved
                or path.is_symlink()
                or not (
                    stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISDIR(metadata.st_mode)
                )
            ):
                raise GateEEnvironmentError(
                    "invalid_read_only_path",
                    "Gate E read-only sandbox path is unsafe",
                )
            quoted = json.dumps(str(path), ensure_ascii=False)
            filter_name = (
                "subpath" if stat.S_ISDIR(metadata.st_mode) else "literal"
            )
            profile_lines.append(
                f"(deny {_READ_ONLY_WRITE_OPERATION} "
                f"({filter_name} {quoted}))"
            )
        if (
            isinstance(verification_mode_files, (str, bytes))
            or not isinstance(verification_mode_files, Sequence)
        ):
            raise GateEEnvironmentError(
                "invalid_verification_mode_file",
                "Gate E verification-mode files are invalid",
            )
        for path in verification_mode_files:
            if not isinstance(path, Path) or not path.is_absolute():
                raise GateEEnvironmentError(
                    "invalid_verification_mode_file",
                    "Gate E verification-mode file is invalid",
                )
            try:
                resolved = path.resolve(strict=True)
                metadata = path.lstat()
            except OSError as exc:
                raise GateEEnvironmentError(
                    "invalid_verification_mode_file",
                    "Gate E verification-mode file is unavailable",
                ) from exc
            if (
                path != resolved
                or path.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or any(
                    path == strict
                    or (
                        strict.is_dir()
                        and strict in path.parents
                    )
                    for strict in read_only_paths
                )
            ):
                raise GateEEnvironmentError(
                    "invalid_verification_mode_file",
                    "Gate E verification-mode file is unsafe",
                )
            quoted = json.dumps(str(path), ensure_ascii=False)
            operations = " ".join(
                _VERIFICATION_MODE_WRITE_OPERATIONS
            )
            profile_lines.append(
                f"(deny {operations} (literal {quoted}))"
            )
        values = [
            "/usr/bin/sandbox-exec",
            "-p",
            "".join(profile_lines),
            *values,
        ]
    try:
        return subprocess.run(
            values,
            cwd=layout.project_root,
            env=execution_environment(layout, hash_seed=hash_seed),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateEEnvironmentError(
            "environment_command_failed",
            "Gate E environment command could not complete",
        ) from exc


def run_controlled(
    layout: GateEEnvironmentLayout,
    command: Sequence[str],
    *,
    hash_seed: str = "101",
    timeout_seconds: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run with the allowlist but without the OS sandbox, for control probes."""
    return _run_environment_command(
        layout,
        command,
        hash_seed=hash_seed,
        sandboxed=False,
        timeout_seconds=timeout_seconds,
        read_only_paths=(),
        verification_mode_files=(),
    )


def run_sandboxed(
    layout: GateEEnvironmentLayout,
    command: Sequence[str],
    *,
    hash_seed: str = "101",
    timeout_seconds: int = 120,
    read_only_paths: Sequence[Path] = (),
    verification_mode_files: Sequence[Path] = (),
) -> subprocess.CompletedProcess[str]:
    """Run with both the allowlist and macOS network/source denial."""
    return _run_environment_command(
        layout,
        command,
        hash_seed=hash_seed,
        sandboxed=True,
        timeout_seconds=timeout_seconds,
        read_only_paths=read_only_paths,
        verification_mode_files=verification_mode_files,
    )


def copy_gate_e_config(
    layout: GateEEnvironmentLayout,
    source: Path,
    *,
    expected_sha256: str,
) -> Path:
    """Copy the canonical config to a distinct, read-only environment inode."""
    _verify_layout(layout)
    if (
        not isinstance(source, Path)
        or type(expected_sha256) is not str
        or _HASH_RE.fullmatch(expected_sha256) is None
    ):
        raise GateEEnvironmentError(
            "invalid_config_copy_contract",
            "Gate E config copy contract is invalid",
        )
    content, source_metadata = _read_regular_single_link(
        source,
        code="unsafe_gate_e_config",
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise GateEEnvironmentError(
            "gate_e_config_hash_mismatch",
            "Gate E source config hash differs from the approved hash",
        )
    _write_immutable_control(
        layout.config_path,
        content,
        unsafe_code="unsafe_gate_e_config_copy",
        conflict_code="gate_e_config_copy_conflict",
        failure_code="gate_e_config_copy_failed",
        label="config copy",
    )
    try:
        layout.config_path.chmod(0o400, follow_symlinks=False)
    except (NotImplementedError, OSError) as exc:
        raise GateEEnvironmentError(
            "gate_e_config_copy_failed",
            "Gate E config copy could not be made read-only",
        ) from exc
    copied, copied_metadata = _read_regular_single_link(
        layout.config_path,
        code="unsafe_gate_e_config_copy",
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )
    if (
        copied != content
        or (source_metadata.st_dev, source_metadata.st_ino)
        == (copied_metadata.st_dev, copied_metadata.st_ino)
    ):
        raise GateEEnvironmentError(
            "gate_e_config_copy_failed",
            "Gate E config copy is not independent",
        )
    return layout.config_path


def stage_environment_inputs(
    layout: GateEEnvironmentLayout,
    release_root: Path,
) -> GateEInputCopy:
    """Replace the empty project scaffold with one atomic 25-file copy."""
    _verify_layout(layout)
    if not isinstance(release_root, Path):
        raise TypeError("release_root must be a Path")
    scaffold = layout.root / ".empty-project-scaffold"
    if os.path.lexists(scaffold):
        raise GateEEnvironmentError(
            "environment_scaffold_conflict",
            "Gate E empty project scaffold already exists",
        )
    expected_directories = {
        layout.project_root,
        layout.project_root / "configs",
        layout.project_root / "configs" / "releases",
        layout.output_root,
    }
    actual_directories: set[Path] = set()
    try:
        for path in (layout.project_root, *layout.project_root.rglob("*")):
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode) and not path.is_symlink():
                actual_directories.add(path)
            else:
                raise GateEEnvironmentError(
                    "environment_scaffold_not_empty",
                    "Gate E project scaffold must be empty",
                )
    except OSError as exc:
        raise GateEEnvironmentError(
            "environment_scaffold_not_empty",
            "Gate E project scaffold cannot be inspected",
        ) from exc
    if actual_directories != expected_directories:
        raise GateEEnvironmentError(
            "environment_scaffold_not_empty",
            "Gate E project scaffold must contain only empty controls",
        )

    root_descriptor = -1
    scaffold_moved = False
    try:
        root_descriptor = os.open(
            layout.root,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
        )
        os.rename(
            layout.project_root.name,
            scaffold.name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )
        scaffold_moved = True
        os.fsync(root_descriptor)
        evidence = stage_gate_e_input_root(
            release_root,
            layout.project_root,
        )
        (layout.project_root / "configs" / "releases").mkdir(
            mode=0o700,
            parents=True,
        )
        layout.output_root.mkdir(mode=0o700)
        os.fsync(root_descriptor)
    except GateEEnvironmentError:
        raise
    except Exception as exc:
        if (
            scaffold_moved
            and not os.path.lexists(layout.project_root)
            and root_descriptor >= 0
        ):
            try:
                os.rename(
                    scaffold.name,
                    layout.project_root.name,
                    src_dir_fd=root_descriptor,
                    dst_dir_fd=root_descriptor,
                )
                os.fsync(root_descriptor)
            except OSError:
                pass
        raise GateEEnvironmentError(
            "environment_input_stage_failed",
            "Gate E input root could not be staged",
        ) from exc
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
    _verify_layout(layout)
    return evidence


def _require_success(
    completed: subprocess.CompletedProcess[str],
    *,
    code: str,
    message: str,
) -> None:
    if completed.returncode != 0:
        raise GateEEnvironmentError(code, message)


def _require_read_only_file(path: Path, *, code: str) -> None:
    metadata = _regular_single_link(path, code=code)
    if metadata.st_mode & 0o222:
        raise GateEEnvironmentError(
            code,
            "sealed Gate E file must not have write bits",
        )


def _require_read_only_wheelhouse(
    wheelhouse: Path,
    *,
    manifest: Path,
    install_lock: Path,
) -> None:
    metadata = _safe_directory(
        wheelhouse,
        code="mutable_wheelhouse",
    )
    if metadata.st_mode & 0o222:
        raise GateEEnvironmentError(
            "mutable_wheelhouse",
            "sealed Gate E wheelhouse must not have write bits",
        )
    try:
        children = tuple(wheelhouse.iterdir())
    except OSError as exc:
        raise GateEEnvironmentError(
            "mutable_wheelhouse",
            "sealed Gate E wheelhouse cannot be inspected",
        ) from exc
    for child in children:
        _require_read_only_file(child, code="mutable_wheelhouse")
    _require_read_only_file(manifest, code="mutable_wheelhouse_control")
    _require_read_only_file(install_lock, code="mutable_wheelhouse_control")


def inspect_installed_environment(
    layout: GateEEnvironmentLayout,
    *,
    hash_seed: str,
    require_gate_e_cli: bool = False,
    read_only_paths: Sequence[Path] = (),
) -> InstalledEnvironmentEvidence:
    """Prove imports, paths, package inventory and installed CLI entry points."""
    _verify_layout(layout)
    script = (
        "import importlib.metadata as m,json,sys;"
        "from pathlib import Path;"
        "import aquant;"
        "pairs=sorted((d.metadata['Name'],d.version) for d in m.distributions());"
        "print(json.dumps({"
        "'aquant_file':str(Path(aquant.__file__).resolve()),"
        "'packages':pairs,"
        "'project_version':m.version('a-share-quant'),"
        "'sys_path':sys.path"
        "},sort_keys=True,separators=(',',':')))"
    )
    completed = run_sandboxed(
        layout,
        [str(layout.python), "-c", script],
        hash_seed=hash_seed,
        read_only_paths=read_only_paths,
    )
    _require_success(
        completed,
        code="environment_import_failed",
        message="installed Gate E package could not be imported",
    )
    try:
        payload = json.loads(
            completed.stdout,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (GateEEnvironmentError, json.JSONDecodeError) as exc:
        raise GateEEnvironmentError(
            "environment_import_evidence_invalid",
            "installed Gate E import evidence is invalid",
        ) from exc
    if (
        type(payload) is not dict
        or set(payload)
        != {"aquant_file", "packages", "project_version", "sys_path"}
        or payload["project_version"] != _PROJECT_VERSION
        or type(payload["aquant_file"]) is not str
        or type(payload["sys_path"]) is not list
        or any(type(value) is not str for value in payload["sys_path"])
        or type(payload["packages"]) is not list
        or any(
            type(item) is not list
            or len(item) != 2
            or any(type(value) is not str for value in item)
            for item in payload["packages"]
        )
    ):
        raise GateEEnvironmentError(
            "environment_import_evidence_invalid",
            "installed Gate E import evidence is invalid",
        )
    aquant_file = Path(payload["aquant_file"])
    try:
        under_venv = aquant_file.is_relative_to(layout.venv.resolve(strict=True))
    except OSError as exc:
        raise GateEEnvironmentError(
            "environment_import_evidence_invalid",
            "installed Gate E import path cannot be verified",
        ) from exc
    repository_text = str(layout.repository_root)
    denied_import_roots = (
        repository_text,
        str(Path.home() / ".local" / "lib"),
        str(Path.home() / "Library" / "Python"),
    )
    packages = tuple((item[0], item[1]) for item in payload["packages"])
    normalized_packages = tuple(
        _normalized_name(name) for name, _version in packages
    )
    if (
        not under_venv
        or any(
            value == denied
            or value.startswith(f"{denied}{os.sep}")
            for denied in denied_import_roots
            for value in payload["sys_path"]
        )
        or len(normalized_packages) != len(set(normalized_packages))
        or normalized_packages.count(_PROJECT_NAME) != 1
    ):
        raise GateEEnvironmentError(
            "environment_import_escape",
            "installed Gate E package escaped its isolated venv",
        )
    portfolio_cli = layout.venv / "bin" / "aquant-portfolio"
    _regular_single_link(
        portfolio_cli,
        code="environment_cli_missing",
    )
    portfolio_help = run_sandboxed(
        layout,
        [str(portfolio_cli), "--help"],
        hash_seed=hash_seed,
        read_only_paths=read_only_paths,
    )
    _require_success(
        portfolio_help,
        code="environment_cli_missing",
        message="installed portfolio CLI is unavailable",
    )
    gate_e_cli = layout.venv / "bin" / "aquant-gate-e"
    if require_gate_e_cli:
        _regular_single_link(
            gate_e_cli,
            code="environment_cli_missing",
        )
        gate_e_help = run_sandboxed(
            layout,
            [str(gate_e_cli), "--help"],
            hash_seed=hash_seed,
            read_only_paths=read_only_paths,
        )
        _require_success(
            gate_e_help,
            code="environment_cli_missing",
            message="installed Gate E controller CLI is unavailable",
        )
    return InstalledEnvironmentEvidence(
        project_version=payload["project_version"],
        aquant_file=aquant_file,
        sys_path=tuple(payload["sys_path"]),
        packages=packages,
        portfolio_cli=portfolio_cli,
        gate_e_cli=gate_e_cli if require_gate_e_cli else None,
    )


def install_gate_e_environment(
    layout: GateEEnvironmentLayout,
    *,
    project_wheel: Path,
    expected_project_sha256: str,
    wheelhouse: Path,
    wheelhouse_manifest: Path,
    install_lock: Path,
    expected_requirements: Mapping[str, str],
    hash_seed: str,
    uv_executable: Path | None = None,
    require_gate_e_cli: bool = False,
    read_only_paths: Sequence[Path] = (),
) -> InstalledEnvironmentEvidence:
    """Install only sealed wheel bytes under the OS network sandbox."""
    _verify_layout(layout)
    if uv_executable is None:
        uv_executable = canonical_uv_executable()
    if (
        not isinstance(project_wheel, Path)
        or not isinstance(uv_executable, Path)
        or type(expected_project_sha256) is not str
        or _HASH_RE.fullmatch(expected_project_sha256) is None
        or not uv_executable.is_absolute()
    ):
        raise GateEEnvironmentError(
            "invalid_environment_install_contract",
            "Gate E installation contract is invalid",
        )
    try:
        resolved_uv = uv_executable.resolve(strict=True)
        uv_metadata = _regular_single_link(
            resolved_uv,
            code="unsafe_uv_executable",
        )
    except OSError as exc:
        raise GateEEnvironmentError(
            "unsafe_uv_executable",
            "Gate E uv executable is unavailable",
        ) from exc
    if not uv_metadata.st_mode & 0o111:
        raise GateEEnvironmentError(
            "unsafe_uv_executable",
            "Gate E uv executable is not executable",
        )
    project = inspect_project_wheel(project_wheel)
    if project.sha256 != expected_project_sha256:
        raise GateEEnvironmentError(
            "project_wheel_hash_mismatch",
            "Gate E project wheel differs from the approved hash",
        )
    _require_read_only_file(
        project_wheel,
        code="mutable_project_wheel",
    )
    before = verify_wheelhouse(
        wheelhouse,
        expected_requirements=expected_requirements,
        manifest=wheelhouse_manifest,
        install_lock=install_lock,
    )
    _require_read_only_wheelhouse(
        wheelhouse,
        manifest=wheelhouse_manifest,
        install_lock=install_lock,
    )
    sealed_paths = (
        project_wheel,
        wheelhouse,
        wheelhouse_manifest,
        install_lock,
        *read_only_paths,
    )
    sync = run_sandboxed(
        layout,
        [
            str(resolved_uv),
            "pip",
            "sync",
            "--python",
            str(layout.python),
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            str(install_lock),
        ],
        hash_seed=hash_seed,
        timeout_seconds=600,
        read_only_paths=sealed_paths,
    )
    _require_success(
        sync,
        code="environment_dependency_install_failed",
        message="sealed Gate E dependencies could not be installed",
    )
    install = run_sandboxed(
        layout,
        [
            str(resolved_uv),
            "pip",
            "install",
            "--python",
            str(layout.python),
            "--no-deps",
            "--only-binary",
            ":all:",
            "--no-index",
            str(project_wheel),
        ],
        hash_seed=hash_seed,
        timeout_seconds=600,
        read_only_paths=sealed_paths,
    )
    _require_success(
        install,
        code="environment_project_install_failed",
        message="sealed Gate E project wheel could not be installed",
    )
    check = run_sandboxed(
        layout,
        [
            str(resolved_uv),
            "pip",
            "check",
            "--python",
            str(layout.python),
        ],
        hash_seed=hash_seed,
        read_only_paths=sealed_paths,
    )
    _require_success(
        check,
        code="environment_dependency_check_failed",
        message="installed Gate E dependency check failed",
    )
    after = verify_wheelhouse(
        wheelhouse,
        expected_requirements=expected_requirements,
        manifest=wheelhouse_manifest,
        install_lock=install_lock,
    )
    _require_read_only_wheelhouse(
        wheelhouse,
        manifest=wheelhouse_manifest,
        install_lock=install_lock,
    )
    project_after = inspect_project_wheel(project_wheel)
    if before != after or project_after.sha256 != project.sha256:
        raise GateEEnvironmentError(
            "environment_install_input_changed",
            "sealed Gate E installation inputs changed during install",
        )
    return inspect_installed_environment(
        layout,
        hash_seed=hash_seed,
        require_gate_e_cli=require_gate_e_cli,
        read_only_paths=read_only_paths,
    )
