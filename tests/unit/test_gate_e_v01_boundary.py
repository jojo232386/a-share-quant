from pathlib import Path

from aquant.gate_e.versioned_audit import (
    load_v02_audit_profile,
    resolve_v02_audit,
)

PROJECT_ROOT = Path(__file__).parents[2]


def test_v02_research_implementation_resolves_from_trust_history() -> None:
    profile = load_v02_audit_profile(
        PROJECT_ROOT / "configs/audit_profiles/v0.2_gate_e.json"
    )
    bindings = resolve_v02_audit(PROJECT_ROOT, profile)

    assert bindings.audit_target == "577c157235cac50e0ab721a7c845b0f0836aa15b"
    assert bindings.implementation_commit == (
        "ae317a01c5c36a7a59836665917afec4a7377125"
    )
