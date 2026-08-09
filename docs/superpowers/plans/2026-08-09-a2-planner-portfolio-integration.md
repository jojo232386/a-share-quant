# A2 Planner → Shared-Cash Rolling Portfolio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans, and use superpowers:test-driven-development for every production change. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a deterministic Planner v1 to shared-cash rolling portfolio seam with isolated SELL accounting, legal residual retry evidence, and no change to legacy v0.2 or Gate E.

**Architecture:** Add a standalone `aquant.rolling` package. `accounting.py` owns the sibling rolling ledger and independent BUY/SELL replay; `orchestration.py` consumes one effective `PlannedTargets` and one T/T+1 execution input set, processes symbol-sorted SELLs before BUYs against one cash balance, and returns desired-versus-realized evidence. Legacy portfolio types and artifact paths remain untouched.

**Tech Stack:** Python 3.11, immutable dataclasses, exact `Decimal`, existing verified calendar/fee/rule objects, pytest, Ruff, uv build, and two-`PYTHONHASHSEED` determinism probes.

---

## 1. Frozen baseline and boundary

- Baseline and implementation HEAD: `772c5d08141b25ebe8a32e24e09f5c4f3bd58e88`
- Branch: `feat/a2-planner-portfolio-integration`
- Worktree: `/Users/ASUS/.local/share/a-share-quant/a2-planner-portfolio-integration`
- Design: `docs/superpowers/specs/2026-08-09-a2-planner-portfolio-integration-design.md`
- Baseline full suite: `1284 passed, 1 skipped, 2 warnings`

Allowed production/test files:

```text
src/aquant/rolling/__init__.py
src/aquant/rolling/accounting.py
src/aquant/rolling/orchestration.py
tests/unit/test_rolling_accounting.py
tests/unit/test_rolling_orchestration.py
tests/rolling_determinism_probe.py        # only if subprocess probe needs a script
docs/engineering/risk_governance.md       # evidence-only final commit
```

Frozen against the baseline:

```text
src/aquant/planner/
src/aquant/research/signals.py
src/aquant/portfolio/
src/aquant/rules/
src/aquant/gate_e/
tests/contracts/import_contract.json
release/
configs/
uv.lock
```

If implementation requires a frozen-file edit, stop for scope review.

## 2. Public rolling contract

`aquant.rolling.accounting` provides:

```python
ROLLING_ACCOUNTING_SCHEMA_VERSION = "1.0.0"

@dataclass(frozen=True)
class LotConsumption:
    lot_id: str
    size: int

@dataclass(frozen=True)
class SellPosting:
    event_id: str
    execution_date: date
    symbol: str
    size: int
    unit_price: Decimal
    fees: FeeBreakdown

@dataclass(frozen=True)
class SellFillEvent:
    event_id: str
    session: date
    symbol: str
    size: int
    unit_price: Decimal
    notional_fen: int
    commission_fen: int
    stamp_duty_fen: int
    transfer_fee_fen: int
    cash_before_fen: int
    cash_after_fen: int
    consumptions: tuple[LotConsumption, ...]

@dataclass(frozen=True)
class RollingPortfolioLedger:
    initial_cash_fen: int
    cash_fen: int
    lots: tuple[PositionLot, ...] = ()
    cash_events: tuple[CashLedgerEvent | SellFillEvent, ...] = ()
    receivables: tuple[CashReceivable, ...] = ()
    daily_snapshots: tuple[DailyAccountSnapshot, ...] = ()
```

Functions: `create_rolling_ledger`, `promote_portfolio_ledger`, `post_rolling_buy`,
`post_rolling_sell`, `close_rolling_session`, and `verify_rolling_ledger`.

`aquant.rolling.orchestration` provides exact immutable `RollingConfig`,
`RollingExecutionInput`, `RebalanceAttempt`, `TargetRealization`,
`RollingRebalanceResult`, and keyword-only `rebalance_to_plan()`.

No rolling type is added to `aquant.portfolio.__all__` or the frozen import contract.

## Task 1: Freeze and implement rolling-only accounting

**Files:**
- Create: `src/aquant/rolling/__init__.py`
- Create: `src/aquant/rolling/accounting.py`
- Create: `tests/unit/test_rolling_accounting.py`

- [ ] **Step 1: Write the first failing public-contract tests**

Add tests named:

```text
test_create_rolling_ledger_delegates_to_verified_pristine_legacy_ledger
test_promote_portfolio_ledger_copies_verified_state_without_aliasing_schema
test_rolling_buy_matches_one_event_legacy_post_buy
test_legacy_dataclass_fields_and_sell_rejection_remain_frozen
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_rolling_accounting.py
```

Expected: collection fails because `aquant.rolling` does not exist. A pass or unrelated import error is not an acceptable RED.

