# Planner v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in the existing `target-weights-planner-v1` worktree. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the frozen, deterministic pure Planner v1 core and its planner-local Signal assembly boundary without modifying A1, v0.2 portfolio, Gate E, replay, audit, or execution code.

**Architecture:** Add a standalone `aquant.planner` package. `core.py` owns immutable planned-state value objects, stable planner error codes, fixed-context validation, and key-preserving override/carry-forward; `assembly.py` owns the single `SignalSpec` truth source, explicit per-signal builders, registry parity, and cardinality checks. State serialization remains a frozen future adapter contract because the approved design explicitly says this implementation round does not implement persistence or serialization I/O.

**Tech Stack:** Python 3.11, standard-library `dataclasses`, `Decimal`, `MappingProxyType`, `StrEnum`, pytest 9, Ruff, and the frozen A1 signal classes.

---

## 1. Frozen baseline and implementation boundary

- Branch: `feat/target-weights-planner-v1`
- Design Freeze commit: `c5e68387d7d1d4caa77fd29ab5445b91ce628d05`
- Approved design: `docs/superpowers/specs/2026-08-09-planner-v1-design.md`
- Design review evidence supplied at handoff: `WORK BUDDY ARCHITECTURE REVIEW PASS`, `P0=0 / P1=0 / P2=0 / P3=0`
- Live starting remote `main`: `43759c64aa247bcbb9b48b0bdcbc676739d7a81f`
- Starting worktree and remote feature HEAD: `c5e68387d7d1d4caa77fd29ab5445b91ce628d05`

This plan applies the frozen design; it does not reopen architecture, change weight meaning, or promote Planner v1 into execution.

### Allowed file map

- Create `src/aquant/planner/core.py`: public core types, stable errors, immutable state construction, validation, and `plan_targets()`.
- Create `src/aquant/planner/assembly.py`: `SignalCardinality`, `SignalSpec`, explicit builders, `SIGNAL_SPECS`, parity enforcement, and `build_signal()`.
- Create `src/aquant/planner/__init__.py`: explicit public Planner v1 exports only.
- Create `tests/unit/test_planner.py`: core contract, negative/fail-closed, immutability, merge, universe, limits, and Decimal-context tests.
- Create `tests/unit/test_planner_assembly.py`: registry parity, builder, config, and cardinality tests.
- Create this implementation plan.

No other repository file is allowlisted. Any need to modify another file stops implementation for scope review.

### Frozen files and components that must remain byte-identical to `origin/main`

- `src/aquant/research/signals.py`, including `Signal`, `SIGNAL_REGISTRY`, `SmaSignal.active_weight`, `TopKMomentumSignal`, and `validate_signal_output()`.
- All `src/aquant/portfolio/`, especially `coordinator.py` and `contracts.py`; `PORTFOLIO_SCHEMA_VERSION` stays `"0.2.0"`.
- All Gate E, release/replay, trust, audit, manifest, configuration, evidence, and frozen release assets.
- Existing A1 tests and existing portfolio/backtest tests.

### Explicitly deferred

- State JSON/envelope loader, dumper, filesystem/database I/O, epoch persistence, locks, hashes, backups, and recovery.
- NAV valuation, price selection, weight-to-fen/lots, execution feasibility, SELL, rebalance, T+1/T+0 execution, broker/paper wiring, and reconciliation.
- Any normalization, equal-weight rewrite, or conversion of a positive Decimal weight into an ACTIVE marker.

## 2. Public API, data types, and stable errors

`aquant.planner` exports exactly these implementation-round public names:

```python
PLANNER_SCHEMA_VERSION = "1.0.0"

class PlannerError(ValueError):
    code: str

class NoPreviousStateReason(StrEnum):
    FIRST_PERIOD = "first_period"
    EXPLICIT_RESET = "explicit_reset"

@dataclass(frozen=True)
class NoPreviousState:
    reason: NoPreviousStateReason

@dataclass(frozen=True, init=False)
class PreviousTargets:
    as_of: date
    targets: Mapping[str, Decimal]

@dataclass(frozen=True, init=False)
class PlannedTargets:
    as_of: date
    targets: Mapping[str, Decimal]

@dataclass(frozen=True)
class PlannerLimits:
    max_single_weight: Decimal = Decimal("1")
    max_gross: Decimal = Decimal("1")
    min_cash_ratio: Decimal = Decimal("0")

class SignalCardinality(StrEnum):
    SINGLE_SYMBOL = "single_symbol"
    MULTI_SYMBOL = "multi_symbol"

@dataclass(frozen=True)
class SignalSpec:
    name: str
    builder: Callable[[Mapping[str, object]], Signal]
    cardinality: SignalCardinality

SIGNAL_SPECS: Mapping[str, SignalSpec]
```

