"""Machine-readable compatibility checks for the PR-0 contraction boundary."""

from __future__ import annotations

import importlib
import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = Path(__file__).with_name("import_contract.json")
FROZEN_MAIN_SHA = "abff692be9587cd25a0d4b95a228851934d98583"
REQUIRED_FIELDS = {
    "schema_version",
    "generated_from_main_sha",
    "protected_modules",
    "top_level_symbols",
    "console_scripts",
    "protected_capabilities",
    "intentional_non_contracts",
}


def _load_contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_manifest_shape_and_frozen_sha() -> None:
    contract = _load_contract()

    assert REQUIRED_FIELDS <= contract.keys()
    assert contract["schema_version"] == "1.0"
    assert contract["generated_from_main_sha"] == FROZEN_MAIN_SHA
    assert contract["top_level_symbols"] == []
    for field in ("protected_modules", "intentional_non_contracts"):
        values = contract[field]
        assert isinstance(values, list)
        assert values
        assert len(values) == len(set(values))

    protected_modules = contract["protected_modules"]
    assert len(protected_modules) == len(set(protected_modules))
    script_names = list(contract["console_scripts"])
    assert len(script_names) == len(set(script_names))


def test_protected_modules_import_without_runtime_tasks() -> None:
    contract = _load_contract()

    for module_name in contract["protected_modules"]:
        imported = importlib.import_module(module_name)
        assert imported.__name__ == module_name


def test_top_level_symbol_contract_is_intentionally_empty() -> None:
    contract = _load_contract()

    importlib.import_module("aquant")
    assert contract["top_level_symbols"] == []


def test_console_scripts_match_pyproject_and_resolve_to_callables() -> None:
    contract = _load_contract()
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project_scripts = pyproject["project"]["scripts"]

    assert contract["console_scripts"] == project_scripts
    assert len(project_scripts) == 7

    for target in project_scripts.values():
        module_name, attribute_name = target.rsplit(":", maxsplit=1)
        module = importlib.import_module(module_name)
        target_object = getattr(module, attribute_name)
        assert callable(target_object)


def test_protected_capability_records_are_repo_relative_and_resolvable() -> None:
    contract = _load_contract()
    protected_modules = set(contract["protected_modules"])
    repo_root = REPO_ROOT.resolve()

    capability_ids = [
        capability["capability_id"]
        for capability in contract["protected_capabilities"]
    ]
    assert len(capability_ids) == len(set(capability_ids))
    assert len(capability_ids) == 5

    for capability in contract["protected_capabilities"]:
        assert capability["modules"]
        assert set(capability["modules"]) <= protected_modules
        assert capability["evidence_paths"]
        for evidence_path in capability["evidence_paths"]:
            relative_path = Path(evidence_path)
            normalized = evidence_path.replace("\\", "/").lower()
            assert not relative_path.is_absolute()
            assert "/users/" not in normalized
            assert "private" not in normalized
            resolved_path = (repo_root / relative_path).resolve()
            assert resolved_path == repo_root or repo_root in resolved_path.parents
            assert resolved_path.exists()
