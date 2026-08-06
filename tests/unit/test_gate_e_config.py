from __future__ import annotations

import copy
import dataclasses
import importlib
import json
import os
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

import aquant.gate_e.config as config_module
from aquant.gate_e.config import (
    GateEConfig,
    GateEConfigError,
    canonical_config_bytes,
    load_gate_e_config,
    verify_gate_e_config,
)

PROJECT_ROOT = Path(__file__).parents[2]
TESTS_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(TESTS_ROOT))
gate_e_support = importlib.import_module("gate_e_support")
sys.path.pop(0)
valid_gate_e_payload = gate_e_support.valid_gate_e_payload


def _valid_payload() -> dict[str, object]:
    return copy.deepcopy(valid_gate_e_payload(PROJECT_ROOT))


def _write_config(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "config.json"
    path.write_bytes(canonical_config_bytes(payload))
    return path


def test_repository_gate_e_config_matches_frozen_release():
    config_path = PROJECT_ROOT / "configs/releases/v0.2_gate_e.json"
    config = load_gate_e_config(config_path)
    release = json.loads(
        (
            PROJECT_ROOT
            / "release/v0.1-research/release_manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert config.config_sha256 == (
        "1794ed454604d77dacdd9bb87b778721afd12cdbe7354f90a6b5d38dadd49935"
    )
    assert config.canonical_bytes == canonical_config_bytes(
        valid_gate_e_payload(PROJECT_ROOT)
    )
    assert config.payload["symbols"] == tuple(
        sorted(release["market_snapshots"])
    )
    assert all(
        type(config.payload[field]) is MappingProxyType
        for field in (
            "market_snapshots",
            "corporate_action_snapshots",
            "input_files",
        )
    )
    assert config.payload["market_snapshots"] == MappingProxyType(
        release["market_snapshots"]
    )
    assert config.payload["corporate_action_snapshots"] == MappingProxyType(
        release["corporate_action_snapshots"]
    )
    assert config.payload["input_files"] == MappingProxyType(
        release["input_files"]
    )


def test_config_bytes_are_canonical_and_preserve_decimal_text(tmp_path):
    payload = _valid_payload()
    path = _write_config(tmp_path, payload)

    config = load_gate_e_config(path)

    assert config.canonical_bytes == canonical_config_bytes(payload)
    assert config.gross_target_weight.as_tuple().exponent == -2
    assert config.payload["stock_minimum_commission_yuan"] == "5.00"
    assert config.payload["etf_minimum_commission_yuan"] == "5.00"
    assert config.to_fee_policy().policy_digest == payload["fee_policy_digest"]


def test_noncanonical_json_bytes_are_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_valid_payload()), encoding="utf-8")

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(path)

    assert captured.value.code == "noncanonical_config"


