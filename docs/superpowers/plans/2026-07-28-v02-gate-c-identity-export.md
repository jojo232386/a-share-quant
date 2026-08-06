# v0.2 Gate C Identity, Atomic Export, and Reverse Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every v0.2 shared-cash portfolio run a deterministic identity and a complete, atomically published audit bundle that an independent verifier can reconstruct and reject after any byte-level damage.

**Architecture:** Keep `PortfolioBacktestResult` as the pure Gate B engine result. Add a verified run wrapper that binds the exact input closure, implementation fingerprint, run ID, and result digest; compute portfolio metrics only from integer-fen ledgers; serialize twelve deterministic payload files; publish with no-replace atomic semantics; and verify the published package through an independent parser rather than through exporter-generated expected bytes.

**Tech Stack:** Python 3.11, immutable dataclasses, `Decimal`, SHA-256, canonical JSON/CSV, safe POSIX file-descriptor operations, pytest, Ruff, uv, and the existing verified A-share loaders and portfolio ledger.

---

## Scope and safety boundary

Create:

- `src/aquant/portfolio/identity.py`
- `src/aquant/portfolio/metrics.py`
- `src/aquant/portfolio/export.py`
- `src/aquant/portfolio/verify.py`
- `src/aquant/portfolio_cli.py`
- `tests/portfolio_gate_c_support.py`
- `tests/portfolio_identity_probe.py`
- `tests/unit/test_portfolio_identity.py`
- `tests/unit/test_portfolio_metrics.py`
- `tests/unit/test_portfolio_export.py`
- `tests/unit/test_portfolio_verify.py`
- `tests/unit/test_portfolio_cli.py`

Modify:

- `src/aquant/portfolio/__init__.py`
- `pyproject.toml`
- `docs/superpowers/specs/2026-07-27-shared-cash-portfolio-design.md`

Do not modify:

- `src/aquant/backtest/**`
- `src/aquant/data/**`
- `src/aquant/rules/**`
- `src/aquant/universe.py`
- `scripts/verify_v01.sh`
- `release/v0.1-research/**`

Do not import these private v0.1 symbols:

- `aquant.backtest.runner._run_id`
- `aquant.backtest.runner._implementation_digest`
- any `aquant.backtest.export._*`
- any `aquant.release_manifest._*`
- any `aquant.backtest_cli._*`

Gate C uses synthetic verified inputs only. Gate D owns v0.1/v0.2 economic equivalence. Gate E owns the frozen `pilot-10` run. No strategy, security, broker, slippage, partial-fill, or market-rule expansion belongs in this plan.

## Fixed Gate C contracts

The portfolio schema and behavior constants are explicit identity inputs:

```python
PORTFOLIO_SCHEMA_VERSION = "0.2.0"
PORTFOLIO_ENGINE = "aquant-shared-cash-portfolio-0.2"
PRICE_STREAM_VERSION = "raw-open-close-v1"
DIVIDEND_TAX_MODE = "gross-before-personal-tax-v1"
NO_BAR_VALUATION_MODE = "carry-last-mark-cash-dividend-adjusted-v1"
BUDGET_MODE = "fixed-equal-notional-fee-aware-lot-reduction-v1"
RETRY_MODE = "next-official-session-bounded-attempts-v1"
```

The final implementation fingerprint is the byte hash of this exact sorted tuple:

```python
_IMPLEMENTATION_FILES = (
    "pyproject.toml",
    "src/aquant/backtest/data_access.py",
    "src/aquant/backtest/feed.py",
    "src/aquant/data/calendar_snapshot.py",
    "src/aquant/data/corporate_actions.py",
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
    "src/aquant/rules/__init__.py",
    "src/aquant/rules/engine.py",
    "src/aquant/rules/fees.py",
    "src/aquant/rules/lots.py",
    "src/aquant/rules/models.py",
    "src/aquant/rules/price_limits.py",
    "src/aquant/universe.py",
)
```

Task 1 starts with the exact subset whose files exist at that checkpoint. Tasks 2–5 append their named new files in the same commit. Task 5 must end with the exact tuple above. At every checkpoint, a parameterized test must make each listed file unreadable and then change one byte returned by the implementation-file reader; missing bytes must fail closed and changed bytes must change `implementation_digest` and `run_id`.

The audit bundle has exactly these payloads plus `artifact_manifest.json`:

```text
run.json
targets.csv
orders.csv
fills.csv
positions.csv
lots.csv
cash.csv
equity.csv
receivables.csv
corporate_actions.csv
availability.csv
metrics.json
```

`cash.csv` is the replayable event ledger. `equity.csv` is the daily portfolio identity and includes shared cash, market value, receivables, and equity. `positions.csv` contains one row per session and symbol valuation. No information is inferred from wall-clock time, current working directory, process ID, object address, or unordered iteration.

### Task 1: Deterministic input closure and verified run binding

**Files:**

