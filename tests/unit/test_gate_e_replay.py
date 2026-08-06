from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import aquant.gate_e.cli as cli_module
import aquant.gate_e.replay as replay_module
from aquant.gate_e.config import load_gate_e_config
from aquant.gate_e.environment import (
    GateEEnvironmentLayout,
    InstalledEnvironmentEvidence,
    WheelEvidence,
    wheelhouse_requirements_from_manifest,
)
from aquant.gate_e.inputs import stage_gate_e_input_root
from aquant.gate_e.replay import (
    CandidateAResult,
    GateEReplay,
    GateEReplayError,
    canonical_python_executable,
    canonical_uv_executable,
    replay_environment_b,
    run_candidate_a,
    verify_approved_review,
)

PROJECT_ROOT = Path(__file__).parents[2]
FIXED_PYTHON = canonical_python_executable()

CANDIDATE_REVIEW_PATH = Path(
    "outputs/Work_Buddy候选A复核_v0.2_Gate_E.md"
)
TRUST_APPROVAL_PATH = Path(
    "outputs/Work_Buddy信任锚复核_v0.2_Gate_E.md"
)


def _candidate_review_text(
    *,
    implementation_commit: str = "a" * 40,
    candidate_evidence_sha256: str = "b" * 64,
    expected_run_id: str = "c" * 64,
    artifact_manifest_sha256: str = "d" * 64,
    project_wheel_sha256: str = "e" * 64,
) -> str:
    return (
        "project = a-share-quant\n"
        "version = v0.2\n"
        "gate = E\n"
        "review_kind = candidate_a\n"
        "decision = PASS\n"
        "P0 = 0\n"
        "P1 = 0\n"
        "P2 = 0\n"
        f"implementation_commit = {implementation_commit}\n"
        f"candidate_evidence_sha256 = {candidate_evidence_sha256}\n"
        f"expected_run_id = {expected_run_id}\n"
        f"artifact_manifest_sha256 = {artifact_manifest_sha256}\n"
        f"project_wheel_sha256 = {project_wheel_sha256}\n"
    )


def _trust_approval_text(
    *,
    trust_anchor_commit: str,
    trust_sha256: str,
    candidate_review_sha256: str,
    implementation_commit: str = "a" * 40,
    expected_run_id: str = "c" * 64,
) -> str:
    return (
        "project = a-share-quant\n"
        "version = v0.2\n"
        "gate = E\n"
        "review_kind = trust_anchor\n"
        "decision = PASS\n"
        "P0 = 0\n"
        "P1 = 0\n"
        "P2 = 0\n"
        f"trust_anchor_commit = {trust_anchor_commit}\n"
        "trust_path = release/v0.2-gate-e/trust_manifest.json\n"
        f"trust_sha256 = {trust_sha256}\n"
        f"candidate_review_path = {CANDIDATE_REVIEW_PATH.as_posix()}\n"
        f"candidate_review_sha256 = {candidate_review_sha256}\n"
        f"implementation_commit = {implementation_commit}\n"
        f"expected_run_id = {expected_run_id}\n"
    )


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _same_tree_commit(repo: Path, commit: str) -> str:
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    parents = _git(repo, "show", "-s", "--format=%P", commit).split()
    arguments = ["commit-tree", tree]
    for parent in parents:
        arguments.extend(("-p", parent))
    arguments.extend(("-m", "same-tree replacement"))
    return _git(repo, *arguments)