@pytest.mark.parametrize(
    "field",
    (
        "gross_target_weight",
        "stock_commission_rate",
        "stock_minimum_commission_yuan",
        "etf_commission_rate",
        "etf_minimum_commission_yuan",
    ),
)
def test_decimal_json_numbers_are_rejected(tmp_path, field):
    payload = _valid_payload()
    payload[field] = 0.95

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(_write_config(tmp_path, payload))

    assert captured.value.code == "invalid_decimal_text"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("initial_cash_fen", True),
        ("initial_cash_fen", 0),
        ("max_entry_attempts", True),
        ("max_entry_attempts", 0),
    ),
)
def test_positive_integer_fields_reject_bool_and_zero(tmp_path, field, value):
    payload = _valid_payload()
    payload[field] = value

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(_write_config(tmp_path, payload))

    assert captured.value.code == "invalid_integer"


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        ("missing", "config_key_mismatch"),
        ("unknown", "config_key_mismatch"),
        ("uppercase_hash", "invalid_sha256"),
        ("reversed_symbols", "symbol_contract_mismatch"),
        ("changed_snapshot", "release_closure_mismatch"),
        ("changed_action_snapshot", "release_closure_mismatch"),
        ("changed_input", "release_closure_mismatch"),
        ("unsafe_input_path", "unsafe_path"),
    ),
)
def test_exact_keys_hashes_symbols_and_release_closure(
    tmp_path,
    mutation,
    code,
):
    payload = _valid_payload()
    if mutation == "missing":
        payload.pop("gate")
    elif mutation == "unknown":
        payload["extra"] = "forbidden"
    elif mutation == "uppercase_hash":
        payload["uv_lock_sha256"] = "A" * 64
    elif mutation == "reversed_symbols":
        payload["symbols"] = list(reversed(payload["symbols"]))
    elif mutation == "changed_snapshot":
        payload["market_snapshots"]["000001"] = "0" * 64
    elif mutation == "changed_action_snapshot":
        payload["corporate_action_snapshots"]["000001"] = "0" * 64
    elif mutation == "changed_input":
        first = sorted(payload["input_files"])[0]
        payload["input_files"][first] = "0" * 64
    elif mutation == "unsafe_input_path":
        first = sorted(payload["input_files"])[0]
        digest = payload["input_files"].pop(first)
        payload["input_files"]["../outside"] = digest

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(_write_config(tmp_path, payload))

    assert captured.value.code == code


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("manifest", "/tmp/manifest.jsonl"),
        ("corporate_action_manifest", "../manifest.jsonl"),
        ("output", r"outputs\portfolios"),
    ),
)
def test_paths_must_be_fixed_safe_relative_paths(tmp_path, field, value):
    payload = _valid_payload()
    payload[field] = value

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(_write_config(tmp_path, payload))

    assert captured.value.code in {"unsafe_path", "unexpected_config"}


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("project_name", "another-project"),
        ("project_version", "0.2.1"),
        ("gate", "D"),
        ("strategy", "sma20"),
        ("schema_version", "2.0"),
        ("portfolio_schema_version", "0.1.0"),
        ("fee_schema_version", "fees-v2"),
        ("python_version", "3.12.0"),
        ("signal_date", "2018-01-03"),
        ("end_date", "2026-07-24"),
        ("post_end_validation_date", "2026-07-25"),
        ("gross_target_weight", "0.950"),
        ("stock_minimum_commission_yuan", "5.0"),
        ("initial_cash_fen", 99_999_999),
        ("max_entry_attempts", 4),
        ("manifest", "data/manifests/other.jsonl"),
        (
            "corporate_action_manifest",
            "data/corporate_actions/other.jsonl",
        ),
        ("output", "outputs/other"),
        ("calendar_id", "0" * 64),
        ("universe_id", "0" * 64),
        ("release_manifest_sha256", "0" * 64),
        ("uv_lock_sha256", "0" * 64),
    ),
)
def test_fixed_release_contract_cannot_drift(tmp_path, field, value):
    payload = _valid_payload()
    payload[field] = value

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(_write_config(tmp_path, payload))

    assert captured.value.code in {
        "unexpected_config",
        "fee_policy_digest_mismatch",
    }


@pytest.mark.parametrize(
    "field",
    ("stamp_duty_schedule", "transfer_fee_schedule"),
)
def test_statutory_schedules_reject_numbers_and_changes(tmp_path, field):
    payload = _valid_payload()
    payload[field][0][1] = 0.001

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(_write_config(tmp_path, payload))

    assert captured.value.code == "invalid_decimal_text"


def test_fee_policy_digest_is_recomputed(tmp_path):
    payload = _valid_payload()
    payload["stock_commission_rate"] = "0.00026"

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(_write_config(tmp_path, payload))

    assert captured.value.code == "fee_policy_digest_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("stock_commission_rate", "0.00026"),
        ("stock_minimum_commission_yuan", "5.01"),
        ("etf_commission_rate", "0.00026"),
        ("etf_minimum_commission_yuan", "5.01"),
    ),
)
def test_every_commission_string_is_digest_bound(tmp_path, field, value):
    payload = _valid_payload()
    payload[field] = value

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(_write_config(tmp_path, payload))

    assert captured.value.code == "fee_policy_digest_mismatch"


