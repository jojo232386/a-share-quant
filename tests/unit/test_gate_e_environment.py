from __future__ import annotations

import hashlib
import json
import shutil
import socket
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import aquant.gate_e.environment as environment_module
from aquant.gate_e.environment import (
    GateEEnvironmentError,
    GateEEnvironmentLayout,
    build_project_wheel,
    capture_environment_execution_guard,
    capture_runtime_execution_guard,
    copy_gate_e_config,
    execution_environment,
    inspect_project_wheel,
    install_gate_e_environment,
    make_environment_layout,
    run_controlled,
    run_sandboxed,
    run_sandboxed_with_environment_guard,
    seal_environment_execution_tree,
    stage_environment_inputs,
    verify_environment_execution_guard,
    verify_runtime_execution_guard,
    verify_wheelhouse,
    write_wheelhouse_install_lock,
    write_wheelhouse_manifest,
)

PROJECT_ROOT = Path(__file__).parents[2]


@contextmanager
def reachable_local_listener():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def test_wheel_must_be_v02_and_contain_portfolio_entry(tmp_path):
    wheel = build_project_wheel(PROJECT_ROOT, tmp_path / "dist")

    evidence = inspect_project_wheel(wheel)

    assert evidence.distribution_version == "0.2.0"
    assert evidence.portfolio_cli_present is True
    assert evidence.entry_point == (
        "aquant-portfolio = aquant.portfolio_cli:main"
    )


