"""External trust manifest for the Gate E isolated release replay."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from aquant.gate_e.audit import (
    GateEAuditError,
    audit_gate_e_bundle,
)
from aquant.gate_e.config import GateEConfigError, load_gate_e_config
from aquant.gate_e.environment import (
    GateEEnvironmentError,
    inspect_project_wheel,
    verify_wheelhouse,
)

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}")
_NORMALIZED_NAME_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?"
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_MAX_FILE_BYTES = 1024 * 1024 * 1024
_MAX_CONTROL_BYTES = 16 * 1024 * 1024
_ARTIFACT_FILES = frozenset(
    {
        "artifact_manifest.json",
        "availability.csv",
        "cash.csv",
        "corporate_actions.csv",
        "equity.csv",
        "fills.csv",
        "lots.csv",
        "metrics.json",
        "orders.csv",
        "positions.csv",
        "receivables.csv",
        "run.json",
        "targets.csv",
    }
)
_PAYLOAD_FILES = _ARTIFACT_FILES - {"artifact_manifest.json"}
_TRUST_KEYS = frozenset(
    {
        "artifact",
        "candidate_review",
        "config",
        "expected_run_id",
        "gate",
        "implementation_commit",
        "project_name",
        "project_version",
        "project_wheel",
        "python",
        "research_boundary",
        "schema_version",
        "uv",
        "uv_lock",
        "v01",
        "wheelhouse",
    }
)
_CANDIDATE_REVIEW_RELATIVE = (
    "outputs/Work_Buddy候选A复核_v0.2_Gate_E.md"
)
_REVIEW_ASSIGNMENT_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*=\s*(\S(?:.*\S)?)\s*$"
)
_CANDIDATE_REVIEW_KEYS = frozenset(
    {
        "P0",
        "P1",
        "P2",
        "artifact_manifest_sha256",
        "candidate_evidence_sha256",
        "decision",
        "expected_run_id",
        "gate",
        "implementation_commit",
        "project",
        "project_wheel_sha256",
        "review_kind",
        "version",
    }
)
_RESEARCH_BOUNDARY = {
    "live_trading": False,
    "profit_claim": False,
    "research_only": True,
    "simulation_only": True,
}


class GateETrustError(RuntimeError):
    """Stable, sanitized failure at the external trust boundary."""

    def __init__(self, code: str, *, cause_code: str | None = None):
        self.code = code
        self.cause_code = cause_code
        super().__init__(code)


@dataclass(frozen=True)
class GateETrustEvidence:
    """Complete live evidence that one trust manifest must bind."""

    implementation_commit: str
    project_wheel: Path
    uv_lock: Path
    python_executable: Path
    uv_executable: Path
    wheelhouse_root: Path
    wheelhouse_manifest: Path
    v01_tag_commit: str
    v01_release_manifest: Path
    config: Path
    artifact: Path
    candidate_review: Path
    reviewed_candidate_evidence: Path


@dataclass(frozen=True)
class VerifiedGateETrust:
    """Small immutable result from complete external re-verification."""

    implementation_commit: str
    expected_run_id: str
    artifact_file_count: int
    payload_file_count: int
    files: tuple[tuple[str, str], ...]
    trust_sha256: str


@dataclass(frozen=True)
class _EvidenceSnapshot:
    implementation_commit: str
    project_wheel: dict[str, object]
    uv_lock: dict[str, object]
    python: dict[str, object]
    uv: dict[str, object]
    wheelhouse: dict[str, object]
    v01: dict[str, object]
    config: dict[str, object]
    artifact: dict[str, object]
    candidate_review: dict[str, object]
    reviewed_candidate_evidence_sha256: str


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _read_regular(
    path: Path,
    *,
    code: str,
    maximum_bytes: int = _MAX_CONTROL_BYTES,
    allow_parent_alias: bool = False,
) -> tuple[bytes, os.stat_result]:
    if not isinstance(path, Path):
        raise TypeError("evidence paths must be Path objects")
    absolute = Path(os.path.abspath(os.fspath(path)))
    descriptor = -1
    try:
        if (
            not allow_parent_alias
            and absolute.resolve(strict=True) != absolute
        ):
            raise GateETrustError(code)
        initial = absolute.lstat()
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or absolute.is_symlink()
            or initial.st_size > maximum_bytes
        ):
            raise GateETrustError(code)
        descriptor = os.open(
            absolute,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
        )
        opened = os.fstat(descriptor)
        if not _same_object(initial, opened):
            raise GateETrustError(code)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, maximum_bytes + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise GateETrustError(code)
        final = os.fstat(descriptor)
        named = absolute.lstat()
        if (
            not stat.S_ISREG(final.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or final.st_nlink != 1
            or named.st_nlink != 1
            or not _same_object(opened, final)
            or not _same_object(opened, named)
            or opened.st_size != final.st_size
            or opened.st_mtime_ns != final.st_mtime_ns
            or opened.st_ctime_ns != final.st_ctime_ns
            or total != final.st_size
        ):
            raise GateETrustError(code)
        return b"".join(chunks), final
    except GateETrustError:
        raise
    except (OSError, RuntimeError) as exc:
        raise GateETrustError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GateETrustError("invalid_trust_manifest")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise GateETrustError("invalid_trust_manifest")


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode()
    except (TypeError, UnicodeError, ValueError) as exc:
        raise GateETrustError("invalid_trust_manifest") from exc


def _parse_canonical_json(
    content: bytes,
    *,
    code: str,
) -> dict[str, object]:
    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except GateETrustError as exc:
        raise GateETrustError(code) from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GateETrustError(code) from exc
    if type(payload) is not dict or _canonical_json_bytes(payload) != content:
        raise GateETrustError(code)
    return payload


def _validate_evidence(evidence: GateETrustEvidence) -> None:
    if type(evidence) is not GateETrustEvidence:
        raise TypeError("evidence must be an exact GateETrustEvidence")
    if (
        _COMMIT_RE.fullmatch(evidence.implementation_commit) is None
        or _COMMIT_RE.fullmatch(evidence.v01_tag_commit) is None
        or any(
            not isinstance(getattr(evidence, field), Path)
            for field in (
                "project_wheel",
                "uv_lock",
                "python_executable",
                "uv_executable",
                "wheelhouse_root",
                "wheelhouse_manifest",
                "v01_release_manifest",
                "config",
                "artifact",
                "candidate_review",
                "reviewed_candidate_evidence",
            )
        )
    ):
        raise GateETrustError("invalid_trust_evidence")


def _project_wheel_snapshot(path: Path) -> dict[str, object]:
    try:
        evidence = inspect_project_wheel(path)
    except (GateEEnvironmentError, OSError, TypeError, ValueError) as exc:
        raise GateETrustError("project_wheel_mismatch") from exc
    return {
        "filename": path.name,
        "sha256": evidence.sha256,
        "size": evidence.size,
        "version": evidence.distribution_version,
    }


def _simple_file_snapshot(path: Path, *, code: str) -> dict[str, object]:
    content, metadata = _read_regular(path, code=code)
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": metadata.st_size,
    }


def _candidate_review_bindings(content: bytes) -> dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise GateETrustError("candidate_review_mismatch") from exc
    bindings: dict[str, str] = {}
    for line in text.splitlines():
        matched = _REVIEW_ASSIGNMENT_RE.fullmatch(line)
        if matched is None:
            continue
        key, value = matched.groups()
        if key not in _CANDIDATE_REVIEW_KEYS or key in bindings:
            raise GateETrustError("candidate_review_mismatch")
        bindings[key] = value
    if (
        set(bindings) != _CANDIDATE_REVIEW_KEYS
        or bindings["project"] != "a-share-quant"
        or bindings["version"] != "v0.2"
        or bindings["gate"] != "E"
        or bindings["review_kind"] != "candidate_a"
        or bindings["decision"] != "PASS"
        or any(bindings[field] != "0" for field in ("P0", "P1", "P2"))
        or _COMMIT_RE.fullmatch(bindings["implementation_commit"])
        is None
        or any(
            _HASH_RE.fullmatch(bindings[field]) is None
            for field in (
                "candidate_evidence_sha256",
                "expected_run_id",
                "artifact_manifest_sha256",
                "project_wheel_sha256",
            )
        )
    ):
        raise GateETrustError("candidate_review_mismatch")
    return bindings


def _candidate_review_snapshot(
    evidence: GateETrustEvidence,
) -> tuple[dict[str, object], str]:
    content, metadata = _read_regular(
        evidence.candidate_review,
        code="candidate_review_mismatch",
    )
    candidate_content, _candidate_metadata = _read_regular(
        evidence.reviewed_candidate_evidence,
        code="candidate_review_mismatch",
    )
    bindings = _candidate_review_bindings(content)
    return (
        {
            "bindings": bindings,
            "path": _CANDIDATE_REVIEW_RELATIVE,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": metadata.st_size,
        },
        hashlib.sha256(candidate_content).hexdigest(),
    )


def _validate_candidate_review_snapshot(
    snapshot: _EvidenceSnapshot,
) -> None:
    bindings = snapshot.candidate_review["bindings"]
    manifest_hashes = {
        item.get("name"): item.get("sha256")
        for item in snapshot.artifact["files"]
        if type(item) is dict
    }
    if (
        type(bindings) is not dict
        or bindings.get("implementation_commit")
        != snapshot.implementation_commit
        or bindings.get("candidate_evidence_sha256")
        != snapshot.reviewed_candidate_evidence_sha256
        or bindings.get("expected_run_id")
        != snapshot.artifact["actual_run_id"]
        or bindings.get("artifact_manifest_sha256")
        != manifest_hashes.get("artifact_manifest.json")
        or bindings.get("project_wheel_sha256")
        != snapshot.project_wheel.get("sha256")
    ):
        raise GateETrustError("candidate_review_mismatch")


def _python_snapshot(path: Path) -> dict[str, object]:
    content, metadata = _read_regular(
        path,
        code="python_mismatch",
        maximum_bytes=_MAX_FILE_BYTES,
        allow_parent_alias=True,
    )
    try:
        completed = subprocess.run(
            [
                str(path),
                "-c",
                (
                    "import json,platform,sys;"
                    "print(json.dumps({"
                    "'architecture':platform.machine(),"
                    "'implementation':platform.python_implementation(),"
                    "'platform':platform.system(),"
                    "'version':'.'.join(str(value) for value in "
                    "sys.version_info[:3])"
                    "},sort_keys=True,separators=(',',':')))"
                ),
            ],
            cwd=path.parent,
            env={
                "HOME": "/private/var/empty",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONNOUSERSITE": "1",
                "TZ": "Asia/Shanghai",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateETrustError("python_mismatch") from exc
    try:
        identity = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GateETrustError("python_mismatch") from exc
    if (
        completed.returncode != 0
        or type(identity) is not dict
        or set(identity)
        != {"architecture", "implementation", "platform", "version"}
        or identity["implementation"] != "CPython"
        or identity["version"] != "3.11.15"
        or any(
            type(identity[field]) is not str or not identity[field]
            for field in ("architecture", "platform")
        )
    ):
        raise GateETrustError("python_mismatch")
    return {
        "architecture": identity["architecture"],
        "implementation": identity["implementation"],
        "name": "python",
        "platform": identity["platform"],
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": metadata.st_size,
        "version": identity["version"],
    }


def _uv_snapshot(path: Path) -> dict[str, object]:
    content, metadata = _read_regular(
        path,
        code="uv_mismatch",
        maximum_bytes=_MAX_FILE_BYTES,
    )
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            cwd=path.parent,
            env={
                "HOME": "/private/var/empty",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "TZ": "Asia/Shanghai",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateETrustError("uv_mismatch") from exc
    fields = completed.stdout.strip().split()
    if (
        completed.returncode != 0
        or len(fields) < 2
        or fields[0] != "uv"
        or _VERSION_RE.fullmatch(fields[1]) is None
    ):
        raise GateETrustError("uv_mismatch")
    return {
        "name": "uv",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": metadata.st_size,
        "version": fields[1],
    }


def _wheelhouse_manifest_requirements(
    path: Path,
) -> tuple[dict[str, str], bytes, os.stat_result]:
    content, metadata = _read_regular(path, code="wheelhouse_mismatch")
    payload = _parse_canonical_json(
        content,
        code="wheelhouse_mismatch",
    )
    wheels = payload.get("wheels")
    if (
        set(payload) != {"schema_version", "wheels"}
        or payload["schema_version"] != "1.0"
        or type(wheels) is not list
        or not wheels
    ):
        raise GateETrustError("wheelhouse_mismatch")
    requirements: dict[str, str] = {}
    for item in wheels:
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
            or Path(item["filename"]).name != item["filename"]
            or type(item["normalized_name"]) is not str
            or _NORMALIZED_NAME_RE.fullmatch(item["normalized_name"]) is None
            or type(item["version"]) is not str
            or _VERSION_RE.fullmatch(item["version"]) is None
            or type(item["sha256"]) is not str
            or _HASH_RE.fullmatch(item["sha256"]) is None
            or type(item["size"]) is not int
            or item["size"] <= 0
            or item["normalized_name"] in requirements
        ):
            raise GateETrustError("wheelhouse_mismatch")
        requirements[item["normalized_name"]] = item["version"]
    return requirements, content, metadata


def _wheelhouse_snapshot(
    root: Path,
    manifest: Path,
) -> dict[str, object]:
    requirements, manifest_content, manifest_metadata = (
        _wheelhouse_manifest_requirements(manifest)
    )
    try:
        evidence = verify_wheelhouse(
            root,
            expected_requirements=requirements,
            manifest=manifest,
        )
    except (GateEEnvironmentError, OSError, TypeError, ValueError) as exc:
        raise GateETrustError("wheelhouse_mismatch") from exc
    return {
        "files": [
            {
                "filename": entry.filename,
                "normalized_name": entry.normalized_name,
                "sha256": entry.sha256,
                "size": entry.size,
                "version": entry.version,
            }
            for entry in evidence.entries
        ],
        "manifest": {
            "filename": manifest.name,
            "sha256": hashlib.sha256(manifest_content).hexdigest(),
            "size": manifest_metadata.st_size,
        },
    }


def _config_snapshot(
    path: Path,
) -> tuple[dict[str, object], str, str]:
    try:
        content, metadata = _read_regular(path, code="config_mismatch")
        config = load_gate_e_config(path)
        payload = json.loads(content)
    except (
        GateEConfigError,
        GateETrustError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise GateETrustError("config_mismatch") from exc
    return (
        {
            "filename": path.name,
            "payload": payload,
            "sha256": config.config_sha256,
            "size": metadata.st_size,
        },
        config.payload["release_manifest_sha256"],
        config.payload["uv_lock_sha256"],
    )


def _artifact_file_bytes(
    directory: Path,
) -> dict[str, tuple[bytes, os.stat_result]]:
    if not isinstance(directory, Path):
        raise TypeError("artifact must be a Path")
    absolute = Path(os.path.abspath(directory))
    try:
        metadata = absolute.lstat()
        if (
            absolute.resolve(strict=True) != absolute
            or not stat.S_ISDIR(metadata.st_mode)
            or absolute.is_symlink()
        ):
            raise GateETrustError("artifact_file_set_mismatch")
        children = tuple(sorted(absolute.iterdir(), key=lambda item: item.name))
    except GateETrustError:
        raise
    except (OSError, RuntimeError) as exc:
        raise GateETrustError("artifact_file_set_mismatch") from exc
    if (
        frozenset(path.name for path in children) != _ARTIFACT_FILES
        or len(children) != len(_ARTIFACT_FILES)
    ):
        raise GateETrustError("artifact_file_set_mismatch")
    result: dict[str, tuple[bytes, os.stat_result]] = {}
    for path in children:
        try:
            result[path.name] = _read_regular(
                path,
                code="artifact_mismatch",
                maximum_bytes=_MAX_FILE_BYTES,
            )
        except GateETrustError as exc:
            raise GateETrustError("artifact_mismatch") from exc
    try:
        if frozenset(path.name for path in absolute.iterdir()) != _ARTIFACT_FILES:
            raise GateETrustError("artifact_file_set_mismatch")
    except OSError as exc:
        raise GateETrustError("artifact_file_set_mismatch") from exc
    return result


def _csv_rows(content: bytes) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(
            io.StringIO(content.decode("utf-8"), newline=""),
            strict=True,
        )
        rows = list(reader)
    except (UnicodeError, csv.Error) as exc:
        raise GateETrustError("artifact_mismatch") from exc
    if reader.fieldnames is None or any(None in row for row in rows):
        raise GateETrustError("artifact_mismatch")
    return rows


def _artifact_snapshot(directory: Path) -> dict[str, object]:
    files = _artifact_file_bytes(directory)
    try:
        audit = audit_gate_e_bundle(directory, expected_run_id=None)
    except GateEAuditError as exc:
        raise GateETrustError(
            "artifact_mismatch",
            cause_code=exc.code,
        ) from exc
    if directory.name != audit.run_id:
        raise GateETrustError("artifact_mismatch")
    try:
        manifest = json.loads(files["artifact_manifest.json"][0])
        row_counts = {
            name: manifest["files"][name]["row_count"]
            for name in sorted(_PAYLOAD_FILES)
        }
        targets = _csv_rows(files["targets.csv"][0])
        equity = _csv_rows(files["equity.csv"][0])
    except (
        GateETrustError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise GateETrustError("artifact_mismatch") from exc
    symbols = tuple(sorted({row["symbol"] for row in targets}))
    return {
        "actual_run_id": audit.run_id,
        "expected_counts": {
            "artifact_file_count": len(files),
            "no_bar_total": audit.no_bar_total,
            "payload_file_count": len(_PAYLOAD_FILES),
            "row_counts": row_counts,
            "session_count": len(equity),
            "symbol_count": len(symbols),
            "target_count": len(targets),
        },
        "files": [
            {
                "name": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": metadata.st_size,
            }
            for name, (content, metadata) in sorted(files.items())
        ],
    }


def _snapshot(evidence: GateETrustEvidence) -> _EvidenceSnapshot:
    _validate_evidence(evidence)
    project_wheel = _project_wheel_snapshot(evidence.project_wheel)
    uv_lock = _simple_file_snapshot(
        evidence.uv_lock,
        code="uv_lock_mismatch",
    )
    python = _python_snapshot(evidence.python_executable)
    uv = _uv_snapshot(evidence.uv_executable)
    wheelhouse = _wheelhouse_snapshot(
        evidence.wheelhouse_root,
        evidence.wheelhouse_manifest,
    )
    (
        config,
        expected_v01_manifest_hash,
        expected_uv_lock_hash,
    ) = _config_snapshot(evidence.config)
    if uv_lock["sha256"] != expected_uv_lock_hash:
        raise GateETrustError("uv_lock_mismatch")
    v01_manifest = _simple_file_snapshot(
        evidence.v01_release_manifest,
        code="v01_trust_mismatch",
    )
    if v01_manifest["sha256"] != expected_v01_manifest_hash:
        raise GateETrustError("v01_trust_mismatch")
    artifact = _artifact_snapshot(evidence.artifact)
    (
        candidate_review,
        reviewed_candidate_evidence_sha256,
    ) = _candidate_review_snapshot(evidence)
    return _EvidenceSnapshot(
        implementation_commit=evidence.implementation_commit,
        project_wheel=project_wheel,
        uv_lock=uv_lock,
        python=python,
        uv=uv,
        wheelhouse=wheelhouse,
        v01={
            "release_manifest": v01_manifest,
            "tag_commit": evidence.v01_tag_commit,
        },
        config=config,
        artifact=artifact,
        candidate_review=candidate_review,
        reviewed_candidate_evidence_sha256=(
            reviewed_candidate_evidence_sha256
        ),
    )


def _trust_payload(
    snapshot: _EvidenceSnapshot,
    *,
    expected_run_id: str,
) -> dict[str, object]:
    return {
        "artifact": snapshot.artifact,
        "candidate_review": snapshot.candidate_review,
        "config": snapshot.config,
        "expected_run_id": expected_run_id,
        "gate": "E",
        "implementation_commit": snapshot.implementation_commit,
        "project_name": "a-share-quant",
        "project_version": "0.2.0",
        "project_wheel": snapshot.project_wheel,
        "python": snapshot.python,
        "research_boundary": _RESEARCH_BOUNDARY,
        "schema_version": "1.0",
        "uv": snapshot.uv,
        "uv_lock": snapshot.uv_lock,
        "v01": snapshot.v01,
        "wheelhouse": snapshot.wheelhouse,
    }


def gate_e_trust_bytes(
    *,
    evidence: GateETrustEvidence,
    expected_run_id: str,
) -> bytes:
    """Return canonical trust bytes without approving or publishing them."""
    if (
        type(expected_run_id) is not str
        or _HASH_RE.fullmatch(expected_run_id) is None
    ):
        raise GateETrustError("invalid_expected_run_id")
    snapshot = _snapshot(evidence)
    _validate_candidate_review_snapshot(snapshot)
    return _canonical_json_bytes(
        _trust_payload(snapshot, expected_run_id=expected_run_id)
    )


def _publish_trust(path: Path, content: bytes) -> None:
    if not isinstance(path, Path):
        raise TypeError("trust path must be a Path")
    absolute = Path(os.path.abspath(path))
    temporary = f".{absolute.name}.tmp-{secrets.token_hex(12)}"
    parent_descriptor = -1
    descriptor = -1
    created = False
    try:
        parent = absolute.parent.resolve(strict=True)
        parent_metadata = absolute.parent.lstat()
        if (
            parent != absolute.parent
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or absolute.parent.is_symlink()
            or absolute.name in {"", ".", ".."}
        ):
            raise GateETrustError("unsafe_trust_manifest")
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o600,
            dir_fd=parent_descriptor,
        )
        created = True
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise GateETrustError("trust_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o444)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary,
            absolute.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=parent_descriptor)
        created = False
        os.fsync(parent_descriptor)
    except FileExistsError as exc:
        raise GateETrustError("trust_manifest_conflict") from exc
    except GateETrustError:
        raise
    except (OSError, RuntimeError) as exc:
        raise GateETrustError("trust_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created and parent_descriptor >= 0:
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except OSError:
                pass
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    written, _metadata = _read_regular(
        absolute,
        code="unsafe_trust_manifest",
    )
    if written != content:
        raise GateETrustError("trust_write_failed")


def write_gate_e_trust(
    path: Path,
    *,
    evidence: GateETrustEvidence,
    expected_run_id: str,
) -> Path:
    """Write one candidate trust blob; this operation does not approve it."""
    content = gate_e_trust_bytes(
        evidence=evidence,
        expected_run_id=expected_run_id,
    )
    _publish_trust(path, content)
    return path


def _read_trust(path: Path) -> tuple[dict[str, object], bytes]:
    content, _metadata = _read_regular(
        path,
        code="unsafe_trust_manifest",
    )
    payload = _parse_canonical_json(
        content,
        code="invalid_trust_manifest",
    )
    if (
        set(payload) != _TRUST_KEYS
        or payload.get("schema_version") != "1.0"
        or payload.get("project_name") != "a-share-quant"
        or payload.get("project_version") != "0.2.0"
        or payload.get("gate") != "E"
        or payload.get("research_boundary") != _RESEARCH_BOUNDARY
        or type(payload.get("expected_run_id")) is not str
        or _HASH_RE.fullmatch(payload["expected_run_id"]) is None
    ):
        raise GateETrustError("invalid_trust_manifest")
    return payload, content


def verify_gate_e_trust(
    path: Path,
    evidence: GateETrustEvidence,
) -> VerifiedGateETrust:
    """Recompute all evidence and verify one externally anchored trust blob."""
    payload, content = _read_trust(path)
    snapshot = _snapshot(evidence)
    expected_run_id = payload["expected_run_id"]
    if expected_run_id != snapshot.artifact["actual_run_id"]:
        raise GateETrustError("trusted_run_id_mismatch")
    if payload["implementation_commit"] != snapshot.implementation_commit:
        raise GateETrustError("implementation_commit_mismatch")
    if payload["project_wheel"] != snapshot.project_wheel:
        raise GateETrustError("project_wheel_mismatch")
    if payload["uv_lock"] != snapshot.uv_lock:
        raise GateETrustError("uv_lock_mismatch")
    if payload["python"] != snapshot.python:
        raise GateETrustError("python_mismatch")
    if payload["uv"] != snapshot.uv:
        raise GateETrustError("uv_mismatch")
    if payload["wheelhouse"] != snapshot.wheelhouse:
        raise GateETrustError("wheelhouse_mismatch")
    if payload["v01"] != snapshot.v01:
        raise GateETrustError("v01_trust_mismatch")
    if payload["config"] != snapshot.config:
        raise GateETrustError("config_mismatch")
    if payload["candidate_review"] != snapshot.candidate_review:
        raise GateETrustError("candidate_review_mismatch")
    artifact = payload["artifact"]
    if type(artifact) is not dict:
        raise GateETrustError("artifact_mismatch")
    if artifact.get("actual_run_id") != snapshot.artifact["actual_run_id"]:
        raise GateETrustError("trusted_run_id_mismatch")
    if artifact.get("files") != snapshot.artifact["files"]:
        raise GateETrustError("artifact_mismatch")
    if artifact.get("expected_counts") != snapshot.artifact["expected_counts"]:
        raise GateETrustError("artifact_count_mismatch")
    if set(artifact) != {"actual_run_id", "expected_counts", "files"}:
        raise GateETrustError("artifact_mismatch")
    _validate_candidate_review_snapshot(snapshot)
    files = tuple(
        (item["name"], item["sha256"])
        for item in snapshot.artifact["files"]
    )
    return VerifiedGateETrust(
        implementation_commit=snapshot.implementation_commit,
        expected_run_id=expected_run_id,
        artifact_file_count=len(files),
        payload_file_count=len(_PAYLOAD_FILES),
        files=files,
        trust_sha256=hashlib.sha256(content).hexdigest(),
    )