- Create: `tests/portfolio_gate_c_support.py`
- Create: `tests/portfolio_identity_probe.py`
- Create: `tests/unit/test_portfolio_identity.py`
- Create: `src/aquant/portfolio/identity.py`
- Modify: `src/aquant/portfolio/__init__.py`

- [ ] **Step 1: Add one loader-backed Gate C fixture**

`tests/portfolio_gate_c_support.py` must expose:

```python
def make_portfolio_case(
    root: Path,
    *,
    symbols: tuple[str, ...] = ("600000", "600001"),
    initial_cash_fen: int = 2_000_000,
    gross_target_weight: Decimal = Decimal("1"),
) -> dict[str, object]:
    ...
```

It must use the existing public snapshot, calendar, corporate-action, universe, and fee-policy factories and loaders. It must not construct verified objects with private tokens. Use three official sessions, two market bars per symbol, fixed 10.00-yuan prices, no corporate-action events, and reversed symbol input as one supported case.

- [ ] **Step 2: Write the failing identity and binding tests**

```python
def test_identity_ignores_tuple_order_clock_pid_and_temporary_root(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("time.time", lambda: 1.0)
    monkeypatch.setattr("os.getpid", lambda: 11)
    first = run_verified_portfolio(
        **make_portfolio_case(tmp_path / "first", symbols=("600001", "600000"))
    )

    monkeypatch.setattr("time.time", lambda: 9_999_999.0)
    monkeypatch.setattr("os.getpid", lambda: 99_999)
    second = run_verified_portfolio(
        **make_portfolio_case(tmp_path / "second", symbols=("600000", "600001"))
    )

    assert first.identity.run_id == second.identity.run_id
    assert first.identity.input_closure_digest == second.identity.input_closure_digest


def test_each_bound_input_changes_run_identity(tmp_path):
    baseline = run_verified_portfolio(**make_portfolio_case(tmp_path / "base"))
    changed_cash = run_verified_portfolio(
        **make_portfolio_case(tmp_path / "cash", initial_cash_fen=2_000_100)
    )
    assert changed_cash.identity.run_id != baseline.identity.run_id


def test_verified_run_detects_post_construction_result_mutation(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    object.__setattr__(run.result.ledger, "cash_fen", 0)
    with pytest.raises(PortfolioError) as captured:
        verify_portfolio_run(run)
    assert captured.value.code == "verified_portfolio_run_modified"
```

Also cover: exact-type rejection, a forged/subclassed wrapper, changed universe membership, calendar, fee policy, market snapshot identity, corporate-action identity or coverage, any behavior-mode constant, and Python hash seeds.

- [ ] **Step 3: Run the tests and verify RED**

```bash
PYTHONPATH=src uv run --no-sync pytest -q tests/unit/test_portfolio_identity.py
```

Expected: collection fails because `aquant.portfolio.identity` does not exist.

- [ ] **Step 4: Implement canonical identity records and the verified wrapper**

```python
@dataclass(frozen=True)
class PortfolioRunIdentity:
    schema_version: str
    engine: str
    run_id: str
    implementation_digest: str
    input_closure_digest: str
    universe_id: str
    calendar_id: str
    calendar_sha256: str
    fee_policy_digest: str
    input_closure_json: bytes


@dataclass(frozen=True, init=False)
class VerifiedPortfolioRun:
    identity: PortfolioRunIdentity
    result: PortfolioBacktestResult


def run_verified_portfolio(
    *,
    config: PortfolioConfig,
    inputs: tuple[PortfolioInstrumentInput, ...],
    universe: VerifiedUniverse,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
) -> VerifiedPortfolioRun:
    ...


def verify_portfolio_run(run: VerifiedPortfolioRun) -> None:
    ...
```

`run_verified_portfolio()` must:

1. re-run every public exact-object verifier;
2. sort inputs by symbol through `validate_portfolio_inputs()`;
3. build canonical input-closure JSON with full config, sorted universe members, calendar ID/hash, fee digest, each market snapshot ID/hash/canonical digest, each corporate-action snapshot ID/hash/schema/version/coverage, and all five behavior-mode versions;
4. hash a fixed repository-relative implementation whitelist;
5. call `run_portfolio_backtest()` once;
6. bind the exact result and its canonical dataclass digest in a private identity registry.

Compute the final identity exactly as:

```python
identity_payload = {
    "engine": PORTFOLIO_ENGINE,
    "implementation_digest": implementation_digest,
    "input_closure_digest": input_closure_digest,
    "schema_version": PORTFOLIO_SCHEMA_VERSION,
}
run_id = sha256(canonical_json_bytes(identity_payload)).hexdigest()
```

The initial fixed whitelist must name the exact Task 1 subset of `_IMPLEMENTATION_FILES`. Every later task adds its named new production file to this explicit tuple in the same commit. Do not use a directory glob: a missing required file must raise `PortfolioError("implementation_file_missing", ...)`, not silently disappear from the digest. Sort repository-relative POSIX paths before hashing; paths locate bytes but never enter `run_id` as absolute values.

