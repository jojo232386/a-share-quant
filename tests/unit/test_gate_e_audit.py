from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import aquant.data.snapshot as snapshot_module
import aquant.gate_e.audit as audit_module
from aquant.data.corporate_actions import CorporateActionEvent
from aquant.gate_e.config import load_gate_e_config
from aquant.portfolio import (
    PortfolioError,
    compute_portfolio_metrics,
    export_portfolio_run,
    run_verified_portfolio,
)
from aquant.rules import InstrumentKind

TESTS_ROOT = Path(__file__).parents[1]
PROJECT_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(TESTS_ROOT))
gate_c_support = importlib.import_module("portfolio_gate_c_support")
sys.path.pop(0)
make_portfolio_case = gate_c_support.make_portfolio_case

from aquant.gate_e.audit import (  # noqa: E402
    GateEAuditError,
    audit_gate_e_bundle,
    audit_gate_e_inputs,
    reconcile_gate_e_no_bar,
)

_DATES = (
    date(2026, 7, 22),
    date(2026, 7, 23),
    date(2026, 7, 24),
)
_DIVIDEND_DATES = (
    date(2026, 7, 21),
    date(2026, 7, 22),
    date(2026, 7, 23),
    date(2026, 7, 24),
)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _refresh_manifest_entry(
    directory: Path,
    filename: str,
    *,
    row_count: int | None = None,
) -> None:
    manifest_path = directory / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    content = (directory / filename).read_bytes()
    manifest["files"][filename]["sha256"] = hashlib.sha256(content).hexdigest()
    if row_count is not None:
        manifest["files"][filename]["row_count"] = row_count
    manifest_path.write_bytes(_json_bytes(manifest))


def _rewrite_csv(directory: Path, filename: str, mutate) -> None:
    path = directory / filename
    reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")))
    fieldnames = list(reader.fieldnames or ())
    rows = list(reader)
    mutate(rows)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(stream.getvalue(), encoding="utf-8", newline="")
    _refresh_manifest_entry(directory, filename)


def _rewrite_json(directory: Path, filename: str, mutate) -> None:
    path = directory / filename
    payload = json.loads(path.read_bytes())
    mutate(payload)
    path.write_bytes(_json_bytes(payload))
    _refresh_manifest_entry(directory, filename)


def _cash_dividend_event(
    *,
    payable_date: date,
    symbol: str = "600000",
) -> CorporateActionEvent:
    return CorporateActionEvent.create(
        symbol=symbol,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        announcement_date=date(2026, 7, 1),
        record_date=date(2026, 7, 22),
        ex_date=date(2026, 7, 23),
        payable_date=payable_date,
        cash_dividend_per_unit=Decimal("1"),
        stock_dividend_ratio=Decimal("0"),
        capitalization_ratio=Decimal("0"),
        rights_ratio=Decimal("0"),
        rights_price=None,
        source_schema="synthetic.cash.v1",
        source_url="https://example.invalid/cash",
    )


def _artifact(
    root: Path,
    *,
    dividend_payable_date: date | None = None,
) -> tuple[Path, str]:
    events = (
        None
        if dividend_payable_date is None
        else {"600000": (_cash_dividend_event(payable_date=dividend_payable_date),)}
    )
    dates = _DATES if dividend_payable_date is None else _DIVIDEND_DATES
    run = run_verified_portfolio(
        **make_portfolio_case(
            root / "inputs",
            symbols=("600000",),
            initial_cash_fen=1_000_000,
            gross_target_weight=Decimal("0.95"),
            calendar_dates=dates,
            market_dates=dates,
            market_opens=tuple(
                Decimal("10") if item <= date(2026, 7, 22) else Decimal("9")
                for item in dates
            ),
            market_closes=tuple(
                Decimal("10") if item <= date(2026, 7, 22) else Decimal("9")
                for item in dates
            ),
            signal_date=dates[0],
            end_date=date(2026, 7, 23),
            action_coverage_start=dates[0],
            corporate_action_events_by_symbol=events,
        )
    )
    return export_portfolio_run(run, root / "outputs"), run.identity.run_id


