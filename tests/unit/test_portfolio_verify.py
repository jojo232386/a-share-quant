from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import textwrap
from datetime import date
from decimal import Decimal, Inexact, Rounded, localcontext
from pathlib import Path

import pytest

from aquant.data.corporate_actions import CorporateActionEvent
from aquant.portfolio import (
    PORTFOLIO_ARTIFACT_FILES,
    PortfolioArtifactError,
    export_portfolio_run,
    run_verified_portfolio,
    verify_portfolio_artifact,
)
from aquant.rules import InstrumentKind
from aquant.universe import (
    UniverseMember,
    canonical_universe_bytes,
    load_verified_universe,
)

TESTS_ROOT = Path(__file__).parents[1]
PROJECT_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(TESTS_ROOT))
gate_c_support = importlib.import_module("portfolio_gate_c_support")
sys.path.pop(0)
make_portfolio_case = gate_c_support.make_portfolio_case
verify_module = importlib.import_module("aquant.portfolio.verify")


def _artifact(tmp_path: Path) -> tuple[Path, str]:
    run = run_verified_portfolio(
        **make_portfolio_case(tmp_path / "inputs"),
    )
    return (
        export_portfolio_run(run, tmp_path / "outputs"),
        run.identity.run_id,
    )


def _directory_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir(), key=lambda item: item.name)
    }


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
    ).encode("utf-8")


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


def _rewrite_json(
    directory: Path,
    filename: str,
    mutate,
) -> None:
    path = directory / filename
    payload = json.loads(path.read_bytes())
    mutate(payload)
    path.write_bytes(_json_bytes(payload))
    _refresh_manifest_entry(directory, filename)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")))
    return list(reader.fieldnames or ()), list(reader)


def _rewrite_csv(
    directory: Path,
    filename: str,
    mutate,
    *,
    update_row_count: bool = False,
) -> None:
    path = directory / filename
    fieldnames, rows = _read_csv(path)
    mutate(rows)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=fieldnames,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(stream.getvalue(), encoding="utf-8", newline="")
    _refresh_manifest_entry(
        directory,
        filename,
        row_count=len(rows) if update_row_count else None,
    )
    if update_row_count:
        _rewrite_json(
            directory,
            "run.json",
            lambda payload: payload["row_counts"].__setitem__(
                filename,
                len(rows),
            ),
        )