`input_closure_json` is the already-canonical UTF-8 JSON plus one LF. The registry must bind its SHA-256 to `input_closure_digest`, and `verify_portfolio_run()` must reject changed bytes, duplicate keys, or noncanonical reserialization.

`verify_portfolio_run()` must require the exact registered object and recompute the canonical result digest. Mutating either the wrapper, identity, nested result, ledger, attempt, fee evidence, or daily valuation must fail.

- [ ] **Step 5: Add the cross-process hash-seed probe**

`tests/portfolio_identity_probe.py` prints only canonical JSON containing `run_id`, `implementation_digest`, and `input_closure_digest`. Invoke it under `PYTHONHASHSEED=1` and `PYTHONHASHSEED=98765`; stdout bytes must match.

- [ ] **Step 6: Run focused tests and Ruff**

```bash
PYTHONPATH=src uv run --no-sync pytest -q tests/unit/test_portfolio_identity.py
PYTHONPATH=src uv run --no-sync ruff check \
  src/aquant/portfolio/identity.py \
  tests/portfolio_gate_c_support.py \
  tests/portfolio_identity_probe.py \
  tests/unit/test_portfolio_identity.py
```

Expected: all identity tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add \
  src/aquant/portfolio/identity.py \
  src/aquant/portfolio/__init__.py \
  tests/portfolio_gate_c_support.py \
  tests/portfolio_identity_probe.py \
  tests/unit/test_portfolio_identity.py
