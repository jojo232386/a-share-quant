# Corporate Actions Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the four-symbol A-share baseline economically coherent across cash dividends, ex-right reference prices, indicator prices, and target-weight sizing without changing the Backtrader engine.

**Architecture:** Add a separately hashed corporate-action data boundary, derive a three-price research frame before it reaches Backtrader, and let the rule-aware broker apply dividend events before open-order processing. Strategies submit target-weight intents at the close; the broker converts those intents to fee-aware board-lot quantities at the next real open.

**Tech Stack:** Python 3.11, AKShare 1.18.64, Backtrader 1.9.78.123, pandas 3.0.3, PyArrow 25.0.0, pytest 9.0.2.

---

## File map

New focused modules:

- `src/aquant/data/corporate_actions.py` — event schema, strict normalization, content-addressed snapshot and exact verified loader.
- `src/aquant/backtest/price_streams.py` — deterministic `indicator_close` and `reference_price` derivation.
- `tests/unit/test_corporate_actions.py` — company-action source, storage, tamper and fail-closed tests.
- `tests/unit/test_price_streams.py` — causal price-stream tests.

Existing modules to change:

- `src/aquant/data/akshare_client.py` — fetch stock and ETF corporate-action inputs with safe provenance.
- `src/aquant/backtest/feed.py` — add `indicator_close` and `reference_price` lines.
- `src/aquant/backtest/models.py` — target-weight config and dividend audit records.
- `src/aquant/backtest/execution.py` — daily company-action lifecycle and next-open sizing.
- `src/aquant/backtest/strategies.py` — indicator-price SMA and target-weight order intent.
- `src/aquant/backtest/runner.py` — verified action input, enriched frame, run identity and accounting gate.
- `src/aquant/backtest/export.py` — action/receivable artifacts and tax-mode metadata.
- `src/aquant/backtest_cli.py` — verified action snapshot and `--target-weight`.
- `docs/price_adjustment_policy.md`, `docs/backtest_baselines.md`, `docs/scope.md` — correct stale behavior claims.

The universe refactor and six additional symbols are intentionally excluded from this plan. They start only after the corrected four-symbol gate passes.

### Task 1: Lock the corporate-action domain contract

**Files:**
- Create: `src/aquant/data/corporate_actions.py`
- Create: `tests/unit/test_corporate_actions.py`

- [ ] **Step 1: Write failing schema and stock-normalization tests**

Add tests that construct a CNInfo-like frame with:

```python
pd.DataFrame(
    {
        "实施方案公告日期": ["2018-05-31", "2018-05-31"],
        "送股比例": [None, None],
        "转增比例": [None, None],
        "派息比例": [10.0, 2.0],
        "股权登记日": ["2018-06-06", "2018-06-06"],
        "除权日": ["2018-06-07", "2018-06-07"],
        "派息日": ["2018-06-07", "2018-06-07"],
    }
)
```

Require one normalized event for `601318` with `cash_dividend_per_unit ==
Decimal("1.2")`, zero non-cash ratios, canonical dates, and a stable event ID. Add separate
tests for unknown columns, invalid dates, negative cash, payable date before ex-date, non-zero
送股/转增, duplicate normalized events, and bool-as-number values.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_corporate_actions.py -q
```

Expected: collection/import failure because `aquant.data.corporate_actions` does not exist.

- [ ] **Step 3: Implement the minimum immutable types and stock normalizer**

Define exact dataclasses:

```python
@dataclass(frozen=True)
class CorporateActionEvent:
    event_id: str
    symbol: str
    instrument_kind: InstrumentKind
    announcement_date: date | None
    record_date: date
    ex_date: date
    payable_date: date
    cash_dividend_per_unit: Decimal
    stock_dividend_ratio: Decimal
    capitalization_ratio: Decimal
    rights_ratio: Decimal
    rights_price: Decimal | None
    source_schema: str
    source_url: str


@dataclass(frozen=True)
class CorporateActionProvenance:
    snapshot_id: str
    file_sha256: str
    symbol: str
    instrument_kind: InstrumentKind
    provider: str
    normalization_version: str
    coverage_start: date
    coverage_end: date
    row_count: int
    verification_method: str
