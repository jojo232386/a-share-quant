# v0.1-research Reproducible Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify a tagged `v0.1-research` release that can rebuild the 20 formal baselines, 30 Week 5 candidates, risk report, experiment, and 100 replay rows from repository-tracked frozen inputs without network access or existing local outputs.

**Architecture:** Keep the existing backtest, report, and experiment public entry points unchanged. Add a strict release-manifest boundary, a process-level network guard, and an isolated replay orchestrator that constructs a temporary project root from an exact frozen-input allowlist. The verifier compares deterministic identities and artifact manifests, while a clean Git archive acceptance test proves that local untracked `data/` and `outputs/` are not dependencies.

**Tech Stack:** Python 3.11, AKShare 1.18.64, Backtrader 1.9.78.123, pandas, PyArrow, uv, pytest, Ruff, POSIX shell, Git.

---

## File map

Create or modify these focused units:

- `src/aquant/release_manifest.py`: strict JSON parsing, release schema validation, safe-path checks, file-closure and SHA-256 verification.
- `src/aquant/release_network.py`: reversible process-level guard against socket and Requests network access.
- `src/aquant/release_replay.py`: isolated temporary-root construction, public-entry-point replay, progress events, identity comparison and artifact verification.
- `src/aquant/release_cli.py`: machine-readable `aquant-release verify` command only.
- `tests/unit/test_identity_determinism.py`: explicit run/report/experiment identity stability and sensitivity contract.
- `tests/unit/test_release_manifest.py`: manifest parser, path, link, hash and exact-closure tests.
- `tests/unit/test_release_network.py`: network-blocking and restoration tests.
- `tests/unit/test_release_replay.py`: minimal replay boundary, conflict-root and output-contract tests.
- `tests/integration/test_v01_release.py`: opt-in full frozen-input reconstruction test.
- `scripts/verify_v01.sh`: no-sync release verification entry point.
- `release/v0.1-research/`: exact frozen inputs, expected identities and reader instructions.
- `README.md`, `docs/support_matrix.md`, `docs/known_limitations.md`, `docs/recovery.md`: user-facing boundary and recovery documentation.
- Existing stage documents: remove stale symbol counts, test counts and roadmap status.

### Task 1: Lock deterministic identity contracts

**Files:**
- Create: `tests/unit/test_identity_determinism.py`
- Read: `src/aquant/backtest/runner.py`
- Read: `src/aquant/reporting/risk_report.py`
- Read: `src/aquant/research/week5.py`

- [ ] **Step 1: Write run identity stability and sensitivity tests**

Use the existing synthetic backtest helpers to create the same logical input twice under different wall-clock/PID monkeypatches and assert equal `run_id` plus equal exported payload bytes. Then change exactly one identity-bearing field, such as `random_seed`, and assert a different `run_id`.

```python
def test_run_identity_ignores_wall_clock_and_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(time, "time", lambda: 1.0)
    monkeypatch.setattr(os, "getpid", lambda: 11)
    first = _synthetic_result(random_seed=7)
    first_dir = export_backtest_result(first, tmp_path / "first")

    monkeypatch.setattr(time, "time", lambda: 9_999_999.0)
    monkeypatch.setattr(os, "getpid", lambda: 99_999)
    second = _synthetic_result(random_seed=7)
    second_dir = export_backtest_result(second, tmp_path / "second")

    assert first.run_id == second.run_id
    assert _bundle_payloads(first_dir) == _bundle_payloads(second_dir)
    assert _synthetic_result(random_seed=8).run_id != first.run_id
```

- [ ] **Step 2: Run the targeted test and confirm the current deterministic contract**

Run:

```bash
uv run --no-sync pytest tests/unit/test_identity_determinism.py -q
```

Expected: PASS. If it fails, stop release work and remove the nondeterministic input from identity construction before continuing.

- [ ] **Step 3: Add report and experiment input-order tests**

Construct equivalent risk-report inputs in forward and reverse order. Construct equivalent Week 5 dictionaries with reversed symbol and period insertion order. Assert equal IDs and equal canonical payload bytes.

```python
assert build_independent_batch_report(tuple(runs), **kwargs) == (
    build_independent_batch_report(tuple(reversed(runs)), **kwargs)
)
assert build_week5_report(forward_candidates, forward_baselines, **kwargs) == (
    build_week5_report(reverse_candidates, reverse_baselines, **kwargs)
)
```