git commit -m "feat: bind deterministic portfolio run identity"
```

### Task 2: Integer-fen portfolio metrics

**Files:**

- Create: `tests/unit/test_portfolio_metrics.py`
- Create: `src/aquant/portfolio/metrics.py`
- Modify: `src/aquant/portfolio/coordinator.py`
- Modify: `src/aquant/portfolio/identity.py`
- Modify: `src/aquant/portfolio/__init__.py`
- Modify: `tests/unit/test_portfolio_coordinator.py`
- Modify: `tests/unit/test_portfolio_identity.py`

- [ ] **Step 1: Write failing metric tests**

```python
def test_metrics_use_daily_integer_fen_equity_and_fixed_252_session_basis(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    metrics = compute_portfolio_metrics(run)

    assert metrics.observation_count == 1
    assert metrics.observed_return_count == 1
    assert metrics.annual_sessions == 252
    assert metrics.risk_free_rate == Decimal("0")
    assert metrics.total_paid_fees_fen == 1_019
    assert metrics.fee_lot_reduction_fen == 100_000
    assert metrics.research_only is True
    assert metrics.live_trading is False
    assert metrics.profit_claim is False
```

Add a multi-session deterministic ledger fixture and assert exact total return, drawdown, daily exposure, highest symbol weight, target/actual weight deviation, trade count, and turnover. Assert:

- fewer than two return observations yields `annualized_volatility=None` and `sharpe_zero_rate=None`;
- zero return standard deviation yields `sharpe_zero_rate=None`;
- no NaN or infinity can enter canonical output;
- tampered or unverified runs are rejected;
- metrics never use Backtrader float cash.

- [ ] **Step 2: Run the tests and verify RED**

```bash
PYTHONPATH=src uv run --no-sync pytest -q tests/unit/test_portfolio_metrics.py
```

Expected: collection fails because `aquant.portfolio.metrics` does not exist.

- [ ] **Step 3: Implement exact portfolio metrics**

```python
@dataclass(frozen=True)
class PortfolioMetrics:
    observation_count: int
    observed_return_count: int
    annual_sessions: int
    risk_free_rate: Decimal
    total_return: Decimal
    annualized_return: Decimal
    annualized_volatility: Decimal | None
    sharpe_zero_rate: Decimal | None
    max_drawdown: Decimal
    turnover: Decimal
    trade_count: int
    rejected_attempt_count: int
    max_gross_exposure: Decimal
    max_symbol_weight: Decimal
    max_target_weight_deviation: Decimal
    daily_gross_exposure: tuple[tuple[date, Decimal], ...]
    final_symbol_weight_deviations: tuple[tuple[str, Decimal], ...]
    planned_cash_reserve_fen: int
    allocation_rounding_remainder_fen: int
    ordinary_lot_rounding_fen: int
    fee_lot_reduction_fen: int
    expired_uninvested_fen: int
    rejected_uninvested_fen: int
    total_paid_fees_fen: int
    research_only: bool = True
    live_trading: bool = False
    profit_claim: bool = False


def compute_portfolio_metrics(run: VerifiedPortfolioRun) -> PortfolioMetrics:
    ...
```

Use `Decimal` inputs derived from `DailyAccountSnapshot.equity_fen` and `SymbolValuation.market_value_fen`. Quantize published decimal strings to 12 fractional places with `ROUND_HALF_UP`, then strip trailing zeros without emitting scientific notation.

Treat `config.initial_cash_fen` on `config.signal_date` as the base equity immediately before the first post-signal official session. Therefore `observed_return_count` equals the number of daily snapshots, while volatility and Sharpe remain `None` until at least two daily returns exist.

For each filled target:

```text
ordinary_lot_rounding_fen
    = target_notional_fen - notional(initial_candidate_size, fill_unit_cost)
fee_lot_reduction_fen
    = notional(initial_candidate_size - requested_size, fill_unit_cost)
```

First extend a filled `EntryAttempt` with exact audit evidence:

```python
cash_available_before_fen: int | None
initial_candidate_cash_required_fen: int | None
requested_cash_required_fen: int | None
quantity_adjustment_reason: str | None
```

For an unshrunk fill, the reason is `None` and both required-cash values are equal. For a fee-aware shrink, the reason is exactly `insufficient_cash_including_fees` and the coordinator must prove:

```text
cash_available_before_fen < initial_candidate_cash_required_fen
cash_available_before_fen >= requested_cash_required_fen
initial_candidate_size > requested_size
```

The existing 1,000-to-900 share regression must assert:

```text
cash_available_before_fen = 999490
initial_candidate_cash_required_fen = 1000510
requested_cash_required_fen = 900509
quantity_adjustment_reason = insufficient_cash_including_fees
```

Rejected attempts without a priced candidate keep all four evidence fields `None`. This adds audit evidence only; it must not alter the Gate B order decision or cash result.

Map filled attempts to `CashLedgerEvent` and `PositionLot` through `fill_event_id`, event reference, and lot ID. An expired target contributes its full fixed target notional only to `expired_uninvested_fen`; an end-of-window pending target with at least one rejected attempt contributes its full target notional only to `rejected_uninvested_fen`. A zero-attempt pending target is invalid for formal metrics. These categories are mutually exclusive and must not double-count rejected attempts.

`daily_gross_exposure` contains every official snapshot session in ascending order. `final_symbol_weight_deviations` contains every universe symbol in symbol order, including zero-position members, and stores final actual weight minus fixed target weight. `rejected_attempt_count` is a count only; rejected attempts never create a second uninvested-cash amount on top of their root target's final filled, expired, or pending category.

Assert this exact target-allocation conservation identity:

```text
gross_target_notional_fen
= allocation_rounding_remainder_fen
+ invested_notional_fen
+ ordinary_lot_rounding_fen
+ fee_lot_reduction_fen
+ rejected_uninvested_fen
+ expired_uninvested_fen
```

`fee_lot_reduction_fen` is the lost notional from `initial_candidate_size - requested_size`; the three cash-evidence fields independently prove that fees caused the reduction. Do not infer the cause merely because the two sizes differ.

Add `src/aquant/portfolio/metrics.py` to the explicit implementation whitelist and assert that omitting, renaming, or changing its bytes changes or invalidates the implementation digest.

- [ ] **Step 4: Run focused tests and Ruff**

```bash
PYTHONPATH=src uv run --no-sync pytest -q tests/unit/test_portfolio_metrics.py
PYTHONPATH=src uv run --no-sync ruff check \
  src/aquant/portfolio/metrics.py \
  tests/unit/test_portfolio_metrics.py
```

Expected: all metric tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit**

```bash
git add \
  src/aquant/portfolio/metrics.py \
  src/aquant/portfolio/coordinator.py \
  src/aquant/portfolio/identity.py \
  src/aquant/portfolio/__init__.py \
  tests/unit/test_portfolio_metrics.py \
  tests/unit/test_portfolio_coordinator.py \
  tests/unit/test_portfolio_identity.py
git commit -m "feat: compute audited portfolio metrics"
```

### Task 3: Deterministic payloads and no-replace atomic export

**Files:**

- Create: `tests/unit/test_portfolio_export.py`
- Create: `src/aquant/portfolio/export.py`
- Modify: `src/aquant/portfolio/identity.py`
- Modify: `src/aquant/portfolio/__init__.py`
- Modify: `tests/unit/test_portfolio_identity.py`

- [ ] **Step 1: Write failing payload and export tests**

```python
def test_export_is_byte_deterministic_and_idempotent(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path / "inputs"))
    first = export_portfolio_run(run, tmp_path / "outputs")
    before = {path.name: path.read_bytes() for path in first.iterdir()}
    second = export_portfolio_run(run, tmp_path / "outputs")
    after = {path.name: path.read_bytes() for path in second.iterdir()}

    assert first == second == tmp_path / "outputs" / run.identity.run_id
    assert before == after
    assert set(before) == EXPECTED_PORTFOLIO_ARTIFACT_FILES
