from __future__ import annotations

import csv
import fcntl
import hashlib
import importlib
import io
import json
import os
import stat
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import aquant.portfolio.export as export_module
from aquant.portfolio import (
    PORTFOLIO_SCHEMA_VERSION,
    PortfolioExportError,
    export_portfolio_run,
    portfolio_payload_bytes,
    run_verified_portfolio,
)

TESTS_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(TESTS_ROOT))
gate_c_support = importlib.import_module("portfolio_gate_c_support")
sys.path.pop(0)
make_portfolio_case = gate_c_support.make_portfolio_case

PAYLOAD_FILES = {
    "run.json",
    "targets.csv",
    "orders.csv",
    "fills.csv",
    "positions.csv",
    "lots.csv",
    "cash.csv",
    "equity.csv",
    "receivables.csv",
    "corporate_actions.csv",
    "availability.csv",
    "metrics.json",
}
ARTIFACT_FILES = PAYLOAD_FILES | {"artifact_manifest.json"}
CSV_HEADERS = {
    "targets.csv": (
        "run_id",
        "schema_version",
        "target_id",
        "symbol",
        "signal_date",
        "target_notional_fen",
        "attempts_used",
        "status",
        "fill_event_id",
    ),
    "orders.csv": (
        "run_id",
        "schema_version",
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
        "cash_available_before_fen",
        "initial_candidate_cash_required_fen",
        "requested_cash_required_fen",
        "quantity_adjustment_reason",
        "fill_event_id",
    ),
    "fills.csv": (
        "run_id",
        "schema_version",
        "fill_event_id",
        "attempt_id",
        "target_id",
        "symbol",
        "execution_session",
        "side",
        "initial_candidate_size",
        "filled_size",
        "unit_price",
        "notional_fen",
        "commission_fen",
        "stamp_duty_fen",
        "transfer_fee_fen",
        "total_fees_fen",
        "cash_before_fen",
        "cash_after_fen",
        "lot_id",
        "available_date",
        "cash_available_before_fen",
        "initial_candidate_cash_required_fen",
        "requested_cash_required_fen",
        "quantity_adjustment_reason",
    ),
    "positions.csv": (
        "run_id",
        "schema_version",
        "session",
        "symbol",
        "total_size",
        "available_size",
        "locked_size",
        "mark_price",
        "market_value_fen",
    ),
    "lots.csv": (
        "run_id",
        "schema_version",
        "lot_id",
        "symbol",
        "acquired_date",
        "available_date",
        "original_size",
        "remaining_size",
        "unit_cost",
    ),
    "cash.csv": (
        "run_id",
        "schema_version",
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
        "total_fees_fen",
        "cash_before_fen",
        "cash_after_fen",
    ),
    "equity.csv": (
        "run_id",
        "schema_version",
        "session",
        "cash_fen",
        "position_market_value_fen",
        "receivable_fen",
        "equity_fen",
    ),
    "receivables.csv": (
        "run_id",
        "schema_version",
        "event_id",
        "symbol",
        "registered_date",
        "source_payable_date",
        "actual_cash_date",
        "amount_fen",
        "paid_date",
    ),
    "corporate_actions.csv": (
        "run_id",
        "schema_version",
        "event_id",
        "symbol",
        "ex_date",
        "source_payable_date",
        "actual_cash_date",
        "entitled_size",
        "cash_dividend_per_unit",
        "amount_fen",
    ),
    "availability.csv": (
        "run_id",
        "schema_version",
        "session",
        "symbol",
        "status",
        "mark_price",
        "carried_sessions",
        "adjustment_reason",
    ),
}


def _run(tmp_path: Path):
    return run_verified_portfolio(**make_portfolio_case(tmp_path))


def _csv(content: bytes) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
    return tuple(reader.fieldnames or ()), list(reader)


def _directory_bytes(directory: Path) -> dict[str, bytes]:
    return {
        item.name: item.read_bytes()
        for item in sorted(directory.iterdir(), key=lambda path: path.name)
    }


