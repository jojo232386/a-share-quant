from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from aquant.backtest import load_verified_snapshot
from aquant.data.calendar_snapshot import CalendarSnapshotStore, load_verified_calendar
from aquant.data.corporate_actions import (
    CorporateActionEvent,
    load_verified_corporate_actions,
    publish_corporate_actions,
)
from aquant.data.manifest import ManifestRecord, ManifestWriter
from aquant.data.snapshot import RawSnapshotStore
from aquant.experiment_cli import main
from aquant.planner import PlannerLimits
from aquant.research.loop import (
    A4_2_INVALID_HANDLING,
    A4_2_RESEARCH_SEMANTICS,
    A4_INSUFFICIENT_EVIDENCE_CRITERIA,
    A4_INVALID_HANDLING,
    A4_PASS_CRITERIA,
    A4_PRIMARY_METRICS,
    A4_RESEARCH_SEMANTICS,
    A4_SECONDARY_METRICS,
    A4_VALIDITY_CRITERIA,
    STRATEGY_ABSOLUTE_MOMENTUM_252,
    STRATEGY_VOLATILITY_REGIME_DEFENSE,
    ResearchLoopConfig,
    ResearchLoopError,
    research_config_payload,
    run_research_loop,
)
from aquant.research.report import build_research_report, publish_research_report
from aquant.rules import InstrumentKind, default_fee_policy
from aquant.universe import (
    UniverseMember,
    canonical_universe_bytes,
    load_verified_universe,
)

SYMBOL = "510300"
KIND = InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF
SESSIONS = tuple(
    session
    for offset in range(13)
    if (session := date(2026, 7, 1) + timedelta(days=offset)).weekday() < 5
)[:9]
MARKET_SESSIONS = SESSIONS[:8]
GIT_HEAD = "a" * 40
PREREGISTRATION_COMMIT = "b" * 40


def _config():
    return ResearchLoopConfig(
        symbol=SYMBOL,
        initial_cash_fen=10_000_000,
        sma_period=2,
        active_weight=Decimal("0.95"),
        limits=PlannerLimits(
            max_single_weight=Decimal("0.95"),
            max_gross=Decimal("0.95"),
            min_cash_ratio=Decimal("0.05"),
        ),
    )


def _a4_config() -> ResearchLoopConfig:
    return ResearchLoopConfig(
        symbol=SYMBOL,
        initial_cash_fen=10_000_000,
        strategy=STRATEGY_VOLATILITY_REGIME_DEFENSE,
        lookback_returns=20,
        annualization=252,
        volatility_threshold=Decimal("0.25"),
        active_weight=Decimal("0.95"),
        limits=PlannerLimits(
            max_single_weight=Decimal("0.95"),
            max_gross=Decimal("0.95"),
            min_cash_ratio=Decimal("0.05"),
        ),
    )


def _a4_2_config() -> ResearchLoopConfig:
    return ResearchLoopConfig(
        symbol=SYMBOL,
        initial_cash_fen=10_000_000,
        strategy=STRATEGY_ABSOLUTE_MOMENTUM_252,
        lookback_sessions=252,
        active_weight=Decimal("0.95"),
        limits=PlannerLimits(
            max_single_weight=Decimal("0.95"),
            max_gross=Decimal("0.95"),
            min_cash_ratio=Decimal("0.05"),
        ),
    )