```

Use Decimal from source strings, not binary floats. Aggregate same-day stock cash rows only after
all fields match. Reject unsupported non-cash ratios with code
`unsupported_corporate_action`; reject source failure separately as
`unavailable_corporate_actions`.

- [ ] **Step 4: Verify GREEN**

Run the Task 1 test file. Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/data/corporate_actions.py tests/unit/test_corporate_actions.py
git commit -m "feat: define verified corporate action events"
```

### Task 2: Normalize ETF dividends with strict two-page matching

**Files:**
- Modify: `src/aquant/data/corporate_actions.py`
- Modify: `src/aquant/data/akshare_client.py`
- Modify: `tests/unit/test_corporate_actions.py`
- Modify: `tests/unit/test_akshare_client.py`

- [ ] **Step 1: Write failing ETF matching tests**

Use a cumulative frame:

```python
pd.DataFrame(
    {
        "日期": ["2023-01-16", "2024-01-18"],
        "累计分红": [0.600, 0.669],
    }
)
```

and a detail frame with record/payable/per-unit rows. Require 2024 cash =
`Decimal("0.069")`, record date `2024-01-17`, ex-date `2024-01-18`, payable
`2024-01-23`. Add RED tests for negative cumulative difference, amount mismatch, missing row,
duplicate detail row, HTTP failure, decoding failure, and changed table headers.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/unit/test_corporate_actions.py tests/unit/test_akshare_client.py -q
```

Expected: failures for missing ETF fetch/normalization functions.

- [ ] **Step 3: Implement the two inputs**

Keep `fund_etf_dividend_sina` behind the injected AKShare object. Add a small injectable HTTP
reader for:

```text
https://stock.finance.sina.com.cn/fundInfo/view/FundInfo_JJFH.php?symbol=<symbol>
```

Decode bytes with GB18030 and select a table only when its first logical row is exactly
`权益登记日、红利发放日、每份分红(元)`. Do not log response bodies or exception messages.
Match per-unit Decimal amounts exactly after canonical decimal normalization.

- [ ] **Step 4: Verify GREEN and existing client tests**

Run both files. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/data/corporate_actions.py src/aquant/data/akshare_client.py tests/unit/test_corporate_actions.py tests/unit/test_akshare_client.py
git commit -m "feat: verify stock and ETF dividend sources"
```

### Task 3: Store and reload immutable action snapshots

**Files:**
- Modify: `src/aquant/data/corporate_actions.py`
- Modify: `tests/unit/test_corporate_actions.py`

- [ ] **Step 1: Write failing storage and bypass tests**

Require:

- canonical UTF-8 JSON bytes with sorted keys and newline;
- path `data/corporate_actions/<symbol>/<sha256>.json`;
- separate append-only `data/corporate_actions/manifest.jsonl`;
- exact file SHA-256, symbol, kind, provider, normalization version, coverage and row count;
- idempotent repeated write;
- conflict refusal;
- symlink/path-traversal refusal;
- tampered bytes, wrong manifest hash, wrong row count/range and fake object refusal;
- returned `events` property is immutable and the verifier token cannot be forged through a
  public constructor.

- [ ] **Step 2: Verify RED**

Run the test file and confirm the new storage tests fail for missing classes.

- [ ] **Step 3: Implement an action-specific store and manifest**

Do not widen `ManifestRecord`, whose path and Parquet invariants are deliberately market-data
specific. Implement:

```python
class CorporateActionSnapshotStore: ...
class CorporateActionManifestWriter: ...
class VerifiedCorporateActions: ...
def load_verified_corporate_actions(...): ...
```

Use `O_NOFOLLOW`, regular-file/link-count checks, temporary file + fsync + atomic link/replace,
duplicate-key rejecting JSON, and an internal loader token. Include empty-but-complete snapshots
for symbols with no events; `row_count` may be zero only in this manifest.

- [ ] **Step 4: Verify GREEN**

