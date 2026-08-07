from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from aquant.gate_e.versioned_audit import (
    VersionedAuditError,
    _remove_verifier_root,
    _verify_artifact_bundle,
    load_v02_audit_profile,
    materialize_historical_source,
    parse_v02_trust_manifest,
    resolve_v02_audit,
    validate_historical_source,
    validate_v02_offline_evidence,
)

PROJECT_ROOT = Path(__file__).parents[2]
PROFILE_PATH = PROJECT_ROOT / "configs/audit_profiles/v0.2_gate_e.json"


def _profile_payload() -> dict[str, object]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _write_profile(tmp_path: Path, mutate) -> Path:
    payload = _profile_payload()
    mutate(payload)
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


def test_v02_profile_resolves_fixed_history_and_trust_bindings() -> None:
    profile = load_v02_audit_profile(PROFILE_PATH)
    resolved = resolve_v02_audit(PROJECT_ROOT, profile)

    assert resolved.audit_target == profile.audit_tag_target
    assert resolved.implementation_commit == (
        "ae317a01c5c36a7a59836665917afec4a7377125"
    )
    assert resolved.expected_run_id == (
        "8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084"
    )
    assert resolved.uv_lock_sha256 == (
        "c8dfc359f40afde9849f7704dafe5449efe47bdef55fd7e29da4ef35214ae712"
    )


def test_v02_profile_rejects_moved_audit_tag_target(tmp_path: Path) -> None:
    profile = load_v02_audit_profile(
        _write_profile(
            tmp_path,
            lambda payload: payload.__setitem__("audit_tag_target", "0" * 40),
        )
    )

    with pytest.raises(VersionedAuditError) as captured:
        resolve_v02_audit(PROJECT_ROOT, profile)

    assert captured.value.code == "audit_tag_target_mismatch"


def test_v02_trust_parser_rejects_implementation_commit_mismatch() -> None:
    profile = load_v02_audit_profile(PROFILE_PATH)
    resolved = resolve_v02_audit(PROJECT_ROOT, profile)
    payload = json.loads(resolved.trust_manifest_bytes)
    payload["candidate_review"]["bindings"]["implementation_commit"] = "0" * 40

    with pytest.raises(VersionedAuditError) as captured:
        parse_v02_trust_manifest(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            .encode()
            + b"\n"
        )

    assert captured.value.code == "implementation_commit_mismatch"


def test_v02_trust_parser_rejects_expected_run_id_mismatch() -> None:
    profile = load_v02_audit_profile(PROFILE_PATH)
    resolved = resolve_v02_audit(PROJECT_ROOT, profile)
    payload = json.loads(resolved.trust_manifest_bytes)
    payload["artifact"]["actual_run_id"] = "0" * 64

    with pytest.raises(VersionedAuditError) as captured:
        parse_v02_trust_manifest(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            .encode()
            + b"\n"
        )

    assert captured.value.code == "expected_run_id_mismatch"


def test_v02_verifier_materializes_trusted_implementation_not_head() -> None:
    profile = load_v02_audit_profile(PROFILE_PATH)
    resolved = resolve_v02_audit(PROJECT_ROOT, profile)

    with materialize_historical_source(PROJECT_ROOT, resolved) as source:
        head = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        expected = subprocess.run(
            [
                "git",
                "-C",
                str(PROJECT_ROOT),
                "show",
                f"{resolved.implementation_commit}:src/aquant/backtest_cli.py",
            ],
            check=True,
            capture_output=True,
        ).stdout

        assert source != PROJECT_ROOT
        assert head == resolved.implementation_commit
        assert (source / "src/aquant/backtest_cli.py").read_bytes() == expected


def test_v02_verifier_rejects_modified_historical_lock_and_inputs() -> None:
    profile = load_v02_audit_profile(PROFILE_PATH)
    resolved = resolve_v02_audit(PROJECT_ROOT, profile)

    with materialize_historical_source(PROJECT_ROOT, resolved) as source:
        lock = source / "uv.lock"
        original_lock = lock.read_bytes()
        lock.write_bytes(original_lock + b"\n")
        with pytest.raises(VersionedAuditError) as captured:
            validate_historical_source(source, resolved)
        assert captured.value.code == "historical_uv_lock_mismatch"
        lock.write_bytes(original_lock)

        relative, _expected = next(iter(resolved.input_files))
        input_file = source / "release/v0.1-research/inputs" / relative
        original_input = input_file.read_bytes()
        input_file.write_bytes(original_input + b"\n")
        with pytest.raises(VersionedAuditError) as captured:
            validate_historical_source(source, resolved)
        assert captured.value.code == "historical_input_mismatch"


