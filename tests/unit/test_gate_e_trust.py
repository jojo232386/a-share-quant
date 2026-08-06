from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import sys
import zipfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import aquant.gate_e.replay as replay_module
from aquant.gate_e.environment import (
    canonical_python_executable,
    canonical_uv_executable,
    snapshot_python_runtime,
    snapshot_uv_runtime,
    write_wheelhouse_manifest,
)
from aquant.portfolio import export_portfolio_run, run_verified_portfolio

TESTS_ROOT = Path(__file__).parents[1]
PROJECT_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(TESTS_ROOT))
gate_c_support = importlib.import_module("portfolio_gate_c_support")
sys.path.pop(0)
make_portfolio_case = gate_c_support.make_portfolio_case

from aquant.gate_e.trust import (  # noqa: E402
    GateETrustError,
    GateETrustEvidence,
    verify_gate_e_trust,
    write_gate_e_trust,
)

_PYTHON = canonical_python_executable()
_UV = canonical_uv_executable()
_CANDIDATE_REVIEW_RELATIVE = (
    "outputs/Work_Buddy候选A复核_v0.2_Gate_E.md"
)


def _json_bytes(value: object) -> bytes:
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


def _make_project_wheel(path: Path) -> None:
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("aquant/portfolio_cli.py", "def main():\n    return 0\n")
        archive.writestr(
            "a_share_quant-0.2.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: a-share-quant\nVersion: 0.2.0\n",
        )
        archive.writestr(
            "a_share_quant-0.2.0.dist-info/entry_points.txt",
            "[console_scripts]\n"
            "aquant-portfolio = aquant.portfolio_cli:main\n",
        )


def _artifact(tmp_path: Path) -> tuple[Path, str]:
    run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "inputs",
            symbols=("600000", "600001"),
            gross_target_weight=Decimal("0.95"),
        )
    )
    return export_portfolio_run(run, tmp_path / "outputs"), run.identity.run_id