def test_payloads_are_byte_deterministic_complete_and_canonical(tmp_path):
    first_run = _run(tmp_path / "first")
    second_run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "second",
            symbols=("600001", "600000"),
        )
    )

    first = portfolio_payload_bytes(first_run)
    second = portfolio_payload_bytes(second_run)

    assert first_run.identity.run_id == second_run.identity.run_id
    assert first == second
    assert set(first) == PAYLOAD_FILES
    assert all(content.endswith(b"\n") for content, _ in first.values())
    assert all(b"\r" not in content for content, _ in first.values())
    for filename, expected_header in CSV_HEADERS.items():
        header, rows = _csv(first[filename][0])
        assert header == expected_header
        assert len(rows) == first[filename][1]
        assert all(row["run_id"] == first_run.identity.run_id for row in rows)
        assert all(row["schema_version"] == PORTFOLIO_SCHEMA_VERSION for row in rows)

    targets = _csv(first["targets.csv"][0])[1]
    orders = _csv(first["orders.csv"][0])[1]
    fills = _csv(first["fills.csv"][0])[1]
    assert [row["symbol"] for row in targets] == ["600000", "600001"]
    assert [(row["execution_session"], row["symbol"], row["attempt_number"]) for row in orders] == [
        ("2026-07-17", "600000", "1"),
        ("2026-07-17", "600001", "1"),
    ]
    assert [row["unit_price"] for row in fills] == ["10", "10"]
    assert fills[1]["quantity_adjustment_reason"] == ("insufficient_cash_including_fees")

    metrics = json.loads(first["metrics.json"][0])
    assert metrics["run_id"] == first_run.identity.run_id
    assert metrics["schema_version"] == PORTFOLIO_SCHEMA_VERSION
    assert metrics["total_return"] == "-0.0005095"
    assert metrics["max_drawdown"] == "-0.0005095"
    assert metrics["annualized_volatility"] is None
    assert metrics["sharpe_zero_rate"] is None
    assert metrics["risk_free_rate"] == "0"
    assert "E" not in metrics["total_return"]

    run_payload = json.loads(first["run.json"][0])
    assert run_payload["input_closure"] == json.loads(first_run.identity.input_closure_json)
    assert run_payload["result_digest"] == first_run.identity.result_digest
    assert run_payload["row_counts"] == {
        filename: row_count for filename, (_, row_count) in sorted(first.items())
    }
    assert len(run_payload["touched_fee_rates"]) == 4


def test_export_is_byte_deterministic_idempotent_and_manifested(tmp_path):
    run = _run(tmp_path / "inputs")

    first = export_portfolio_run(run, tmp_path / "outputs")
    before = _directory_bytes(first)
    second = export_portfolio_run(run, tmp_path / "outputs")
    after = _directory_bytes(second)

    assert first == second == tmp_path / "outputs" / run.identity.run_id
    assert before == after
    assert set(before) == ARTIFACT_FILES
    manifest = json.loads(before["artifact_manifest.json"])
    assert set(manifest) == {
        "artifact_schema_version",
        "files",
        "run_id",
        "status",
    }
    assert manifest["artifact_schema_version"] == PORTFOLIO_SCHEMA_VERSION
    assert manifest["run_id"] == run.identity.run_id
    assert manifest["status"] == "complete"
    assert set(manifest["files"]) == PAYLOAD_FILES
    payloads = portfolio_payload_bytes(run)
    for filename, entry in manifest["files"].items():
        assert set(entry) == {
            "row_count",
            "run_id",
            "schema_version",
            "sha256",
        }
        assert entry["row_count"] == payloads[filename][1]
        assert entry["run_id"] == run.identity.run_id
        assert entry["schema_version"] == PORTFOLIO_SCHEMA_VERSION
        assert len(entry["sha256"]) == 64
        assert entry["sha256"] == entry["sha256"].lower()
        assert entry["sha256"] == hashlib.sha256(before[filename]).hexdigest()