- [ ] **Step 4: Add a fresh-interpreter hash-seed test**

Invoke the same identity fixture in two subprocesses with `PYTHONHASHSEED=1` and `PYTHONHASHSEED=98765`. Compare stdout exactly; do not inherit old output directories.

```python
def _identity_subprocess(seed: str) -> bytes:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    return subprocess.check_output(
        [sys.executable, "-m", "tests.identity_probe"],
        cwd=PROJECT_ROOT,
        env=env,
    )

assert _identity_subprocess("1") == _identity_subprocess("98765")
```

Implement the probe as `tests/identity_probe.py` with canonical JSON stdout only.

- [ ] **Step 5: Run deterministic contract tests twice**

Run:

```bash
uv run --no-sync pytest tests/unit/test_identity_determinism.py -q
uv run --no-sync pytest tests/unit/test_identity_determinism.py -q
```

Expected: both runs pass with the same collected test count.

- [ ] **Step 6: Commit the identity contract**

```bash
git add tests/identity_probe.py tests/unit/test_identity_determinism.py
git commit -m "test: lock deterministic research identities"
```

### Task 2: Implement strict release manifest verification

**Files:**
- Create: `src/aquant/release_manifest.py`
- Create: `tests/unit/test_release_manifest.py`

- [ ] **Step 1: Write failing tests for strict JSON and schema validation**

Cover duplicate keys, unknown top-level fields, incorrect fixed values, uppercase or malformed hashes, wrong value types, and missing fields. Use a minimal complete valid manifest factory; each test mutates one field.

```python
with pytest.raises(ReleaseVerificationError) as error:
    load_release_manifest(path_with_duplicate_release_name)
assert error.value.code == "duplicate_manifest_key"

payload = valid_manifest_payload()
payload["surprise"] = True
with pytest.raises(ReleaseVerificationError) as error:
    load_release_manifest(_write_manifest(tmp_path, payload))
assert error.value.code == "manifest_schema_invalid"
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```bash
uv run --no-sync pytest tests/unit/test_release_manifest.py -q
```

Expected: FAIL because `aquant.release_manifest` does not exist.

- [ ] **Step 3: Implement the manifest value objects and strict loader**

Define immutable dataclasses for expected counts and the release manifest. Parse with an `object_pairs_hook` that rejects duplicate keys before constructing a dictionary. Compare the exact top-level key set against a constant whitelist.

```python
HASH_RE = re.compile(r"[0-9a-f]{64}")
ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "release_name",
        "implementation_commit",
        "python_version",
        "akshare_version",
        "backtrader_version",
        "universe_id",
        "calendar_id",
        "market_snapshots",
        "corporate_action_snapshots",
        "input_files",
        "baseline_run_ids",
        "candidate_run_ids",
        "risk_report_id",
        "week5_experiment_id",
        "expected_counts",
        "research_boundary",
    }
)

class ReleaseVerificationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
```

The returned object must contain tuples sorted by stable logical key, never raw caller dictionaries.

- [ ] **Step 4: Add failing safe-path, link and closure tests**

Test absolute paths, `..`, empty components, non-normalized paths, symlinks, files with `st_nlink > 1`, missing files, extra files, changed bytes and declared-but-unread files.

```python
for unsafe in ("/tmp/value", "../outside", "inputs/../outside", ""):
    payload = valid_manifest_payload()
    payload["input_files"] = {unsafe: "a" * 64}
    assert _error_code(payload) == "unsafe_input_path"
```

- [ ] **Step 5: Implement safe-path and exact-file verification**

Resolve paths beneath the release directory without following a path outside it. Reject a symlink at every component, reject non-regular files, reject hard-linked files, compare SHA-256 using streamed reads, and compare:

```python
declared = frozenset(manifest.input_files)
actual = frozenset(
    path.relative_to(release_root).as_posix()
    for path in (release_root / "inputs").rglob("*")
    if path.is_file()
)
if declared != actual:
    raise ReleaseVerificationError("input_file_set_mismatch")