def _copy_regular(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def _evidence(
    tmp_path: Path,
) -> tuple[GateETrustEvidence, str]:
    artifact, run_id = _artifact(tmp_path / "portfolio")
    project_wheel = tmp_path / "project/a_share_quant-0.2.0-py3-none-any.whl"
    _make_project_wheel(project_wheel)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheelhouse_wheel = wheelhouse / project_wheel.name
    shutil.copyfile(project_wheel, wheelhouse_wheel)
    wheelhouse_manifest = tmp_path / "controls/wheelhouse_manifest.json"
    wheelhouse_manifest.parent.mkdir()
    write_wheelhouse_manifest(
        wheelhouse,
        wheelhouse_manifest,
        expected_requirements={"a-share-quant": "0.2.0"},
    )
    candidate_evidence = tmp_path / "review/candidate-a-evidence.json"
    candidate_evidence.parent.mkdir()
    candidate_evidence.write_bytes(b'{"candidate":"A"}\n')
    candidate_review = tmp_path / "review/candidate-review.md"
    candidate_review.write_text(
        "project = a-share-quant\n"
        "version = v0.2\n"
        "gate = E\n"
        "review_kind = candidate_a\n"
        "decision = PASS\n"
        "P0 = 0\n"
        "P1 = 0\n"
        "P2 = 0\n"
        f"implementation_commit = {'a' * 40}\n"
        "candidate_evidence_sha256 = "
        f"{hashlib.sha256(candidate_evidence.read_bytes()).hexdigest()}\n"
        f"expected_run_id = {run_id}\n"
        "artifact_manifest_sha256 = "
        f"{hashlib.sha256((artifact / 'artifact_manifest.json').read_bytes()).hexdigest()}\n"
        "project_wheel_sha256 = "
        f"{hashlib.sha256(project_wheel.read_bytes()).hexdigest()}\n",
        encoding="utf-8",
    )
    evidence = GateETrustEvidence(
        implementation_commit="a" * 40,
        project_wheel=project_wheel,
        uv_lock=_copy_regular(PROJECT_ROOT / "uv.lock", tmp_path / "locks/uv.lock"),
        python_executable=_PYTHON,
        uv_executable=_UV,
        expected_python_snapshot=snapshot_python_runtime(_PYTHON),
        expected_uv_snapshot=snapshot_uv_runtime(_UV),
        wheelhouse_root=wheelhouse,
        wheelhouse_manifest=wheelhouse_manifest,
        v01_tag_commit="b" * 40,
        v01_release_manifest=_copy_regular(
            PROJECT_ROOT / "release/v0.1-research/release_manifest.json",
            tmp_path / "release/release_manifest.json",
        ),
        config=_copy_regular(
            PROJECT_ROOT / "configs/releases/v0.2_gate_e.json",
            tmp_path / "config/v0.2_gate_e.json",
        ),
        artifact=artifact,
        candidate_review=candidate_review,
        reviewed_candidate_evidence=candidate_evidence,
    )
    return evidence, run_id


def _trust(
    tmp_path: Path,
) -> tuple[Path, GateETrustEvidence, str]:
    evidence, run_id = _evidence(tmp_path)
    path = write_gate_e_trust(
        tmp_path / "trust.json",
        evidence=evidence,
        expected_run_id=run_id,
    )
    return path, evidence, run_id


def test_public_build_verify_round_trip_requires_fixed_review_path(
    tmp_path,
    monkeypatch,
):
    evidence, run_id = _evidence(tmp_path)
    repository_root = tmp_path / "repo"
    fixed_review = repository_root / _CANDIDATE_REVIEW_RELATIVE
    fixed_review.parent.mkdir(parents=True)
    shutil.copyfile(evidence.candidate_review, fixed_review)
    bound_evidence = replace(evidence, candidate_review=fixed_review)

    def to_trust_evidence(
        review: Path,
        *,
        reviewed_candidate_evidence: Path | None = None,
    ) -> GateETrustEvidence:
        return replace(
            bound_evidence,
            candidate_review=review,
            reviewed_candidate_evidence=(
                reviewed_candidate_evidence
                if reviewed_candidate_evidence is not None
                else bound_evidence.reviewed_candidate_evidence
            ),
        )

    candidate = SimpleNamespace(
        artifact=bound_evidence.artifact,
        candidate="A",
        repository_root=repository_root,
        run_id=run_id,
        to_trust_evidence=to_trust_evidence,
    )
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
    evidence_path = tmp_path / "candidate-a-evidence.json"

    trust = tmp_path / "trust.json"
    trust.write_bytes(
        replay_module.build_trust_from_candidate(
            evidence_path=evidence_path,
            approved_review=Path(_CANDIDATE_REVIEW_RELATIVE),
        )
    )
    result = replay_module.verify_candidate_trust(
        trust=trust,
        evidence_path=evidence_path,
        artifact=bound_evidence.artifact,
        approved_review=Path(_CANDIDATE_REVIEW_RELATIVE),
    )

    assert result["status"] == "trusted"
    assert result["expected_run_id"] == run_id

    elsewhere = tmp_path / fixed_review.name
    shutil.copyfile(fixed_review, elsewhere)
    with pytest.raises(replay_module.GateEReplayError) as captured:
        replay_module.verify_candidate_trust(
            trust=trust,
            evidence_path=evidence_path,
            artifact=bound_evidence.artifact,
            approved_review=elsewhere,
        )
    assert captured.value.code == "candidate_review_not_approved"

    fixed_review.unlink()
    with pytest.raises(replay_module.GateEReplayError) as captured:
        replay_module.verify_candidate_trust(
            trust=trust,
            evidence_path=evidence_path,
            artifact=bound_evidence.artifact,
            approved_review=Path(_CANDIDATE_REVIEW_RELATIVE),
        )
    assert captured.value.code == "candidate_review_not_approved"


def _rewrite_trust(path: Path, mutate) -> None:
    payload = json.loads(path.read_bytes())
    mutate(payload)
    path.chmod(0o600)
    path.write_bytes(_json_bytes(payload))


def test_trust_manifest_rejects_wrong_run_id(tmp_path):
    evidence, _run_id = _evidence(tmp_path)
    trust = write_gate_e_trust(
        tmp_path / "trust.json",
        evidence=evidence,
        expected_run_id="0" * 64,
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "trusted_run_id_mismatch"


def test_trust_manifest_binds_13_files(tmp_path):
    trust, evidence, run_id = _trust(tmp_path)

    verified = verify_gate_e_trust(trust, evidence)

    assert verified.expected_run_id == run_id
    assert verified.artifact_file_count == 13
    assert verified.payload_file_count == 12
    assert tuple(name for name, _digest in verified.files) == (
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
    )
    assert verified.trust_sha256 == hashlib.sha256(trust.read_bytes()).hexdigest()


def test_trust_manifest_binds_complete_candidate_review_snapshot(tmp_path):
    trust, evidence, run_id = _trust(tmp_path)

    payload = json.loads(trust.read_bytes())
    review = payload["candidate_review"]
    review_bytes = evidence.candidate_review.read_bytes()
    assert review == {
        "bindings": {
            "P0": "0",
            "P1": "0",
            "P2": "0",
            "artifact_manifest_sha256": hashlib.sha256(
                (evidence.artifact / "artifact_manifest.json").read_bytes()
            ).hexdigest(),
            "candidate_evidence_sha256": hashlib.sha256(
                evidence.reviewed_candidate_evidence.read_bytes()
            ).hexdigest(),
            "decision": "PASS",
            "expected_run_id": run_id,
            "gate": "E",
            "implementation_commit": "a" * 40,
            "project": "a-share-quant",
            "project_wheel_sha256": hashlib.sha256(
                evidence.project_wheel.read_bytes()
            ).hexdigest(),
            "review_kind": "candidate_a",
            "version": "v0.2",
        },
        "path": _CANDIDATE_REVIEW_RELATIVE,
        "sha256": hashlib.sha256(review_bytes).hexdigest(),
        "size": len(review_bytes),
    }

    evidence.candidate_review.write_bytes(review_bytes + b"\n")
    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)
    assert captured.value.code == "candidate_review_mismatch"


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("implementation_commit", "f" * 40),
        ("candidate_evidence_sha256", "f" * 64),
        ("expected_run_id", "f" * 64),
        ("artifact_manifest_sha256", "f" * 64),
        ("project_wheel_sha256", "f" * 64),
    ),
)
def test_candidate_review_cannot_be_reused_for_different_evidence(
    tmp_path,
    field,
    replacement,
):
    evidence, run_id = _evidence(tmp_path)
    review = evidence.candidate_review
    review.write_text(
        "\n".join(
            f"{field} = {replacement}"
            if line.startswith(f"{field} = ")
            else line
            for line in review.read_text(encoding="utf-8").splitlines()
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(GateETrustError) as captured:
        write_gate_e_trust(
            tmp_path / "trust.json",
            evidence=evidence,
            expected_run_id=run_id,
        )

    assert captured.value.code == "candidate_review_mismatch"


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("path", "outputs/other.md"),
        ("sha256", "f" * 64),
        ("size", 1),
        ("bindings", {}),
    ),
)
def test_each_trust_candidate_review_snapshot_field_is_bound(
    tmp_path,
    field,
    replacement,
):
    trust, evidence, _run_id = _trust(tmp_path)
    _rewrite_trust(
        trust,
        lambda payload: payload["candidate_review"].__setitem__(
            field,
            replacement,
        ),
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "candidate_review_mismatch"


def test_implementation_commit_is_external_and_bound(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(
            trust,
            replace(evidence, implementation_commit="c" * 40),
        )

    assert captured.value.code == "implementation_commit_mismatch"


def test_project_wheel_name_size_and_hash_are_bound(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    evidence.project_wheel.write_bytes(evidence.project_wheel.read_bytes() + b"x")

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "project_wheel_mismatch"


def test_uv_lock_hash_is_bound(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    evidence.uv_lock.write_bytes(evidence.uv_lock.read_bytes() + b"\n")

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "uv_lock_mismatch"


def test_python_binary_hash_and_version_are_bound(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    fake_python = _copy_regular(
        evidence.python_executable,
        tmp_path / "fake/python3.11",
    )
    fake_python.chmod(0o755)

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(
            trust,
            replace(evidence, python_executable=fake_python),
        )

    assert captured.value.code == "python_mismatch"


def test_trust_rejects_uv_replaced_after_candidate_snapshot(tmp_path):
    evidence, run_id = _evidence(tmp_path)
    fake_uv = _copy_regular(
        evidence.uv_executable,
        tmp_path / "fake/uv",
    )
    fake_uv.chmod(0o755)
    candidate_snapshot = snapshot_uv_runtime(fake_uv)
    changed = fake_uv.read_bytes() + b"runtime-drift"
    fake_uv.write_bytes(changed)
    drifted = replace(
        evidence,
        uv_executable=fake_uv,
        expected_uv_snapshot=candidate_snapshot,
    )

    with pytest.raises(GateETrustError) as captured:
        write_gate_e_trust(
            tmp_path / "drifted-trust.json",
            evidence=drifted,
            expected_run_id=run_id,
        )

    assert captured.value.code == "uv_mismatch"
    assert not (tmp_path / "drifted-trust.json").exists()


def test_trust_executable_snapshots_are_device_neutral(tmp_path):
    trust, _evidence, _run_id = _trust(tmp_path)
    payload = json.loads(trust.read_bytes())

    assert "path" not in payload["python"]
    assert "path" not in payload["uv"]
    assert payload["python"]["implementation"] == "CPython"
    assert payload["uv"]["name"] == "uv"


def test_uv_binary_hash_and_version_are_bound(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    fake_uv = _copy_regular(
        evidence.uv_executable,
        tmp_path / "fake/uv",
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(
            trust,
            replace(evidence, uv_executable=fake_uv),
        )

    assert captured.value.code == "uv_mismatch"


def test_each_wheelhouse_entry_is_bound(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    wheel = next(evidence.wheelhouse_root.iterdir())
    wheel.write_bytes(wheel.read_bytes() + b"x")

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "wheelhouse_mismatch"


def test_wheelhouse_manifest_hash_is_bound(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    evidence.wheelhouse_manifest.write_bytes(
        evidence.wheelhouse_manifest.read_bytes() + b"\n"
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "wheelhouse_mismatch"


def test_v01_tag_commit_is_bound(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(
            trust,
            replace(evidence, v01_tag_commit="c" * 40),
        )

    assert captured.value.code == "v01_trust_mismatch"


def test_v01_release_manifest_hash_is_bound(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    evidence.v01_release_manifest.write_bytes(
        evidence.v01_release_manifest.read_bytes() + b"\n"
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "v01_trust_mismatch"


def test_complete_config_bytes_and_hash_are_bound(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    evidence.config.write_bytes(evidence.config.read_bytes() + b"\n")

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "config_mismatch"


def test_artifact_extra_file_is_rejected(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    (evidence.artifact / "extra.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "artifact_file_set_mismatch"


def test_artifact_directory_name_must_equal_run_id(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    renamed = evidence.artifact.parent / ("0" * 64)
    evidence.artifact.rename(renamed)

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(
            trust,
            replace(evidence, artifact=renamed),
        )

    assert captured.value.code == "artifact_mismatch"


@pytest.mark.parametrize(
    "filename",
    (
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
    ),
)
def test_each_artifact_size_and_hash_is_bound(tmp_path, filename):
    trust, evidence, _run_id = _trust(tmp_path)
    target = evidence.artifact / filename
    target.write_bytes(target.read_bytes() + b"x")

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "artifact_mismatch"


@pytest.mark.parametrize(
    "field",
    (
        "artifact_file_count",
        "no_bar_total",
        "payload_file_count",
        "session_count",
        "symbol_count",
        "target_count",
    ),
)
def test_expected_counts_are_bound(tmp_path, field):
    trust, evidence, _run_id = _trust(tmp_path)
    _rewrite_trust(
        trust,
        lambda payload: payload["artifact"]["expected_counts"].__setitem__(
            field,
            payload["artifact"]["expected_counts"][field] + 1,
        ),
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "artifact_count_mismatch"


def test_expected_row_counts_are_bound(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    _rewrite_trust(
        trust,
        lambda payload: payload["artifact"]["expected_counts"][
            "row_counts"
        ].__setitem__(
            "targets.csv",
            payload["artifact"]["expected_counts"]["row_counts"][
                "targets.csv"
            ]
            + 1,
        ),
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "artifact_count_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("sha256", "0" * 64),
        ("size", 1),
        ("version", "3.11.14"),
    ),
)
def test_python_manifest_fields_are_individually_bound(
    tmp_path,
    field,
    value,
):
    trust, evidence, _run_id = _trust(tmp_path)
    _rewrite_trust(
        trust,
        lambda payload: payload["python"].__setitem__(field, value),
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "python_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("sha256", "0" * 64),
        ("size", 1),
        ("version", "0.0.0"),
    ),
)
def test_uv_manifest_fields_are_individually_bound(
    tmp_path,
    field,
    value,
):
    trust, evidence, _run_id = _trust(tmp_path)
    _rewrite_trust(
        trust,
        lambda payload: payload["uv"].__setitem__(field, value),
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "uv_mismatch"


def test_config_decimal_must_remain_canonical_text(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    _rewrite_trust(
        trust,
        lambda payload: payload["config"]["payload"].__setitem__(
            "gross_target_weight",
            0.95,
        ),
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "config_mismatch"


def test_unknown_trust_key_is_rejected(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    _rewrite_trust(
        trust,
        lambda payload: payload.__setitem__("unknown", True),
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "invalid_trust_manifest"


def test_trust_file_link_is_rejected(tmp_path):
    trust, evidence, _run_id = _trust(tmp_path)
    alias = tmp_path / "trust-alias.json"
    alias.symlink_to(trust)

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(alias, evidence)

    assert captured.value.code == "unsafe_trust_manifest"