- [ ] **Step 3: Add the minimal package and BUY seam**

Create the exact dataclasses and constructors from §2. `create_rolling_ledger()` must call
`create_portfolio_ledger()` and `verify_portfolio_ledger()`; promotion only accepts an exact verified
`PortfolioLedger`. `post_rolling_buy()` must validate one event through existing `post_buy()` using current
rolling cash, then append the exact returned lot and `CashLedgerEvent`.

- [ ] **Step 4: Verify the first GREEN**

Run the Task 1 test file. Expected: the four tests pass and no legacy file changed.

- [ ] **Step 5: Add one failing SELL/FIFO test at a time**

Add and individually run these tests before implementing their behavior:

```text
test_sell_cash_uses_notional_and_all_fee_components
test_sell_consumes_only_same_symbol_fifo_and_retains_zero_lots
test_sell_preserves_locked_lots_and_rejects_insufficient_sellable_size
test_buy_after_sell_uses_the_updated_shared_cash
test_failed_sell_is_atomic
test_rolling_verifier_rejects_tampered_consumption_cash_fee_and_order
```

Each RED must fail for the missing SELL behavior, not for malformed fixtures.

- [ ] **Step 6: Implement minimal SELL posting and replay**

Filter lots by symbol before calling `sellable_size`, `validate_sell_size`, and `consume_fifo`. Record exact
ordered consumptions, merge updated lots back into global tuple positions, calculate proceeds through
`notional_fen` and `cash_after_fill(OrderSide.SELL)`, and independently replay all events from initial cash.
Keep legacy BUY fill-to-original-lot bijection and compare replayed terminal lots/cash exactly.

- [ ] **Step 7: Add snapshot/receivable replay tests RED, then GREEN**

Tests:

```text
test_close_rolling_session_recomputes_cash_market_receivable_and_equity
test_historical_snapshot_remains_valid_after_a_later_sell
test_pristine_conditions_cannot_be_inferred_from_missing_snapshots_only
```

`close_rolling_session()` accepts sorted exact `SymbolValuation` values, reconstructs positions at the snapshot
session from original lots and dated SELL consumptions, and independently validates outstanding receivables.

- [ ] **Step 8: Run accounting and frozen legacy regression**

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_rolling_accounting.py \
  tests/unit/test_portfolio_accounting.py \
  tests/unit/test_portfolio_identity.py \
  tests/unit/test_portfolio_export.py \
  tests/unit/test_portfolio_verify.py \
  tests/contracts/test_import_contract.py
```

Expected: all pass; exact legacy dataclass/schema tests remain unchanged.

- [ ] **Step 9: Inspect and commit accounting only**

```bash
git diff --check
git diff --name-only 772c5d08141b25ebe8a32e24e09f5c4f3bd58e88
git add src/aquant/rolling/__init__.py src/aquant/rolling/accounting.py tests/unit/test_rolling_accounting.py
git commit -m "feat: add isolated rolling sell accounting"
```

## Task 2: Implement one-session Planner/shared-cash orchestration

**Files:**
- Create: `src/aquant/rolling/orchestration.py`
- Modify: `src/aquant/rolling/__init__.py`
- Create: `tests/unit/test_rolling_orchestration.py`

- [ ] **Step 1: Write and verify RED for staging invariants**

Tests:

```text
test_rebalance_requires_exact_planned_targets_and_exact_next_session
test_rebalance_uses_T_close_equity_including_receivable
test_pristine_fallback_requires_every_D5_condition_and_legacy_verification
test_target_notional_remains_exact_until_one_share_floor
test_held_symbol_missing_from_effective_plan_fails_atomically
test_total_target_notional_uses_same_equity_and_respects_max_gross
test_calendar_end_without_next_session_fails_not_residuals
```

Run the test file and confirm failure because orchestration types/functions are absent.

- [ ] **Step 2: Implement validation, reconciliation, sizing and evidence types**

`RollingExecutionInput` binds symbol, instrument kind, T, T+1, and either two exact prices or two `None`
values for no bar. `rebalance_to_plan()` consumes only exact `PlannedTargets`, uses the latest T snapshot or
strict pristine fallback, rejects missing held-symbol keys, computes exact Decimal target notionals, performs
one share-space floor, and records target states without copying Planner history.

- [ ] **Step 3: Verify staging GREEN**

Run only the staging tests, then the entire orchestration file. Expected: staging tests pass; no order behavior
is claimed yet.

- [ ] **Step 4: Add execution-order and cash tests RED**

Tests:

```text
test_all_sells_run_before_all_buys_and_each_side_is_symbol_sorted
test_sell_proceeds_are_available_to_later_buy_in_one_shared_cash_account
test_buy_affordability_decrements_exactly_100_shares_including_fees
test_target_up_down_and_already_aligned_use_realized_share_delta
test_partial_legal_sell_keeps_the_unfilled_residual
```

- [ ] **Step 5: Implement execution through existing rules**

Call `check_bar_availability()` before price/fee work. For available bars call `evaluate_order()` with
`signal_date=plan.as_of`; post authorized SELL/BUY through rolling accounting. Process SELL symbols first,
then BUY symbols. BUY retries only `INSUFFICIENT_CASH` by subtracting 100; other contract errors fail closed.

- [ ] **Step 6: Add refrozen residual tests RED, then GREEN**

Tests:

```text
test_explicit_zero_no_bar_keeps_desired_realized_and_residual_visible
test_explicit_zero_price_limit_keeps_residual_visible
test_later_effective_zero_plan_and_legal_session_recomputes_and_converges
test_residual_does_not_create_a_shadow_target_or_mark_failure_achieved
```

Use legitimate no-bar or price-limit inputs. Do not construct a lot with forged post-T+1 availability. The
later retry must use a new plan date and its own T snapshot.

- [ ] **Step 7: Reuse the existing T+1 primitive test**

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_a_share_rules.py::test_t_plus_one_uses_calendar_and_does_not_wait_for_symbol_bar
```