```

Reject any directory or file symlink before `rglob` results are trusted.

- [ ] **Step 6: Run focused tests and Ruff**

Run:

```bash
uv run --no-sync pytest tests/unit/test_release_manifest.py -q
uv run --no-sync ruff check src/aquant/release_manifest.py tests/unit/test_release_manifest.py
```

Expected: all pass.

- [ ] **Step 7: Commit the manifest boundary**

```bash
git add src/aquant/release_manifest.py tests/unit/test_release_manifest.py
git commit -m "feat: verify strict release manifests"
```

### Task 3: Block network access during research replay

**Files:**
- Create: `src/aquant/release_network.py`
- Create: `tests/unit/test_release_network.py`

- [ ] **Step 1: Write failing guard tests**

Test direct socket connect, `connect_ex`, `socket.create_connection`, and `requests.Session.request`. Also test that the original callables are restored after leaving the context.

```python
with offline_network_guard():
    with pytest.raises(ReleaseNetworkError) as error:
        socket.create_connection(("127.0.0.1", 9))
    assert error.value.code == "network_access_forbidden"

assert socket.create_connection is original_create_connection
```

- [ ] **Step 2: Confirm tests fail**

Run:

```bash
uv run --no-sync pytest tests/unit/test_release_network.py -q
```

Expected: FAIL because the guard is not implemented.

- [ ] **Step 3: Implement a reversible context manager**

Use `unittest.mock.patch` in an `ExitStack`. Every blocked callable raises the same sanitized exception without including host, port, URL or proxy values.

```python
@contextmanager
def offline_network_guard():
    def blocked(*_args, **_kwargs):
        raise ReleaseNetworkError("network_access_forbidden")

    with ExitStack() as stack:
        stack.enter_context(patch.object(socket.socket, "connect", blocked))
        stack.enter_context(patch.object(socket.socket, "connect_ex", blocked))
        stack.enter_context(patch.object(socket, "create_connection", blocked))
        stack.enter_context(patch.object(requests.Session, "request", blocked))
        yield
```

- [ ] **Step 4: Run guard tests and Ruff**

Run:

```bash
uv run --no-sync pytest tests/unit/test_release_network.py -q
uv run --no-sync ruff check src/aquant/release_network.py tests/unit/test_release_network.py
```

Expected: all pass.

- [ ] **Step 5: Commit network isolation**

```bash
git add src/aquant/release_network.py tests/unit/test_release_network.py
git commit -m "feat: block network during release replay"
```

### Task 4: Build the isolated replay orchestrator

**Files:**
- Create: `src/aquant/release_replay.py`
- Create: `tests/unit/test_release_replay.py`
- Read: `src/aquant/backtest_cli.py`
- Read: `src/aquant/report_cli.py`
- Read: `src/aquant/experiment_cli.py`

- [ ] **Step 1: Write a failing temporary-root isolation test**

Build a one-symbol miniature release fixture. Put a conflicting valid-looking snapshot in the caller’s root `data/` and a conflicting run in `outputs/`. Replay must use only the fixture copied into a new temporary root and must report the expected identity.

```python
summary = verify_release(
    project_root=caller_root,
    release_root=mini_release,
    progress=lambda _event: None,
)
assert summary.baseline_run_count == 2
assert summary.candidate_run_count == 3
assert outside_conflict.read_bytes() == original_conflict
```

- [ ] **Step 2: Confirm the replay test fails**

Run:

```bash
uv run --no-sync pytest tests/unit/test_release_replay.py -q
```

Expected: FAIL because `verify_release` does not exist.

- [ ] **Step 3: Implement temporary project construction**

Create a `TemporaryDirectory`, then copy only:

- `configs/universes/<universe_id>.json`;
- every verified `input_files` entry under its project-relative destination;
- no caller-root `data/` or `outputs/`.

Use `shutil.copyfile` only after manifest verification and reject existing destinations. Create fresh `outputs/backtests`, `outputs/reports` and `outputs/experiments/week5/candidates`.

- [ ] **Step 4: Implement public-entry-point invocation**

Call `backtest_cli.main`, `report_cli.main` and `experiment_cli.main` in-process so the network guard and data-read audit remain effective. Redirect their stdout/stderr to private buffers and parse exactly one canonical JSON object. A nonzero exit or unexpected stream becomes a sanitized `ReleaseVerificationError`.

For each of 10 symbols run:

```text
buy_and_hold, sma(period=20)
sma(period=10), sma(period=20), sma(period=60) under candidate output root
```

Use the frozen parameters from the release manifest contract and existing project defaults:

```text
initial_cash=1000000
target_weight=0.95
random_seed=0
train_end=2023-12-29
holdout_start=2024-01-02
replay_days=10
stock_commission_rate=0.00025
stock_minimum_commission=5.0
etf_commission_rate=0.00025
etf_minimum_commission=5.0
```

If the current formal run metadata contains different values, stop and update this plan/spec before writing the manifest; do not silently bless a different parameter set.

- [ ] **Step 5: Implement data-read closure auditing**

Wrap the data-access seams imported by `aquant.backtest_cli` and
`aquant.experiment_cli` while invoking the public entry points:

- `ManifestWriter.read_all`;
- `load_verified_snapshot`;
- `CalendarSnapshotStore.read_manifest`;
- `load_verified_calendar`;
- `read_corporate_action_manifest`;
- `load_verified_corporate_actions`.

Each wrapper calls the original function first, then records the exact successfully verified
manifest or snapshot path beneath the temporary `data/` directory. This is more reliable than
patching `Path.open`, because Parquet reads may occur inside PyArrow without calling Python's
`Path.open`. At the end require the observed data paths to equal the manifest’s project-data path
set exactly. Exclude application code, configs and newly written outputs from this equality.

```python
def audited_load_snapshot(project_root, record):
    result = original_load_snapshot(project_root, record)
    audit.record(project_root / record.snapshot_relative_path)
    return result

