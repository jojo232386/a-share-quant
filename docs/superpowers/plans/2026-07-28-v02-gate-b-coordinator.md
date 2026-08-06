# v0.2 Gate B Portfolio Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic multi-symbol official-session loop that turns the
already-verified Gate A inputs into audited buy attempts, shared-cash fills, T+1
lots, dividend receivables, and same-session daily account identities.

**Architecture:** Keep every v0.1 implementation file byte-identical. Add one
v0.2-only public availability gate and one portfolio coordinator under
`aquant.portfolio`; the coordinator reuses verified calendars, fee policies,
`evaluate_order()`, `create_buy_lot()`, and the Gate A ledger. All decisions are
immutable records, sorted by symbol, and derived only from data available at the
current official session.

**Tech Stack:** Python 3.11, pandas, immutable dataclasses, `Decimal`, pytest,
Ruff, existing `aquant.data`, `aquant.rules`, and `aquant.portfolio.accounting`.

---

## File map

- Create `src/aquant/portfolio/availability.py`: the only v0.2 bar-availability
  gate; validates that an attempt session is exactly the next official session.
- Create `src/aquant/portfolio/coordinator.py`: immutable target/attempt/audit
  records, dividend cash-date helper, preflight verification, and the official
  session loop.
- Modify `src/aquant/portfolio/__init__.py`: export only the new public v0.2
  contracts and runner.
- Create `tests/unit/test_portfolio_availability.py`: exact-session, no-bar,
  forged-calendar, and invalid-set tests.
- Create `tests/unit/test_portfolio_coordinator.py`: 2/3/10 member allocation,
  five retries, price-limit rejection, cash-aware fills, T+1 lots, dividend
  registration/payment, no-bar marking, deterministic order, and fail-closed
  preflight tests.
- Modify `docs/superpowers/specs/2026-07-27-shared-cash-portfolio-design.md`:
  preserve the v0.1 freeze while naming the v0.2 availability boundary and
  requiring one post-end calendar session.

## Task 1: Public v0.2 availability gate

**Files:**
- Create: `tests/unit/test_portfolio_availability.py`
- Create: `src/aquant/portfolio/availability.py`
- Modify: `src/aquant/portfolio/__init__.py`

- [ ] **Step 1: Write the exact-session tests**

```python
def test_exact_next_official_session_with_bar_is_available(calendar):
    decision = check_bar_availability(
        intent_session=date(2026, 7, 17),
        execution_session=date(2026, 7, 20),
        calendar=calendar,
        available_bar_dates=frozenset({date(2026, 7, 20)}),
    )
    assert decision.status is AvailabilityStatus.AVAILABLE
    assert decision.source_rule_reason is None


def test_exact_next_official_session_without_bar_is_conservatively_unavailable(calendar):
    decision = check_bar_availability(
        intent_session=date(2026, 7, 17),
        execution_session=date(2026, 7, 20),
        calendar=calendar,
        available_bar_dates=frozenset(),
    )
    assert decision.status is AvailabilityStatus.NO_BAR_UNAVAILABLE
    assert decision.source_rule_reason is RejectionReason.SUSPENDED_NO_BAR
```

Also assert rejection of a skipped official session, non-`frozenset` dates,
non-date members, and a forged or mutated calendar.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
pytest -q tests/unit/test_portfolio_availability.py
```

Expected: collection fails because `aquant.portfolio.availability` does not
exist.

- [ ] **Step 3: Implement the minimal public gate**

```python
class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    NO_BAR_UNAVAILABLE = "no_bar_unavailable"


@dataclass(frozen=True)
class AvailabilityDecision:
    status: AvailabilityStatus
    source_rule_reason: RejectionReason | None


def check_bar_availability(
    *,
    intent_session: date,
    execution_session: date,
    calendar: VerifiedTradingCalendar,
    available_bar_dates: frozenset[date],
) -> AvailabilityDecision:
    verify_trading_calendar(calendar)
    if (
        type(intent_session) is not date
        or type(execution_session) is not date
        or type(available_bar_dates) is not frozenset
        or any(type(item) is not date for item in available_bar_dates)
        or calendar.next_session(intent_session) != execution_session
    ):
        raise PortfolioError(
            "invalid_attempt_session",
            "attempt must target the exact next official session",
        )
    if execution_session not in available_bar_dates:
        return AvailabilityDecision(
            AvailabilityStatus.NO_BAR_UNAVAILABLE,
            RejectionReason.SUSPENDED_NO_BAR,
        )
    return AvailabilityDecision(AvailabilityStatus.AVAILABLE, None)
