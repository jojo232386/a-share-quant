# a-share-quant v0.2 Gate E Isolated Release Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify a fully offline, two-environment replay of the frozen 10-symbol A-share shared-cash portfolio from one sealed `a-share-quant 0.2.0` wheel.

**Architecture:** Keep the existing portfolio engine unchanged. Add a strict Gate E configuration contract, a `run-config` adapter around the existing portfolio CLI, isolated-input and environment controllers, and an independent Gate E auditor/trust manifest. Candidate A is reviewed and anchored in Git before environment B is allowed to run.

**Tech Stack:** Python 3.11.15, uv 0.11.23, pytest, Hatchling wheel builds, macOS `sandbox-exec`, existing `aquant.portfolio` engine/export/verifier, canonical JSON and SHA-256.

---

## Scope and file map

Create:

- `src/aquant/gate_e/__init__.py` — public Gate E types and entry points.
- `src/aquant/gate_e/config.py` — strict canonical configuration loading.
- `src/aquant/gate_e/inputs.py` — frozen-input verification, quarantine and A/B copy.
- `src/aquant/gate_e/environment.py` — wheel, wheelhouse and sandboxed venv controls.
- `src/aquant/gate_e/audit.py` — date, no-bar, accounting and byte-comparison checks.
- `src/aquant/gate_e/trust.py` — external trust manifest parsing and verification.
- `src/aquant/gate_e/replay.py` — stage orchestration and progress events.
- `src/aquant/gate_e/cli.py` — safe controller CLI.
- `configs/releases/v0.2_gate_e.json` — immutable machine configuration.
- `scripts/verify_v02_gate_e.sh` — thin, offline-aware shell entry.
- `tests/unit/test_gate_e_config.py`
- `tests/unit/test_gate_e_inputs.py`
- `tests/unit/test_gate_e_environment.py`
- `tests/unit/test_gate_e_audit.py`
- `tests/unit/test_gate_e_trust.py`
- `tests/unit/test_gate_e_replay.py`
- `release/v0.2-gate-e/deviations/manifest.jsonl.lock.quarantined`
- `outputs/Gate_E冻结输入偏差记录.md`

Modify:

- `pyproject.toml` — distribution version `0.2.0`, `aquant-gate-e` entry point.
- `uv.lock` — synchronized project metadata.
- `src/aquant/portfolio_cli.py` — config-only Gate E execution mode.
- `src/aquant/portfolio/verify.py` — named 13/12 file counts.
- `tests/unit/test_portfolio_cli.py`
- `tests/unit/test_portfolio_verify.py`

Generated outside Git until the trust-anchor checkpoint:

- Gate E project wheel and sealed wheelhouse.
- Environment A/B roots and raw logs.
- Candidate A audit bundle.

Create only after candidate A receives independent PASS:

- `release/v0.2-gate-e/trust_manifest.json`
- `outputs/Work_Buddy候选A复核_v0.2_Gate_E.md`
- `outputs/Work_Buddy信任锚复核_v0.2_Gate_E.md`
- `outputs/Codex自审_v0.2_Gate_E.md`
- `outputs/Work_Buddy代码与审计包复核_v0.2_Gate_E.md`
- `outputs/A股量化项目_v0.2_Gate_E交付与验收.md`

### Task 1: Freeze distribution identity at 0.2.0

**Files:**
- Modify: `pyproject.toml:7`
- Modify: `uv.lock:11-12`
- Create: `tests/unit/test_gate_e_package_identity.py`

- [ ] **Step 1: Write the failing package-identity test**

```python
from __future__ import annotations

import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[2]


def test_gate_e_distribution_identity_is_v02():
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    lock_text = (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8")

    assert project["project"]["name"] == "a-share-quant"
    assert project["project"]["version"] == "0.2.0"
    assert (
        'name = "a-share-quant"\nversion = "0.2.0"'
        in lock_text
    )
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_package_identity.py
```

Expected: FAIL because both files still say `0.1.0`.

- [ ] **Step 3: Change project version and refresh the lock offline**

Change `pyproject.toml`:

```toml
[project]
name = "a-share-quant"
version = "0.2.0"
```

Run:

```bash
UV_OFFLINE=1 uv lock
uv lock --check
```

Expected: project package in `uv.lock` is `0.2.0`; dependency versions do not change.

- [ ] **Step 4: Verify GREEN and the v0.1 runtime boundary**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_package_identity.py \
  tests/unit/test_release_manifest.py
git diff --exit-code v0.1-research -- \
  src/aquant/backtest src/aquant/data src/aquant/rules \
  src/aquant/backtest_cli.py
```

Expected: tests PASS and frozen v0.1 production-path diff is empty.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock tests/unit/test_gate_e_package_identity.py
git commit -m "build: set v02 distribution identity"
```

### Task 2: Implement the strict canonical Gate E configuration

**Files:**
- Create: `src/aquant/gate_e/__init__.py`
- Create: `src/aquant/gate_e/config.py`
- Create: `tests/gate_e_support.py`
- Create: `tests/unit/test_gate_e_config.py`

- [ ] **Step 1: Write strict-type and canonical-byte failing tests**

```python
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from aquant.gate_e.config import (
    GateEConfigError,
    canonical_config_bytes,
    load_gate_e_config,
)

PROJECT_ROOT = Path(__file__).parents[2]
TESTS_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(TESTS_ROOT))
gate_e_support = importlib.import_module("gate_e_support")
sys.path.pop(0)
valid_gate_e_payload = gate_e_support.valid_gate_e_payload


def _valid_payload() -> dict[str, object]:
    return valid_gate_e_payload(PROJECT_ROOT)


def test_decimal_json_numbers_are_rejected(tmp_path):
    payload = _valid_payload()
    payload["gross_target_weight"] = 0.95
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GateEConfigError) as captured:
        load_gate_e_config(path)

    assert captured.value.code == "invalid_decimal_text"


def test_config_bytes_are_canonical(tmp_path):
    payload = _valid_payload()
    path = tmp_path / "config.json"
    expected = canonical_config_bytes(payload)
    path.write_bytes(expected)

    config = load_gate_e_config(path)

    assert config.canonical_bytes == expected
    assert config.gross_target_weight.as_tuple().exponent == -2
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_config.py
```

Expected: import error because `aquant.gate_e.config` does not exist.

- [ ] **Step 3: Implement strict loader primitives**

Create these public interfaces in `src/aquant/gate_e/config.py`:

```python
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath


_HASH_RE = re.compile(r"[0-9a-f]{64}")
_DECIMAL_FIELDS = frozenset(
    {
        "gross_target_weight",
        "stock_commission_rate",
        "stock_minimum_commission_yuan",
        "etf_commission_rate",
        "etf_minimum_commission_yuan",
    }
)


class GateEConfigError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class GateEConfig:
    payload: dict[str, object]
    canonical_bytes: bytes
    config_sha256: str
    gross_target_weight: Decimal
    signal_date: date
    end_date: date
    post_end_validation_date: date


def canonical_config_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _decimal_text(value: object) -> Decimal:
    if type(value) is not str:
        raise GateEConfigError(
            "invalid_decimal_text",
            "decimal fields must be canonical strings",
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise GateEConfigError(
            "invalid_decimal_text",
            "decimal fields must be canonical strings",
        ) from exc
    if not parsed.is_finite() or str(parsed) != value:
        raise GateEConfigError(
            "invalid_decimal_text",
            "decimal fields must be canonical strings",
        )
    return parsed


def load_gate_e_config(path: Path) -> GateEConfig:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateEConfigError(
            "invalid_config",
            "Gate E config is invalid",
        ) from exc
    if type(payload) is not dict or raw != canonical_config_bytes(payload):
        raise GateEConfigError(
            "noncanonical_config",
            "Gate E config must use canonical JSON bytes",
        )
    decimals = {
        key: _decimal_text(payload.get(key))
        for key in _DECIMAL_FIELDS
    }
    signal = date.fromisoformat(str(payload.get("signal_date")))
    end = date.fromisoformat(str(payload.get("end_date")))
    post_end = date.fromisoformat(
        str(payload.get("post_end_validation_date"))
    )
    if (
        payload.get("project_name") != "a-share-quant"
        or payload.get("project_version") != "0.2.0"
        or payload.get("gate") != "E"
        or payload.get("strategy") != "buy_and_hold"
        or signal != date(2018, 1, 2)
        or end != date(2026, 7, 23)
        or post_end != date(2026, 7, 24)
    ):
        raise GateEConfigError(
            "unexpected_config",
            "Gate E config differs from the approved release contract",
        )
    return GateEConfig(
        payload=payload,
        canonical_bytes=raw,
        config_sha256=hashlib.sha256(raw).hexdigest(),
        gross_target_weight=decimals["gross_target_weight"],
        signal_date=signal,
        end_date=end,
        post_end_validation_date=post_end,
    )
```