def _reidentify_bundle_with_result_digest(
    directory: Path,
    result_digest: str,
) -> Path:
    run_path = directory / "run.json"
    run = json.loads(run_path.read_bytes())
    identity_payload = {
        "engine": run["engine"],
        "implementation_digest": run["implementation_digest"],
        "input_closure_digest": run["input_closure_digest"],
        "result_digest": result_digest,
        "schema_version": run["schema_version"],
    }
    run_id = hashlib.sha256(_json_bytes(identity_payload)).hexdigest()
    run["result_digest"] = result_digest
    run["run_id"] = run_id
    run_path.write_bytes(_json_bytes(run))

    metrics_path = directory / "metrics.json"
    metrics = json.loads(metrics_path.read_bytes())
    metrics["run_id"] = run_id
    metrics_path.write_bytes(_json_bytes(metrics))
    for filename in sorted(verify_module._CSV_SCHEMAS):
        path = directory / filename
        fieldnames, rows = _read_csv(path)
        for row in rows:
            row["run_id"] = run_id
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(
            stream,
            fieldnames=fieldnames,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        path.write_text(
            stream.getvalue(),
            encoding="utf-8",
            newline="",
        )

    manifest_path = directory / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["run_id"] = run_id
    for filename, entry in manifest["files"].items():
        entry["run_id"] = run_id
        entry["sha256"] = hashlib.sha256((directory / filename).read_bytes()).hexdigest()
    manifest_path.write_bytes(_json_bytes(manifest))
    destination = directory.with_name(run_id)
    directory.rename(destination)
    return destination


def test_verified_artifact_file_counts_include_manifest(tmp_path):
    directory, run_id = _artifact(tmp_path)

    artifact = verify_portfolio_artifact(
        directory,
        expected_run_id=run_id,
    )

    assert artifact.run_id == run_id
    assert artifact.status == "verified"
    assert artifact.artifact_file_count == 13
    assert artifact.payload_file_count == 12
    assert artifact.file_count == 13
    assert artifact.trade_count == 2
    assert (
        artifact.artifact_manifest_sha256
        == hashlib.sha256((directory / "artifact_manifest.json").read_bytes()).hexdigest()
    )
    assert dict(artifact.row_counts)["fills.csv"] == 2


def test_verifier_reconstructs_universe_identity_from_canonical_member_order(
    tmp_path,
):
    case = make_portfolio_case(
        tmp_path / "inputs",
        symbols=("600000", "600001"),
    )
    members = (
        UniverseMember("600001", InstrumentKind.MAIN_BOARD_STOCK.value),
        UniverseMember("600000", InstrumentKind.MAIN_BOARD_STOCK.value),
    )
    content = canonical_universe_bytes("gate-c-test", members)
    universe_id = hashlib.sha256(content).hexdigest()
    universe_path = tmp_path / "universes" / f"{universe_id}.json"
    universe_path.parent.mkdir(parents=True)
    universe_path.write_bytes(content)
    case["universe"] = load_verified_universe(
        universe_path,
        expected_id=universe_id,
    )
    run = run_verified_portfolio(**case)
    directory = export_portfolio_run(run, tmp_path / "outputs")

    verified = verify_portfolio_artifact(
        directory,
        expected_run_id=run.identity.run_id,
    )

    assert verified.status == "verified"
    assert json.loads((directory / "run.json").read_bytes())["input_closure"][
        "universe"
    ]["members"] == [
        {"kind": item.kind, "symbol": item.symbol}
        for item in members
    ]


def test_verifier_is_independent_of_callers_decimal_context(tmp_path):
    directory, run_id = _artifact(tmp_path)
    results = [
        verify_portfolio_artifact(
            directory,
            expected_run_id=run_id,
        )
    ]
    for precision in (10, 28, 80):
        with localcontext() as context:
            context.prec = precision
            results.append(
                verify_portfolio_artifact(
                    directory,
                    expected_run_id=run_id,
                )
            )
    with localcontext() as context:
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        results.append(
            verify_portfolio_artifact(
                directory,
                expected_run_id=run_id,
            )
        )

    assert all(item == results[0] for item in results[1:])


def test_verifier_accepts_multi_session_risk_and_zero_volatility(tmp_path):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
        date(2026, 7, 21),
        date(2026, 7, 22),
    )
    risk_run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "risk" / "inputs",
            calendar_dates=official_dates,
            market_dates=official_dates[:4],
            market_opens=(Decimal("10"),) * 4,
            market_closes=(
                Decimal("10"),
                Decimal("10"),
                Decimal("9"),
                Decimal("11"),
            ),
            signal_date=official_dates[0],
            end_date=official_dates[3],
        )
    )
    risk_directory = export_portfolio_run(
        risk_run,
        tmp_path / "risk" / "outputs",
    )
    zero_run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "zero" / "inputs",
            initial_cash_fen=2_001_020,
            calendar_dates=official_dates[:4],
            market_dates=official_dates[:3],
            market_opens=(Decimal("10"),) * 3,
            market_closes=(Decimal("10.0051"),) * 3,
            signal_date=official_dates[0],
            end_date=official_dates[2],
        )
    )
    zero_directory = export_portfolio_run(
        zero_run,
        tmp_path / "zero" / "outputs",
    )

    assert verify_portfolio_artifact(risk_directory).status == "verified"
    assert verify_portfolio_artifact(zero_directory).status == "verified"
    risk_metrics = json.loads((risk_directory / "metrics.json").read_bytes())
    zero_metrics = json.loads((zero_directory / "metrics.json").read_bytes())
    assert risk_metrics["annualized_volatility"] is not None
    assert risk_metrics["sharpe_zero_rate"] is not None
    assert zero_metrics["annualized_volatility"] == "0"
    assert zero_metrics["sharpe_zero_rate"] is None


