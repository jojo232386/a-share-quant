"""Versioned, fail-closed verification for the frozen v0.2 audit.

This module deliberately separates the v0.2 historical implementation from
the active checkout.  It never changes the current implementation identity;
it only selects, materializes, and validates the historical identity named by
the v0.2 trust material.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from argparse import ArgumentParser
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_PROFILE_KEYS = frozenset(
    {
        "approval_commit",
        "audit_tag",
        "audit_tag_target",
        "historical_entry",
        "release_version",
        "schema_version",
        "trust_anchor_commit",
        "trust_manifest",
    }
)
_V02_PROFILE_RELATIVE = PurePosixPath("configs/audit_profiles/v0.2_gate_e.json")
_HISTORICAL_CONFIG = PurePosixPath("configs/releases/v0.2_gate_e.json")
_HISTORICAL_INPUT_ROOT = PurePosixPath("release/v0.1-research/inputs")
_CANDIDATE_REVIEW_INSTALL_LOCK = re.compile(
    r"install lock SHA-256[^0-9a-f]*([0-9a-f]{64})"
)


class VersionedAuditError(RuntimeError):
    """Stable fail-closed error raised by the versioned audit verifier."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class V02AuditProfile:
    """Small release selector; frozen hashes remain in historical trust."""

    audit_tag: str
    audit_tag_target: str
    historical_entry: str
    release_version: str
    schema_version: str
    trust_manifest: PurePosixPath
    trust_anchor_commit: str
    approval_commit: str


@dataclass(frozen=True)
class V02TrustBindings:
    """Verified values read from the immutable v0.2 trust material."""

    audit_target: str
    implementation_commit: str
    expected_run_id: str
    uv_lock_sha256: str
    config_sha256: str
    project_wheel_filename: str
    project_wheel_sha256: str
    project_wheel_size: int
    wheelhouse_manifest_sha256: str
    wheelhouse_manifest_size: int
    install_lock_sha256: str
    python_sha256: str
    python_size: int
    uv_sha256: str
    uv_size: int
    input_files: tuple[tuple[PurePosixPath, str], ...]
    artifact_files: tuple[tuple[PurePosixPath, int, str], ...]
    trust_manifest_bytes: bytes


@dataclass(frozen=True)
class V02OfflineEvidence:
    """Caller-supplied, sealed formal files used only by the v0.2 replay."""

    root: Path
    project_wheel: Path
    wheelhouse: Path
    wheelhouse_manifest: Path
    install_lock: Path


@dataclass(frozen=True)
class V02AuditResult:
    """Minimal structured result of one successful historical replay."""

    audit_target: str
    implementation_commit: str
    run_id: str
    artifact_file_count: int


def _controlled_git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/private/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "PYTHONNOUSERSITE": "1",
        "TZ": "Asia/Shanghai",
    }