def test_cross_module_bundle_is_stable_across_order_root_clock_and_pid(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("time.time", lambda: 1.0)
    monkeypatch.setattr("os.getpid", lambda: 11)
    first_run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "first-inputs",
            symbols=("600001", "600000"),
        )
    )
    first_directory = export_portfolio_run(
        first_run,
        tmp_path / "first-outputs",
    )
    first_bytes = _directory_bytes(first_directory)
    repeated_directory = export_portfolio_run(
        first_run,
        tmp_path / "first-outputs",
    )

    monkeypatch.setattr("time.time", lambda: 9_999_999.0)
    monkeypatch.setattr("os.getpid", lambda: 99_999)
    second_run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "second-inputs",
            symbols=("600000", "600001"),
        )
    )
    second_directory = export_portfolio_run(
        second_run,
        tmp_path / "second-outputs",
    )

    assert first_run.identity.run_id == second_run.identity.run_id
    assert first_run.identity.implementation_digest == second_run.identity.implementation_digest
    assert first_run.identity.input_closure_digest == second_run.identity.input_closure_digest
    assert repeated_directory == first_directory
    assert _directory_bytes(repeated_directory) == first_bytes
    assert _directory_bytes(second_directory) == first_bytes
    assert set(first_bytes) == ARTIFACT_FILES