@pytest.mark.parametrize(
    ("field", "mutation"),
    (
        ("stamp_duty_schedule", "changed"),
        ("stamp_duty_schedule", "duplicate"),
        ("stamp_duty_schedule", "reversed"),
        ("transfer_fee_schedule", "changed"),
        ("transfer_fee_schedule", "duplicate"),
        ("transfer_fee_schedule", "reversed"),
    ),
)
def test_statutory_schedule_order_and_values_are_exact(
    tmp_path,
    field,
    mutation,
):
    payload = _valid_payload()
    schedule = payload[field]
    if mutation == "changed":
        schedule[0][1] = "0.00099"
    elif mutation == "duplicate":
        schedule[1][0] = schedule[0][0]
    elif mutation == "reversed":
        schedule.reverse()

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(_write_config(tmp_path, payload))

    assert captured.value.code in {
        "fee_policy_digest_mismatch",
        "unexpected_config",
    }


def test_validated_payload_is_deeply_immutable(tmp_path):
    config = load_gate_e_config(_write_config(tmp_path, _valid_payload()))

    with pytest.raises(TypeError):
        config.payload["market_snapshots"]["000001"] = "0" * 64

    with pytest.raises(TypeError):
        config.payload["input_files"]["new"] = "0" * 64

    with pytest.raises(TypeError):
        config.payload["symbols"][0] = "000000"

    with pytest.raises(TypeError):
        config.payload["stamp_duty_schedule"][0][1] = "0"

    with pytest.raises(TypeError):
        config.payload["transfer_fee_schedule"][0][1] = "0"


def test_portfolio_namespace_preserves_exact_strings_and_sort_order(tmp_path):
    config = load_gate_e_config(_write_config(tmp_path, _valid_payload()))

    namespace = config.to_portfolio_namespace(project_root=".")

    assert namespace.command == "run-config"
    assert namespace.project_root == "."
    assert namespace.output == "outputs/portfolios"
    assert namespace.initial_cash_fen == "100000000"
    assert namespace.gross_target_weight == "0.95"
    assert namespace.stock_minimum_commission == "5.00"
    assert namespace.etf_minimum_commission == "5.00"
    assert namespace.market_snapshot == tuple(sorted(namespace.market_snapshot))
    assert namespace.corporate_action_snapshot == tuple(
        sorted(namespace.corporate_action_snapshot)
    )
    assert len(namespace.market_snapshot) == 10
    assert len(namespace.corporate_action_snapshot) == 10
    assert not hasattr(namespace, "post_end_validation_date")
    assert all(
        value != "2026-07-24"
        for value in vars(namespace).values()
    )

    with pytest.raises(TypeError):
        namespace.market_snapshot[0] = "000001=" + "0" * 64

    with pytest.raises(TypeError):
        namespace.corporate_action_snapshot[0] = "000001=" + "0" * 64


def test_symlinked_config_is_rejected(tmp_path):
    real = _write_config(tmp_path, _valid_payload())
    link = tmp_path / "linked.json"
    link.symlink_to(real)

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(link)

    assert captured.value.code == "unsafe_config_file"


