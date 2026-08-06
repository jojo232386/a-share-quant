from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aquant.gate_e.inputs import (
    EMPTY_SHA256,
    GateEInputError,
    main,
    quarantine_manifest_lock,
    stage_gate_e_input_root,
    verify_and_copy_gate_e_inputs,
    verify_gate_e_input_roots_independent,
    verify_gate_e_release_inputs,
    verify_post_run_input_root,
)
from aquant.portfolio import PORTFOLIO_ARTIFACT_FILES

PROJECT_ROOT = Path(__file__).parents[2]
FROZEN_RELEASE = PROJECT_ROOT / "release/v0.1-research"
LOCK_RELATIVE = Path("data/manifests/manifest.jsonl.lock")
QUARANTINE_RELATIVE = Path("release/v0.2-gate-e/deviations/manifest.jsonl.lock.quarantined")


def _materialize_release(
    tmp_path: Path,
    *,
    with_lock: bool = True,
) -> tuple[Path, Path]:
    project_root = tmp_path / "project"
    release_root = project_root / "release/v0.1-research"
    shutil.copytree(FROZEN_RELEASE, release_root)
    lock = release_root / "inputs" / LOCK_RELATIVE
    if with_lock:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch(exist_ok=True)
    else:
        lock.unlink(missing_ok=True)
    return project_root, release_root


def _quarantine_path(project_root: Path) -> Path:
    return project_root / QUARANTINE_RELATIVE


def _manifest_paths(release_root: Path) -> tuple[str, ...]:
    payload = json.loads((release_root / "release_manifest.json").read_text(encoding="utf-8"))
    return tuple(sorted(payload["input_files"]))


def _actual_relative_files(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    )


def test_extra_frozen_lock_is_rejected_before_quarantine(tmp_path):
    _project_root, release_root = _materialize_release(tmp_path)

    with pytest.raises(GateEInputError) as captured:
        verify_gate_e_release_inputs(release_root)

    assert captured.value.code == "input_file_set_mismatch"


def test_release_manifest_requires_external_trusted_hash(tmp_path):
    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    manifest = release_root / "release_manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")

    with pytest.raises(GateEInputError) as captured:
        verify_gate_e_release_inputs(release_root)

    assert captured.value.code == "untrusted_release_manifest"


def test_parsed_manifest_is_bound_to_the_trusted_bytes(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    original = inputs_module.load_release_manifest(release_root / "release_manifest.json")
    forged = replace(
        original,
        input_files=tuple((f"data/raw/forged-{index}.bin", f"{index:064x}") for index in range(25)),
    )
    monkeypatch.setattr(
        inputs_module,
        "load_release_manifest",
        lambda _path: forged,
    )

    with pytest.raises(GateEInputError) as captured:
        verify_gate_e_release_inputs(release_root)

    assert captured.value.code == "untrusted_release_manifest"


def test_quarantine_moves_only_exact_empty_single_link_lock(tmp_path):
    project_root, release_root = _materialize_release(tmp_path)
    source = release_root / "inputs" / LOCK_RELATIVE
    original = source.lstat()
    destination = _quarantine_path(project_root)

    deviation = quarantine_manifest_lock(
        release_root,
        destination,
        recorded_at_utc=datetime(
            2026,
            7,
            29,
            12,
            0,
            tzinfo=UTC,
        ),
    )

    assert not source.exists()
    assert destination.read_bytes() == b""
    metadata = destination.lstat()
    assert metadata.st_nlink == 1
    assert metadata.st_ino == original.st_ino
    assert deviation.original_path == (
        "release/v0.1-research/inputs/data/manifests/manifest.jsonl.lock"
    )
    assert deviation.quarantine_path == (
        "release/v0.2-gate-e/deviations/manifest.jsonl.lock.quarantined"
    )
    assert deviation.size == 0
    assert deviation.sha256 == EMPTY_SHA256
    assert deviation.device == metadata.st_dev
    assert deviation.inode == metadata.st_ino
    assert deviation.link_count_before == 1
    assert deviation.link_count_after == 1
    assert deviation.recorded_at_utc == "2026-07-29T12:00:00Z"
    assert deviation.modified_at_utc.endswith("Z")
    assert deviation.birth_at_utc.endswith("Z")
    assert deviation.move_reason == "stale_untracked_reader_lock"
    assert deviation.research_semantics == ("declared_25_input_bytes_unchanged")
    assert len(verify_gate_e_release_inputs(release_root)) == 25


@pytest.mark.parametrize(
    "mutation",
    ("missing", "nonempty", "symlink", "hardlink", "directory"),
)
def test_quarantine_rejects_unexpected_lock_without_losing_source(
    tmp_path,
    mutation,
):
    project_root, release_root = _materialize_release(tmp_path)
    source = release_root / "inputs" / LOCK_RELATIVE
    if mutation == "missing":
        source.unlink()
    elif mutation == "nonempty":
        source.write_bytes(b"x")
    elif mutation == "symlink":
        source.unlink()
        target = project_root / "other.lock"
        target.touch()
        source.symlink_to(target)
    elif mutation == "hardlink":
        other = project_root / "other.lock"
        other.touch()
        source.unlink()
        os.link(other, source)
    elif mutation == "directory":
        source.unlink()
        source.mkdir()

    with pytest.raises(GateEInputError) as captured:
        quarantine_manifest_lock(
            release_root,
            _quarantine_path(project_root),
        )

    assert captured.value.code == "unexpected_lock_file"
    assert not _quarantine_path(project_root).exists()


def test_quarantine_rejects_wrong_or_preexisting_destination(tmp_path):
    project_root, release_root = _materialize_release(tmp_path)
    source = release_root / "inputs" / LOCK_RELATIVE
    wrong = project_root / "elsewhere/quarantined.lock"
    with pytest.raises(GateEInputError) as captured:
        quarantine_manifest_lock(release_root, wrong)
    assert captured.value.code == "invalid_quarantine_path"
    assert source.exists()

    destination = _quarantine_path(project_root)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"keep")
    with pytest.raises(GateEInputError) as captured:
        quarantine_manifest_lock(release_root, destination)
    assert captured.value.code == "quarantine_destination_conflict"
    assert source.exists()
    assert destination.read_bytes() == b"keep"


