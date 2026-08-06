import hashlib
import json
from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest

import aquant.reporting.risk_report as risk_report_module
from aquant.backtest import (
    BacktestConfig,
    StrategyName,
    export_backtest_result,
    run_synthetic_backtest,
)
from aquant.report_cli import main as report_cli_main
from aquant.reporting.risk_report import (
    RiskReportError,
    build_independent_batch_report,
    load_audited_run_metrics,
    publish_risk_report,
)
from aquant.universe import UniverseMember, canonical_universe_bytes


def market_frame():
    return pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2026-07-13",
                    "2026-07-14",
                    "2026-07-15",
                    "2026-07-16",
                    "2026-07-17",
                    "2026-07-20",
                ]
            ),
            "open": [10, 11, 12, 13, 14, 15],
            "high": [11, 12, 13, 14, 15, 16],
            "low": [9, 10, 11, 12, 13, 14],
            "close": [10.5, 11.5, 12.5, 13.5, 14.5, 15.5],
            "volume": [10_000] * 6,
            "amount": [100_000.0] * 6,
        }
    )


def _bundle(tmp_path, strategy=StrategyName.BUY_AND_HOLD):
    result = run_synthetic_backtest(
        market_frame(),
        config=BacktestConfig(
            strategy=strategy,
            initial_cash=10_000,
            target_weight=Decimal("0.95"),
            sma_period=2 if strategy is StrategyName.SMA else None,
        ),
    )
    return result, export_backtest_result(result, tmp_path)


def test_loads_verified_bundle_and_recomputes_metrics(tmp_path):
    result, directory = _bundle(tmp_path)

    audited = load_audited_run_metrics(directory)

    assert audited.run_id == result.run_id
    assert len(audited.artifact_manifest_sha256) == 64
    assert audited.symbol == "600519"
    assert audited.strategy == "buy_and_hold"
    assert audited.metrics.observation_count == 6
    assert audited.metrics.total_return == pytest.approx(
        result.equity_curve[-1].equity / result.equity_curve[0].equity - 1
    )


def test_report_loader_rejects_payload_tampering_even_if_csv_still_parses(tmp_path):
    _, directory = _bundle(tmp_path)
    equity = directory / "equity.csv"
    equity.write_text(
        equity.read_text(encoding="utf-8").replace("10000.0", "99999.0", 1),
        encoding="utf-8",
    )

    with pytest.raises(RiskReportError, match="SHA-256"):
        load_audited_run_metrics(directory)


def test_manifest_rewrite_does_not_bypass_daily_accounting_recomputation(
    tmp_path,
):
    _, directory = _bundle(tmp_path)
    cash = directory / "cash.csv"
    cash.write_text(
        cash.read_text(encoding="utf-8").replace("10000.0", "9999.0", 1),
        encoding="utf-8",
    )
    manifest_path = directory / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["cash.csv"] = hashlib.sha256(cash.read_bytes()).hexdigest()
    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RiskReportError, match="accounting identity"):
        load_audited_run_metrics(directory)


def test_batch_report_rejects_missing_universe_or_duplicate_strategy_pair(tmp_path):
    _, directory = _bundle(tmp_path)
    audited = load_audited_run_metrics(directory)

    with pytest.raises(RiskReportError, match="universe"):
        build_independent_batch_report(
            (audited,),
            expected_universe_id="a" * 64,
            expected_symbols=("600519",),
        )

    forged = replace(audited, universe_id="a" * 64)
    with pytest.raises(RiskReportError, match="duplicate"):
        build_independent_batch_report(
            (forged, forged),
            expected_universe_id="a" * 64,
            expected_symbols=("600519",),
        )