def test_verifier_accepts_dividend_weekend_payment_and_no_bar_carry(
    tmp_path,
):
    official_dates = (
        date(2026, 7, 15),
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
        date(2026, 7, 21),
    )
    event = CorporateActionEvent.create(
        symbol="600000",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        announcement_date=date(2026, 7, 1),
        record_date=date(2026, 7, 16),
        ex_date=date(2026, 7, 17),
        payable_date=date(2026, 7, 18),
        cash_dividend_per_unit=Decimal("1"),
        stock_dividend_ratio=Decimal("0"),
        capitalization_ratio=Decimal("0"),
        rights_ratio=Decimal("0"),
        rights_price=None,
        source_schema="synthetic.cash.v1",
        source_url="https://example.invalid/cash",
    )
    run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "inputs",
            symbols=("600000",),
            initial_cash_fen=1_000_000,
            gross_target_weight=Decimal("0.95"),
            calendar_dates=official_dates,
            market_dates=(
                official_dates[0],
                official_dates[1],
                official_dates[3],
                official_dates[4],
            ),
            market_opens=(
                Decimal("10"),
                Decimal("10"),
                Decimal("9"),
                Decimal("9"),
            ),
            market_closes=(
                Decimal("10"),
                Decimal("10"),
                Decimal("9"),
                Decimal("9"),
            ),
            signal_date=official_dates[0],
            end_date=official_dates[3],
            action_coverage_start=official_dates[0],
            corporate_action_events_by_symbol={"600000": (event,)},
        )
    )
    directory = export_portfolio_run(run, tmp_path / "outputs")

    artifact = verify_portfolio_artifact(
        directory,
        expected_run_id=run.identity.run_id,
    )

    assert artifact.status == "verified"
    assert dict(artifact.row_counts)["corporate_actions.csv"] == 1
    assert dict(artifact.row_counts)["receivables.csv"] == 1
    assert dict(artifact.row_counts)["cash.csv"] == 2


@pytest.mark.parametrize(
    ("max_entry_attempts", "expected_field"),
    ((5, "rejected_uninvested_fen"), (1, "expired_uninvested_fen")),
)
def test_verifier_accepts_rejected_and_expired_target_classification(
    tmp_path,
    max_entry_attempts,
    expected_field,
):
    run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "inputs",
            market_opens=(Decimal("10"), Decimal("100000")),
            market_closes=(Decimal("10"), Decimal("100000")),
            max_entry_attempts=max_entry_attempts,
        )
    )
    directory = export_portfolio_run(run, tmp_path / "outputs")

    assert verify_portfolio_artifact(directory).status == "verified"
    metrics = json.loads((directory / "metrics.json").read_bytes())
    assert metrics["trade_count"] == 0
    assert metrics[expected_field] == 2_000_000


@pytest.mark.parametrize("filename", sorted(PORTFOLIO_ARTIFACT_FILES))
def test_any_artifact_byte_damage_is_detected_without_repair(
    tmp_path,
    filename,
):
    directory, _run_id = _artifact(tmp_path)
    path = directory / filename
    path.write_bytes(path.read_bytes() + b"x")
    before = _directory_bytes(directory)

    with pytest.raises(PortfolioArtifactError):
        verify_portfolio_artifact(directory)

    assert _directory_bytes(directory) == before


def test_expected_run_id_and_directory_name_are_both_bound(tmp_path):
    directory, run_id = _artifact(tmp_path)

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory, expected_run_id="0" * 64)
    assert captured.value.code == "artifact_identity_mismatch"

    renamed = directory.with_name("1" * 64)
    directory.rename(renamed)
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(renamed, expected_run_id=run_id)
    assert captured.value.code == "artifact_identity_mismatch"


def test_trusted_run_id_rejects_another_internally_valid_bundle(tmp_path):
    _trusted_directory, trusted_run_id = _artifact(tmp_path / "trusted")
    changed = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "changed" / "inputs",
            final_close=Decimal("10.05"),
        ),
    )
    changed_directory = export_portfolio_run(
        changed,
        tmp_path / "changed" / "outputs",
    )

    assert verify_portfolio_artifact(changed_directory).status == "verified"
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(
            changed_directory,
            expected_run_id=trusted_run_id,
        )

    assert captured.value.code == "artifact_identity_mismatch"


def test_semantic_result_digest_rejects_coherent_transport_rehash(tmp_path):
    directory, _run_id = _artifact(tmp_path)
    reidentified = _reidentify_bundle_with_result_digest(
        directory,
        "0" * 64,
    )

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(reidentified)

    assert captured.value.code == "result_digest_mismatch"


@pytest.mark.parametrize("mode", ("missing", "extra"))
def test_artifact_file_set_must_be_exact(tmp_path, mode):
    directory, _run_id = _artifact(tmp_path)
    if mode == "missing":
        (directory / "availability.csv").unlink()
    else:
        (directory / "unexpected.txt").write_text("keep", encoding="utf-8")
    before = _directory_bytes(directory)

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "artifact_file_set_mismatch"
    assert _directory_bytes(directory) == before


