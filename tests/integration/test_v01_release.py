from __future__ import annotations

import os
from pathlib import Path

import pytest

from aquant.release_replay import verify_release

PROJECT_ROOT = Path(__file__).parents[2]


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("AQUANT_RUN_RELEASE_INTEGRATION") != "1",
    reason="full frozen release replay is an explicit acceptance test",
)
def test_rebuilds_complete_v01_release_from_frozen_inputs():
    summary = verify_release(
        project_root=PROJECT_ROOT,
        release_root=PROJECT_ROOT / "release" / "v0.1-research",
        progress=lambda _event: None,
    )

    assert summary.release_name == "v0.1-research"
    assert summary.baseline_run_count == 20
    assert summary.candidate_run_count == 30
    assert summary.replay_row_count == 100
    assert summary.risk_report_id == (
        "aa0c944a959796726bb7d394561fa6caa9524f32b4f86d16fb19354099932607"
    )
    assert summary.week5_experiment_id == (
        "20ace094094275e1c83e2f0338e46e8c42d283293bfb9be60b5b758e322ea6da"
    )