def _git(
    repository_root: Path,
    *arguments: str,
    binary: bool = False,
    path_output: bool = False,
    missing_code: str = "historical_object_missing",
) -> str | bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), *arguments],
            check=False,
            capture_output=True,
            env=_controlled_git_environment(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VersionedAuditError(missing_code) from exc
    if completed.returncode != 0:
        raise VersionedAuditError(missing_code)
    if binary:
        return completed.stdout
    try:
        if path_output:
            return os.fsdecode(completed.stdout).strip()
        return completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise VersionedAuditError(missing_code) from exc


def _require_safe_relative(value: object, *, code: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise VersionedAuditError(code)
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise VersionedAuditError(code)
    return candidate


def _require_hash(value: object, *, code: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise VersionedAuditError(code)
    return value


def _require_commit(value: object, *, code: str) -> str:
    if type(value) is not str or _COMMIT_RE.fullmatch(value) is None:
        raise VersionedAuditError(code)
    return value


def _read_single_link(path: Path, *, code: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VersionedAuditError(code) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or path.is_symlink()
    ):
        raise VersionedAuditError(code)
    try:
        content = path.read_bytes()
        final = path.lstat()
    except OSError as exc:
        raise VersionedAuditError(code) from exc
    if (
        len(content) != metadata.st_size
        or (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns)
        != (final.st_dev, final.st_ino, final.st_mtime_ns, final.st_ctime_ns)
    ):
        raise VersionedAuditError(code)
    return content


def _require_size(value: object, *, code: str) -> int:
    if type(value) is not int or value <= 0:
        raise VersionedAuditError(code)
    return value


def _safe_directory(path: Path, *, code: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError) as exc:
        raise VersionedAuditError(code) from exc
    if (
        resolved != path
        or not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
    ):
        raise VersionedAuditError(code)
    return resolved


def load_v02_audit_profile(path: Path) -> V02AuditProfile:
    """Load the small, strict v0.2 release selector."""
    try:
        raw = _read_single_link(path, code="invalid_audit_profile")
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionedAuditError("invalid_audit_profile") from exc
    if not isinstance(payload, dict) or set(payload) != _PROFILE_KEYS:
        raise VersionedAuditError("invalid_audit_profile")
    profile = V02AuditProfile(
        audit_tag=payload["audit_tag"],
        audit_tag_target=_require_commit(
            payload["audit_tag_target"], code="invalid_audit_profile"
        ),
        historical_entry=payload["historical_entry"],
        release_version=payload["release_version"],
        schema_version=payload["schema_version"],
        trust_manifest=_require_safe_relative(
            payload["trust_manifest"], code="invalid_audit_profile"
        ),
        trust_anchor_commit=_require_commit(
            payload["trust_anchor_commit"], code="invalid_audit_profile"
        ),
        approval_commit=_require_commit(
            payload["approval_commit"], code="invalid_audit_profile"
        ),
    )
    if (
        profile.audit_tag != "v0.2-gate-e-public-audit"
        or profile.historical_entry != "candidate-a"
        or profile.release_version != "v0.2"
        or profile.schema_version != "1.0"
        or profile.trust_manifest
        != PurePosixPath("release/v0.2-gate-e/trust_manifest.json")
    ):
        raise VersionedAuditError("invalid_audit_profile")
    return profile


def _parse_manifest_bytes(raw: bytes) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionedAuditError("invalid_trust_manifest") from exc
    if not isinstance(payload, dict):
        raise VersionedAuditError("invalid_trust_manifest")
    return payload


def parse_v02_trust_manifest(raw: bytes) -> V02TrustBindings:
    """Parse and cross-check the immutable, canonical v0.2 trust bytes."""
    payload = _parse_manifest_bytes(raw)
    implementation_commit = _require_commit(
        payload.get("implementation_commit"), code="invalid_trust_manifest"
    )
    expected_run_id = _require_hash(
        payload.get("expected_run_id"), code="invalid_trust_manifest"
    )
    config = payload.get("config")
    uv_lock = payload.get("uv_lock")
    project_wheel = payload.get("project_wheel")
    wheelhouse = payload.get("wheelhouse")
    candidate_review = payload.get("candidate_review")
    artifact = payload.get("artifact")
    python = payload.get("python")
    uv = payload.get("uv")
    if not all(
        isinstance(value, dict)
        for value in (
            config,
            uv_lock,
            project_wheel,
            wheelhouse,
            candidate_review,
            artifact,
            python,
            uv,
        )
    ):
        raise VersionedAuditError("invalid_trust_manifest")
    config_payload = config.get("payload")
    if not isinstance(config_payload, dict):
        raise VersionedAuditError("invalid_trust_manifest")
    uv_lock_sha256 = _require_hash(
        uv_lock.get("sha256"), code="invalid_trust_manifest"
    )
    if config_payload.get("uv_lock_sha256") != uv_lock_sha256:
        raise VersionedAuditError("historical_uv_lock_mismatch")
    config_sha256 = _require_hash(
        config.get("sha256"), code="invalid_trust_manifest"
    )
    project_wheel_sha256 = _require_hash(
        project_wheel.get("sha256"), code="invalid_trust_manifest"
    )
    project_wheel_filename = project_wheel.get("filename")
    project_wheel_size = _require_size(
        project_wheel.get("size"), code="invalid_trust_manifest"
    )
    if (
        type(project_wheel_filename) is not str
    ):
        raise VersionedAuditError("invalid_trust_manifest")
    wheelhouse_manifest = wheelhouse.get("manifest")
    if not isinstance(wheelhouse_manifest, dict):
        raise VersionedAuditError("invalid_trust_manifest")
    wheelhouse_manifest_sha256 = _require_hash(
        wheelhouse_manifest.get("sha256"), code="invalid_trust_manifest"
    )
    wheelhouse_manifest_size = _require_size(
        wheelhouse_manifest.get("size"), code="invalid_trust_manifest"
    )
    python_sha256 = _require_hash(
        python.get("sha256"), code="invalid_trust_manifest"
    )
    python_size = _require_size(python.get("size"), code="invalid_trust_manifest")
    uv_sha256 = _require_hash(uv.get("sha256"), code="invalid_trust_manifest")
    uv_size = _require_size(uv.get("size"), code="invalid_trust_manifest")
    bindings = candidate_review.get("bindings")
    if not isinstance(bindings, dict):
        raise VersionedAuditError("invalid_trust_manifest")
    if bindings.get("implementation_commit") != implementation_commit:
        raise VersionedAuditError("implementation_commit_mismatch")
    if bindings.get("expected_run_id") != expected_run_id:
        raise VersionedAuditError("expected_run_id_mismatch")
    if bindings.get("project_wheel_sha256") != project_wheel_sha256:
        raise VersionedAuditError("project_wheel_mismatch")
    if artifact.get("actual_run_id") != expected_run_id:
        raise VersionedAuditError("expected_run_id_mismatch")
    inputs = config_payload.get("input_files")
    files = artifact.get("files")
    if not isinstance(inputs, dict) or not isinstance(files, list):
        raise VersionedAuditError("invalid_trust_manifest")
    input_files = tuple(
        sorted(
            (
                _require_safe_relative(name, code="invalid_trust_manifest"),
                _require_hash(digest, code="invalid_trust_manifest"),
            )
            for name, digest in inputs.items()
        )
    )
    artifact_files: list[tuple[PurePosixPath, int, str]] = []
    for item in files:
        if not isinstance(item, dict):
            raise VersionedAuditError("invalid_trust_manifest")
        name = _require_safe_relative(item.get("name"), code="invalid_trust_manifest")
        size = item.get("size")
        if type(size) is not int or size <= 0:
            raise VersionedAuditError("invalid_trust_manifest")
        artifact_files.append(
            (
                name,
                size,
                _require_hash(item.get("sha256"), code="invalid_trust_manifest"),
            )
        )
    if len({name for name, _, _ in artifact_files}) != len(artifact_files):
        raise VersionedAuditError("invalid_trust_manifest")
    return V02TrustBindings(
        audit_target="",
        implementation_commit=implementation_commit,
        expected_run_id=expected_run_id,
        uv_lock_sha256=uv_lock_sha256,
        config_sha256=config_sha256,
        project_wheel_filename=project_wheel_filename,
        project_wheel_sha256=project_wheel_sha256,
        project_wheel_size=project_wheel_size,
        wheelhouse_manifest_sha256=wheelhouse_manifest_sha256,
        wheelhouse_manifest_size=wheelhouse_manifest_size,
        install_lock_sha256="",
        python_sha256=python_sha256,
        python_size=python_size,
        uv_sha256=uv_sha256,
        uv_size=uv_size,
        input_files=input_files,
        artifact_files=tuple(sorted(artifact_files)),
        trust_manifest_bytes=raw,
    )


def _read_git_blob(
    repository_root: Path,
    commit: str,
    relative: PurePosixPath,
) -> bytes:
    object_spec = f"{commit}:{relative.as_posix()}"
    return _git(
        repository_root,
        "show",
        object_spec,
        binary=True,
        missing_code="historical_object_missing",
    )


def _verify_git_history(
    repository_root: Path,
    profile: V02AuditProfile,
) -> str:
    target = _git(
        repository_root,
        "rev-parse",
        "--verify",
        f"refs/tags/{profile.audit_tag}^{{commit}}",
    )
    if target != profile.audit_tag_target:
        raise VersionedAuditError("audit_tag_target_mismatch")
    _git(repository_root, "cat-file", "-e", f"{target}^{{commit}}")
    replace_refs = _git(
        repository_root,
        "for-each-ref",
        "--format=%(refname)",
        "refs/replace",
    )
    if replace_refs:
        raise VersionedAuditError("historical_object_missing")
    for commit in (profile.trust_anchor_commit, profile.approval_commit):
        if _git(repository_root, "cat-file", "-t", commit) != "commit":
            raise VersionedAuditError("historical_object_missing")
    for first, second in (
        (profile.trust_anchor_commit, profile.approval_commit),
        (profile.approval_commit, profile.audit_tag_target),
    ):
        _git(
            repository_root,
            "merge-base",
            "--is-ancestor",
            first,
            second,
            missing_code="historical_object_missing",
        )
    return target


def resolve_v02_audit(
    repository_root: Path,
    profile: V02AuditProfile,
) -> V02TrustBindings:
    """Resolve all frozen v0.2 bindings from Git, never active files."""
    root = _safe_directory(repository_root, code="historical_object_missing")
    top_level = _git(root, "rev-parse", "--show-toplevel", path_output=True)
    if not isinstance(top_level, str) or Path(top_level).resolve() != root:
        raise VersionedAuditError("historical_object_missing")
    target = _verify_git_history(root, profile)
    trust_bytes = _read_git_blob(root, target, profile.trust_manifest)
    parsed = parse_v02_trust_manifest(trust_bytes)
    _git(root, "cat-file", "-e", f"{parsed.implementation_commit}^{{commit}}")
    _git(
        root,
        "merge-base",
        "--is-ancestor",
        parsed.implementation_commit,
        target,
        missing_code="implementation_commit_mismatch",
    )
    anchored = _read_git_blob(
        root,
        profile.trust_anchor_commit,
        profile.trust_manifest,
    )
    if anchored != trust_bytes:
        raise VersionedAuditError("trust_anchor_mismatch")
    review = _parse_manifest_bytes(trust_bytes).get("candidate_review")
    if not isinstance(review, dict):
        raise VersionedAuditError("invalid_trust_manifest")
    review_path = _require_safe_relative(
        review.get("path"), code="invalid_trust_manifest"
    )
    review_bytes = _read_git_blob(root, target, review_path)
    if hashlib.sha256(review_bytes).hexdigest() != _require_hash(
        review.get("sha256"), code="invalid_trust_manifest"
    ):
        raise VersionedAuditError("candidate_review_mismatch")
    approval_review = _read_git_blob(
        root,
        profile.approval_commit,
        review_path,
    )
    if approval_review != review_bytes:
        raise VersionedAuditError("approval_mismatch")
    matches = _CANDIDATE_REVIEW_INSTALL_LOCK.findall(
        review_bytes.decode("utf-8", errors="strict")
    )
    if len(matches) != 1:
        raise VersionedAuditError("candidate_review_mismatch")
    return V02TrustBindings(
        audit_target=target,
        implementation_commit=parsed.implementation_commit,
        expected_run_id=parsed.expected_run_id,
        uv_lock_sha256=parsed.uv_lock_sha256,
        config_sha256=parsed.config_sha256,
        project_wheel_filename=parsed.project_wheel_filename,
        project_wheel_sha256=parsed.project_wheel_sha256,
        project_wheel_size=parsed.project_wheel_size,
        wheelhouse_manifest_sha256=parsed.wheelhouse_manifest_sha256,
        wheelhouse_manifest_size=parsed.wheelhouse_manifest_size,
        install_lock_sha256=matches[0],
        python_sha256=parsed.python_sha256,
        python_size=parsed.python_size,
        uv_sha256=parsed.uv_sha256,
        uv_size=parsed.uv_size,
        input_files=parsed.input_files,
        artifact_files=parsed.artifact_files,
        trust_manifest_bytes=trust_bytes,
    )


@contextlib.contextmanager
def materialize_historical_source(
    repository_root: Path,
    bindings: V02TrustBindings,
) -> Iterator[Path]:
    """Materialize only the trust-named implementation in a detached tree."""
    root = _safe_directory(repository_root, code="historical_object_missing")
    temporary_root = Path(tempfile.mkdtemp(prefix="aquant-v02-audit-")).resolve()
    source = temporary_root / "implementation"
    created = False
    try:
        _git(
            root,
            "worktree",
            "add",
            "--detach",
            str(source),
            bindings.implementation_commit,
            missing_code="historical_materialization_failed",
        )
        created = True
        source = _safe_directory(source, code="historical_materialization_failed")
        if source == root or _git(source, "rev-parse", "HEAD") != bindings.implementation_commit:
            raise VersionedAuditError("historical_source_substitution")
        yield source
    finally:
        if created:
            _git(
                root,
                "worktree",
                "remove",
                "--force",
                str(source),
                missing_code="historical_cleanup_failed",
            )
        try:
            shutil.rmtree(temporary_root)
        except OSError as exc:
            raise VersionedAuditError("historical_cleanup_failed") from exc


def validate_historical_source(
    source: Path,
    bindings: V02TrustBindings,
) -> None:
    """Check lock, config and every frozen input in historical source."""
    root = _safe_directory(source, code="historical_source_substitution")
    if _git(root, "rev-parse", "HEAD") != bindings.implementation_commit:
        raise VersionedAuditError("historical_source_substitution")
    lock_bytes = _read_single_link(
        root / "uv.lock",
        code="historical_uv_lock_mismatch",
    )
    if hashlib.sha256(lock_bytes).hexdigest() != bindings.uv_lock_sha256:
        raise VersionedAuditError("historical_uv_lock_mismatch")
    config = root / _HISTORICAL_CONFIG.as_posix()
    config_bytes = _read_single_link(
        config,
        code="historical_config_mismatch",
    )
    if hashlib.sha256(config_bytes).hexdigest() != bindings.config_sha256:
        raise VersionedAuditError("historical_config_mismatch")
    input_root = root / _HISTORICAL_INPUT_ROOT.as_posix()
    for relative, expected_sha256 in bindings.input_files:
        input_file = input_root / relative.as_posix()
        observed = hashlib.sha256(
            _read_single_link(input_file, code="historical_input_mismatch")
        ).hexdigest()
        if observed != expected_sha256:
            raise VersionedAuditError("historical_input_mismatch")


def _digest_file(path: Path, *, code: str) -> tuple[int, str]:
    content = _read_single_link(path, code=code)
    return len(content), hashlib.sha256(content).hexdigest()


def validate_v02_offline_evidence(
    evidence_root: Path,
    bindings: V02TrustBindings,
) -> V02OfflineEvidence:
    """Validate sealed formal wheel inputs before any historical execution."""
    from aquant.gate_e.environment import (
        GateEEnvironmentError,
        inspect_project_wheel,
        verify_wheelhouse,
        wheelhouse_requirements_from_manifest,
    )

    root = _safe_directory(evidence_root, code="unsafe_offline_evidence")
    project_wheel = root / "project-wheel" / bindings.project_wheel_filename
    wheelhouse = root / "sealed" / "wheelhouse"
    wheelhouse_manifest = root / "sealed" / "wheelhouse_manifest.json"
    install_lock = root / "sealed" / "requirements.install.lock.txt"
    wheel_size, wheel_sha256 = _digest_file(
        project_wheel,
        code="project_wheel_mismatch",
    )
    if (
        wheel_size != bindings.project_wheel_size
        or wheel_sha256 != bindings.project_wheel_sha256
    ):
        raise VersionedAuditError("project_wheel_mismatch")
    manifest_size, manifest_sha256 = _digest_file(
        wheelhouse_manifest,
        code="wheelhouse_mismatch",
    )
    if (
        manifest_size != bindings.wheelhouse_manifest_size
        or manifest_sha256 != bindings.wheelhouse_manifest_sha256
    ):
        raise VersionedAuditError("wheelhouse_mismatch")
    lock_size, lock_sha256 = _digest_file(
        install_lock,
        code="wheelhouse_mismatch",
    )
    if lock_size <= 0 or lock_sha256 != bindings.install_lock_sha256:
        raise VersionedAuditError("wheelhouse_mismatch")
    _safe_directory(wheelhouse, code="unsafe_offline_evidence")
    try:
        project = inspect_project_wheel(project_wheel)
        requirements = wheelhouse_requirements_from_manifest(
            wheelhouse_manifest
        )
        verify_wheelhouse(
            wheelhouse,
            expected_requirements=requirements,
            manifest=wheelhouse_manifest,
            install_lock=install_lock,
        )
    except GateEEnvironmentError as exc:
        raise VersionedAuditError("wheelhouse_mismatch") from exc
    if project.sha256 != bindings.project_wheel_sha256:
        raise VersionedAuditError("project_wheel_mismatch")
    return V02OfflineEvidence(
        root=root,
        project_wheel=project_wheel,
        wheelhouse=wheelhouse,
        wheelhouse_manifest=wheelhouse_manifest,
        install_lock=install_lock,
    )


def _verify_historical_runtimes(bindings: V02TrustBindings) -> tuple[Path, Path]:
    from aquant.gate_e.environment import (
        GateEEnvironmentError,
        canonical_python_executable,
        canonical_uv_executable,
    )

    try:
        python = canonical_python_executable()
        uv = canonical_uv_executable()
    except GateEEnvironmentError as exc:
        raise VersionedAuditError("historical_runtime_mismatch") from exc
    for executable, size, digest in (
        (python, bindings.python_size, bindings.python_sha256),
        (uv, bindings.uv_size, bindings.uv_sha256),
    ):
        observed_size, observed_digest = _digest_file(
            executable,
            code="historical_runtime_mismatch",
        )
        if observed_size != size or observed_digest != digest:
            raise VersionedAuditError("historical_runtime_mismatch")
    return python, uv


def _verify_artifact_bundle(
    artifact: Path,
    bindings: V02TrustBindings,
) -> None:
    root = _safe_directory(artifact, code="artifact_mismatch")
    expected = {name: (size, digest) for name, size, digest in bindings.artifact_files}
    try:
        observed = {
            PurePosixPath(child.name): child
            for child in root.iterdir()
        }
    except OSError as exc:
        raise VersionedAuditError("artifact_mismatch") from exc
    if set(observed) != set(expected):
        raise VersionedAuditError("artifact_mismatch")
    for name, child in observed.items():
        size, digest = _digest_file(child, code="artifact_mismatch")
        if (size, digest) != expected[name]:
            raise VersionedAuditError("artifact_mismatch")
    run_json = _read_single_link(root / "run.json", code="artifact_mismatch")
    try:
        run = json.loads(run_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionedAuditError("artifact_mismatch") from exc
    if not isinstance(run, dict) or run.get("run_id") != bindings.expected_run_id:
        raise VersionedAuditError("expected_run_id_mismatch")


def _remove_verifier_root(root: Path) -> None:
    try:
        entries = sorted(
            root.rglob("*"),
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for entry in entries:
            metadata = entry.lstat()
            mode = stat.S_IRWXU if stat.S_ISDIR(metadata.st_mode) else stat.S_IRUSR | stat.S_IWUSR
            os.chmod(entry, mode, follow_symlinks=False)
        os.chmod(root, stat.S_IRWXU, follow_symlinks=False)
    except OSError as exc:
        raise VersionedAuditError("historical_cleanup_failed") from exc

    def retry_with_owner_write(function, path, _exception) -> None:
        try:
            os.chmod(Path(path).parent, stat.S_IRWXU, follow_symlinks=False)
            os.chmod(path, stat.S_IRWXU, follow_symlinks=False)
            function(path)
        except OSError as exc:
            raise VersionedAuditError("historical_cleanup_failed") from exc

    try:
        shutil.rmtree(root, onerror=retry_with_owner_write)
    except OSError as exc:
        raise VersionedAuditError("historical_cleanup_failed") from exc


def verify_v02_historical_audit(
    repository_root: Path,
    *,
    profile_path: Path,
    evidence_root: Path,
) -> V02AuditResult:
    """Run the historical Candidate-A verifier in a new offline environment."""
    from aquant.gate_e.environment import (
        GateEEnvironmentError,
        capture_runtime_execution_guard,
        copy_gate_e_config,
        install_gate_e_environment,
        make_environment_layout,
        run_sandboxed_with_environment_guard,
        stage_environment_inputs,
        wheelhouse_requirements_from_manifest,
    )

    profile = load_v02_audit_profile(profile_path)
    bindings = resolve_v02_audit(repository_root, profile)
    evidence = validate_v02_offline_evidence(evidence_root, bindings)
    python, uv = _verify_historical_runtimes(bindings)
    temporary_root = Path(tempfile.mkdtemp(prefix="aquant-v02-execution-")).resolve()
    failure: BaseException | None = None
    try:
        with materialize_historical_source(repository_root, bindings) as source:
            validate_historical_source(source, bindings)
            try:
                layout = make_environment_layout(
                    temporary_root / "candidate-a",
                    repository_root=source,
                    base_python=python,
                )
                uv_guard = capture_runtime_execution_guard(uv)
                installed = install_gate_e_environment(
                    layout,
                    project_wheel=evidence.project_wheel,
                    expected_project_sha256=bindings.project_wheel_sha256,
                    wheelhouse=evidence.wheelhouse,
                    wheelhouse_manifest=evidence.wheelhouse_manifest,
                    install_lock=evidence.install_lock,
                    expected_requirements=wheelhouse_requirements_from_manifest(
                        evidence.wheelhouse_manifest
                    ),
                    hash_seed="101",
                    uv_executable=uv,
                    expected_uv_guard=uv_guard,
                    require_gate_e_cli=False,
                )
                copied = stage_environment_inputs(
                    layout,
                    source / "release/v0.1-research",
                )
                if copied.file_count != len(bindings.input_files):
                    raise VersionedAuditError("historical_input_mismatch")
                copy_gate_e_config(
                    layout,
                    source / _HISTORICAL_CONFIG.as_posix(),
                    expected_sha256=bindings.config_sha256,
                )
            except GateEEnvironmentError as exc:
                raise VersionedAuditError("historical_environment_failed") from exc
            try:
                run = run_sandboxed_with_environment_guard(
                    layout,
                    [
                        str(installed.portfolio_cli),
                        "run-config",
                        "--config",
                        "configs/releases/v0.2_gate_e.json",
                    ],
                    expected_execution_guard=installed.execution_guard,
                    hash_seed="101",
                    timeout_seconds=900,
                )
            except GateEEnvironmentError as exc:
                raise VersionedAuditError("historical_replay_failed") from exc
            if run.returncode != 0:
                raise VersionedAuditError("historical_replay_failed")
            try:
                response = json.loads(run.stdout)
            except json.JSONDecodeError as exc:
                raise VersionedAuditError("historical_replay_failed") from exc
            if (
                not isinstance(response, dict)
                or response.get("run_id") != bindings.expected_run_id
            ):
                raise VersionedAuditError("expected_run_id_mismatch")
            artifact = layout.output_root / "portfolios" / bindings.expected_run_id
            try:
                reverse = run_sandboxed_with_environment_guard(
                    layout,
                    [
                        str(installed.portfolio_cli),
                        "verify",
                        "--project-root",
                        ".",
                        "--artifact",
                        f"outputs/portfolios/{bindings.expected_run_id}",
                        "--expected-run-id",
                        bindings.expected_run_id,
                    ],
                    expected_execution_guard=installed.execution_guard,
                    hash_seed="101",
                    timeout_seconds=900,
                    verification_mode_files=(layout.config_path,),
                )
            except GateEEnvironmentError as exc:
                raise VersionedAuditError("historical_replay_failed") from exc
            if reverse.returncode != 0:
                raise VersionedAuditError("historical_replay_failed")
            _verify_artifact_bundle(artifact, bindings)
            return V02AuditResult(
                audit_target=bindings.audit_target,
                implementation_commit=bindings.implementation_commit,
                run_id=bindings.expected_run_id,
                artifact_file_count=len(bindings.artifact_files),
            )
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            _remove_verifier_root(temporary_root)
        except VersionedAuditError:
            if failure is None:
                raise


def main(argv: list[str] | None = None) -> int:
    """Run the explicit v0.2 historical verifier without a project CLI change."""
    parser = ArgumentParser(prog="python -m aquant.gate_e.versioned_audit")
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument(
        "--profile",
        default=_V02_PROFILE_RELATIVE.as_posix(),
    )
    arguments = parser.parse_args(argv)
    try:
        result = verify_v02_historical_audit(
            Path(arguments.repository_root),
            profile_path=Path(arguments.profile),
            evidence_root=Path(arguments.evidence_root),
        )
    except VersionedAuditError as exc:
        print(json.dumps({"status": "error", "error_code": exc.code}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "artifact_file_count": result.artifact_file_count,
                "audit_target": result.audit_target,
                "implementation_commit": result.implementation_commit,
                "run_id": result.run_id,
                "status": "verified",
            },
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "V02AuditProfile",
    "V02AuditResult",
    "V02OfflineEvidence",
    "V02TrustBindings",
    "VersionedAuditError",
    "load_v02_audit_profile",
    "materialize_historical_source",
    "parse_v02_trust_manifest",
    "resolve_v02_audit",
    "validate_historical_source",
    "validate_v02_offline_evidence",
    "verify_v02_historical_audit",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
