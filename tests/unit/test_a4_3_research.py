from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from aquant.backtest import load_verified_snapshot
from aquant.data.calendar_snapshot import CalendarSnapshotStore, load_verified_calendar
from aquant.data.corporate_actions import (
    load_verified_corporate_actions,
    publish_corporate_actions,
)
from aquant.data.manifest import ManifestRecord, ManifestWriter
from aquant.data.snapshot import RawSnapshotStore
from aquant.experiment_cli import main
from aquant.planner import PlannerLimits
from aquant.research.loop import (
    A4_3_BENCHMARK,
    A4_3_FORMAL_RUN_CONTROLS,
    A4_3_INVALID_HANDLING,
    A4_3_RESEARCH_SEMANTICS,
    A4_3_SECONDARY_METRICS,
    A4_3_TURNOVER_UNIT,
    A4_3_VALIDITY_CRITERIA,
    A4_INSUFFICIENT_EVIDENCE_CRITERIA,
    A4_PASS_CRITERIA,
    A4_PRIMARY_METRICS,
    STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12,
    ResearchLoopConfig,
    ResearchLoopError,
    research_config_payload,
    run_research_loop,
)
from aquant.research.report import build_research_report
from aquant.rules import InstrumentKind, default_fee_policy
from aquant.universe import (
    UniverseMember,
    canonical_universe_bytes,
    load_verified_universe,
)

SYMBOLS = ("510300", "510500")
KIND = InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF
SESSIONS = (
    date(2018, 1, 31),
    date(2018, 2, 28),
    date(2018, 3, 30),
    date(2018, 4, 27),
    date(2018, 5, 31),
    date(2018, 6, 29),
    date(2018, 7, 31),
    date(2018, 8, 31),
    date(2018, 9, 28),
    date(2018, 10, 31),
    date(2018, 11, 30),
    date(2018, 12, 28),
    date(2019, 1, 31),
    date(2019, 2, 1),
    date(2019, 2, 28),
    date(2019, 3, 1),
    date(2019, 3, 29),
)
GIT_HEAD = "a" * 40
PREREGISTRATION_COMMIT = "b" * 40


def _config() -> ResearchLoopConfig:
    return ResearchLoopConfig(
        symbol="510300",
        secondary_symbol="510500",
        initial_cash_fen=10_000_000,
        strategy=STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12,
        lookback_start_month=2,
        lookback_end_month=12,
        active_weight=Decimal("0.95"),
        limits=PlannerLimits(
            max_single_weight=Decimal("0.95"),
            max_gross=Decimal("0.95"),
            min_cash_ratio=Decimal("0.05"),
        ),
    )


def _market_record(root, symbol: str, closes: tuple[float, ...]) -> ManifestRecord:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(SESSIONS),
            "open": closes,
            "high": [value + 0.2 for value in closes],
            "low": [value - 0.2 for value in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
            "amount": [10_000_000.0] * len(closes),
        }
    )
    artifact = RawSnapshotStore(root).write(
        frame,
        symbol=symbol,
        source_slug="a4_3_synthetic_fixture",
        snapshot_date=SESSIONS[-1],
    )
    record = ManifestRecord.create(
        schema_version="1.0",
        symbol=symbol,
        instrument_kind=KIND.value,
        provider="synthetic_public_fixture",
        source_function="deterministic_ohlcv_v1",
        source_schema="synthetic_public_fixture",
        endpoint_host="synthetic-public-fixture.invalid",
        provider_symbol=f"fixture-{symbol}",
        fetched_at_utc=datetime(2026, 7, 10, tzinfo=UTC),
        requested_start=SESSIONS[0],
        requested_end=SESSIONS[-1],
        actual_start=SESSIONS[0],
        actual_end=SESSIONS[-1],
        row_count=len(frame),
        snapshot_relative_path=artifact.relative_path,
        file_sha256=artifact.sha256,
        adjustment="",
        factor_source=None,
        latest_market_date=SESSIONS[-1],
        akshare_version="synthetic-v1",
        raw_volume_unit="unit",
        volume_multiplier_to_canonical=1,
        full_history_download=True,
        local_date_slice=False,
        quality_issue_counts={
            "duplicate_date": 0,
            "empty_frame": 0,
            "invalid_high": 0,
            "invalid_low": 0,
            "negative_amount": 0,
            "negative_volume": 0,
            "non_finite_numeric": 0,
            "non_positive_price": 0,
            "null": 0,
            "out_of_order_date": 0,
        },
    )
    ManifestWriter(root / "data" / "manifests" / "manifest.jsonl").append(record)
    return record