Run corporate-action tests, then the full manifest/snapshot tests.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/data/corporate_actions.py tests/unit/test_corporate_actions.py
git commit -m "feat: add immutable corporate action snapshots"
```

### Task 4: Derive three causal price streams

**Files:**
- Create: `src/aquant/backtest/price_streams.py`
- Create: `tests/unit/test_price_streams.py`
- Modify: `src/aquant/backtest/feed.py`
- Modify: `tests/unit/test_market_data.py`

- [ ] **Step 1: Write failing price-stream tests**

Use raw closes `100, 98, 99` with a `2` cash dividend on the second date. Assert:

```python
reference_price == [NaN, Decimal("98"), Decimal("98")]
indicator_close == [100.0, 100.0, 99.0 * 100.0 / 98.0]
```

The third-day reference is the second-day raw close (`98`), not the ex-right reference. Add tests
for an event on a non-market date, multiple events on one ex-date, event outside coverage,
non-positive reference, first-bar event, modified verified actions and missing coverage.

- [ ] **Step 2: Verify RED**

Run `tests/unit/test_price_streams.py`; expected missing-module failure.

- [ ] **Step 3: Implement deterministic enrichment**

Expose:

```python
PRICE_STREAM_VERSION = "causal-cash-v1"

def derive_price_streams(
    raw_frame: pd.DataFrame,
    actions: VerifiedCorporateActions,
) -> pd.DataFrame:
    ...
```

Return a copy with canonical raw columns plus float64 `indicator_close` and
`reference_price`. Start cumulative factor at `Decimal("1")`; update only on ex-date using the
previous raw close/reference ratio. Hash the enriched values separately from the raw market
digest.

- [ ] **Step 4: Add a custom Backtrader feed**

Create a `bt.feeds.PandasData` subclass with:

```python
lines = ("indicator_close", "reference_price")
params = (("indicator_close", "indicator_close"), ("reference_price", "reference_price"))
```

Keep `close` mapped to raw close. `make_backtrader_feed` must reject frames missing either extra
line.

- [ ] **Step 5: Verify GREEN and commit**

Run price-stream and market-data tests, then commit.

### Task 5: Prove and implement next-open target sizing

**Files:**
- Modify: `src/aquant/backtest/models.py`
- Modify: `src/aquant/backtest/strategies.py`
- Modify: `src/aquant/backtest/execution.py`
- Modify: `tests/unit/test_backtest_baselines.py`
- Modify: `tests/unit/test_a_share_execution.py`

- [ ] **Step 1: Write the Backtrader private-contract RED test**

For a buy order submitted after day T close, instrument the broker so day T+1
`_try_exec_market` sets:

```python
order.size = desired_size
order.created.size = desired_size
order.executed.remsize = desired_size
```

before `super()._try_exec_market`. Assert one completed fill at T+1 open, the exact desired size,
no placeholder size in orders/fills, and no duplicate notification.

- [ ] **Step 2: Verify RED**

Run the exact test and confirm it fails because current execution remains `stake=100`.

- [ ] **Step 3: Replace config and strategy intent**

`BacktestConfig` becomes:

```python
@dataclass(frozen=True)
class BacktestConfig:
    strategy: StrategyName
    initial_cash: float
    target_weight: Decimal
    sma_period: int | None = None
    random_seed: int = 0
```

Validate exact Decimal, finite, `0 < target_weight <= 1`. Strategy buy submission stores
`target_weight` in order info. On notification, copy broker-set `requested_size` from info into
the audit record. SMA reads `self.data.indicator_close`; raw close remains the position ledger
price.

- [ ] **Step 4: Add RED sizing cases**

For open prices 10, 100 and 1,000 with identical cash and target 0.95, assert actual exposure is
the largest fee-affordable 100-share multiple at or below target. Add cases for minimum
commission, open gap, one-lot unaffordability, price-limit rejection before size mutation,
insufficient cash shrink, and sell-all behavior.

- [ ] **Step 5: Implement minimal fee-aware sizing**

In `_try_exec_market`, after date/limit eligibility and before `evaluate_order`, calculate open
equity and target size. Reuse `calculate_fees`; decrement by the instrument lot size until cash
covers notional plus fees. Record final size and target weight in order info. If the private
contract test does not pass, stop and revise the design rather than layering workarounds.

- [ ] **Step 6: Verify GREEN and commit**

Run baseline and A-share execution tests. Commit target sizing only after all pass.

### Task 6: Apply dividends before the open and audit receivables

**Files:**
- Modify: `src/aquant/backtest/models.py`
- Modify: `src/aquant/backtest/execution.py`
- Modify: `src/aquant/backtest/strategies.py`
- Modify: `tests/unit/test_a_share_execution.py`
- Modify: `tests/unit/test_backtest_baselines.py`

- [ ] **Step 1: Write RED lifecycle tests**

Build a held 100-share position with a 2 yuan dividend:

- ex-date: raw price drops 100→98, cash unchanged, receivable +200, equity continuous;
- payable date: cash +200, receivable 0, equity unchanged by the transfer;
- sale at ex-date open retains entitlement;
- purchase at ex-date open does not receive the already-registered dividend;
- no pending order still applies the dividend;
- `payable_date == ex_date` registers then pays exactly once;
- `payable_date < ex_date` is rejected by snapshot normalization.

- [ ] **Step 2: Verify RED**

Run the exact lifecycle tests and inspect the expected accounting failures.

- [ ] **Step 3: Implement broker-level daily events**

Bind the feed explicitly to `RuleAwareBackBroker`. Override `next()` to:

1. read current date;
2. register ex-date receivables from the pre-open position;
3. transfer due receivables directly into broker cash;
4. call `super().next()`.

Define:

```python
@dataclass(frozen=True)
class CorporateActionRecord: ...