def test_directory_and_payload_links_are_rejected(tmp_path):
    directory, _run_id = _artifact(tmp_path / "directory")
    moved = directory.with_name("moved-artifact")
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)
    assert captured.value.code == "unsafe_artifact"

    linked_directory, _run_id = _artifact(tmp_path / "payload")
    payload = linked_directory / "run.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(payload.read_bytes())
    payload.unlink()
    payload.symlink_to(outside)
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(linked_directory)
    assert captured.value.code == "unsafe_artifact"

    hardlinked_directory, _run_id = _artifact(tmp_path / "hardlink")
    os.link(hardlinked_directory / "run.json", tmp_path / "hardlink.json")
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(hardlinked_directory)
    assert captured.value.code == "unsafe_artifact"


def test_payload_link_and_entry_races_are_rejected(
    monkeypatch,
    tmp_path,
):
    hardlink_directory, _run_id = _artifact(tmp_path / "hardlink-race")
    original_read = verify_module.os.read
    armed = True

    def add_hardlink_during_read(descriptor, size):
        nonlocal armed
        if armed:
            os.link(
                hardlink_directory / "artifact_manifest.json",
                tmp_path / "late-hardlink.json",
            )
            armed = False
        return original_read(descriptor, size)

    monkeypatch.setattr(verify_module.os, "read", add_hardlink_during_read)
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(hardlink_directory)
    assert captured.value.code == "unsafe_artifact"

    monkeypatch.setattr(verify_module.os, "read", original_read)
    replaced_directory, _run_id = _artifact(tmp_path / "entry-race")
    original_parse = verify_module._parse_json
    armed = True

    def replace_entry_after_read(content):
        nonlocal armed
        if armed:
            run_path = replaced_directory / "run.json"
            saved = tmp_path / "saved-run.json"
            run_path.replace(saved)
            run_path.write_bytes(saved.read_bytes())
            armed = False
        return original_parse(content)

    monkeypatch.setattr(
        verify_module,
        "_parse_json",
        replace_entry_after_read,
    )
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(replaced_directory)
    assert captured.value.code == "unsafe_artifact"

    monkeypatch.setattr(verify_module, "_parse_json", original_parse)
    extra_directory, _run_id = _artifact(tmp_path / "extra-race")
    armed = True

    def add_extra_entry_after_listing(content):
        nonlocal armed
        if armed:
            (extra_directory / "late-extra.txt").write_text(
                "keep",
                encoding="utf-8",
            )
            armed = False
        return original_parse(content)

    monkeypatch.setattr(
        verify_module,
        "_parse_json",
        add_extra_entry_after_listing,
    )
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(extra_directory)
    assert captured.value.code == "unsafe_artifact"


@pytest.mark.parametrize(
    "mode",
    ("same_inode_overwrite", "transient_hardlink_overwrite"),
)
def test_same_inode_content_races_are_rejected(
    monkeypatch,
    tmp_path,
    mode,
):
    directory, _run_id = _artifact(tmp_path)
    original_parse = verify_module._parse_json
    armed = True

    def change_metrics_after_all_files_are_read(content):
        nonlocal armed
        if armed:
            metrics_path = directory / "metrics.json"
            if mode == "same_inode_overwrite":
                write_path = metrics_path
            else:
                write_path = tmp_path / "transient-metrics-link.json"
                os.link(metrics_path, write_path)
            with write_path.open("r+b") as stream:
                stream.write(b"!")
                stream.flush()
                os.fsync(stream.fileno())
            if mode == "transient_hardlink_overwrite":
                write_path.unlink()
            armed = False
        return original_parse(content)

    monkeypatch.setattr(
        verify_module,
        "_parse_json",
        change_metrics_after_all_files_are_read,
    )

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "unsafe_artifact"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("status", "verified"),
        ("run_id", "A" * 64),
        ("artifact_schema_version", "0.2"),
    ),
)
def test_manifest_exact_contract_rejects_changed_values(
    tmp_path,
    field,
    value,
):
    directory, _run_id = _artifact(tmp_path)
    path = directory / "artifact_manifest.json"
    payload = json.loads(path.read_bytes())
    payload[field] = value
    path.write_bytes(_json_bytes(payload))

    with pytest.raises(PortfolioArtifactError):
        verify_portfolio_artifact(directory)


@pytest.mark.parametrize("mode", ("unknown", "missing", "uppercase_hash"))
def test_manifest_exact_contract_rejects_shape_and_hash_variants(
    tmp_path,
    mode,
):
    directory, _run_id = _artifact(tmp_path)
    path = directory / "artifact_manifest.json"
    payload = json.loads(path.read_bytes())
    if mode == "unknown":
        payload["unexpected"] = True
    elif mode == "missing":
        payload.pop("status")
    else:
        payload["files"]["run.json"]["sha256"] = payload["files"]["run.json"]["sha256"].upper()
    path.write_bytes(_json_bytes(payload))

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "invalid_artifact_manifest"