with ExitStack() as stack:
    stack.enter_context(
        patch.object(backtest_cli, "load_verified_snapshot", audited_load_snapshot)
    )
    # Install equivalent wrappers for the five other declared seams.
    _invoke_public_entries()

if audit.paths != expected_data_paths:
    raise ReleaseVerificationError("input_read_closure_mismatch")
```

The audit must not record failed probing of nonexistent optional files as a successful read.

- [ ] **Step 6: Verify every output artifact and all identities**

After each run, use the existing artifact-manifest verifier or audited bundle loader. Build and verify the risk report. Build the Week 5 experiment and verify its artifact manifest. Compare:

- exact 20 baseline key-to-run-ID pairs;
- exact 30 candidate key-to-run-ID pairs;
- risk `report_id`;
- Week 5 `experiment_id`;
- 10 symbols, 20 baselines, 30 candidates and 100 replay rows.

Sort mappings before comparison and reject missing, duplicate or extra outputs.

- [ ] **Step 7: Emit five progress stages without paths**

The orchestrator accepts a callback and emits:

```python
ProgressEvent(stage="inputs_verified", completed=1, total=5)
ProgressEvent(stage="baselines_rebuilt", completed=2, total=5)
ProgressEvent(stage="risk_report_rebuilt", completed=3, total=5)
ProgressEvent(stage="experiment_rebuilt", completed=4, total=5)
ProgressEvent(stage="identities_verified", completed=5, total=5)
```

No event may contain filesystem paths, user names or raw exception messages.

- [ ] **Step 8: Run replay unit tests and Ruff**

Run:

```bash
uv run --no-sync pytest tests/unit/test_release_replay.py -q
uv run --no-sync ruff check src/aquant/release_replay.py tests/unit/test_release_replay.py
```

Expected: all pass.

- [ ] **Step 9: Commit the replay engine**

```bash
git add src/aquant/release_replay.py tests/unit/test_release_replay.py
git commit -m "feat: rebuild releases in an isolated project"
```

### Task 5: Add the machine-readable release command

**Files:**
- Create: `src/aquant/release_cli.py`
- Modify: `pyproject.toml`
- Create: `scripts/verify_v01.sh`
- Modify: `tests/unit/test_release_replay.py`

- [ ] **Step 1: Write failing CLI contract tests**

Test `verify --project-root`, invalid arguments, unsafe release paths, one-line success stdout, progress-only stderr, sanitized one-line failure stderr and nonzero failure status.

```python
assert success_payload == {
    "baseline_run_count": 2,
    "candidate_run_count": 3,
    "elapsed_seconds": pytest.approx(expected, abs=1),
    "release_name": "v0.1-research",
    "replay_row_count": 10,
    "status": "verified",
}
assert all("path" not in json.loads(line) for line in captured.err.splitlines())
```

- [ ] **Step 2: Confirm CLI tests fail**

Run:

```bash
uv run --no-sync pytest tests/unit/test_release_replay.py -q
```

Expected: FAIL because `aquant.release_cli` is missing.

- [ ] **Step 3: Implement `aquant-release verify`**

Use a safe `argparse.ArgumentParser` subclass matching the existing CLIs. Resolve `--project-root` and the fixed `release/v0.1-research` path beneath it. Install the network guard around the complete replay. Write progress JSON to stderr, one success JSON to stdout, and only this failure shape to stderr:

```json
{"error_code":"...","error_type":"...","status":"error"}
```

Measure elapsed time with `time.monotonic()`; elapsed time is output metadata and must not enter any identity.

- [ ] **Step 4: Register the console script**

Add:

```toml
aquant-release = "aquant.release_cli:main"
```

under `[project.scripts]`.

- [ ] **Step 5: Add the no-sync shell entry point**

Create an executable script:

```sh
#!/bin/sh
set -eu