The two keyword-only function signatures are:

```text
plan_targets(*, as_of: date, signal_output: Mapping[str, Decimal], previous: PreviousTargets | NoPreviousState, eligible_symbols: frozenset[str], limits: PlannerLimits) -> PlannedTargets
build_signal(*, name: str, config: Mapping[str, object], eligible_symbols: frozenset[str]) -> Signal
```

Stable core/assembly codes implemented in this round:

- `invalid_as_of`
- `invalid_previous_state`
- `non_ascending_previous_state`
- `invalid_eligible_symbols`
- `universe_mismatch`
- `invalid_output_type`
- `invalid_symbol`
- `non_decimal_weight`
- `non_finite_weight`
- `negative_weight`
- `weight_above_one`
- `hard_gross_ceiling_exceeded`
- `max_single_weight_exceeded`
- `max_gross_exceeded`
- `min_cash_ratio_violated`
- `invalid_limits`
- `unknown_signal_spec`
- `invalid_signal_config`
- `unsupported_cardinality`
- `signal_spec_registry_mismatch`
- `planner_invariant_violation`

Serialization-only error codes from design §11.2 are not implemented because no serialization adapter is implemented in this round.

## 3. Validation and deterministic representation

- Exact dates use `type(value) is date`; `datetime` and pandas timestamps fail closed.
- Exact weights use `type(value) is Decimal`; no bool/int/float coercion.
- Weight validation order is type, finite, nonnegative, at-most-one.
- Gross uses a new local `decimal.Context(prec=60)` and starts at `Decimal("0")`; caller global precision/rounding cannot affect it.
- `PreviousTargets` and `PlannedTargets` validate and sort a defensive `dict` copy, then expose a `MappingProxyType` over that private copy. Caller mapping mutation and public item assignment/deletion cannot change state.
- State equality compares normalized sorted content, not caller insertion order or proxy identity.
- `plan_targets()` validates current and previous layers before universe checks, performs sorted key-preserving merge, then validates the complete effective layer in this fixed order: structure, hard gross, max single, max gross, min cash.
- Explicit zero remains a present key; omission carries a previous value, including zero; ordinary planning never deletes a historical key.
- `eligible_symbols` must be an exact non-empty `frozenset[str]` of non-empty strings.

## Task 1: Freeze the core contract in failing tests

**Files:**
- Create: `tests/unit/test_planner.py`

- [ ] **Step 1: Add public API, sentinel, date, and immutability tests**

Tests import the planned API from `aquant.planner` and cover:

```python
def test_previous_is_required_and_none_fails_closed():
    with pytest.raises(TypeError):
        plan_targets(
            as_of=date(2026, 8, 10),
            signal_output={},
            eligible_symbols=frozenset({"600519"}),
            limits=PlannerLimits(),
        )
    with pytest.raises(PlannerError) as exc:
        plan_targets(
            as_of=date(2026, 8, 10),
            signal_output={},
            previous=None,
            eligible_symbols=frozenset({"600519"}),
            limits=PlannerLimits(),
        )
    assert exc.value.code == "invalid_previous_state"


def test_state_defensively_copies_and_normalizes_caller_mapping():
    raw = {"600519": Decimal("0.6"), "000001": Decimal("0")}
    state = PreviousTargets(as_of=date(2026, 8, 9), targets=raw)
    raw["600519"] = Decimal("0.1")
    del raw["000001"]
    raw.clear()
    assert tuple(state.targets.items()) == (
        ("000001", Decimal("0")),
        ("600519", Decimal("0.6")),
    )
    with pytest.raises(TypeError):
        state.targets["600519"] = Decimal("0.2")
```

Add the same defensive-copy check for `PlannedTargets`, insertion-order equality, explicit-zero preservation, deletion rejection, exact-date rejection for state/as_of, valid `FIRST_PERIOD` and `EXPLICIT_RESET`, invalid sentinel reason, and `PLANNER_SCHEMA_VERSION == "1.0.0"`.

- [ ] **Step 2: Add merge, chronology, and multi-period tests**