def test_current_implementation_digest_remains_source_sensitive(tmp_path: Path) -> None:
    worktree = tmp_path / "current"
    subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True,
        capture_output=True,
    )
    try:
        code = (
            "from aquant.backtest.runner import _implementation_digest; "
            "print(_implementation_digest())"
        )
        environment = {
            "PATH": os.environ["PATH"],
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(worktree / "src"),
        }
        before = subprocess.run(
            [sys.executable, "-c", code],
            cwd=worktree,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        changed = worktree / "src/aquant/backtest_cli.py"
        changed.write_bytes(changed.read_bytes() + b"\n# B0 controlled identity probe\n")
        after = subprocess.run(
            [sys.executable, "-c", code],
            cwd=worktree,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    finally:
        subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "worktree", "remove", "--force", str(worktree)],
            check=True,
            capture_output=True,
        )

    assert before != after
    assert after != "75740270db998f1bff4bb8bc7501b2ac3fa53e747815c6b90ce2eb9e57ec64c5"


def test_materialized_historical_source_ignores_active_worktree_copy(
    tmp_path: Path,
) -> None:
    profile = load_v02_audit_profile(PROFILE_PATH)
    resolved = resolve_v02_audit(PROJECT_ROOT, profile)
    active_copy = tmp_path / "active-copy"
    active_copy.mkdir()
    active_file = active_copy / "backtest_cli.py"
    active_file.write_text("changed outside history\n", encoding="utf-8")

    with materialize_historical_source(PROJECT_ROOT, resolved) as source:
        historical = source / "src/aquant/backtest_cli.py"
        assert hashlib.sha256(historical.read_bytes()).hexdigest() != hashlib.sha256(
            active_file.read_bytes()
        ).hexdigest()
        validate_historical_source(source, resolved)


def test_v02_profile_rejects_missing_historical_object(tmp_path: Path) -> None:
    repository = tmp_path / "empty-history"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)
    profile = load_v02_audit_profile(PROFILE_PATH)

    with pytest.raises(VersionedAuditError) as captured:
        resolve_v02_audit(repository, profile)

    assert captured.value.code == "historical_object_missing"


def test_verifier_cleanup_removes_read_only_controller_files(tmp_path: Path) -> None:
    root = tmp_path / "controller"
    locked = root / "cache" / ".lock"
    locked.parent.mkdir(parents=True)
    locked.write_text("sealed\n", encoding="utf-8")
    locked.chmod(0o400)
    locked.parent.chmod(0o500)

    _remove_verifier_root(root)

    assert not root.exists()


def test_offline_evidence_rejects_wrong_wheel_and_symlink_root(
    tmp_path: Path,
) -> None:
    profile = load_v02_audit_profile(PROFILE_PATH)
    bindings = resolve_v02_audit(PROJECT_ROOT, profile)
    evidence = tmp_path / "evidence"
    wheel = evidence / "project-wheel" / bindings.project_wheel_filename
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"not the formal wheel")

    with pytest.raises(VersionedAuditError) as captured:
        validate_v02_offline_evidence(evidence, bindings)
    assert captured.value.code == "project_wheel_mismatch"

    link = tmp_path / "evidence-link"
    link.symlink_to(evidence, target_is_directory=True)
    with pytest.raises(VersionedAuditError) as captured:
        validate_v02_offline_evidence(link, bindings)
    assert captured.value.code == "unsafe_offline_evidence"


def test_historical_inputs_reject_symlink_and_hardlink_aliases(
    tmp_path: Path,
) -> None:
    profile = load_v02_audit_profile(PROFILE_PATH)
    bindings = resolve_v02_audit(PROJECT_ROOT, profile)

    with materialize_historical_source(PROJECT_ROOT, bindings) as source:
        relative, _expected = next(iter(bindings.input_files))
        input_file = source / "release/v0.1-research/inputs" / relative
        backup = tmp_path / "input-backup"
        backup.write_bytes(input_file.read_bytes())
        input_file.unlink()
        input_file.symlink_to(backup)
        with pytest.raises(VersionedAuditError) as captured:
            validate_historical_source(source, bindings)
        assert captured.value.code == "historical_input_mismatch"

        input_file.unlink()
        input_file.hardlink_to(backup)
        with pytest.raises(VersionedAuditError) as captured:
            validate_historical_source(source, bindings)
        assert captured.value.code == "historical_input_mismatch"


def test_artifact_verifier_rejects_any_changed_formal_byte(tmp_path: Path) -> None:
    profile = load_v02_audit_profile(PROFILE_PATH)
    bindings = resolve_v02_audit(PROJECT_ROOT, profile)
    run_id = "a" * 64
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    run_json = json.dumps({"run_id": run_id}, sort_keys=True).encode()
    (artifact / "run.json").write_bytes(run_json)
    narrowed = replace(
        bindings,
        expected_run_id=run_id,
        artifact_files=(
            (
                PurePosixPath("run.json"),
                len(run_json),
                hashlib.sha256(run_json).hexdigest(),
            ),
        ),
    )
    _verify_artifact_bundle(artifact, narrowed)

    (artifact / "run.json").write_bytes(run_json + b"\n")
    with pytest.raises(VersionedAuditError) as captured:
        _verify_artifact_bundle(artifact, narrowed)
    assert captured.value.code == "artifact_mismatch"