def test_valid_bundle_is_independently_reconciled(tmp_path):
    directory, run_id = _artifact(tmp_path)

    audit = audit_gate_e_bundle(directory, expected_run_id=run_id)

    assert audit.run_id == run_id
    assert audit.end_date == date(2026, 7, 23)
    assert audit.initial_cash_fen == 1_000_000
    assert audit.ending_cash_fen == (
        audit.initial_cash_fen
        - audit.invested_notional_fen
        - audit.paid_fees_fen
        + audit.dividend_cash_paid_fen
    )
    assert audit.ending_equity_fen == (
        audit.ending_cash_fen
        + audit.ending_position_market_value_fen
        + audit.ending_receivable_fen
    )
    assert audit.observation_count == 1
    assert audit.latest_plan_date == date(2026, 7, 24)


def test_same_session_cash_events_are_replayed_by_cash_chain(tmp_path):
    symbols = ("600001", "600002")
    events = {
        symbol: (
            _cash_dividend_event(
                payable_date=date(2026, 7, 23),
                symbol=symbol,
            ),
        )
        for symbol in symbols
    }
    run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "inputs",
            symbols=symbols,
            initial_cash_fen=2_000_000,
            gross_target_weight=Decimal("0.95"),
            calendar_dates=_DIVIDEND_DATES,
            market_dates=_DIVIDEND_DATES,
            market_opens=(Decimal("10"),) * len(_DIVIDEND_DATES),
            market_closes=(
                Decimal("10"),
                Decimal("10"),
                Decimal("9"),
                Decimal("9"),
            ),
            signal_date=_DIVIDEND_DATES[0],
            end_date=_DIVIDEND_DATES[2],
            action_coverage_start=_DIVIDEND_DATES[0],
            corporate_action_events_by_symbol=events,
        )
    )
    directory = export_portfolio_run(run, tmp_path / "outputs")
    with (directory / "cash.csv").open(encoding="utf-8") as stream:
        cash_rows = list(csv.DictReader(stream))
    dividends = [
        row
        for row in cash_rows
        if row["event_kind"] == "dividend_payment"
    ]
    cash_before_dividends = next(
        int(row["cash_after_fen"])
        for row in cash_rows
        if row["event_kind"] == "fill"
        and row["symbol"] == symbols[-1]
    )

    assert [row["symbol"] for row in dividends] == ["600002", "600001"]
    assert int(dividends[0]["cash_before_fen"]) != cash_before_dividends

    audit = audit_gate_e_bundle(
        directory,
        expected_run_id=run.identity.run_id,
    )

    assert audit.dividend_cash_paid_fen == sum(
        int(row["notional_fen"])
        for row in dividends
    )


def test_post_end_cash_event_is_rejected(tmp_path):
    directory, _run_id = _artifact(tmp_path)
    _rewrite_csv(
        directory,
        "cash.csv",
        lambda rows: rows[0].__setitem__("session", "2026-07-24"),
    )

    with pytest.raises(GateEAuditError) as captured:
        audit_gate_e_bundle(directory, expected_run_id=None)

    assert captured.value.code == "post_end_economic_event"


@pytest.mark.parametrize(
    ("filename", "field", "dividend_payable_date"),
    (
        ("targets.csv", "signal_date", None),
        ("orders.csv", "original_signal_date", None),
        ("orders.csv", "intent_session", None),
        ("orders.csv", "execution_session", None),
        ("fills.csv", "execution_session", None),
        ("positions.csv", "session", None),
        ("lots.csv", "acquired_date", None),
        ("cash.csv", "session", None),
        ("equity.csv", "session", None),
        ("receivables.csv", "registered_date", _DATES[1]),
        ("receivables.csv", "paid_date", _DATES[1]),
        ("corporate_actions.csv", "ex_date", _DATES[1]),
        ("availability.csv", "session", None),
    ),
)
def test_every_post_end_economic_csv_date_is_rejected(
    tmp_path,
    filename,
    field,
    dividend_payable_date,
):
    directory, _run_id = _artifact(
        tmp_path,
        dividend_payable_date=dividend_payable_date,
    )
    _rewrite_csv(
        directory,
        filename,
        lambda rows: rows[0].__setitem__(field, "2026-07-24"),
    )

    with pytest.raises(GateEAuditError) as captured:
        audit_gate_e_bundle(directory, expected_run_id=None)

    assert captured.value.code == "post_end_economic_event"


def test_post_end_metric_observation_is_rejected(tmp_path):
    directory, _run_id = _artifact(tmp_path)
    _rewrite_json(
        directory,
        "metrics.json",
        lambda payload: payload["daily_gross_exposure"][0].__setitem__(
            "session",
            "2026-07-24",
        ),
    )

    with pytest.raises(GateEAuditError) as captured:
        audit_gate_e_bundle(directory, expected_run_id=None)

    assert captured.value.code == "post_end_economic_event"