```

Catch `CalendarError` and expose one stable `PortfolioError` without including
paths or raw input values.

- [ ] **Step 4: Run the tests and verify GREEN**

Run:

```bash
pytest -q tests/unit/test_portfolio_availability.py
```

Expected: all availability tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/portfolio/availability.py src/aquant/portfolio/__init__.py tests/unit/test_portfolio_availability.py
git commit -m "feat: add v02 portfolio availability gate"
```

## Task 2: Immutable target and attempt state

**Files:**
- Create: `tests/unit/test_portfolio_coordinator.py`
- Create: `src/aquant/portfolio/coordinator.py`
- Modify: `src/aquant/portfolio/__init__.py`

- [ ] **Step 1: Write target-state and cash-date tests**

```python
def test_actual_cash_date_keeps_session_or_moves_to_next_official_session(calendar):
    assert actual_cash_date(calendar, date(2026, 7, 17)) == date(2026, 7, 17)
    assert actual_cash_date(calendar, date(2026, 7, 18)) == date(2026, 7, 20)


def test_target_attempt_ids_and_order_are_deterministic(portfolio_fixture):
    result = run_portfolio_backtest(**portfolio_fixture(symbols=("600001", "600000")))
    assert tuple(item.symbol for item in result.targets) == ("600000", "600001")
    assert tuple(item.attempt_number for item in result.attempts) == (1, 1)
    assert len({item.attempt_id for item in result.attempts}) == 2
```

Test exact dataclass types, positive target notionals, strictly increasing
attempt numbers, stable identifiers, and incompatible status-field
combinations.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
pytest -q tests/unit/test_portfolio_coordinator.py -k "cash_date or deterministic"
```

Expected: import failure because the coordinator contracts do not exist.

- [ ] **Step 3: Implement exact immutable records and helpers**

```python
class TargetStatus(StrEnum):
    PENDING = "pending"
    FILLED = "filled"
    EXPIRED_UNFILLED = "expired_unfilled"


class AttemptStatus(StrEnum):
    FILLED = "filled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class EntryTarget:
    target_id: str
    symbol: str
    signal_date: date
    target_notional_fen: int
    attempts_used: int
    status: TargetStatus
    fill_event_id: str | None


@dataclass(frozen=True)
class EntryAttempt:
    attempt_id: str
    target_id: str
    symbol: str
    original_signal_date: date
    intent_session: date
    execution_session: date
    attempt_number: int
    initial_candidate_size: int
    requested_size: int
    availability_status: AvailabilityStatus
    status: AttemptStatus
    rejection_reason: RejectionReason | None
    fill_event_id: str | None


def actual_cash_date(
    calendar: VerifiedTradingCalendar,
    source_payable_date: date,
) -> date:
    verify_trading_calendar(calendar)
    if type(source_payable_date) is not date:
        raise PortfolioError("invalid_payable_date", "payable date is invalid")
    result = (
        source_payable_date
        if calendar.contains(source_payable_date)
        else calendar.next_session(source_payable_date)
    )
    if result is None:
        raise PortfolioError(
            "missing_calendar_coverage",
            "calendar does not cover the dividend cash date",
        )
    return result
```

Use `target:{symbol}:{signal_date}` and
`attempt:{symbol}:{signal_date}:{attempt_number}` as deterministic identifiers;
all validators require exact primitive and enum types.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
pytest -q tests/unit/test_portfolio_coordinator.py -k "cash_date or deterministic"
```

Expected: the helper and contract tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/portfolio/coordinator.py src/aquant/portfolio/__init__.py tests/unit/test_portfolio_coordinator.py
git commit -m "feat: add deterministic portfolio target state"
```

## Task 3: Official-session attempts, shared cash, and T+1

**Files:**
- Modify: `tests/unit/test_portfolio_coordinator.py`
- Modify: `src/aquant/portfolio/coordinator.py`

- [ ] **Step 1: Write failing execution tests**

Cover these exact behaviors:

```python
def test_five_no_bar_sessions_create_five_attempts_then_expire(fixture):
    result = run_portfolio_backtest(**fixture(no_bars_after_signal=True, sessions=7))
    assert [item.attempt_number for item in result.attempts] == [1, 2, 3, 4, 5]
    assert all(
        item.rejection_reason is RejectionReason.SUSPENDED_NO_BAR
        for item in result.attempts
    )
    assert result.targets[0].status is TargetStatus.EXPIRED_UNFILLED