```

Also assert exact headers, row order, decimal/date/enum serialization, LF line endings, `metrics.json` null behavior, and the exact manifest entry shape:

```json
{
  "sha256": "<lowercase sha256>",
  "row_count": 1,
  "run_id": "<same run id>",
  "schema_version": "0.2.0"
}
```

The manifest top level is exact:

```json
{
  "artifact_schema_version": "0.2.0",
  "files": {
    "<payload name>": {
      "row_count": 1,
      "run_id": "<same lowercase SHA-256>",
      "schema_version": "0.2.0",
      "sha256": "<payload lowercase SHA-256>"
    }
  },
  "run_id": "<same lowercase SHA-256>",
  "status": "complete"
}
```

Write negative tests for partial existing directories, extra files, conflicting bytes, target-directory symlinks, payload symlinks, payload hardlinks, unsafe lock files, and a simulated competitor creating the final directory immediately before publish. No negative case may overwrite or supplement existing content.

- [ ] **Step 2: Run the tests and verify RED**

```bash
PYTHONPATH=src uv run --no-sync pytest -q tests/unit/test_portfolio_export.py
```

Expected: collection fails because `aquant.portfolio.export` does not exist.

- [ ] **Step 3: Implement canonical payload generation**

```python
def portfolio_payload_bytes(
    run: VerifiedPortfolioRun,
) -> dict[str, tuple[bytes, int]]:
    ...


def export_portfolio_run(
    run: VerifiedPortfolioRun,
    output_root: str | Path,
) -> Path:
    ...
```

`portfolio_payload_bytes()` must call `verify_portfolio_run()` and `compute_portfolio_metrics()` first. Serialize:

- `run.json` with the complete run identity, full canonical input closure, config, behavior-mode versions, row counts, and every touched fee-rate record;
- root targets sorted by symbol;
- orders sorted by execution session, symbol, and attempt number;
- fills as the filled subset joined to fee and lot evidence;
- positions by session then symbol;
- lots by symbol, acquired date, and lot ID;
- cash events by session and event ID;
- daily equity by session;
- receivables by actual cash date and event ID;
- corporate-action audits by ex-date, symbol, and event ID;
- availability by session and symbol.

Use `csv.DictWriter` with explicit field tuples. Never derive headers from arbitrary dictionaries.

Add `src/aquant/portfolio/export.py` to the explicit implementation whitelist in the same commit and extend the missing/changed implementation-file identity test.

- [ ] **Step 4: Implement safe atomic publication**

The publisher must:

1. require a safe real output root;
2. acquire a single-link regular lock file with `O_NOFOLLOW`;
3. create a same-parent hidden temporary directory;
4. write each file with exclusive creation, flush, and file `fsync`;
5. `fsync` the temporary directory;
6. publish with an OS no-replace atomic rename primitive;
7. `fsync` the parent directory;
8. fail closed if no no-replace primitive is available;
9. remove only its own validated temporary directory after failure.

On macOS use `renamex_np(..., RENAME_EXCL)`. If Linux support is implemented, use `renameat2(..., RENAME_NOREPLACE)`. Do not fall back to overwrite-capable `os.replace()` or unchecked `os.rename()`.

If the final directory already exists, independently compare its exact safe file set and bytes. An identical package is idempotent success; every other state is `PortfolioExportError("artifact_conflict", ...)`.

- [ ] **Step 5: Run focused tests and Ruff**

```bash
PYTHONPATH=src uv run --no-sync pytest -q tests/unit/test_portfolio_export.py
PYTHONPATH=src uv run --no-sync ruff check \
  src/aquant/portfolio/export.py \
  tests/unit/test_portfolio_export.py
```

Expected: all export tests pass and Ruff reports no errors.

- [ ] **Step 6: Commit**

```bash
git add \
  src/aquant/portfolio/export.py \
  src/aquant/portfolio/identity.py \
  src/aquant/portfolio/__init__.py \
  tests/unit/test_portfolio_export.py \
  tests/unit/test_portfolio_identity.py
git commit -m "feat: atomically export portfolio audit bundles"
```

### Task 4: Independent reverse verifier

**Files:**

- Create: `tests/unit/test_portfolio_verify.py`
- Create: `src/aquant/portfolio/verify.py`
- Modify: `src/aquant/portfolio/identity.py`
- Modify: `src/aquant/portfolio/__init__.py`
- Modify: `tests/unit/test_portfolio_identity.py`
- Modify: `docs/superpowers/specs/2026-07-27-shared-cash-portfolio-design.md`

- [ ] **Step 1: Add `verify.py` to the approved module map**

Document that `verify.py` independently parses artifact bytes, reconstructs typed audit rows, replays accounting identities, and must not use exporter-generated expected bytes as its sole proof.

- [ ] **Step 2: Write the failing reverse-verification tests**

```python
def test_verifier_reconstructs_complete_bundle(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path / "inputs"))
    directory = export_portfolio_run(run, tmp_path / "outputs")
    artifact = verify_portfolio_artifact(
        directory,
        expected_run_id=run.identity.run_id,
    )

    assert artifact.run_id == run.identity.run_id
    assert artifact.status == "verified"
    assert artifact.file_count == 12
    assert artifact.trade_count == 2