def test_manifest_duplicate_key_is_rejected(tmp_path):
    directory, _run_id = _artifact(tmp_path)
    path = directory / "artifact_manifest.json"
    content = path.read_bytes()
    path.write_bytes(
        content.replace(
            b'{"artifact_schema_version":',
            b'{"status":"complete","artifact_schema_version":',
            1,
        )
    )

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "noncanonical_json"


def test_manifest_and_run_row_count_lie_is_rejected(tmp_path):
    directory, _run_id = _artifact(tmp_path)
    run_path = directory / "run.json"
    run = json.loads(run_path.read_bytes())
    run["row_counts"]["lots.csv"] += 1
    run_path.write_bytes(_json_bytes(run))

    manifest_path = directory / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"]["lots.csv"]["row_count"] += 1
    manifest["files"]["run.json"]["sha256"] = hashlib.sha256(
        run_path.read_bytes()
    ).hexdigest()
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "artifact_row_count_mismatch"


def test_duplicate_and_noncanonical_json_are_rejected_after_hash_refresh(
    tmp_path,
):
    duplicate_directory, _run_id = _artifact(tmp_path / "duplicate")
    run_path = duplicate_directory / "run.json"
    content = run_path.read_bytes()
    run_path.write_bytes(
        content.replace(
            b'{"behavior_modes":',
            b'{"run_id":"0","behavior_modes":',
            1,
        )
    )
    _refresh_manifest_entry(duplicate_directory, "run.json")
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(duplicate_directory)
    assert captured.value.code == "noncanonical_json"

    noncanonical_directory, _run_id = _artifact(tmp_path / "noncanonical")
    metrics_path = noncanonical_directory / "metrics.json"
    payload = json.loads(metrics_path.read_bytes())
    metrics_path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    _refresh_manifest_entry(noncanonical_directory, "metrics.json")
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(noncanonical_directory)
    assert captured.value.code == "noncanonical_json"


def test_csv_types_dates_identity_and_primary_order_are_strict(tmp_path):
    malformed_int, _run_id = _artifact(tmp_path / "int")
    _rewrite_csv(
        malformed_int,
        "cash.csv",
        lambda rows: rows[0].__setitem__("cash_after_fen", "1.0"),
    )
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(malformed_int)
    assert captured.value.code == "invalid_artifact_value"

    malformed_date, _run_id = _artifact(tmp_path / "date")
    _rewrite_csv(
        malformed_date,
        "equity.csv",
        lambda rows: rows[0].__setitem__("session", "2026-7-17"),
    )
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(malformed_date)
    assert captured.value.code == "invalid_artifact_value"

    wrong_identity, _run_id = _artifact(tmp_path / "identity")
    _rewrite_csv(
        wrong_identity,
        "targets.csv",
        lambda rows: rows[0].__setitem__("run_id", "0" * 64),
    )
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(wrong_identity)
    assert captured.value.code == "artifact_identity_mismatch"

    reordered, _run_id = _artifact(tmp_path / "order")
    _rewrite_csv(reordered, "orders.csv", lambda rows: rows.reverse())
    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(reordered)
    assert captured.value.code == "invalid_artifact_order"


def test_negative_touched_fee_rate_is_rejected_at_value_boundary(
    tmp_path,
):
    directory, _run_id = _artifact(tmp_path)
    _rewrite_json(
        directory,
        "run.json",
        lambda payload: payload["touched_fee_rates"][0].__setitem__(
            "rate",
            "-0.1",
        ),
    )

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "invalid_artifact_value"


def test_duplicate_primary_key_is_rejected_even_with_consistent_counts(
    tmp_path,
):
    directory, _run_id = _artifact(tmp_path)

    def duplicate(rows):
        rows.append(dict(rows[-1]))

    _rewrite_csv(
        directory,
        "targets.csv",
        duplicate,
        update_row_count=True,
    )

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "duplicate_artifact_key"


