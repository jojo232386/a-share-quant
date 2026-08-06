"""Staged, fail-closed orchestration for the Gate E isolated replay."""

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
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path, PurePosixPath

from aquant.gate_e.audit import (
    GateEAccountingAudit,
    GateEAuditError,
    GateEInputAudit,
    audit_gate_e_bundle,
    audit_gate_e_inputs,
    reconcile_gate_e_no_bar,
)
from aquant.gate_e.config import (
    GateEConfig,
    GateEConfigError,
    load_gate_e_config,
)
from aquant.gate_e.environment import (
    GateEEnvironmentError,
    GateEEnvironmentLayout,
    InstalledEnvironmentEvidence,
    RuntimeExecutionGuard,
    WheelEvidence,
    WheelhouseEvidence,
    canonical_python_executable,
    canonical_uv_executable,
    capture_runtime_execution_guard,
    copy_gate_e_config,
    inspect_installed_environment,
    inspect_project_wheel,
    install_gate_e_environment,
    make_environment_layout,
    run_sandboxed,
    snapshot_python_runtime,
    snapshot_uv_runtime,
    stage_environment_inputs,
    verify_runtime_execution_guard,
    verify_wheelhouse,
    wheelhouse_requirements_from_manifest,
)
from aquant.gate_e.inputs import (
    GateEInputError,
    verify_gate_e_release_inputs,
    verify_post_run_input_root,
)
from aquant.gate_e.trust import (
    GateETrustError,
    GateETrustEvidence,
    gate_e_trust_bytes,
    verify_gate_e_trust,
)
from aquant.portfolio import PORTFOLIO_ARTIFACT_FILES

