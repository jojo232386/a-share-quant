from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd

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
from aquant.research.loop import ResearchLoopConfig, run_research_loop
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
        config=ResearchLoopConfig(
            symbol=SYMBOL,
            initial_cash_fen=10_000_000,
            sma_period=2,
            active_weight=Decimal("0.95"),
            limits=PlannerLimits(
                max_single_weight=Decimal("0.95"),
                max_gross=Decimal("0.95"),
                min_cash_ratio=Decimal("0.05"),
            ),
        ),
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
        config=ResearchLoopConfig(
            symbol=SYMBOL,
            initial_cash_fen=10_000_000,
            sma_period=2,
            active_weight=Decimal("0.95"),
            limits=PlannerLimits(
                max_single_weight=Decimal("0.95"),
                max_gross=Decimal("0.95"),
                min_cash_ratio=Decimal("0.05"),
            ),
        ),
        market_data=records[3],
        corporate_actions=records[4],
        calendar=records[5],
        fee_policy=default_fee_policy(),
        universe=records[6],
    )

    assert result.strategy.attempts[0].status.value == "rejected"
    assert result.strategy.attempts[0].execution_session == SESSIONS[2]
    assert result.strategy.fills[0].execution_date == SESSIONS[3]


def test_research_loop_cli_selects_exact_verified_inputs(tmp_path, capsys):
    records = _fixture(tmp_path)

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
    assert (tmp_path / response["research_directory"] / "metrics.json").is_file()