@pytest.mark.parametrize("filename", sorted(EXPECTED_PORTFOLIO_ARTIFACT_FILES))
def test_any_payload_byte_damage_is_detected(tmp_path, filename):
    directory = exported_case(tmp_path)
    path = directory / filename
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(PortfolioArtifactError):
        verify_portfolio_artifact(directory)
```

Also cover duplicate JSON keys, unknown/missing manifest fields, noncanonical JSON, changed manifest hash, wrong row count/schema/run ID, duplicate or reordered primary keys, malformed CSV numbers/dates, cash replay failure, daily equity failure, position/lot mismatch, receivable mismatch, metric recomputation mismatch, missing/extra files, directory/file symlinks, file hardlinks, and an uppercase or malformed hash.

- [ ] **Step 3: Run the tests and verify RED**

```bash
PYTHONPATH=src uv run --no-sync pytest -q tests/unit/test_portfolio_verify.py
```

Expected: collection fails because `aquant.portfolio.verify` does not exist.

- [ ] **Step 4: Implement strict independent parsing**

```python
@dataclass(frozen=True)
class VerifiedPortfolioArtifact:
    run_id: str
    status: str
    artifact_manifest_sha256: str
    file_count: int
    trade_count: int
    row_counts: tuple[tuple[str, int], ...]


def verify_portfolio_artifact(
    directory: str | Path,
    *,
    expected_run_id: str | None = None,
) -> VerifiedPortfolioArtifact:
    ...
```

The verifier must open files with `O_NOFOLLOW`, require regular single-link files, parse JSON with duplicate-key rejection, require the exact top-level manifest contract above with `status="complete"`, and compare each payload file's raw SHA-256 before parsing it. It must reconstruct typed rows with exact `int`, `Decimal`, `date`, and enum boundaries. A one-byte change to `artifact_manifest.json` itself must also fail canonical parsing or exact-contract validation.

Recompute `input_closure_digest` from the canonical closure in `run.json`. Independently reconstruct the persisted semantic result, normalize ordering and Decimal scale, recompute `result_digest`, then rebuild the canonical identity payload and `run_id`. Require equality with `run.json`, the artifact manifest, the directory name, and `expected_run_id` when supplied.

Independently replay:

```text
cash_after = cash_before - buy_notional - fees
receivable registration: cash unchanged
receivable payment: cash increases by amount and unpaid balance falls
daily equity = cash + sum(position market values) + receivables
available_size + locked_size = total_size
```

Recompute metrics from reconstructed integer-fen rows and compare canonical `metrics.json` values. `verify.py` must not import or call `export.py`, `run_verified_portfolio()`, the identity builder, or `compute_portfolio_metrics()`. It may share only immutable schema constants, enums, and dataclass type declarations. The verifier owns separate parsing, identity hashing, accounting replay, and metric-recomputation code.

Without an externally trusted `expected_run_id`, reverse verification proves internal consistency only, not origin authenticity: a party that can rewrite every payload and every hash can create a different internally valid run. Publication acceptance must therefore pass the approved run ID from a release manifest, read-only record, or equivalent external trust anchor. Local SHA-256 is not a signature or trusted timestamp.

Add `src/aquant/portfolio/verify.py` to the explicit implementation whitelist and extend the implementation-file identity test.

- [ ] **Step 5: Run focused tests and Ruff**

```bash
PYTHONPATH=src uv run --no-sync pytest -q tests/unit/test_portfolio_verify.py
PYTHONPATH=src uv run --no-sync ruff check \
  src/aquant/portfolio/verify.py \
  tests/unit/test_portfolio_verify.py
```

Expected: all verifier tests pass and Ruff reports no errors.

- [ ] **Step 6: Commit**

```bash
git add \
  src/aquant/portfolio/verify.py \
  src/aquant/portfolio/identity.py \
  src/aquant/portfolio/__init__.py \
  tests/unit/test_portfolio_verify.py \
  tests/unit/test_portfolio_identity.py \
  docs/superpowers/specs/2026-07-27-shared-cash-portfolio-design.md
git commit -m "feat: independently verify portfolio audit bundles"
```

### Task 5: Safe offline portfolio CLI

**Files:**

- Create: `tests/unit/test_portfolio_cli.py`
- Create: `src/aquant/portfolio_cli.py`
- Modify: `src/aquant/portfolio/identity.py`
- Modify: `tests/unit/test_portfolio_identity.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing CLI contract tests**