@dataclass(frozen=True)
class ReceivableRecord:
    date: date
    balance: float
```

Override public `getvalue()` to add current receivables while leaving internal `_value` untouched.
Expose immutable audited records from the broker.

- [ ] **Step 4: Extend the daily ledger**

Record the receivable balance on every market date. Change the identity gate to join position,
cash, receivable and equity by exact date and assert:

```python
cash + market_value + receivable == equity
```

- [ ] **Step 5: Verify GREEN and commit**

Run lifecycle, baseline and accounting tests, then commit.

### Task 7: Bind actions to run identity and export

**Files:**
- Modify: `src/aquant/backtest/models.py`
- Modify: `src/aquant/backtest/runner.py`
- Modify: `src/aquant/backtest/export.py`
- Modify: `tests/unit/test_backtest_baselines.py`
- Modify: `tests/unit/test_snapshot_manifest.py`

- [ ] **Step 1: Write RED run-ID and bundle tests**

Assert run ID changes for action snapshot hash, normalization version, target weight, price-stream
version and dividend tax mode. Require bundle files:

```text
run.json
orders.csv
fills.csv
positions.csv
cash.csv
equity.csv
lots.csv
corporate_actions.csv
receivables.csv
missing_sessions.json
artifact_manifest.json
```

Assert every payload hash is in `artifact_manifest.json`, repeated identical export is
idempotent, partial/conflicting export is refused, and run.json declares
`gross_before_personal_tax`.

- [ ] **Step 2: Verify RED**

Run export/run-id tests and confirm missing fields/files.

- [ ] **Step 3: Require verified actions in formal runner**

Change:

```python
run_backtest(
    market_data,
    *,
    corporate_actions,
    calendar,
    fee_policy,
    config,
)
```

to require an exact `VerifiedCorporateActions` with matching symbol/kind and complete coverage.
Synthetic runner accepts a separately labelled synthetic action fixture. Enrich the frame before
feed creation, pass actions to the broker, and bind all new identities to run ID.

- [ ] **Step 4: Extend models and export**

Add the two CSVs, row counts, action provenance, price-stream version, actual exposure,
target-weight policy and tax disclosure. Increment result/artifact schema versions because the
bundle contract changes.

- [ ] **Step 5: Verify GREEN and commit**

Run export, snapshot and baseline suites; commit.

### Task 8: Update CLI without weakening path or source gates

**Files:**
- Modify: `src/aquant/backtest_cli.py`
- Modify: `src/aquant/cli.py`
- Modify: `tests/unit/test_ingestion_cli.py`
- Modify: `tests/unit/test_backtest_baselines.py`

- [ ] **Step 1: Write RED CLI tests**

Require `--corporate-action-snapshot-id` and `--target-weight`. Reject removed `--stake`,
unverified action IDs, mismatched symbol, action file outside project, path traversal/symlink,
unavailable source and unsupported action. Ensure all errors use the existing redacted JSON
envelope.

- [ ] **Step 2: Verify RED**

Run CLI tests and confirm missing-argument/unknown-option behavior.

- [ ] **Step 3: Implement commands**

Add an ingestion command for one symbol's corporate-action snapshot and formal backtest loading
by snapshot ID. Keep acquisition and backtest separate: a backtest never downloads from the
network. Default `target_weight` is serialized canonically as `"0.95"`.

- [ ] **Step 4: Verify GREEN and commit**

Run all CLI tests; commit.

### Task 9: Correct documentation and supersede old semantic packages

**Files:**
- Modify: `docs/price_adjustment_policy.md`
- Modify: `docs/backtest_baselines.md`
- Modify: `docs/scope.md`
- Modify: `outputs/A股量化项目_第3周交付与验收.md`
- Create: `outputs/backtests/index.json`
- Modify: `tests/unit/test_snapshot_manifest.py`

- [ ] **Step 1: Write the index RED tests**

Require atomic index publishing, no mutation of old run directories, exact 16 existing run IDs,
status `superseded_semantic_bug`, and the three reasons from the design. Reject unknown files,
duplicate IDs and index path symlinks.

- [ ] **Step 2: Verify RED**

Run the index tests; expected missing publisher.

- [ ] **Step 3: Implement the smallest index publisher**

Keep it in `src/aquant/backtest/export.py` unless the file exceeds a clear single responsibility;
then create `src/aquant/backtest/index.py`. Publish via temporary file, fsync and atomic replace.
Do not delete or move any old bundle.

- [ ] **Step 4: Update docs**

State that:

- raw prices execute and value positions;
- causal indicator prices feed SMA;
- reference prices feed limits;
- gross dividends enter receivable/cash;
- old Week 3 packages are engineering history, not valid performance baselines;
- corrected four-symbol packages still do not prove strategy effectiveness.

- [ ] **Step 5: Verify GREEN and commit**

Run index tests and Markdown link/path checks used by the repository, then commit.

### Task 10: Real four-symbol gate and independent review

**Files:**
- Create/update: `data/corporate_actions/**`
- Create: `outputs/A股量化项目_公司行为修正交付与验收.md`
- Create: `outputs/Claude代码级复核结论_公司行为修正.md`
- Create: corrected run directories under `outputs/backtests/`

- [ ] **Step 1: Run complete automated verification**

Run:

```bash
uv run pytest -q
uv run ruff check .
```

Expected: zero failures and zero lint errors.

- [ ] **Step 2: Acquire and verify four action snapshots**

Fetch 510300, 600519, 601318 and 000001. Re-open every snapshot through the exact verified loader.
Record event counts, coverage, total gross cash per unit and unsupported-event count.

- [ ] **Step 3: Run eight corrected packages**

For each symbol run Buy & Hold and SMA(20) with identical initial cash, target weight, calendar and
fee policy. Re-open each artifact manifest, recompute every SHA-256 and verify no old run directory
changed.

- [ ] **Step 4: Manual economic spot checks**

At minimum inspect:

- 600519 one ex-date and payable transfer;
- 601318 2018-06-07 aggregated two-row dividend;
- 510300 2024-01-18 ex-date and 2024-01-23 payment;
- exposure percentages for all four symbols.

Do not report strategy performance as evidence of correctness.

- [ ] **Step 5: Submit code and exact run IDs to Claude Code**

Claude must inspect source, run the full suite, recompute one dividend lifecycle and verify all
eight bundles. Gate requires `P0=0、P1=0`; P2 items must be documented with an explicit accept/fix
decision.

- [ ] **Step 6: Commit the verified delivery**

Commit only source, tests, docs and intended small manifests/reports. Do not commit large raw or
generated bundles unless repository policy already tracks them.

## Plan self-review

- Spec coverage: Tasks 1-4 cover verified events and three prices; Tasks 5-6 cover target sizing
  and dividend accounting; Tasks 7-9 cover identity, CLI, export and old-package status; Task 10
  covers real evidence and independent review.
- Scope separation: no universe refactor or new symbols are included before the four-symbol
  correctness gate.
- No silent fallback: source unavailability, incomplete ETF matching and unsupported actions all
  stop formal execution.
- Type consistency: formal runner accepts `VerifiedMarketData` plus
  `VerifiedCorporateActions`; strategy submits Decimal `target_weight`; output uses
  `CorporateActionRecord` and `ReceivableRecord`.
- Backtrader private API risk: isolated as the first test in Task 5, before production behavior
  relies on it.
