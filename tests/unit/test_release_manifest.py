from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from aquant.release_manifest import (
    ReleaseVerificationError,
    load_release_manifest,
    verify_release_inputs,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _payload(file_sha256: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "release_name": "v0.1-research",
        "implementation_commit": "1" * 40,
        "python_version": "3.11.15",
        "akshare_version": "1.18.64",
        "backtrader_version": "1.9.78.123",
        "universe_id": HASH_A,
        "calendar_id": HASH_B,
        "market_snapshots": {"600519": HASH_A},
        "corporate_action_snapshots": {"600519": HASH_C},
        "input_files": {"data/raw/600519.parquet": file_sha256},
        "baseline_run_ids": {
            "600519|buy_and_hold": HASH_A,
            "600519|sma20": HASH_B,
        },
        "candidate_run_ids": {
            "600519|sma10": HASH_A,
            "600519|sma20": HASH_B,
            "600519|sma60": HASH_C,
        },
        "risk_report_id": HASH_A,
        "week5_experiment_id": HASH_B,
        "expected_counts": {
            "symbols": 1,
            "baseline_runs": 2,
            "candidate_runs": 3,
            "replay_rows": 10,
        },
        "research_boundary": {
            "live_trading": False,
            "profit_claim": False,
            "research_only": True,
            "simulation_only": True,
        },
    }


def _write_manifest(root: Path, payload: dict[str, object]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "release_manifest.json"
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    release_root = tmp_path / "release"
    input_path = release_root / "inputs" / "data" / "raw" / "600519.parquet"
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"verified fixture")
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    return release_root, _payload(digest)


def _error_code(path: Path) -> str:
    with pytest.raises(ReleaseVerificationError) as error:
        load_release_manifest(path)
    return error.value.code


def test_loads_canonical_release_manifest_and_sorts_mappings(tmp_path):
    release_root, payload = _fixture(tmp_path)
    manifest = load_release_manifest(_write_manifest(release_root, payload))

    assert manifest.release_name == "v0.1-research"
    assert manifest.symbols == ("600519",)
    assert manifest.input_files == (
        (
            "data/raw/600519.parquet",
            payload["input_files"]["data/raw/600519.parquet"],
        ),
    )


def test_rejects_duplicate_json_keys_before_dictionary_construction(tmp_path):
    path = tmp_path / "release_manifest.json"
    path.write_text(
        '{"schema_version":"1.0","schema_version":"1.0"}\n',
        encoding="utf-8",
    )

    assert _error_code(path) == "duplicate_manifest_key"


def test_rejects_unknown_or_missing_manifest_fields(tmp_path):
    release_root, payload = _fixture(tmp_path)
    payload["surprise"] = True
    assert _error_code(_write_manifest(release_root, payload)) == (
        "manifest_schema_invalid"
    )

    payload = _payload(HASH_A)
    del payload["calendar_id"]
    assert _error_code(_write_manifest(release_root, payload)) == (
        "manifest_schema_invalid"
    )


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "/tmp/value",
        "../outside",
        "data/../outside",
        "./data/raw/value",
        "data//raw/value",
        "data\\raw\\value",
        "",
    ),
)
def test_rejects_unsafe_input_paths(tmp_path, unsafe_path):
    release_root, payload = _fixture(tmp_path)
    payload["input_files"] = {unsafe_path: HASH_A}

    assert _error_code(_write_manifest(release_root, payload)) == (
        "unsafe_input_path"
    )


def test_rejects_uppercase_or_malformed_hashes(tmp_path):
    release_root, payload = _fixture(tmp_path)
    payload["calendar_id"] = "A" * 64
    assert _error_code(_write_manifest(release_root, payload)) == (
        "manifest_schema_invalid"
    )

    payload = _payload(HASH_A)
    payload["risk_report_id"] = "a" * 63
    assert _error_code(_write_manifest(release_root, payload)) == (
        "manifest_schema_invalid"
    )


def test_rejects_symbol_and_expected_count_mismatches(tmp_path):
    release_root, payload = _fixture(tmp_path)
    payload["corporate_action_snapshots"] = {"000001": HASH_C}
    assert _error_code(_write_manifest(release_root, payload)) == (
        "manifest_schema_invalid"
    )

    payload = _payload(HASH_A)
    payload["expected_counts"]["baseline_runs"] = 3
    assert _error_code(_write_manifest(release_root, payload)) == (
        "manifest_schema_invalid"
    )


def test_verifies_exact_regular_single_link_input_set(tmp_path):
    release_root, payload = _fixture(tmp_path)
    manifest = load_release_manifest(_write_manifest(release_root, payload))

    verified = verify_release_inputs(manifest, release_root)

    assert verified == (
        release_root / "inputs" / "data" / "raw" / "600519.parquet",
    )


def test_rejects_changed_missing_and_extra_input_files(tmp_path):
    release_root, payload = _fixture(tmp_path)
    manifest = load_release_manifest(_write_manifest(release_root, payload))
    input_path = release_root / "inputs" / "data" / "raw" / "600519.parquet"

    input_path.write_bytes(b"changed")
    with pytest.raises(ReleaseVerificationError) as error:
        verify_release_inputs(manifest, release_root)
    assert error.value.code == "input_hash_mismatch"

    input_path.unlink()
    with pytest.raises(ReleaseVerificationError) as error:
        verify_release_inputs(manifest, release_root)
    assert error.value.code == "input_file_set_mismatch"

    input_path.write_bytes(b"verified fixture")
    extra = release_root / "inputs" / "data" / "raw" / "extra.parquet"
    extra.write_bytes(b"extra")
    with pytest.raises(ReleaseVerificationError) as error:
        verify_release_inputs(manifest, release_root)
    assert error.value.code == "input_file_set_mismatch"


def test_rejects_soft_links_in_any_input_component(tmp_path):
    release_root, payload = _fixture(tmp_path)
    real_raw = release_root / "inputs" / "data" / "real_raw"
    raw = release_root / "inputs" / "data" / "raw"
    raw.rename(real_raw)
    raw.symlink_to(real_raw, target_is_directory=True)
    manifest = load_release_manifest(_write_manifest(release_root, payload))

    with pytest.raises(ReleaseVerificationError) as error:
        verify_release_inputs(manifest, release_root)

    assert error.value.code == "unsafe_input_link"


def test_rejects_hard_linked_input_files(tmp_path):
    release_root, payload = _fixture(tmp_path)
    input_path = release_root / "inputs" / "data" / "raw" / "600519.parquet"
    linked_path = input_path.with_name("linked.parquet")
    os.link(input_path, linked_path)
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    payload["input_files"] = {
        "data/raw/600519.parquet": digest,
        "data/raw/linked.parquet": digest,
    }
    manifest = load_release_manifest(_write_manifest(release_root, payload))

    with pytest.raises(ReleaseVerificationError) as error:
        verify_release_inputs(manifest, release_root)

    assert error.value.code == "unsafe_input_link"
