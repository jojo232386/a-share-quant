import subprocess
from pathlib import Path

import pytest

from aquant.gate_e.versioned_audit import (
    VersionedAuditError,
    _git,
    _require_commit,
    load_v02_audit_profile,
    resolve_v02_audit,
)

PROJECT_ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("directory", ["ascii-worktree", "量化-worktree"])
def test_git_top_level_path_supports_ascii_and_non_ascii_worktrees(
    tmp_path: Path,
    directory: str,
) -> None:
    repository = tmp_path / directory
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
    )

    top_level = _git(
        repository,
        "rev-parse",
        "--show-toplevel",
        path_output=True,
    )

    assert isinstance(top_level, str)
    assert Path(top_level).resolve() == repository.resolve()


def test_git_identity_validation_remains_strict_and_fail_closed() -> None:
    with pytest.raises(VersionedAuditError) as captured:
        _require_commit("g" * 40, code="invalid_test_commit")

    assert captured.value.code == "invalid_test_commit"


def test_v02_research_implementation_resolves_from_trust_history() -> None:
    profile = load_v02_audit_profile(
        PROJECT_ROOT / "configs/audit_profiles/v0.2_gate_e.json"
    )
    bindings = resolve_v02_audit(PROJECT_ROOT, profile)

    assert bindings.audit_target == "577c157235cac50e0ab721a7c845b0f0836aa15b"
    assert bindings.implementation_commit == (
        "ae317a01c5c36a7a59836665917afec4a7377125"
    )
