# A 股共享现金组合 Gate A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the immutable v0.2 portfolio contracts, deterministic equal-target allocation, and integer-fen shared ledger needed before any multi-symbol trading loop is written.

**Architecture:** Keep every file bound into the frozen v0.1 implementation digest unchanged. Add an isolated `aquant.portfolio` package that validates exact loader-created market and corporate-action objects, represents all cash in integer fen, and exposes small immutable accounting transitions. Gate A deliberately does not implement the calendar loop, order retries, portfolio run ID, export, CLI, or the formal 10-symbol run.

**Tech Stack:** Python 3.11, `dataclasses`, `Decimal`, pandas canonical digests, existing verified A-share data contracts, pytest, Ruff, uv.

---

## Scope and safety boundary

Create:

- `src/aquant/portfolio/__init__.py`
- `src/aquant/portfolio/models.py`
- `src/aquant/portfolio/accounting.py`
- `tests/unit/test_portfolio_models.py`
- `tests/unit/test_portfolio_accounting.py`

Do not modify:

- `src/aquant/backtest/**`
- `src/aquant/data/**`
- `src/aquant/rules/**`
- `src/aquant/universe.py`
- any file in `release/v0.1-research/`

The old v0.1 implementation digest therefore remains stable. After every production-code checkpoint,
reinstall the local package non-editably before the full suite because Python 3.11.15 ignores the
underscore-prefixed editable `.pth` generated in this environment:

```bash
uv sync --frozen --no-editable --reinstall-package a-share-quant
```

### Task 1: Lock the immutable configuration and equal-target allocation

**Files:**

- Create: `tests/unit/test_portfolio_models.py`
- Create: `src/aquant/portfolio/models.py`
- Create: `src/aquant/portfolio/__init__.py`

- [x] **Step 1: Write failing exact-type configuration tests**

Cover:

- `strategy` must be the exact `PortfolioStrategy.BUY_AND_HOLD`;
- `initial_cash_fen` must be a positive exact `int`, with `True` rejected;
- `gross_target_weight` must be a finite exact `Decimal` in `(0, 1]`;
- `signal_date` and `end_date` must be exact dates and strictly ordered;
- `max_entry_attempts` must be an exact integer in `[1, 20]`;
- formally approved values construct successfully.

```python
config = PortfolioConfig(
    strategy=PortfolioStrategy.BUY_AND_HOLD,
    initial_cash_fen=100_000_000,
    gross_target_weight=Decimal("0.95"),
    signal_date=date(2025, 1, 2),
    end_date=date(2026, 7, 22),
    max_entry_attempts=5,
)
assert config.initial_cash_fen == 100_000_000
```

- [x] **Step 2: Run the test and observe import failure**

```bash
uv run --no-sync pytest tests/unit/test_portfolio_models.py -q
```

Expected: FAIL because `aquant.portfolio` does not exist.

- [x] **Step 3: Implement the smallest immutable contract**

Add:

- `PortfolioError(code, message)`;
- `PortfolioStrategy`;
- frozen `PortfolioConfig`;
- frozen `TargetAllocation`;
- `allocate_equal_targets(config, member_count)`.

Allocation uses `ROUND_FLOOR`:

```text
gross_target_notional_fen = floor(initial_cash_fen * gross_target_weight)
per_symbol_target_notional_fen = floor(gross_target_notional_fen / member_count)
planned_cash_reserve_fen = initial_cash_fen - gross_target_notional_fen
allocation_rounding_remainder_fen
    = gross_target_notional_fen - per_symbol_target_notional_fen * member_count
```

- [x] **Step 4: Add allocation edge tests**

Cover member counts 1, 2, 3, and 10; reject booleans, zero, negative, and counts over 100. Assert:

```python
allocation = allocate_equal_targets(config, 10)
assert allocation.gross_target_notional_fen == 95_000_000
assert allocation.per_symbol_target_notional_fen == 9_500_000
assert allocation.planned_cash_reserve_fen == 5_000_000
assert allocation.allocation_rounding_remainder_fen == 0
```

Also use a value that leaves a non-zero allocation remainder and assert the exact conservation identity.

- [x] **Step 5: Run focused tests and Ruff**