def test_price_limit_rejection_retries_on_next_official_session(fixture):
    result = run_portfolio_backtest(**fixture(opens=("11.00", "10.50")))
    assert result.attempts[0].rejection_reason is RejectionReason.PRICE_LIMIT_OPEN
    assert result.attempts[1].status is AttemptStatus.FILLED


def test_fill_uses_fixed_target_notional_and_shared_cash(fixture):
    result = run_portfolio_backtest(**fixture(symbols=("600001", "600000")))
    assert tuple(event.symbol for event in result.ledger.cash_events) == (
        "600000",
        "600001",
    )
    assert result.ledger.cash_fen >= 0
    assert all(lot.original_size % 100 == 0 for lot in result.ledger.lots)
    assert all(lot.available_date > lot.acquired_date for lot in result.ledger.lots)
```

Add 2-, 3-, and 10-member tests, weekend/holiday exclusion, successful-fill
termination, no duplicate symbol/session attempt, insufficient-one-lot
rejection, fee-caused whole-lot reduction, and tuple-order invariance.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
pytest -q tests/unit/test_portfolio_coordinator.py -k "attempt or fill or member or limit"
```

Expected: failures identify the missing session loop.

- [ ] **Step 3: Implement the minimal execution loop**

The production loop must:

1. require exact `PortfolioConfig`, verified universe, verified inputs, verified
   fee policy, and verified calendar;
2. require `signal_date` and `end_date` in the calendar plus one post-end
   session;
3. require a signal-date bar for every symbol;
4. iterate only the calendar dates in `(signal_date, end_date]`;
5. process pending targets by six-digit symbol;
6. call `check_bar_availability()` before accessing the current bar;
7. size from the immutable per-symbol target notional:

```python
target_yuan = Decimal(target.target_notional_fen) / Decimal(100)
units = int((target_yuan / execution_open).to_integral_value(rounding=ROUND_FLOOR))
initial_candidate = units // 100 * 100
```

8. call `evaluate_order()` with the preceding official session as
   `OrderIntent.signal_date`;
9. on `INSUFFICIENT_CASH`, reduce only the current symbol by one 100-unit lot
   and retry; never increase another symbol;
10. on allowance, call `create_buy_lot()` and `post_buy()` with deterministic
    lot/fill identifiers;
11. consume exactly one attempt per official session, stop after fill, and mark
    the target expired immediately after its final rejected attempt.

Missing or invalid fee/calendar coverage is an overall `PortfolioError`, not an
ordinary attempt rejection.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
pytest -q tests/unit/test_portfolio_availability.py tests/unit/test_portfolio_coordinator.py
```

Expected: attempt, shared-cash, price-limit, fee, and T+1 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/portfolio/coordinator.py tests/unit/test_portfolio_coordinator.py
git commit -m "feat: execute shared cash portfolio entries"
```

## Task 4: Dividend timing, no-bar marks, and daily identity

**Files:**
- Modify: `tests/unit/test_portfolio_coordinator.py`
- Modify: `src/aquant/portfolio/coordinator.py`

- [ ] **Step 1: Write failing dividend and valuation tests**

```python
def test_nontrading_payable_date_pays_on_next_session_without_symbol_bar(fixture):
    result = run_portfolio_backtest(
        **fixture(
            dividend_ex_date=date(2026, 7, 17),
            dividend_payable_date=date(2026, 7, 18),
            missing_bar_dates={date(2026, 7, 20)},
        )
    )
    receivable = result.ledger.receivables[0]
    assert receivable.actual_cash_date == date(2026, 7, 20)
    assert receivable.paid_date == date(2026, 7, 20)


def test_no_bar_ex_date_adjusts_mark_and_receivable_without_double_count(fixture):
    result = run_portfolio_backtest(**fixture(no_bar_on_ex_date=True))
    snapshot = next(
        item for item in result.ledger.daily_snapshots
        if item.session == date(2026, 7, 17)
    )
    assert snapshot.equity_fen == (
        snapshot.cash_fen
        + snapshot.position_market_value_fen
        + snapshot.receivable_fen
    )
```

