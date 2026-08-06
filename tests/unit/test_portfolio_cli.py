from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import socket
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
import requests

import aquant.portfolio_cli as portfolio_cli
from aquant.data.akshare_client import AkshareClient
from aquant.gate_e.config import load_gate_e_config
from aquant.portfolio_cli import main

PROJECT_ROOT = Path(__file__).parents[2]
FROZEN_INPUT_ROOT = (
    PROJECT_ROOT / "release/v0.1-research/inputs"
)
TESTS_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(TESTS_ROOT))
gate_c_support = importlib.import_module("portfolio_gate_c_support")
gate_e_support = importlib.import_module("gate_e_support")
sys.path.pop(0)
materialize_portfolio_cli_case = gate_c_support.materialize_portfolio_cli_case
write_gate_e_cli_config = gate_c_support.write_gate_e_cli_config
valid_gate_e_payload = gate_e_support.valid_gate_e_payload


def _option(
    arguments: tuple[str, ...],
    name: str,
    value: str,
) -> tuple[str, ...]:
    result = list(arguments)
    position = result.index(name)
    result[position + 1] = value
    return tuple(result)


def _remove_first_option(
    arguments: tuple[str, ...],
    name: str,
) -> tuple[str, ...]:
    result = list(arguments)
    position = result.index(name)
    del result[position : position + 2]
    return tuple(result)


def _directory_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir(), key=lambda item: item.name)
    }


def _file_digest_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _verify_arguments(
    case,
    *,
    expected_run_id: str | None = None,
) -> tuple[str, ...]:
    result = [
        "verify",
        "--project-root",
        str(case.project_root),
        "--artifact",
        case.expected_relative_artifact,
    ]
    if expected_run_id is not None:
        result.extend(("--expected-run-id", expected_run_id))
    return tuple(result)


def test_cli_runs_explicit_verified_inputs_and_emits_stable_json(
    tmp_path,
    capsys,
):
    case = materialize_portfolio_cli_case(tmp_path)

    exit_code = main(case.arguments)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "artifact_directory": case.expected_relative_artifact,
        "run_id": case.expected_run_id,
        "status": "ok",
        "symbol_count": 2,
    }
    assert (case.project_root / case.expected_relative_artifact).is_dir()


