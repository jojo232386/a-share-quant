from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def test_gate_e_distribution_identity_is_v02():
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    lock = tomllib.loads(
        (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8")
    )
    locked_project = tuple(
        package
        for package in lock["package"]
        if package["name"] == "a-share-quant"
    )

    assert project["project"]["name"] == "a-share-quant"
    assert project["project"]["version"] == "0.2.0"
    assert len(locked_project) == 1
    assert locked_project[0]["version"] == "0.2.0"
