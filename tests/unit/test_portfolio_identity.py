from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import weakref
from dataclasses import fields, make_dataclass, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath

import pytest

import aquant.portfolio.identity as identity_module
from aquant.portfolio import (
    PORTFOLIO_ARTIFACT_FILES,
    AvailabilityAudit,
    CashLedgerEvent,
    CashReceivable,
    DailyAccountSnapshot,
    DividendAudit,
    EntryAttempt,
    EntryTarget,
    PortfolioBacktestResult,
    PortfolioConfig,
    PortfolioError,
    PortfolioLedger,
    SymbolValuation,
    TargetAllocation,
    VerifiedPortfolioRun,
    run_verified_portfolio,
    verify_portfolio_run,
)
from aquant.rules import FeeBreakdown, FeeRateTouch, PositionLot
from aquant.universe import UniverseMember

PROJECT_ROOT = Path(__file__).parents[2]
TESTS_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(TESTS_ROOT))
gate_c_support = importlib.import_module("portfolio_gate_c_support")
sys.path.pop(0)
OFFICIAL_DATES = gate_c_support.OFFICIAL_DATES
make_portfolio_case = gate_c_support.make_portfolio_case
EXPECTED_TASK_5_IMPLEMENTATION_FILES = (
    "pyproject.toml",
    "src/aquant/backtest/data_access.py",
    "src/aquant/backtest/feed.py",
    "src/aquant/data/calendar_snapshot.py",
    "src/aquant/data/corporate_actions.py",
    "src/aquant/data/manifest.py",
    "src/aquant/gate_e/__init__.py",
    "src/aquant/gate_e/config.py",
    "src/aquant/gate_e/frozen_manifest.py",
    "src/aquant/portfolio/__init__.py",
    "src/aquant/portfolio/accounting.py",
    "src/aquant/portfolio/availability.py",
    "src/aquant/portfolio/contracts.py",
    "src/aquant/portfolio/coordinator.py",
    "src/aquant/portfolio/export.py",
    "src/aquant/portfolio/identity.py",
    "src/aquant/portfolio/metrics.py",
    "src/aquant/portfolio/models.py",
    "src/aquant/portfolio/verify.py",
    "src/aquant/portfolio_cli.py",
    "src/aquant/release_network.py",
    "src/aquant/rules/__init__.py",
    "src/aquant/rules/engine.py",
    "src/aquant/rules/fees.py",
    "src/aquant/rules/lots.py",
    "src/aquant/rules/models.py",
    "src/aquant/rules/price_limits.py",
    "src/aquant/universe.py",
)


def test_semantic_result_schema_tracks_every_persisted_dataclass_field():
    expected = {
        PortfolioBacktestResult: (
            "config",
            "allocation",
            "targets",
            "attempts",
            "dividends",
            "availability",
            "ledger",
        ),
        PortfolioConfig: (
            "strategy",
            "initial_cash_fen",
            "gross_target_weight",
            "signal_date",
            "end_date",
            "max_entry_attempts",
        ),
        TargetAllocation: (
            "member_count",
            "gross_target_notional_fen",
            "per_symbol_target_notional_fen",
            "planned_cash_reserve_fen",
            "allocation_rounding_remainder_fen",
        ),
        EntryTarget: (
            "target_id",
            "symbol",
            "signal_date",
            "target_notional_fen",
            "attempts_used",
            "status",
            "fill_event_id",
        ),
        EntryAttempt: (
            "attempt_id",
            "target_id",
            "symbol",
            "original_signal_date",
            "intent_session",
            "execution_session",
            "attempt_number",
            "initial_candidate_size",
            "requested_size",
            "availability_status",
            "status",
            "rejection_reason",
            "fees",
            "fill_event_id",
            "cash_available_before_fen",
            "initial_candidate_cash_required_fen",
            "requested_cash_required_fen",
            "quantity_adjustment_reason",
        ),
        FeeBreakdown: (
            "commission_fen",
            "stamp_duty_fen",
            "transfer_fee_fen",
            "touched_rates",
        ),
        FeeRateTouch: (
            "fee_name",
            "effective_date",
            "rate",
            "minimum_yuan",
        ),
        DividendAudit: (
            "event_id",
            "symbol",
            "ex_date",
            "source_payable_date",
            "actual_cash_date",
            "entitled_size",
            "cash_dividend_per_unit",
            "amount_fen",
        ),
        AvailabilityAudit: (
            "session",
            "symbol",
            "status",
            "mark_price",
            "carried_sessions",
            "adjustment_reason",
        ),
        PortfolioLedger: (
            "initial_cash_fen",
            "cash_fen",
            "lots",
            "cash_events",
            "receivables",
            "daily_snapshots",
        ),
        PositionLot: (
            "lot_id",
            "symbol",
            "acquired_date",
            "available_date",
            "original_size",
            "remaining_size",
            "unit_cost",
        ),
        CashLedgerEvent: (
            "event_id",
            "event_kind",
            "session",
            "side",
            "symbol",
            "reference_id",
            "notional_fen",
            "commission_fen",
            "stamp_duty_fen",
            "transfer_fee_fen",
            "cash_before_fen",
            "cash_after_fen",
        ),
        CashReceivable: (
            "event_id",
            "symbol",
            "registered_date",
            "source_payable_date",
            "actual_cash_date",
            "amount_fen",
            "paid_date",
        ),
        DailyAccountSnapshot: (
            "session",
            "cash_fen",
            "position_market_value_fen",
            "receivable_fen",
            "equity_fen",
            "valuations",
        ),
        SymbolValuation: (
            "symbol",
            "total_size",
            "available_size",
            "locked_size",
            "mark_price",
            "market_value_fen",
        ),
    }

    assert {model: tuple(item.name for item in fields(model)) for model in expected} == expected