def test_missing_wheelhouse_dependency_fails_closed(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_wheelhouse(
            wheelhouse,
            expected_requirements={"akshare": "1.18.64"},
        )

    assert captured.value.code == "wheelhouse_incomplete"


def test_requirement_contract_rejects_non_distribution_name(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_wheelhouse(
            wheelhouse,
            expected_requirements={"bad\nname": "1.0"},
        )

    assert captured.value.code == "invalid_wheelhouse_contract"


def test_wheelhouse_manifest_binds_exact_wheel_bytes(tmp_path):
    wheel = build_project_wheel(PROJECT_ROOT, tmp_path / "dist")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    sealed_wheel = wheelhouse / wheel.name
    shutil.copy2(wheel, sealed_wheel)
    manifest = tmp_path / "wheelhouse_manifest.json"
    requirements = {"a-share-quant": "0.2.0"}

    written = write_wheelhouse_manifest(
        wheelhouse,
        manifest,
        expected_requirements=requirements,
    )
    evidence = verify_wheelhouse(
        wheelhouse,
        expected_requirements=requirements,
        manifest=manifest,
    )

    assert written == manifest
    assert evidence.manifest_sha256 is not None
    assert len(evidence.entries) == 1

    with sealed_wheel.open("ab") as stream:
        stream.write(b"x")

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_wheelhouse(
            wheelhouse,
            expected_requirements=requirements,
            manifest=manifest,
        )

    assert captured.value.code == "wheelhouse_manifest_mismatch"


def test_wheelhouse_rejects_nonwheel_and_hardlink(tmp_path):
    wheel = build_project_wheel(PROJECT_ROOT, tmp_path / "dist")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    copied = wheelhouse / wheel.name
    shutil.copy2(wheel, copied)
    (wheelhouse / "unexpected.txt").write_text("x", encoding="utf-8")

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_wheelhouse(
            wheelhouse,
            expected_requirements={"a-share-quant": "0.2.0"},
        )

    assert captured.value.code == "wheelhouse_unexpected_file"

    (wheelhouse / "unexpected.txt").unlink()
    (wheelhouse / f"duplicate-{copied.name}").hardlink_to(copied)

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_wheelhouse(
            wheelhouse,
            expected_requirements={"a-share-quant": "0.2.0"},
        )

    assert captured.value.code == "unsafe_wheel"


def test_wheelhouse_rejects_duplicate_distribution_and_symlink(tmp_path):
    wheel = build_project_wheel(PROJECT_ROOT, tmp_path / "dist")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(wheel, wheelhouse / wheel.name)
    duplicate = wheelhouse / "a_share_quant-0.2.0-py2-none-any.whl"
    shutil.copy2(wheel, duplicate)

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_wheelhouse(
            wheelhouse,
            expected_requirements={"a-share-quant": "0.2.0"},
        )

    assert captured.value.code == "wheelhouse_duplicate"

    duplicate.unlink()
    (wheelhouse / "linked.whl").symlink_to(wheelhouse / wheel.name)

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_wheelhouse(
            wheelhouse,
            expected_requirements={"a-share-quant": "0.2.0"},
        )

    assert captured.value.code == "unsafe_wheel"


def test_wheelhouse_manifest_is_external_canonical_and_immutable(tmp_path):
    wheel = build_project_wheel(PROJECT_ROOT, tmp_path / "dist")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(wheel, wheelhouse / wheel.name)
    requirements = {"a-share-quant": "0.2.0"}

    with pytest.raises(GateEEnvironmentError) as captured:
        write_wheelhouse_manifest(
            wheelhouse,
            wheelhouse / "manifest.json",
            expected_requirements=requirements,
        )

    assert captured.value.code == "unsafe_wheelhouse_manifest"

    manifest = tmp_path / "manifest.json"
    write_wheelhouse_manifest(
        wheelhouse,
        manifest,
        expected_requirements=requirements,
    )
    original = manifest.read_bytes()

    with pytest.raises(GateEEnvironmentError) as captured:
        write_wheelhouse_manifest(
            wheelhouse,
            manifest,
            expected_requirements=requirements,
        )

    assert captured.value.code == "wheelhouse_manifest_conflict"
    assert manifest.read_bytes() == original

    parsed = original.decode("utf-8")
    manifest.write_text(parsed.replace(',"wheels"', ', "wheels"'), encoding="utf-8")

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_wheelhouse(
            wheelhouse,
            expected_requirements=requirements,
            manifest=manifest,
        )

    assert captured.value.code == "wheelhouse_manifest_invalid"


def test_platform_install_lock_binds_the_built_wheel_hash(tmp_path):
    wheel = build_project_wheel(PROJECT_ROOT, tmp_path / "dist")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    sealed_wheel = wheelhouse / wheel.name
    shutil.copy2(wheel, sealed_wheel)
    requirements = {"a-share-quant": "0.2.0"}
    manifest = tmp_path / "wheelhouse_manifest.json"
    install_lock = tmp_path / "requirements.install.lock.txt"
    write_wheelhouse_manifest(
        wheelhouse,
        manifest,
        expected_requirements=requirements,
    )

    write_wheelhouse_install_lock(
        wheelhouse,
        install_lock,
        expected_requirements=requirements,
        manifest=manifest,
    )
    evidence = verify_wheelhouse(
        wheelhouse,
        expected_requirements=requirements,
        manifest=manifest,
        install_lock=install_lock,
    )

    assert install_lock.read_text(encoding="utf-8") == (
        "a-share-quant==0.2.0 "
        f"--hash=sha256:{evidence.entries[0].sha256}\n"
    )
    assert evidence.install_lock_sha256 is not None

    install_lock.write_bytes(install_lock.read_bytes() + b"# changed\n")
    with pytest.raises(GateEEnvironmentError) as captured:
        verify_wheelhouse(
            wheelhouse,
            expected_requirements=requirements,
            manifest=manifest,
            install_lock=install_lock,
        )

    assert captured.value.code == "wheelhouse_install_lock_mismatch"


def test_environment_roots_and_mutable_files_are_independent(tmp_path):
    first = make_environment_layout(
        tmp_path / "a",
        repository_root=PROJECT_ROOT,
    )
    second = make_environment_layout(
        tmp_path / "b",
        repository_root=PROJECT_ROOT,
    )

    assert isinstance(first, GateEEnvironmentLayout)
    assert first.root != second.root
    assert first.home != second.home
    assert first.uv_cache != second.uv_cache
    assert first.output_root != second.output_root
    assert first.root.stat().st_ino != second.root.stat().st_ino
    assert first.python.exists()
    assert second.python.exists()


def test_environment_bootstrap_denies_writes_to_candidate_a(
    tmp_path,
    monkeypatch,
):
    candidate_a = tmp_path / "candidate-a"
    candidate_a.mkdir()
    marker = candidate_a / "evidence.txt"
    marker.write_bytes(b"sealed")
    before = (
        marker.read_bytes(),
        marker.stat().st_mtime_ns,
        candidate_a.stat().st_mtime_ns,
    )
    original_run = environment_module.subprocess.run
    commands = []

    def recording_run(command, *args, **kwargs):
        if command[0] == "/usr/bin/sandbox-exec":
            commands.append(tuple(command))
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(
        environment_module.subprocess,
        "run",
        recording_run,
    )

    layout = make_environment_layout(
        tmp_path / "candidate-b",
        repository_root=PROJECT_ROOT,
        read_only_paths=(candidate_a,),
    )

    assert layout.python.exists()
    assert len(commands) == 2
    for command in commands:
        assert command[:2] == ("/usr/bin/sandbox-exec", "-p")
        assert str(candidate_a) in command[2]
        assert str(layout.base_python) in command[2]
        assert "file-write*" in command[2]
    assert (
        marker.read_bytes(),
        marker.stat().st_mtime_ns,
        candidate_a.stat().st_mtime_ns,
    ) == before


def test_runtime_guard_rejects_same_bytes_restored_after_mutation(tmp_path):
    executable = tmp_path / "runtime"
    source = environment_module.canonical_uv_executable()
    shutil.copyfile(source, executable)
    executable.chmod(0o755)
    original = executable.read_bytes()
    guard = capture_runtime_execution_guard(executable)

    executable.write_bytes(b"x" * len(original))
    executable.write_bytes(original)
    executable.chmod(0o755)

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_runtime_execution_guard(executable, guard)

    assert captured.value.code == "runtime_execution_changed"


def test_runtime_guard_rejects_restored_python_symlink(tmp_path):
    target = tmp_path / "python-target"
    shutil.copyfile(environment_module.canonical_python_executable(), target)
    target.chmod(0o755)
    intermediate = tmp_path / "python3.11"
    intermediate.symlink_to(target.name)
    executable = tmp_path / "python"
    executable.symlink_to(intermediate.name)
    guard = capture_runtime_execution_guard(executable)

    intermediate.unlink()
    intermediate.symlink_to(target.name)

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_runtime_execution_guard(executable, guard)

    assert captured.value.code == "runtime_execution_changed"


def test_environment_guard_rejects_writable_tree(tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "module.py").write_bytes(b"value = 1\n")

    with pytest.raises(GateEEnvironmentError) as captured:
        capture_environment_execution_guard(venv)

    assert captured.value.code == "environment_execution_changed"


def test_environment_guard_rejects_unapproved_external_symlink(tmp_path):
    external = tmp_path / "external"
    external.write_bytes(b"external\n")
    external.chmod(0o444)
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "escaped").symlink_to(external)
    venv.chmod(0o555)

    with pytest.raises(GateEEnvironmentError) as captured:
        capture_environment_execution_guard(venv)

    assert captured.value.code == "environment_execution_changed"


def test_sealed_environment_guard_rejects_same_bytes_restored(tmp_path):
    venv = tmp_path / "venv"
    package = venv / "lib/python3.11/site-packages/aquant"
    package.mkdir(parents=True)
    module = package / "__init__.py"
    module.write_bytes(b"VERSION = '0.2.0'\n")
    executable = venv / "bin/aquant-portfolio"
    executable.parent.mkdir()
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)

    guard = seal_environment_execution_tree(venv)

    assert guard == capture_environment_execution_guard(venv)
    assert not venv.stat().st_mode & 0o222
    assert not module.stat().st_mode & 0o222
    assert executable.stat().st_mode & 0o111
    package.chmod(0o755)
    module.chmod(0o644)
    original = module.read_bytes()
    module.unlink()
    module.write_bytes(original)
    module.chmod(0o444)
    package.chmod(0o555)

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_environment_execution_guard(venv, guard)

    assert captured.value.code == "environment_execution_changed"