def _fixture(root):
    closes = {
        "510300": (10.0,) * 11 + (20.0, 11.0, 11.0, 11.0, 11.0, 11.0),
        "510500": (10.0,) * 11 + (15.0, 30.0, 30.0, 30.0, 30.0, 30.0),
    }
    market_records = {
        symbol: _market_record(root, symbol, closes[symbol]) for symbol in SYMBOLS
    }
    action_records = {
        symbol: publish_corporate_actions(
            root,
            (),
            symbol=symbol,
            instrument_kind=KIND,
            provider="synthetic",
            source_schema="synthetic.cash.v1",
            normalization_version="cash-only-v1",
            coverage_start=SESSIONS[0],
            coverage_end=SESSIONS[-1],
        )
        for symbol in SYMBOLS
    }
    calendar_record = CalendarSnapshotStore(root).write(
        SESSIONS,
        source_provider="synthetic",
        source_function="a4_3_research_fixture",
        source_version="1",
        fetched_at_utc=datetime(2026, 7, 10, tzinfo=UTC),
    )
    content = canonical_universe_bytes(
        "a4-3-research-fixture",
        tuple(UniverseMember(symbol, KIND.value) for symbol in SYMBOLS),
    )
    universe_id = hashlib.sha256(content).hexdigest()
    universe_path = root / "configs" / "universes" / f"{universe_id}.json"
    universe_path.parent.mkdir(parents=True)
    universe_path.write_bytes(content)
    return (
        market_records,
        action_records,
        calendar_record,
        {
            symbol: load_verified_snapshot(root, market_records[symbol])
            for symbol in SYMBOLS
        },
        {
            symbol: load_verified_corporate_actions(root, action_records[symbol])
            for symbol in SYMBOLS
        },
        load_verified_calendar(root, calendar_record),
        load_verified_universe(universe_path, expected_id=universe_id),
    )