@pytest.mark.parametrize(
    ("filename", "field", "value", "expected_code"),
    (
        (
            "cash.csv",
            "cash_after_fen",
            "999491",
            "cash_reconciliation_failed",
        ),
        (
            "equity.csv",
            "equity_fen",
            "1998982",
            "daily_accounting_identity_failed",
        ),
        (
            "positions.csv",
            "total_size",
            "900",
            "position_reconciliation_failed",
        ),
        (
            "fills.csv",
            "filled_size",
            "800",
            "fill_reconciliation_failed",
        ),
    ),
)
def test_independent_replay_rejects_semantic_damage(
    tmp_path,
    filename,
    field,
    value,
    expected_code,
):
    directory, _run_id = _artifact(tmp_path)
    if filename == "positions.csv":

        def mutate(rows):
            rows[0]["total_size"] = value
            rows[0]["locked_size"] = value
            rows[0]["market_value_fen"] = "900000"
    else:

        def mutate(rows):
            rows[0][field] = value

    _rewrite_csv(directory, filename, mutate)

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == expected_code


def test_independent_replay_rejects_internally_consistent_lot_damage(
    tmp_path,
):
    directory, _run_id = _artifact(tmp_path)

    def shrink_lot(rows):
        rows[0]["original_size"] = "900"
        rows[0]["remaining_size"] = "900"

    _rewrite_csv(directory, "lots.csv", shrink_lot)

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "fill_reconciliation_failed"


def test_semantic_digest_rejects_cross_file_coherent_lot_id_damage(
    tmp_path,
):
    directory, _run_id = _artifact(tmp_path)
    _fields, lots = _read_csv(directory / "lots.csv")
    original_lot_id = lots[0]["lot_id"]
    forged_lot_id = "lot:coherent-tamper"

    _rewrite_csv(
        directory,
        "lots.csv",
        lambda rows: rows[0].__setitem__("lot_id", forged_lot_id),
    )
    _rewrite_csv(
        directory,
        "fills.csv",
        lambda rows: next(
            row for row in rows if row["lot_id"] == original_lot_id
        ).__setitem__("lot_id", forged_lot_id),
    )
    _rewrite_csv(
        directory,
        "cash.csv",
        lambda rows: next(
            row for row in rows if row["reference_id"] == original_lot_id
        ).__setitem__("reference_id", forged_lot_id),
    )

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "result_digest_mismatch"


def test_receivable_without_corporate_action_is_rejected(tmp_path):
    directory, run_id = _artifact(tmp_path)

    def add_receivable(rows):
        rows.append(
            {
                "run_id": run_id,
                "schema_version": "0.2.0",
                "event_id": "dividend:forged",
                "symbol": "600000",
                "registered_date": "2026-07-17",
                "source_payable_date": "2026-07-20",
                "actual_cash_date": "2026-07-20",
                "amount_fen": "100",
                "paid_date": "",
            }
        )

    _rewrite_csv(
        directory,
        "receivables.csv",
        add_receivable,
        update_row_count=True,
    )

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "receivable_reconciliation_failed"


def test_no_bar_availability_chain_requires_real_dividend_adjustment(
    tmp_path,
):
    directory_run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path / "inputs",
            market_dates=(date(2026, 7, 16),),
            market_opens=(Decimal("10"),),
            market_closes=(Decimal("10"),),
        )
    )
    directory = export_portfolio_run(
        directory_run,
        tmp_path / "outputs",
    )

    def forge_no_bar_state(rows):
        rows[0]["carried_sessions"] = "99"
        rows[0]["adjustment_reason"] = "cash_dividend"

    _rewrite_csv(directory, "availability.csv", forge_no_bar_state)

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "position_reconciliation_failed"


def test_metrics_are_recomputed_independently(tmp_path):
    directory, _run_id = _artifact(tmp_path)
    _rewrite_json(
        directory,
        "metrics.json",
        lambda payload: payload.__setitem__("total_return", "0"),
    )

    with pytest.raises(PortfolioArtifactError) as captured:
        verify_portfolio_artifact(directory)

    assert captured.value.code == "metric_recomputation_failed"


def test_verifier_does_not_import_forbidden_producer_modules():
    module = importlib.import_module("aquant.portfolio.verify")
    source = Path(module.__file__).read_text(encoding="utf-8")

    assert "aquant.portfolio.export" not in source
    assert "run_verified_portfolio" not in source
    assert "compute_portfolio_metrics" not in source

    script = textwrap.dedent(
        """
        import sys
        import aquant.portfolio.verify

        forbidden = {
            "aquant.portfolio.export",
            "aquant.portfolio.identity",
            "aquant.portfolio.metrics",
        }
        loaded = forbidden.intersection(sys.modules)
        if loaded:
            raise SystemExit("producer modules loaded: " + repr(sorted(loaded)))
        """
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