Cover exact positive override, explicit-zero override, omitted positive carry-forward, omitted-zero carry-forward, first/reset with empty output, equal/future previous rejection, multi-period historical-key preservation, and no invented key:

```python
def test_override_zero_and_omit_are_distinct():
    result = plan_targets(
        as_of=date(2026, 8, 10),
        signal_output={"600519": Decimal("0")},
        previous=PreviousTargets(
            as_of=date(2026, 8, 9),
            targets={"600519": Decimal("0.6"), "000001": Decimal("0.4")},
        ),
        eligible_symbols=frozenset({"600519", "000001"}),
        limits=PlannerLimits(),
    )
    assert result.targets == {
        "000001": Decimal("0.4"),
        "600519": Decimal("0"),
    }
```

- [ ] **Step 3: Add structure, universe, limits, and Decimal-context tests**

Use parametrized tests to assert all stable codes and boundaries:

```python
@pytest.mark.parametrize(
    ("weight", "code"),
    [
        (0, "non_decimal_weight"),
        (0.5, "non_decimal_weight"),
        (Decimal("NaN"), "non_finite_weight"),
        (Decimal("Infinity"), "non_finite_weight"),
        (Decimal("-0.01"), "negative_weight"),
        (Decimal("1.01"), "weight_above_one"),
    ],
)
def test_invalid_weights_fail_closed(weight, code):
    with pytest.raises(PlannerError) as exc:
        PreviousTargets(
            as_of=date(2026, 8, 9),
            targets={"600519": weight},
        )
    assert exc.value.code == code
```

The completed parametrized body constructs a public state or calls `plan_targets()` and asserts `exc.value.code == code`; it contains no production test doubles.

Also cover non-Mapping output, invalid/empty/non-frozenset eligible sets, current unknown symbol, previous drift including an ineligible explicit zero, current and previous validation before merge, effective `0.6 + 0.6` hard-gross failure, and each `PlannerLimits` field/range. Assert the three configured violations separately: single `0.6 > max_single_weight 0.5`, gross `0.8 > max_gross 0.7`, and gross `0.8 > 1 - min_cash_ratio 0.3`. Create identical results under low precision/alternate rounding and the default global context.

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```bash
uv run --no-sync pytest -q tests/unit/test_planner.py
```

Expected: collection fails because `aquant.planner` does not yet exist. Record the exact failure before adding production code.

## Task 2: Implement the minimal pure Planner core

**Files:**
- Create: `src/aquant/planner/core.py`
- Create: `src/aquant/planner/__init__.py`
- Test: `tests/unit/test_planner.py`

- [ ] **Step 1: Add errors, sentinels, limits, and immutable states**

Implement `PlannerError`, `NoPreviousStateReason`, `NoPreviousState`, `PlannerLimits`, `PreviousTargets`, and `PlannedTargets`. Both target-state classes use one private constructor helper that validates exact date/mapping/key/weight rules, creates `dict(sorted(items))`, and wraps the new dict in `MappingProxyType`.

Use an explicit custom equality implementation based on `(as_of, tuple(targets.items()))` if dataclass field comparison would compare proxy identity or otherwise fail normalized-content equality. Do not make the private mutable dict reachable from public attributes.

- [ ] **Step 2: Add fixed-context layer validation and merge**

Implement private helpers equivalent to:

```python
_DECIMAL_CONTEXT = decimal.Context(prec=60)

def _gross(weights: Iterable[Decimal]) -> Decimal:
    with decimal.localcontext(_DECIMAL_CONTEXT):
        total = Decimal("0")
        for weight in weights:
            total += weight
    return total


def plan_targets(*, as_of, signal_output, previous, eligible_symbols, limits):
    _validate_exact_date(as_of)
    _validate_eligible_symbols(eligible_symbols)
    _validate_limits(limits)
    current = _validated_target_copy(signal_output, output=True)
    prior = _validated_previous(previous, as_of)
    _validate_universe("current", current, eligible_symbols)
    _validate_universe("previous", prior, eligible_symbols)
    effective = dict(prior)
    effective.update(current)
    _validate_effective(effective, limits)
    return PlannedTargets(as_of=as_of, targets=effective)
```

The final implementation preserves the design's exact validation/error order and catches only expected public input failures. It does not import `validate_signal_output`, read a universe, normalize weights, or catch arbitrary exceptions.

- [ ] **Step 3: Export only the frozen public surface**