def _preregistration_content() -> bytes:
    values = {
        "benchmark": "buy_and_hold",
        "evaluation_period": {
            "start": MARKET_SESSIONS[0].isoformat(),
            "end": MARKET_SESSIONS[-1].isoformat(),
        },
        "hypothesis": "SMA signal outperforms buy and hold on the preregistered criteria.",
        "pass_criteria": {
            "strategy_max_drawdown": "<= benchmark_max_drawdown",
            "strategy_sharpe_zero_rate": "> benchmark_sharpe_zero_rate",
            "strategy_total_return": "> benchmark_total_return",
        },
        "primary_metrics": ["total_return", "sharpe_zero_rate", "max_drawdown"],
        "reject_criteria": "otherwise",
        "strategy_parameters": {
            "active_weight": "0.95",
            "initial_cash_fen": 10_000_000,
            "limits": {
                "max_gross": "0.95",
                "max_single_weight": "0.95",
                "min_cash_ratio": "0.05",
            },
            "sma_period": 2,
            "symbol": SYMBOL,
        },
        "universe": [SYMBOL],
    }
    return (json.dumps(values, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _a4_preregistration_content(records) -> bytes:
    config = _a4_config()
    values = {
        "benchmark": "buy_and_hold",
        "evaluation_period": {
            "start": MARKET_SESSIONS[0].isoformat(),
            "end": MARKET_SESSIONS[-1].isoformat(),
        },
        "hypothesis": "High volatility defense may improve risk-adjusted performance.",
        "hypothesis_id": "A4_1_510300_VOLATILITY_REGIME_DEFENSE",
        "input_identities": {
            "calendar_id": records[2].calendar_id,
            "corporate_action_snapshot_id": records[1].snapshot_id,
            "market_snapshot_id": records[0].snapshot_id,
            "universe_id": records[6].universe_id,
        },
        "insufficient_evidence_criteria": list(A4_INSUFFICIENT_EVIDENCE_CRITERIA),
        "invalid_handling": A4_INVALID_HANDLING,
        "pass_criteria": A4_PASS_CRITERIA,
        "primary_metrics": list(A4_PRIMARY_METRICS),
        "reject_criteria": "any_core_threshold_failure",
        "research_semantics": A4_RESEARCH_SEMANTICS,
        "secondary_metrics": list(A4_SECONDARY_METRICS),
        "strategy_parameters": research_config_payload(config),
        "subject": SYMBOL,
        "universe": [SYMBOL],
        "validity_criteria": A4_VALIDITY_CRITERIA,
    }
    return (json.dumps(values, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _a4_2_preregistration_content(records) -> bytes:
    config = _a4_2_config()
    values = {
        "benchmark": "buy_and_hold",
        "evaluation_period": {
            "start": MARKET_SESSIONS[0].isoformat(),
            "end": MARKET_SESSIONS[-1].isoformat(),
        },
        "hypothesis": "Absolute momentum may improve risk-adjusted performance.",
        "hypothesis_id": "A4_2_510300_ABSOLUTE_MOMENTUM_252",
        "input_identities": {
            "calendar_id": records[2].calendar_id,
            "corporate_action_snapshot_id": records[1].snapshot_id,
            "market_snapshot_id": records[0].snapshot_id,
            "universe_id": records[6].universe_id,
        },
        "insufficient_evidence_criteria": list(A4_INSUFFICIENT_EVIDENCE_CRITERIA),
        "invalid_handling": A4_2_INVALID_HANDLING,
        "pass_criteria": A4_PASS_CRITERIA,
        "primary_metrics": list(A4_PRIMARY_METRICS),
        "reject_criteria": "any_core_threshold_failure",
        "research_semantics": A4_2_RESEARCH_SEMANTICS,
        "secondary_metrics": list(A4_SECONDARY_METRICS),
        "strategy_parameters": research_config_payload(config),
        "subject": SYMBOL,
        "universe": [SYMBOL],
        "validity_criteria": A4_VALIDITY_CRITERIA,
    }
    return (json.dumps(values, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _commit_preregistration(root, content: bytes | None = None) -> tuple[str, str]:
    preregistration = root / "configs" / "research" / "hypothesis.json"
    preregistration.parent.mkdir(parents=True)
    preregistration.write_bytes(_preregistration_content() if content is None else content)
    (root / ".gitignore").write_text("/configs/universes/\n/data/\n/outputs/\n")
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.name", "Research Test"), cwd=root, check=True)
    subprocess.run(
        ("git", "config", "user.email", "research-test@example.invalid"),
        cwd=root,
        check=True,
    )
    subprocess.run(
        ("git", "add", ".gitignore", "configs/research/hypothesis.json"),
        cwd=root,
        check=True,
    )
    subprocess.run(("git", "commit", "-qm", "preregister hypothesis"), cwd=root, check=True)
    head = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=root, text=True).strip()
    return "configs/research/hypothesis.json", head


def _fixture(root, *, opens=None):
    closes = (10.0, 10.5, 11.0, 10.4, 9.8, 10.3, 10.8, 10.1)
    open_values = closes if opens is None else opens
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(MARKET_SESSIONS),
            "open": open_values,
            "high": [
                max(open_price, close_price) + 0.2
                for open_price, close_price in zip(open_values, closes, strict=True)
            ],
            "low": [
                min(open_price, close_price) - 0.2
                for open_price, close_price in zip(open_values, closes, strict=True)
            ],
            "close": closes,
            "volume": [1_000_000] * len(closes),
            "amount": [10_000_000.0] * len(closes),
        }
    )
    artifact = RawSnapshotStore(root).write(
        frame,
        symbol=SYMBOL,
        source_slug="synthetic_public_fixture",
        snapshot_date=SESSIONS[-1],
    )
    market_record = ManifestRecord.create(
        schema_version="1.0",
        symbol=SYMBOL,
        instrument_kind=KIND.value,
        provider="synthetic_public_fixture",
        source_function="deterministic_ohlcv_v1",
        source_schema="synthetic_public_fixture",
        endpoint_host="synthetic-public-fixture.invalid",
        provider_symbol=f"fixture-{SYMBOL}",
        fetched_at_utc=datetime(2026, 7, 10, tzinfo=UTC),
        requested_start=MARKET_SESSIONS[0],
        requested_end=MARKET_SESSIONS[-1],
        actual_start=MARKET_SESSIONS[0],
        actual_end=MARKET_SESSIONS[-1],
        row_count=len(frame),
        snapshot_relative_path=artifact.relative_path,
        file_sha256=artifact.sha256,
        adjustment="",
        factor_source=None,
        latest_market_date=MARKET_SESSIONS[-1],
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
    ManifestWriter(root / "data" / "manifests" / "manifest.jsonl").append(market_record)
    action = CorporateActionEvent.create(
        symbol=SYMBOL,
        instrument_kind=KIND,
        announcement_date=SESSIONS[1],
        record_date=SESSIONS[2],
        ex_date=SESSIONS[3],
        payable_date=SESSIONS[5],
        cash_dividend_per_unit=Decimal("0.10"),
        stock_dividend_ratio=Decimal("0"),
        capitalization_ratio=Decimal("0"),
        rights_ratio=Decimal("0"),
        rights_price=None,
        source_schema="synthetic.cash.v1",
        source_url="https://synthetic-public-fixture.invalid/",
    )
    action_record = publish_corporate_actions(
        root,
        (action,),
        symbol=SYMBOL,
        instrument_kind=KIND,
        provider="synthetic",
        source_schema="synthetic.cash.v1",
        normalization_version="cash-only-v1",
        coverage_start=MARKET_SESSIONS[0],
        coverage_end=MARKET_SESSIONS[-1],
    )
    calendar_record = CalendarSnapshotStore(root).write(
        SESSIONS,
        source_provider="synthetic",
        source_function="research_loop_fixture",
        source_version="1",
        fetched_at_utc=datetime(2026, 7, 10, tzinfo=UTC),
    )
    content = canonical_universe_bytes(
        "research-loop-fixture",
        (UniverseMember(SYMBOL, KIND.value),),
    )
    universe_id = hashlib.sha256(content).hexdigest()
    universe_path = root / "configs" / "universes" / f"{universe_id}.json"
    universe_path.parent.mkdir(parents=True)
    universe_path.write_bytes(content)
    return (
        market_record,
        action_record,
        calendar_record,
        load_verified_snapshot(root, market_record),
        load_verified_corporate_actions(root, action_record),
        load_verified_calendar(root, calendar_record),
        load_verified_universe(universe_path, expected_id=universe_id),
    )


def _run(root):
    records = _fixture(root)
    result = run_research_loop(
        git_head=GIT_HEAD,
        preregistration_commit=PREREGISTRATION_COMMIT,
        preregistration_content=_preregistration_content(),
        config=_config(),
        market_data=records[3],
        corporate_actions=records[4],
        calendar=records[5],
        fee_policy=default_fee_policy(),
        universe=records[6],
    )
    return records, result


def test_research_loop_replays_signal_planner_fills_dividends_and_metrics(tmp_path):
    _, result = _run(tmp_path)

    assert result.simulation_start == MARKET_SESSIONS[0]
    assert result.simulation_end == MARKET_SESSIONS[-1]
    assert result.settlement_buffer_session == SESSIONS[-1]
    assert len(result.strategy.ledger.daily_snapshots) == len(MARKET_SESSIONS)
    assert result.strategy.plans[0].targets == {}
    assert result.strategy.fills[0].execution_date == SESSIONS[2]
    assert result.strategy.fills[0].side == "buy"
    assert result.strategy.transaction_count >= 2
    assert result.benchmark.transaction_count == 1
    assert result.strategy.dividends[0].entitled_size > 0
    assert result.strategy.dividends[0].amount_fen > 0
    assert any(item.paid_date == SESSIONS[5] for item in result.strategy.ledger.receivables)
    assert result.strategy.metrics.observation_count == len(MARKET_SESSIONS)
    assert result.strategy.equity_curve[0].equity == 100_000.0


def test_research_loop_and_report_are_repeatable_and_hash_inventoried(tmp_path):
    _, first = _run(tmp_path / "first")
    _, second = _run(tmp_path / "second")

    assert first == second
    first_report = build_research_report(first)
    second_report = build_research_report(second)
    assert first_report == second_report
    directory = publish_research_report(first_report, tmp_path / "reports")
    assert publish_research_report(second_report, tmp_path / "reports") == directory
    manifest = json.loads((directory / "artifact_manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert set(manifest["files"]) == set(first_report.payload)
    assert "不证明 Alpha" in (directory / "report.md").read_text()


def test_unchanged_target_retries_after_price_limit_rejection(tmp_path):
    records = _fixture(
        tmp_path,
        opens=(10.0, 10.5, 12.0, 10.4, 9.8, 10.3, 10.8, 10.1),
    )
    result = run_research_loop(
        git_head=GIT_HEAD,
        preregistration_commit=PREREGISTRATION_COMMIT,
        preregistration_content=_preregistration_content(),
        config=_config(),
        market_data=records[3],
        corporate_actions=records[4],
        calendar=records[5],
        fee_policy=default_fee_policy(),
        universe=records[6],
    )

    assert result.strategy.attempts[0].status.value == "rejected"
    assert result.strategy.attempts[0].execution_session == SESSIONS[2]
    assert result.strategy.fills[0].execution_date == SESSIONS[3]


def test_a4_volatility_strategy_reuses_loop_and_produces_deterministic_artifacts(tmp_path):
    records = _fixture(tmp_path)
    content = _a4_preregistration_content(records)
    arguments = {
        "git_head": GIT_HEAD,
        "preregistration_commit": PREREGISTRATION_COMMIT,
        "preregistration_content": content,
        "config": _a4_config(),
        "market_data": records[3],
        "corporate_actions": records[4],
        "calendar": records[5],
        "fee_policy": default_fee_policy(),
        "universe": records[6],
    }

    first = run_research_loop(**arguments)
    second = run_research_loop(**arguments)
    first_report = build_research_report(first)
    second_report = build_research_report(second)

    assert first == second
    assert first_report == second_report
    assert first.strategy.label == "volatility_regime_defense_planner"
    assert all(not decision.output for decision in first.strategy.decisions)
    assert first_report.assessment == "REJECT"
    run = json.loads(first_report.payload["run.json"])
    assert run["config"] == research_config_payload(_a4_config())
    assert run["preregistration_identity"] == {
        "commit": PREREGISTRATION_COMMIT,
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }
    metrics = json.loads(first_report.payload["metrics.json"])
    assert metrics["assessment"] == "REJECT"
    assert metrics["preregistered_thresholds"]["sharpe_pass"] is False


def test_a4_preregistration_rejects_wrong_frozen_input_identity(tmp_path):
    records = _fixture(tmp_path)
    values = json.loads(_a4_preregistration_content(records))
    values["input_identities"]["market_snapshot_id"] = "0" * 64
    content = (json.dumps(values, sort_keys=True, separators=(",", ":")) + "\n").encode()

    with pytest.raises(ResearchLoopError) as exc:
        run_research_loop(
            git_head=GIT_HEAD,
            preregistration_commit=PREREGISTRATION_COMMIT,
            preregistration_content=content,
            config=_a4_config(),
            market_data=records[3],
            corporate_actions=records[4],
            calendar=records[5],
            fee_policy=default_fee_policy(),
            universe=records[6],
        )
    assert exc.value.code == "preregistration_mismatch"


def test_a4_strategy_parameters_are_frozen_against_rescue():
    config = _a4_config()
    for changes in (
        {"lookback_returns": 10},
        {"annualization": 250},
        {"volatility_threshold": Decimal("0.30")},
        {"active_weight": Decimal("0.90")},
    ):
        with pytest.raises(ResearchLoopError) as exc:
            replace(config, **changes)
        assert exc.value.code == "invalid_config"


def test_a4_2_absolute_momentum_reuses_loop_and_binds_preregistration(tmp_path):
    records = _fixture(tmp_path)
    content = _a4_2_preregistration_content(records)
    arguments = {
        "git_head": GIT_HEAD,
        "preregistration_commit": PREREGISTRATION_COMMIT,
        "preregistration_content": content,
        "config": _a4_2_config(),
        "market_data": records[3],
        "corporate_actions": records[4],
        "calendar": records[5],
        "fee_policy": default_fee_policy(),
        "universe": records[6],
    }

    first = run_research_loop(**arguments)
    second = run_research_loop(**arguments)
    first_report = build_research_report(first)

    assert first == second
    assert first_report == build_research_report(second)
    assert first.strategy.label == "absolute_momentum_252_planner"
    assert all(not decision.output for decision in first.strategy.decisions)
    assert first_report.assessment == "REJECT"
    run = json.loads(first_report.payload["run.json"])
    assert run["config"] == research_config_payload(_a4_2_config())
    assert run["preregistration_identity"] == {
        "commit": PREREGISTRATION_COMMIT,
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }
    assert "A4_2_DECISION" in first_report.payload["report.md"].decode()


def test_a4_2_strategy_parameters_are_frozen_against_rescue():
    config = _a4_2_config()
    for changes in (
        {"lookback_sessions": 200},
        {"active_weight": Decimal("0.90")},
    ):
        with pytest.raises(ResearchLoopError) as exc:
            replace(config, **changes)
        assert exc.value.code == "invalid_config"


def test_research_loop_cli_selects_exact_verified_inputs(tmp_path, capsys):
    records = _fixture(tmp_path)
    preregistration, actual_head = _commit_preregistration(tmp_path)

    exit_code = main(
        [
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
            records[0].snapshot_id,
            "--corporate-action-snapshot-id",
            records[1].snapshot_id,
            "--preregistration",
            preregistration,
            "--symbol",
            SYMBOL,
            "--sma-period",
            "2",
            "--initial-cash-yuan",
            "100000.00",
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert response["status"] == "ok"
    directory = tmp_path / response["research_directory"]
    assert (directory / "metrics.json").is_file()
    run = json.loads((directory / "run.json").read_text())
    assert len(run["git_head"]) == 40
    assert run["git_head"] == actual_head
    assert run["preregistration_identity"] == {
        "commit": actual_head,
        "content_sha256": hashlib.sha256(_preregistration_content()).hexdigest(),
    }


def test_a4_cli_binds_frozen_strategy_parameters_and_inputs(tmp_path, capsys):
    records = _fixture(tmp_path)
    content = _a4_preregistration_content(records)
    preregistration, actual_head = _commit_preregistration(tmp_path, content)

    exit_code = main(
        [
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
            records[0].snapshot_id,
            "--corporate-action-snapshot-id",
            records[1].snapshot_id,
            "--preregistration",
            preregistration,
            "--symbol",
            SYMBOL,
            "--strategy",
            STRATEGY_VOLATILITY_REGIME_DEFENSE,
            "--initial-cash-yuan",
            "100000.00",
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert response["assessment"] == "REJECT"
    directory = tmp_path / response["research_directory"]
    run = json.loads((directory / "run.json").read_text())
    assert run["git_head"] == actual_head
    assert run["config"] == research_config_payload(_a4_config())
    assert run["input_identity"]["market_snapshot_id"] == records[0].snapshot_id


def test_a4_2_cli_binds_frozen_strategy_parameters_and_inputs(tmp_path, capsys):
    records = _fixture(tmp_path)
    content = _a4_2_preregistration_content(records)
    preregistration, actual_head = _commit_preregistration(tmp_path, content)

    exit_code = main(
        [
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
            records[0].snapshot_id,
            "--corporate-action-snapshot-id",
            records[1].snapshot_id,
            "--preregistration",
            preregistration,
            "--symbol",
            SYMBOL,
            "--strategy",
            STRATEGY_ABSOLUTE_MOMENTUM_252,
            "--lookback-sessions",
            "252",
            "--initial-cash-yuan",
            "100000.00",
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert response["assessment"] == "REJECT"
    directory = tmp_path / response["research_directory"]
    run = json.loads((directory / "run.json").read_text())
    assert run["git_head"] == actual_head
    assert run["config"] == research_config_payload(_a4_2_config())
    assert run["input_identity"]["market_snapshot_id"] == records[0].snapshot_id


def test_formal_research_rejects_modified_preregistration_before_artifact(tmp_path, capsys):
    records = _fixture(tmp_path)
    preregistration, _ = _commit_preregistration(tmp_path)
    (tmp_path / preregistration).write_bytes(_preregistration_content() + b"\n")

    exit_code = main(
        [
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
            records[0].snapshot_id,
            "--corporate-action-snapshot-id",
            records[1].snapshot_id,
            "--preregistration",
            preregistration,
            "--symbol",
            SYMBOL,
            "--sma-period",
            "2",
            "--initial-cash-yuan",
            "100000.00",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert json.loads(captured.err)["error_code"] == "repository_not_clean"
    assert not (tmp_path / "outputs" / "research_loop").exists()