def test_quarantine_rejects_ancestor_symlink_without_external_write(
    tmp_path,
):
    project_root, release_root = _materialize_release(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (project_root / "release/v0.2-gate-e").symlink_to(
        external,
        target_is_directory=True,
    )

    with pytest.raises(GateEInputError) as captured:
        quarantine_manifest_lock(
            release_root,
            _quarantine_path(project_root),
        )

    assert captured.value.code == "invalid_quarantine_path"
    assert not (external / "deviations").exists()
    assert (release_root / "inputs" / LOCK_RELATIVE).exists()


def test_quarantine_parent_rebind_race_never_writes_external(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    project_root, release_root = _materialize_release(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    gate_path = project_root / "release/v0.2-gate-e"
    displaced = project_root / "release/v0.2-gate-e.displaced"
    real_open = inputs_module._open_directory_at
    swapped = False

    def swap_after_open(*args, **kwargs):
        nonlocal swapped
        handle = real_open(*args, **kwargs)
        if kwargs.get("name") == "v0.2-gate-e" and not swapped:
            gate_path.rename(displaced)
            gate_path.symlink_to(external, target_is_directory=True)
            swapped = True
        return handle

    monkeypatch.setattr(
        inputs_module,
        "_open_directory_at",
        swap_after_open,
    )

    with pytest.raises(GateEInputError) as captured:
        quarantine_manifest_lock(
            release_root,
            _quarantine_path(project_root),
        )

    assert captured.value.code == "invalid_quarantine_path"
    assert tuple(external.iterdir()) == ()
    assert (release_root / "inputs" / LOCK_RELATIVE).exists()


def test_quarantine_atomic_move_race_preserves_competitor(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    project_root, release_root = _materialize_release(tmp_path)
    source = release_root / "inputs" / LOCK_RELATIVE
    destination = _quarantine_path(project_root)
    real_rename = inputs_module._rename_no_replace
    competitor_inode: int | None = None

    def competitor(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal competitor_inode
        descriptor = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=destination_descriptor,
        )
        try:
            os.write(descriptor, b"competitor")
            os.fsync(descriptor)
            competitor_inode = os.fstat(descriptor).st_ino
        finally:
            os.close(descriptor)
        real_rename(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(
        inputs_module,
        "_rename_no_replace",
        competitor,
    )

    with pytest.raises(GateEInputError) as captured:
        quarantine_manifest_lock(
            release_root,
            destination,
        )

    assert captured.value.code == "quarantine_destination_conflict"
    assert source.exists()
    assert source.lstat().st_nlink == 1
    assert destination.read_bytes() == b"competitor"
    assert destination.lstat().st_ino == competitor_inode


def test_quarantine_never_unlinks_source_name(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    project_root, release_root = _materialize_release(tmp_path)
    destination = _quarantine_path(project_root)
    real_unlink = inputs_module.os.unlink

    def reject_source_unlink(path, *args, **kwargs):
        if path == LOCK_RELATIVE.name and kwargs.get("dir_fd") is not None:
            raise AssertionError("source name must move atomically")
        return real_unlink(path, *args, **kwargs)

    supported = inputs_module.os.supports_dir_fd | {reject_source_unlink}
    monkeypatch.setattr(inputs_module.os, "unlink", reject_source_unlink)
    monkeypatch.setattr(
        inputs_module.os,
        "supports_dir_fd",
        supported,
    )

    deviation = quarantine_manifest_lock(
        release_root,
        destination,
    )

    assert deviation.inode == destination.lstat().st_ino
    assert not (release_root / "inputs" / LOCK_RELATIVE).exists()


def test_quarantine_post_rename_fsync_failure_keeps_recovery_target(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    project_root, release_root = _materialize_release(tmp_path)
    source = release_root / "inputs" / LOCK_RELATIVE
    source_inode = source.lstat().st_ino
    destination = _quarantine_path(project_root)
    real_fsync = inputs_module.os.fsync
    failed = False

    def fail_after_move(descriptor: int) -> None:
        nonlocal failed
        if destination.exists() and not failed:
            failed = True
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(inputs_module.os, "fsync", fail_after_move)

    with pytest.raises(GateEInputError) as captured:
        quarantine_manifest_lock(
            release_root,
            destination,
        )

    assert captured.value.code == "quarantine_move_failed"
    assert not source.exists()
    assert destination.lstat().st_ino == source_inode
    assert destination.read_bytes() == b""


def test_quarantine_closes_every_directory_handle(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    project_root, release_root = _materialize_release(tmp_path)
    real_open = inputs_module._open_directory_at
    descriptors: list[int] = []

    def recording_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        descriptors.append(handle.descriptor)
        return handle

    monkeypatch.setattr(
        inputs_module,
        "_open_directory_at",
        recording_open,
    )

    quarantine_manifest_lock(
        release_root,
        _quarantine_path(project_root),
    )

    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_quarantine_can_retry_with_exact_safe_parent_already_present(
    tmp_path,
):
    project_root, release_root = _materialize_release(tmp_path)
    _quarantine_path(project_root).parent.mkdir(parents=True)

    deviation = quarantine_manifest_lock(
        release_root,
        _quarantine_path(project_root),
    )

    assert deviation.sha256 == EMPTY_SHA256
    assert _quarantine_path(project_root).is_file()


def test_quarantine_cli_emits_one_canonical_sanitized_json_line(
    tmp_path,
    monkeypatch,
    capsys,
):
    project_root, _release_root = _materialize_release(tmp_path)
    monkeypatch.chdir(project_root)

    assert (
        main(
            (
                "quarantine",
                "--release-root",
                "release/v0.1-research",
                "--destination",
                QUARANTINE_RELATIVE.as_posix(),
            )
        )
        == 0
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert captured.out == (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    assert payload["original_path"] == (
        "release/v0.1-research/inputs/data/manifests/manifest.jsonl.lock"
    )
    assert payload["quarantine_path"] == (QUARANTINE_RELATIVE.as_posix())
    assert payload["sha256"] == EMPTY_SHA256
    assert payload["size"] == 0
    assert payload["status"] == "quarantined"
    assert payload["link_count_before"] == 1
    assert payload["link_count_after"] == 1
    assert payload["recorded_at_utc"].endswith("Z")
    assert payload["birth_at_utc"].endswith("Z")
    assert payload["modified_at_utc"].endswith("Z")
    assert payload["move_reason"] == "stale_untracked_reader_lock"
    assert payload["research_semantics"] == ("declared_25_input_bytes_unchanged")

    assert main(("quarantine", "--release-root", "/private/secret")) == 1
    error = capsys.readouterr()
    assert error.out == ""
    error_payload = json.loads(error.err)
    assert error_payload == {
        "error_code": "invalid_arguments",
        "status": "error",
    }
    assert "/private/secret" not in error.err
    assert str(project_root) not in error.err


def test_quarantine_cli_rejects_wrong_path_and_existing_target(
    tmp_path,
    monkeypatch,
    capsys,
):
    project_root, _release_root = _materialize_release(tmp_path)
    monkeypatch.chdir(project_root)
    assert (
        main(
            (
                "quarantine",
                "--release-root",
                "release/v0.1-research",
                "--destination",
                "../outside.lock",
            )
        )
        == 1
    )
    wrong = capsys.readouterr()
    assert json.loads(wrong.err) == {
        "error_code": "invalid_quarantine_path",
        "status": "error",
    }
    assert ".." not in wrong.err

    destination = _quarantine_path(project_root)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"keep")
    assert (
        main(
            (
                "quarantine",
                "--release-root",
                "release/v0.1-research",
                "--destination",
                QUARANTINE_RELATIVE.as_posix(),
            )
        )
        == 1
    )
    conflict = capsys.readouterr()
    assert json.loads(conflict.err) == {
        "error_code": "quarantine_destination_conflict",
        "status": "error",
    }
    assert destination.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "mutation",
    ("missing", "nonempty", "symlink", "hardlink", "directory"),
)
def test_quarantine_cli_rejects_each_bad_lock_with_sanitized_json(
    tmp_path,
    monkeypatch,
    capsys,
    mutation,
):
    project_root, release_root = _materialize_release(tmp_path)
    source = release_root / "inputs" / LOCK_RELATIVE
    if mutation == "missing":
        source.unlink()
    elif mutation == "nonempty":
        source.write_bytes(b"x")
    elif mutation == "symlink":
        source.unlink()
        target = project_root / "other.lock"
        target.touch()
        source.symlink_to(target)
    elif mutation == "hardlink":
        other = project_root / "other.lock"
        other.touch()
        source.unlink()
        os.link(other, source)
    elif mutation == "directory":
        source.unlink()
        source.mkdir()
    monkeypatch.chdir(project_root)

    assert (
        main(
            (
                "quarantine",
                "--release-root",
                "release/v0.1-research",
                "--destination",
                QUARANTINE_RELATIVE.as_posix(),
            )
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error_code": "unexpected_lock_file",
        "status": "error",
    }
    assert str(project_root) not in captured.err


def test_candidate_a_is_staged_and_verified_before_b_exists(
    tmp_path,
):
    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    copy_parent = tmp_path / "copies"
    copy_parent.mkdir()
    first = copy_parent / "A"
    second = copy_parent / "B"

    first_evidence = stage_gate_e_input_root(
        release_root,
        first,
    )

    assert first_evidence.file_count == 25
    assert first_evidence.destination_root == first
    assert first.is_dir()
    assert not second.exists()

    second_evidence = stage_gate_e_input_root(
        release_root,
        second,
    )
    combined = verify_gate_e_input_roots_independent(
        release_root,
        first,
        second,
    )

    assert second_evidence.file_count == 25
    assert combined.destination_roots == (first, second)


def test_copy_parent_rebind_never_writes_external(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    parent = tmp_path / "copies"
    parent.mkdir()
    displaced = tmp_path / "copies.displaced"
    external = tmp_path / "external"
    external.mkdir()
    real_create = inputs_module._create_staging_root
    swapped = False

    def swap_after_create(*args, **kwargs):
        nonlocal swapped
        result = real_create(*args, **kwargs)
        if not swapped:
            parent.rename(displaced)
            parent.symlink_to(external, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(
        inputs_module,
        "_create_staging_root",
        swap_after_create,
    )

    with pytest.raises(GateEInputError) as captured:
        stage_gate_e_input_root(
            release_root,
            parent / "A",
        )

    assert captured.value.code == "copy_destination_unsafe"
    assert tuple(external.iterdir()) == ()
    assert len(tuple(displaced.glob(".*.gate-e-staging-*"))) == 1
    assert len(verify_gate_e_release_inputs(release_root)) == 25


def test_staging_creation_rebind_closes_handle_and_names_evidence(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    parent = tmp_path / "copies"
    parent.mkdir()
    displaced = tmp_path / "copies.displaced"
    external = tmp_path / "external"
    external.mkdir()
    real_open = inputs_module._open_directory_at
    staging_descriptor: int | None = None

    def swap_before_parent_recheck(*args, **kwargs):
        nonlocal staging_descriptor
        handle = real_open(*args, **kwargs)
        if kwargs["name"].startswith(".A.gate-e-staging-"):
            staging_descriptor = handle.descriptor
            parent.rename(displaced)
            parent.symlink_to(external, target_is_directory=True)
        return handle

    monkeypatch.setattr(
        inputs_module,
        "_open_directory_at",
        swap_before_parent_recheck,
    )

    with pytest.raises(GateEInputError) as captured:
        stage_gate_e_input_root(
            release_root,
            parent / "A",
        )

    error = captured.value
    assert error.code == "copy_destination_unsafe"
    assert error.cause_code == "copy_destination_unsafe"
    assert error.publication_state == "staging_retained"
    assert error.evidence_name is not None
    assert (displaced / error.evidence_name).is_dir()
    assert tuple(external.iterdir()) == ()
    assert staging_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(staging_descriptor)


def test_prepublication_failure_identifies_this_retained_staging(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    parent = tmp_path / "copies"
    parent.mkdir()
    old_names = {
        ".A.gate-e-staging-old-one",
        ".A.gate-e-staging-old-two",
    }
    for name in old_names:
        (parent / name).mkdir()
    monkeypatch.setattr(inputs_module.sys, "platform", "unsupported")

    with pytest.raises(GateEInputError) as captured:
        stage_gate_e_input_root(
            release_root,
            parent / "A",
        )

    error = captured.value
    assert error.code == "atomic_publish_unavailable"
    assert error.cause_code == "atomic_publish_unavailable"
    assert error.publication_state == "staging_retained"
    assert error.evidence_name not in old_names
    assert error.evidence_name is not None
    assert error.evidence_name.startswith(".A.gate-e-staging-")
    assert (parent / error.evidence_name).is_dir()
    assert _actual_relative_files(parent / error.evidence_name) == _manifest_paths(release_root)


def test_copy_stages_exact_25_separate_inodes_in_a_and_b(tmp_path):
    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    copy_parent = tmp_path / "copies"
    copy_parent.mkdir()
    first = copy_parent / "A"
    second = copy_parent / "B"

    evidence = verify_and_copy_gate_e_inputs(
        release_root,
        first,
        second,
    )

    expected = _manifest_paths(release_root)
    assert evidence.file_count == 25
    assert evidence.destination_roots == (first, second)
    assert _actual_relative_files(first) == expected
    assert _actual_relative_files(second) == expected
    for relative in expected:
        source_meta = (release_root / "inputs" / relative).lstat()
        first_meta = (first / relative).lstat()
        second_meta = (second / relative).lstat()
        objects = {
            (source_meta.st_dev, source_meta.st_ino),
            (first_meta.st_dev, first_meta.st_ino),
            (second_meta.st_dev, second_meta.st_ino),
        }
        assert len(objects) == 3


def test_copy_rejects_existing_overlap_and_extra_empty_directory(
    tmp_path,
):
    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    parent = tmp_path / "copies"
    parent.mkdir()
    existing = parent / "existing"
    existing.mkdir()

    with pytest.raises(GateEInputError) as captured:
        verify_and_copy_gate_e_inputs(
            release_root,
            existing,
            parent / "B",
        )
    assert captured.value.code == "copy_destination_conflict"

    with pytest.raises(GateEInputError) as captured:
        verify_and_copy_gate_e_inputs(
            release_root,
            parent / "same",
            parent / "same",
        )
    assert captured.value.code == "copy_destination_overlap"

    (release_root / "inputs/extra-empty").mkdir()
    with pytest.raises(GateEInputError) as captured:
        verify_and_copy_gate_e_inputs(
            release_root,
            parent / "C",
            parent / "D",
        )
    assert captured.value.code == "input_directory_set_mismatch"


def test_copy_publish_race_never_replaces_first_competitor(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    parent = tmp_path / "copies"
    parent.mkdir()
    first = parent / "A"
    second = parent / "B"
    real_rename = inputs_module._rename_no_replace
    competitor_inode: int | None = None

    def competitor(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal competitor_inode
        os.mkdir(destination_name, dir_fd=destination_descriptor)
        competitor_inode = os.stat(
            destination_name,
            dir_fd=destination_descriptor,
            follow_symlinks=False,
        ).st_ino
        real_rename(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(
        inputs_module,
        "_rename_no_replace",
        competitor,
    )

    with pytest.raises(GateEInputError) as captured:
        verify_and_copy_gate_e_inputs(
            release_root,
            first,
            second,
        )

    assert captured.value.code == "copy_destination_conflict"
    assert first.is_dir()
    assert first.lstat().st_ino == competitor_inode
    assert tuple(first.iterdir()) == ()
    assert not second.exists()
    assert len(tuple(parent.glob(".*.gate-e-staging-*"))) == 1


def test_second_publish_race_preserves_competitor_and_first_evidence(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    parent = tmp_path / "copies"
    parent.mkdir()
    first = parent / "A"
    second = parent / "B"
    real_rename = inputs_module._rename_no_replace
    calls = 0
    competitor_inode: int | None = None

    def competitor(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal calls, competitor_inode
        calls += 1
        if calls == 2:
            os.mkdir(destination_name, dir_fd=destination_descriptor)
            competitor_inode = os.stat(
                destination_name,
                dir_fd=destination_descriptor,
                follow_symlinks=False,
            ).st_ino
        real_rename(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(
        inputs_module,
        "_rename_no_replace",
        competitor,
    )

    with pytest.raises(GateEInputError) as captured:
        verify_and_copy_gate_e_inputs(
            release_root,
            first,
            second,
        )

    assert captured.value.code == "copy_partial_publication"
    assert captured.value.cause_code == "copy_destination_conflict"
    assert _actual_relative_files(first) == _manifest_paths(release_root)
    assert second.is_dir()
    assert second.lstat().st_ino == competitor_inode
    assert tuple(second.iterdir()) == ()
    assert len(tuple(parent.glob(".*.gate-e-staging-*"))) == 1


def test_post_rename_fsync_failure_reports_partial_publication(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    parent = tmp_path / "copies"
    parent.mkdir()
    first = parent / "A"
    second = parent / "B"
    real_fsync = inputs_module.os.fsync
    failed = False

    def fail_after_first_publish(descriptor: int) -> None:
        nonlocal failed
        if first.exists() and not failed:
            failed = True
            raise OSError("injected parent fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(inputs_module.os, "fsync", fail_after_first_publish)

    with pytest.raises(GateEInputError) as captured:
        verify_and_copy_gate_e_inputs(
            release_root,
            first,
            second,
        )

    assert captured.value.code == "copy_partial_publication"
    assert captured.value.cause_code == ("copy_post_publish_verification_failed")
    assert _actual_relative_files(first) == _manifest_paths(release_root)
    assert not second.exists()
    assert not tuple(parent.glob(".*.gate-e-staging-*"))


def test_post_publish_parent_failure_always_has_cause_and_evidence(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    parent = tmp_path / "copies"
    parent.mkdir()
    target = parent / "A"
    real_assert = inputs_module._assert_directory_bound
    post_publish_parent_checks = 0

    def fail_second_post_publish_parent_check(*args, **kwargs):
        nonlocal post_publish_parent_checks
        handle = args[0]
        real_assert(*args, **kwargs)
        if handle.absolute_path == parent and target.exists():
            post_publish_parent_checks += 1
            if post_publish_parent_checks == 2:
                raise GateEInputError(kwargs["code"])

    monkeypatch.setattr(
        inputs_module,
        "_assert_directory_bound",
        fail_second_post_publish_parent_check,
    )

    with pytest.raises(GateEInputError) as captured:
        stage_gate_e_input_root(
            release_root,
            target,
        )

    error = captured.value
    assert error.code == "copy_partial_publication"
    assert error.cause_code == ("copy_post_publish_parent_binding_failed")
    assert error.evidence_name == "A"
    assert error.publication_state == ("destination_published_unverified")
    assert _actual_relative_files(target) == _manifest_paths(release_root)


def test_missing_atomic_rename_primitive_fails_closed(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    parent = tmp_path / "copies"
    parent.mkdir()
    source = parent / "source"
    source.mkdir()
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(inputs_module.sys, "platform", "unsupported")
    try:
        with pytest.raises(GateEInputError) as captured:
            inputs_module._rename_no_replace(
                descriptor,
                source.name,
                descriptor,
                "destination",
            )
    finally:
        os.close(descriptor)

    assert captured.value.code == "atomic_publish_unavailable"
    assert source.is_dir()
    assert not (parent / "destination").exists()


def test_post_run_requires_exact_unchanged_25_file_tree(
    tmp_path,
):
    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    parent = tmp_path / "copies"
    parent.mkdir()
    first = parent / "A"
    second = parent / "B"
    verify_and_copy_gate_e_inputs(release_root, first, second)

    evidence = verify_post_run_input_root(release_root, first)
    assert evidence.file_count == 25
    assert not hasattr(evidence, "sidecar_sha256")

    lock = first / LOCK_RELATIVE
    lock.touch()
    with pytest.raises(GateEInputError) as captured:
        verify_post_run_input_root(release_root, first)
    assert captured.value.code == "post_run_input_set_mismatch"


def test_post_run_project_mode_accepts_only_the_fixed_runtime_closure(
    tmp_path,
):
    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    runtime_root = tmp_path / "runtime-project"
    stage_gate_e_input_root(release_root, runtime_root)
    config = runtime_root / "configs/releases/v0.2_gate_e.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(b"{}\n")
    run_id = "c" * 64
    artifact = runtime_root / "outputs/portfolios" / run_id
    artifact.mkdir(parents=True)
    for name in PORTFOLIO_ARTIFACT_FILES:
        (artifact / name).write_bytes(b"")
    lock = runtime_root / "outputs/portfolios" / f".{run_id}.lock"
    lock.write_bytes(b"")

    evidence = verify_post_run_input_root(
        release_root,
        runtime_root,
        expected_run_id=run_id,
    )

    assert evidence.file_count == 25
    (runtime_root / "outputs/unexpected-empty").mkdir()
    with pytest.raises(GateEInputError) as captured:
        verify_post_run_input_root(
            release_root,
            runtime_root,
            expected_run_id=run_id,
        )
    assert captured.value.code == "input_directory_set_mismatch"


@pytest.mark.parametrize(
    "mutation",
    (
        "extra_file",
        "extra_directory",
        "changed_input",
        "nonempty_lock",
        "symlink_lock",
        "hardlink_lock",
        "misplaced_lock",
    ),
)
def test_post_run_rejects_every_other_mutation(tmp_path, mutation):
    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    parent = tmp_path / "copies"
    parent.mkdir()
    first = parent / "A"
    second = parent / "B"
    verify_and_copy_gate_e_inputs(release_root, first, second)
    lock = first / LOCK_RELATIVE

    if mutation == "extra_file":
        (first / "extra.txt").write_text("x", encoding="utf-8")
    elif mutation == "extra_directory":
        (first / "extra-empty").mkdir()
    elif mutation == "changed_input":
        relative = _manifest_paths(release_root)[0]
        changed = first / relative
        changed.chmod(0o644)
        changed.write_bytes(changed.read_bytes() + b"x")
    elif mutation == "nonempty_lock":
        lock.write_bytes(b"x")
    elif mutation == "symlink_lock":
        target = tmp_path / "other.lock"
        target.touch()
        lock.symlink_to(target)
    elif mutation == "hardlink_lock":
        target = tmp_path / "other.lock"
        target.touch()
        os.link(target, lock)
    elif mutation == "misplaced_lock":
        (first / "data/manifests/other.lock").touch()

    with pytest.raises(GateEInputError):
        verify_post_run_input_root(release_root, first)


def test_copy_detects_source_change_before_publication(
    tmp_path,
    monkeypatch,
):
    import aquant.gate_e.inputs as inputs_module

    _project_root, release_root = _materialize_release(
        tmp_path,
        with_lock=False,
    )
    parent = tmp_path / "copies"
    parent.mkdir()
    original = inputs_module._copy_verified_file_at
    call_count = 0

    def mutate_after_first_copy(*args, **kwargs):
        nonlocal call_count
        result = original(*args, **kwargs)
        call_count += 1
        if call_count == 25:
            relative = _manifest_paths(release_root)[0]
            source = release_root / "inputs" / relative
            source.write_bytes(source.read_bytes() + b"x")
        return result

    monkeypatch.setattr(
        inputs_module,
        "_copy_verified_file_at",
        mutate_after_first_copy,
    )

    with pytest.raises(GateEInputError) as captured:
        verify_and_copy_gate_e_inputs(
            release_root,
            parent / "A",
            parent / "B",
        )

    assert captured.value.code in {
        "input_hash_mismatch",
        "input_changed_during_copy",
    }
    assert not (parent / "A").exists()
    assert not (parent / "B").exists()
    assert len(tuple(parent.glob(".*.gate-e-staging-*"))) == 1