def _approval_repository(
    tmp_path: Path,
    *,
    candidate_at_anchor: bool = True,
    mutate_candidate_after_anchor: bool = False,
    wrong_approval_binding: bool = False,
    approval_review: bool = True,
    approval_mutation: tuple[str, str] | None = None,
) -> tuple[GateEReplay, str, str, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Gate E Test")
    _git(repo, "config", "user.email", "gate-e@example.invalid")
    trust = repo / "release/v0.2-gate-e/trust_manifest.json"
    candidate_review = repo / CANDIDATE_REVIEW_PATH
    approval = repo / TRUST_APPROVAL_PATH
    trust.parent.mkdir(parents=True)
    candidate_review.parent.mkdir(parents=True)
    trust.write_bytes(b'{"gate":"E"}\n')
    candidate_bytes = _candidate_review_text().encode()
    if candidate_at_anchor:
        candidate_review.write_bytes(candidate_bytes)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "anchor")
    anchor = _git(repo, "rev-parse", "HEAD")
    if not candidate_at_anchor:
        candidate_review.write_bytes(candidate_bytes)
    if mutate_candidate_after_anchor:
        candidate_review.write_bytes(candidate_bytes + b"\nchanged\n")
    if approval_review:
        approval_text = _trust_approval_text(
                trust_anchor_commit=anchor,
                trust_sha256=(
                    "0" * 64
                    if wrong_approval_binding
                    else hashlib.sha256(trust.read_bytes()).hexdigest()
                ),
                candidate_review_sha256=hashlib.sha256(
                    candidate_bytes
                ).hexdigest(),
            )
        if approval_mutation is not None:
            field, value = approval_mutation
            approval_text = "\n".join(
                f"{field} = {value}"
                if line.startswith(f"{field} = ")
                else line
                for line in approval_text.splitlines()
            ) + "\n"
        approval.write_text(approval_text, encoding="utf-8")
    else:
        (repo / "approval-marker.txt").write_text("no review\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "approval")
    approval_commit = _git(repo, "rev-parse", "HEAD")
    replay = replace(
        _replay(tmp_path),
        repository_root=repo,
        workspace_a=tmp_path / "environment-a",
        workspace_b=tmp_path / "environment-b",
    )
    replay.workspace_a.mkdir()
    evidence_path = replay.workspace_a / "candidate-a-evidence.json"
    evidence_path.write_bytes(b'{"candidate":"A"}\n')
    candidate = SimpleNamespace(
        evidence_path=evidence_path,
        candidate="A",
        implementation_commit=replay.implementation_commit,
        v01_tag_commit=replay.v01_tag_commit,
        run_id="c" * 64,
        project_wheel=replay.project_wheel,
        wheelhouse_root=replay.wheelhouse_root,
        artifact=tmp_path / ("c" * 64),
    )
    return replay, anchor, approval_commit, candidate


def _replay(tmp_path: Path) -> GateEReplay:
    controls = tmp_path / "runtime-dependencies"
    return GateEReplay(
        repository_root=PROJECT_ROOT,
        release_root=PROJECT_ROOT / "release/v0.1-research",
        config_path=PROJECT_ROOT / "configs/releases/v0.2_gate_e.json",
        project_wheel=tmp_path / "a_share_quant-0.2.0-py3-none-any.whl",
        wheelhouse_root=controls / "wheelhouse",
        wheelhouse_manifest=controls / "wheelhouse_manifest.json",
        install_lock=controls / "requirements.install.lock.txt",
        uv_lock=PROJECT_ROOT / "uv.lock",
        workspace_a=tmp_path / "environment-a",
        workspace_b=tmp_path / "environment-b",
        implementation_commit="a" * 40,
        v01_tag_commit="b" * 40,
        python_executable=FIXED_PYTHON,
        uv_executable=canonical_uv_executable(),
        expected_requirements=(("a-share-quant", "0.2.0"),),
    )


def _mock_successful_replay_b(tmp_path, monkeypatch):
    replay = _replay(tmp_path)
    replay.workspace_a.mkdir()
    replay.wheelhouse_root.mkdir(parents=True)
    run_id = "c" * 64
    evidence_a = replay.workspace_a / "candidate-a-evidence.json"
    evidence_b = tmp_path / "candidate-b-evidence.json"
    evidence_a.write_bytes(b'{"candidate":"A"}\n')
    evidence_b.write_bytes(b'{"candidate":"B"}\n')
    packages = [
        {"name": f"package-{index:02d}", "version": "1.0"}
        for index in range(36)
    ] + [{"name": "pip", "version": "24.0"}]
    common = {
        "implementation_commit": replay.implementation_commit,
        "v01_tag_commit": replay.v01_tag_commit,
        "run_id": run_id,
        "project_wheel": replay.project_wheel,
        "wheelhouse_root": replay.wheelhouse_root,
        "payload": {"installed_packages": packages},
    }
    candidate_a = SimpleNamespace(
        evidence_path=evidence_a,
        candidate="A",
        artifact=tmp_path / "artifact-a",
        **common,
    )
    candidate_b = SimpleNamespace(
        evidence_path=evidence_b,
        candidate="B",
        artifact=tmp_path / "artifact-b",
        **common,
    )
    candidates = iter((candidate_a, candidate_b))
    monkeypatch.setattr(
        replay_module,
        "_load_candidate_evidence",
        lambda _path: next(candidates),
    )
    monkeypatch.setattr(
        replay_module,
        "_verify_git_approval_closure",
        lambda *_args, **_kwargs: (
            b'{"gate":"E"}\n',
            _candidate_review_text().encode(),
        ),
    )
    monkeypatch.setattr(
        replay_module,
        "_verify_candidate_trust_evidence",
        lambda **_kwargs: {"expected_run_id": run_id},
    )
    result = CandidateAResult(
        evidence_path=evidence_b,
        artifact=candidate_b.artifact,
        run_id=run_id,
    )
    stage_hook = {"value": None}

    def execute(*_args, **kwargs):
        hook = stage_hook["value"]
        if hook is not None:
            hook(kwargs["stage"])
        return (
            result
            if kwargs["stage"] == "candidate_a_audited"
            else None
        )

    monkeypatch.setattr(replay_module, "_execute_candidate_a_stage", execute)
    monkeypatch.setattr(
        replay_module,
        "_compare_artifacts",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        replay_module,
        "_verify_input_roots_independent",
        lambda *_args: None,
    )
    arguments = {
        "trust_anchor_commit": "a" * 40,
        "approval_commit": "b" * 40,
        "trust_path": Path("release/v0.2-gate-e/trust_manifest.json"),
    }
    return replay, arguments, stage_hook


def _workspace_observation(root: Path):
    observed = []
    for path in (root, *sorted(root.rglob("*"))):
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            content_identity = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(metadata.st_mode):
            content_identity = os.readlink(path)
        else:
            content_identity = None
        observed.append(
            (
                path.relative_to(root).as_posix()
                if path != root
                else ".",
                metadata.st_mode,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                content_identity,
            )
        )
    return tuple(observed)


def _materialized_project_tree(tmp_path):
    release_root = PROJECT_ROOT / "release/v0.1-research"
    source_config = PROJECT_ROOT / "configs/releases/v0.2_gate_e.json"
    project_root = tmp_path / "project"
    stage_gate_e_input_root(release_root, project_root)
    config_path = project_root / "configs/releases/v0.2_gate_e.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(source_config.read_bytes())
    config_path.chmod(0o400)
    output_root = project_root / "outputs"
    output_root.mkdir()
    layout = GateEEnvironmentLayout(
        root=tmp_path,
        home=tmp_path / "home",
        xdg_cache=tmp_path / "xdg-cache",
        uv_cache=tmp_path / "uv-cache",
        venv=tmp_path / "venv",
        python=tmp_path / "venv/bin/python",
        project_root=project_root,
        input_root=project_root,
        output_root=output_root,
        config_path=config_path,
        repository_root=PROJECT_ROOT,
        base_python=FIXED_PYTHON,
    )
    replay = replace(
        _replay(tmp_path),
        release_root=release_root,
        config_path=source_config,
    )
    return replay, layout, load_gate_e_config(source_config)


def test_replay_b_uses_dedicated_trust_root_without_touching_workspace_a(
    tmp_path,
    monkeypatch,
):
    replay, arguments, _stage_hook = _mock_successful_replay_b(
        tmp_path,
        monkeypatch,
    )
    old_timestamp = 1_700_000_000_000_000_000
    os.utime(
        replay.workspace_a,
        ns=(old_timestamp, old_timestamp),
    )
    before = _workspace_observation(replay.workspace_a)
    original_create_root = replay_module._create_anchored_temporary_root
    temporary_directories = []

    def recording_create_root(replay_contract):
        root = original_create_root(replay_contract)
        temporary_directories.append(root.path)
        return root

    monkeypatch.setattr(
        replay_module,
        "_create_anchored_temporary_root",
        recording_create_root,
    )

    replayed = replay_environment_b(replay, **arguments)

    assert replayed.run_id == "c" * 64
    assert _workspace_observation(replay.workspace_a) == before
    assert len(temporary_directories) == 1
    trust_root = temporary_directories[0]
    assert trust_root != replay.workspace_a
    assert replay.workspace_a not in trust_root.parents
    assert replay.workspace_b not in trust_root.parents
    assert not trust_root.exists()


def test_replay_b_rejects_overlapping_workspace_before_creating_temp_root(
    tmp_path,
    monkeypatch,
):
    replay, arguments, _stage_hook = _mock_successful_replay_b(
        tmp_path,
        monkeypatch,
    )
    replay = replace(
        replay,
        workspace_b=replay.workspace_a / "candidate-b",
    )
    before = _workspace_observation(replay.workspace_a)
    create_root_called = False

    def unexpected_create_root(*_args, **_kwargs):
        nonlocal create_root_called
        create_root_called = True
        raise AssertionError("temp root must not run for overlapping roots")

    monkeypatch.setattr(
        replay_module,
        "_create_anchored_temporary_root",
        unexpected_create_root,
    )

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(replay, **arguments)

    assert captured.value.code == "candidate_workspace_overlap"
    assert create_root_called is False
    assert _workspace_observation(replay.workspace_a) == before


@pytest.mark.parametrize(
    "mutation",
    (
        "content",
        "empty_directory",
        "mtime",
        "cache_file",
        "lock_file",
        "cleanup_delete",
    ),
)
def test_replay_b_rejects_any_candidate_a_workspace_change(
    tmp_path,
    monkeypatch,
    mutation,
):
    replay, arguments, stage_hook = _mock_successful_replay_b(
        tmp_path,
        monkeypatch,
    )
    marker = replay.workspace_a / "cache.lock"
    marker.write_bytes(b"original")
    uv_cache = replay.workspace_a / "uv-cache"
    uv_cache.mkdir()
    old_timestamp = 1_700_000_000_000_000_000
    os.utime(marker, ns=(old_timestamp, old_timestamp))
    changed = False

    def mutate_once(_stage):
        nonlocal changed
        if not changed:
            changed = True
            if mutation == "content":
                marker.write_bytes(b"changed")
            elif mutation == "empty_directory":
                (replay.workspace_a / "unexpected-empty").mkdir()
            elif mutation == "mtime":
                os.utime(
                    marker,
                    ns=(old_timestamp + 1, old_timestamp + 1),
                )
            elif mutation == "cache_file":
                (uv_cache / "runtime.cache").write_bytes(b"x")
            elif mutation == "lock_file":
                (replay.workspace_a / "runtime.lock").write_bytes(b"")
            elif mutation == "cleanup_delete":
                marker.unlink()

    stage_hook["value"] = mutate_once

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(replay, **arguments)

    assert captured.value.code == "candidate_a_workspace_changed"


def test_replay_b_temporary_cleanup_failure_is_a_gate_failure(
    tmp_path,
    monkeypatch,
):
    replay, arguments, _stage_hook = _mock_successful_replay_b(
        tmp_path,
        monkeypatch,
    )
    original_unlink = replay_module.os.unlink

    def fail_anchored_unlink(path, *args, **kwargs):
        if ".gate-e-anchored-" in os.fspath(path):
            raise PermissionError("injected cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(replay_module.os, "unlink", fail_anchored_unlink)

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(replay, **arguments)

    assert captured.value.code == "trust_temporary_cleanup_failed"


def test_replay_b_reports_cleanup_failure_with_workspace_change(
    tmp_path,
    monkeypatch,
):
    replay, arguments, stage_hook = _mock_successful_replay_b(
        tmp_path,
        monkeypatch,
    )
    marker = replay.workspace_a / "audited.txt"
    marker.write_bytes(b"before")
    stage_hook["value"] = lambda _stage: marker.write_bytes(b"after")
    original_unlink = replay_module.os.unlink

    def fail_anchored_unlink(path, *args, **kwargs):
        if ".gate-e-anchored-" in os.fspath(path):
            raise PermissionError("injected cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(replay_module.os, "unlink", fail_anchored_unlink)

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(replay, **arguments)

    assert (
        captured.value.code
        == "candidate_a_workspace_changed_and_cleanup_failed"
    )
    assert captured.value.cause_code == "trust_temporary_cleanup_failed"


def test_anchored_temp_parent_swap_cannot_redirect_writes_into_a(
    tmp_path,
):
    replay = _replay(tmp_path)
    replay.workspace_a.mkdir()
    replay.wheelhouse_root.mkdir(parents=True)
    controller_parent = tmp_path / "controller"
    controller_parent.mkdir()
    replay = replace(
        replay,
        workspace_b=controller_parent / "environment-b",
    )
    before = _workspace_observation(replay.workspace_a)
    root = replay_module._create_anchored_temporary_root(replay)
    moved_parent = tmp_path / "controller-moved"
    controller_parent.rename(moved_parent)
    controller_parent.symlink_to(
        replay.workspace_a,
        target_is_directory=True,
    )
    item = None
    try:
        item = replay_module._write_anchored_temporary(
            root,
            prefix=".gate-e-anchored-",
            suffix=".json",
            content=b"{}\n",
        )
        replay_module._cleanup_anchored_temporary_root(
            root,
            (item,),
        )
        assert not (moved_parent / root.name).exists()
        assert _workspace_observation(replay.workspace_a) == before
    finally:
        if controller_parent.is_symlink():
            controller_parent.unlink()
        if moved_parent.exists():
            moved_parent.rename(controller_parent)


def test_anchored_temp_cleanup_rejects_replaced_root_without_touching_a(
    tmp_path,
):
    replay = _replay(tmp_path)
    replay.workspace_a.mkdir()
    replay.wheelhouse_root.mkdir(parents=True)
    before = _workspace_observation(replay.workspace_a)
    root = replay_module._create_anchored_temporary_root(replay)
    item = replay_module._write_anchored_temporary(
        root,
        prefix=".gate-e-anchored-",
        suffix=".json",
        content=b"{}\n",
    )
    moved_root = root.path.with_name(f"{root.name}.moved")
    root.path.rename(moved_root)
    root.path.symlink_to(replay.workspace_a, target_is_directory=True)
    try:
        with pytest.raises(GateEReplayError) as captured:
            replay_module._cleanup_anchored_temporary_root(root, (item,))

        assert captured.value.code == "trust_temporary_cleanup_failed"
        assert _workspace_observation(replay.workspace_a) == before
    finally:
        root.path.unlink()
        for entry in moved_root.iterdir():
            entry.unlink()
        moved_root.rmdir()


def test_anchored_temp_cleanup_rejects_replaced_entry_without_unlinking_it(
    tmp_path,
):
    replay = _replay(tmp_path)
    replay.workspace_a.mkdir()
    replay.wheelhouse_root.mkdir(parents=True)
    marker = replay.workspace_a / "protected.txt"
    marker.write_bytes(b"protected")
    root = replay_module._create_anchored_temporary_root(replay)
    item = replay_module._write_anchored_temporary(
        root,
        prefix=".gate-e-anchored-",
        suffix=".json",
        content=b"{}\n",
    )
    original_entry = item.path.with_name(f"{item.name}.original")
    item.path.rename(original_entry)
    os.link(marker, item.path)
    after_attack = _workspace_observation(replay.workspace_a)
    try:
        with pytest.raises(GateEReplayError) as captured:
            replay_module._cleanup_anchored_temporary_root(root, (item,))

        assert captured.value.code == "trust_temporary_cleanup_failed"
        assert item.path.exists()
        assert _workspace_observation(replay.workspace_a) == after_attack
    finally:
        item.path.unlink()
        original_entry.unlink()
        root.path.rmdir()


def test_candidate_evidence_temporary_cleanup_failure_is_explicit(
    tmp_path,
    monkeypatch,
):
    original_unlink = replay_module.os.unlink

    def fail_publish(*_args, **_kwargs):
        raise PermissionError("injected publish failure")

    def fail_temporary_unlink(path, *args, **kwargs):
        if ".candidate-a-evidence.json.tmp-" in os.fspath(path):
            raise PermissionError("injected cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(replay_module.os, "link", fail_publish)
    monkeypatch.setattr(
        replay_module.os,
        "unlink",
        fail_temporary_unlink,
    )

    with pytest.raises(GateEReplayError) as captured:
        replay_module._write_candidate_evidence(
            tmp_path / "candidate-a-evidence.json",
            {"candidate": "A"},
        )

    assert captured.value.code == "candidate_evidence_cleanup_failed"


def test_project_tree_rejects_an_extra_empty_directory(
    tmp_path,
):
    replay, layout, config = _materialized_project_tree(tmp_path)
    replay_module._verify_staged_inputs(
        replay,
        layout,
        config,
        run_id=None,
    )
    (layout.project_root / "unexpected-empty").mkdir()

    with pytest.raises(GateEReplayError) as captured:
        replay_module._verify_staged_inputs(
            replay,
            layout,
            config,
            run_id=None,
        )

    assert captured.value.code == "environment_project_tree_mismatch"


def test_staged_input_state_allows_idempotent_mode_check_but_rejects_mtime(
    tmp_path,
):
    replay, layout, config = _materialized_project_tree(tmp_path)
    baseline = replay_module._snapshot_staged_input_state(
        config,
        layout.project_root,
    )
    relative = sorted(config.payload["input_files"])[0]
    target = layout.project_root / relative

    target.chmod(stat.S_IMODE(target.lstat().st_mode))
    assert (
        replay_module._snapshot_staged_input_state(
            config,
            layout.project_root,
        )
        == baseline
    )

    changed_mtime = target.lstat().st_mtime_ns + 1
    os.utime(target, ns=(changed_mtime, changed_mtime))

    with pytest.raises(GateEReplayError) as captured:
        replay_module._verify_staged_inputs(
            replay,
            layout,
            config,
            run_id=None,
            expected_input_state=baseline,
        )

    assert captured.value.code == "environment_input_state_changed"


def test_post_run_project_verification_calls_input_root_verifier(
    tmp_path,
    monkeypatch,
):
    replay, layout, config = _materialized_project_tree(tmp_path)
    run_id = "c" * 64
    artifact = layout.output_root / "portfolios" / run_id
    artifact.mkdir(parents=True)
    for name in replay_module.PORTFOLIO_ARTIFACT_FILES:
        (artifact / name).write_bytes(b"")
    (layout.output_root / "portfolios" / f".{run_id}.lock").write_bytes(
        b""
    )
    calls = []

    def verify_inputs(release_root, input_root, **kwargs):
        calls.append((release_root, input_root, kwargs))
        return SimpleNamespace(file_count=25)

    monkeypatch.setattr(
        replay_module,
        "verify_post_run_input_root",
        verify_inputs,
        raising=False,
    )

    replay_module._verify_staged_inputs(
        replay,
        layout,
        config,
        run_id=run_id,
    )

    assert len(calls) == 1
    assert calls[0][0] == replay.release_root
    assert calls[0][1] == layout.project_root
    assert calls[0][2] == {"expected_run_id": run_id}


def test_run_and_reverse_stages_verify_the_post_run_project_tree(
    tmp_path,
    monkeypatch,
):
    replay = _replay(tmp_path)
    config = load_gate_e_config(replay.config_path)
    layout = GateEEnvironmentLayout(
        root=tmp_path / "workspace",
        home=tmp_path / "workspace/home",
        xdg_cache=tmp_path / "workspace/xdg-cache",
        uv_cache=tmp_path / "workspace/uv-cache",
        venv=tmp_path / "workspace/venv",
        python=tmp_path / "workspace/venv/bin/python",
        project_root=tmp_path / "workspace/project",
        input_root=tmp_path / "workspace/project",
        output_root=tmp_path / "workspace/project/outputs",
        config_path=(
            tmp_path
            / "workspace/project/configs/releases/v0.2_gate_e.json"
        ),
        repository_root=PROJECT_ROOT,
        base_python=FIXED_PYTHON,
    )
    installed = InstalledEnvironmentEvidence(
        project_version="0.2.0",
        aquant_file=tmp_path / "site-packages/aquant/__init__.py",
        sys_path=("site-packages",),
        packages=(("a-share-quant", "0.2.0"),),
        portfolio_cli=tmp_path / "venv/bin/aquant-portfolio",
        gate_e_cli=tmp_path / "venv/bin/aquant-gate-e",
    )
    run_id = "c" * 64
    artifact = layout.output_root / "portfolios" / run_id
    state = {
        "candidate": "A",
        "hash_seed": "101",
        "workspace": layout.root,
        "config": config,
        "layout": layout,
        "installed": installed,
        "input_state": tuple((str(index),) for index in range(25)),
    }
    verifications = []
    monkeypatch.setattr(
        replay_module,
        "_run_portfolio_candidate",
        lambda *_args, **_kwargs: (run_id, artifact, ("run",)),
    )
    monkeypatch.setattr(
        replay_module,
        "_reverse_candidate",
        lambda *_args, **_kwargs: ("verify",),
    )
    monkeypatch.setattr(
        replay_module,
        "_verify_staged_inputs",
        lambda *_args, **kwargs: verifications.append(kwargs["run_id"]),
    )

    replay_module._execute_candidate_a_stage(
        replay,
        stage="candidate_a_run",
        state=state,
    )
    replay_module._execute_candidate_a_stage(
        replay,
        stage="candidate_a_reversed",
        state=state,
    )

    assert verifications == [run_id, run_id]


def test_candidate_b_environment_stage_denies_writes_to_candidate_a(
    tmp_path,
    monkeypatch,
):
    replay = _replay(tmp_path)
    replay.workspace_a.mkdir()
    wheel = WheelEvidence(
        path=replay.project_wheel,
        size=1,
        sha256="c" * 64,
        distribution_version="0.2.0",
        portfolio_cli_present=True,
        entry_point="aquant-portfolio = aquant.portfolio_cli:main",
    )
    layout = SimpleNamespace(root=replay.workspace_b)
    installed = SimpleNamespace(project_version="0.2.0")
    calls = []

    def make_layout(*_args, **kwargs):
        calls.append(("layout", kwargs["read_only_paths"]))
        return layout

    def install_environment(*_args, **kwargs):
        calls.append(("install", kwargs["read_only_paths"]))
        return installed

    monkeypatch.setattr(
        replay_module,
        "make_environment_layout",
        make_layout,
    )
    monkeypatch.setattr(
        replay_module,
        "install_gate_e_environment",
        install_environment,
    )
    state = {
        "candidate": "B",
        "hash_seed": "909",
        "workspace": replay.workspace_b,
        "project_wheel_evidence": wheel,
    }

    replay_module._execute_candidate_a_stage(
        replay,
        stage="environment_a_installed",
        state=state,
    )

    assert calls == [
        ("layout", (replay.workspace_a,)),
        ("install", (replay.workspace_a,)),
    ]
    assert state["layout"] is layout
    assert state["installed"] is installed


def test_wheelhouse_manifest_exposes_exact_requirement_mapping(tmp_path):
    manifest = tmp_path / "wheelhouse_manifest.json"
    payload = {
        "schema_version": "1.0",
        "wheels": [
            {
                "filename": "example-1.2.3-py3-none-any.whl",
                "normalized_name": "example",
                "sha256": "1" * 64,
                "size": 123,
                "version": "1.2.3",
            }
        ],
    }
    manifest.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    assert wheelhouse_requirements_from_manifest(manifest) == {
        "example": "1.2.3"
    }


def test_runtime_executables_are_discovered_as_real_regular_files():
    executables = (
        canonical_python_executable(),
        canonical_uv_executable(),
    )

    for executable in executables:
        assert executable.is_absolute()
        assert executable == executable.resolve(strict=True)
        assert executable.is_file()
        assert not executable.is_symlink()


def test_environment_b_cannot_run_before_anchor_pass(tmp_path):
    replay = _replay(tmp_path)

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(
            replay,
            trust_anchor_commit=None,
            approval_commit=None,
        )

    assert captured.value.code == "trust_anchor_not_approved"
    assert not replay.workspace_b.exists()

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(
            replay,
            trust_anchor_commit="a" * 40,
            approval_commit="a" * 40,
            trust_path=Path("release/v0.2-gate-e/trust_manifest.json"),
        )

    assert captured.value.code == "trust_anchor_not_approved"
    assert not replay.workspace_b.exists()


@pytest.mark.parametrize(
    ("case", "expected_error"),
    (
        ("matching", None),
        ("version_mismatch", "replay_installed_packages_mismatch"),
        ("missing", "replay_installed_packages_mismatch"),
        ("extra", "replay_installed_packages_mismatch"),
        ("same_short", "replay_installed_packages_mismatch"),
        ("reordered", "replay_installed_packages_mismatch"),
    ),
)
def test_environment_b_requires_exact_installed_package_inventory(
    tmp_path,
    monkeypatch,
    case,
    expected_error,
):
    replay = _replay(tmp_path)
    replay.workspace_a.mkdir()
    run_id = "c" * 64
    evidence_a = replay.workspace_a / "candidate-a-evidence.json"
    evidence_b = tmp_path / "candidate-b-evidence.json"
    evidence_a.write_bytes(b'{"candidate":"A"}\n')
    evidence_b.write_bytes(b'{"candidate":"B"}\n')
    common = {
        "implementation_commit": replay.implementation_commit,
        "v01_tag_commit": replay.v01_tag_commit,
        "run_id": run_id,
        "project_wheel": replay.project_wheel,
        "wheelhouse_root": replay.wheelhouse_root,
    }
    packages_a = [
        {"name": f"package-{index:02d}", "version": "1.0"}
        for index in range(36)
    ] + [{"name": "pip", "version": "24.0"}]
    packages_b = [dict(item) for item in packages_a]
    if case == "version_mismatch":
        packages_b[-1]["version"] = "25.0"
    elif case == "missing":
        packages_b.pop()
    elif case == "extra":
        packages_b.append({"name": "extra", "version": "1.0"})
    elif case == "same_short":
        packages_a.pop()
        packages_b.pop()
    elif case == "reordered":
        packages_b.reverse()
    candidate_a = SimpleNamespace(
        evidence_path=evidence_a,
        candidate="A",
        artifact=tmp_path / "artifact-a",
        payload={"installed_packages": packages_a},
        **common,
    )
    candidate_b = SimpleNamespace(
        evidence_path=evidence_b,
        candidate="B",
        artifact=tmp_path / "artifact-b",
        payload={"installed_packages": packages_b},
        **common,
    )
    candidates = iter((candidate_a, candidate_b))
    monkeypatch.setattr(
        replay_module,
        "_load_candidate_evidence",
        lambda _path: next(candidates),
    )
    monkeypatch.setattr(
        replay_module,
        "_verify_git_approval_closure",
        lambda *_args, **_kwargs: (
            b'{"gate":"E"}\n',
            _candidate_review_text().encode(),
        ),
    )
    monkeypatch.setattr(
        replay_module,
        "_verify_candidate_trust_evidence",
        lambda **_kwargs: {"expected_run_id": run_id},
    )
    result = CandidateAResult(
        evidence_path=evidence_b,
        artifact=candidate_b.artifact,
        run_id=run_id,
    )
    monkeypatch.setattr(
        replay_module,
        "_execute_candidate_a_stage",
        lambda *_args, **kwargs: (
            result
            if kwargs["stage"] == "candidate_a_audited"
            else None
        ),
    )
    monkeypatch.setattr(
        replay_module,
        "_compare_artifacts",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        replay_module,
        "_verify_input_roots_independent",
        lambda *_args: None,
    )

    arguments = {
        "trust_anchor_commit": "a" * 40,
        "approval_commit": "b" * 40,
        "trust_path": Path(
            "release/v0.2-gate-e/trust_manifest.json"
        ),
    }
    if expected_error is None:
        replayed = replay_environment_b(replay, **arguments)
        assert replayed.run_id == run_id
    else:
        with pytest.raises(GateEReplayError) as captured:
            replay_environment_b(replay, **arguments)

        assert captured.value.code == expected_error

@pytest.mark.parametrize(
    "case",
    (
        "candidate_missing_from_anchor",
        "candidate_changed_after_anchor",
        "approval_missing",
        "approval_wrong_binding",
    ),
)
def test_git_review_closure_fails_before_environment_b(
    tmp_path,
    monkeypatch,
    case,
):
    replay, anchor, approval_commit, candidate = _approval_repository(
        tmp_path,
        candidate_at_anchor=case != "candidate_missing_from_anchor",
        mutate_candidate_after_anchor=(
            case == "candidate_changed_after_anchor"
        ),
        wrong_approval_binding=case == "approval_wrong_binding",
        approval_review=case != "approval_missing",
    )
    stages: list[str] = []
    monkeypatch.setattr(
        replay_module,
        "_load_candidate_evidence",
        lambda _path: candidate,
    )
    monkeypatch.setattr(
        replay_module,
        "_execute_candidate_a_stage",
        lambda *_args, **kwargs: stages.append(kwargs["stage"]),
    )
    monkeypatch.setattr(
        replay_module,
        "verify_candidate_trust",
        lambda **_kwargs: pytest.fail(
            "trust verification must not run before Git approval closes"
        ),
    )

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(
            replay,
            trust_anchor_commit=anchor,
            approval_commit=approval_commit,
            trust_path=Path(
                "release/v0.2-gate-e/trust_manifest.json"
            ),
        )

    assert captured.value.code == "trust_anchor_not_approved"
    assert stages == []
    assert not replay.workspace_b.exists()


@pytest.mark.parametrize(
    "relative",
    (CANDIDATE_REVIEW_PATH, TRUST_APPROVAL_PATH, Path("release/v0.2-gate-e/trust_manifest.json")),
)
def test_current_review_closure_file_cannot_be_deleted(
    tmp_path,
    monkeypatch,
    relative,
):
    replay, anchor, approval_commit, candidate = _approval_repository(
        tmp_path
    )
    (replay.repository_root / relative).unlink()
    monkeypatch.setattr(
        replay_module,
        "_load_candidate_evidence",
        lambda _path: candidate,
    )
    monkeypatch.setattr(
        replay_module,
        "_execute_candidate_a_stage",
        lambda *_args, **_kwargs: pytest.fail("B stage must not run"),
    )

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(
            replay,
            trust_anchor_commit=anchor,
            approval_commit=approval_commit,
            trust_path=Path("release/v0.2-gate-e/trust_manifest.json"),
        )

    assert captured.value.code == "trust_anchor_not_approved"
    assert not replay.workspace_b.exists()


@pytest.mark.parametrize(
    "relative",
    (
        Path("release/v0.2-gate-e/trust_manifest.json"),
        CANDIDATE_REVIEW_PATH,
        TRUST_APPROVAL_PATH,
    ),
)
def test_head_review_blob_cannot_be_changed_then_hidden_by_worktree(
    tmp_path,
    monkeypatch,
    relative,
):
    replay, anchor, approval_commit, candidate = _approval_repository(
        tmp_path
    )
    protected = replay.repository_root / relative
    approved_bytes = protected.read_bytes()
    protected.write_bytes(approved_bytes + b"\nHEAD changed\n")
    _git(replay.repository_root, "add", relative.as_posix())
    _git(replay.repository_root, "commit", "-q", "-m", "tamper HEAD")
    protected.write_bytes(approved_bytes)
    stages: list[str] = []
    monkeypatch.setattr(
        replay_module,
        "_load_candidate_evidence",
        lambda _path: candidate,
    )
    monkeypatch.setattr(
        replay_module,
        "_execute_candidate_a_stage",
        lambda *_args, **kwargs: stages.append(kwargs["stage"]),
    )
    monkeypatch.setattr(
        replay_module,
        "_verify_candidate_trust_evidence",
        lambda **_kwargs: pytest.fail(
            "trust verification must not run after a HEAD blob change"
        ),
    )

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(
            replay,
            trust_anchor_commit=anchor,
            approval_commit=approval_commit,
            trust_path=Path(
                "release/v0.2-gate-e/trust_manifest.json"
            ),
        )

    assert captured.value.code == "trust_anchor_not_approved"
    assert stages == []
    assert not replay.workspace_b.exists()


@pytest.mark.parametrize("commit_kind", ("anchor", "approval"))
def test_review_commit_ids_must_name_commit_objects(
    tmp_path,
    monkeypatch,
    commit_kind,
):
    replay, anchor, approval_commit, candidate = _approval_repository(
        tmp_path
    )
    target = anchor if commit_kind == "anchor" else approval_commit
    tag_name = f"reviewed-{commit_kind}"
    _git(
        replay.repository_root,
        "tag",
        "-a",
        tag_name,
        target,
        "-m",
        tag_name,
    )
    tag_object = _git(replay.repository_root, "rev-parse", tag_name)
    monkeypatch.setattr(
        replay_module,
        "_load_candidate_evidence",
        lambda _path: candidate,
    )
    monkeypatch.setattr(
        replay_module,
        "_execute_candidate_a_stage",
        lambda *_args, **_kwargs: pytest.fail("B stage must not run"),
    )
    monkeypatch.setattr(
        replay_module,
        "_verify_candidate_trust_evidence",
        lambda **_kwargs: pytest.fail(
            "trust verification must not accept an annotated tag object"
        ),
    )

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(
            replay,
            trust_anchor_commit=(
                tag_object if commit_kind == "anchor" else anchor
            ),
            approval_commit=(
                tag_object
                if commit_kind == "approval"
                else approval_commit
            ),
            trust_path=Path(
                "release/v0.2-gate-e/trust_manifest.json"
            ),
        )

    assert captured.value.code == "trust_anchor_not_approved"
    assert not replay.workspace_b.exists()


def test_approval_commit_may_equal_head_when_review_blobs_are_unchanged(
    tmp_path,
):
    replay, anchor, approval_commit, candidate = _approval_repository(
        tmp_path
    )

    assert approval_commit == _git(replay.repository_root, "rev-parse", "HEAD")
    trust_blob, candidate_review_blob = (
        replay_module._verify_git_approval_closure(
            replay,
            candidate,
            trust_anchor_commit=anchor,
            approval_commit=approval_commit,
        )
    )

    assert trust_blob == (
        replay.repository_root
        / "release/v0.2-gate-e/trust_manifest.json"
    ).read_bytes()
    assert candidate_review_blob == (
        replay.repository_root / CANDIDATE_REVIEW_PATH
    ).read_bytes()


def test_git_reader_ignores_replace_objects(tmp_path):
    replay, anchor, _approval_commit, _candidate = _approval_repository(
        tmp_path
    )
    trust_path = "release/v0.2-gate-e/trust_manifest.json"
    original = replay_module._git(
        replay.repository_root,
        "show",
        f"{anchor}:{trust_path}",
        code="test_git_failed",
        binary=True,
    )
    trust = replay.repository_root / trust_path
    trust.write_bytes(b'{"gate":"FORGED"}\n')
    _git(replay.repository_root, "add", trust_path)
    _git(replay.repository_root, "commit", "-q", "-m", "forged tree")
    forged = _git(replay.repository_root, "rev-parse", "HEAD")
    _git(replay.repository_root, "replace", anchor, forged)

    resolved = replay_module._git(
        replay.repository_root,
        "show",
        f"{anchor}:{trust_path}",
        code="test_git_failed",
        binary=True,
    )

    assert original == b'{"gate":"E"}\n'
    assert resolved == original


@pytest.mark.parametrize("target_name", ("anchor", "approval", "head"))
def test_git_approval_closure_rejects_replace_refs(
    tmp_path,
    target_name,
):
    replay, anchor, approval_commit, candidate = _approval_repository(
        tmp_path
    )
    _git(
        replay.repository_root,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "current head",
    )
    head = _git(replay.repository_root, "rev-parse", "HEAD")
    targets = {
        "anchor": anchor,
        "approval": approval_commit,
        "head": head,
    }
    target = targets[target_name]
    replacement = _same_tree_commit(replay.repository_root, target)
    _git(replay.repository_root, "replace", target, replacement)

    with pytest.raises(GateEReplayError) as captured:
        replay_module._verify_git_approval_closure(
            replay,
            candidate,
            trust_anchor_commit=anchor,
            approval_commit=approval_commit,
        )

    assert captured.value.code == "trust_anchor_not_approved"
    assert not replay.workspace_b.exists()


def test_git_approval_closure_rejects_grafts_file(tmp_path):
    replay, anchor, approval_commit, candidate = _approval_repository(
        tmp_path
    )
    common_dir = Path(
        _git(replay.repository_root, "rev-parse", "--git-common-dir")
    )
    if not common_dir.is_absolute():
        common_dir = replay.repository_root / common_dir
    grafts = common_dir / "info/grafts"
    grafts.parent.mkdir(parents=True, exist_ok=True)
    grafts.write_bytes(b"")

    with pytest.raises(GateEReplayError) as captured:
        replay_module._verify_git_approval_closure(
            replay,
            candidate,
            trust_anchor_commit=anchor,
            approval_commit=approval_commit,
        )

    assert captured.value.code == "trust_anchor_not_approved"
    assert not replay.workspace_b.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("project", "other-project"),
        ("version", "v0.1"),
        ("gate", "D"),
        ("review_kind", "candidate_a"),
        ("decision", "FAIL"),
        ("P0", "1"),
        ("P1", "1"),
        ("P2", "1"),
        ("trust_anchor_commit", "f" * 40),
        ("trust_path", "release/other.json"),
        ("trust_sha256", "f" * 64),
        ("candidate_review_path", "outputs/other.md"),
        ("candidate_review_sha256", "f" * 64),
        ("implementation_commit", "f" * 40),
        ("expected_run_id", "f" * 64),
    ),
)
def test_each_trust_anchor_review_binding_is_enforced_before_b(
    tmp_path,
    monkeypatch,
    field,
    value,
):
    replay, anchor, approval_commit, candidate = _approval_repository(
        tmp_path,
        approval_mutation=(field, value),
    )
    monkeypatch.setattr(
        replay_module,
        "_load_candidate_evidence",
        lambda _path: candidate,
    )
    monkeypatch.setattr(
        replay_module,
        "_execute_candidate_a_stage",
        lambda *_args, **_kwargs: pytest.fail("B stage must not run"),
    )

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(
            replay,
            trust_anchor_commit=anchor,
            approval_commit=approval_commit,
            trust_path=Path("release/v0.2-gate-e/trust_manifest.json"),
        )

    assert captured.value.code == "trust_anchor_not_approved"
    assert not replay.workspace_b.exists()

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(
            replay,
            trust_anchor_commit="a" * 40,
            approval_commit="b" * 40,
            trust_path=None,
        )

    assert captured.value.code == "trust_anchor_not_approved"
    assert not replay.workspace_b.exists()


def test_candidate_a_emits_fixed_progress_order(tmp_path, monkeypatch):
    replay = _replay(tmp_path)
    events: list[str] = []
    expected = CandidateAResult(
        evidence_path=tmp_path / "candidate-a-evidence.json",
        artifact=tmp_path / ("0" * 64),
        run_id="0" * 64,
    )

    def execute(_replay, *, stage, state):
        del _replay, state
        return expected if stage == "candidate_a_audited" else None

    monkeypatch.setattr(
        replay_module,
        "_execute_candidate_a_stage",
        execute,
    )

    result = run_candidate_a(
        replay,
        progress=lambda event: events.append(event.stage),
    )

    assert result.evidence_path == expected.evidence_path
    assert result.artifact == expected.artifact
    assert result.run_id == expected.run_id
    assert events == [
        "trust_roots_verified",
        "wheel_verified",
        "environment_a_installed",
        "inputs_a_verified",
        "candidate_a_run",
        "candidate_a_reversed",
        "candidate_a_audited",
    ]
    assert [event.completed for event in result.progress] == list(
        range(1, 8)
    )
    assert all(event.total == 7 for event in result.progress)


_VALID_COMMANDS = {
    "candidate-a": (
        "--config",
        "config.json",
        "--wheel",
        "project.whl",
        "--wheelhouse",
        "wheelhouse",
        "--workspace",
        "workspace-a",
    ),
    "audit-candidate": (
        "--evidence",
        "candidate-a-evidence.json",
        "--artifact",
        "artifact",
    ),
    "build-trust": (
        "--evidence",
        "candidate-a-evidence.json",
        "--approved-review",
        "review.md",
    ),
    "verify-trust": (
        "--trust",
        "trust.json",
        "--evidence",
        "candidate-a-evidence.json",
        "--artifact",
        "artifact",
        "--approved-review",
        str(CANDIDATE_REVIEW_PATH),
    ),
    "replay-b": (
        "--trust-anchor-commit",
        "a" * 40,
        "--approval-commit",
        "b" * 40,
        "--trust-path",
        "release/v0.2-gate-e/trust_manifest.json",
        "--wheel",
        "project.whl",
        "--wheelhouse",
        "wheelhouse",
        "--workspace-a",
        "workspace-a",
        "--workspace-b",
        "workspace-b",
    ),
}


def test_cli_exposes_only_the_five_reviewed_subcommands():
    assert cli_module.GATE_E_COMMANDS == tuple(_VALID_COMMANDS)

    for command, arguments in _VALID_COMMANDS.items():
        parsed = cli_module.parse_gate_e_arguments((command, *arguments))
        assert parsed.command == command

    with pytest.raises(GateEReplayError) as captured:
        cli_module.parse_gate_e_arguments(("candidate-b",))

    assert captured.value.code == "invalid_arguments"


@pytest.mark.parametrize(
    ("command", "arguments", "missing_index"),
    tuple(
        (command, arguments, index)
        for command, arguments in _VALID_COMMANDS.items()
        for index in range(0, len(arguments), 2)
    ),
)
def test_every_cli_argument_is_required(
    command,
    arguments,
    missing_index,
):
    incomplete = (
        *arguments[:missing_index],
        *arguments[missing_index + 2 :],
    )

    with pytest.raises(GateEReplayError) as captured:
        cli_module.parse_gate_e_arguments((command, *incomplete))

    assert captured.value.code == "invalid_arguments"


def test_cli_error_never_echoes_raw_paths(tmp_path, capsys):
    secret = tmp_path / "private-user-path/config.json"

    assert (
        cli_module.main(("candidate-a", "--config", str(secret))) == 1
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload == {
        "error_code": "invalid_arguments",
        "error_type": "GateEReplayError",
        "status": "error",
    }
    assert str(secret) not in captured.err
    assert captured.out == ""


def test_legacy_seven_line_review_is_rejected(tmp_path):
    review = tmp_path / "review.md"
    review.write_text(
        "project = a-share-quant\n"
        "version = v0.2\n"
        "gate = E\n"
        "decision = PASS\n"
        "P0 = 0\n"
        "P1 = 0\n"
        "P2 = 0\n",
        encoding="utf-8",
    )

    with pytest.raises(GateEReplayError) as captured:
        verify_approved_review(review)

    assert captured.value.code == "candidate_review_not_approved"


def test_review_must_bind_candidate_a_identity(tmp_path):
    review = tmp_path / "review.md"

    review.write_text(_candidate_review_text(), encoding="utf-8")

    assert verify_approved_review(review) == review


@pytest.mark.parametrize(
    "extra",
    (
        "unknown_binding = value\n",
        "decision = PASS\n",
    ),
)
def test_candidate_review_rejects_unknown_or_duplicate_assignments(
    tmp_path,
    extra,
):
    review = tmp_path / "review.md"
    review.write_text(
        _candidate_review_text() + extra,
        encoding="utf-8",
    )

    with pytest.raises(GateEReplayError) as captured:
        verify_approved_review(review)

    assert captured.value.code == "candidate_review_not_approved"


def test_build_trust_accepts_only_fixed_review_path(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    approved = repo / CANDIDATE_REVIEW_PATH
    approved.parent.mkdir(parents=True)
    approved.write_text(_candidate_review_text(), encoding="utf-8")
    candidate = SimpleNamespace(
        candidate="A",
        repository_root=repo,
        run_id="c" * 64,
        to_trust_evidence=lambda path: path,
    )
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        replay_module,
        "_load_candidate_evidence",
        lambda _path: candidate,
    )
    monkeypatch.setattr(
        replay_module,
        "_audit_verified_candidate",
        lambda _candidate, **_kwargs: None,
    )
    monkeypatch.setattr(
        replay_module,
        "gate_e_trust_bytes",
        lambda *, evidence, expected_run_id: (
            f"{evidence}:{expected_run_id}\n".encode()
        ),
    )

    built = replay_module.build_trust_from_candidate(
        evidence_path=tmp_path / "candidate-a-evidence.json",
        approved_review=CANDIDATE_REVIEW_PATH,
    )
    assert built == f"{approved}:{'c' * 64}\n".encode()

    elsewhere = tmp_path / CANDIDATE_REVIEW_PATH.name
    elsewhere.write_text(_candidate_review_text(), encoding="utf-8")
    with pytest.raises(GateEReplayError) as captured:
        replay_module.build_trust_from_candidate(
            evidence_path=tmp_path / "candidate-a-evidence.json",
            approved_review=elsewhere,
        )
    assert captured.value.code == "candidate_review_not_approved"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("project", "other-project"),
        ("version", "v0.1"),
        ("gate", "D"),
        ("review_kind", "trust_anchor"),
        ("decision", "FAIL"),
        ("P0", "1"),
        ("P1", "1"),
        ("P2", "1"),
        ("implementation_commit", "f" * 39),
        ("candidate_evidence_sha256", "f" * 63),
        ("expected_run_id", "f" * 63),
        ("artifact_manifest_sha256", "f" * 63),
        ("project_wheel_sha256", "f" * 63),
    ),
)
def test_each_candidate_review_binding_is_required(
    tmp_path,
    field,
    value,
):
    review = tmp_path / "review.md"
    lines = _candidate_review_text().splitlines()
    review.write_text(
        "\n".join(
            f"{field} = {value}" if line.startswith(f"{field} = ") else line
            for line in lines
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(GateEReplayError) as captured:
        verify_approved_review(review)

    assert captured.value.code == "candidate_review_not_approved"


def test_gate_e_entrypoint_and_wrapper_have_no_source_fallback():
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    wrapper = (
        PROJECT_ROOT / "scripts/verify_v02_gate_e.sh"
    ).read_text(encoding="utf-8")

    assert project["project"]["scripts"]["aquant-gate-e"] == (
        "aquant.gate_e.cli:main"
    )
    assert "set -eu" in wrapper
    assert "AQUANT_GATE_E_CLI" in wrapper
    assert "PYTHONPATH" in wrapper
    assert "unset PYTHONPATH" in wrapper
    assert "uv.lock" in wrapper
    assert "exec " in wrapper
    assert "python -m" not in wrapper
    assert "src/aquant" not in wrapper


def test_candidate_a_cli_dispatches_once_and_sanitizes_success(
    tmp_path,
    monkeypatch,
    capsys,
):
    replay = _replay(tmp_path)
    result = CandidateAResult(
        evidence_path=tmp_path / "candidate-a-evidence.json",
        artifact=tmp_path / ("1" * 64),
        run_id="1" * 64,
        progress=tuple(
            replay_module.GateEProgressEvent(stage, index, 7)
            for index, stage in enumerate(
                (
                    "trust_roots_verified",
                    "wheel_verified",
                    "environment_a_installed",
                    "inputs_a_verified",
                    "candidate_a_run",
                    "candidate_a_reversed",
                    "candidate_a_audited",
                ),
                start=1,
            )
        ),
    )
    calls: list[object] = []
    monkeypatch.setattr(
        cli_module,
        "create_gate_e_replay",
        lambda **_kwargs: replay,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "run_candidate_a",
        lambda value: calls.append(value) or result,
        raising=False,
    )

    exit_code = cli_module.main(
        (
            "candidate-a",
            "--config",
            str(replay.config_path),
            "--wheel",
            str(replay.project_wheel),
            "--wheelhouse",
            str(replay.wheelhouse_root),
            "--workspace",
            str(replay.workspace_a),
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == [replay]
    assert json.loads(captured.out) == {
        "candidate": "A",
        "evidence": "candidate-a-evidence.json",
        "run_id": "1" * 64,
        "stage_count": 7,
        "status": "candidate",
        "trusted": False,
    }
    assert str(tmp_path) not in captured.out
    assert captured.err == ""


def test_build_trust_cli_writes_only_canonical_payload(
    tmp_path,
    monkeypatch,
    capsys,
):
    expected = b'{"gate":"E","schema_version":"1.0"}\n'
    monkeypatch.setattr(
        cli_module,
        "build_trust_from_candidate",
        lambda **_kwargs: expected,
        raising=False,
    )

    exit_code = cli_module.main(
        (
            "build-trust",
            "--evidence",
            str(tmp_path / "candidate-a-evidence.json"),
            "--approved-review",
            str(tmp_path / "review.md"),
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.encode() == expected
    assert captured.err == ""


def test_verify_trust_cli_forwards_approved_review_exactly(
    tmp_path,
    monkeypatch,
    capsys,
):
    calls: list[dict[str, object]] = []
    expected = {
        "artifact_file_count": 13,
        "status": "trusted",
    }
    monkeypatch.setattr(
        cli_module,
        "verify_candidate_trust",
        lambda **kwargs: calls.append(kwargs) or expected,
    )
    trust = tmp_path / "trust.json"
    evidence = tmp_path / "candidate-a-evidence.json"
    artifact = tmp_path / "artifact"
    review = tmp_path / "approved-review.md"

    exit_code = cli_module.main(
        (
            "verify-trust",
            "--trust",
            str(trust),
            "--evidence",
            str(evidence),
            "--artifact",
            str(artifact),
            "--approved-review",
            str(review),
        )
    )

    assert exit_code == 0
    assert calls == [
        {
            "trust": trust,
            "evidence_path": evidence,
            "artifact": artifact,
            "approved_review": review,
        }
    ]
    assert json.loads(capsys.readouterr().out) == expected


def test_replay_b_cli_forwards_approval_commit_exactly(
    tmp_path,
    monkeypatch,
    capsys,
):
    replay = object()
    create_calls: list[dict[str, Path]] = []
    replay_calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        cli_module,
        "create_gate_e_replay_b",
        lambda **kwargs: create_calls.append(kwargs) or replay,
    )
    monkeypatch.setattr(
        cli_module,
        "replay_environment_b",
        lambda value, **kwargs: (
            replay_calls.append((value, kwargs))
            or SimpleNamespace(run_id="c" * 64)
        ),
    )
    anchor = "a" * 40
    approval = "b" * 40
    trust_path = Path("release/v0.2-gate-e/trust_manifest.json")
    wheel = tmp_path / "project.whl"
    wheelhouse = tmp_path / "wheelhouse"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"

    exit_code = cli_module.main(
        (
            "replay-b",
            "--trust-anchor-commit",
            anchor,
            "--approval-commit",
            approval,
            "--trust-path",
            trust_path.as_posix(),
            "--wheel",
            str(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--workspace-a",
            str(workspace_a),
            "--workspace-b",
            str(workspace_b),
        )
    )

    assert exit_code == 0
    assert create_calls == [
        {
            "project_wheel": wheel,
            "wheelhouse": wheelhouse,
            "workspace_a": workspace_a,
            "workspace_b": workspace_b,
        }
    ]
    assert replay_calls == [
        (
            replay,
            {
                "trust_anchor_commit": anchor,
                "approval_commit": approval,
                "trust_path": trust_path,
            },
        )
    ]
    assert json.loads(capsys.readouterr().out) == {
        "candidate": "B",
        "run_id": "c" * 64,
        "status": "verified_replay",
        "trusted": True,
    }