def test_paid_receivable_actual_cash_date_after_end_is_rejected(tmp_path):
    directory, _run_id = _artifact(
        tmp_path,
        dividend_payable_date=date(2026, 7, 23),
    )
    _rewrite_csv(
        directory,
        "receivables.csv",
        lambda rows: rows[0].__setitem__("actual_cash_date", "2026-07-24"),
    )

    with pytest.raises(GateEAuditError) as captured:
        audit_gate_e_bundle(directory, expected_run_id=None)

    assert captured.value.code == "post_end_economic_event"


def test_plan_dates_after_end_are_allowed_for_t1_and_unpaid_receivable(
    tmp_path,
):
    directory, run_id = _artifact(
        tmp_path,
        dividend_payable_date=date(2026, 7, 24),
    )

    audit = audit_gate_e_bundle(directory, expected_run_id=run_id)

    assert audit.ending_receivable_fen > 0
    assert audit.dividend_cash_paid_fen == 0
    assert audit.latest_plan_date == date(2026, 7, 24)


@pytest.mark.parametrize(
    ("filename", "field", "code"),
    (
        ("fills.csv", "notional_fen", "cash_reconciliation_failed"),
        ("fills.csv", "total_fees_fen", "cash_reconciliation_failed"),
        ("equity.csv", "cash_fen", "cash_reconciliation_failed"),
        (
            "metrics.json",
            "gross_target_notional_fen",
            "allocation_reconciliation_failed",
        ),
        (
            "metrics.json",
            "allocation_rounding_remainder_fen",
            "allocation_reconciliation_failed",
        ),
        (
            "metrics.json",
            "invested_notional_fen",
            "allocation_reconciliation_failed",
        ),
        (
            "metrics.json",
            "ordinary_lot_rounding_fen",
            "allocation_reconciliation_failed",
        ),
        (
            "metrics.json",
            "fee_lot_reduction_fen",
            "allocation_reconciliation_failed",
        ),
        (
            "metrics.json",
            "rejected_uninvested_fen",
            "allocation_reconciliation_failed",
        ),
        (
            "metrics.json",
            "expired_uninvested_fen",
            "allocation_reconciliation_failed",
        ),
        ("equity.csv", "equity_fen", "equity_reconciliation_failed"),
        (
            "equity.csv",
            "position_market_value_fen",
            "equity_reconciliation_failed",
        ),
    ),
)
def test_each_accounting_identity_fails_closed_on_value_mutation(
    tmp_path,
    filename,
    field,
    code,
):
    directory, _run_id = _artifact(tmp_path)
    if filename.endswith(".json"):
        _rewrite_json(
            directory,
            filename,
            lambda payload: payload.__setitem__(field, payload[field] + 1),
        )
    else:
        _rewrite_csv(
            directory,
            filename,
            lambda rows: rows[-1].__setitem__(
                field,
                str(int(rows[-1][field]) + 1),
            ),
        )

    with pytest.raises(GateEAuditError) as captured:
        audit_gate_e_bundle(directory, expected_run_id=None)

    assert captured.value.code == code


def test_initial_cash_identity_term_fails_closed_on_mutation(tmp_path):
    directory, _run_id = _artifact(tmp_path)
    _rewrite_json(
        directory,
        "run.json",
        lambda payload: payload["config"].__setitem__(
            "initial_cash_fen",
            payload["config"]["initial_cash_fen"] + 1,
        ),
    )

    with pytest.raises(GateEAuditError) as captured:
        audit_gate_e_bundle(directory, expected_run_id=None)

    assert captured.value.code == "cash_reconciliation_failed"


def test_dividend_cash_identity_term_fails_closed_on_mutation(tmp_path):
    directory, _run_id = _artifact(
        tmp_path,
        dividend_payable_date=date(2026, 7, 23),
    )

    def mutate(rows):
        dividend = next(row for row in rows if row["event_kind"] == "dividend_payment")
        dividend["notional_fen"] = str(int(dividend["notional_fen"]) + 1)

    _rewrite_csv(directory, "cash.csv", mutate)

    with pytest.raises(GateEAuditError) as captured:
        audit_gate_e_bundle(directory, expected_run_id=None)

    assert captured.value.code == "cash_reconciliation_failed"