def test_sealed_environment_guard_rejects_added_then_removed_empty_dir(
    tmp_path,
):
    venv = tmp_path / "venv"
    package = venv / "site-packages"
    package.mkdir(parents=True)
    (package / "module.py").write_bytes(b"value = 1\n")
    guard = seal_environment_execution_tree(venv)

    venv.chmod(0o755)
    extra = venv / "temporary-empty"
    extra.mkdir()
    extra.rmdir()
    venv.chmod(0o555)

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_environment_execution_guard(venv, guard)

    assert captured.value.code == "environment_execution_changed"


def test_environment_guard_wraps_command_and_rejects_restoration(
    tmp_path,
    monkeypatch,
):
    venv = tmp_path / "venv"
    package = venv / "site-packages"
    package.mkdir(parents=True)
    module = package / "aquant.py"
    module.write_bytes(b"value = 1\n")
    base_python = environment_module.canonical_python_executable()
    python = venv / "bin/python"
    python.parent.mkdir()
    python.symlink_to(base_python)
    guard = seal_environment_execution_tree(
        venv,
        allowed_external_targets=(base_python,),
    )
    observed_read_only = []

    def mutate_then_restore(*_args, **kwargs):
        observed_read_only.extend(kwargs["read_only_paths"])
        venv.chmod(0o755)
        package.chmod(0o755)
        module.chmod(0o644)
        original = module.read_bytes()
        module.unlink()
        module.write_bytes(original)
        module.chmod(0o444)
        package.chmod(0o555)
        venv.chmod(0o555)
        return environment_module.subprocess.CompletedProcess(
            args=["probe"],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(
        environment_module,
        "run_sandboxed",
        mutate_then_restore,
    )
    layout = environment_module.GateEEnvironmentLayout(
        root=tmp_path,
        home=tmp_path / "home",
        xdg_cache=tmp_path / "cache",
        uv_cache=tmp_path / "uv-cache",
        venv=venv,
        python=python,
        project_root=tmp_path / "project",
        input_root=tmp_path / "project",
        output_root=tmp_path / "project/outputs",
        config_path=tmp_path / "project/config.json",
        repository_root=PROJECT_ROOT,
        base_python=base_python,
    )

    with pytest.raises(GateEEnvironmentError) as captured:
        run_sandboxed_with_environment_guard(
            layout,
            ["probe"],
            expected_execution_guard=guard,
        )

    assert captured.value.code == "environment_execution_changed"
    assert venv in observed_read_only
    assert base_python in observed_read_only


def test_guarded_subprocess_rejects_runtime_mutation_then_restore(
    tmp_path,
    monkeypatch,
):
    executable = tmp_path / "runtime"
    shutil.copyfile(environment_module.canonical_uv_executable(), executable)
    executable.chmod(0o755)
    original = executable.read_bytes()
    guard = capture_runtime_execution_guard(executable)

    def mutate_then_restore(*_args, **_kwargs):
        executable.write_bytes(b"x" * len(original))
        executable.write_bytes(original)
        executable.chmod(0o755)
        return environment_module.subprocess.CompletedProcess(
            args=[str(executable)],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(
        environment_module,
        "run_sandboxed",
        mutate_then_restore,
    )

    with pytest.raises(GateEEnvironmentError) as captured:
        environment_module._run_with_runtime_guard(
            object(),
            [str(executable)],
            runtime_path=executable,
            runtime_guard=guard,
            hash_seed="101",
        )

    assert captured.value.code == "runtime_execution_changed"


def test_environment_root_rejects_symlinked_parent(tmp_path):
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)

    with pytest.raises(GateEEnvironmentError) as captured:
        make_environment_layout(
            linked_parent / "a",
            repository_root=PROJECT_ROOT,
        )

    assert captured.value.code == "unsafe_environment_root"


def test_execution_environment_is_an_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted")
    layout = make_environment_layout(
        tmp_path / "a",
        repository_root=PROJECT_ROOT,
    )

    environment = execution_environment(layout, hash_seed="101")

    assert environment == {
        "HOME": str(layout.home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PIP_CONFIG_FILE": "/dev/null",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "101",
        "PYTHONNOUSERSITE": "1",
        "TZ": "Asia/Shanghai",
        "UV_CACHE_DIR": str(layout.uv_cache),
        "UV_LINK_MODE": "copy",
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "XDG_CACHE_HOME": str(layout.xdg_cache),
    }

    forged = replace(layout, home=Path.home())
    with pytest.raises(GateEEnvironmentError) as captured:
        execution_environment(forged, hash_seed="101")

    assert captured.value.code == "invalid_environment_layout"


def test_sandbox_denies_a_proven_reachable_socket(tmp_path):
    layout = make_environment_layout(
        tmp_path / "a",
        repository_root=PROJECT_ROOT,
    )
    with reachable_local_listener() as port:
        command = [
            str(layout.python),
            "-c",
            (
                "import socket;"
                f"socket.create_connection(('127.0.0.1',{port}),timeout=1)"
            ),
        ]
        baseline = run_controlled(layout, command)
        completed = run_sandboxed(layout, command)

    assert baseline.returncode == 0
    assert completed.returncode != 0


def test_sandbox_cannot_read_repository_sentinel(tmp_path):
    layout = make_environment_layout(
        tmp_path / "a",
        repository_root=PROJECT_ROOT,
    )
    sentinel = PROJECT_ROOT / ".gate-e-source-read-probe"
    sentinel.write_text("denied", encoding="utf-8")
    try:
        baseline = run_controlled(
            layout,
            [str(layout.python), "-c", f"open({str(sentinel)!r}).read()"],
        )
        completed = run_sandboxed(
            layout,
            [str(layout.python), "-c", f"open({str(sentinel)!r}).read()"],
        )
    finally:
        sentinel.unlink()

    assert baseline.returncode == 0
    assert completed.returncode != 0


def test_sandbox_cannot_modify_declared_read_only_path(tmp_path):
    layout = make_environment_layout(
        tmp_path / "a",
        repository_root=PROJECT_ROOT,
    )
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    evidence = sealed / "evidence.txt"
    evidence.write_text("sealed", encoding="utf-8")

    completed = run_sandboxed(
        layout,
        [
            str(layout.python),
            "-c",
            f"open({str(evidence)!r},'w').write('changed')",
        ],
        read_only_paths=[sealed],
    )

    assert completed.returncode != 0
    assert evidence.read_text(encoding="utf-8") == "sealed"


def test_sandbox_read_only_path_denies_even_idempotent_mode_change(
    tmp_path,
):
    layout = make_environment_layout(
        tmp_path / "a",
        repository_root=PROJECT_ROOT,
    )
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    evidence = sealed / "evidence.txt"
    evidence.write_text("sealed", encoding="utf-8")
    evidence.chmod(0o444)
    script = (
        "import os;"
        f"fd=os.open({str(evidence)!r},os.O_RDONLY);"
        "data=os.read(fd,1024);"
        "os.fchmod(fd,0o444);"
        "os.close(fd);"
        "print(data.decode())"
    )

    completed = run_sandboxed(
        layout,
        [str(layout.python), "-c", script],
        read_only_paths=[sealed],
    )

    assert completed.returncode != 0
    assert evidence.read_text(encoding="utf-8") == "sealed"
    assert evidence.stat().st_mode & 0o777 == 0o444


def test_sandbox_verification_mode_file_allows_fchmod_but_no_data_write(
    tmp_path,
):
    layout = make_environment_layout(
        tmp_path / "a",
        repository_root=PROJECT_ROOT,
    )
    evidence = tmp_path / "snapshot.parquet"
    evidence.write_text("sealed", encoding="utf-8")
    evidence.chmod(0o444)
    verify_script = (
        "import os;"
        f"fd=os.open({str(evidence)!r},os.O_RDONLY);"
        "data=os.read(fd,1024);"
        "os.fchmod(fd,0o444);"
        "os.close(fd);"
        "print(data.decode())"
    )

    verified = run_sandboxed(
        layout,
        [str(layout.python), "-c", verify_script],
        verification_mode_files=[evidence],
    )
    modified = run_sandboxed(
        layout,
        [
            str(layout.python),
            "-c",
            f"open({str(evidence)!r},'w').write('changed')",
        ],
        verification_mode_files=[evidence],
    )
    removed = run_sandboxed(
        layout,
        [
            str(layout.python),
            "-c",
            f"import os;os.unlink({str(evidence)!r})",
        ],
        verification_mode_files=[evidence],
    )
    assert verified.returncode == 0
    assert verified.stdout == "sealed\n"
    assert modified.returncode != 0
    assert removed.returncode != 0
    assert evidence.read_text(encoding="utf-8") == "sealed"
    assert evidence.stat().st_mode & 0o777 == 0o444


@pytest.mark.parametrize(
    "script",
    (
        "import os;os.chmod({marker!r},0o600)",
        "open({marker!r},'w').write('changed')",
        "open({created!r},'w').write('new')",
        "import os;os.mkdir({created!r})",
        "import os;os.unlink({marker!r})",
        "import os;os.utime({marker!r},ns=(1,1))",
    ),
)
def test_sandbox_candidate_a_boundary_denies_every_write_class(
    tmp_path,
    script,
):
    layout = make_environment_layout(
        tmp_path / "candidate-b",
        repository_root=PROJECT_ROOT,
    )
    candidate_a = tmp_path / "candidate-a"
    candidate_a.mkdir()
    marker = candidate_a / "evidence.txt"
    marker.write_bytes(b"sealed")
    marker.chmod(0o444)
    created = candidate_a / "created.txt"
    before = (
        marker.read_bytes(),
        marker.stat().st_mode,
        marker.stat().st_mtime_ns,
        candidate_a.stat().st_mtime_ns,
        tuple(path.name for path in candidate_a.iterdir()),
    )

    completed = run_sandboxed(
        layout,
        [
            str(layout.python),
            "-c",
            script.format(marker=str(marker), created=str(created)),
        ],
        read_only_paths=(candidate_a,),
    )

    assert completed.returncode != 0
    assert not created.exists()
    assert (
        marker.read_bytes(),
        marker.stat().st_mode,
        marker.stat().st_mtime_ns,
        candidate_a.stat().st_mtime_ns,
        tuple(path.name for path in candidate_a.iterdir()),
    ) == before


def test_configs_are_equal_but_do_not_share_inodes(tmp_path):
    source = PROJECT_ROOT / "configs/releases/v0.2_gate_e.json"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    first = make_environment_layout(
        tmp_path / "a",
        repository_root=PROJECT_ROOT,
    )
    second = make_environment_layout(
        tmp_path / "b",
        repository_root=PROJECT_ROOT,
    )

    first_config = copy_gate_e_config(first, source, expected_sha256=digest)
    second_config = copy_gate_e_config(second, source, expected_sha256=digest)

    assert first_config.read_bytes() == source.read_bytes()
    assert second_config.read_bytes() == source.read_bytes()
    assert len(
        {
            source.stat().st_ino,
            first_config.stat().st_ino,
            second_config.stat().st_ino,
        }
    ) == 3


def test_environment_input_copies_are_independent(tmp_path):
    release_root = PROJECT_ROOT / "release/v0.1-research"
    manifest = json.loads(
        (release_root / "release_manifest.json").read_text(encoding="utf-8")
    )
    relatives = tuple(sorted(manifest["input_files"]))
    first = make_environment_layout(
        tmp_path / "a",
        repository_root=PROJECT_ROOT,
    )
    second = make_environment_layout(
        tmp_path / "b",
        repository_root=PROJECT_ROOT,
    )

    stage_environment_inputs(first, release_root)
    stage_environment_inputs(second, release_root)

    assert len(relatives) == 25
    for relative in relatives:
        source = release_root / "inputs" / relative
        copied_a = first.project_root / relative
        copied_b = second.project_root / relative
        assert source.read_bytes() == copied_a.read_bytes()
        assert source.read_bytes() == copied_b.read_bytes()
        assert len(
            {
                (source.stat().st_dev, source.stat().st_ino),
                (copied_a.stat().st_dev, copied_a.stat().st_ino),
                (copied_b.stat().st_dev, copied_b.stat().st_ino),
            }
        ) == 3


def test_environment_install_rejects_wrong_project_wheel_hash(tmp_path):
    wheel = build_project_wheel(PROJECT_ROOT, tmp_path / "dist")
    layout = make_environment_layout(
        tmp_path / "environment",
        repository_root=PROJECT_ROOT,
    )

    with pytest.raises(GateEEnvironmentError) as captured:
        install_gate_e_environment(
            layout,
            project_wheel=wheel,
            expected_project_sha256="0" * 64,
            wheelhouse=tmp_path / "unused-wheelhouse",
            wheelhouse_manifest=tmp_path / "unused-manifest.json",
            install_lock=tmp_path / "unused-install-lock.txt",
            expected_requirements={"a-share-quant": "0.2.0"},
            hash_seed="101",
        )

    assert captured.value.code == "project_wheel_hash_mismatch"


def test_environment_install_rejects_writable_project_wheel(tmp_path):
    wheel = build_project_wheel(PROJECT_ROOT, tmp_path / "dist")
    wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    layout = make_environment_layout(
        tmp_path / "environment",
        repository_root=PROJECT_ROOT,
    )

    with pytest.raises(GateEEnvironmentError) as captured:
        install_gate_e_environment(
            layout,
            project_wheel=wheel,
            expected_project_sha256=wheel_sha256,
            wheelhouse=tmp_path / "unused-wheelhouse",
            wheelhouse_manifest=tmp_path / "unused-manifest.json",
            install_lock=tmp_path / "unused-install-lock.txt",
            expected_requirements={"a-share-quant": "0.2.0"},
            hash_seed="101",
        )

    assert captured.value.code == "mutable_project_wheel"