def test_config_with_symlinked_ancestor_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    real = _write_config(outside, _valid_payload())
    project = tmp_path / "project"
    project.mkdir()
    (project / "configs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(project / "configs" / real.name)

    assert captured.value.code == "unsafe_config_file"


def test_config_final_component_is_opened_nonblocking(
    tmp_path,
    monkeypatch,
):
    path = _write_config(tmp_path, _valid_payload())
    original_open = config_module.os.open
    final_flags = []

    def observing_open(value, flags, *args, **kwargs):
        if value == path.name:
            final_flags.append(flags)
        return original_open(value, flags, *args, **kwargs)

    monkeypatch.setattr(config_module.os, "open", observing_open)

    load_gate_e_config(path)

    assert final_flags
    assert final_flags[-1] & getattr(os, "O_NONBLOCK", 0)


def test_config_candidate_descriptor_closes_when_fstat_fails(
    tmp_path,
    monkeypatch,
):
    path = _write_config(tmp_path, _valid_payload())
    original_open = config_module.os.open
    original_close = config_module.os.close
    original_fstat = config_module.os.fstat
    opened = []
    closed = []
    failed = False

    def tracking_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracking_close(descriptor):
        if descriptor in opened:
            closed.append(descriptor)
        return original_close(descriptor)

    def failing_fstat(descriptor):
        nonlocal failed
        if not failed and descriptor in opened:
            failed = True
            raise OSError("injected fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(config_module.os, "open", tracking_open)
    monkeypatch.setattr(config_module.os, "close", tracking_close)
    monkeypatch.setattr(config_module.os, "fstat", failing_fstat)

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(path)

    assert captured.value.code == "unsafe_config_file"
    assert failed is True
    assert sorted(opened) == sorted(closed)


def test_hard_linked_config_is_rejected(tmp_path):
    real = _write_config(tmp_path, _valid_payload())
    linked = tmp_path / "hard-linked.json"
    linked.hardlink_to(real)

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(real)

    assert captured.value.code == "unsafe_config_file"


def test_duplicate_json_key_is_rejected_as_noncanonical(tmp_path):
    raw = canonical_config_bytes(_valid_payload())
    duplicate = raw.replace(
        b'{"calendar_id":',
        b'{"calendar_id":"0","calendar_id":',
        1,
    )
    path = tmp_path / "duplicate.json"
    path.write_bytes(duplicate)

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(path)

    assert captured.value.code == "noncanonical_config"


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_nonfinite_json_constants_have_stable_errors(tmp_path, constant):
    raw = canonical_config_bytes(_valid_payload()).replace(
        b'"gross_target_weight":"0.95"',
        f'"gross_target_weight":{constant}'.encode(),
        1,
    )
    path = tmp_path / "nonfinite.json"
    path.write_bytes(raw)

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(path)

    assert captured.value.code == "invalid_config"


def test_unpaired_unicode_surrogate_has_stable_error():
    payload = _valid_payload()
    payload["output"] = "\ud800"

    with pytest.raises(GateEConfigError) as captured:
        canonical_config_bytes(payload)

    assert captured.value.code == "invalid_config"


def test_unpaired_unicode_surrogate_in_file_has_stable_error(tmp_path):
    raw = canonical_config_bytes(_valid_payload()).replace(
        b'"output":"outputs/portfolios"',
        b'"output":"\\ud800"',
        1,
    )
    path = tmp_path / "surrogate.json"
    path.write_bytes(raw)

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(path)

    assert captured.value.code == "invalid_config"


def test_config_binding_is_rechecked_after_read(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, _valid_payload())
    hard_link = tmp_path / "created-during-read.json"
    original_read = config_module.os.read
    raced = False

    def racing_read(descriptor, size):
        nonlocal raced
        if not raced:
            hard_link.hardlink_to(config_path)
            raced = True
        return original_read(descriptor, size)

    monkeypatch.setattr(config_module.os, "read", racing_read)

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(config_path)

    assert captured.value.code == "unsafe_config_file"


def test_unregistered_config_objects_cannot_project():
    forged = GateEConfig()

    with pytest.raises(GateEConfigError) as captured:
        forged.to_portfolio_namespace(project_root=".")

    assert captured.value.code == "unverified_config"


def test_object_new_and_subclass_cannot_bypass_registration():
    blank = object.__new__(GateEConfig)

    with pytest.raises(GateEConfigError) as captured:
        verify_gate_e_config(blank)

    assert captured.value.code == "unverified_config"

    class ForgedGateEConfig(GateEConfig):
        pass

    forged = object.__new__(ForgedGateEConfig)
    with pytest.raises(TypeError):
        verify_gate_e_config(forged)


def test_replace_and_object_setattr_cannot_mutate_verified_config(tmp_path):
    config = load_gate_e_config(_write_config(tmp_path, _valid_payload()))

    with pytest.raises((TypeError, ValueError)):
        dataclasses.replace(config)

    changed = dict(config.payload)
    changed["output"] = "../../outside"
    object.__setattr__(config, "payload", MappingProxyType(changed))

    with pytest.raises(GateEConfigError) as captured:
        config.to_portfolio_namespace(project_root=".")

    assert captured.value.code == "verified_config_modified"