```bash
uv run --no-sync pytest tests/unit/test_portfolio_models.py -q
uv run --no-sync ruff check src/aquant/portfolio tests/unit/test_portfolio_models.py
```

### Task 2: Enforce the verified portfolio input boundary

**Files:**

- Modify: `tests/unit/test_portfolio_models.py`
- Modify: `src/aquant/portfolio/models.py`
- Modify: `src/aquant/portfolio/__init__.py`

- [x] **Step 1: Add verified synthetic fixture helpers**

Use existing public test factories and loaders to create:

- an exact `VerifiedMarketData`;
- an exact `VerifiedCorporateActions`;
- an exact `VerifiedUniverse`.

Do not construct loader-guarded objects with private tokens.

- [x] **Step 2: Write failing input-contract tests**

Cover:

- naked DataFrame and dict rejection;
- market/corporate-action symbol mismatch;
- instrument-kind mismatch;
- modified market digest rejection;
- modified corporate-action object rejection;
- duplicate symbols;
- missing universe member;
- out-of-universe member;
- tuple ordering is normalized by symbol.

The public validator is:

```python
validated = validate_portfolio_inputs(inputs, universe=verified_universe)
assert tuple(item.symbol for item in validated) == tuple(sorted(expected_symbols))
```

- [x] **Step 3: Implement `PortfolioInstrumentInput`**

The frozen object stores exact `VerifiedMarketData` and `VerifiedCorporateActions`. Its validation must:

- require exact concrete types;
- recompute `canonical_market_digest(market_data.frame)`;
- call `verify_verified_corporate_actions`;
- require unadjusted market provenance;
- require matching symbol and `InstrumentKind`;
- require corporate-action coverage to include the entire market frame.

Expose read-only `symbol` and `instrument_kind` properties from verified provenance.

- [x] **Step 4: Implement `validate_portfolio_inputs`**

Require an exact tuple of exact `PortfolioInstrumentInput` objects and an exact verified universe.
Re-run every item’s digest checks at the public boundary, compare the complete `(symbol, kind)` set
against the universe, reject duplicates, and return a symbol-sorted tuple.

- [x] **Step 5: Run the input tests**

```bash
uv run --no-sync pytest tests/unit/test_portfolio_models.py -q
```

Expected: PASS.

### Task 3: Implement exact cash formulas and immutable buy postings

**Files:**

- Create: `tests/unit/test_portfolio_accounting.py`
- Create: `src/aquant/portfolio/accounting.py`
- Modify: `src/aquant/portfolio/__init__.py`

- [x] **Step 1: Write failing cash-formula tests**

Add `cash_after_fill` tests for buy and sell:

```text
buy cash after = before - notional - all fees
sell cash after = before + notional - all fees
```

Reject:

- negative or boolean fen values;
- zero/negative notional;
- a buy that would make shared cash negative;
- a non-exact `OrderSide`;
- a non-exact `FeeBreakdown`.

- [x] **Step 2: Implement exact amount helpers**

Add:

- `decimal_yuan_to_fen(value)` using `ROUND_HALF_UP`;
- `notional_fen(unit_price, size)`;
- `cash_after_fill(...)`.

All returned money is exact `int` fen. Do not use `float`.

- [x] **Step 3: Write failing immutable buy-posting tests**

Define a frozen `BuyPosting` and `PortfolioLedger`. Cover:

- cash decreases by notional plus fees;
- the position lot is appended once;
- duplicate event ID and lot ID are rejected;
- lot acquired date equals execution date;
- posting notional matches `unit_cost * original_size`;
- insufficient cash fails without mutating the original ledger;
- the approved 10,000 yuan / 0.95 / ETF 95 yuan case buys 100 units and leaves
  495 yuan after the 5 yuan minimum commission.

- [x] **Step 4: Implement buy posting**

Use the existing exact `PositionLot` and `FeeBreakdown` contracts. The state transition returns
a new frozen ledger and retains the previous value unchanged.

- [x] **Step 5: Run accounting tests**

```bash
uv run --no-sync pytest tests/unit/test_portfolio_accounting.py -q
```

### Task 4: Implement receivables and daily accounting identity

**Files:**