```python
def test_cli_runs_only_explicit_verified_inputs_and_emits_stable_json(
    tmp_path,
    capsys,
):
    case = materialized_cli_case(tmp_path)
    exit_code = main(case.arguments)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload == {
        "artifact_directory": case.expected_relative_artifact,
        "run_id": case.expected_run_id,
        "status": "ok",
        "symbol_count": 2,
    }


def test_cli_verify_rejects_one_damaged_byte(tmp_path, capsys):
    case = materialized_cli_case(tmp_path)
    assert main(case.arguments) == 0
    damage_one_payload(case.artifact_directory)
    assert main(["verify", "--project-root", str(tmp_path), "--artifact", case.relative_artifact]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "error"
    assert error["error_code"] == "artifact_hash_mismatch"
```

Add exact tests for:

- duplicate, missing, extra, or malformed `SYMBOL=SHA256` market/action mappings;
- mapping symbols not equal to the verified universe;
- explicit absolute input/output paths, `..`, symlinked path components, and unsafe hardlinks;
- argument parser failures that do not echo raw values;
- output conflicts that remain unchanged;
- a public-entry network tripwire on `socket`, `requests`, and AKShare;
- `verify` requiring an optional exact expected run ID;
- two runs from different temporary project roots producing the same run ID and artifact bytes.

- [ ] **Step 2: Run the tests and verify RED**

```bash
PYTHONPATH=src uv run --no-sync pytest -q tests/unit/test_portfolio_cli.py
```

Expected: collection fails because `aquant.portfolio_cli` does not exist.

- [ ] **Step 3: Implement the two-command CLI**

```text
aquant-portfolio run
  --project-root .
  --manifest data/manifests/manifest.jsonl
  --corporate-action-manifest data/corporate_actions/manifest.jsonl
  --output outputs/portfolios
  --calendar-id <sha256>
  --universe-id <sha256>
  --market-snapshot SYMBOL=SHA256          # repeat for every member
  --corporate-action-snapshot SYMBOL=SHA256 # repeat for every member
  --initial-cash-fen <positive int>
  --gross-target-weight <exact decimal>
  --signal-date YYYY-MM-DD
  --end-date YYYY-MM-DD
  --max-entry-attempts 5
  --stock-commission-rate <exact decimal>
  --stock-minimum-commission <exact decimal>
  --etf-commission-rate <exact decimal>
  --etf-minimum-commission <exact decimal>

aquant-portfolio verify
  --project-root .
  --artifact outputs/portfolios/<run_id>
  --expected-run-id <optional sha256>
```

The parser must raise a sanitized `PortfolioCliError(code, message)`. Path checking must reject lexically absolute values and `..` before descriptor-based traversal. Do not use `resolve()+relative_to()` as the security boundary.

The `run` command must call only public manifest readers, verified loaders, `make_fee_policy()`, `run_verified_portfolio()`, and `export_portfolio_run()`. It must never select “latest” snapshots and must never call a network provider.

Add both `src/aquant/portfolio_cli.py` and `pyproject.toml` to the explicit implementation whitelist and extend the missing/changed implementation-file identity test.

- [ ] **Step 4: Register the console entry**

```toml
[project.scripts]
aquant-portfolio = "aquant.portfolio_cli:main"
```

- [ ] **Step 5: Run focused tests and an installed-entry smoke test**

```bash
PYTHONPATH=src uv run --no-sync pytest -q tests/unit/test_portfolio_cli.py
uv sync --frozen --no-editable --reinstall-package a-share-quant
uv run --no-sync aquant-portfolio --help
PYTHONPATH=src uv run --no-sync ruff check \
  src/aquant/portfolio_cli.py \
  tests/unit/test_portfolio_cli.py
```

Expected: CLI tests pass, help exits zero, and Ruff reports no errors.

- [ ] **Step 6: Commit**

```bash
git add \
  src/aquant/portfolio_cli.py \
  src/aquant/portfolio/identity.py \
  tests/unit/test_portfolio_cli.py \
  tests/unit/test_portfolio_identity.py \
  pyproject.toml
git commit -m "feat: add offline portfolio audit CLI"
```

### Task 6: Gate C determinism, regression, and independent review

**Files:**

- Modify: `tests/unit/test_portfolio_identity.py`
- Modify: `tests/unit/test_portfolio_export.py`
- Modify: `tests/unit/test_portfolio_verify.py`
- Create: `outputs/Codex自审_v0.2_Gate_C.md`
- Create: `outputs/Work_Buddy代码复核_v0.2_Gate_C.md`

- [ ] **Step 1: Add the final cross-module acceptance tests**

Run the same two-symbol verified case with:

- forward and reversed input tuples;
- different project and temporary directories;
- patched wall clock and PID;
- `PYTHONHASHSEED=1` and `98765`;
- two consecutive exports.

Assert identical `run_id`, `implementation_digest`, `input_closure_digest`, and all thirteen package files including `artifact_manifest.json`.

Then change exactly one item at a time—config, universe member, calendar content, fee policy, market snapshot, corporate-action coverage, implementation source byte, or behavior-mode version—and assert a different `run_id`.