`src/aquant/planner/__init__.py` imports and defines `__all__` for the core names and, after Task 4, the assembly names. It does not change `src/aquant/__init__.py` or any existing package.

- [ ] **Step 4: Run core tests and verify GREEN**

Run:

```bash
uv run --no-sync pytest -q tests/unit/test_planner.py
```

Expected: all core tests pass with no warnings.

- [ ] **Step 5: Inspect the core diff and commit**

Stage only:

```text
src/aquant/planner/core.py
src/aquant/planner/__init__.py
tests/unit/test_planner.py
```

Commit message:

```text
feat(planner): add immutable rolling target core
```

## Task 3: Freeze SignalSpec assembly in failing tests

**Files:**
- Create: `tests/unit/test_planner_assembly.py`

- [ ] **Step 1: Add registry/spec parity and builder-type tests**

```python
def test_signal_specs_are_the_single_registry_aligned_truth_source():
    assert set(SIGNAL_SPECS) == set(SIGNAL_REGISTRY)
    assert all(key == spec.name for key, spec in SIGNAL_SPECS.items())
    assert type(
        build_signal(
            name="sma",
            config={"period": 20},
            eligible_symbols=frozenset({"600519"}),
        )
    ) is SIGNAL_REGISTRY["sma"]
    assert type(
        build_signal(
            name="top_k_momentum",
            config={"lookback": 20, "k": 3},
            eligible_symbols=frozenset({"600519", "000001"}),
        )
    ) is SIGNAL_REGISTRY["top_k_momentum"]
```

Assert the SMA builder preserves its explicit optional `active_weight` Decimal and the TopK builder preserves `lookback`/`k`; no output computation is used to decide cardinality.

- [ ] **Step 2: Add exact config and cardinality failures**

Cover unknown signal, non-Mapping config, missing/unknown keys, wrong parameter types/values, SMA zero/multiple eligible symbols, TopK empty eligible set, and TopK one-or-more success. All builder/config failures exposed by `build_signal()` use `invalid_signal_config`; invalid eligible container uses `invalid_eligible_symbols`; scenario mismatch uses `unsupported_cardinality`.

Add a parity fail-closed test by monkeypatching the module's `SIGNAL_SPECS` to a mismatched immutable mapping and asserting `signal_spec_registry_mismatch` before builder selection.

- [ ] **Step 3: Run assembly tests and verify RED**

Run:

```bash
uv run --no-sync pytest -q tests/unit/test_planner_assembly.py
```

Expected: collection fails because the assembly public names do not exist.

## Task 4: Implement planner-local SignalSpec assembly

**Files:**
- Create: `src/aquant/planner/assembly.py`
- Modify: `src/aquant/planner/__init__.py`
- Test: `tests/unit/test_planner_assembly.py`

- [ ] **Step 1: Implement explicit per-signal builders**

`_build_sma_signal(config)` accepts exactly required `period` and optional `active_weight`; `_build_top_k_momentum_signal(config)` accepts exactly required `lookback` and `k`. Both require a Mapping, reject missing/extra keys, construct the frozen A1 class directly, and translate expected `SignalError`/argument failures into `PlannerError("invalid_signal_config", "signal configuration is invalid")` without exposing raw mappings in messages.

- [ ] **Step 2: Implement immutable specs and assembly validation**

Create `SIGNAL_SPECS` as a read-only mapping whose two entries colocate builder and cardinality. `build_signal()` first validates exact eligible symbols, then checks:

```python
if set(SIGNAL_SPECS) != set(SIGNAL_REGISTRY) or any(
    key != spec.name for key, spec in SIGNAL_SPECS.items()
):
    raise PlannerError(
        "signal_spec_registry_mismatch",
        "planner signal specs do not match the frozen signal registry",
    )
```

It rejects unknown names, applies `SINGLE_SYMBOL` exactly-one or `MULTI_SYMBOL` one-or-more before construction, calls the colocated builder, and verifies the constructed runtime type is exactly `SIGNAL_REGISTRY[name]`; an impossible wrong type raises `planner_invariant_violation`.

- [ ] **Step 3: Export the assembly surface**

Add `SignalCardinality`, `SignalSpec`, `SIGNAL_SPECS`, and `build_signal` to `aquant.planner.__all__`. Do not re-export or modify A1 registry/classes.

- [ ] **Step 4: Run assembly and combined planner tests and verify GREEN**

Run:

```bash
uv run --no-sync pytest -q tests/unit/test_planner_assembly.py
uv run --no-sync pytest -q tests/unit/test_planner.py tests/unit/test_planner_assembly.py
```