Implement the rest of the contract explicitly:

- `_require_exact_keys()` compares the payload key set with one fixed
  `GATE_E_CONFIG_KEYS` constant and rejects both missing and unknown keys;
- `_safe_relative_path()` accepts only non-empty POSIX relative paths, rejects
  `.`/`..`, absolute paths, backslashes and duplicate normalized paths;
- `_sha256_text()` accepts lowercase 64-character SHA-256 strings only;
- `_schedule()` accepts only ordered two-item string pairs, parses dates and
  canonical Decimal text, rejects duplicate or non-increasing dates, and
  requires the two approved stamp-duty entries or the two approved transfer-fee
  entries exactly;
- `_symbol_mapping()` requires the exact sorted ten-symbol tuple and a
  one-to-one mapping of every symbol to one SHA-256 value;
- `_input_file_mapping()` requires exactly the 25 release-manifest paths, safe
  relative paths, sorted keys and SHA-256 values;
- `_fee_policy_digest()` rebuilds the existing fee-policy canonical payload
  from the five Decimal strings and both schedules and requires the approved
  digest;
- `load_gate_e_config()` verifies the release-manifest SHA, `uv.lock` SHA,
  universe/calendar IDs, schema versions, Python version, integer-only
  `initial_cash_fen`/`max_entry_attempts`, all fixed dates and all fixed
  economic values before returning;
- `GateEConfig.to_portfolio_namespace()` creates the complete legacy
  `_load_run_arguments()` namespace using only the validated payload and the
  supplied controlled project root;
- returned mappings and sequences use immutable sorted tuples or
  `MappingProxyType`, so callers cannot mutate validated state.

Add named negative tests for every bullet above, plus a test that `"5.00"`
round-trips unchanged and a test that the fee-policy digest changes if any
single rate string changes.

`tests/gate_e_support.py::valid_gate_e_payload()` must assemble a complete
successful contract from the SHA-verified frozen release manifest: the exact
sorted 10 symbols, both 10-item snapshot maps and all 25 input paths/hashes.
It must use the real universe/calendar IDs and fixed Gate E values, not dummy
hashes. Every negative test starts from this one valid 10/25 fixture and mutates
exactly one field.

- [ ] **Step 4: Run focused tests and Ruff**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_config.py
UV_OFFLINE=1 uv run --no-sync ruff check \
  src/aquant/gate_e tests/gate_e_support.py \
  tests/unit/test_gate_e_config.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/gate_e tests/gate_e_support.py \
  tests/unit/test_gate_e_config.py