- Modify: `tests/unit/test_portfolio_accounting.py`
- Modify: `src/aquant/portfolio/accounting.py`
- Modify: `src/aquant/portfolio/__init__.py`

- [x] **Step 1: Write failing receivable tests**

Add frozen `CashReceivable` and transitions:

- registration increases outstanding receivables and leaves cash unchanged;
- exact actual cash date payment increases cash by the same amount and marks it paid;
- payment before the actual date does nothing;
- a receivable is never paid twice;
- duplicate corporate-action event IDs are rejected;
- source payable date and actual cash date are both retained.

The calculation of `actual_cash_date` belongs to Gate B; Gate A receives an already verified date
and enforces only `actual_cash_date >= source_payable_date`.

- [x] **Step 2: Write failing daily snapshot tests**

Add frozen `SymbolValuation` and `DailyAccountSnapshot`. `close_session` must:

- require exact, unique, sorted symbol valuations;
- sum position market values;
- sum only outstanding receivables;
- calculate `equity = cash + market value + receivables`;
- reject negative cash, size, market value, or receivable values;
- require session dates to be strictly increasing;
- reject a deliberately corrupted ledger via `verify_portfolio_ledger`.

- [x] **Step 3: Implement receivable and close transitions**

Every transition returns a new immutable ledger. `verify_portfolio_ledger` independently replays
cash events and recomputes every daily identity; it must not merely trust stored totals.

- [x] **Step 4: Run both Gate A test files**

```bash
uv run --no-sync pytest \
  tests/unit/test_portfolio_models.py \
  tests/unit/test_portfolio_accounting.py -q
```

Expected: PASS.

### Task 5: Gate A regression and evidence

**Files:**

- Modify: `docs/superpowers/plans/2026-07-28-v02-gate-a-accounting.md`
- Create: `outputs/Codex自审_v0.2_Gate_A.md`

- [x] **Step 1: Reinstall the current package non-editably**

```bash
uv sync --frozen --no-editable --reinstall-package a-share-quant
```

- [x] **Step 2: Run the full local gate**

```bash
uv lock --check
uv run --no-sync pytest -q
uv run --no-sync ruff check .
git diff --check
uv build
```

- [x] **Step 3: Re-run the frozen v0.1 verifier**

```bash
./scripts/verify_v01.sh
```

Expected:

- all old tests plus new Gate A tests pass;
- frozen 20 baselines, 30 candidates, risk report, experiment and 100 replay rows verify;
- v0.1 expected IDs are unchanged.

- [x] **Step 4: Perform a Codex adversarial self-review**

Check at minimum:

- bool-as-int bypasses;
- mutable verified input bypasses;
- duplicate posting/payment;
- same-day ledger mismatches;
- off-by-one allocation;
- fee paid inside target notional by mistake;
- float leakage;
- accidental edits to any v0.1 implementation-digest file.

Record exact evidence and unresolved issues in `outputs/Codex自审_v0.2_Gate_A.md`.

- [ ] **Step 5: Ask Claude Code (DeepSeek API) for one bounded code review**

Provide only the Gate A diff, design sections 4, 5, and 7, and the self-review report. Require:

```text
P0/P1/P2 findings with exact file and line;
contract bypass attempts;
integer-fen and cash-conservation review;
explicit distinction between static review and executed test evidence.
```

2026-07-28 执行记录：已进行两次有边界调用，但适配层分别返回
`error_during_execution` 与 `error_max_budget_usd`，没有可用审查正文；累计报告成本
0.721752 美元。证据见 `outputs/Claude代码复核尝试_v0.2_Gate_A.md`。本步骤保持未完成，
不进行第三次无产出调用。

- [ ] **Step 6: Fix valid findings with new failing tests**

Do not edit production code first. Every accepted finding gets a reproducing test, the minimum fix,
focused tests, and the full local gate again.

- [x] **Step 7: Commit Gate A**

```bash
git add src/aquant/portfolio tests/unit/test_portfolio_*.py \
  docs/superpowers/plans/2026-07-28-v02-gate-a-accounting.md \
  outputs/Codex自审_v0.2_Gate_A.md
git commit -m "feat: add shared cash portfolio accounting core"
```

Do not push or create `v0.2-research` here. Gates B through E and the final release gate remain open.