_CANDIDATE_A_STAGES = (
    "trust_roots_verified",
    "wheel_verified",
    "environment_a_installed",
    "inputs_a_verified",
    "candidate_a_run",
    "candidate_a_reversed",
    "candidate_a_audited",
)
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SAFE_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}")
_TRUST_RELATIVE = PurePosixPath(
    "release/v0.2-gate-e/trust_manifest.json"
)
_CANDIDATE_REVIEW_RELATIVE = PurePosixPath(
    "outputs/Work_Buddy候选A复核_v0.2_Gate_E.md"
)
_TRUST_APPROVAL_RELATIVE = PurePosixPath(
    "outputs/Work_Buddy信任锚复核_v0.2_Gate_E.md"
)
_CONFIG_RELATIVE = PurePosixPath("configs/releases/v0.2_gate_e.json")
_RELEASE_RELATIVE = PurePosixPath("release/v0.1-research")
_CANDIDATE_EVIDENCE_NAME = "candidate-a-evidence.json"
_CANDIDATE_B_EVIDENCE_NAME = "candidate-b-evidence.json"
_EXPECTED_SYMBOLS = (
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
_EXPECTED_NO_BAR_COUNTS = (
    ("000001", 3),
    ("000858", 3),
    ("510300", 2),
    ("510500", 2),
    ("600030", 3),
    ("600036", 3),
    ("600519", 3),
    ("600900", 3),
    ("601166", 3),
    ("601318", 3),
)
_EXPECTED_SESSIONS = 2072
_EXPECTED_POSITION_ROWS = _EXPECTED_SESSIONS * len(_EXPECTED_SYMBOLS)
_RESEARCH_BOUNDARY = {
    "live_trading": False,
    "profit_claim": False,
    "research_only": True,
    "simulation_only": True,
}
_CANDIDATE_KEYS = frozenset(
    {
        "accounting",
        "artifact_files",
        "business_evidence",
        "candidate",
        "commands",
        "config_sha256",
        "counts",
        "gate",
        "hash_seed",
        "implementation_commit",
        "installed_packages",
        "paths",
        "progress",
        "project_name",
        "project_version",
        "research_boundary",
        "run_id",
        "runtime",
        "schema_version",
        "trust_created",
        "v01_tag_commit",
        "wheelhouse",
    }
)
_CANDIDATE_PATH_KEYS = frozenset(
    {
        "artifact",
        "config",
        "evidence",
        "install_lock",
        "project_wheel",
        "python_executable",
        "release_root",
        "repository_root",
        "uv_executable",
        "uv_lock",
        "wheelhouse_manifest",
        "wheelhouse_root",
        "workspace",
    }
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
_TRUST_APPROVAL_KEYS = frozenset(
    {
        "P0",
        "P1",
        "P2",
        "candidate_review_path",
        "candidate_review_sha256",
        "decision",
        "expected_run_id",
        "gate",
        "implementation_commit",
        "project",
        "review_kind",
        "trust_anchor_commit",
        "trust_path",
        "trust_sha256",
        "version",
    }
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
class GateEReplayError(RuntimeError):
    """Stable, sanitized failure from the Gate E controller."""

    def __init__(self, code: str, *, cause_code: str | None = None):
        self.code = code
        self.cause_code = cause_code
        super().__init__(code)


@dataclass(frozen=True)
class GateEProgressEvent:
    """One completed stage in the fixed Gate E replay order."""

    stage: str
    completed: int
    total: int


@dataclass(frozen=True)
class GateEReplay:
    """All externally supplied roots for one A/B isolated replay."""

    repository_root: Path
    release_root: Path
    config_path: Path
    project_wheel: Path
    wheelhouse_root: Path
    wheelhouse_manifest: Path
    install_lock: Path
    uv_lock: Path
    workspace_a: Path
    workspace_b: Path
    implementation_commit: str
    v01_tag_commit: str
    python_executable: Path
    uv_executable: Path
    expected_requirements: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CandidateAResult:
    """Minimal public handle for an untrusted Candidate A."""

    evidence_path: Path
    artifact: Path
    run_id: str
    progress: tuple[GateEProgressEvent, ...] = ()


@dataclass(frozen=True)
class ReplayBResult:
    """Verified handle for environment B after its Git trust anchor."""

    evidence_path: Path
    artifact: Path
    run_id: str
    trust_sha256: str
    compared_files: int


@dataclass(frozen=True)
class VerifiedCandidateEvidence:
    """Strictly reloaded Candidate A/B evidence and its live path bindings."""

    evidence_path: Path
    candidate: str
    implementation_commit: str
    v01_tag_commit: str
    run_id: str
    repository_root: Path
    release_root: Path
    config: Path
    project_wheel: Path
    wheelhouse_root: Path
    wheelhouse_manifest: Path
    install_lock: Path
    uv_lock: Path
    python_executable: Path
    uv_executable: Path
    runtime_python: dict[str, object]
    runtime_uv: dict[str, object]
    workspace: Path
    artifact: Path
    payload: dict[str, object]

    def to_trust_evidence(
        self,
        approved_review: Path,
        *,
        reviewed_candidate_evidence: Path | None = None,
    ) -> GateETrustEvidence:
        return GateETrustEvidence(
            implementation_commit=self.implementation_commit,
            project_wheel=self.project_wheel,
            uv_lock=self.uv_lock,
            python_executable=self.python_executable,
            uv_executable=self.uv_executable,
            expected_python_snapshot=self.runtime_python,
            expected_uv_snapshot=self.runtime_uv,
            wheelhouse_root=self.wheelhouse_root,
            wheelhouse_manifest=self.wheelhouse_manifest,
            v01_tag_commit=self.v01_tag_commit,
            v01_release_manifest=(
                self.release_root / "release_manifest.json"
            ),
            config=self.config,
            artifact=self.artifact,
            candidate_review=approved_review,
            reviewed_candidate_evidence=(
                self.evidence_path
                if reviewed_candidate_evidence is None
                else reviewed_candidate_evidence
            ),
        )


@dataclass(frozen=True)
class _AnchoredTemporaryRoot:
    """Directory-FD-bound root for Candidate B trust controls."""

    path: Path
    name: str
    parent_descriptor: int
    descriptor: int
    parent_identity: tuple[int, int, int]
    root_identity: tuple[int, int, int]


@dataclass(frozen=True)
class _AnchoredTemporaryFile:
    """One immutable control file created relative to an anchored root."""

    path: Path
    name: str
    identity: tuple[int, ...]


def _read_regular_bytes(
    path: Path,
    *,
    code: str,
    maximum_bytes: int,
) -> bytes:
    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise TypeError("maximum_bytes must be a positive integer")
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or path.is_symlink()
            or before.st_size > maximum_bytes
        ):
            raise GateEReplayError(code)
        descriptor = os.open(
            path,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
        )
        opened = os.fstat(descriptor)
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
                raise GateEReplayError(code)
        content = b"".join(chunks)
        final = os.fstat(descriptor)
        named = path.lstat()
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(final.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or opened.st_nlink != 1
            or final.st_nlink != 1
            or named.st_nlink != 1
            or identity != (before.st_dev, before.st_ino)
            or identity != (final.st_dev, final.st_ino)
            or identity != (named.st_dev, named.st_ino)
            or len(content) != final.st_size
            or len(content) > maximum_bytes
            or opened.st_size != final.st_size
            or opened.st_mtime_ns != final.st_mtime_ns
            or opened.st_ctime_ns != final.st_ctime_ns
        ):
            raise GateEReplayError(code)
        return content
    except GateEReplayError:
        raise
    except OSError as exc:
        raise GateEReplayError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_small_regular(path: Path, *, code: str) -> bytes:
    return _read_regular_bytes(
        path,
        code=code,
        maximum_bytes=4 * 1024 * 1024,
    )


def _parse_review_assignments(
    content: bytes,
    *,
    expected_keys: frozenset[str],
    code: str,
) -> dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise GateEReplayError(code) from exc
    bindings: dict[str, str] = {}
    for line in text.splitlines():
        matched = _REVIEW_ASSIGNMENT_RE.fullmatch(line)
        if matched is None:
            continue
        key, value = matched.groups()
        if key not in expected_keys or key in bindings:
            raise GateEReplayError(code)
        bindings[key] = value
    if set(bindings) != expected_keys:
        raise GateEReplayError(code)
    return bindings


def _parse_candidate_review_bytes(content: bytes) -> dict[str, str]:
    bindings = _parse_review_assignments(
        content,
        expected_keys=_CANDIDATE_REVIEW_KEYS,
        code="candidate_review_not_approved",
    )
    if (
        bindings["project"] != "a-share-quant"
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
        raise GateEReplayError("candidate_review_not_approved")
    return bindings


def _parse_candidate_review(path: Path) -> dict[str, str]:
    """Parse one complete, uniquely bound Candidate A review."""
    return _parse_candidate_review_bytes(
        _read_small_regular(
            path,
            code="candidate_review_not_approved",
        )
    )


def _parse_trust_approval_bytes(content: bytes) -> dict[str, str]:
    bindings = _parse_review_assignments(
        content,
        expected_keys=_TRUST_APPROVAL_KEYS,
        code="trust_anchor_not_approved",
    )
    if (
        bindings["project"] != "a-share-quant"
        or bindings["version"] != "v0.2"
        or bindings["gate"] != "E"
        or bindings["review_kind"] != "trust_anchor"
        or bindings["decision"] != "PASS"
        or any(bindings[field] != "0" for field in ("P0", "P1", "P2"))
        or _COMMIT_RE.fullmatch(bindings["trust_anchor_commit"])
        is None
        or _COMMIT_RE.fullmatch(bindings["implementation_commit"])
        is None
        or any(
            _HASH_RE.fullmatch(bindings[field]) is None
            for field in (
                "trust_sha256",
                "candidate_review_sha256",
                "expected_run_id",
            )
        )
        or bindings["trust_path"] != _TRUST_RELATIVE.as_posix()
        or bindings["candidate_review_path"]
        != _CANDIDATE_REVIEW_RELATIVE.as_posix()
    ):
        raise GateEReplayError("trust_anchor_not_approved")
    return bindings


def verify_approved_review(path: Path) -> Path:
    """Require a complete, identity-bound Work Buddy Candidate A decision."""
    _parse_candidate_review(path)
    return path


def _fixed_candidate_review_argument(
    path: Path,
    *,
    repository_root: Path,
) -> Path:
    if (
        not isinstance(path, Path)
        or "\x00" in os.fspath(path)
        or any(part == ".." for part in path.parts)
    ):
        raise GateEReplayError("candidate_review_not_approved")
    absolute = path if path.is_absolute() else repository_root / path
    expected = (
        repository_root / _CANDIDATE_REVIEW_RELATIVE.as_posix()
    )
    if absolute != expected:
        raise GateEReplayError("candidate_review_not_approved")
    return _safe_existing_file(
        absolute,
        code="candidate_review_not_approved",
    )


def _canonical_json_bytes(payload: object) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode()
    except (TypeError, UnicodeError, ValueError) as exc:
        raise GateEReplayError("candidate_evidence_invalid") from exc


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GateEReplayError("candidate_evidence_invalid")
        result[key] = value
    return result


def _parse_canonical_json(content: bytes) -> dict[str, object]:
    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError
            ),
        )
    except GateEReplayError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise GateEReplayError("candidate_evidence_invalid") from exc
    if (
        type(payload) is not dict
        or _canonical_json_bytes(payload) != content
    ):
        raise GateEReplayError("candidate_evidence_invalid")
    return payload


def _safe_existing_directory(path: Path, *, code: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise GateEReplayError(code) from exc
    if (
        path != resolved
        or path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise GateEReplayError(code)
    return path


def _safe_existing_file(
    path: Path,
    *,
    code: str,
    allow_parent_alias: bool = False,
) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise GateEReplayError(code) from exc
    if (
        (not allow_parent_alias and path != resolved)
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise GateEReplayError(code)
    return path


def _absolute_argument(
    path: Path,
    *,
    base: Path,
) -> Path:
    if not isinstance(path, Path):
        raise TypeError("path arguments must be Path objects")
    if "\x00" in os.fspath(path) or any(
        part == ".." for part in path.parts
    ):
        raise GateEReplayError("unsafe_replay_path")
    return path if path.is_absolute() else base / path


def _workspace_argument(path: Path, *, repository_root: Path) -> Path:
    candidate = _absolute_argument(path, base=repository_root)
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GateEReplayError("unsafe_replay_workspace") from exc
    intended = parent / candidate.name
    if (
        candidate.parent != parent
        or candidate.name in {"", ".", ".."}
        or intended == repository_root
        or intended in repository_root.parents
        or repository_root in intended.parents
    ):
        raise GateEReplayError("unsafe_replay_workspace")
    return intended


def _git(
    repository_root: Path,
    *arguments: str,
    code: str,
    binary: bool = False,
) -> str | bytes:
    try:
        completed = subprocess.run(
            ["/usr/bin/git", *arguments],
            cwd=repository_root,
            env={
                "GIT_NO_REPLACE_OBJECTS": "1",
                "HOME": os.fspath(repository_root),
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            },
            capture_output=True,
            text=not binary,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateEReplayError(code) from exc
    if completed.returncode != 0:
        raise GateEReplayError(code)
    if binary:
        if type(completed.stdout) is not bytes:
            raise GateEReplayError(code)
        return completed.stdout
    if type(completed.stdout) is not str:
        raise GateEReplayError(code)
    return completed.stdout.strip()


def _repository_from_cwd() -> Path:
    candidate = Path.cwd().resolve(strict=True)
    root_text = _git(
        candidate,
        "rev-parse",
        "--show-toplevel",
        code="repository_identity_invalid",
    )
    if type(root_text) is not str:
        raise GateEReplayError("repository_identity_invalid")
    return _safe_existing_directory(
        Path(root_text),
        code="repository_identity_invalid",
    )


def _sha256_file(path: Path, *, code: str) -> str:
    _safe_existing_file(
        path,
        code=code,
    )
    digest = hashlib.sha256()
    descriptor = -1
    try:
        before = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
        )
        opened = os.fstat(descriptor)
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        final = os.fstat(descriptor)
        named = path.lstat()
        identity = (opened.st_dev, opened.st_ino)
        if (
            identity != (before.st_dev, before.st_ino)
            or identity != (final.st_dev, final.st_ino)
            or identity != (named.st_dev, named.st_ino)
            or opened.st_size != final.st_size
            or opened.st_mtime_ns != final.st_mtime_ns
            or opened.st_ctime_ns != final.st_ctime_ns
        ):
            raise GateEReplayError(code)
    except GateEReplayError:
        raise
    except OSError as exc:
        raise GateEReplayError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest()


def create_gate_e_replay(
    *,
    config: Path,
    project_wheel: Path,
    wheelhouse: Path,
    workspace_a: Path,
    workspace_b: Path | None = None,
) -> GateEReplay:
    """Construct one replay after path and Git verification."""
    repository_root = _repository_from_cwd()
    config_path = _absolute_argument(config, base=repository_root)
    if config_path != repository_root / _CONFIG_RELATIVE.as_posix():
        raise GateEReplayError("unexpected_gate_e_config")
    _safe_existing_file(
        config_path,
        code="unsafe_gate_e_config",
    )
    wheel_path = _absolute_argument(
        project_wheel,
        base=repository_root,
    )
    _safe_existing_file(
        wheel_path,
        code="unsafe_project_wheel",
    )
    wheelhouse_root = _absolute_argument(
        wheelhouse,
        base=repository_root,
    )
    _safe_existing_directory(
        wheelhouse_root,
        code="unsafe_wheelhouse",
    )
    manifest = wheelhouse_root.parent / "wheelhouse_manifest.json"
    install_lock = (
        wheelhouse_root.parent / "requirements.install.lock.txt"
    )
    _safe_existing_file(
        manifest,
        code="unsafe_wheelhouse_manifest",
    )
    _safe_existing_file(
        install_lock,
        code="unsafe_wheelhouse_install_lock",
    )
    expected_requirements = tuple(
        sorted(
            wheelhouse_requirements_from_manifest(manifest).items()
        )
    )
    first_workspace = _workspace_argument(
        workspace_a,
        repository_root=repository_root,
    )
    if workspace_b is None:
        if os.path.lexists(first_workspace):
            raise GateEReplayError("candidate_workspace_conflict")
        second_workspace = first_workspace.with_name(
            f"{first_workspace.name}-b"
        )
    else:
        second_workspace = _workspace_argument(
            workspace_b,
            repository_root=repository_root,
        )
        if first_workspace == second_workspace:
            raise GateEReplayError("candidate_workspace_overlap")
    implementation_commit = _git(
        repository_root,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        code="implementation_commit_invalid",
    )
    v01_tag_commit = _git(
        repository_root,
        "rev-parse",
        "--verify",
        "refs/tags/v0.1-research^{commit}",
        code="v01_tag_invalid",
    )
    if (
        type(implementation_commit) is not str
        or _COMMIT_RE.fullmatch(implementation_commit) is None
        or type(v01_tag_commit) is not str
        or _COMMIT_RE.fullmatch(v01_tag_commit) is None
    ):
        raise GateEReplayError("repository_identity_invalid")
    return GateEReplay(
        repository_root=repository_root,
        release_root=repository_root / _RELEASE_RELATIVE.as_posix(),
        config_path=config_path,
        project_wheel=wheel_path,
        wheelhouse_root=wheelhouse_root,
        wheelhouse_manifest=manifest,
        install_lock=install_lock,
        uv_lock=repository_root / "uv.lock",
        workspace_a=first_workspace,
        workspace_b=second_workspace,
        implementation_commit=implementation_commit,
        v01_tag_commit=v01_tag_commit,
        python_executable=canonical_python_executable(),
        uv_executable=canonical_uv_executable(),
        expected_requirements=expected_requirements,
    )


def _require_candidate_a_repository(replay: GateEReplay) -> None:
    status = _git(
        replay.repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        code="implementation_worktree_dirty",
    )
    if status:
        raise GateEReplayError("implementation_worktree_dirty")
    head = _git(
        replay.repository_root,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        code="implementation_commit_invalid",
    )
    tag = _git(
        replay.repository_root,
        "rev-parse",
        "--verify",
        "refs/tags/v0.1-research^{commit}",
        code="v01_tag_invalid",
    )
    if (
        head != replay.implementation_commit
        or tag != replay.v01_tag_commit
    ):
        raise GateEReplayError("repository_identity_changed")
    for relative in (
        _CONFIG_RELATIVE.as_posix(),
        "uv.lock",
        f"{_RELEASE_RELATIVE.as_posix()}/release_manifest.json",
    ):
        tracked = _git(
            replay.repository_root,
            "ls-files",
            "--error-unmatch",
            "--",
            relative,
            code="untracked_trust_root",
        )
        if tracked != relative:
            raise GateEReplayError("untracked_trust_root")
    if os.path.lexists(
        replay.repository_root / _TRUST_RELATIVE.as_posix()
    ):
        raise GateEReplayError("candidate_a_after_trust_anchor")


def _verify_trust_roots(
    replay: GateEReplay,
    *,
    require_clean: bool,
) -> GateEConfig:
    if type(replay) is not GateEReplay:
        raise GateEReplayError("invalid_replay_contract")
    if require_clean:
        _require_candidate_a_repository(replay)
    _safe_existing_directory(
        replay.repository_root,
        code="repository_identity_invalid",
    )
    _safe_existing_directory(
        replay.release_root,
        code="unsafe_release_root",
    )
    _safe_existing_file(
        replay.config_path,
        code="unsafe_gate_e_config",
    )
    _safe_existing_file(
        replay.uv_lock,
        code="unsafe_uv_lock",
    )
    _safe_existing_file(
        replay.python_executable,
        code="unsafe_python_executable",
        allow_parent_alias=True,
    )
    _safe_existing_file(
        replay.uv_executable,
        code="unsafe_uv_executable",
    )
    if (
        _COMMIT_RE.fullmatch(replay.implementation_commit) is None
        or _COMMIT_RE.fullmatch(replay.v01_tag_commit) is None
    ):
        raise GateEReplayError("repository_identity_invalid")
    tag = _git(
        replay.repository_root,
        "rev-parse",
        "--verify",
        "refs/tags/v0.1-research^{commit}",
        code="v01_tag_invalid",
    )
    _git(
        replay.repository_root,
        "cat-file",
        "-e",
        f"{replay.implementation_commit}^{{commit}}",
        code="implementation_commit_invalid",
    )
    if tag != replay.v01_tag_commit:
        raise GateEReplayError("v01_tag_changed")
    config = load_gate_e_config(replay.config_path)
    release_manifest = replay.release_root / "release_manifest.json"
    if (
        _sha256_file(
            release_manifest,
            code="v01_release_manifest_mismatch",
        )
        != config.payload["release_manifest_sha256"]
        or _sha256_file(
            replay.uv_lock,
            code="uv_lock_mismatch",
        )
        != config.payload["uv_lock_sha256"]
    ):
        raise GateEReplayError("trust_root_hash_mismatch")
    verified_inputs = verify_gate_e_release_inputs(
        replay.release_root
    )
    if len(verified_inputs) != 25:
        raise GateEReplayError("release_input_count_mismatch")
    return config


def _verified_wheel_inputs(
    replay: GateEReplay,
) -> tuple[WheelEvidence, WheelhouseEvidence]:
    project = inspect_project_wheel(replay.project_wheel)
    wheelhouse = verify_wheelhouse(
        replay.wheelhouse_root,
        expected_requirements=dict(replay.expected_requirements),
        manifest=replay.wheelhouse_manifest,
        install_lock=replay.install_lock,
    )
    if (
        wheelhouse.manifest_sha256 is None
        or wheelhouse.install_lock_sha256 is None
        or len(wheelhouse.entries) != len(replay.expected_requirements)
    ):
        raise GateEReplayError("wheelhouse_evidence_incomplete")
    return project, wheelhouse


def _capture_runtime_snapshots(
    replay: GateEReplay,
) -> dict[str, dict[str, object]]:
    try:
        python = snapshot_python_runtime(replay.python_executable)
        uv = snapshot_uv_runtime(replay.uv_executable)
    except (GateEEnvironmentError, OSError, TypeError, ValueError) as exc:
        raise GateEReplayError(
            "candidate_runtime_changed",
            cause_code=getattr(exc, "code", None),
        ) from exc
    return {"python": python, "uv": uv}


def _verify_runtime_execution_guards(
    replay: GateEReplay,
    guards: dict[str, RuntimeExecutionGuard],
) -> None:
    if (
        type(guards) is not dict
        or set(guards) != {"python", "uv"}
        or type(guards["python"]) is not RuntimeExecutionGuard
        or type(guards["uv"]) is not RuntimeExecutionGuard
    ):
        raise GateEReplayError("invalid_replay_state")
    try:
        verify_runtime_execution_guard(
            replay.python_executable,
            guards["python"],
        )
        verify_runtime_execution_guard(
            replay.uv_executable,
            guards["uv"],
        )
    except (GateEEnvironmentError, OSError, TypeError, ValueError) as exc:
        raise GateEReplayError(
            "candidate_runtime_changed",
            cause_code=getattr(exc, "code", None),
        ) from exc


def _capture_runtime_controls(
    replay: GateEReplay,
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, RuntimeExecutionGuard],
]:
    try:
        guards = {
            "python": capture_runtime_execution_guard(
                replay.python_executable
            ),
            "uv": capture_runtime_execution_guard(replay.uv_executable),
        }
    except (GateEEnvironmentError, OSError, TypeError, ValueError) as exc:
        raise GateEReplayError(
            "candidate_runtime_changed",
            cause_code=getattr(exc, "code", None),
        ) from exc
    runtime = _capture_runtime_snapshots(replay)
    _verify_runtime_execution_guards(replay, guards)
    return runtime, guards


def _verify_candidate_runtime(
    candidate: VerifiedCandidateEvidence,
) -> dict[str, dict[str, object]]:
    replay = _replay_from_candidate(candidate)
    observed = _capture_runtime_snapshots(replay)
    expected = {
        "python": candidate.runtime_python,
        "uv": candidate.runtime_uv,
    }
    if observed != expected:
        raise GateEReplayError("candidate_runtime_changed")
    return observed


def _input_paths(
    config: GateEConfig,
    project_root: Path,
) -> tuple[Path, ...]:
    raw = config.payload["input_files"]
    return tuple(
        project_root / relative
        for relative in sorted(raw)
    )


def _snapshot_staged_input_state(
    config: GateEConfig,
    project_root: Path,
) -> tuple[tuple[object, ...], ...]:
    """Bind staged input identity while tolerating idempotent chmod ctime."""
    if type(config) is not GateEConfig or not isinstance(
        project_root,
        Path,
    ):
        raise GateEReplayError("invalid_replay_state")
    observed: list[tuple[object, ...]] = []
    for relative in sorted(config.payload["input_files"]):
        path = project_root / relative
        try:
            before = path.lstat()
        except OSError as exc:
            raise GateEReplayError(
                "environment_input_state_changed"
            ) from exc
        digest = _sha256_file(
            path,
            code="environment_input_state_changed",
        )
        try:
            after = path.lstat()
        except OSError as exc:
            raise GateEReplayError(
                "environment_input_state_changed"
            ) from exc
        stable_before = (
            before.st_mode,
            before.st_dev,
            before.st_ino,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        )
        stable_after = (
            after.st_mode,
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        )
        if (
            stable_before != stable_after
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or after.st_mode & 0o222
            or path.is_symlink()
        ):
            raise GateEReplayError(
                "environment_input_state_changed"
            )
        observed.append((relative, *stable_after, digest))
    return tuple(observed)


def _candidate_b_write_denials(
    replay: GateEReplay,
    *,
    workspace: Path,
) -> tuple[Path, ...]:
    if workspace == replay.workspace_a:
        return ()
    if workspace == replay.workspace_b:
        return (replay.workspace_a,)
    raise GateEReplayError("invalid_replay_state")


def _scan_tree(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    _safe_existing_directory(root, code="unsafe_project_tree")
    files: list[str] = []
    directories: list[str] = []
    try:
        for path in root.rglob("*"):
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                if path.is_symlink():
                    raise GateEReplayError("unsafe_project_tree")
                directories.append(path.relative_to(root).as_posix())
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or path.is_symlink()
            ):
                raise GateEReplayError("unsafe_project_tree")
            files.append(path.relative_to(root).as_posix())
    except GateEReplayError:
        raise
    except OSError as exc:
        raise GateEReplayError("unsafe_project_tree") from exc
    return tuple(sorted(files)), tuple(sorted(directories))


def _scan_regular_tree(root: Path) -> tuple[str, ...]:
    files, directories = _scan_tree(root)
    if directories:
        raise GateEReplayError("unsafe_project_tree")
    return files


def _expected_directories(paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for relative in paths:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _verify_staged_inputs(
    replay: GateEReplay,
    layout: GateEEnvironmentLayout,
    config: GateEConfig,
    *,
    run_id: str | None,
    expected_input_state: tuple[tuple[object, ...], ...] | None = None,
) -> None:
    expected_inputs = dict(config.payload["input_files"])
    expected_files = set(expected_inputs)
    expected_files.add(_CONFIG_RELATIVE.as_posix())
    if run_id is not None:
        expected_files.add(
            f"outputs/portfolios/.{run_id}.lock"
        )
        expected_files.update(
            f"outputs/portfolios/{run_id}/{name}"
            for name in PORTFOLIO_ARTIFACT_FILES
        )
    actual_files, actual_directories = _scan_tree(layout.project_root)
    expected_directories = _expected_directories(expected_files)
    if run_id is None:
        expected_directories.add("outputs")
    if (
        set(actual_files) != expected_files
        or set(actual_directories) != expected_directories
    ):
        raise GateEReplayError("environment_project_tree_mismatch")
    for relative, expected_hash in expected_inputs.items():
        destination = layout.project_root / relative
        source = replay.release_root / "inputs" / relative
        if (
            _sha256_file(
                destination,
                code="environment_input_hash_mismatch",
            )
            != expected_hash
            or _sha256_file(
                source,
                code="release_input_hash_mismatch",
            )
            != expected_hash
        ):
            raise GateEReplayError("environment_input_hash_mismatch")
        source_metadata = source.lstat()
        destination_metadata = destination.lstat()
        if (
            (source_metadata.st_dev, source_metadata.st_ino)
            == (
                destination_metadata.st_dev,
                destination_metadata.st_ino,
            )
            or destination_metadata.st_mode & 0o222
        ):
            raise GateEReplayError("environment_input_copy_not_independent")
    if (
        _sha256_file(
            layout.config_path,
            code="environment_config_hash_mismatch",
        )
        != config.config_sha256
        or layout.config_path.lstat().st_mode & 0o222
    ):
        raise GateEReplayError("environment_config_hash_mismatch")
    if (
        expected_input_state is not None
        and _snapshot_staged_input_state(
            config,
            layout.project_root,
        )
        != expected_input_state
    ):
        raise GateEReplayError("environment_input_state_changed")
    if run_id is not None:
        lock = (
            layout.output_root
            / "portfolios"
            / f".{run_id}.lock"
        )
        if lock.lstat().st_size != 0:
            raise GateEReplayError("unexpected_output_lock")
        try:
            verified = verify_post_run_input_root(
                replay.release_root,
                layout.project_root,
                expected_run_id=run_id,
            )
        except GateEInputError as exc:
            raise GateEReplayError(
                "post_run_input_verification_failed",
                cause_code=exc.code,
            ) from exc
        if verified.file_count != 25:
            raise GateEReplayError(
                "post_run_input_verification_failed"
            )


def _parse_cli_output(
    stdout: str,
    stderr: str,
    *,
    expected_keys: frozenset[str],
    failure_code: str,
) -> dict[str, object]:
    if stderr != "":
        raise GateEReplayError(failure_code)
    try:
        payload = json.loads(
            stdout,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError
            ),
        )
    except (GateEReplayError, json.JSONDecodeError, ValueError) as exc:
        raise GateEReplayError(failure_code) from exc
    if type(payload) is not dict or set(payload) != expected_keys:
        raise GateEReplayError(failure_code)
    return payload


def _run_portfolio_candidate(
    replay: GateEReplay,
    layout: GateEEnvironmentLayout,
    installed: InstalledEnvironmentEvidence,
    config: GateEConfig,
    *,
    hash_seed: str,
) -> tuple[str, Path, tuple[str, ...]]:
    command = (
        os.fspath(installed.portfolio_cli),
        "run-config",
        "--config",
        _CONFIG_RELATIVE.as_posix(),
    )
    completed = run_sandboxed(
        layout,
        command,
        hash_seed=hash_seed,
        timeout_seconds=600,
        read_only_paths=(
            *_candidate_b_write_denials(
                replay,
                workspace=layout.root,
            ),
            replay.project_wheel,
            replay.wheelhouse_root,
            replay.wheelhouse_manifest,
            replay.install_lock,
            layout.config_path,
        ),
        verification_mode_files=_input_paths(
            config,
            layout.project_root,
        ),
    )
    if completed.returncode != 0:
        raise GateEReplayError("candidate_portfolio_run_failed")
    payload = _parse_cli_output(
        completed.stdout,
        completed.stderr,
        expected_keys=frozenset(
            {
                "artifact_directory",
                "run_id",
                "status",
                "symbol_count",
            }
        ),
        failure_code="candidate_portfolio_output_invalid",
    )
    run_id = payload["run_id"]
    relative = payload["artifact_directory"]
    if (
        type(run_id) is not str
        or _HASH_RE.fullmatch(run_id) is None
        or type(relative) is not str
        or relative
        != f"outputs/portfolios/{run_id}"
        or payload["status"] != "ok"
        or payload["symbol_count"] != 10
    ):
        raise GateEReplayError("candidate_portfolio_output_invalid")
    artifact = layout.project_root / relative
    _safe_existing_directory(
        artifact,
        code="candidate_artifact_missing",
    )
    return run_id, artifact, command


def _reverse_candidate(
    replay: GateEReplay,
    layout: GateEEnvironmentLayout,
    installed: InstalledEnvironmentEvidence,
    config: GateEConfig,
    *,
    run_id: str,
    artifact: Path,
    hash_seed: str,
) -> tuple[str, ...]:
    relative = artifact.relative_to(layout.project_root).as_posix()
    command = (
        os.fspath(installed.portfolio_cli),
        "verify",
        "--project-root",
        ".",
        "--artifact",
        relative,
        "--expected-run-id",
        run_id,
    )
    completed = run_sandboxed(
        layout,
        command,
        hash_seed=hash_seed,
        timeout_seconds=600,
        read_only_paths=(
            *_candidate_b_write_denials(
                replay,
                workspace=layout.root,
            ),
            replay.project_wheel,
            replay.wheelhouse_root,
            replay.wheelhouse_manifest,
            replay.install_lock,
            layout.config_path,
            artifact,
            *_input_paths(config, layout.project_root),
        ),
    )
    if completed.returncode != 0:
        raise GateEReplayError("candidate_reverse_verification_failed")
    payload = _parse_cli_output(
        completed.stdout,
        completed.stderr,
        expected_keys=frozenset(
            {
                "artifact_file_count",
                "artifact_manifest_sha256",
                "file_count",
                "payload_file_count",
                "run_id",
                "status",
                "trade_count",
            }
        ),
        failure_code="candidate_reverse_output_invalid",
    )
    if (
        payload["run_id"] != run_id
        or payload["status"] != "verified"
        or payload["artifact_file_count"] != 13
        or payload["payload_file_count"] != 12
        or payload["file_count"] != 13
        or type(payload["trade_count"]) is not int
        or type(payload["artifact_manifest_sha256"]) is not str
        or _HASH_RE.fullmatch(
            payload["artifact_manifest_sha256"]
        )
        is None
    ):
        raise GateEReplayError("candidate_reverse_output_invalid")
    return command


def _csv_rows(path: Path) -> list[dict[str, str]]:
    content = _read_regular_bytes(
        path,
        code="candidate_artifact_invalid",
        maximum_bytes=256 * 1024 * 1024,
    )
    try:
        stream = io.StringIO(content.decode("utf-8"), newline="")
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or len(reader.fieldnames) != len(
            set(reader.fieldnames)
        ):
            raise ValueError
        rows = list(reader)
    except (UnicodeError, csv.Error, ValueError) as exc:
        raise GateEReplayError("candidate_artifact_invalid") from exc
    return rows


def _status_counts(
    rows: list[dict[str, str]],
    field: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(field)
        if type(value) is not str:
            raise GateEReplayError("candidate_artifact_invalid")
        counts[value] = counts.get(value, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise GateEReplayError("candidate_artifact_invalid")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _artifact_evidence(
    artifact: Path,
    accounting: GateEAccountingAudit,
) -> tuple[
    list[dict[str, object]],
    dict[str, int],
    dict[str, object],
]:
    manifest = _parse_canonical_json(
        _read_small_regular(
            artifact / "artifact_manifest.json",
            code="candidate_artifact_invalid",
        )
    )
    raw_files = manifest.get("files")
    if (
        type(raw_files) is not dict
        or set(raw_files) != PORTFOLIO_ARTIFACT_FILES - {
            "artifact_manifest.json"
        }
    ):
        raise GateEReplayError("candidate_artifact_invalid")
    row_counts: dict[str, int] = {}
    for name, item in raw_files.items():
        if (
            type(item) is not dict
            or type(item.get("row_count")) is not int
        ):
            raise GateEReplayError("candidate_artifact_invalid")
        row_counts[name] = item["row_count"]
    artifact_files = [
        {
            "name": name,
            "sha256": _sha256_file(
                artifact / name,
                code="candidate_artifact_invalid",
            ),
            "size": (artifact / name).lstat().st_size,
        }
        for name in sorted(PORTFOLIO_ARTIFACT_FILES)
    ]
    targets = _csv_rows(artifact / "targets.csv")
    orders = _csv_rows(artifact / "orders.csv")
    positions = _csv_rows(artifact / "positions.csv")
    availability = _csv_rows(artifact / "availability.csv")
    equity = _csv_rows(artifact / "equity.csv")
    metrics = _parse_canonical_json(
        _read_small_regular(
            artifact / "metrics.json",
            code="candidate_artifact_invalid",
        )
    )
    if (
        len(targets) != len(_EXPECTED_SYMBOLS)
        or tuple(sorted(row["symbol"] for row in targets))
        != _EXPECTED_SYMBOLS
        or len(positions) != _EXPECTED_POSITION_ROWS
        or len(equity) != _EXPECTED_SESSIONS
        or row_counts.get("targets.csv") != len(_EXPECTED_SYMBOLS)
        or row_counts.get("positions.csv") != _EXPECTED_POSITION_ROWS
        or row_counts.get("equity.csv") != _EXPECTED_SESSIONS
    ):
        raise GateEReplayError("candidate_structure_mismatch")
    final_session = equity[-1]["session"]
    final_positions = tuple(
        row for row in positions if row["session"] == final_session
    )
    if (
        len(final_positions) != len(_EXPECTED_SYMBOLS)
        or accounting.ending_equity_fen <= 0
    ):
        raise GateEReplayError("candidate_structure_mismatch")
    final_weights = [
        {
            "symbol": row["symbol"],
            "value": _decimal_text(
                Decimal(row["market_value_fen"])
                / Decimal(accounting.ending_equity_fen)
            ),
        }
        for row in sorted(
            final_positions,
            key=lambda item: item["symbol"],
        )
    ]
    business = {
        "availability_status_counts": _status_counts(
            availability,
            "status",
        ),
        "final_cash_weight": _decimal_text(
            Decimal(accounting.ending_cash_fen)
            / Decimal(accounting.ending_equity_fen)
        ),
        "final_symbol_weights": final_weights,
        "no_bar_counts": [
            {"count": count, "symbol": symbol}
            for symbol, count in accounting.no_bar_counts
        ],
        "no_bar_total": accounting.no_bar_total,
        "order_rejection_reason_counts": _status_counts(
            orders,
            "rejection_reason",
        ),
        "order_status_counts": _status_counts(orders, "status"),
        "reported_max_drawdown": metrics.get("max_drawdown"),
        "reported_total_return": metrics.get("total_return"),
        "target_status_counts": _status_counts(targets, "status"),
    }
    return artifact_files, row_counts, business


def _validate_formal_audits(
    *,
    bundle: GateEAccountingAudit,
    inputs: GateEInputAudit,
    run_id: str,
) -> None:
    reconcile_gate_e_no_bar(
        bundle.no_bar_dates,
        bundle.no_bar_carried_sessions,
        inputs.no_bar_dates,
        inputs.no_bar_carried_sessions,
    )
    if (
        bundle.run_id != run_id
        or bundle.observation_count != _EXPECTED_SESSIONS
        or inputs.session_count != _EXPECTED_SESSIONS
        or bundle.no_bar_counts != _EXPECTED_NO_BAR_COUNTS
        or inputs.no_bar_counts != _EXPECTED_NO_BAR_COUNTS
        or bundle.no_bar_total != 28
        or inputs.no_bar_total != 28
        or bundle.ending_cash_fen
        != (
            bundle.initial_cash_fen
            - bundle.invested_notional_fen
            - bundle.paid_fees_fen
            + bundle.dividend_cash_paid_fen
        )
        or bundle.ending_equity_fen
        != (
            bundle.ending_cash_fen
            + bundle.ending_position_market_value_fen
            + bundle.ending_receivable_fen
        )
        or bundle.gross_target_notional_fen
        != (
            bundle.invested_notional_fen
            + bundle.allocation_rounding_fen
            + bundle.ordinary_lot_rounding_fen
            + bundle.fee_lot_reduction_fen
            + bundle.pending_uninvested_fen
            + bundle.expired_uninvested_fen
        )
    ):
        raise GateEReplayError("candidate_audit_mismatch")


def _accounting_payload(
    audit: GateEAccountingAudit,
) -> dict[str, object]:
    return {
        "allocation_identity_verified": True,
        "allocation_rounding_fen": audit.allocation_rounding_fen,
        "cash_identity_verified": True,
        "dividend_cash_paid_fen": audit.dividend_cash_paid_fen,
        "ending_cash_fen": audit.ending_cash_fen,
        "ending_equity_fen": audit.ending_equity_fen,
        "ending_position_market_value_fen": (
            audit.ending_position_market_value_fen
        ),
        "ending_receivable_fen": audit.ending_receivable_fen,
        "expired_uninvested_fen": audit.expired_uninvested_fen,
        "fee_lot_reduction_fen": audit.fee_lot_reduction_fen,
        "gross_target_notional_fen": audit.gross_target_notional_fen,
        "initial_cash_fen": audit.initial_cash_fen,
        "invested_notional_fen": audit.invested_notional_fen,
        "net_asset_identity_verified": True,
        "ordinary_lot_rounding_fen": audit.ordinary_lot_rounding_fen,
        "paid_fees_fen": audit.paid_fees_fen,
        "pending_uninvested_fen": audit.pending_uninvested_fen,
    }


def _wheelhouse_payload(
    evidence: WheelhouseEvidence,
) -> dict[str, object]:
    return {
        "install_lock_sha256": evidence.install_lock_sha256,
        "manifest_sha256": evidence.manifest_sha256,
        "wheels": [
            {
                "filename": item.filename,
                "normalized_name": item.normalized_name,
                "sha256": item.sha256,
                "size": item.size,
                "version": item.version,
            }
            for item in evidence.entries
        ],
    }


def _candidate_payload(
    replay: GateEReplay,
    *,
    candidate: str,
    hash_seed: str,
    layout: GateEEnvironmentLayout,
    installed: InstalledEnvironmentEvidence,
    config: GateEConfig,
    project_wheel: WheelEvidence,
    wheelhouse: WheelhouseEvidence,
    run_id: str,
    artifact: Path,
    run_command: tuple[str, ...],
    verify_command: tuple[str, ...],
    accounting: GateEAccountingAudit,
    input_audit: GateEInputAudit,
    evidence_path: Path,
    runtime: dict[str, dict[str, object]],
) -> dict[str, object]:
    artifact_files, row_counts, business = _artifact_evidence(
        artifact,
        accounting,
    )
    return {
        "accounting": _accounting_payload(accounting),
        "artifact_files": artifact_files,
        "business_evidence": business,
        "candidate": candidate,
        "commands": {
            "run": list(run_command),
            "verify": list(verify_command),
        },
        "config_sha256": config.config_sha256,
        "counts": {
            "artifact_files": 13,
            "equity_rows": row_counts["equity.csv"],
            "input_files": len(config.payload["input_files"]),
            "no_bar_total": input_audit.no_bar_total,
            "payload_files": 12,
            "position_rows": row_counts["positions.csv"],
            "sessions": input_audit.session_count,
            "symbols": len(_EXPECTED_SYMBOLS),
            "target_rows": row_counts["targets.csv"],
        },
        "gate": "E",
        "hash_seed": hash_seed,
        "implementation_commit": replay.implementation_commit,
        "installed_packages": [
            {"name": name, "version": version}
            for name, version in installed.packages
        ],
        "paths": {
            "artifact": os.fspath(artifact),
            "config": os.fspath(replay.config_path),
            "evidence": os.fspath(evidence_path),
            "install_lock": os.fspath(replay.install_lock),
            "project_wheel": os.fspath(replay.project_wheel),
            "python_executable": os.fspath(replay.python_executable),
            "release_root": os.fspath(replay.release_root),
            "repository_root": os.fspath(replay.repository_root),
            "uv_executable": os.fspath(replay.uv_executable),
            "uv_lock": os.fspath(replay.uv_lock),
            "wheelhouse_manifest": os.fspath(
                replay.wheelhouse_manifest
            ),
            "wheelhouse_root": os.fspath(replay.wheelhouse_root),
            "workspace": os.fspath(layout.root),
        },
        "progress": [
            {
                "completed": index,
                "stage": stage,
                "total": len(_CANDIDATE_A_STAGES),
            }
            for index, stage in enumerate(
                _CANDIDATE_A_STAGES,
                start=1,
            )
        ],
        "project_name": "a-share-quant",
        "project_version": "0.2.0",
        "research_boundary": _RESEARCH_BOUNDARY,
        "run_id": run_id,
        "runtime": runtime,
        "schema_version": "1.1",
        "trust_created": False,
        "v01_tag_commit": replay.v01_tag_commit,
        "wheelhouse": {
            **_wheelhouse_payload(wheelhouse),
            "project_wheel": {
                "sha256": project_wheel.sha256,
                "size": project_wheel.size,
                "version": project_wheel.distribution_version,
            },
        },
    }


def _write_candidate_evidence(
    path: Path,
    payload: dict[str, object],
) -> Path:
    content = _canonical_json_bytes(payload)
    parent = _safe_existing_directory(
        path.parent,
        code="unsafe_candidate_evidence",
    )
    if path.parent != parent or os.path.lexists(path):
        raise GateEReplayError("candidate_evidence_conflict")
    temporary = f".{path.name}.tmp-{os.getpid()}-{hashlib.sha256(content).hexdigest()[:16]}"
    parent_descriptor = -1
    descriptor = -1
    created = False
    try:
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
                raise GateEReplayError("candidate_evidence_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o444)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=parent_descriptor)
        created = False
        os.fsync(parent_descriptor)
    except FileExistsError as exc:
        raise GateEReplayError("candidate_evidence_conflict") from exc
    except GateEReplayError:
        raise
    except OSError as exc:
        raise GateEReplayError("candidate_evidence_write_failed") from exc
    finally:
        cleanup_error: OSError | None = None
        if descriptor >= 0:
            os.close(descriptor)
        if created and parent_descriptor >= 0:
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except OSError as exc:
                cleanup_error = exc
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        if cleanup_error is not None:
            raise GateEReplayError(
                "candidate_evidence_cleanup_failed"
            ) from cleanup_error
    if _read_small_regular(
        path,
        code="candidate_evidence_write_failed",
    ) != content:
        raise GateEReplayError("candidate_evidence_write_failed")
    return path


def _candidate_path(
    value: object,
    *,
    directory: bool,
    allow_parent_alias: bool = False,
) -> Path:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
    ):
        raise GateEReplayError("candidate_evidence_invalid")
    path = Path(value)
    if not path.is_absolute() or any(
        part == ".." for part in path.parts
    ):
        raise GateEReplayError("candidate_evidence_invalid")
    if directory:
        return _safe_existing_directory(
            path,
            code="candidate_evidence_invalid",
        )
    return _safe_existing_file(
        path,
        code="candidate_evidence_invalid",
        allow_parent_alias=allow_parent_alias,
    )


def _validate_candidate_progress(value: object) -> None:
    expected = [
        {
            "completed": index,
            "stage": stage,
            "total": len(_CANDIDATE_A_STAGES),
        }
        for index, stage in enumerate(_CANDIDATE_A_STAGES, start=1)
    ]
    if value != expected:
        raise GateEReplayError("candidate_evidence_invalid")


def _candidate_runtime_payload(
    value: object,
) -> tuple[dict[str, object], dict[str, object]]:
    if type(value) is not dict or set(value) != {"python", "uv"}:
        raise GateEReplayError("candidate_evidence_invalid")
    python = value["python"]
    uv = value["uv"]
    if (
        type(python) is not dict
        or set(python)
        != {
            "architecture",
            "implementation",
            "name",
            "platform",
            "sha256",
            "size",
            "version",
        }
        or python["name"] != "python"
        or python["implementation"] != "CPython"
        or python["version"] != "3.11.15"
        or type(python["architecture"]) is not str
        or _SAFE_VERSION_RE.fullmatch(python["architecture"]) is None
        or type(python["platform"]) is not str
        or _SAFE_VERSION_RE.fullmatch(python["platform"]) is None
        or type(python["sha256"]) is not str
        or _HASH_RE.fullmatch(python["sha256"]) is None
        or type(python["size"]) is not int
        or python["size"] <= 0
        or type(uv) is not dict
        or set(uv) != {"name", "sha256", "size", "version"}
        or uv["name"] != "uv"
        or type(uv["version"]) is not str
        or _SAFE_VERSION_RE.fullmatch(uv["version"]) is None
        or type(uv["sha256"]) is not str
        or _HASH_RE.fullmatch(uv["sha256"]) is None
        or type(uv["size"]) is not int
        or uv["size"] <= 0
    ):
        raise GateEReplayError("candidate_evidence_invalid")
    return dict(python), dict(uv)


def _load_candidate_evidence(
    evidence_path: Path,
) -> VerifiedCandidateEvidence:
    if not isinstance(evidence_path, Path):
        raise TypeError("evidence_path must be a Path")
    absolute = (
        evidence_path
        if evidence_path.is_absolute()
        else Path.cwd() / evidence_path
    )
    absolute = _safe_existing_file(
        absolute,
        code="candidate_evidence_invalid",
    )
    payload = _parse_canonical_json(
        _read_small_regular(
            absolute,
            code="candidate_evidence_invalid",
        )
    )
    paths = payload.get("paths")
    candidate = payload.get("candidate")
    hash_seed = payload.get("hash_seed")
    implementation_commit = payload.get("implementation_commit")
    v01_tag_commit = payload.get("v01_tag_commit")
    run_id = payload.get("run_id")
    runtime_python, runtime_uv = _candidate_runtime_payload(
        payload.get("runtime")
    )
    if (
        set(payload) != _CANDIDATE_KEYS
        or payload.get("schema_version") != "1.1"
        or payload.get("project_name") != "a-share-quant"
        or payload.get("project_version") != "0.2.0"
        or payload.get("gate") != "E"
        or payload.get("research_boundary") != _RESEARCH_BOUNDARY
        or payload.get("trust_created") is not False
        or candidate not in {"A", "B"}
        or hash_seed != ("101" if candidate == "A" else "909")
        or type(implementation_commit) is not str
        or _COMMIT_RE.fullmatch(implementation_commit) is None
        or type(v01_tag_commit) is not str
        or _COMMIT_RE.fullmatch(v01_tag_commit) is None
        or type(run_id) is not str
        or _HASH_RE.fullmatch(run_id) is None
        or type(paths) is not dict
        or set(paths) != _CANDIDATE_PATH_KEYS
    ):
        raise GateEReplayError("candidate_evidence_invalid")
    _validate_candidate_progress(payload.get("progress"))
    expected_name = (
        _CANDIDATE_EVIDENCE_NAME
        if candidate == "A"
        else _CANDIDATE_B_EVIDENCE_NAME
    )
    if absolute.name != expected_name:
        raise GateEReplayError("candidate_evidence_invalid")
    repository_root = _candidate_path(
        paths["repository_root"],
        directory=True,
    )
    release_root = _candidate_path(
        paths["release_root"],
        directory=True,
    )
    workspace = _candidate_path(
        paths["workspace"],
        directory=True,
    )
    wheelhouse_root = _candidate_path(
        paths["wheelhouse_root"],
        directory=True,
    )
    artifact = _candidate_path(paths["artifact"], directory=True)
    config = _candidate_path(paths["config"], directory=False)
    project_wheel = _candidate_path(
        paths["project_wheel"],
        directory=False,
    )
    wheelhouse_manifest = _candidate_path(
        paths["wheelhouse_manifest"],
        directory=False,
    )
    install_lock = _candidate_path(
        paths["install_lock"],
        directory=False,
    )
    uv_lock = _candidate_path(paths["uv_lock"], directory=False)
    python_executable = _candidate_path(
        paths["python_executable"],
        directory=False,
        allow_parent_alias=True,
    )
    uv_executable = _candidate_path(
        paths["uv_executable"],
        directory=False,
    )
    if (
        paths["evidence"] != os.fspath(absolute)
        or absolute.parent != workspace
        or config
        != repository_root / _CONFIG_RELATIVE.as_posix()
        or release_root
        != repository_root / _RELEASE_RELATIVE.as_posix()
        or uv_lock != repository_root / "uv.lock"
        or wheelhouse_manifest
        != wheelhouse_root.parent / "wheelhouse_manifest.json"
        or install_lock
        != wheelhouse_root.parent / "requirements.install.lock.txt"
        or artifact.name != run_id
        or artifact
        != workspace
        / "project"
        / "outputs"
        / "portfolios"
        / run_id
    ):
        raise GateEReplayError("candidate_evidence_invalid")
    commands = payload.get("commands")
    expected_portfolio_cli = (
        workspace / "venv" / "bin" / "aquant-portfolio"
    )
    if (
        type(commands) is not dict
        or set(commands) != {"run", "verify"}
        or type(commands["run"]) is not list
        or type(commands["verify"]) is not list
        or commands["run"]
        != [
            os.fspath(expected_portfolio_cli),
            "run-config",
            "--config",
            _CONFIG_RELATIVE.as_posix(),
        ]
        or commands["verify"]
        != [
            os.fspath(expected_portfolio_cli),
            "verify",
            "--project-root",
            ".",
            "--artifact",
            f"outputs/portfolios/{run_id}",
            "--expected-run-id",
            run_id,
        ]
    ):
        raise GateEReplayError("candidate_evidence_invalid")
    counts = payload.get("counts")
    if (
        type(counts) is not dict
        or counts
        != {
            "artifact_files": 13,
            "equity_rows": _EXPECTED_SESSIONS,
            "input_files": 25,
            "no_bar_total": 28,
            "payload_files": 12,
            "position_rows": _EXPECTED_POSITION_ROWS,
            "sessions": _EXPECTED_SESSIONS,
            "symbols": 10,
            "target_rows": 10,
        }
        or type(payload.get("config_sha256")) is not str
        or _HASH_RE.fullmatch(payload["config_sha256"]) is None
        or type(payload.get("artifact_files")) is not list
        or type(payload.get("accounting")) is not dict
        or type(payload.get("business_evidence")) is not dict
        or type(payload.get("wheelhouse")) is not dict
        or type(payload.get("installed_packages")) is not list
    ):
        raise GateEReplayError("candidate_evidence_invalid")
    return VerifiedCandidateEvidence(
        evidence_path=absolute,
        candidate=candidate,
        implementation_commit=implementation_commit,
        v01_tag_commit=v01_tag_commit,
        run_id=run_id,
        repository_root=repository_root,
        release_root=release_root,
        config=config,
        project_wheel=project_wheel,
        wheelhouse_root=wheelhouse_root,
        wheelhouse_manifest=wheelhouse_manifest,
        install_lock=install_lock,
        uv_lock=uv_lock,
        python_executable=python_executable,
        uv_executable=uv_executable,
        runtime_python=runtime_python,
        runtime_uv=runtime_uv,
        workspace=workspace,
        artifact=artifact,
        payload=payload,
    )


def _replay_from_candidate(
    candidate: VerifiedCandidateEvidence,
    *,
    workspace_b: Path | None = None,
) -> GateEReplay:
    requirements = tuple(
        sorted(
            wheelhouse_requirements_from_manifest(
                candidate.wheelhouse_manifest
            ).items()
        )
    )
    second = (
        candidate.workspace.with_name(
            f"{candidate.workspace.name}-b"
        )
        if workspace_b is None
        else workspace_b
    )
    return GateEReplay(
        repository_root=candidate.repository_root,
        release_root=candidate.release_root,
        config_path=candidate.config,
        project_wheel=candidate.project_wheel,
        wheelhouse_root=candidate.wheelhouse_root,
        wheelhouse_manifest=candidate.wheelhouse_manifest,
        install_lock=candidate.install_lock,
        uv_lock=candidate.uv_lock,
        workspace_a=candidate.workspace,
        workspace_b=second,
        implementation_commit=candidate.implementation_commit,
        v01_tag_commit=candidate.v01_tag_commit,
        python_executable=candidate.python_executable,
        uv_executable=candidate.uv_executable,
        expected_requirements=requirements,
    )


def _candidate_layout(
    candidate: VerifiedCandidateEvidence,
) -> GateEEnvironmentLayout:
    root = candidate.workspace
    return GateEEnvironmentLayout(
        root=root,
        home=root / "home",
        xdg_cache=root / "xdg-cache",
        uv_cache=root / "uv-cache",
        venv=root / "venv",
        python=root / "venv" / "bin" / "python",
        project_root=root / "project",
        input_root=root / "project",
        output_root=root / "project" / "outputs",
        config_path=(
            root
            / "project"
            / _CONFIG_RELATIVE.as_posix()
        ),
        repository_root=candidate.repository_root,
        base_python=candidate.python_executable,
    )


def _audit_verified_candidate(
    candidate: VerifiedCandidateEvidence,
    *,
    read_only_paths: tuple[Path, ...] = (),
) -> tuple[
    GateEAccountingAudit,
    GateEInputAudit,
    InstalledEnvironmentEvidence,
]:
    runtime = _verify_candidate_runtime(candidate)
    replay = _replay_from_candidate(candidate)
    config = _verify_trust_roots(replay, require_clean=False)
    project_wheel, wheelhouse = _verified_wheel_inputs(replay)
    layout = _candidate_layout(candidate)
    input_audit = audit_gate_e_inputs(config, layout.project_root)
    accounting = audit_gate_e_bundle(
        candidate.artifact,
        expected_run_id=candidate.run_id,
    )
    _validate_formal_audits(
        bundle=accounting,
        inputs=input_audit,
        run_id=candidate.run_id,
    )
    _verify_staged_inputs(
        replay,
        layout,
        config,
        run_id=candidate.run_id,
    )
    installed = inspect_installed_environment(
        layout,
        hash_seed=(
            "101" if candidate.candidate == "A" else "909"
        ),
        require_gate_e_cli=True,
        read_only_paths=read_only_paths,
    )
    commands = candidate.payload["commands"]
    expected_payload = _candidate_payload(
        replay,
        candidate=candidate.candidate,
        hash_seed=(
            "101" if candidate.candidate == "A" else "909"
        ),
        layout=layout,
        installed=installed,
        config=config,
        project_wheel=project_wheel,
        wheelhouse=wheelhouse,
        run_id=candidate.run_id,
        artifact=candidate.artifact,
        run_command=tuple(commands["run"]),
        verify_command=tuple(commands["verify"]),
        accounting=accounting,
        input_audit=input_audit,
        evidence_path=candidate.evidence_path,
        runtime=runtime,
    )
    if expected_payload != candidate.payload:
        raise GateEReplayError("candidate_evidence_mismatch")
    _verify_candidate_runtime(candidate)
    return accounting, input_audit, installed


def audit_candidate(
    *,
    evidence_path: Path,
    artifact: Path,
) -> dict[str, object]:
    """Re-run the independent Candidate A/B audit."""
    candidate = _load_candidate_evidence(evidence_path)
    candidate_artifact = (
        artifact if artifact.is_absolute() else Path.cwd() / artifact
    )
    _safe_existing_directory(
        candidate_artifact,
        code="candidate_artifact_mismatch",
    )
    if candidate_artifact != candidate.artifact:
        raise GateEReplayError("candidate_artifact_mismatch")
    accounting, input_audit, _installed = _audit_verified_candidate(
        candidate
    )
    return {
        "artifact_file_count": 13,
        "candidate": candidate.candidate,
        "cash_identity_verified": True,
        "input_file_count": 25,
        "net_asset_identity_verified": True,
        "no_bar_total": input_audit.no_bar_total,
        "payload_file_count": 12,
        "run_id": candidate.run_id,
        "session_count": input_audit.session_count,
        "status": "audited",
        "symbol_count": len(_EXPECTED_SYMBOLS),
        "trade_cash_ending_fen": accounting.ending_cash_fen,
        "trusted": False,
    }


def build_trust_from_candidate(
    *,
    evidence_path: Path,
    approved_review: Path,
) -> bytes:
    """Build canonical trust bytes after explicit external approval."""
    candidate = _load_candidate_evidence(evidence_path)
    if candidate.candidate != "A":
        raise GateEReplayError("candidate_review_not_approved")
    approved_review = _fixed_candidate_review_argument(
        approved_review,
        repository_root=candidate.repository_root,
    )
    verify_approved_review(approved_review)
    _audit_verified_candidate(candidate)
    try:
        return gate_e_trust_bytes(
            evidence=candidate.to_trust_evidence(approved_review),
            expected_run_id=candidate.run_id,
        )
    except GateETrustError as exc:
        raise GateEReplayError(
            "trust_build_failed",
            cause_code=exc.code,
        ) from exc


def verify_candidate_trust(
    *,
    trust: Path,
    evidence_path: Path,
    artifact: Path,
    approved_review: Path,
    reviewed_candidate_evidence: Path | None = None,
) -> dict[str, object]:
    """Verify one trust blob against live Candidate A/B evidence."""
    candidate = _load_candidate_evidence(evidence_path)
    approved_review = _fixed_candidate_review_argument(
        approved_review,
        repository_root=candidate.repository_root,
    )
    verify_approved_review(approved_review)
    return _verify_candidate_trust_evidence(
        trust=trust,
        candidate=candidate,
        artifact=artifact,
        approved_review=approved_review,
        reviewed_candidate_evidence=reviewed_candidate_evidence,
    )


def _verify_candidate_trust_evidence(
    *,
    trust: Path,
    candidate: VerifiedCandidateEvidence,
    artifact: Path,
    approved_review: Path,
    reviewed_candidate_evidence: Path | None = None,
    read_only_paths: tuple[Path, ...] = (),
) -> dict[str, object]:
    """Verify trust with review bytes already secured by an internal caller."""
    candidate_artifact = (
        artifact if artifact.is_absolute() else Path.cwd() / artifact
    )
    if candidate_artifact != candidate.artifact:
        raise GateEReplayError("candidate_artifact_mismatch")
    _audit_verified_candidate(
        candidate,
        read_only_paths=read_only_paths,
    )
    try:
        verified = verify_gate_e_trust(
            trust,
            candidate.to_trust_evidence(
                approved_review,
                reviewed_candidate_evidence=reviewed_candidate_evidence,
            ),
        )
    except GateETrustError as exc:
        raise GateEReplayError(
            "trust_verification_failed",
            cause_code=exc.code,
        ) from exc
    return {
        "artifact_file_count": verified.artifact_file_count,
        "expected_run_id": verified.expected_run_id,
        "implementation_commit": verified.implementation_commit,
        "payload_file_count": verified.payload_file_count,
        "status": "trusted",
        "trust_sha256": verified.trust_sha256,
    }


def _execute_candidate_a_stage(
    replay: GateEReplay,
    *,
    stage: str,
    state: dict[str, object],
) -> CandidateAResult | None:
    try:
        candidate = state["candidate"]
        hash_seed = state["hash_seed"]
        workspace = state["workspace"]
        if (
            candidate not in {"A", "B"}
            or hash_seed not in {"101", "909"}
            or not isinstance(workspace, Path)
        ):
            raise GateEReplayError("invalid_replay_state")
        if stage == "trust_roots_verified":
            state["config"] = _verify_trust_roots(
                replay,
                require_clean=(candidate == "A"),
            )
        elif stage == "wheel_verified":
            project, wheelhouse = _verified_wheel_inputs(replay)
            state["project_wheel_evidence"] = project
            state["wheelhouse_evidence"] = wheelhouse
        elif stage == "environment_a_installed":
            project = state.get("project_wheel_evidence")
            runtime_guards = state.get("runtime_guards")
            if type(project) is not WheelEvidence:
                raise GateEReplayError("invalid_replay_state")
            if type(runtime_guards) is not dict:
                raise GateEReplayError("invalid_replay_state")
            _verify_runtime_execution_guards(replay, runtime_guards)
            layout = make_environment_layout(
                workspace,
                repository_root=replay.repository_root,
                base_python=replay.python_executable,
                expected_base_python_guard=runtime_guards["python"],
                read_only_paths=_candidate_b_write_denials(
                    replay,
                    workspace=workspace,
                ),
            )
            installed = install_gate_e_environment(
                layout,
                project_wheel=replay.project_wheel,
                expected_project_sha256=project.sha256,
                wheelhouse=replay.wheelhouse_root,
                wheelhouse_manifest=replay.wheelhouse_manifest,
                install_lock=replay.install_lock,
                expected_requirements=dict(
                    replay.expected_requirements
                ),
                hash_seed=hash_seed,
                uv_executable=replay.uv_executable,
                expected_uv_guard=runtime_guards["uv"],
                require_gate_e_cli=True,
                read_only_paths=_candidate_b_write_denials(
                    replay,
                    workspace=workspace,
                ),
            )
            _verify_runtime_execution_guards(replay, runtime_guards)
            state["layout"] = layout
            state["installed"] = installed
        elif stage == "inputs_a_verified":
            config = state.get("config")
            layout = state.get("layout")
            if (
                type(config) is not GateEConfig
                or type(layout) is not GateEEnvironmentLayout
            ):
                raise GateEReplayError("invalid_replay_state")
            copied = stage_environment_inputs(
                layout,
                replay.release_root,
            )
            if copied.file_count != 25:
                raise GateEReplayError(
                    "environment_input_count_mismatch"
                )
            copy_gate_e_config(
                layout,
                replay.config_path,
                expected_sha256=config.config_sha256,
            )
            _verify_staged_inputs(
                replay,
                layout,
                config,
                run_id=None,
            )
            input_audit = audit_gate_e_inputs(
                config,
                layout.project_root,
            )
            if (
                input_audit.session_count != _EXPECTED_SESSIONS
                or input_audit.no_bar_counts
                != _EXPECTED_NO_BAR_COUNTS
                or input_audit.no_bar_total != 28
            ):
                raise GateEReplayError(
                    "candidate_input_audit_mismatch"
                )
            state["input_audit"] = input_audit
            state["input_state"] = _snapshot_staged_input_state(
                config,
                layout.project_root,
            )
        elif stage == "candidate_a_run":
            config = state.get("config")
            layout = state.get("layout")
            installed = state.get("installed")
            input_state = state.get("input_state")
            if (
                type(config) is not GateEConfig
                or type(layout) is not GateEEnvironmentLayout
                or type(installed) is not InstalledEnvironmentEvidence
                or type(input_state) is not tuple
                or len(input_state) != 25
            ):
                raise GateEReplayError("invalid_replay_state")
            run_id, artifact, command = _run_portfolio_candidate(
                replay,
                layout,
                installed,
                config,
                hash_seed=hash_seed,
            )
            _verify_staged_inputs(
                replay,
                layout,
                config,
                run_id=run_id,
                expected_input_state=input_state,
            )
            state["run_id"] = run_id
            state["artifact"] = artifact
            state["run_command"] = command
        elif stage == "candidate_a_reversed":
            config = state.get("config")
            layout = state.get("layout")
            installed = state.get("installed")
            input_state = state.get("input_state")
            run_id = state.get("run_id")
            artifact = state.get("artifact")
            if (
                type(config) is not GateEConfig
                or type(layout) is not GateEEnvironmentLayout
                or type(installed) is not InstalledEnvironmentEvidence
                or type(input_state) is not tuple
                or len(input_state) != 25
                or type(run_id) is not str
                or not isinstance(artifact, Path)
            ):
                raise GateEReplayError("invalid_replay_state")
            verify_command = _reverse_candidate(
                replay,
                layout,
                installed,
                config,
                run_id=run_id,
                artifact=artifact,
                hash_seed=hash_seed,
            )
            _verify_staged_inputs(
                replay,
                layout,
                config,
                run_id=run_id,
                expected_input_state=input_state,
            )
            state["verify_command"] = verify_command
        elif stage == "candidate_a_audited":
            config = state.get("config")
            layout = state.get("layout")
            installed = state.get("installed")
            project_before = state.get("project_wheel_evidence")
            wheelhouse_before = state.get("wheelhouse_evidence")
            input_audit = state.get("input_audit")
            input_state = state.get("input_state")
            run_id = state.get("run_id")
            artifact = state.get("artifact")
            run_command = state.get("run_command")
            verify_command = state.get("verify_command")
            runtime = state.get("runtime")
            runtime_guards = state.get("runtime_guards")
            if (
                type(config) is not GateEConfig
                or type(layout) is not GateEEnvironmentLayout
                or type(installed) is not InstalledEnvironmentEvidence
                or type(project_before) is not WheelEvidence
                or type(wheelhouse_before) is not WheelhouseEvidence
                or type(input_audit) is not GateEInputAudit
                or type(input_state) is not tuple
                or len(input_state) != 25
                or type(run_id) is not str
                or not isinstance(artifact, Path)
                or type(run_command) is not tuple
                or type(verify_command) is not tuple
                or type(runtime) is not dict
                or set(runtime) != {"python", "uv"}
                or type(runtime_guards) is not dict
            ):
                raise GateEReplayError("invalid_replay_state")
            _verify_runtime_execution_guards(replay, runtime_guards)
            if _capture_runtime_snapshots(replay) != runtime:
                raise GateEReplayError("candidate_runtime_changed")
            accounting = audit_gate_e_bundle(
                artifact,
                expected_run_id=run_id,
            )
            input_after = audit_gate_e_inputs(
                config,
                layout.project_root,
            )
            if input_after != input_audit:
                raise GateEReplayError(
                    "candidate_input_changed_during_run"
                )
            _validate_formal_audits(
                bundle=accounting,
                inputs=input_after,
                run_id=run_id,
            )
            _verify_staged_inputs(
                replay,
                layout,
                config,
                run_id=run_id,
                expected_input_state=input_state,
            )
            project_after, wheelhouse_after = (
                _verified_wheel_inputs(replay)
            )
            if (
                project_after != project_before
                or wheelhouse_after != wheelhouse_before
            ):
                raise GateEReplayError(
                    "sealed_install_inputs_changed"
                )
            installed_after = inspect_installed_environment(
                layout,
                hash_seed=hash_seed,
                require_gate_e_cli=True,
                read_only_paths=_candidate_b_write_denials(
                    replay,
                    workspace=workspace,
                ),
            )
            if installed_after != installed:
                raise GateEReplayError(
                    "installed_environment_changed"
                )
            evidence_name = (
                _CANDIDATE_EVIDENCE_NAME
                if candidate == "A"
                else _CANDIDATE_B_EVIDENCE_NAME
            )
            evidence_path = layout.root / evidence_name
            payload = _candidate_payload(
                replay,
                candidate=candidate,
                hash_seed=hash_seed,
                layout=layout,
                installed=installed,
                config=config,
                project_wheel=project_after,
                wheelhouse=wheelhouse_after,
                run_id=run_id,
                artifact=artifact,
                run_command=run_command,
                verify_command=verify_command,
                accounting=accounting,
                input_audit=input_after,
                evidence_path=evidence_path,
                runtime=runtime,
            )
            _write_candidate_evidence(evidence_path, payload)
            _verify_runtime_execution_guards(replay, runtime_guards)
            if _capture_runtime_snapshots(replay) != runtime:
                raise GateEReplayError("candidate_runtime_changed")
            return CandidateAResult(
                evidence_path=evidence_path,
                artifact=artifact,
                run_id=run_id,
            )
        else:
            raise GateEReplayError("invalid_candidate_stage")
    except GateEReplayError:
        raise
    except (
        GateEAuditError,
        GateEConfigError,
        GateEEnvironmentError,
        GateEInputError,
        GateETrustError,
    ) as exc:
        raise GateEReplayError(
            "candidate_stage_failed",
            cause_code=getattr(exc, "code", None),
        ) from exc
    return None


def run_candidate_a(
    replay: GateEReplay,
    *,
    progress: Callable[[GateEProgressEvent], None] | None = None,
) -> CandidateAResult:
    """Run Candidate A in the one fixed order and never create trust."""
    if type(replay) is not GateEReplay:
        raise GateEReplayError("invalid_replay_contract")
    callback = progress if progress is not None else lambda _event: None
    runtime, runtime_guards = _capture_runtime_controls(replay)
    state: dict[str, object] = {
        "candidate": "A",
        "hash_seed": "101",
        "runtime": runtime,
        "runtime_guards": runtime_guards,
        "workspace": replay.workspace_a,
    }
    events: list[GateEProgressEvent] = []
    result: CandidateAResult | None = None
    for completed, stage in enumerate(_CANDIDATE_A_STAGES, start=1):
        stage_result = _execute_candidate_a_stage(
            replay,
            stage=stage,
            state=state,
        )
        event = GateEProgressEvent(
            stage=stage,
            completed=completed,
            total=len(_CANDIDATE_A_STAGES),
        )
        events.append(event)
        callback(event)
        if stage_result is not None:
            result = stage_result
    if result is None:
        raise GateEReplayError("candidate_evidence_missing")
    return replace(result, progress=tuple(events))


def create_gate_e_replay_b(
    *,
    project_wheel: Path,
    wheelhouse: Path,
    workspace_a: Path,
    workspace_b: Path,
) -> GateEReplay:
    """Rehydrate the implementation identity only from Candidate A."""
    repository_root = _repository_from_cwd()
    first = _workspace_argument(
        workspace_a,
        repository_root=repository_root,
    )
    evidence = _load_candidate_evidence(
        first / _CANDIDATE_EVIDENCE_NAME
    )
    if (
        evidence.candidate != "A"
        or evidence.repository_root != repository_root
        or evidence.project_wheel
        != _absolute_argument(
            project_wheel,
            base=repository_root,
        )
        or evidence.wheelhouse_root
        != _absolute_argument(
            wheelhouse,
            base=repository_root,
        )
    ):
        raise GateEReplayError("candidate_a_identity_mismatch")
    second = _workspace_argument(
        workspace_b,
        repository_root=repository_root,
    )
    if (
        first == second
        or os.path.lexists(second)
        or first in second.parents
        or second in first.parents
    ):
        raise GateEReplayError("candidate_workspace_overlap")
    return _replay_from_candidate(
        evidence,
        workspace_b=second,
    )


def _verify_input_roots_independent(
    replay: GateEReplay,
    candidate_a: VerifiedCandidateEvidence,
    candidate_b: VerifiedCandidateEvidence,
) -> None:
    config = load_gate_e_config(replay.config_path)
    for relative in config.payload["input_files"]:
        source = replay.release_root / "inputs" / relative
        first = candidate_a.workspace / "project" / relative
        second = candidate_b.workspace / "project" / relative
        identities = {
            (source.lstat().st_dev, source.lstat().st_ino),
            (first.lstat().st_dev, first.lstat().st_ino),
            (second.lstat().st_dev, second.lstat().st_ino),
        }
        if len(identities) != 3:
            raise GateEReplayError("copy_inode_not_independent")


def _compare_installed_package_inventories(
    candidate_a: VerifiedCandidateEvidence,
    candidate_b: VerifiedCandidateEvidence,
) -> None:
    first = candidate_a.payload.get("installed_packages")
    second = candidate_b.payload.get("installed_packages")
    if (
        type(first) is not list
        or type(second) is not list
        or len(first) != 37
        or len(second) != 37
        or first != second
    ):
        raise GateEReplayError("replay_installed_packages_mismatch")


def _compare_artifacts(
    first: Path,
    second: Path,
) -> None:
    if (
        set(_scan_regular_tree(first))
        != PORTFOLIO_ARTIFACT_FILES
        or set(_scan_regular_tree(second))
        != PORTFOLIO_ARTIFACT_FILES
    ):
        raise GateEReplayError("replay_artifact_set_mismatch")
    for name in sorted(PORTFOLIO_ARTIFACT_FILES):
        first_content = _read_regular_bytes(
            first / name,
            code="replay_artifact_mismatch",
            maximum_bytes=256 * 1024 * 1024,
        )
        second_content = _read_regular_bytes(
            second / name,
            code="replay_artifact_mismatch",
            maximum_bytes=256 * 1024 * 1024,
        )
        if first_content != second_content:
            raise GateEReplayError("replay_artifact_mismatch")


def _anchored_blob(
    repository_root: Path,
    commit: str,
    relative: PurePosixPath,
) -> bytes:
    object_spec = f"{commit}:{relative.as_posix()}"
    size_text = _git(
        repository_root,
        "cat-file",
        "-s",
        object_spec,
        code="trust_anchor_not_approved",
    )
    if (
        type(size_text) is not str
        or not size_text.isascii()
        or not size_text.isdecimal()
        or int(size_text) > 4 * 1024 * 1024
    ):
        raise GateEReplayError("trust_anchor_not_approved")
    blob = _git(
        repository_root,
        "show",
        object_spec,
        code="trust_anchor_not_approved",
        binary=True,
    )
    if type(blob) is not bytes or len(blob) != int(size_text):
        raise GateEReplayError("trust_anchor_not_approved")
    return blob


def _require_current_blob(
    repository_root: Path,
    relative: PurePosixPath,
    expected: bytes,
) -> None:
    current = repository_root / relative.as_posix()
    if not os.path.lexists(current) or _read_small_regular(
        current,
        code="trust_anchor_not_approved",
    ) != expected:
        raise GateEReplayError("trust_anchor_not_approved")


def _verify_git_approval_closure(
    replay: GateEReplay,
    candidate_a: VerifiedCandidateEvidence,
    *,
    trust_anchor_commit: str,
    approval_commit: str,
) -> tuple[bytes, bytes]:
    """Bind Candidate A review, trust, and post-anchor approval in Git."""
    replacement_refs = _git(
        replay.repository_root,
        "for-each-ref",
        "--format=%(refname)",
        "refs/replace",
        code="trust_anchor_not_approved",
    )
    common_dir_text = _git(
        replay.repository_root,
        "rev-parse",
        "--git-common-dir",
        code="trust_anchor_not_approved",
    )
    if (
        type(replacement_refs) is not str
        or replacement_refs
        or type(common_dir_text) is not str
        or not common_dir_text
        or "\x00" in common_dir_text
    ):
        raise GateEReplayError("trust_anchor_not_approved")
    common_dir = Path(common_dir_text)
    if not common_dir.is_absolute():
        common_dir = replay.repository_root / common_dir
    try:
        common_dir = common_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GateEReplayError("trust_anchor_not_approved") from exc
    if os.path.lexists(common_dir / "info/grafts"):
        raise GateEReplayError("trust_anchor_not_approved")
    if trust_anchor_commit == approval_commit:
        raise GateEReplayError("trust_anchor_not_approved")
    for commit in (trust_anchor_commit, approval_commit):
        if (
            _git(
                replay.repository_root,
                "cat-file",
                "-t",
                commit,
                code="trust_anchor_not_approved",
            )
            != "commit"
        ):
            raise GateEReplayError("trust_anchor_not_approved")
    head_commit = _git(
        replay.repository_root,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        code="trust_anchor_not_approved",
    )
    if (
        type(head_commit) is not str
        or _COMMIT_RE.fullmatch(head_commit) is None
    ):
        raise GateEReplayError("trust_anchor_not_approved")
    _git(
        replay.repository_root,
        "merge-base",
        "--is-ancestor",
        trust_anchor_commit,
        approval_commit,
        code="trust_anchor_not_approved",
    )
    _git(
        replay.repository_root,
        "merge-base",
        "--is-ancestor",
        approval_commit,
        head_commit,
        code="trust_anchor_not_approved",
    )
    trust_blob = _anchored_blob(
        replay.repository_root,
        trust_anchor_commit,
        _TRUST_RELATIVE,
    )
    candidate_review_blob = _anchored_blob(
        replay.repository_root,
        trust_anchor_commit,
        _CANDIDATE_REVIEW_RELATIVE,
    )
    if (
        _anchored_blob(
            replay.repository_root,
            approval_commit,
            _TRUST_RELATIVE,
        )
        != trust_blob
        or _anchored_blob(
            replay.repository_root,
            approval_commit,
            _CANDIDATE_REVIEW_RELATIVE,
        )
        != candidate_review_blob
    ):
        raise GateEReplayError("trust_anchor_not_approved")
    approval_blob = _anchored_blob(
        replay.repository_root,
        approval_commit,
        _TRUST_APPROVAL_RELATIVE,
    )
    if (
        _anchored_blob(
            replay.repository_root,
            head_commit,
            _TRUST_RELATIVE,
        )
        != trust_blob
        or _anchored_blob(
            replay.repository_root,
            head_commit,
            _CANDIDATE_REVIEW_RELATIVE,
        )
        != candidate_review_blob
        or _anchored_blob(
            replay.repository_root,
            head_commit,
            _TRUST_APPROVAL_RELATIVE,
        )
        != approval_blob
    ):
        raise GateEReplayError("trust_anchor_not_approved")
    _require_current_blob(
        replay.repository_root,
        _TRUST_RELATIVE,
        trust_blob,
    )
    _require_current_blob(
        replay.repository_root,
        _CANDIDATE_REVIEW_RELATIVE,
        candidate_review_blob,
    )
    _require_current_blob(
        replay.repository_root,
        _TRUST_APPROVAL_RELATIVE,
        approval_blob,
    )
    candidate_bindings = _parse_candidate_review_bytes(
        candidate_review_blob
    )
    approval_bindings = _parse_trust_approval_bytes(approval_blob)
    if (
        candidate_bindings["implementation_commit"]
        != candidate_a.implementation_commit
        or candidate_bindings["expected_run_id"] != candidate_a.run_id
        or approval_bindings["trust_anchor_commit"]
        != trust_anchor_commit
        or approval_bindings["trust_sha256"]
        != hashlib.sha256(trust_blob).hexdigest()
        or approval_bindings["candidate_review_sha256"]
        != hashlib.sha256(candidate_review_blob).hexdigest()
        or approval_bindings["implementation_commit"]
        != candidate_a.implementation_commit
        or approval_bindings["expected_run_id"] != candidate_a.run_id
    ):
        raise GateEReplayError("trust_anchor_not_approved")
    return trust_blob, candidate_review_blob


def _anchored_object_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _write_anchored_temporary(
    root: _AnchoredTemporaryRoot,
    *,
    prefix: str,
    suffix: str,
    content: bytes,
) -> _AnchoredTemporaryFile:
    descriptor = -1
    name: str | None = None
    created = False
    try:
        if type(root) is not _AnchoredTemporaryRoot:
            raise GateEReplayError("trust_temporary_root_failed")
        for _attempt in range(128):
            candidate = f"{prefix}{secrets.token_hex(16)}{suffix}"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | _NOFOLLOW
                    | _CLOEXEC,
                    0o600,
                    dir_fd=root.descriptor,
                )
                name = candidate
                created = True
                break
            except FileExistsError:
                continue
        if descriptor < 0 or name is None:
            raise GateEReplayError("trust_anchor_not_approved")
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise GateEReplayError("trust_anchor_not_approved")
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise GateEReplayError("trust_anchor_not_approved")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        final = os.fstat(descriptor)
        named = os.stat(
            name,
            dir_fd=root.descriptor,
            follow_symlinks=False,
        )
        if not (
            _workspace_metadata(final) == _workspace_metadata(named)
            and final.st_dev == opened.st_dev
            and final.st_ino == opened.st_ino
            and final.st_nlink == 1
        ):
            raise GateEReplayError("trust_anchor_not_approved")
        os.close(descriptor)
        descriptor = -1
        os.fsync(root.descriptor)
        return _AnchoredTemporaryFile(
            path=root.path / name,
            name=name,
            identity=_workspace_metadata(final),
        )
    except GateEReplayError:
        raise
    except OSError as exc:
        raise GateEReplayError("trust_anchor_not_approved") from exc
    finally:
        cleanup_error: OSError | None = None
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = exc
        if created and descriptor >= 0 and name is not None:
            try:
                os.unlink(name, dir_fd=root.descriptor)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise GateEReplayError(
                "trust_temporary_cleanup_failed"
            ) from cleanup_error


def _workspace_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_mode,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _workspace_regular_digest(
    path: Path,
    before: os.stat_result,
) -> str:
    descriptor = -1
    digest = hashlib.sha256()
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
        )
        opened = os.fstat(descriptor)
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        final = os.fstat(descriptor)
        named = path.lstat()
        if not (
            stat.S_ISREG(opened.st_mode)
            and stat.S_ISREG(final.st_mode)
            and stat.S_ISREG(named.st_mode)
            and _workspace_metadata(before)
            == _workspace_metadata(opened)
            == _workspace_metadata(final)
            == _workspace_metadata(named)
        ):
            raise GateEReplayError(
                "candidate_a_workspace_unverifiable"
            )
    except GateEReplayError:
        raise
    except OSError as exc:
        raise GateEReplayError(
            "candidate_a_workspace_unverifiable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest()


def _snapshot_candidate_a_workspace(
    root: Path,
) -> tuple[tuple[object, ...], ...]:
    root = _safe_existing_directory(
        root,
        code="candidate_a_workspace_unverifiable",
    )
    try:
        paths = (root, *sorted(root.rglob("*")))
        observed: list[tuple[object, ...]] = []
        for path in paths:
            before = path.lstat()
            relative = (
                "."
                if path == root
                else path.relative_to(root).as_posix()
            )
            if stat.S_ISREG(before.st_mode):
                kind = "file"
                content_identity: str | None = _workspace_regular_digest(
                    path,
                    before,
                )
            elif stat.S_ISDIR(before.st_mode):
                kind = "directory"
                content_identity = None
            elif stat.S_ISLNK(before.st_mode):
                kind = "symlink"
                content_identity = os.readlink(path)
            else:
                raise GateEReplayError(
                    "candidate_a_workspace_unverifiable"
                )
            after = path.lstat()
            if _workspace_metadata(before) != _workspace_metadata(after):
                raise GateEReplayError(
                    "candidate_a_workspace_unverifiable"
                )
            observed.append(
                (
                    relative,
                    kind,
                    *_workspace_metadata(after),
                    content_identity,
                )
            )
        return tuple(observed)
    except GateEReplayError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise GateEReplayError(
            "candidate_a_workspace_unverifiable"
        ) from exc


def _validated_replay_b_temporary_parent(replay: GateEReplay) -> Path:
    repository = _safe_existing_directory(
        replay.repository_root,
        code="candidate_workspace_overlap",
    )
    first = _workspace_argument(
        replay.workspace_a,
        repository_root=repository,
    )
    second = _workspace_argument(
        replay.workspace_b,
        repository_root=repository,
    )
    if (
        first != replay.workspace_a
        or second != replay.workspace_b
        or first == second
        or first in second.parents
        or second in first.parents
    ):
        raise GateEReplayError("candidate_workspace_overlap")
    parent = _safe_existing_directory(
        second.parent,
        code="candidate_workspace_overlap",
    )
    protected = [
        _safe_existing_directory(
            first,
            code="candidate_workspace_overlap",
        ),
        repository,
    ]
    for raw in (replay.release_root, replay.wheelhouse_root):
        if (
            not isinstance(raw, Path)
            or not raw.is_absolute()
            or "\x00" in os.fspath(raw)
            or any(part == ".." for part in raw.parts)
        ):
            raise GateEReplayError("candidate_workspace_overlap")
        try:
            resolved = raw.resolve(strict=True)
        except FileNotFoundError:
            resolved = raw
        except (OSError, RuntimeError) as exc:
            raise GateEReplayError(
                "candidate_workspace_overlap"
            ) from exc
        if resolved != raw:
            raise GateEReplayError("candidate_workspace_overlap")
        protected.append(raw)
    if any(
        parent == path or path in parent.parents
        for path in protected
    ):
        raise GateEReplayError("candidate_workspace_overlap")
    return parent


def _create_anchored_temporary_root(
    replay: GateEReplay,
) -> _AnchoredTemporaryRoot:
    parent = _validated_replay_b_temporary_parent(replay)
    parent_descriptor = -1
    descriptor = -1
    name: str | None = None
    created = False
    try:
        before = parent.lstat()
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
        )
        opened_parent = os.fstat(parent_descriptor)
        named_parent = parent.lstat()
        if not (
            stat.S_ISDIR(opened_parent.st_mode)
            and _anchored_object_identity(before)
            == _anchored_object_identity(opened_parent)
            == _anchored_object_identity(named_parent)
        ):
            raise GateEReplayError("trust_temporary_root_failed")
        for _attempt in range(128):
            candidate = (
                f".{replay.workspace_b.name}.gate-e-trust-"
                f"{secrets.token_hex(16)}"
            )
            try:
                os.mkdir(
                    candidate,
                    mode=0o700,
                    dir_fd=parent_descriptor,
                )
                name = candidate
                created = True
                break
            except FileExistsError:
                continue
        if name is None:
            raise GateEReplayError("trust_temporary_root_failed")
        descriptor = os.open(
            name,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
            dir_fd=parent_descriptor,
        )
        opened_root = os.fstat(descriptor)
        named_root = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        parent_after = parent.lstat()
        if not (
            stat.S_ISDIR(opened_root.st_mode)
            and _anchored_object_identity(opened_root)
            == _anchored_object_identity(named_root)
            and _anchored_object_identity(parent_after)
            == _anchored_object_identity(opened_parent)
        ):
            raise GateEReplayError("trust_temporary_root_failed")
        os.fsync(parent_descriptor)
        return _AnchoredTemporaryRoot(
            path=parent / name,
            name=name,
            parent_descriptor=parent_descriptor,
            descriptor=descriptor,
            parent_identity=_anchored_object_identity(opened_parent),
            root_identity=_anchored_object_identity(opened_root),
        )
    except (GateEReplayError, OSError) as exc:
        cleanup_error: OSError | None = None
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as close_exc:
                cleanup_error = close_exc
        if created and name is not None and parent_descriptor >= 0:
            try:
                os.rmdir(name, dir_fd=parent_descriptor)
            except OSError as remove_exc:
                cleanup_error = remove_exc
        if parent_descriptor >= 0:
            try:
                os.close(parent_descriptor)
            except OSError as close_exc:
                cleanup_error = close_exc
        if cleanup_error is not None:
            raise GateEReplayError(
                "trust_temporary_cleanup_failed"
            ) from cleanup_error
        if isinstance(exc, GateEReplayError):
            raise
        raise GateEReplayError("trust_temporary_root_failed") from exc


def _cleanup_anchored_temporary_root(
    root: _AnchoredTemporaryRoot,
    temporary_files: tuple[_AnchoredTemporaryFile | None, ...],
) -> None:
    cleanup_error: BaseException | None = None
    try:
        if type(root) is not _AnchoredTemporaryRoot:
            raise GateEReplayError("trust_temporary_cleanup_failed")
        opened_root = os.fstat(root.descriptor)
        named_root = os.stat(
            root.name,
            dir_fd=root.parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _anchored_object_identity(opened_root)
            != root.root_identity
            or _anchored_object_identity(named_root)
            != root.root_identity
        ):
            raise GateEReplayError("trust_temporary_cleanup_failed")
        expected = {
            item.name: item
            for item in temporary_files
            if item is not None
        }
        if set(os.listdir(root.descriptor)) != set(expected):
            raise GateEReplayError("trust_temporary_cleanup_failed")
        for name, item in expected.items():
            entry_metadata = os.stat(
                name,
                dir_fd=root.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(entry_metadata.st_mode)
                or entry_metadata.st_nlink != 1
                or _workspace_metadata(entry_metadata)
                != item.identity
            ):
                raise GateEReplayError(
                    "trust_temporary_cleanup_failed"
                )
        for name in sorted(expected):
            os.unlink(name, dir_fd=root.descriptor)
        os.fsync(root.descriptor)
        if os.listdir(root.descriptor):
            raise GateEReplayError("trust_temporary_cleanup_failed")
        os.rmdir(root.name, dir_fd=root.parent_descriptor)
        os.fsync(root.parent_descriptor)
    except (GateEReplayError, OSError) as exc:
        cleanup_error = exc
    finally:
        for descriptor in (root.descriptor, root.parent_descriptor):
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        if isinstance(cleanup_error, GateEReplayError):
            raise cleanup_error
        raise GateEReplayError(
            "trust_temporary_cleanup_failed"
        ) from cleanup_error


def _verified_anchored_temporary_path(
    root: _AnchoredTemporaryRoot,
    item: _AnchoredTemporaryFile,
) -> Path:
    try:
        parent = root.path.parent.lstat()
        named_root = root.path.lstat()
        named_item = item.path.lstat()
    except OSError as exc:
        raise GateEReplayError("trust_anchor_not_approved") from exc
    if (
        _anchored_object_identity(parent) != root.parent_identity
        or _anchored_object_identity(named_root) != root.root_identity
        or _workspace_metadata(named_item) != item.identity
        or not stat.S_ISREG(named_item.st_mode)
        or named_item.st_nlink != 1
    ):
        raise GateEReplayError("trust_anchor_not_approved")
    return item.path


def _load_approved_candidate_a(
    replay: GateEReplay,
    *,
    trust_anchor_commit: str,
    approval_commit: str,
) -> tuple[VerifiedCandidateEvidence, bytes, bytes]:
    candidate_a = _load_candidate_evidence(
        replay.workspace_a / _CANDIDATE_EVIDENCE_NAME
    )
    if (
        candidate_a.candidate != "A"
        or candidate_a.implementation_commit
        != replay.implementation_commit
        or candidate_a.v01_tag_commit != replay.v01_tag_commit
        or candidate_a.project_wheel != replay.project_wheel
        or candidate_a.wheelhouse_root != replay.wheelhouse_root
    ):
        raise GateEReplayError("candidate_a_identity_mismatch")
    blob, candidate_review_blob = _verify_git_approval_closure(
        replay,
        candidate_a,
        trust_anchor_commit=trust_anchor_commit,
        approval_commit=approval_commit,
    )
    return candidate_a, blob, candidate_review_blob


def replay_environment_b(
    replay: GateEReplay,
    *,
    trust_anchor_commit: str | None,
    approval_commit: str | None,
    trust_path: Path | None = None,
) -> ReplayBResult:
    """Run B only after A and an exact reviewed Git trust blob pass."""
    if (
        trust_anchor_commit is None
        or _COMMIT_RE.fullmatch(trust_anchor_commit) is None
        or approval_commit is None
        or _COMMIT_RE.fullmatch(approval_commit) is None
        or approval_commit == trust_anchor_commit
        or trust_path is None
        or not isinstance(trust_path, Path)
        or trust_path.is_absolute()
        or trust_path.as_posix() != _TRUST_RELATIVE.as_posix()
    ):
        raise GateEReplayError("trust_anchor_not_approved")
    if type(replay) is not GateEReplay:
        raise GateEReplayError("invalid_replay_contract")
    _validated_replay_b_temporary_parent(replay)
    if os.path.lexists(replay.workspace_b):
        raise GateEReplayError("candidate_workspace_conflict")
    workspace_a_before = _snapshot_candidate_a_workspace(
        replay.workspace_a
    )
    temporary_root: _AnchoredTemporaryRoot | None = None
    temporary_path: _AnchoredTemporaryFile | None = None
    temporary_review: _AnchoredTemporaryFile | None = None
    try:
        candidate_a, blob, candidate_review_blob = (
            _load_approved_candidate_a(
                replay,
                trust_anchor_commit=trust_anchor_commit,
                approval_commit=approval_commit,
            )
        )
        temporary_root = _create_anchored_temporary_root(replay)
        temporary_path = _write_anchored_temporary(
            temporary_root,
            prefix=".gate-e-anchored-trust-",
            suffix=".json",
            content=blob,
        )
        temporary_review = _write_anchored_temporary(
            temporary_root,
            prefix=".gate-e-anchored-candidate-review-",
            suffix=".md",
            content=candidate_review_blob,
        )
        _verify_candidate_trust_evidence(
            trust=_verified_anchored_temporary_path(
                temporary_root,
                temporary_path,
            ),
            candidate=candidate_a,
            artifact=candidate_a.artifact,
            approved_review=_verified_anchored_temporary_path(
                temporary_root,
                temporary_review,
            ),
            read_only_paths=(replay.workspace_a,),
        )
        runtime, runtime_guards = _capture_runtime_controls(replay)
        if runtime != {
            "python": candidate_a.runtime_python,
            "uv": candidate_a.runtime_uv,
        }:
            raise GateEReplayError("candidate_runtime_changed")
        state: dict[str, object] = {
            "candidate": "B",
            "hash_seed": "909",
            "runtime": runtime,
            "runtime_guards": runtime_guards,
            "workspace": replay.workspace_b,
        }
        result: CandidateAResult | None = None
        for stage in _CANDIDATE_A_STAGES:
            stage_result = _execute_candidate_a_stage(
                replay,
                stage=stage,
                state=state,
            )
            if stage_result is not None:
                result = stage_result
        if result is None:
            raise GateEReplayError("candidate_evidence_missing")
        candidate_b = _load_candidate_evidence(
            result.evidence_path
        )
        if (
            candidate_b.runtime_python != candidate_a.runtime_python
            or candidate_b.runtime_uv != candidate_a.runtime_uv
        ):
            raise GateEReplayError("candidate_runtime_changed")
        trusted_b = _verify_candidate_trust_evidence(
            trust=_verified_anchored_temporary_path(
                temporary_root,
                temporary_path,
            ),
            candidate=candidate_b,
            artifact=candidate_b.artifact,
            approved_review=_verified_anchored_temporary_path(
                temporary_root,
                temporary_review,
            ),
            reviewed_candidate_evidence=candidate_a.evidence_path,
            read_only_paths=(replay.workspace_a,),
        )
        _compare_installed_package_inventories(
            candidate_a,
            candidate_b,
        )
        if (
            candidate_b.run_id != candidate_a.run_id
            or trusted_b["expected_run_id"] != candidate_a.run_id
        ):
            raise GateEReplayError("replay_run_id_mismatch")
        _compare_artifacts(
            candidate_a.artifact,
            candidate_b.artifact,
        )
        _verify_input_roots_independent(
            replay,
            candidate_a,
            candidate_b,
        )
        return ReplayBResult(
            evidence_path=candidate_b.evidence_path,
            artifact=candidate_b.artifact,
            run_id=candidate_b.run_id,
            trust_sha256=hashlib.sha256(blob).hexdigest(),
            compared_files=len(PORTFOLIO_ARTIFACT_FILES),
        )
    except GateEReplayError:
        raise
    except OSError as exc:
        raise GateEReplayError("trust_anchor_not_approved") from exc
    finally:
        cleanup_error: GateEReplayError | None = None
        workspace_error: GateEReplayError | None = None
        if temporary_root is not None:
            try:
                _cleanup_anchored_temporary_root(
                    temporary_root,
                    (temporary_path, temporary_review),
                )
            except GateEReplayError as exc:
                cleanup_error = exc
        try:
            workspace_a_after = _snapshot_candidate_a_workspace(
                replay.workspace_a
            )
            if workspace_a_after != workspace_a_before:
                workspace_error = GateEReplayError(
                    "candidate_a_workspace_changed"
                )
        except GateEReplayError as exc:
            workspace_error = GateEReplayError(
                "candidate_a_workspace_changed",
                cause_code=exc.code,
            )
        if workspace_error is not None and cleanup_error is not None:
            raise GateEReplayError(
                "candidate_a_workspace_changed_and_cleanup_failed",
                cause_code=cleanup_error.code,
            )
        if workspace_error is not None:
            raise workspace_error
        if cleanup_error is not None:
            raise cleanup_error