command -v uv >/dev/null 2>&1 || {
  printf '%s\n' '{"error_code":"uv_not_found","error_type":"EnvironmentError","status":"error"}' >&2
  exit 1
}

uv lock --check >/dev/null
exec uv run --no-sync aquant-release verify --project-root .
```

It must not invoke data ingestion, `uv sync`, `pip`, AKShare download APIs, or existing root outputs.

- [ ] **Step 6: Run CLI tests and package checks**

Run:

```bash
chmod +x scripts/verify_v01.sh
uv sync --frozen --no-editable --reinstall-package a-share-quant
uv run --no-sync pytest tests/unit/test_release_replay.py -q
uv run --no-sync ruff check src/aquant/release_cli.py tests/unit/test_release_replay.py
uv lock --check
```

Expected: all pass.

- [ ] **Step 7: Commit the CLI**

```bash
git add pyproject.toml uv.lock scripts/verify_v01.sh src/aquant/release_cli.py tests/unit/test_release_replay.py
git commit -m "feat: add offline release verification command"
```

### Task 6: Freeze the exact v0.1 inputs and expected identities

**Files:**
- Create: `release/v0.1-research/README.md`
- Create: `release/v0.1-research/release_manifest.json`
- Create: `release/v0.1-research/inputs/**`
- Create: `tests/integration/test_v01_release.py`

- [ ] **Step 1: Verify the source evidence before copying**

From the original main worktree, verify:

```bash
uv run --no-sync aquant-report verify \
  --project-root . \
  --report-id 226bf973d6211f4ca80d464a0dff19e96dbfd1e660a51f966efffcb32009f389
```

Expected: `status=verified`, `run_count=20`, universe
`bba6760fa738a829bb09a72f0c90919aeba02429018b8fd189c65e2d6c82a20e`.

Also verify the Week 5 artifact manifest and confirm experiment ID
`8f7ca7cce348d376165b6bd319d3efa262adff86c0be377ce7b3ed72c511b1db`.

- [ ] **Step 2: Resolve the exact input closure**

Read the formal run metadata to list the 10 market snapshot IDs, one calendar ID and 10 corporate-action snapshot IDs. Resolve each ID to one manifest record. Reject duplicates or fallback snapshots. Record the exact relative paths and SHA-256 values.

Expected fixed high-level identities:

```text
universe_id=bba6760fa738a829bb09a72f0c90919aeba02429018b8fd189c65e2d6c82a20e
calendar_id=fb24e5167d11fee3a58869f8de7910a0ea979d55d3481698bc5baf18cd508983
implementation_digest=d0e7b112ad10e5b64616d7ff2b500ff0ba4c2c7c98ec20ace1163259375eeedd
```

- [ ] **Step 3: Copy only verified source files**

Create the release input layout and copy the exact market manifest, 10 formal market snapshots, calendar manifest, one calendar snapshot, corporate-action manifest and 10 action snapshots. Do not copy current `outputs/`, fallback snapshots, caches or virtual environments.

After copying, run:

```bash
find release/v0.1-research/inputs -type f | LC_ALL=C sort
du -sh release/v0.1-research/inputs
```

Expected: the list matches the derived closure and total size remains approximately 1.4 MB.

- [ ] **Step 4: Write an initial strict manifest with verified IDs**

Populate every required field from verified run/report/experiment metadata. Set `implementation_commit` to the clean commit that contains Tasks 1 through 5. Use canonical JSON with sorted keys, compact separators and one trailing newline.

Do not include:

- generation timestamps;
- machine paths;
- PID or UUID;
- a self-hash;
- the future final release commit.

- [ ] **Step 5: Write the full reconstruction integration test**

Mark it `@pytest.mark.integration`. Load the real release directory and call `verify_release`. Assert exact expected counts, risk report ID, experiment ID and stable summary fields. Do not assert elapsed time equality.

- [ ] **Step 6: Run release verification twice**

Run:

```bash
./scripts/verify_v01.sh > /tmp/aquant-v01-first.json 2> /tmp/aquant-v01-first.progress
./scripts/verify_v01.sh > /tmp/aquant-v01-second.json 2> /tmp/aquant-v01-second.progress
```

Expected: both exit zero; identity-bearing summary fields and progress stages are identical. `elapsed_seconds` may differ.

- [ ] **Step 7: Run the integration test**

Run:

```bash
AQUANT_RUN_RELEASE_INTEGRATION=1 \
  uv run --no-sync pytest tests/integration/test_v01_release.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit the frozen release**

```bash
git add release/v0.1-research tests/integration/test_v01_release.py
git commit -m "release: freeze v0.1 research inputs"
```

### Task 7: Close documentation drift and publish beginner instructions

**Files:**
- Create: `README.md`
- Create: `docs/support_matrix.md`
- Create: `docs/known_limitations.md`
- Create: `docs/recovery.md`
- Modify: `docs/a_share_execution_rules.md`
- Modify: `outputs/A股量化项目_第5周阶段交付.md`
- Modify: the active six-week roadmap document located by `rg -l "六周|6 周" docs outputs`
- Modify: `release/v0.1-research/README.md`

- [ ] **Step 1: Write the top-level README**

Include:

- run all commands from the repository root;
- old desktop folder is an archive only;
- install with `./scripts/bootstrap_env.sh`;
- verify with `./scripts/verify_v01.sh`;
- typical verified duration and machine environment, filled only after Task 6;
- explicit “research/simulation only, no broker, no profit proof” boundary;
- links to support, limitations and recovery documents.

- [ ] **Step 2: Write the support matrix**

Use exactly four statuses: supported, conservative approximation, rejected, not implemented. Cover instrument kinds, T+1, lot sizes, fees, suspension, one-price limit boards, corporate actions, ST/new listings, ChiNext/STAR/BSE, partial fills, slippage, volume limits, shared cash and live trading.

- [ ] **Step 3: Write known limitations**

State:

- fixed present-day sample and survivorship-bias boundary;
- AKShare/free-source rewrite risk;
- `research_approx` adjustment risk;
- daily OHLC cannot prove queue execution;
- independent single-instrument runs are not a shared-cash portfolio;
- no partial fill/slippage/volume model;
- local hashes and Git tags are integrity aids, not third-party digital signatures;
- profitability remains unproven.

- [ ] **Step 4: Write recovery instructions**

Provide exact safe recovery for missing uv, lock mismatch, fixture hash failure, output conflict and interrupted replay. Never recommend deleting frozen snapshots or existing user outputs as a normal repair.

- [ ] **Step 5: Update stale stage documents**

Change the old four-symbol support wording to the approved ten-symbol universe. Replace stale test counts only with the fresh full-suite count from Task 8. Mark Weeks 1–5 complete and Week 6 in progress until final acceptance. Preserve all negative strategy results.

- [ ] **Step 6: Run documentation consistency checks**

Run:

```bash
rg -n "4个标的|4 个标的|366 passed|第 1 至第 4 周已" README.md docs outputs release
rg -n "实盘可用|已验证盈利|保证收益" README.md docs outputs release
```

Expected: no stale count/status text and no unsupported live/profit claim.

- [ ] **Step 7: Commit documentation**

```bash
git add README.md docs release/v0.1-research/README.md outputs/A股量化项目_第5周阶段交付.md
git commit -m "docs: publish v0.1 research boundaries"
```

### Task 8: Prove the release from a clean Git archive

**Files:**
- Modify: `release/v0.1-research/README.md`
- Create after verification: `outputs/A股量化项目_v0.1-research交付与验收.md`

- [ ] **Step 1: Run the full local quality gate**

Run:

```bash
uv lock --check
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv build
git status --short
```

Expected: lock valid, all tests pass, Ruff passes, sdist and wheel build, only expected release-report changes remain.

- [ ] **Step 2: Create a tracked-files-only archive**

Run from the feature worktree:

```bash
archive_dir="$(mktemp -d)"
git archive --format=tar HEAD | tar -xf - -C "$archive_dir"
```

Verify the archive does not contain `.venv`, caches, old output bundles or the original untracked `data/`.

- [ ] **Step 3: Bootstrap the archive environment**

Run:

```bash
cd "$archive_dir"
./scripts/bootstrap_env.sh
```

Expected: the environment resolves exactly from `uv.lock`; no market-data download command runs.

- [ ] **Step 4: Run research verification offline**

Disconnect outbound networking at the OS or execution-sandbox level after bootstrap, then run:

```bash
./scripts/verify_v01.sh
```

Expected: exit zero, five sanitized progress events and one success JSON. Save elapsed seconds and exact verified counts. If OS-level blocking is unavailable, record that limitation and require the process guard test plus a later manual disconnected run before tagging.

- [ ] **Step 5: Run tests, Ruff, lock check and build in the archive**

Run:

```bash
uv lock --check
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv build
```

Expected: all pass from tracked files only.

- [ ] **Step 6: Record actual duration and delivery evidence**

Update `release/v0.1-research/README.md` with the measured machine and elapsed range. Create the delivery report containing:

- Git commit under test;
- lock/test/Ruff/build results;
- clean archive method;
- offline verification result and duration;
- exact run/report/experiment identities;
- negative strategy/risk findings and unsupported scope;
- explicit statement that this is not live-trading approval.

- [ ] **Step 7: Re-run the clean archive after documentation changes**

Commit the duration/report update, create a new archive from that commit and repeat `./scripts/verify_v01.sh`. The manifest’s `implementation_commit` remains the pre-manifest business implementation commit; the Git tag later binds the final documentation commit.

- [ ] **Step 8: Commit acceptance evidence**

```bash
git add release/v0.1-research/README.md outputs/A股量化项目_v0.1-research交付与验收.md
git commit -m "docs: record clean v0.1 release acceptance"
```

### Task 9: Obtain Claude Code review, fix findings, and tag

**Files:**
- Create: `outputs/Claude代码级复核结论_v0.1-research.md`
- Modify: any source/test/doc file required by validated findings
- Modify: `outputs/A股量化项目_v0.1-research交付与验收.md`

- [ ] **Step 1: Run Claude Code as a read-only reviewer**

Ask Claude Code through the configured DeepSeek API to inspect:

- deterministic identities;
- release-manifest bypasses;
- path traversal, soft-link and hard-link handling;
- network-guard coverage;
- temporary-root isolation;
- read-closure audit;
- stdout/stderr sanitization;
- artifact and expected-identity comparison;
- documentation boundaries.

Require exact file/function evidence and separate static-review claims from commands it actually executed.

- [ ] **Step 2: Validate every finding locally**

For each finding, reproduce it with a failing test before changing code. Reject speculative findings that do not survive local evidence. Classify P0/P1/P2.

- [ ] **Step 3: Fix validated findings with TDD**

For each valid item:

```text
write failing regression test
run focused test and observe failure
make the minimum scoped fix
run focused and neighboring tests
commit with finding-specific message
```

- [ ] **Step 4: Re-run the complete final gate**

Run:

```bash
uv lock --check
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv build
./scripts/verify_v01.sh
```

Then repeat the tracked-files-only clean archive verification.

Expected: P0=0, unresolved P1=0. Any remaining P2 must be explicitly accepted in known limitations; target P2=0 for this release.

- [ ] **Step 5: Write the review log**

Record:

- reviewer command/config boundary without secrets;
- reviewed commit;
- files and attack paths checked;
- actual commands run by Claude versus local Codex verification;
- findings, regression tests and fixes;
- final P0/P1/P2 counts.

- [ ] **Step 6: Commit the review record**

```bash
git add outputs/Claude代码级复核结论_v0.1-research.md outputs/A股量化项目_v0.1-research交付与验收.md
git commit -m "docs: record v0.1 code review"
```

- [ ] **Step 7: Verify a clean final state and create the tag**

Run:

```bash
git status --short
git log -1 --oneline
git tag --list v0.1-research
```

Expected: no tracked changes and no existing tag. Then create:

```bash
git tag -a v0.1-research -m "A-share research platform v0.1"
git show --no-patch --decorate v0.1-research
```

Do not move or recreate the tag after publication. The tag denotes a reproducible research release only.