Expected: pass without adding another T+1 implementation or modifying rules.

- [ ] **Step 8: Add and verify determinism**

Run identical cases with reversed execution-input order and confirm structural equality. If a subprocess is
needed, create `tests/rolling_determinism_probe.py`, then run it with:

```bash
PYTHONHASHSEED=11 .venv/bin/python tests/rolling_determinism_probe.py
PYTHONHASHSEED=97 .venv/bin/python tests/rolling_determinism_probe.py
```

Expected: byte-identical stdout and terminal ledger/attempt ordering.

- [ ] **Step 9: Run focused integration and regressions**

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_rolling_accounting.py \
  tests/unit/test_rolling_orchestration.py \
  tests/unit/test_planner.py \
  tests/unit/test_planner_assembly.py \
  tests/unit/test_a_share_rules.py \
  tests/unit/test_portfolio_accounting.py \
  tests/unit/test_portfolio_availability.py \
  tests/unit/test_portfolio_coordinator.py
```

- [ ] **Step 10: Inspect and commit orchestration only**

```bash
git diff --check
git add src/aquant/rolling/__init__.py src/aquant/rolling/orchestration.py \
  tests/unit/test_rolling_orchestration.py tests/rolling_determinism_probe.py
git commit -m "feat: integrate planner targets with rolling shared cash"
```

Omit the probe path from `git add` if the probe was unnecessary.

## Task 3: R-007 evidence and final verification

**Files:**
- Modify only if evidence is complete: `docs/engineering/risk_governance.md`

- [ ] **Step 1: Map named tests to each R-007 acceptance item**

Record effective carry-forward exposure, shared cash, gross, deterministic multi-symbol state, explicit zero
versus missing, and desired-versus-realized residual retry. Do not close R-007 for unrun or inferred evidence.

- [ ] **Step 2: Run CI-equivalent checks**

```bash
uv lock --check
./scripts/check_committed_whitespace.sh 772c5d08141b25ebe8a32e24e09f5c4f3bd58e88 HEAD
tests/scripts/test_check_committed_whitespace.sh
.venv/bin/python -m pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv build
```

Every command must exit 0 before a PASS claim.

- [ ] **Step 3: Wheel/install smoke in a temporary Python 3.11 environment**

Install the newly built wheel without `PYTHONPATH`, then import `aquant`, `aquant.planner`,
`aquant.portfolio`, and `aquant.rolling`. Remove only the temporary directory created for this probe.

- [ ] **Step 4: Update and commit R-007 only when evidence is complete**

```bash
git add docs/engineering/risk_governance.md
git commit -m "docs: record A2 shared-cash evidence"
```

If any acceptance item is missing, leave the register open and do not create this commit.

- [ ] **Step 5: Protected-scope and final diff audit**

```bash
git diff --exit-code 772c5d08141b25ebe8a32e24e09f5c4f3bd58e88 -- \
  src/aquant/planner src/aquant/research/signals.py src/aquant/portfolio \
  src/aquant/rules src/aquant/gate_e release configs uv.lock
git status --short
git log --oneline 772c5d08141b25ebe8a32e24e09f5c4f3bd58e88..HEAD
```

- [ ] **Step 6: Independent spec review, then code-quality review**

Bind review to the exact baseline/head range. Fix every P0/P1 and every important spec/quality issue, rerun
affected tests, then rerun final gates. Stop locally after `PASS_READY_FOR_INDEPENDENT_REVIEW`; do not push,
open a PR, merge, release, enter Gate F, or start paper/live trading.