def test_batch_report_is_deterministic_and_labels_limits_without_claiming_alpha(
    tmp_path,
):
    _, directory = _bundle(tmp_path)
    audited = replace(
        load_audited_run_metrics(directory),
        universe_id="a" * 64,
    )
    _, sma_directory = _bundle(tmp_path, StrategyName.SMA)
    sma = replace(
        load_audited_run_metrics(sma_directory),
        universe_id="a" * 64,
    )

    first = build_independent_batch_report(
        (audited, sma),
        expected_universe_id="a" * 64,
        expected_symbols=("600519",),
        max_drawdown_limit=0.01,
        max_exposure_limit=1.0,
    )
    second = build_independent_batch_report(
        (audited, sma),
        expected_universe_id="a" * 64,
        expected_symbols=("600519",),
        max_drawdown_limit=0.01,
        max_exposure_limit=1.0,
    )

    assert first == second
    assert len(first.report_id) == 64
    payload = json.loads(first.json_bytes)
    assert payload["report_kind"] == "independent_single_instrument_batch"
    assert payload["runs"][0]["interpretation"] in {
        "observed_positive_return_not_validated_alpha",
        "strategy_loss",
        "risk_limit_breach",
    }
    assert "不构成共享现金组合" in first.markdown

    directory = publish_risk_report(first, tmp_path / "reports")
    repeated = publish_risk_report(second, tmp_path / "reports")
    assert directory == repeated
    manifest = json.loads(
        (directory / "artifact_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "complete"
    assert set(manifest["files"]) == {"report.json", "report.md"}


def test_markdown_lists_each_source_artifact_manifest_hash(tmp_path):
    _, buy_directory = _bundle(tmp_path)
    _, sma_directory = _bundle(tmp_path, StrategyName.SMA)
    buy = replace(
        load_audited_run_metrics(buy_directory),
        universe_id="a" * 64,
    )
    sma = replace(
        load_audited_run_metrics(sma_directory),
        universe_id="a" * 64,
    )

    report = build_independent_batch_report(
        (buy, sma),
        expected_universe_id="a" * 64,
        expected_symbols=("600519",),
    )

    assert "## 源回测包验真清单" in report.markdown
    assert buy.artifact_manifest_sha256 in report.markdown
    assert sma.artifact_manifest_sha256 in report.markdown


def _published_report_with_sources(tmp_path):
    buy_result, _ = _bundle(tmp_path)
    sma_result, _ = _bundle(tmp_path, StrategyName.SMA)
    universe_id = "a" * 64
    buy_directory = export_backtest_result(
        replace(
            buy_result,
            run_id=hashlib.sha256(
                f"{buy_result.run_id}\0{universe_id}".encode()
            ).hexdigest(),
            universe_id=universe_id,
        ),
        tmp_path,
    )
    sma_directory = export_backtest_result(
        replace(
            sma_result,
            run_id=hashlib.sha256(
                f"{sma_result.run_id}\0{universe_id}".encode()
            ).hexdigest(),
            universe_id=universe_id,
        ),
        tmp_path,
    )
    buy = load_audited_run_metrics(buy_directory)
    sma = load_audited_run_metrics(sma_directory)
    report = build_independent_batch_report(
        (buy, sma),
        expected_universe_id=universe_id,
        expected_symbols=("600519",),
    )
    report_directory = publish_risk_report(report, tmp_path / "reports")
    return report, report_directory, buy_directory


def test_verifies_published_report_against_current_source_runs(tmp_path):
    report, report_directory, _ = _published_report_with_sources(tmp_path)

    verified = risk_report_module.verify_published_risk_report(
        report_directory,
        tmp_path,
    )

    assert verified.report_id == report.report_id
    assert verified.run_count == 2
    assert verified.universe_id == "a" * 64


def test_published_report_verification_rejects_tampered_source_run(tmp_path):
    _, report_directory, buy_directory = _published_report_with_sources(tmp_path)
    cash = buy_directory / "cash.csv"
    cash.write_text(
        cash.read_text(encoding="utf-8").replace("10000.0", "9999.0", 1),
        encoding="utf-8",
    )

    with pytest.raises(RiskReportError, match="SHA-256"):
        risk_report_module.verify_published_risk_report(
            report_directory,
            tmp_path,
        )


def test_report_verify_cli_rechecks_current_source_runs(tmp_path, capsys):
    report, _, _ = _published_report_with_sources(tmp_path)

    exit_code = report_cli_main(
        [
            "verify",
            "--project-root",
            str(tmp_path),
            "--report-id",
            report.report_id,
            "--backtests",
            ".",
            "--reports",
            "reports",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert payload == {
        "report_id": report.report_id,
        "run_count": 2,
        "status": "verified",
        "universe_id": "a" * 64,
    }


def test_report_cli_builds_one_verified_two_baseline_report(tmp_path, capsys):
    member = UniverseMember("600519", "main_board_stock")
    content = canonical_universe_bytes("pytest", (member,))
    universe_id = hashlib.sha256(content).hexdigest()
    universe_directory = tmp_path / "configs" / "universes"
    universe_directory.mkdir(parents=True)
    (universe_directory / f"{universe_id}.json").write_bytes(content)
    backtest_root = tmp_path / "outputs" / "backtests"
    for strategy in (StrategyName.BUY_AND_HOLD, StrategyName.SMA):
        result = run_synthetic_backtest(
            market_frame(),
            config=BacktestConfig(
                strategy=strategy,
                initial_cash=10_000,
                target_weight=Decimal("0.95"),
                sma_period=2 if strategy is StrategyName.SMA else None,
            ),
        )
        export_backtest_result(
            replace(result, universe_id=universe_id),
            backtest_root,
        )

    exit_code = report_cli_main(
        [
            "build",
            "--project-root",
            str(tmp_path),
            "--universe-id",
            universe_id,
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert payload["run_count"] == 2
    assert payload["universe_id"] == universe_id
    assert (
        tmp_path
        / payload["report_directory"]
        / "artifact_manifest.json"
    ).is_file()
