import json

import pytest

from aquant.backtest.index import BacktestIndexError, publish_superseded_index


def _run_ids() -> tuple[str, ...]:
    return tuple(f"{value:064x}" for value in range(1, 17))


def _prepare_runs(root, run_ids):
    for run_id in run_ids:
        directory = root / run_id
        directory.mkdir(parents=True)
        (directory / "run.json").write_text(
            json.dumps({"run_id": run_id}) + "\n",
            encoding="utf-8",
        )
        (directory / "artifact_manifest.json").write_text(
            json.dumps({"run_id": run_id}) + "\n",
            encoding="utf-8",
        )


def test_superseded_index_is_atomic_idempotent_and_does_not_mutate_runs(tmp_path):
    run_ids = _run_ids()
    _prepare_runs(tmp_path, run_ids)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    first = publish_superseded_index(tmp_path, run_ids)
    second = publish_superseded_index(tmp_path, run_ids)

    assert first == second == tmp_path / "index.json"
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["superseded"]["status"] == "superseded_semantic_bug"
    assert payload["superseded"]["run_ids"] == list(run_ids)
    assert payload["superseded"]["reasons"] == [
        "missing_corporate_actions",
        "ex_right_reference_price",
        "fixed_one_lot_baseline",
    ]
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name != "index.json"
    }
    assert before == after


@pytest.mark.parametrize(
    "change",
    ["duplicate", "missing", "unknown_directory", "symlink_index"],
)
def test_superseded_index_rejects_unsafe_or_inexact_run_sets(tmp_path, change):
    run_ids = _run_ids()
    _prepare_runs(tmp_path, run_ids)
    supplied = run_ids
    if change == "duplicate":
        supplied = (*run_ids[:-1], run_ids[0])
    elif change == "missing":
        supplied = run_ids[:-1]
    elif change == "unknown_directory":
        (tmp_path / ("f" * 64)).mkdir()
    else:
        external = tmp_path / "external"
        external.write_text("outside", encoding="utf-8")
        (tmp_path / "index.json").symlink_to(external)

    with pytest.raises(BacktestIndexError):
        publish_superseded_index(tmp_path, supplied)