def _preregistration_content(records) -> bytes:
    market_records, action_records, calendar_record, _, _, _, universe = records
    values = {
        "benchmark": A4_3_BENCHMARK,
        "evaluation_period": {"start": "2019-01-31", "end": "2019-03-01"},
        "first_execution_session": "2019-02-01",
        "first_signal_session": "2019-01-31",
        "formal_run_controls": A4_3_FORMAL_RUN_CONTROLS,
        "hypothesis": "Monthly relative momentum may improve risk-adjusted performance.",
        "hypothesis_id": "A4_3_510300_510500_MONTHLY_RELATIVE_MOMENTUM_2_12",
        "input_identities": {
            "calendar_id": calendar_record.calendar_id,
            "corporate_action_snapshot_ids": {
                symbol: action_records[symbol].snapshot_id for symbol in SYMBOLS
            },
            "market_snapshot_ids": {
                symbol: market_records[symbol].snapshot_id for symbol in SYMBOLS
            },
            "universe_id": universe.universe_id,
        },
        "insufficient_evidence_criteria": list(A4_INSUFFICIENT_EVIDENCE_CRITERIA),
        "invalid_handling": A4_3_INVALID_HANDLING,
        "pass_criteria": A4_PASS_CRITERIA,
        "primary_metrics": list(A4_PRIMARY_METRICS),
        "reject_criteria": "any_core_threshold_failure",
        "research_semantics": A4_3_RESEARCH_SEMANTICS,
        "secondary_metrics": list(A4_3_SECONDARY_METRICS),
        "strategy_parameters": research_config_payload(_config()),
        "subject": "510300,510500",
        "turnover_unit": A4_3_TURNOVER_UNIT,
        "universe": list(SYMBOLS),
        "validity_criteria": A4_3_VALIDITY_CRITERIA,
    }
    return (json.dumps(values, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _run(root):
    records = _fixture(root)
    result = run_research_loop(
        git_head=GIT_HEAD,
        preregistration_commit=PREREGISTRATION_COMMIT,
        preregistration_content=_preregistration_content(records),
        config=_config(),
        market_data=records[3],
        corporate_actions=records[4],
        calendar=records[5],
        fee_policy=default_fee_policy(),
        universe=records[6],
    )
    return records, result


def test_a4_3_two_symbol_rotation_reuses_planner_shared_cash_and_t_plus_one(tmp_path):
    _, result = _run(tmp_path)

    assert result.simulation_start == date(2019, 1, 31)
    assert result.simulation_end == date(2019, 3, 1)
    assert dict(result.strategy.decisions[0].output) == {
        "510300": Decimal("0.95"),
        "510500": Decimal("0"),
    }
    february_decision = next(
        item for item in result.strategy.decisions if item.session == date(2019, 2, 28)
    )
    assert dict(february_decision.output) == {
        "510300": Decimal("0"),
        "510500": Decimal("0.95"),
    }
    assert all(
        not decision.output
        for decision in result.strategy.decisions
        if decision.session in {date(2019, 2, 1), date(2019, 3, 1)}
    )

    rotation = [
        item
        for item in result.strategy.attempts
        if item.execution_session == date(2019, 3, 1)
    ]
    assert [(item.side.value, item.symbol) for item in rotation] == [
        ("sell", "510300"),
        ("buy", "510500"),
    ]
    assert rotation[1].cash_before_fen == rotation[0].cash_after_fen
    first_lot = next(lot for lot in result.strategy.ledger.lots if lot.symbol == "510300")
    assert first_lot.acquired_date == date(2019, 2, 1)
    assert first_lot.available_date == date(2019, 2, 28)


def test_a4_3_static_benchmark_initializes_once_and_never_rebalances(tmp_path):
    _, result = _run(tmp_path)

    assert [(fill.execution_date, fill.side) for fill in result.benchmark.fills] == [
        (date(2019, 2, 1), "buy"),
        (date(2019, 2, 1), "buy"),
    ]
    assert {item.symbol for item in result.benchmark.attempts} == set(SYMBOLS)
    assert all(
        item.execution_session == date(2019, 2, 1)
        for item in result.benchmark.attempts
    )


def test_a4_3_provenance_report_binds_both_symbols(tmp_path):
    records, result = _run(tmp_path)
    run = json.loads(build_research_report(result).payload["run.json"])

    assert run["input_identity"]["market_snapshot_ids"] == {
        symbol: records[0][symbol].snapshot_id for symbol in SYMBOLS
    }
    assert run["input_identity"]["corporate_action_snapshot_ids"] == {
        symbol: records[1][symbol].snapshot_id for symbol in SYMBOLS
    }
    assert run["config"] == research_config_payload(_config())


def test_a4_3_preregistration_is_immutable_and_exact(tmp_path):
    records = _fixture(tmp_path)
    values = json.loads(_preregistration_content(records))
    values["benchmark"]["active_rebalancing"] = True
    modified = (json.dumps(values, sort_keys=True, separators=(",", ":")) + "\n").encode()

    with pytest.raises(ResearchLoopError) as exc:
        run_research_loop(
            git_head=GIT_HEAD,
            preregistration_commit=PREREGISTRATION_COMMIT,
            preregistration_content=modified,
            config=_config(),
            market_data=records[3],
            corporate_actions=records[4],
            calendar=records[5],
            fee_policy=default_fee_policy(),
            universe=records[6],
        )
    assert exc.value.code == "preregistration_mismatch"


def test_a4_3_parameters_are_frozen_against_rescue():
    config = _config()
    for changes in (
        {"secondary_symbol": "600519"},
        {"lookback_start_month": 1},
        {"lookback_end_month": 11},
        {"active_weight": Decimal("0.90")},
    ):
        with pytest.raises(ResearchLoopError) as exc:
            replace(config, **changes)
        assert exc.value.code == "invalid_config"


def test_a4_3_cli_selects_both_exact_verified_inputs(tmp_path, capsys):
    records = _fixture(tmp_path)
    preregistration = tmp_path / "configs" / "research" / "a4_3.json"
    preregistration.parent.mkdir(parents=True)
    preregistration.write_bytes(_preregistration_content(records))
    (tmp_path / ".gitignore").write_text("/data/\n/outputs/\n")
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "config", "user.name", "A4-3 Research Test"),
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.email", "a4-3-test@example.invalid"),
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(("git", "add", ".gitignore", "configs"), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "commit", "-qm", "preregister A4-3 fixture"),
        cwd=tmp_path,
        check=True,
    )

    exit_code = main(
        (
            "research-loop",
            "--project-root",
            str(tmp_path),
            "--data-root",
            str(tmp_path),
            "--universe-id",
            records[6].universe_id,
            "--calendar-id",
            records[2].calendar_id,
            "--snapshot-id",
            records[0]["510300"].snapshot_id,
            "--corporate-action-snapshot-id",
            records[1]["510300"].snapshot_id,
            "--secondary-snapshot-id",
            records[0]["510500"].snapshot_id,
            "--secondary-corporate-action-snapshot-id",
            records[1]["510500"].snapshot_id,
            "--preregistration",
            "configs/research/a4_3.json",
            "--symbol",
            "510300",
            "--secondary-symbol",
            "510500",
            "--strategy",
            STRATEGY_MONTHLY_RELATIVE_MOMENTUM_2_12,
            "--lookback-start-month",
            "2",
            "--lookback-end-month",
            "12",
            "--initial-cash-yuan",
            "100000.00",
            "--active-weight",
            "0.95",
            "--output",
            "outputs/a4_3",
        )
    )

    assert exit_code == 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "ok"
    assert response["research_directory"].startswith("outputs/a4_3/")