def test_ending_receivable_identity_term_fails_closed_on_mutation(tmp_path):
    directory, _run_id = _artifact(
        tmp_path,
        dividend_payable_date=date(2026, 7, 24),
    )
    _rewrite_csv(
        directory,
        "equity.csv",
        lambda rows: rows[-1].__setitem__(
            "receivable_fen",
            str(int(rows[-1]["receivable_fen"]) + 1),
        ),
    )

    with pytest.raises(GateEAuditError) as captured:
        audit_gate_e_bundle(directory, expected_run_id=None)

    assert captured.value.code == "equity_reconciliation_failed"


def test_metrics_are_rebuilt_from_accepted_equity_rows(tmp_path):
    directory, _run_id = _artifact(tmp_path)
    _rewrite_json(
        directory,
        "metrics.json",
        lambda payload: payload.__setitem__("total_return", "0"),
    )

    with pytest.raises(GateEAuditError) as captured:
        audit_gate_e_bundle(directory, expected_run_id=None)

    assert captured.value.code == "metric_recomputation_failed"


def test_no_bar_carried_sessions_are_reconciled_inside_bundle(tmp_path):
    run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "inputs",
            symbols=("600000",),
            calendar_dates=_DATES,
            market_dates=(_DATES[0],),
            market_opens=(Decimal("10"),),
            market_closes=(Decimal("10"),),
            signal_date=_DATES[0],
            end_date=_DATES[1],
        )
    )
    directory = export_portfolio_run(run, tmp_path / "outputs")
    _rewrite_csv(
        directory,
        "availability.csv",
        lambda rows: rows[0].__setitem__("carried_sessions", "99"),
    )

    with pytest.raises(GateEAuditError) as captured:
        audit_gate_e_bundle(directory, expected_run_id=None)

    assert captured.value.code == "no_bar_reconciliation_failed"


def test_formal_input_audit_reconstructs_exact_no_bar_dates(
    monkeypatch: pytest.MonkeyPatch,
):
    config = load_gate_e_config(
        PROJECT_ROOT / "configs/releases/v0.2_gate_e.json"
    )
    input_root = PROJECT_ROOT / "release/v0.1-research/inputs"
    before = {
        relative: (
            (input_root / relative).lstat().st_mode,
            (input_root / relative).lstat().st_dev,
            (input_root / relative).lstat().st_ino,
            (input_root / relative).lstat().st_nlink,
            (input_root / relative).lstat().st_size,
            (input_root / relative).lstat().st_mtime_ns,
            (input_root / relative).lstat().st_ctime_ns,
            hashlib.sha256((input_root / relative).read_bytes()).hexdigest(),
        )
        for relative in config.payload["input_files"]
    }

    def reject_fchmod(_descriptor: int, _mode: int) -> None:
        raise AssertionError("Gate E input audit must be strictly read-only")

    monkeypatch.setattr(snapshot_module.os, "fchmod", reject_fchmod)

    audit = audit_gate_e_inputs(
        config,
        input_root,
    )
    after = {
        relative: (
            (input_root / relative).lstat().st_mode,
            (input_root / relative).lstat().st_dev,
            (input_root / relative).lstat().st_ino,
            (input_root / relative).lstat().st_nlink,
            (input_root / relative).lstat().st_size,
            (input_root / relative).lstat().st_mtime_ns,
            (input_root / relative).lstat().st_ctime_ns,
            hashlib.sha256((input_root / relative).read_bytes()).hexdigest(),
        )
        for relative in config.payload["input_files"]
    }

    assert audit.session_count == 2_072
    assert after == before
    assert audit.no_bar_counts == (
        ("000001", 3),
        ("000858", 3),
        ("510300", 2),
        ("510500", 2),
        ("600030", 3),
        ("600036", 3),
        ("600519", 3),
        ("600900", 3),
        ("601166", 3),
        ("601318", 3),
    )
    assert audit.no_bar_total == 28
    assert dict(audit.no_bar_dates)["600030"] == (
        date(2019, 5, 22),
        date(2022, 11, 23),
        date(2025, 7, 30),
    )
    assert dict(audit.no_bar_dates)["600900"] == (
        date(2019, 5, 17),
        date(2022, 11, 18),
        date(2025, 7, 25),
    )