Also test same-session cash payment before close, payment idempotence,
unsupported non-cash actions fail before output, a non-positive ex-dividend
mark fails closed, and every daily valuation date equals its snapshot date.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
pytest -q tests/unit/test_portfolio_coordinator.py -k "dividend or payable or mark or identity"
```

Expected: failures identify missing receivable and close-session behavior.

- [ ] **Step 3: Implement session ordering and audit records**

For each official session:

1. find verified cash events whose `ex_date` equals the session;
2. reject non-cash action fields before any ledger mutation;
3. compute entitlement from lots with `acquired_date < ex_date`;
4. register one `CashReceivable` per entitled event using integer-fen
   `ROUND_HALF_UP`;
5. subtract the per-unit cash dividend from the last trusted mark even if the
   symbol has no bar;
6. call `pay_receivables()` before attempts and before daily close;
7. replace the mark with raw close when a verified bar exists, otherwise carry
   the adjusted trusted mark and retain an immutable availability audit record;
8. build sorted `SymbolValuation` records from current lots and call
   `close_session()`.

The returned immutable result contains config, allocation, sorted targets,
attempts, dividend audit records, availability audit records, and the verified
Gate A ledger.

- [ ] **Step 4: Run focused and full portfolio tests**

Run:

```bash
pytest -q tests/unit/test_portfolio_models.py tests/unit/test_portfolio_accounting.py tests/unit/test_portfolio_availability.py tests/unit/test_portfolio_coordinator.py
```

Expected: all Gate A and Gate B tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/aquant/portfolio/coordinator.py tests/unit/test_portfolio_coordinator.py
git commit -m "feat: close portfolio sessions with dividends"
```

## Task 5: Gate B verification and review evidence

**Files:**
- Create: `outputs/Codex自审_v0.2_Gate_B.md`
- Modify: `outputs/Claude代码复核尝试_v0.2_Gate_A.md`

- [ ] **Step 1: Run static and full tests**

Run:

```bash
ruff check src tests
pytest -q
```

Expected: Ruff passes; the complete suite passes with only the existing
documented skip.

- [ ] **Step 2: Re-run frozen v0.1 acceptance**

Run the repository's frozen replay verifier and assert all expected run IDs and
experiment IDs remain unchanged. Any identity drift is a Gate B failure.

- [ ] **Step 3: Inspect the exact diff**

Confirm that no file under these frozen paths changed:

```text
src/aquant/backtest/**
src/aquant/data/**
src/aquant/rules/**
src/aquant/universe.py
```

Review the strongest counterexamples: five no-bar sessions, non-trading
payable date with no symbol bar, symbol input permutation, cash just below one
lot plus fees, and final-run-date T+1 coverage.

- [ ] **Step 4: Record review truthfully**

Write exact commands, counts, commit IDs, accepted limitations, and unresolved
risks. A Claude Code result counts only if it contains an inspectable review
body; an API response, model usage line, timeout, or green local test does not
count as independent review.

- [ ] **Step 5: Commit the Gate B evidence**

```bash
git add outputs/Codex自审_v0.2_Gate_B.md outputs/Claude代码复核尝试_v0.2_Gate_A.md
git commit -m "docs: record v02 gate b verification"
```

## Plan self-review

- Spec coverage: Gate B's 2/3/10-member, official-session retry, no-bar,
  price-limit, fee, T+1, dividend, and daily-accounting requirements each map
  to an explicit failing test and implementation step.
- Freeze boundary: the plan creates only `aquant.portfolio` production files
  and does not modify any v0.1 implementation-digest file.
- Type consistency: `AvailabilityStatus`, `EntryTarget`, `EntryAttempt`,
  `TargetStatus`, `AttemptStatus`, `actual_cash_date()`, and
  `run_portfolio_backtest()` have one spelling throughout.
- Determinism: IDs, session iteration, targets, attempts, fees, and valuations
  all use explicit stable order; wall-clock time, PID, object address, random
  iteration, network access, and output export are absent from Gate B.
- Scope control: run identity and artifact export remain Gate C; economic
  equivalence remains Gate D; frozen ten-symbol execution remains Gate E.