def test_cli_verify_emits_named_file_counts_and_detects_damage(
    tmp_path,
    capsys,
):
    case = materialize_portfolio_cli_case(tmp_path)
    assert main(case.arguments) == 0
    capsys.readouterr()

    assert (
        main(
            _verify_arguments(
                case,
                expected_run_id=case.expected_run_id,
            )
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "verified"
    assert verified["run_id"] == case.expected_run_id
    assert verified["artifact_file_count"] == 13
    assert verified["payload_file_count"] == 12
    assert verified["file_count"] == 13

    metrics = case.project_root / case.expected_relative_artifact / "metrics.json"
    metrics.write_bytes(metrics.read_bytes() + b"x")
    assert main(_verify_arguments(case)) == 1
    error = json.loads(capsys.readouterr().err)
    assert error == {
        "error_code": "artifact_hash_mismatch",
        "error_type": "PortfolioArtifactError",
        "status": "error",
    }


@pytest.mark.parametrize(
    "mode",
    ("duplicate", "missing", "extra", "malformed"),
)
def test_cli_market_snapshot_mapping_contract_is_exact(
    tmp_path,
    capsys,
    mode,
):
    case = materialize_portfolio_cli_case(tmp_path)
    arguments = case.arguments
    if mode == "duplicate":
        symbol, snapshot_id = case.market_snapshot_ids[0]
        arguments = (
            *arguments,
            "--market-snapshot",
            f"{symbol}={snapshot_id}",
        )
        expected_code = "invalid_snapshot_mapping"
    elif mode == "missing":
        arguments = _remove_first_option(
            arguments,
            "--market-snapshot",
        )
        expected_code = "snapshot_mapping_mismatch"
    elif mode == "extra":
        arguments = (
            *arguments,
            "--market-snapshot",
            f"600002={'0' * 64}",
        )
        expected_code = "snapshot_mapping_mismatch"
    else:
        arguments = _option(
            arguments,
            "--market-snapshot",
            "600000=NOT-A-HASH",
        )
        expected_code = "invalid_snapshot_mapping"

    assert main(arguments) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error_code"] == expected_code


def test_cli_action_mapping_must_equal_verified_universe(
    tmp_path,
    capsys,
):
    case = materialize_portfolio_cli_case(tmp_path)
    arguments = _remove_first_option(
        case.arguments,
        "--corporate-action-snapshot",
    )

    assert main(arguments) == 1

    assert json.loads(capsys.readouterr().err)["error_code"] == ("snapshot_mapping_mismatch")


@pytest.mark.parametrize(
    "mode",
    ("duplicate", "extra", "malformed"),
)
def test_cli_action_snapshot_mapping_rejects_every_nonexact_form(
    tmp_path,
    capsys,
    mode,
):
    case = materialize_portfolio_cli_case(tmp_path)
    if mode == "duplicate":
        symbol, snapshot_id = case.action_snapshot_ids[0]
        arguments = (
            *case.arguments,
            "--corporate-action-snapshot",
            f"{symbol}={snapshot_id}",
        )
        expected_code = "invalid_snapshot_mapping"
    elif mode == "extra":
        arguments = (
            *case.arguments,
            "--corporate-action-snapshot",
            f"600002={'0' * 64}",
        )
        expected_code = "snapshot_mapping_mismatch"
    else:
        arguments = _option(
            case.arguments,
            "--corporate-action-snapshot",
            "600000=NOT-A-HASH",
        )
        expected_code = "invalid_snapshot_mapping"

    assert main(arguments) == 1

    assert json.loads(capsys.readouterr().err)["error_code"] == expected_code


@pytest.mark.parametrize(
    ("option", "value"),
    (
        ("--manifest", "/tmp/outside.jsonl"),
        ("--output", "/tmp/outside-output"),
        ("--manifest", "../outside.jsonl"),
        ("--output", "../outside-output"),
    ),
)
def test_cli_rejects_absolute_and_parent_paths(
    tmp_path,
    capsys,
    option,
    value,
):
    case = materialize_portfolio_cli_case(tmp_path)

    assert main(_option(case.arguments, option, value)) == 1

    assert json.loads(capsys.readouterr().err)["error_code"] == ("unsafe_path")


def test_cli_rejects_symlinked_components_and_hardlinked_inputs(
    tmp_path,
    capsys,
):
    symlink_case = materialize_portfolio_cli_case(
        tmp_path / "symlink",
    )
    (symlink_case.project_root / "linked-data").symlink_to(
        symlink_case.project_root / "data",
        target_is_directory=True,
    )
    symlink_arguments = _option(
        symlink_case.arguments,
        "--manifest",
        "linked-data/manifests/manifest.jsonl",
    )

    assert main(symlink_arguments) == 1
    assert json.loads(capsys.readouterr().err)["error_code"] == ("unsafe_path")

    hardlink_case = materialize_portfolio_cli_case(
        tmp_path / "hardlink",
    )
    hardlink = hardlink_case.project_root / "hardlinked-manifest.jsonl"
    os.link(
        hardlink_case.project_root / "data/manifests/manifest.jsonl",
        hardlink,
    )
    hardlink_arguments = _option(
        hardlink_case.arguments,
        "--manifest",
        "hardlinked-manifest.jsonl",
    )

    assert main(hardlink_arguments) == 1
    assert json.loads(capsys.readouterr().err)["error_code"] == ("unsafe_path")


def test_cli_argument_errors_are_sanitized(tmp_path, capsys):
    secret = "PRIVATE-RAW-ARGUMENT"

    assert main(("run", "--project-root", secret)) == 1

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert json.loads(captured.err) == {
        "error_code": "invalid_arguments",
        "error_type": "PortfolioCliError",
        "status": "error",
    }


def test_cli_conflict_is_unchanged_and_never_silently_overwritten(
    tmp_path,
    capsys,
):
    case = materialize_portfolio_cli_case(tmp_path)
    assert main(case.arguments) == 0
    capsys.readouterr()
    directory = case.project_root / case.expected_relative_artifact
    (directory / "competitor.txt").write_text(
        "keep",
        encoding="utf-8",
    )
    before = _directory_bytes(directory)

    assert main(case.arguments) == 1

    assert json.loads(capsys.readouterr().err)["error_code"] == ("artifact_conflict")
    assert _directory_bytes(directory) == before


def test_cli_public_run_path_never_uses_network(
    monkeypatch,
    tmp_path,
    capsys,
):
    case = materialize_portfolio_cli_case(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(AkshareClient, "fetch_batch", forbidden)

    assert main(case.arguments) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_cli_zero_commission_bundle_round_trips_through_verifier(
    tmp_path,
    capsys,
):
    case = materialize_portfolio_cli_case(tmp_path)
    arguments = case.arguments
    for option in (
        "--stock-commission-rate",
        "--stock-minimum-commission",
        "--etf-commission-rate",
        "--etf-minimum-commission",
    ):
        arguments = _option(arguments, option, "0")

    assert main(arguments) == 0
    run = json.loads(capsys.readouterr().out)

    assert (
        main(
            (
                "verify",
                "--project-root",
                str(case.project_root),
                "--artifact",
                run["artifact_directory"],
                "--expected-run-id",
                run["run_id"],
            )
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["run_id"] == run["run_id"]
    assert verified["status"] == "verified"


@pytest.mark.parametrize(
    "option",
    (
        "--stock-commission-rate",
        "--stock-minimum-commission",
        "--etf-commission-rate",
        "--etf-minimum-commission",
    ),
)
def test_cli_negative_commission_arguments_are_rejected(
    tmp_path,
    capsys,
    option,
):
    case = materialize_portfolio_cli_case(tmp_path)

    assert main(_option(case.arguments, option, "-0.1")) == 1

    assert json.loads(capsys.readouterr().err)["error_code"] == (
        "invalid_arguments"
    )


def test_cli_verify_wrong_expected_run_id_is_rejected(
    tmp_path,
    capsys,
):
    case = materialize_portfolio_cli_case(tmp_path)
    assert main(case.arguments) == 0
    capsys.readouterr()

    assert (
        main(
            _verify_arguments(
                case,
                expected_run_id="0" * 64,
            )
        )
        == 1
    )

    assert json.loads(capsys.readouterr().err)["error_code"] == ("artifact_identity_mismatch")


def test_cli_is_root_independent_and_artifacts_are_byte_identical(
    tmp_path,
    capsys,
):
    first = materialize_portfolio_cli_case(tmp_path / "first")
    second = materialize_portfolio_cli_case(tmp_path / "second")

    assert main(first.arguments) == 0
    capsys.readouterr()
    assert main(second.arguments) == 0
    capsys.readouterr()

    assert first.expected_run_id == second.expected_run_id
    assert _directory_bytes(
        first.project_root / first.expected_relative_artifact
    ) == _directory_bytes(second.project_root / second.expected_relative_artifact)


def test_gate_e_run_config_rejects_parameter_overrides(capsys):
    assert (
        main(
            (
                "run-config",
                "--config",
                "configs/releases/v0.2_gate_e.json",
                "--initial-cash-fen",
                "1",
            )
        )
        == 1
    )

    assert json.loads(capsys.readouterr().err)["error_code"] == (
        "invalid_arguments"
    )


def test_gate_e_run_config_rejects_ancestor_rebind_after_path_check(
    tmp_path,
    capsys,
    monkeypatch,
):
    payload = valid_gate_e_payload(PROJECT_ROOT)
    project = tmp_path / "project"
    config = write_gate_e_cli_config(project, payload)
    external = tmp_path / "external-releases"
    external.mkdir()
    (external / config.name).write_bytes(config.read_bytes())
    original_safe_path = portfolio_cli._safe_relative_path
    swapped = False

    def rebind_after_check(root, descriptor, value, *, kind):
        nonlocal swapped
        result = original_safe_path(
            root,
            descriptor,
            value,
            kind=kind,
        )
        if (
            not swapped
            and value == "configs/releases/v0.2_gate_e.json"
            and kind == "file"
        ):
            releases = project / "configs/releases"
            releases.rename(project / "configs/releases.checked")
            releases.symlink_to(external, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(
        portfolio_cli,
        "_safe_relative_path",
        rebind_after_check,
    )
    monkeypatch.setattr(
        portfolio_cli,
        "_load_run_arguments",
        lambda *_args, **_kwargs: (
            SimpleNamespace(
                identity=SimpleNamespace(run_id="a" * 64),
                result=SimpleNamespace(targets=tuple(range(10))),
            ),
            PurePosixPath(f"outputs/portfolios/{'a' * 64}"),
        ),
    )
    monkeypatch.chdir(project)

    assert main(("run-config", "--config", "configs/releases/v0.2_gate_e.json")) == 1
    assert swapped is True
    assert json.loads(capsys.readouterr().err)["error_code"] == (
        "unsafe_config_file"
    )


def test_gate_e_run_config_adapter_projects_same_config_across_roots(
    tmp_path,
    capsys,
    monkeypatch,
):
    payload = valid_gate_e_payload(PROJECT_ROOT)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_config = write_gate_e_cli_config(first, payload)
    second_config = write_gate_e_cli_config(second, payload)
    run_id = "a" * 64
    calls = []

    def fake_load(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return (
            SimpleNamespace(
                identity=SimpleNamespace(run_id=run_id),
                result=SimpleNamespace(targets=tuple(range(10))),
            ),
            PurePosixPath(f"outputs/portfolios/{run_id}"),
        )

    monkeypatch.setattr(
        portfolio_cli,
        "_load_run_arguments",
        fake_load,
    )

    monkeypatch.chdir(first)
    assert main(("run-config", "--config", first_config.relative_to(first).as_posix())) == 0
    first_result = json.loads(capsys.readouterr().out)

    monkeypatch.chdir(second)
    assert main(("run-config", "--config", second_config.relative_to(second).as_posix())) == 0
    second_result = json.loads(capsys.readouterr().out)

    assert first_result == second_result == {
        "artifact_directory": f"outputs/portfolios/{run_id}",
        "run_id": run_id,
        "status": "ok",
        "symbol_count": 10,
    }
    assert len(calls) == 2
    assert all(call[0] is None for call in calls)
    assert all(
        call[1]["gate_e_config"].to_portfolio_namespace(
            project_root="."
        ).market_snapshot
        == tuple(
            sorted(
                call[1]["gate_e_config"].to_portfolio_namespace(
                    project_root="."
                ).market_snapshot
            )
        )
        for call in calls
    )
    assert all(
        call[1]["gate_e_config"].to_fee_policy().policy_digest
        == payload["fee_policy_digest"]
        for call in calls
    )
    assert all(
        call[1][
            "gate_e_config"
        ].post_end_validation_date.isoformat()
        == "2026-07-24"
        for call in calls
    )


def test_gate_e_formal_paths_use_python_network_guard(
    tmp_path,
    capsys,
    monkeypatch,
):
    payload = valid_gate_e_payload(PROJECT_ROOT)
    config = write_gate_e_cli_config(tmp_path, payload)

    def network_run(*_args, **_kwargs):
        socket.create_connection(("127.0.0.1", 9))

    monkeypatch.setattr(
        portfolio_cli,
        "_load_run_arguments",
        network_run,
    )
    monkeypatch.chdir(tmp_path)

    assert main(("run-config", "--config", config.relative_to(tmp_path).as_posix())) == 1
    assert json.loads(capsys.readouterr().err)["error_code"] == (
        "network_access_forbidden"
    )

    artifact = tmp_path / "artifact"
    artifact.mkdir()

    def network_verify(*_args, **_kwargs):
        socket.create_connection(("127.0.0.1", 9))

    monkeypatch.setattr(
        portfolio_cli,
        "verify_portfolio_artifact",
        network_verify,
    )
    assert (
        main(
            (
                "verify",
                "--project-root",
                str(tmp_path),
                "--artifact",
                "artifact",
            )
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error_code"] == (
        "network_access_forbidden"
    )


def test_load_run_arguments_rejects_external_gate_e_namespace(tmp_path):
    case = materialize_portfolio_cli_case(tmp_path)
    arguments = portfolio_cli._parser().parse_args(case.arguments)
    formal_config_path = write_gate_e_cli_config(
        tmp_path,
        valid_gate_e_payload(PROJECT_ROOT),
    )
    formal_config = load_gate_e_config(formal_config_path)

    with portfolio_cli._safe_project_root(str(tmp_path)) as (
        root,
        descriptor,
        metadata,
    ):
        with pytest.raises(portfolio_cli.PortfolioCliError) as captured:
            portfolio_cli._load_run_arguments(
                arguments,
                root=root,
                root_descriptor=descriptor,
                root_metadata=metadata,
                gate_e_config=formal_config,
            )

    assert captured.value.code == "invalid_gate_e_contract"


def test_load_run_arguments_consumes_verified_gate_e_config_directly(
    tmp_path,
    monkeypatch,
):
    payload = valid_gate_e_payload(PROJECT_ROOT)
    frozen_inputs = tmp_path / "frozen-inputs"
    shutil.copytree(
        FROZEN_INPUT_ROOT,
        frozen_inputs,
        ignore=shutil.ignore_patterns("*.lock"),
    )
    market_lock = (
        frozen_inputs / "data/manifests/manifest.jsonl.lock"
    )
    before = _file_digest_tree(frozen_inputs)
    assert before == payload["input_files"]
    assert len(before) == 25
    assert not market_lock.exists()
    formal_config = load_gate_e_config(
        write_gate_e_cli_config(
            tmp_path,
            payload,
        )
    )
    run_id = "b" * 64
    observed = {}

    def fake_run_verified_portfolio(**kwargs):
        observed.update(kwargs)
        return SimpleNamespace(identity=SimpleNamespace(run_id=run_id))

    def fake_export(run, output_root):
        assert run.identity.run_id == run_id
        return output_root / run_id

    monkeypatch.setattr(
        portfolio_cli,
        "run_verified_portfolio",
        fake_run_verified_portfolio,
    )
    monkeypatch.setattr(
        portfolio_cli,
        "export_portfolio_run",
        fake_export,
    )

    with portfolio_cli._safe_project_root(str(frozen_inputs)) as (
        root,
        descriptor,
        metadata,
    ):
        run, relative = portfolio_cli._load_run_arguments(
            None,
            root=root,
            root_descriptor=descriptor,
            root_metadata=metadata,
            gate_e_config=formal_config,
        )

    assert run.identity.run_id == run_id
    assert relative == PurePosixPath(f"outputs/portfolios/{run_id}")
    assert observed["config"].initial_cash_fen == 100_000_000
    assert observed["config"].gross_target_weight == portfolio_cli.Decimal(
        "0.95"
    )
    assert observed["config"].end_date.isoformat() == "2026-07-23"
    assert (
        observed["fee_policy"].policy_digest
        == formal_config.payload["fee_policy_digest"]
    )
    assert not market_lock.exists()
    assert _file_digest_tree(frozen_inputs) == before