def test_identity_ignores_tuple_order_clock_pid_and_temporary_root(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("time.time", lambda: 1.0)
    monkeypatch.setattr("os.getpid", lambda: 11)
    first = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "first",
            symbols=("600001", "600000"),
        )
    )

    monkeypatch.setattr("time.time", lambda: 9_999_999.0)
    monkeypatch.setattr("os.getpid", lambda: 99_999)
    second = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "second",
            symbols=("600000", "600001"),
        )
    )

    assert first.identity.run_id == second.identity.run_id
    assert first.identity.implementation_digest == second.identity.implementation_digest
    assert first.identity.input_closure_digest == second.identity.input_closure_digest
    assert first.result == second.result


def test_run_id_binds_the_canonical_result_digest(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    expected_result_digest = identity_module._semantic_result_digest(run.result)
    expected_run_id = hashlib.sha256(
        identity_module._canonical_json_bytes(
            {
                "engine": identity_module.PORTFOLIO_ENGINE,
                "implementation_digest": (run.identity.implementation_digest),
                "input_closure_digest": (run.identity.input_closure_digest),
                "result_digest": expected_result_digest,
                "schema_version": identity_module.PORTFOLIO_SCHEMA_VERSION,
            }
        )
    ).hexdigest()

    assert run.identity.result_digest == expected_result_digest
    assert run.identity.run_id == expected_run_id


def test_semantic_result_digest_normalizes_order_and_decimal_scale(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    result = run.result

    def scaled(value: Decimal) -> Decimal:
        return Decimal(f"{value}0")

    ledger = replace(
        result.ledger,
        lots=tuple(
            replace(item, unit_cost=scaled(item.unit_cost)) for item in reversed(result.ledger.lots)
        ),
        cash_events=tuple(reversed(result.ledger.cash_events)),
        receivables=tuple(reversed(result.ledger.receivables)),
        daily_snapshots=tuple(
            replace(
                item,
                valuations=tuple(
                    replace(
                        value,
                        mark_price=scaled(value.mark_price),
                    )
                    for value in reversed(item.valuations)
                ),
            )
            for item in reversed(result.ledger.daily_snapshots)
        ),
    )
    equivalent = replace(
        result,
        targets=tuple(reversed(result.targets)),
        attempts=tuple(reversed(result.attempts)),
        dividends=tuple(reversed(result.dividends)),
        availability=tuple(
            replace(
                item,
                mark_price=scaled(item.mark_price),
            )
            for item in reversed(result.availability)
        ),
        ledger=ledger,
    )

    assert identity_module._result_digest(equivalent) != (identity_module._result_digest(result))
    assert identity_module._semantic_result_digest(equivalent) == (
        identity_module._semantic_result_digest(result)
    )


def test_each_valid_bound_input_changes_run_identity(tmp_path):
    baseline = run_verified_portfolio(**make_portfolio_case(tmp_path / "base"))
    changed_cases = (
        make_portfolio_case(tmp_path / "cash", initial_cash_fen=2_000_100),
        make_portfolio_case(
            tmp_path / "calendar",
            calendar_dates=(*OFFICIAL_DATES, date(2026, 7, 21)),
        ),
        make_portfolio_case(
            tmp_path / "market",
            final_close=Decimal("10.01"),
        ),
        make_portfolio_case(
            tmp_path / "actions",
            action_coverage_start=date(2026, 7, 15),
        ),
        make_portfolio_case(
            tmp_path / "fees",
            stock_commission_rate=Decimal("0.00030"),
        ),
        make_portfolio_case(
            tmp_path / "universe",
            symbols=("600000", "600002"),
        ),
    )

    changed_runs = tuple(run_verified_portfolio(**case) for case in changed_cases)
    assert all(item.identity.run_id != baseline.identity.run_id for item in changed_runs)


@pytest.mark.parametrize(
    "constant_name",
    (
        "PRICE_STREAM_VERSION",
        "DIVIDEND_TAX_MODE",
        "NO_BAR_VALUATION_MODE",
        "BUDGET_MODE",
        "RETRY_MODE",
    ),
)
def test_behavior_mode_changes_run_identity(monkeypatch, tmp_path, constant_name):
    baseline = run_verified_portfolio(**make_portfolio_case(tmp_path / "base"))
    monkeypatch.setattr(identity_module, constant_name, f"changed-{constant_name}")
    changed = run_verified_portfolio(**make_portfolio_case(tmp_path / "changed"))
    assert changed.identity.run_id != baseline.identity.run_id


def test_input_change_during_run_fails_closed(monkeypatch, tmp_path):
    case = make_portfolio_case(tmp_path)
    original = identity_module._read_implementation_file
    mutated = False

    def mutate_config_during_fingerprint(filename: str) -> bytes:
        nonlocal mutated
        if not mutated:
            object.__setattr__(
                case["config"],
                "initial_cash_fen",
                2_000_100,
            )
            mutated = True
        return original(filename)

    monkeypatch.setattr(
        identity_module,
        "_read_implementation_file",
        mutate_config_during_fingerprint,
    )
    with pytest.raises(PortfolioError) as captured:
        run_verified_portfolio(**case)
    assert captured.value.code == "portfolio_inputs_changed_during_run"


def test_implementation_change_during_run_fails_closed(monkeypatch, tmp_path):
    original = identity_module._read_implementation_file
    call_count = 0
    file_count = len(identity_module._IMPLEMENTATION_FILES)

    def unstable_source(filename: str) -> bytes:
        nonlocal call_count
        call_count += 1
        content = original(filename)
        if call_count > file_count and filename == identity_module._IMPLEMENTATION_FILES[0]:
            return content + b"x"
        return content

    monkeypatch.setattr(
        identity_module,
        "_read_implementation_file",
        unstable_source,
    )
    with pytest.raises(PortfolioError) as captured:
        run_verified_portfolio(**make_portfolio_case(tmp_path))
    assert captured.value.code == "implementation_changed_during_run"


def test_input_closure_is_complete_canonical_json(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    closure = json.loads(run.identity.input_closure_json)

    assert run.identity.input_closure_json.endswith(b"\n")
    assert set(closure) == {
        "behavior_modes",
        "calendar",
        "config",
        "corporate_actions",
        "fee_policy",
        "market_data",
        "universe",
    }
    assert [item["symbol"] for item in closure["market_data"]] == [
        "600000",
        "600001",
    ]
    assert [item["symbol"] for item in closure["corporate_actions"]] == [
        "600000",
        "600001",
    ]
    assert closure["universe"]["members"] == [
        {"kind": "main_board_stock", "symbol": "600000"},
        {"kind": "main_board_stock", "symbol": "600001"},
    ]


def test_verified_run_detects_post_construction_result_mutation(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    object.__setattr__(run.result.ledger, "cash_fen", 0)
    with pytest.raises(PortfolioError) as captured:
        verify_portfolio_run(run)
    assert captured.value.code == "verified_portfolio_run_modified"


def test_verified_run_rejects_same_fields_forged_nested_dataclass(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    original = run.result.attempts[0]
    forged_type = make_dataclass(
        "ForgedEntryAttempt",
        [(item.name, object) for item in fields(original)],
        frozen=True,
    )
    forged = forged_type(**{item.name: getattr(original, item.name) for item in fields(original)})
    object.__setattr__(
        run.result,
        "attempts",
        (forged, *run.result.attempts[1:]),
    )

    with pytest.raises(PortfolioError) as captured:
        verify_portfolio_run(run)
    assert captured.value.code == "verified_portfolio_run_modified"


def test_verified_run_rejects_same_value_forged_nested_enum(tmp_path):
    class ForgedAttemptStatus(StrEnum):
        FILLED = "filled"

    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    object.__setattr__(
        run.result.attempts[0],
        "status",
        ForgedAttemptStatus.FILLED,
    )

    with pytest.raises(PortfolioError) as captured:
        verify_portfolio_run(run)
    assert captured.value.code == "verified_portfolio_run_modified"


def test_verified_run_detects_replaced_identity(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    object.__setattr__(
        run,
        "identity",
        replace(run.identity, run_id="0" * 64),
    )
    with pytest.raises(PortfolioError) as captured:
        verify_portfolio_run(run)
    assert captured.value.code == "verified_portfolio_run_modified"


def test_verified_run_rejects_forged_and_subclassed_wrappers(tmp_path):
    real = run_verified_portfolio(**make_portfolio_case(tmp_path))
    forged = object.__new__(VerifiedPortfolioRun)
    object.__setattr__(forged, "identity", real.identity)
    object.__setattr__(forged, "result", real.result)

    class SubclassedRun(VerifiedPortfolioRun):
        pass

    subclassed = object.__new__(SubclassedRun)
    object.__setattr__(subclassed, "identity", real.identity)
    object.__setattr__(subclassed, "result", real.result)

    with pytest.raises(PortfolioError) as forged_error:
        verify_portfolio_run(forged)
    assert forged_error.value.code == "unverified_portfolio_run"
    with pytest.raises(TypeError):
        verify_portfolio_run(subclassed)


def test_verified_run_rejects_noncanonical_or_duplicate_key_closure(tmp_path):
    noncanonical = run_verified_portfolio(**make_portfolio_case(tmp_path / "noncanonical"))
    parsed = json.loads(noncanonical.identity.input_closure_json)
    object.__setattr__(
        noncanonical.identity,
        "input_closure_json",
        (json.dumps(parsed, indent=2) + "\n").encode(),
    )
    with pytest.raises(PortfolioError) as noncanonical_error:
        verify_portfolio_run(noncanonical)
    assert noncanonical_error.value.code == "noncanonical_input_closure"

    duplicate = run_verified_portfolio(**make_portfolio_case(tmp_path / "duplicate"))
    object.__setattr__(
        duplicate.identity,
        "input_closure_json",
        b'{"duplicate":1,"duplicate":1}\n',
    )
    with pytest.raises(PortfolioError) as duplicate_error:
        verify_portfolio_run(duplicate)
    assert duplicate_error.value.code == "duplicate_input_closure_key"


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_verified_run_rejects_nonfinite_json_constants_with_stable_error(
    tmp_path,
    constant,
):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    object.__setattr__(
        run.identity,
        "input_closure_json",
        f'{{"value":{constant}}}\n'.encode(),
    )

    with pytest.raises(PortfolioError) as captured:
        verify_portfolio_run(run)
    assert captured.value.code == "noncanonical_input_closure"


def test_verified_run_registry_does_not_retain_completed_runs(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    registry_key = id(run)
    run_reference = weakref.ref(run)

    assert registry_key in identity_module._VERIFIED_RUN_REGISTRY
    del run

    assert run_reference() is None
    assert registry_key not in identity_module._VERIFIED_RUN_REGISTRY


def test_changed_verified_universe_is_rejected(tmp_path):
    case = make_portfolio_case(tmp_path)
    object.__setattr__(
        case["universe"],
        "members",
        (UniverseMember("600002", "main_board_stock"),),
    )
    with pytest.raises(PortfolioError):
        run_verified_portfolio(**case)


def test_changed_verified_calendar_is_rejected(tmp_path):
    case = make_portfolio_case(tmp_path)
    object.__setattr__(case["calendar"], "calendar_id", "0" * 64)
    with pytest.raises(PortfolioError):
        run_verified_portfolio(**case)


def test_changed_verified_fee_policy_is_rejected(tmp_path):
    case = make_portfolio_case(tmp_path)
    object.__setattr__(case["fee_policy"], "policy_digest", "0" * 64)
    with pytest.raises(PortfolioError):
        run_verified_portfolio(**case)


def test_changed_market_snapshot_identity_is_rejected(tmp_path):
    case = make_portfolio_case(tmp_path)
    market = case["inputs"][0].market_data
    object.__setattr__(market.provenance, "file_sha256", "0" * 64)
    with pytest.raises(PortfolioError):
        run_verified_portfolio(**case)


def test_changed_corporate_action_coverage_is_rejected(tmp_path):
    case = make_portfolio_case(tmp_path)
    actions = case["inputs"][0].corporate_actions
    object.__setattr__(
        actions.provenance,
        "coverage_start",
        date(2026, 7, 15),
    )
    with pytest.raises(PortfolioError):
        run_verified_portfolio(**case)


def test_task_5_implementation_whitelist_is_exact():
    assert identity_module._IMPLEMENTATION_FILES == (EXPECTED_TASK_5_IMPLEMENTATION_FILES)


def test_implementation_digest_works_from_non_editable_package_layout(
    monkeypatch,
    tmp_path,
):
    expected = identity_module._implementation_digest()
    installed_package = tmp_path / "site-packages" / "aquant"
    for filename in EXPECTED_TASK_5_IMPLEMENTATION_FILES:
        if filename == "pyproject.toml":
            destination = installed_package / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((PROJECT_ROOT / filename).read_bytes())
            continue
        parts = PurePosixPath(filename).parts
        assert parts[:2] == ("src", "aquant")
        destination = installed_package.joinpath(*parts[2:])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((PROJECT_ROOT / filename).read_bytes())

    monkeypatch.setattr(
        identity_module,
        "_PACKAGE_ROOT",
        installed_package,
        raising=False,
    )
    monkeypatch.setattr(
        identity_module,
        "_PROJECT_ROOT",
        tmp_path / "source-tree-does-not-exist",
        raising=False,
    )

    assert identity_module._implementation_digest() == expected


@pytest.mark.parametrize("filename", EXPECTED_TASK_5_IMPLEMENTATION_FILES)
def test_missing_implementation_file_fails_closed(monkeypatch, tmp_path, filename):
    original = identity_module._read_implementation_file

    def missing(candidate: str) -> bytes:
        if candidate == filename:
            raise OSError("unreadable")
        return original(candidate)

    monkeypatch.setattr(identity_module, "_read_implementation_file", missing)
    with pytest.raises(PortfolioError) as captured:
        run_verified_portfolio(**make_portfolio_case(tmp_path))
    assert captured.value.code == "implementation_file_missing"


@pytest.mark.parametrize("filename", EXPECTED_TASK_5_IMPLEMENTATION_FILES)
def test_changed_implementation_byte_changes_identity(
    monkeypatch,
    tmp_path,
    filename,
):
    baseline = run_verified_portfolio(**make_portfolio_case(tmp_path / "baseline"))
    original = identity_module._read_implementation_file

    def changed(candidate: str) -> bytes:
        content = original(candidate)
        return content + b"x" if candidate == filename else content

    monkeypatch.setattr(identity_module, "_read_implementation_file", changed)
    modified = run_verified_portfolio(**make_portfolio_case(tmp_path / "modified"))

    assert modified.identity.input_closure_digest == baseline.identity.input_closure_digest
    assert modified.identity.implementation_digest != baseline.identity.implementation_digest
    assert modified.identity.run_id != baseline.identity.run_id


def test_identity_is_stable_across_python_hash_seeds():
    def probe(seed: str) -> bytes:
        return subprocess.check_output(
            [sys.executable, str(PROJECT_ROOT / "tests" / "portfolio_identity_probe.py")],
            cwd=PROJECT_ROOT,
            env={
                **os.environ,
                "PYTHONHASHSEED": seed,
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
            },
        )

    first = probe("1")
    second = probe("98765")

    assert first == second
    payload = json.loads(first)
    assert set(payload) == {
        "artifact_sha256",
        "implementation_digest",
        "input_closure_digest",
        "run_id",
    }
    assert set(payload["artifact_sha256"]) == set(PORTFOLIO_ARTIFACT_FILES)