git commit -m "feat: add strict gate e config contract"
```

### Task 3: Add the config-only portfolio entry

**Files:**
- Modify: `src/aquant/portfolio_cli.py`
- Modify: `src/aquant/portfolio/identity.py`
- Modify: `tests/unit/test_portfolio_cli.py`
- Modify: `tests/unit/test_portfolio_identity.py`
- Modify: `tests/portfolio_gate_c_support.py`

- [ ] **Step 1: Write failing `run-config` tests**

```python
def test_gate_e_run_config_rejects_parameter_overrides(tmp_path, capsys):
    case = materialize_portfolio_cli_case(tmp_path)
    config = write_gate_e_cli_config(case)

    assert (
        main(
            [
                "run-config",
                "--config",
                config.relative_to(case.project_root).as_posix(),
                "--initial-cash-fen",
                "1",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error_code"] == (
        "invalid_arguments"
    )


def test_gate_e_run_config_is_root_independent(tmp_path, capsys, monkeypatch):
    first = materialize_gate_e_config_case(tmp_path / "first")
    second = materialize_gate_e_config_case(tmp_path / "second")

    monkeypatch.chdir(first.project_root)
    assert main(["run-config", "--config", first.config_relative]) == 0
    first_payload = json.loads(capsys.readouterr().out)

    monkeypatch.chdir(second.project_root)
    assert main(["run-config", "--config", second.config_relative]) == 0
    second_payload = json.loads(capsys.readouterr().out)

    assert first_payload["run_id"] == second_payload["run_id"]
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_portfolio_cli.py -k 'run_config'
```

Expected: FAIL because `run-config` is not a supported command.

- [ ] **Step 3: Add a separate parser branch and adapter**

Add to `_parser()`:

```python
run_config = subparsers.add_parser(
    "run-config",
    help="run the immutable a-share-quant v0.2 Gate E config",
)
run_config.add_argument("--config", required=True)
```

Add a function that loads the config relative to the current safe project root,
constructs the existing run namespace and calls `_load_run_arguments()`:

```python
def _run_config_command(args) -> dict[str, object]:
    with _safe_project_root(".") as (
        root,
        root_descriptor,
        root_metadata,
    ):
        config_path, _relative = _safe_relative_path(
            root,
            root_descriptor,
            args.config,
            kind="file",
        )
        config = load_gate_e_config(config_path)
        namespace = config.to_portfolio_namespace(project_root=str(root))
        run, relative_directory = _load_run_arguments(
            namespace,
            root=root,
            root_descriptor=root_descriptor,
            root_metadata=root_metadata,
            fee_policy_override=config.to_fee_policy(),
            post_end_validation_date=config.post_end_validation_date,
        )
        return {
            "artifact_directory": relative_directory.as_posix(),
            "run_id": run.identity.run_id,
            "status": "ok",
            "symbol_count": len(run.result.targets),
        }
```

Dispatch `run-config` separately in `main()`. Do not add economic override flags
to this subparser. Extend `_load_run_arguments()` with optional
`fee_policy_override` and `post_end_validation_date` keyword-only parameters.
The old `run` path passes neither and remains byte-compatible. `run-config`
must:

- preserve every Decimal source string, including `"5.00"`;
- pass both date-effective statutory schedules explicitly to
  `make_fee_policy()` and require policy digest
  `6935d9e8727417370a69dd97c021514f5517b4f22107fb89b548145195dfa782`;
- assert
  `calendar.next_session(end_date) == post_end_validation_date` before the
  engine call, while never putting the validation date into `PortfolioConfig`;
- execute both the engine run and installed artifact verification inside
  `offline_network_guard()` as the Python-level network barrier.

Add tests that the old `run` case retains its prior run ID, the formal `"5.00"`
fee strings produce the approved digest, a changed statutory rate is rejected,
the validation date never appears in the economic config/result, and a mocked
socket call from both formal run and verify paths is denied.

Because `src/aquant/gate_e/config.py` now interprets the identity-bearing
economic configuration, add that exact path to
`portfolio.identity._IMPLEMENTATION_FILES` and its explicit expected-file
regression tuple. Add a mutation probe proving that changing only this file
changes the implementation digest. Do this before candidate A; never alter the
fingerprinted file set after the candidate is anchored.

- [ ] **Step 4: Verify focused and existing CLI tests**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_config.py \
  tests/unit/test_portfolio_cli.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/portfolio_cli.py src/aquant/portfolio/identity.py \
  tests/unit/test_portfolio_cli.py tests/unit/test_portfolio_identity.py \
  tests/portfolio_gate_c_support.py
git commit -m "feat: run gate e from one frozen config"
```

### Task 4: Correct the artifact and payload count contract

**Files:**
- Modify: `src/aquant/portfolio/verify.py:213-222,2883-2895`
- Modify: `src/aquant/portfolio_cli.py:545-569`
- Modify: `tests/unit/test_portfolio_verify.py`
- Modify: `tests/unit/test_portfolio_cli.py`

- [ ] **Step 1: Write failing named-count tests**

```python
def test_verified_artifact_reports_13_artifacts_and_12_payloads(tmp_path):
    directory, _run_id = _artifact(tmp_path)

    verified = verify_portfolio_artifact(directory)

    assert verified.artifact_file_count == 13
    assert verified.payload_file_count == 12
    assert verified.file_count == 13
```

Add a CLI assertion:

```python
assert payload["artifact_file_count"] == 13
assert payload["payload_file_count"] == 12
assert payload["file_count"] == 13
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_portfolio_verify.py \
  tests/unit/test_portfolio_cli.py -k 'file_count or artifact_count'
```

Expected: FAIL because `file_count` is currently 12 and named fields are absent.

- [ ] **Step 3: Implement explicit count fields**

Change the verified result:

```python
@dataclass(frozen=True)
class VerifiedPortfolioArtifact:
    run_id: str
    status: str
    artifact_manifest_sha256: str
    artifact_file_count: int
    payload_file_count: int
    file_count: int
    trade_count: int
    row_counts: tuple[tuple[str, int], ...]
```

Return:

```python
artifact_file_count=len(_ARTIFACT_FILES),
payload_file_count=len(_PAYLOAD_FILES),
file_count=len(_ARTIFACT_FILES),
```

Expose all three fields from `_verify_command()`.

- [ ] **Step 4: Verify**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_portfolio_verify.py \
  tests/unit/test_portfolio_cli.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/portfolio/verify.py src/aquant/portfolio_cli.py \
  tests/unit/test_portfolio_verify.py tests/unit/test_portfolio_cli.py
git commit -m "fix: report complete portfolio artifact counts"
```

### Task 5: Quarantine the stale lock and stage exactly 25 inputs

**Files:**
- Create: `src/aquant/gate_e/inputs.py`
- Create: `tests/unit/test_gate_e_inputs.py`
- Create later in this task:
  `release/v0.2-gate-e/deviations/manifest.jsonl.lock.quarantined`
- Create: `outputs/Gate_E冻结输入偏差记录.md`

- [ ] **Step 1: Write failure and safe-quarantine tests**

```python
def test_extra_frozen_lock_is_rejected_before_quarantine(tmp_path):
    release_root = materialize_gate_e_release(tmp_path)
    lock = (
        release_root
        / "inputs/data/manifests/manifest.jsonl.lock"
    )
    lock.touch()

    with pytest.raises(GateEInputError) as captured:
        verify_gate_e_release_inputs(release_root)

    assert captured.value.code == "input_file_set_mismatch"


def test_quarantine_requires_exact_empty_single_link_lock(tmp_path):
    release_root = materialize_gate_e_release(tmp_path)
    lock = (
        release_root
        / "inputs/data/manifests/manifest.jsonl.lock"
    )
    lock.write_bytes(b"x")

    with pytest.raises(GateEInputError) as captured:
        quarantine_manifest_lock(
            release_root,
            tmp_path / "quarantine/manifest.jsonl.lock.quarantined",
        )

    assert captured.value.code == "unexpected_lock_file"
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_inputs.py
```

Expected: import error.

- [ ] **Step 3: Implement exact lock and allowlist behavior**

Public lifecycle:

```python
def stage_gate_e_input_root(
    release_root: Path,
    destination: Path,
) -> GateEInputCopy: ...


def verify_gate_e_input_roots_independent(
    release_root: Path,
    destination_a: Path,
    destination_b: Path,
) -> GateEInputCopies: ...
```

`quarantine_manifest_lock()` must walk the fixed source and destination
directories with held directory descriptors and `O_NOFOLLOW`, lock and bind the
exact empty single-link source inode, then use the supported platform
no-replace primitive (`renameatx_np(RENAME_EXCL)` on macOS or
`renameat2(RENAME_NOREPLACE)` on Linux) for one atomic cross-directory move.
It must never implement the move as hard-link plus later source unlink, never
overwrite a destination, and fail closed when the atomic primitive is
unavailable.

Candidate input roots are deliberately staged one at a time. Candidate A is
created and verified before any candidate B directory exists. Candidate B is
created only after the candidate-A review and trust-anchor gates. Each staging
operation must call the existing release-manifest verifier, write only the 25
declared relative paths through held directory descriptors, reject links,
re-hash the published root and reject a source/destination same-inode pair.
After B exists, `verify_gate_e_input_roots_independent()` must prove exact
three-way source/A/B inode independence for every declared file.

Every destination publication uses an atomic no-replace rename. If an error
occurs before publication, the randomly named staging root is retained as
failure evidence; code must not call path-based recursive deletion that could
delete a concurrently rebound object. If publication has happened but
post-publication binding or durability checks fail, return
`copy_partial_publication` with a sanitized machine-readable `cause_code`.
Every partial publication is a blocking Gate E result: do not consume it, do
not automatically retry it and do not automatically delete it. Copy failures
must also expose a basename-only `evidence_name` and an allowlisted
`publication_state`, so the controller can identify this invocation's retained
staging or published destination without guessing from a directory glob. Any
copy error carrying retained or published evidence must also include a
non-empty sanitized `cause_code`.

Add `verify_post_run_input_root()` and require the post-run tree to remain
exactly the 25 declared byte-identical files. No lock, temporary file, empty
directory, symlink, hard link, changed input or other sidecar is allowed.
The Gate E manifest reader must open only existing directories, use
non-blocking no-follow file access, hash the bytes it actually consumed against
the config-pinned manifest SHA-256, and verify stable descriptor/path metadata.
Add negative tests for each violation and call this verifier after every A/B
run and verification stage.

Add `main(argv: Sequence[str] | None = None) -> int` in this module with only
the `quarantine --release-root --destination` subcommand. It must resolve both
paths without following the source symlink, call the tested function, print one
canonical JSON evidence line, return a stable sanitized error code, and expose:

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

Unit tests must invoke `main()` for the successful empty single-link file and
for a non-empty, linked, symlinked, absent, wrong-path and pre-existing
destination case. They must also prove the post-run verifier accepts only the
unchanged 25-file set and rejects every sidecar, including an empty lock.

- [ ] **Step 4: Run unit tests**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_inputs.py \
  tests/unit/test_release_manifest.py
```

Expected: PASS.

- [ ] **Step 5: Resolve the real lock with a recoverable move**

First verify no process has the exact file open:

```bash
lsof -- \
  release/v0.1-research/inputs/data/manifests/manifest.jsonl.lock
```

Expected: no output.

Run the tested quarantine function against the exact source and destination,
then verify:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync python -m \
  aquant.gate_e.inputs quarantine \
  --release-root release/v0.1-research \
  --destination \
  release/v0.2-gate-e/deviations/manifest.jsonl.lock.quarantined
```

Expected: source is absent, destination is an empty regular file with SHA-256
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`,
and all 25 declared files verify.

Record exact metadata in `outputs/Gate_E冻结输入偏差记录.md`.

- [ ] **Step 6: Commit**

```bash
git add src/aquant/gate_e/inputs.py tests/unit/test_gate_e_inputs.py \
  release/v0.2-gate-e/deviations/manifest.jsonl.lock.quarantined \
  outputs/Gate_E冻结输入偏差记录.md
git commit -m "fix: isolate frozen input lock deviation"
```

### Task 6: Freeze the real ten-symbol JSON configuration

**Files:**
- Create: `configs/releases/v0.2_gate_e.json`
- Modify: `tests/unit/test_gate_e_config.py`

- [ ] **Step 1: Write the failing real-config test**

```python
def test_repository_gate_e_config_matches_frozen_release():
    config_path = (
        PROJECT_ROOT / "configs/releases/v0.2_gate_e.json"
    )
    config = load_gate_e_config(config_path)
    release = json.loads(
        (
            PROJECT_ROOT
            / "release/v0.1-research/release_manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert config.payload["symbols"] == sorted(release["market_snapshots"])
    assert config.payload["market_snapshots"] == release["market_snapshots"]
    assert (
        config.payload["corporate_action_snapshots"]
        == release["corporate_action_snapshots"]
    )
    assert config.payload["input_files"] == release["input_files"]
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_config.py::\
test_repository_gate_e_config_matches_frozen_release
```

Expected: FAIL because the tracked config is absent.

- [ ] **Step 3: Create canonical JSON**

Use the exact 10 snapshot mappings and 25 input-file mappings from the
SHA-verified `release/v0.1-research/release_manifest.json`. Add the fixed fields
from the approved design. Compute the current `uv.lock` SHA-256 after Task 1.
Use a temporary read-only helper only to print the canonical payload and its
SHA-256 for review:

```python
print(canonical_config_bytes(payload).decode(), end="")
print(hashlib.sha256(canonical_config_bytes(payload)).hexdigest(), file=sys.stderr)
```

Add the reviewed complete JSON to the repository with `apply_patch`; do not let
the helper write the tracked file. Re-read the tracked bytes through
`load_gate_e_config()` and compare them byte-for-byte with the helper output
before commit.

- [ ] **Step 4: Verify exact config and run-config**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_config.py \
  tests/unit/test_portfolio_cli.py -k 'run_config'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add configs/releases/v0.2_gate_e.json \
  tests/unit/test_gate_e_config.py
git commit -m "config: freeze v02 gate e pilot"
```

### Task 7: Build and verify a sealed wheel and wheelhouse

**Files:**
- Create: `src/aquant/gate_e/environment.py`
- Create: `tests/unit/test_gate_e_environment.py`

- [ ] **Step 1: Write failing wheel and wheelhouse tests**

```python
def test_wheel_must_be_v02_and_contain_portfolio_entry(tmp_path):
    wheel = build_project_wheel(PROJECT_ROOT, tmp_path / "dist")
    evidence = inspect_project_wheel(wheel)

    assert evidence.distribution_version == "0.2.0"
    assert evidence.portfolio_cli_present is True
    assert evidence.entry_point == (
        "aquant-portfolio = aquant.portfolio_cli:main"
    )


def test_missing_wheelhouse_dependency_fails_closed(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    with pytest.raises(GateEEnvironmentError) as captured:
        verify_wheelhouse(
            wheelhouse,
            expected_requirements={"akshare": "1.18.64"},
        )

    assert captured.value.code == "wheelhouse_incomplete"
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_environment.py
```

Expected: import error.

- [ ] **Step 3: Implement wheel evidence and sealed wheelhouse verification**

Expose:

```python
@dataclass(frozen=True)
class WheelEvidence:
    path: Path
    size: int
    sha256: str
    distribution_version: str
    portfolio_cli_present: bool
    entry_point: str


def build_project_wheel(project_root: Path, dist: Path) -> Path:
    completed = subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--no-sources",
            "--out-dir",
            str(dist),
        ],
        cwd=project_root,
        env={**sanitized_environment(), "UV_OFFLINE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise GateEEnvironmentError(
            "wheel_build_failed",
            "the Gate E wheel could not be built",
        )
    matches = tuple(dist.glob("a_share_quant-0.2.0-*.whl"))
    if len(matches) != 1:
        raise GateEEnvironmentError(
            "wheel_identity_mismatch",
            "exactly one v0.2.0 wheel is required",
        )
    return matches[0]
```

`inspect_project_wheel()` must inspect ZIP names and `.dist-info` metadata.
`verify_wheelhouse()` must reject non-wheel files, links, duplicate
name/version pairs, missing lock requirements and any hash not present in its
canonical manifest.

Wheels built by this unit test are disposable packaging probes, not the formal
Gate E project wheel. Build and seal the one formal project wheel only after
Task 11 creates the clean `implementation_commit`.

- [ ] **Step 4: Prepare the real wheelhouse before the offline core**

Export runtime requirements:

```bash
uv export --frozen --no-dev --no-emit-project \
  --format requirements.txt \
  --output-file <temporary-preparation-root>/requirements.lock.txt
```

Use the fixed Python 3.11.15 interpreter and exact commands below:

```bash
<python-3.11.15-root>/bin/python3.11 \
  -m venv <temporary-preparation-root>/prep-venv
<temporary-preparation-root>/prep-venv/bin/python -m pip wheel \
  --require-hashes \
  --wheel-dir <temporary-preparation-root>/wheelhouse \
  --requirement <temporary-preparation-root>/requirements.lock.txt
```

Network is allowed only in this preparation step; do not change VPN, proxy,
DNS or system settings. After preparation, reject sdists, links and duplicate
distribution/version pairs, then seal a canonical wheelhouse manifest with
relative path, normalized distribution name, version, size and SHA-256 for
every wheel. Copy the finished wheelhouse into a fresh read-only directory
before any Gate E environment is created. Remove all write bits recursively,
prove a sandboxed write probe fails, and recompute the complete manifest before
and after every install/run/verify stage; any byte, mode, file-set or hash
change blocks the Gate.

Keep the original `requirements.lock.txt` and its SHA-256 as resolver/source
evidence. An upstream sdist hash is not a valid hash for a locally built wheel:
`jsonpath==0.82.2` currently exercises this boundary. After verifying the
wheelhouse, generate a second canonical
`requirements.install.lock.txt`, one current-platform wheel per line:

```text
normalized-name==version --hash=sha256:<exact-wheel-sha256>
```

This platform install lock must be derived from the exact sealed wheel bytes,
verified against the wheelhouse manifest, stored outside the wheelhouse and
made read-only. Record the hashes of the original resolver lock, the platform
install lock and the wheelhouse manifest. No sdist may remain in the sealed
wheelhouse.

Do not place the sealed trust root under macOS File Provider, iCloud Drive or
another directory that asynchronously rewrites mode bits. A preparation copy
under such a directory is not formal evidence even when its hashes match.
Use a non-synchronized local root, deny sandbox writes to the wheelhouse and
control files, and recheck both hashes and modes after a delay as well as
before/after every install.

Expected: all locked macOS arm64 runtime dependencies are present as wheels.

- [ ] **Step 5: Verify with an empty uv cache and no index**

Run the test-created install probe with:

```bash
UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never \
uv pip sync --python <probe-venv>/bin/python \
  --require-hashes --only-binary :all: \
  --no-index --find-links <sealed-wheelhouse> \
  <sealed-root>/requirements.install.lock.txt
UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never \
uv pip install --python <probe-venv>/bin/python \
  --no-deps --only-binary :all: --no-index \
  <sealed-project-wheel>
```

Both commands run inside `sandbox-exec` with `deny network*`. Expected:
dependencies and the exact project wheel install successfully, `uv pip check`
passes, and the installed distribution is `a-share-quant==0.2.0`. Removing any
one required dependency wheel or changing the project-wheel hash makes the
same sequence fail.

- [ ] **Step 6: Commit**

```bash
git add src/aquant/gate_e/environment.py \
  tests/unit/test_gate_e_environment.py
git commit -m "feat: seal gate e wheel inputs"
```

Do not commit the wheelhouse binaries.

### Task 8: Create two sandboxed, independent execution environments

**Files:**
- Modify: `src/aquant/gate_e/environment.py`
- Modify: `tests/unit/test_gate_e_environment.py`

- [ ] **Step 1: Write failing isolation and network tests**

```python
def test_environment_roots_and_mutable_files_are_independent(tmp_path):
    first = make_environment_layout(tmp_path / "a")
    second = make_environment_layout(tmp_path / "b")

    assert first.root != second.root
    assert first.home != second.home
    assert first.uv_cache != second.uv_cache
    assert first.output_root != second.output_root
    assert first.root.stat().st_ino != second.root.stat().st_ino


def test_sandbox_denies_a_proven_reachable_socket(tmp_path):
    layout = make_environment_layout(tmp_path / "a")
    with reachable_local_listener() as port:
        command = [
            str(layout.python),
            "-c",
            (
                "import socket;"
                f"socket.create_connection(('127.0.0.1',{port}),timeout=1)"
            ),
        ]
        baseline = run_controlled(layout, command)
        completed = run_sandboxed(layout, command)

    assert baseline.returncode == 0
    assert completed.returncode != 0


def test_sandbox_cannot_read_repository_sentinel(tmp_path):
    layout = make_environment_layout(tmp_path / "a")
    sentinel = PROJECT_ROOT / ".gate-e-source-read-probe"
    sentinel.write_text("denied", encoding="utf-8")
    try:
        completed = run_sandboxed(
            layout,
            [str(layout.python), "-c", f"open({str(sentinel)!r}).read()"],
        )
    finally:
        sentinel.unlink()

    assert completed.returncode != 0
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_environment.py -k 'independent or sandbox'
```

Expected: FAIL because layout and sandbox helpers are absent.

- [ ] **Step 3: Implement controlled layouts and environment**

The sanitizer must start from an allowlist rather than `os.environ.copy()`:

```python
def execution_environment(
    layout: GateEEnvironmentLayout,
    *,
    hash_seed: str,
) -> dict[str, str]:
    return {
        "HOME": str(layout.home),
        "XDG_CACHE_HOME": str(layout.xdg_cache),
        "UV_CACHE_DIR": str(layout.uv_cache),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": "/dev/null",
        "PYTHONHASHSEED": hash_seed,
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "Asia/Shanghai",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
```

Do not add `PYTHONPATH`. Build a `sandbox-exec` profile with `allow default`,
`deny network*` and a deny rule for the repository source path. Allow the
read-only uv-managed Python 3.11.15 base and each environment's own paths.
The controller must invoke the two venv CLIs by absolute path, never by PATH:
`<environment>/venv/bin/aquant-portfolio` and
`<environment>/venv/bin/aquant-gate-e`.

Copy the canonical config into each independent project root at
`configs/releases/v0.2_gate_e.json`, verify its hash after copy, and prove the
A/B config files do not share an inode with each other or the tracked source.
This control file is additional to, and not counted among, the 25 declared
frozen input files.

- [ ] **Step 4: Prove site-packages and CLI isolation**

Tests must assert:

- `aquant.__file__` is under the environment venv;
- `sys.path` excludes repository and user site;
- A/B package name-version lists are byte-identical;
- `uv pip check` passes;
- absolute `<venv>/bin/aquant-portfolio --help` and
  `<venv>/bin/aquant-gate-e --help` succeed without `PYTHONPATH`;
- A/B mutable venv files and input copies do not share inodes.
- the copied A/B config bytes and hashes equal the tracked canonical config,
  while all three inodes differ;
- a direct Python `socket` call made within each installed formal run/verify
  path is rejected by `offline_network_guard()`;
- the reachable-listener control succeeds without the OS sandbox and fails
  with it, and the repository sentinel is unreadable only inside the sandbox.

Task 8 can prove the environment and installed `aquant-portfolio` entry point
with its disposable packaging probe. The `aquant-gate-e` entry point does not
exist until Task 11 creates the controller; do not add a stub merely to satisfy
this ordering. Its absolute-path/no-`PYTHONPATH` probe remains mandatory in
Task 11 and in the formal Candidate A/B installation.

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_environment.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/gate_e/environment.py \
  tests/unit/test_gate_e_environment.py
git commit -m "feat: isolate gate e replay environments"
```

### Task 9: Independently audit dates, no-bar evidence and accounting

**Files:**
- Create: `src/aquant/gate_e/audit.py`
- Create: `tests/unit/test_gate_e_audit.py`

- [ ] **Step 1: Write failing adversarial tests**

```python
def test_post_end_cash_event_is_rejected(tmp_path):
    bundle = materialize_gate_e_bundle(tmp_path)
    append_csv_row(
        bundle / "cash.csv",
        {"session": "2026-07-24"},
    )

    with pytest.raises(GateEAuditError) as captured:
        audit_gate_e_bundle(bundle, expected_run_id=None)

    assert captured.value.code == "post_end_economic_event"


def test_plan_date_after_end_is_allowed_for_unpaid_receivable(tmp_path):
    bundle = materialize_gate_e_bundle(
        tmp_path,
        unpaid_receivable=True,
        actual_cash_date="2026-07-24",
    )

    audit = audit_gate_e_bundle(bundle, expected_run_id=None)

    assert audit.ending_receivable_fen > 0


def test_common_date_intersection_cannot_hide_no_bar(tmp_path):
    case = materialize_gate_e_no_bar_case(
        tmp_path,
        missing={"600030": 17, "600900": 11},
    )

    audit = audit_gate_e_inputs(case.config, case.project_root)

    assert audit.no_bar_counts == (("600030", 17), ("600900", 11))
    assert audit.no_bar_total == 28
```

Add parameterized failures that place `2026-07-24` in every economic field:

- `targets.signal_date`;
- `orders.original_signal_date`, `intent_session` and `execution_session`;
- `fills.execution_session`;
- `positions.session`;
- `lots.acquired_date`;
- `cash.session`;
- `equity.session`;
- `receivables.registered_date` and non-empty `paid_date`;
- `corporate_actions.ex_date`;
- `availability.session`;
- the metrics observation window or final session.

Keep separate positive tests for allowed plan metadata:
`lots.available_date` and an unpaid receivable's `source_payable_date` or
`actual_cash_date`. Add a metamorphic test that changes only the synthetic
2026-07-24 market row and proves every pre-end economic value and metric is
unchanged after identity fields are excluded. Because Gate C deliberately binds
the complete frozen market input, the changed input must produce a different
`input_closure_digest` and run ID. An in-place row mutation that keeps the old
provenance must fail closed instead of producing any run.

For no-bar, compare the exact missing-date tuple per symbol and every
`carried_sessions` value with an independent calendar-minus-symbol-bars
reconstruction. The 17/11/28 counts alone are insufficient. For accounting,
parameterize one-byte/value mutations over every term of each of the three
identities and require the corresponding stable reconciliation error.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_audit.py
```

Expected: import error.

- [ ] **Step 3: Implement independent audits**

Expose immutable results:

```python
@dataclass(frozen=True)
class GateEAccountingAudit:
    initial_cash_fen: int
    invested_notional_fen: int
    paid_fees_fen: int
    dividend_cash_paid_fen: int
    ending_cash_fen: int
    gross_target_notional_fen: int
    allocation_rounding_fen: int
    ordinary_lot_rounding_fen: int
    fee_lot_reduction_fen: int
    pending_uninvested_fen: int
    expired_uninvested_fen: int
    ending_position_market_value_fen: int
    ending_receivable_fen: int
    ending_equity_fen: int
```

Require all three exact identities:

```python
ending_cash_fen == (
    initial_cash_fen
    - invested_notional_fen
    - paid_fees_fen
    + dividend_cash_paid_fen
)
gross_target_notional_fen == (
    invested_notional_fen
    + allocation_rounding_fen
    + ordinary_lot_rounding_fen
    + fee_lot_reduction_fen
    + pending_uninvested_fen
    + expired_uninvested_fen
)
ending_equity_fen == (
    ending_cash_fen
    + ending_position_market_value_fen
    + ending_receivable_fen
)
```

Recompute no-bar dates from the full official calendar and each single-symbol
bar set. Do not derive them from the output's availability rows alone. Rebuild
metrics only from the accepted equity rows through 2026-07-23; a persisted
metric is never accepted as its own evidence.

- [ ] **Step 4: Verify**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_audit.py \
  tests/unit/test_portfolio_accounting.py \
  tests/unit/test_portfolio_metrics.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/gate_e/audit.py tests/unit/test_gate_e_audit.py
git commit -m "feat: independently audit gate e economics"
```

### Task 10: Implement the external trust manifest

**Files:**
- Create: `src/aquant/gate_e/trust.py`
- Create: `tests/unit/test_gate_e_trust.py`

- [ ] **Step 1: Write failing trust-boundary tests**

```python
def test_trust_manifest_rejects_wrong_run_id(tmp_path):
    bundle = materialize_gate_e_bundle(tmp_path)
    evidence = materialize_gate_e_trust_evidence(tmp_path, artifact=bundle)
    trust = write_gate_e_trust(
        tmp_path / "trust.json",
        evidence=evidence,
        expected_run_id="0" * 64,
    )

    with pytest.raises(GateETrustError) as captured:
        verify_gate_e_trust(trust, evidence)

    assert captured.value.code == "trusted_run_id_mismatch"


def test_trust_manifest_binds_13_files(tmp_path):
    bundle = materialize_gate_e_bundle(tmp_path)
    evidence = materialize_gate_e_trust_evidence(tmp_path, artifact=bundle)
    trust = write_gate_e_trust(
        tmp_path / "trust.json",
        evidence=evidence,
        expected_run_id=bundle.name,
    )

    verified = verify_gate_e_trust(trust, evidence)

    assert verified.artifact_file_count == 13
    assert tuple(name for name, _digest in verified.files) == (
        "artifact_manifest.json",
        "availability.csv",
        "cash.csv",
        "corporate_actions.csv",
        "equity.csv",
        "fills.csv",
        "lots.csv",
        "metrics.json",
        "orders.csv",
        "positions.csv",
        "receivables.csv",
        "run.json",
        "targets.csv",
    )
```

Define the complete verification context:

```python
@dataclass(frozen=True)
class GateETrustEvidence:
    implementation_commit: str
    project_wheel: Path
    uv_lock: Path
    python_executable: Path
    wheelhouse_root: Path
    wheelhouse_manifest: Path
    v01_tag_commit: str
    v01_release_manifest: Path
    config: Path
    artifact: Path
```

Add one mutation test for every field or evidence class: implementation commit,
project-wheel name/size/hash, lock hash, Python path/hash/version, each
wheelhouse entry and manifest hash, v0.1 tag commit/release-manifest hash,
complete config bytes/hash, expected run ID, all 13 artifact names/sizes/hashes
and expected row/session/symbol/no-bar counts. Every mismatch must fail closed
with a stable specific error code.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_trust.py
```

Expected: import error.

- [ ] **Step 3: Implement strict canonical trust parsing**

The trust file must bind the pre-anchor implementation commit, wheel,
`uv.lock`, Python, wheelhouse files, v0.1 trust root, complete config,
expected run ID, 13 artifact hashes and expected counts. Reject unknown keys,
JSON numbers for Decimal values, links, missing files, extra files and any
hash mismatch. `verify_gate_e_trust()` must require `GateETrustEvidence`; an
artifact path by itself is never a sufficient trust context.

Public verification result:

```python
@dataclass(frozen=True)
class VerifiedGateETrust:
    implementation_commit: str
    expected_run_id: str
    artifact_file_count: int
    payload_file_count: int
    files: tuple[tuple[str, str], ...]
    trust_sha256: str
```

- [ ] **Step 4: Verify**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_trust.py \
  tests/unit/test_portfolio_verify.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/gate_e/trust.py tests/unit/test_gate_e_trust.py
git commit -m "feat: verify external gate e trust anchors"
```

### Task 11: Orchestrate candidate A without self-approval

**Files:**
- Create: `src/aquant/gate_e/replay.py`
- Create: `src/aquant/gate_e/cli.py`
- Create: `scripts/verify_v02_gate_e.sh`
- Create: `tests/unit/test_gate_e_replay.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Write failing stage-order tests**

```python
def test_environment_b_cannot_run_before_anchor_pass(tmp_path):
    replay = materialize_gate_e_replay(tmp_path)

    with pytest.raises(GateEReplayError) as captured:
        replay_environment_b(
            replay,
            trust_anchor_commit=None,
            approval_commit=None,
        )

    assert captured.value.code == "trust_anchor_not_approved"


def test_candidate_a_emits_fixed_progress_order(tmp_path):
    replay = materialize_gate_e_replay(tmp_path)
    events: list[str] = []

    run_candidate_a(replay, progress=lambda event: events.append(event.stage))

    assert events == [
        "trust_roots_verified",
        "wheel_verified",
        "environment_a_installed",
        "inputs_a_verified",
        "candidate_a_run",
        "candidate_a_reversed",
        "candidate_a_audited",
    ]
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_replay.py
```

Expected: import error.

- [ ] **Step 3: Implement a staged controller**

The controller must:

1. require a clean implementation commit;
2. verify the v0.1 tag commit and release-manifest SHA;
3. verify the tracked Gate E config;
4. verify the wheel and wheelhouse;
5. create environment A with hash seed `101`;
6. copy only 25 declared inputs plus the separately controlled canonical config;
7. verify copied hashes and distinct inodes before execution;
8. invoke the absolute installed
   `<environment-a>/venv/bin/aquant-portfolio run-config`;
9. invoke the absolute installed
   `<environment-a>/venv/bin/aquant-portfolio verify`;
10. run the independent input-root, date, no-bar and accounting audits;
11. re-hash the read-only wheelhouse and project wheel;
12. stop and return candidate evidence without creating trust.

Stable progress type:

```python
@dataclass(frozen=True)
class GateEProgressEvent:
    stage: str
    completed: int
    total: int
```

The controller CLI has exactly these tested subcommands:

- `candidate-a --config --wheel --wheelhouse --workspace`;
- `audit-candidate --evidence --artifact`;
- `build-trust --evidence --approved-review` (canonical JSON to stdout only);
- `verify-trust --trust --evidence --artifact --approved-review`;
- `replay-b --trust-anchor-commit --approval-commit --trust-path --wheel --wheelhouse
  --workspace-a --workspace-b`.

`replay-b` must reject missing, unordered or unapproved anchor/approval commits,
extract trust and Candidate A review from the exact anchor plus the post-anchor
review from the exact approval commit, machine-reverify A, then and only then
create/run B. It alone
may invoke the B portfolio subprocess; a direct B `aquant-portfolio` command is
not an accepted Gate E path. Add parser and stage-order tests for every
subcommand, every required argument and the direct-B bypass.

The controller CLI writes one canonical JSON success line or one sanitized
error line. It must not expose HOME, usernames, temp paths or raw arguments.

- [ ] **Step 4: Add entry point and shell wrapper**

Add:

```toml
aquant-gate-e = "aquant.gate_e.cli:main"
```

`scripts/verify_v02_gate_e.sh` must use `set -eu`, require clean lock metadata,
and invoke only the absolute `aquant-gate-e` installed from the sealed formal
wheel. It must not set `PYTHONPATH` or fall back to repository source.

- [ ] **Step 5: Verify**

Run:

```bash
UV_OFFLINE=1 uv lock
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_replay.py \
  tests/unit/test_gate_e_environment.py \
  tests/unit/test_gate_e_audit.py \
  tests/unit/test_gate_e_trust.py
UV_OFFLINE=1 uv run --no-sync ruff check src/aquant/gate_e tests
```

Expected: PASS.

- [ ] **Step 6: Commit implementation candidate**

```bash
git add pyproject.toml uv.lock src/aquant/gate_e \
  scripts/verify_v02_gate_e.sh tests/unit/test_gate_e_*.py
git commit -m "feat: orchestrate v02 gate e candidate"
```

Record this commit as `implementation_commit`. Do not create trust yet.

### Task 12: Run candidate A and collect the test inventory

**Files:**
- Create outside Git: candidate A environment and bundle.
- Create: `outputs/Codex候选A自审_v0.2_Gate_E.md`
- Create: `outputs/pytest_nodeids_v0.2_Gate_E.txt`

- [ ] **Step 1: Run all specified Gate E tests**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_package_identity.py \
  tests/unit/test_gate_e_config.py \
  tests/unit/test_gate_e_inputs.py \
  tests/unit/test_gate_e_environment.py \
  tests/unit/test_gate_e_audit.py \
  tests/unit/test_gate_e_trust.py \
  tests/unit/test_gate_e_replay.py \
  tests/unit/test_portfolio_cli.py \
  tests/unit/test_portfolio_verify.py
```

Expected: all PASS.

- [ ] **Step 2: Save normalized test node IDs**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest \
  --collect-only -q \
  | LC_ALL=C sed -n '/::/p' \
  > outputs/pytest_nodeids_v0.2_Gate_E.txt
shasum -a 256 outputs/pytest_nodeids_v0.2_Gate_E.txt
```

Expected: every Gate E test file appears, no duplicate node ID, and the
reported collected count equals the file's line count.

- [ ] **Step 3: Run candidate A only**

Use the sealed wheelhouse and candidate implementation commit:

```bash
UV_OFFLINE=1 ./scripts/verify_v02_gate_e.sh candidate-a \
  --config configs/releases/v0.2_gate_e.json \
  --wheel <sealed-a-share-quant-0.2.0-wheel> \
  --wheelhouse <sealed-wheelhouse>
```

Expected runtime: approximately 3 minutes. Expected structural evidence:
10 targets, 10 symbols, one shared equity series, 2,074 official sessions,
20,740 position rows and independently recomputed 28 no-bar dates. The run ID
is a candidate, not trusted.

- [ ] **Step 4: Independently verify candidate A**

Run installed CLI verification with `--expected-run-id` equal to candidate A,
then run `aquant-gate-e audit-candidate`. Record 13 file hashes, exact counts,
three accounting identities, final cash weight, symbol weights, no-bar chains,
input-tree immutability and all business failures.

- [ ] **Step 5: Commit evidence but not trust**

```bash
git add outputs/Codex候选A自审_v0.2_Gate_E.md \
  outputs/pytest_nodeids_v0.2_Gate_E.txt
git commit -m "test: record v02 gate e candidate a"
```

### Task 13: Work Buddy candidate review and trust-anchor commit

**Files:**
- Create: `outputs/Work_Buddy候选A复核_v0.2_Gate_E.md`
- Create: `release/v0.2-gate-e/trust_manifest.json`

- [ ] **Step 1: Request a read-only candidate review**

Provide Work Buddy:

- `implementation_commit`;
- design and plan;
- project wheel and SHA;
- wheelhouse manifest;
- candidate A bundle;
- raw commands and environment evidence;
- config and frozen-input trust roots.

Require A-share and quant-core review, direct reverse verification and
`P0/P1/P2`.

Expected: only `P0=0 / P1=0 / P2=0` permits the next step.

The fixed review path is
`outputs/Work_Buddy候选A复核_v0.2_Gate_E.md`. Its unique machine-readable
bindings must include project/version/gate, `review_kind=candidate_a`, PASS,
P0/P1/P2=0, implementation commit, Candidate A evidence SHA-256, expected run
ID, artifact-manifest SHA-256 and project-wheel SHA-256. The complete review
bytes, size, fixed logical path and parsed bindings are included in trust.

- [ ] **Step 2: Generate the canonical trust payload from approved evidence**

Use a dedicated command that prints canonical JSON to stdout. Review every
field before adding the file. The payload records the pre-anchor
`implementation_commit`; it does not contain its own future Git commit.

- [ ] **Step 3: Verify trust against A before commit**

Run:

```bash
<environment-a>/venv/bin/aquant-gate-e \
  verify-trust \
  --trust release/v0.2-gate-e/trust_manifest.json \
  --evidence <candidate-a-evidence.json> \
  --approved-review outputs/Work_Buddy候选A复核_v0.2_Gate_E.md \
  --artifact <candidate-a-run-directory>
```

Expected: PASS with expected run ID, 13 artifacts and 12 payloads.

Both `build-trust` and the public `verify-trust` command accept the Candidate A
review only at the fixed physical repository path above. A missing fixed file
or a byte-identical copy elsewhere is rejected. Environment B may use only the
private, already Git-anchored review-byte verification path after the Git
closure has passed; this does not weaken the public CLI contract.

- [ ] **Step 4: Commit the exact trust blob**

```bash
git add release/v0.2-gate-e/trust_manifest.json \
  outputs/Work_Buddy候选A复核_v0.2_Gate_E.md
git commit -m "release: anchor v02 gate e candidate"
```

Record the resulting commit as `trust_anchor_commit`.

- [ ] **Step 5: Work Buddy verifies the Git blob**

Review exactly:

```text
trust_anchor_commit:release/v0.2-gate-e/trust_manifest.json
```

The review must compare `git show` bytes with the approved candidate and write
`outputs/Work_Buddy信任锚复核_v0.2_Gate_E.md`.

Expected: `P0=0 / P1=0 / P2=0`. Any change requires a new commit and another
review; never amend the approved trust commit.

- [ ] **Step 6: Commit the post-anchor approval separately**

The post-anchor review uniquely binds project/version/gate,
`review_kind=trust_anchor`, PASS, P0/P1/P2=0, `trust_anchor_commit`, fixed trust
path and SHA-256, fixed candidate-review path and SHA-256, implementation
commit and expected run ID.

```bash
git add outputs/Work_Buddy信任锚复核_v0.2_Gate_E.md
git commit -m "review: approve v02 gate e trust anchor"
```

Record this distinct descendant as `approval_commit`. Both recorded IDs must be
40-lowercase-hex object IDs whose exact object type is `commit`; annotated tag
object IDs are rejected. Every controller Git subprocess sets
`GIT_NO_REPLACE_OBJECTS=1`, and the approval closure rejects any local
`refs/replace` or legacy `info/grafts` file before resolving ancestry or blobs.
Git proves only that the
review bytes belong to the commit; it does not prove the real identity of Work
Buddy. Preserve that limitation in the final report.

### Task 14: Replay environment B from the anchored Git blob

**Files:**
- Create outside Git: environment B and audit bundle.
- Create: `outputs/Codex双环境复演_v0.2_Gate_E.md`

- [ ] **Step 1: Extract trust from the exact commit**

Run:

```bash
git show \
  <trust_anchor_commit>:release/v0.2-gate-e/trust_manifest.json \
  > <temporary-trust-copy>
```

Before any B directory is created, the controller must verify that the two
commit IDs are distinct exact commit objects, that `trust_anchor_commit` is an
ancestor of `approval_commit` and that `approval_commit` is an ancestor of the
resolved HEAD commit. The ancestor relation is inclusive, so
`approval_commit == HEAD` is valid. Replacement objects and grafted histories
are forbidden repository state, even when they would resolve to byte-identical
trees. It must extract trust and the
Candidate A review from the anchor commit, extract the post-anchor review from
the approval commit, and prove that the approval commit did not alter the trust
or Candidate A review bytes. It must then prove that all three blobs in HEAD
still have those exact anchor/approval bytes; only after that may it check that
the current checkout's three files are regular single-link files with those
same bytes. Then call the installed
controller's `verify-trust` against candidate A using the extracted trust and
candidate review plus complete `GateETrustEvidence`. Re-run candidate A's
post-run input-root verifier and wheel/wheelhouse hashes. Any failure blocks B.

- [ ] **Step 2: Run B with a different hash seed**

Create new venv, HOME, XDG cache, uv cache, input copy, config copy, project
root and output root. Use hash seed `909`. Install from the same project wheel
and same sealed wheelhouse in `sandbox-exec`.

The only accepted top-level command is:

```bash
<environment-a>/venv/bin/aquant-gate-e replay-b \
  --trust-anchor-commit <trust_anchor_commit> \
  --approval-commit <approval_commit> \
  --trust-path release/v0.2-gate-e/trust_manifest.json \
  --wheel <sealed-project-wheel> \
  --wheelhouse <sealed-wheelhouse> \
  --workspace-a <environment-a-root> \
  --workspace-b <new-environment-b-root>
```

Inside the reviewed controller, B may invoke only absolute installed B
`aquant-portfolio run-config` and `verify` paths. Expected: run ID equals
trusted candidate A.

- [ ] **Step 3: Verify B with the anchored identity**

Run installed verification with `--expected-run-id`, then external Gate E
trust and accounting audits. Run `verify_post_run_input_root()` and require
exactly the unchanged 25 inputs with no runtime sidecar. Recompute and compare
project-wheel and wheelhouse hashes again. Before returning
`verified_replay`, require Candidate A and B evidence to contain byte-for-byte
equal complete `installed_packages` arrays; a name, version, order, missing or
extra distribution difference is a hard failure.

Expected: all PASS.

- [ ] **Step 4: Compare exact file sets and raw bytes**

Require:

```python
set(files_a) == set(files_b) == PORTFOLIO_ARTIFACT_FILES
all(path_a.read_bytes() == path_b.read_bytes() for each name)
```

Also require the output root to contain only the run-ID directory and the exact
zero-byte `.<run_id>.lock` sidecar.

- [ ] **Step 5: Record evidence**

Write `outputs/Codex双环境复演_v0.2_Gate_E.md` with both environments, commands,
hashes, 13-file comparison, no-bar evidence, accounting, actual weights and
business failures. Do not call the strategy profitable or live-ready.

### Task 15: Run the final engineering and independent release gates

**Files:**
- Create: `outputs/Codex自审_v0.2_Gate_E.md`
- Create: `outputs/Work_Buddy代码与审计包复核_v0.2_Gate_E.md`
- Create: `outputs/A股量化项目_v0.2_Gate_E交付与验收.md`

- [ ] **Step 1: Run focused and full tests**

Run:

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_gate_e_*.py \
  tests/unit/test_portfolio_cli.py \
  tests/unit/test_portfolio_verify.py
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q -rs \
  --junitxml=<temporary-test-evidence>/full-suite.xml
python3 - <temporary-test-evidence>/full-suite.xml <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
skipped = {
    f"{case.attrib['classname']}::{case.attrib['name']}"
    for case in root.iter("testcase")
    if case.find("skipped") is not None
}
assert skipped == {
    (
        "tests.integration.test_v01_release::"
        "test_rebuilds_complete_v01_release_from_frozen_inputs"
    )
}
PY
```

Expected: no failure. The machine-checked only skip is:

```text
tests/integration/test_v01_release.py::
test_rebuilds_complete_v01_release_from_frozen_inputs
```

- [ ] **Step 2: Run the explicit v0.1 frozen replay**

Run:

```bash
AQUANT_RUN_RELEASE_INTEGRATION=1 \
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/integration/test_v01_release.py
```

Expected: PASS after the full frozen v0.1 reconstruction.

- [ ] **Step 3: Run static, lock, build and boundary gates**

Run:

```bash
UV_OFFLINE=1 uv run --no-sync ruff check .
uv lock --check
git diff --check
UV_OFFLINE=1 uv build --wheel --no-sources \
  --out-dir <temporary-reproducibility-probe>
git diff --exit-code v0.1-research -- \
  src/aquant/backtest src/aquant/data src/aquant/rules \
  src/aquant/backtest_cli.py
```

Expected: all pass. This disposable reproducibility probe must have the same
SHA-256 as the sealed formal candidate wheel if the source tree and build
inputs are unchanged; it must not replace or modify the formal wheel.

- [ ] **Step 4: Work Buddy performs final code and artifact review**

Require direct inspection of:

- implementation and trust commits;
- exact config and input closure;
- A/B raw bundles and byte comparison;
- `artifact_file_count=13`, `payload_file_count=12`;
- 7/24 boundary;
- 28 no-bar recomputation;
- three accounting identities;
- candidate and trust review history;
- full command evidence and remaining limitations.

Expected:

```text
P0 = 0
P1 = 0
P2 = 0
```

- [ ] **Step 5: Write final report and commit**

The report header must state:

```text
project = a-share-quant
version = v0.2
gate = E
research_only = true
simulation_only = true
profit_claim = false
live_trading = false
```

Include wheel and wheelhouse hashes, 25 input hashes, config hash, both
quarantined deviation-lock hashes, test node ID hash, implementation/trust
commits, expected run ID, 13 artifact hashes, A/B environment evidence, actual
weights, final cash source decomposition, no-bar and all failure evidence.

Commit:

```bash
git add \
  outputs/Work_Buddy信任锚复核_v0.2_Gate_E.md \
  outputs/Codex双环境复演_v0.2_Gate_E.md \
  outputs/Codex自审_v0.2_Gate_E.md \
  outputs/Work_Buddy代码与审计包复核_v0.2_Gate_E.md \
  outputs/A股量化项目_v0.2_Gate_E交付与验收.md
git commit -m "release: verify v02 gate e isolated replay"
```

- [ ] **Step 6: Push and verify remote identity**

Run:

```bash
git push origin codex/v02-shared-cash-portfolio
git rev-parse HEAD
git rev-parse origin/codex/v02-shared-cash-portfolio
git status --short
```

Expected: local and remote SHA are identical and the tracked worktree is clean.

## Stop rules

Stop Gate E immediately if:

- a frozen input differs or an undeclared input appears;
- the wheel or wheelhouse cannot install from empty caches offline;
- the formal process reads repository source or user site-packages;
- 2026-07-24 produces an economic row;
- any of 28 known no-bar dates disappears without source evidence;
- A/B run IDs, file sets or raw bytes differ;
- trust verification, accounting or reverse reconstruction fails;
- candidate A or trust-anchor Work Buddy review has P0/P1 or an unclosed P2;
- full tests regress or the named skip set changes.

Preserve all failed candidates and sanitized evidence. Do not change the ten
symbols, dates, weights, fees, attempts or strategy in response to failure.