For each of the twelve payload files and `artifact_manifest.json`, damage one byte and assert independent verification failure without writing, deleting, or repairing any file.

- [ ] **Step 2: Run Gate C focused tests**

```bash
PYTHONPATH=src uv run --no-sync pytest -q \
  tests/unit/test_portfolio_identity.py \
  tests/unit/test_portfolio_metrics.py \
  tests/unit/test_portfolio_export.py \
  tests/unit/test_portfolio_verify.py \
  tests/unit/test_portfolio_cli.py
```

Expected: all Gate C tests pass.

- [ ] **Step 3: Prove the v0.1 frozen boundary is unchanged**

```bash
git diff --name-only v0.1-research -- \
  src/aquant/backtest \
  src/aquant/data \
  src/aquant/rules \
  src/aquant/universe.py \
  scripts/verify_v01.sh \
  release/v0.1-research/inputs
```

Expected: no output.

- [ ] **Step 4: Run the complete local gate**

```bash
PYTHONPATH=src uv run --no-sync pytest -q
PYTHONPATH=src uv run --no-sync ruff check .
uv lock --check
uv build
PYTHONPATH=src ./scripts/verify_v01.sh
git diff --check
```

Expected:

- pytest has zero failures;
- Ruff reports `All checks passed!`;
- lock check resolves without changes;
- source and wheel builds succeed;
- frozen release reports `status="verified"` with 20 baselines, 30 candidates, and 100 replay rows;
- `git diff --check` emits no output.

- [ ] **Step 5: Write the Codex self-review**

`outputs/Codex自审_v0.2_Gate_C.md` must state:

```text
LOCAL_GATE_C = PASS or FAIL
CODEX_SELF_REVIEW_P0 = <count>
CODEX_SELF_REVIEW_P1 = <count>
CODEX_SELF_REVIEW_P2 = <count>
WORK_BUDDY_REVIEW = REQUIRED
CLAUDE_REVIEW = RETIRED_BY_USER
```

It must separately report identity evidence, atomic-publication evidence, reverse-verification damage tests, CLI path/network evidence, v0.1 replay evidence, and the unchanged research/simulation boundary.

- [ ] **Step 6: Obtain Work Buddy independent review**

Work Buddy must inspect the committed Gate C diff and at least one real synthetic artifact. The review must try:

- a missing implementation fingerprint file;
- tuple reordering and hash-seed changes;
- a forged or mutated verified-run wrapper;
- a partial/conflicting existing directory;
- file and parent symlinks;
- payload hardlinks;
- a publish race at the no-replace boundary;
- one-byte damage in every payload;
- manifest duplicate keys and row-count lies;
- cross-file cash/equity/lot/receivable inconsistencies;
- CLI path traversal and network attempts.

Gate C passes only with:

```text
WORK_BUDDY_REVIEW = PASS
WORK_BUDDY_P0 = 0
WORK_BUDDY_P1 = 0
```

Every P2 must be fixed or explicitly approved for deferral by the user.

- [ ] **Step 7: Fix findings with one failing regression per issue**

For each accepted finding:

1. add the smallest failing test;
2. run it and record the expected failure;
3. implement the minimum fix;
4. run the focused test and full Gate C set;
5. ask Work Buddy to recheck the exact fix.

- [ ] **Step 8: Record the independent review and commit**

```bash
git add \
  src/aquant/portfolio \
  src/aquant/portfolio_cli.py \
  tests/portfolio_gate_c_support.py \
  tests/portfolio_identity_probe.py \
  tests/unit/test_portfolio_identity.py \
  tests/unit/test_portfolio_metrics.py \
  tests/unit/test_portfolio_export.py \
  tests/unit/test_portfolio_verify.py \
  tests/unit/test_portfolio_cli.py \
  outputs/Codex自审_v0.2_Gate_C.md \
  outputs/Work_Buddy代码复核_v0.2_Gate_C.md \
  docs/superpowers/specs/2026-07-27-shared-cash-portfolio-design.md \
  pyproject.toml
git commit -m "docs: record v02 gate c verification"
```

Do not start Gate D until the local Gate C evidence and Work Buddy review both satisfy the stated gate.

## Plan self-review checklist

- [ ] Every requirement in design sections 8, 9, 10, and 11.1 maps to an explicit task and test.
- [ ] The implementation never imports a forbidden v0.1 private symbol.
- [ ] `PortfolioBacktestResult` remains a pure engine result.
- [ ] Identity binds every data, rule, mode, and implementation input named in the design.
- [ ] Export and verification are independent code paths.
- [ ] File and path safety covers symlinks, hardlinks, path traversal, publish races, conflicts, and partial bundles.
- [ ] All decimal, date, enum, and JSON-null serialization is canonical.
- [ ] Gate C does not run the frozen ten-symbol portfolio or compare v0.1/v0.2 economics.
- [ ] Claude remains retired and Work Buddy is the required independent reviewer.