def test_export_fsyncs_every_payload_and_both_directories(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    synced_modes: list[int] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        synced_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(export_module.os, "fsync", record_fsync)
    export_portfolio_run(run, tmp_path / "outputs")

    assert sum(stat.S_ISREG(mode) for mode in synced_modes) == len(ARTIFACT_FILES)
    assert sum(stat.S_ISDIR(mode) for mode in synced_modes) == 2


def test_native_no_replace_primitive_refuses_existing_directory(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "source.txt").write_text("source", encoding="utf-8")
    (destination / "destination.txt").write_text(
        "destination",
        encoding="utf-8",
    )

    root_descriptor = os.open(
        tmp_path,
        os.O_RDONLY | export_module._DIRECTORY,
    )
    try:
        with pytest.raises(FileExistsError):
            export_module._rename_no_replace(
                root_descriptor,
                source.name,
                root_descriptor,
                destination.name,
            )
    finally:
        os.close(root_descriptor)

    assert (source / "source.txt").read_text(encoding="utf-8") == "source"
    assert (destination / "destination.txt").read_text(encoding="utf-8") == "destination"


def test_parent_fsync_failure_leaves_complete_idempotent_bundle(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    real_fsync = os.fsync
    directory_syncs = 0

    def fail_parent_sync(descriptor: int) -> None:
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs += 1
            if directory_syncs == 2:
                raise OSError("simulated parent fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(export_module.os, "fsync", fail_parent_sync)
    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)
    assert captured.value.code == "parent_fsync_failed"

    target = output_root / run.identity.run_id
    assert set(_directory_bytes(target)) == ARTIFACT_FILES
    monkeypatch.setattr(export_module.os, "fsync", real_fsync)
    assert export_portfolio_run(run, output_root) == target


def test_cleanup_never_deletes_replaced_temporary_directory(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    attacker_marker: Path | None = None

    def replace_temporary(
        source_descriptor: int,
        source_name: str,
        _destination_descriptor: int,
        _destination_name: str,
    ) -> None:
        nonlocal attacker_marker
        quarantine_name = f"{source_name}.quarantine"
        os.rename(
            source_name,
            quarantine_name,
            src_dir_fd=source_descriptor,
            dst_dir_fd=source_descriptor,
        )
        os.mkdir(source_name, dir_fd=source_descriptor)
        attacker_marker = output_root / source_name / "attacker.txt"
        attacker_marker.write_text("keep", encoding="utf-8")
        raise PortfolioExportError(
            "atomic_publish_failed",
            "simulated publish failure",
        )

    monkeypatch.setattr(
        export_module,
        "_rename_no_replace",
        replace_temporary,
    )
    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "unsafe_temporary_cleanup"
    assert attacker_marker is not None
    assert attacker_marker.read_text(encoding="utf-8") == "keep"


def test_output_root_swap_cannot_redirect_publication(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    root = tmp_path / "outputs"
    moved_root = tmp_path / "moved-output"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_complete = export_module._complete_bundle_bytes

    def swap_root(value):
        payload = real_complete(value)
        root.rename(moved_root)
        root.symlink_to(outside, target_is_directory=True)
        return payload

    monkeypatch.setattr(export_module, "_complete_bundle_bytes", swap_root)
    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, root)

    assert captured.value.code == "output_root_changed"
    assert tuple(outside.iterdir()) == ()


def test_output_root_swap_during_publish_cannot_redirect_publication(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    root = tmp_path / "outputs"
    moved_root = tmp_path / "moved-output"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_rename = export_module._rename_no_replace

    def swap_root_then_publish(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        root.rename(moved_root)
        root.symlink_to(outside, target_is_directory=True)
        real_rename(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(
        export_module,
        "_rename_no_replace",
        swap_root_then_publish,
    )
    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, root)

    assert captured.value.code == "output_root_changed"
    assert tuple(outside.iterdir()) == ()
    assert (moved_root / run.identity.run_id / "artifact_manifest.json").is_file()


def test_existing_target_swap_cannot_be_verified_through_symlink(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    target = export_portfolio_run(run, output_root)
    moved_target = tmp_path / "moved-target"
    outside = tmp_path / "outside"
    outside.mkdir()
    for name, content in _directory_bytes(target).items():
        (outside / name).write_bytes(content)
    outside_before = _directory_bytes(outside)
    real_listdir = export_module.os.listdir
    swapped = False

    def swap_target_before_listing(path_or_descriptor):
        nonlocal swapped
        if not swapped:
            swapped = True
            target.rename(moved_target)
            target.symlink_to(outside, target_is_directory=True)
        return real_listdir(path_or_descriptor)

    monkeypatch.setattr(
        export_module.os,
        "listdir",
        swap_target_before_listing,
    )
    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "artifact_conflict"
    assert target.is_symlink()
    assert _directory_bytes(outside) == outside_before


def test_cleanup_swap_after_validation_never_deletes_competitor(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    real_listdir = export_module.os.listdir
    competitor_marker: Path | None = None
    source_descriptor: int | None = None
    source_name: str | None = None
    swapped = False

    def fail_publish(
        passed_source_descriptor: int,
        passed_source_name: str,
        _destination_descriptor: int,
        _destination_name: str,
    ) -> None:
        nonlocal source_descriptor, source_name
        source_descriptor = passed_source_descriptor
        source_name = passed_source_name
        raise PortfolioExportError(
            "atomic_publish_failed",
            "simulated publish failure",
        )

    def swap_before_remove(descriptor: int):
        nonlocal competitor_marker, swapped
        if swapped:
            return real_listdir(descriptor)
        assert source_descriptor is not None
        assert source_name is not None
        swapped = True
        os.rename(
            source_name,
            f"{source_name}.owned",
            src_dir_fd=source_descriptor,
            dst_dir_fd=source_descriptor,
        )
        os.mkdir(source_name, dir_fd=source_descriptor)
        competitor_marker = output_root / source_name / "competitor.txt"
        competitor_marker.write_text("keep", encoding="utf-8")
        return real_listdir(descriptor)

    monkeypatch.setattr(export_module, "_rename_no_replace", fail_publish)
    monkeypatch.setattr(export_module.os, "listdir", swap_before_remove)
    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "unsafe_temporary_cleanup"
    assert competitor_marker is not None
    assert competitor_marker.read_text(encoding="utf-8") == "keep"


def test_failed_export_never_path_deletes_final_temporary_directory(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    rmdir_calls = 0

    def fail_publish(
        _source_descriptor: int,
        _source_name: str,
        _destination_descriptor: int,
        _destination_name: str,
    ) -> None:
        raise PortfolioExportError(
            "atomic_publish_failed",
            "simulated publish failure",
        )

    def forbidden_rmdir(*_args, **_kwargs) -> None:
        nonlocal rmdir_calls
        rmdir_calls += 1
        raise AssertionError("path-based final directory removal is unsafe")

    monkeypatch.setattr(export_module, "_rename_no_replace", fail_publish)
    monkeypatch.setattr(export_module.os, "rmdir", forbidden_rmdir)
    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "atomic_publish_failed"
    assert rmdir_calls == 0
    retained = tuple(item for item in output_root.iterdir() if item.name.endswith(".tmp"))
    assert len(retained) == 1
    assert tuple(retained[0].iterdir()) == ()


def test_decimal_text_is_fixed_point_without_trailing_zero_or_exponent():
    assert export_module._decimal_text(Decimal("0E-12")) == "0"
    assert export_module._decimal_text(Decimal("-0.000509500000")) == ("-0.0005095")
    assert export_module._decimal_text(Decimal("1E-12")) == "0.000000000001"


def test_every_csv_primary_order_is_deterministic(tmp_path):
    payloads = portfolio_payload_bytes(_run(tmp_path))
    targets = _csv(payloads["targets.csv"][0])[1]
    orders = _csv(payloads["orders.csv"][0])[1]
    fills = _csv(payloads["fills.csv"][0])[1]
    positions = _csv(payloads["positions.csv"][0])[1]
    lots = _csv(payloads["lots.csv"][0])[1]
    cash = _csv(payloads["cash.csv"][0])[1]
    equity = _csv(payloads["equity.csv"][0])[1]
    receivables = _csv(payloads["receivables.csv"][0])[1]
    corporate_actions = _csv(payloads["corporate_actions.csv"][0])[1]
    availability = _csv(payloads["availability.csv"][0])[1]

    assert [row["symbol"] for row in targets] == sorted(row["symbol"] for row in targets)
    assert [
        (row["execution_session"], row["symbol"], int(row["attempt_number"])) for row in orders
    ] == sorted(
        (
            row["execution_session"],
            row["symbol"],
            int(row["attempt_number"]),
        )
        for row in orders
    )
    assert [
        (row["execution_session"], row["symbol"], row["attempt_id"]) for row in fills
    ] == sorted((row["execution_session"], row["symbol"], row["attempt_id"]) for row in fills)
    assert [(row["session"], row["symbol"]) for row in positions] == sorted(
        (row["session"], row["symbol"]) for row in positions
    )
    assert [(row["symbol"], row["acquired_date"], row["lot_id"]) for row in lots] == sorted(
        (row["symbol"], row["acquired_date"], row["lot_id"]) for row in lots
    )
    assert [(row["session"], row["event_id"]) for row in cash] == sorted(
        (row["session"], row["event_id"]) for row in cash
    )
    assert [row["session"] for row in equity] == sorted(row["session"] for row in equity)
    assert [(row["actual_cash_date"], row["event_id"]) for row in receivables] == sorted(
        (row["actual_cash_date"], row["event_id"]) for row in receivables
    )
    assert [
        (row["ex_date"], row["symbol"], row["event_id"]) for row in corporate_actions
    ] == sorted((row["ex_date"], row["symbol"], row["event_id"]) for row in corporate_actions)
    assert [(row["session"], row["symbol"]) for row in availability] == sorted(
        (row["session"], row["symbol"]) for row in availability
    )


@pytest.mark.parametrize("mode", ("partial", "extra", "conflict"))
def test_existing_conflicting_bundle_is_never_supplemented_or_overwritten(
    tmp_path,
    mode,
):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    target = output_root / run.identity.run_id
    if mode == "partial":
        target.mkdir(parents=True)
        (target / "run.json").write_bytes(b'{"partial":true}\n')
    else:
        export_portfolio_run(run, output_root)
        if mode == "extra":
            (target / "unexpected.txt").write_text("keep", encoding="utf-8")
        else:
            (target / "run.json").write_bytes(b'{"conflict":true}\n')
    before = _directory_bytes(target)

    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "artifact_conflict"
    assert _directory_bytes(target) == before


def test_target_directory_symlink_is_rejected_without_touching_destination(
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("keep", encoding="utf-8")
    (output_root / run.identity.run_id).symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "artifact_conflict"
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("kind", ("symlink", "hardlink"))
def test_unsafe_existing_payload_is_rejected(tmp_path, kind):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    target = export_portfolio_run(run, output_root)
    payload = target / "run.json"
    outside = tmp_path / "outside.json"
    if kind == "symlink":
        outside.write_bytes(payload.read_bytes())
        payload.unlink()
        payload.symlink_to(outside)
    else:
        os.link(payload, outside)

    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "artifact_conflict"


@pytest.mark.parametrize("kind", ("symlink", "hardlink"))
def test_unsafe_lock_is_rejected_before_publication(tmp_path, kind):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    lock = output_root / f".{run.identity.run_id}.lock"
    outside = tmp_path / "outside.lock"
    outside.write_text("keep", encoding="utf-8")
    if kind == "symlink":
        lock.symlink_to(outside)
    else:
        os.link(outside, lock)

    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "unsafe_lock"
    assert not (output_root / run.identity.run_id).exists()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_lock_release_failure_is_stable_and_closes_descriptor(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    before = set(os.listdir("/dev/fd"))
    real_flock = fcntl.flock

    def fail_unlock(descriptor: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError("simulated lock release failure")
        real_flock(descriptor, operation)

    monkeypatch.setattr(export_module.fcntl, "flock", fail_unlock)
    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "unsafe_lock"
    assert set(os.listdir("/dev/fd")) == before


def test_temporary_open_validation_failure_retains_only_empty_directory(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    real_open = export_module.os.open
    real_fstat = export_module.os.fstat
    temporary_descriptor: int | None = None

    def record_temporary_open(path, flags, *args, **kwargs):
        nonlocal temporary_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if isinstance(path, str) and path.endswith(".tmp") and flags & export_module._DIRECTORY:
            temporary_descriptor = descriptor
        return descriptor

    def fail_temporary_fstat(descriptor: int):
        if descriptor == temporary_descriptor:
            raise OSError("simulated temporary fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(export_module.os, "open", record_temporary_open)
    monkeypatch.setattr(export_module.os, "fstat", fail_temporary_fstat)
    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "unsafe_temporary_directory"
    retained = tuple(item for item in output_root.iterdir() if item.name.endswith(".tmp"))
    assert len(retained) == 1
    assert tuple(retained[0].iterdir()) == ()


def test_output_root_symlink_is_rejected(tmp_path):
    run = _run(tmp_path / "inputs")
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "outputs"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, root)

    assert captured.value.code == "unsafe_output_root"
    assert tuple(outside.iterdir()) == ()


def test_output_root_symlinked_parent_is_rejected_without_creating_content(
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, linked_parent / "outputs")

    assert captured.value.code == "unsafe_output_root"
    assert tuple(outside.iterdir()) == ()


def test_publish_race_never_overwrites_competitor(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"
    target = output_root / run.identity.run_id

    def competitor(
        _source_descriptor: int,
        _source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        assert destination_name == target.name
        os.mkdir(destination_name, dir_fd=destination_descriptor)
        (target / "competitor.txt").write_text("keep", encoding="utf-8")
        raise FileExistsError("competitor won")

    monkeypatch.setattr(export_module, "_rename_no_replace", competitor)
    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "artifact_conflict"
    assert (target / "competitor.txt").read_text(encoding="utf-8") == "keep"
    retained = tuple(item for item in output_root.iterdir() if item.name.endswith(".tmp"))
    assert len(retained) == 1
    assert tuple(retained[0].iterdir()) == ()


def test_missing_no_replace_primitive_fails_closed(
    monkeypatch,
    tmp_path,
):
    run = _run(tmp_path / "inputs")
    output_root = tmp_path / "outputs"

    def unavailable(
        _source_descriptor: int,
        _source_name: str,
        _destination_descriptor: int,
        _destination_name: str,
    ) -> None:
        raise PortfolioExportError(
            "atomic_publish_unavailable",
            "no no-replace primitive",
        )

    monkeypatch.setattr(export_module, "_rename_no_replace", unavailable)
    with pytest.raises(PortfolioExportError) as captured:
        export_portfolio_run(run, output_root)

    assert captured.value.code == "atomic_publish_unavailable"
    assert not (output_root / run.identity.run_id).exists()
    retained = tuple(item for item in output_root.iterdir() if item.name.endswith(".tmp"))
    assert len(retained) == 1
    assert tuple(retained[0].iterdir()) == ()