def test_input_audit_binds_config_hash_to_market_manifest_before_read(
    monkeypatch: pytest.MonkeyPatch,
):
    config = load_gate_e_config(
        PROJECT_ROOT / "configs/releases/v0.2_gate_e.json"
    )
    input_root = PROJECT_ROOT / "release/v0.1-research/inputs"
    manifest_relative = config.payload["manifest"]
    records = audit_module.read_frozen_manifest(
        input_root / manifest_relative,
        expected_sha256=config.payload["input_files"][manifest_relative],
    )
    record = next(
        item
        for item in records
        if item.snapshot_id
        == config.payload["market_snapshots"]["000001"]
    )
    input_files = dict(config.payload["input_files"])
    input_files[record.snapshot_relative_path.as_posix()] = "0" * 64

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("mismatched manifest identity must fail before I/O")

    monkeypatch.setattr(audit_module, "_safe_input_file", unexpected_read)

    with pytest.raises(GateEAuditError) as captured:
        audit_module._read_gate_e_market_dates(
            input_root,
            record,
            input_files,
        )

    assert captured.value.code == "market_snapshot_mismatch"


def test_exact_input_no_bar_evidence_matches_bundle_evidence(tmp_path):
    run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "inputs",
            symbols=("600000",),
            calendar_dates=_DATES,
            market_dates=(_DATES[0],),
            market_opens=(Decimal("10"),),
            market_closes=(Decimal("10"),),
            signal_date=_DATES[0],
            end_date=_DATES[1],
        )
    )
    directory = export_portfolio_run(run, tmp_path / "outputs")
    bundle = audit_gate_e_bundle(directory, expected_run_id=run.identity.run_id)

    reconcile_gate_e_no_bar(
        bundle.no_bar_dates,
        bundle.no_bar_carried_sessions,
        (
            ("600000", (date(2026, 7, 23),)),
        ),
        (
            ("600000", date(2026, 7, 23), 1),
        ),
    )


def test_no_bar_input_output_difference_is_rejected():
    with pytest.raises(GateEAuditError) as captured:
        reconcile_gate_e_no_bar(
            (("600030", (date(2022, 1, 19),)),),
            (("600030", date(2022, 1, 19), 1),),
            (("600030", (date(2022, 1, 20),)),),
            (("600030", date(2022, 1, 20), 1),),
        )

    assert captured.value.code == "no_bar_input_output_mismatch"


def test_post_end_market_row_changes_identity_but_not_pre_end_economics(
    tmp_path,
):
    baseline = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "baseline",
            symbols=("600000",),
            calendar_dates=_DATES,
            market_dates=_DATES,
            market_opens=(Decimal("10"), Decimal("10"), Decimal("9")),
            market_closes=(Decimal("10"), Decimal("10"), Decimal("9")),
            signal_date=_DATES[0],
            end_date=_DATES[1],
        )
    )
    changed = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "changed",
            symbols=("600000",),
            calendar_dates=_DATES,
            market_dates=_DATES,
            market_opens=(Decimal("10"), Decimal("10"), Decimal("999")),
            market_closes=(Decimal("10"), Decimal("10"), Decimal("999")),
            signal_date=_DATES[0],
            end_date=_DATES[1],
        )
    )

    assert baseline.result == changed.result
    assert compute_portfolio_metrics(baseline) == compute_portfolio_metrics(changed)
    assert baseline.identity.input_closure_digest != (
        changed.identity.input_closure_digest
    )
    assert baseline.identity.run_id != changed.identity.run_id


def test_in_place_post_end_market_mutation_with_old_provenance_fails_closed(
    tmp_path,
):
    case = make_portfolio_case(
        tmp_path,
        symbols=("600000",),
        calendar_dates=_DATES,
        market_dates=_DATES,
        market_opens=(Decimal("10"), Decimal("10"), Decimal("9")),
        market_closes=(Decimal("10"), Decimal("10"), Decimal("9")),
        signal_date=_DATES[0],
        end_date=_DATES[1],
    )
    market = case["inputs"][0].market_data
    damaged = market._frame.copy(deep=True)
    damaged.loc[damaged.index[-1], "close"] = 999
    object.__setattr__(market, "_frame", damaged)

    with pytest.raises(PortfolioError) as captured:
        run_verified_portfolio(**case)

    assert captured.value.code == "verified_market_data_modified"


def test_gate_e_audit_does_not_import_portfolio_producers():
    module = importlib.import_module("aquant.gate_e.audit")
    source = Path(module.__file__).read_text(encoding="utf-8")

    assert "aquant.portfolio.export" not in source
    assert "run_verified_portfolio" not in source
    assert "compute_portfolio_metrics" not in source
