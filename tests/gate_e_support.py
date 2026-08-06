from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def valid_gate_e_payload(project_root: Path) -> dict[str, object]:
    release_manifest_path = (
        project_root / "release/v0.1-research/release_manifest.json"
    )
    release = json.loads(release_manifest_path.read_text(encoding="utf-8"))
    symbols = sorted(release["market_snapshots"])
    return {
        "calendar_id": release["calendar_id"],
        "corporate_action_manifest": (
            "data/corporate_actions/manifest.jsonl"
        ),
        "corporate_action_snapshots": release[
            "corporate_action_snapshots"
        ],
        "end_date": "2026-07-23",
        "etf_commission_rate": "0.00025",
        "etf_minimum_commission_yuan": "5.00",
        "fee_policy_digest": (
            "6935d9e8727417370a69dd97c021514f5517b4f22107fb89b548145195dfa782"
        ),
        "fee_schema_version": "date-effective-fees-v1",
        "gate": "E",
        "gross_target_weight": "0.95",
        "initial_cash_fen": 100_000_000,
        "input_files": release["input_files"],
        "manifest": "data/manifests/manifest.jsonl",
        "market_snapshots": release["market_snapshots"],
        "max_entry_attempts": 5,
        "output": "outputs/portfolios",
        "portfolio_schema_version": "0.2.0",
        "post_end_validation_date": "2026-07-24",
        "project_name": "a-share-quant",
        "project_version": "0.2.0",
        "python_version": "3.11.15",
        "release_manifest_sha256": _sha256(release_manifest_path),
        "schema_version": "1.0",
        "signal_date": "2018-01-02",
        "stamp_duty_schedule": [
            ["2008-09-19", "0.001"],
            ["2023-08-28", "0.0005"],
        ],
        "stock_commission_rate": "0.00025",
        "stock_minimum_commission_yuan": "5.00",
        "strategy": "buy_and_hold",
        "symbols": symbols,
        "transfer_fee_schedule": [
            ["2015-08-01", "0.00002"],
            ["2022-04-29", "0.00001"],
        ],
        "universe_id": release["universe_id"],
        "uv_lock_sha256": _sha256(project_root / "uv.lock"),
    }