Expected: both commands pass with no warnings.

- [ ] **Step 5: Inspect the assembly diff and commit**

Stage only:

```text
src/aquant/planner/assembly.py
src/aquant/planner/__init__.py
tests/unit/test_planner_assembly.py
```

Commit message:

```text
feat(planner): add signal assembly specifications
```

## Task 5: Frozen regression, quality, and scope verification

**Files:** no new files; verification only.

- [ ] **Step 1: Run focused Planner and A1 regression**

```bash
uv run --no-sync pytest -q \
  tests/unit/test_planner.py \
  tests/unit/test_planner_assembly.py \
  tests/unit/test_signals.py
```

- [ ] **Step 2: Run directly related frozen portfolio/backtest regression**

```bash
uv run --no-sync pytest -q \
  tests/unit/test_portfolio_models.py \
  tests/unit/test_portfolio_coordinator.py \
  tests/unit/test_portfolio_accounting.py \
  tests/unit/test_portfolio_equivalence.py \
  tests/unit/test_backtest_baselines.py
```

- [ ] **Step 3: Run repository CI-equivalent checks**

```bash
uv lock --check
tests/scripts/test_check_committed_whitespace.sh
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv build
```

If a local full-suite failure appears, capture the exact failing command, node ID, traceback, Python path/version, and environment. Reproduce it before classification; do not reuse an old Chinese-path or subprocess-import explanation without current evidence, and do not modify frozen code to force green.

- [ ] **Step 4: Verify frozen-file identity and allowlist**

```bash
git diff --exit-code origin/main -- \
  src/aquant/research/signals.py \
  src/aquant/portfolio \
  src/aquant/gate_e \
  release configs

git diff --name-only "$(git merge-base origin/main HEAD)" HEAD
git status --short
```

Expected changed paths are only the frozen design document, this plan, and the five Planner implementation/test files. The final status must be clean after commits.

- [ ] **Step 5: Self-review the complete diff**

Check every frozen design acceptance item against a specific test; confirm `aquant.planner` never imports/calls A1 `validate_signal_output()`, never imports portfolio/coordinator/universe/execution code, preserves exact Decimal values, and contains no serialization, SELL, rebalance, T+0, broker, paper, or CLI wiring.

## Task 6: Independent review, publish, PR, and CI gates

**Files:** only minimal allowlisted fixes if an independent finding is valid.

- [ ] **Step 1: Push implementation commits without rewriting reviewed history**

Push `feat/target-weights-planner-v1` normally. Do not amend the Design Freeze or plan-review commits; do not rebase or force-push.

- [ ] **Step 2: Request Work Buddy read-only code review**

Bind the review to the live repository, Design Freeze SHA, plan SHA, implementation commit range, and latest pushed remote HEAD. Require exact `P0/P1/P2/P3`, file/function, reproducible symptom, risk, smallest correction, and acceptance command. Work Buddy must not modify files.

If `P0/P1/P2 > 0`, apply only valid minimal fixes in allowlisted files using a failing regression test first, rerun affected/full gates, commit separately, push, and request another Work Buddy review. Do not lower severity or widen scope. Proceed only when `P0=P1=P2=0`.

- [ ] **Step 3: Create a ready-for-review PR targeting `main`**

The non-Draft PR body records the Design Freeze commit, plan commit, all implementation/fix commits, pure planner scope, explicitly deferred execution/SELL/rebalance/T+0/serialization adapter, exact local verification outputs, Work Buddy final PASS/counts, frozen-file boundary evidence, and any accepted P3/deferred items.

- [ ] **Step 4: Wait for and inspect real GitHub Checks**

Read every check and job from GitHub. If a check fails, classify it with logs and a local reproduction. A PR regression receives a minimal test-first fix, affected/full validation, a new commit/push, and Work Buddy re-review of affected code. Existing/environment failures are reported with evidence and do not authorize unrelated fixes.

- [ ] **Step 5: Final STOP gate**

Stop only after all are true:

- non-Draft PR exists from `feat/target-weights-planner-v1` to `main`;
- GitHub CI/checks are successful;
- Work Buddy final `P0=0 / P1=0 / P2=0`;
- worktree is clean;
- local HEAD equals live remote feature HEAD;
- PR head/base and mergeability are reported.

Never merge, enable auto-merge, delete the branch, or begin Execution Integration/T+0.
