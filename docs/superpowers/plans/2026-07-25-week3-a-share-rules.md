# Week 3 A-Share Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a conservative, auditable A-share execution layer for the fixed four-instrument sample without expanding into unsupported boards, live trading, or strategy-performance claims.

**Architecture:** A content-addressed trading calendar and exact fee policy enter the formal backtest as verified inputs. Pure rule modules decide lot size, T+1 availability, price limits, fees, cash, and rejection reasons; one thin Backtrader adapter enforces every decision before execution.

**Tech Stack:** Python 3.11+, Backtrader, pandas, Decimal, dataclasses, pytest, Ruff, uv.

**Design source:** `docs/superpowers/specs/2026-07-25-week3-a-share-rules-design.md`

---

## File map

Create:

- `src/aquant/data/calendar_snapshot.py` — immutable calendar content, manifest, storage, verification.
- `src/aquant/rules/__init__.py` — public rule contracts.
- `src/aquant/rules/models.py` — enums and immutable decision/lot/fee models.
- `src/aquant/rules/calendar.py` — session lookup and coverage.
- `src/aquant/rules/fees.py` — date-effective schedules and integer-fen fee calculations.
- `src/aquant/rules/lots.py` — T+1 lots, FIFO consumption, odd-lot policy.
- `src/aquant/rules/price_limits.py` — tick-aware 10% limit calculations.
- `src/aquant/rules/engine.py` — deterministic pre-trade decision composition.
- `src/aquant/backtest/execution.py` — thin Backtrader enforcement adapter.
- `tests/unit/test_calendar_snapshot.py` — calendar artifact security and determinism.
- `tests/unit/test_a_share_rules.py` — pure rule matrix.
- `tests/unit/test_a_share_execution.py` — Backtrader integration and no-bypass tests.
- `docs/a_share_execution_rules.md` — operator-facing rule and limitation guide.
- `outputs/Claude复核清单_第3周.md` — code-level review contract.

Modify:

- `src/aquant/data/ingestion.py` — publish the validated calendar and retain market-session gaps.
- `src/aquant/data/manifest.py` — keep symbol manifest independent while permitting recorded gaps.
- `src/aquant/backtest/models.py` — add verified rule inputs and richer ledgers.
- `src/aquant/backtest/data_access.py` — pair exact market and calendar inputs.
- `src/aquant/backtest/runner.py` — install rule adapter, bind run identity, assert ledgers.
- `src/aquant/backtest/strategies.py` — emit intents through the shared adapter only.
- `src/aquant/backtest/export.py` — export fees, lots, missing sessions, and hashes.
- `src/aquant/backtest_cli.py` — require `--calendar-id` and explicit fee assumptions.
- `src/aquant/backtest/__init__.py` — export formal APIs.
- `tests/unit/test_ingestion_cli.py` — calendar publication and gap policy.
- `tests/unit/test_backtest_baselines.py` — preserve Week 2 behavior through the new formal contract.

---

### Task 1: Freeze the shared trading calendar

**Files:**
- Create: `src/aquant/data/calendar_snapshot.py`
- Create: `tests/unit/test_calendar_snapshot.py`
- Modify: `src/aquant/data/__init__.py`

- [ ] **Step 1: Write failing content-addressing and verification tests**

```python
def test_calendar_id_is_canonical_content_sha256(tmp_path):
    store = CalendarSnapshotStore(tmp_path)
    first = store.write(
        [date(2026, 7, 22), date(2026, 7, 23)],
        source_provider="sina",
        source_function="tool_trade_date_hist_sina",
        source_version="1.18.64",
        fetched_at_utc=datetime(2026, 7, 24, tzinfo=UTC),
    )
    second = store.write(
        [date(2026, 7, 22), date(2026, 7, 23)],
        source_provider="sina",
        source_function="tool_trade_date_hist_sina",
        source_version="1.18.65",
        fetched_at_utc=datetime(2026, 7, 25, tzinfo=UTC),
    )
    assert first.calendar_id == second.calendar_id
    assert first.relative_path == second.relative_path


@pytest.mark.parametrize(
    "dates",
    [
        [date(2026, 7, 23), date(2026, 7, 22)],
        [date(2026, 7, 22), date(2026, 7, 22)],
        [date(2026, 7, 25)],
    ],
)
def test_calendar_rejects_unsorted_duplicate_or_weekend_dates(tmp_path, dates):
    with pytest.raises(CalendarError, match="calendar dates are invalid"):
        CalendarSnapshotStore(tmp_path).write(
            dates,
            source_provider="sina",
            source_function="tool_trade_date_hist_sina",
            source_version="1.18.64",
            fetched_at_utc=datetime(2026, 7, 24, tzinfo=UTC),
        )
```

- [ ] **Step 2: Run the new tests and confirm RED**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q tests/unit/test_calendar_snapshot.py
```

Expected: import failure for `aquant.data.calendar_snapshot`.

- [ ] **Step 3: Implement canonical payload, record, store, and loader**

```python
@dataclass(frozen=True)
class CalendarRecord:
    schema_version: str
    calendar_id: str
    file_sha256: str
    source_provider: str
    source_function: str
    source_version: str
    fetched_at_utc: datetime
    first_date: date
    last_complete_date: date
    row_count: int
    relative_path: Path


def canonical_calendar_bytes(dates: tuple[date, ...]) -> bytes:
    payload = {
        "schema_version": "1.0",
        "dates": [value.isoformat() for value in dates],
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


@dataclass(frozen=True, init=False)
class VerifiedTradingCalendar:
    dates: tuple[date, ...]
    calendar_id: str
    file_sha256: str

    def next_session(self, value: date) -> date | None:
        index = bisect.bisect_right(self.dates, value)
        return None if index == len(self.dates) else self.dates[index]
```

Implement the storage boundary explicitly:

1. resolve `project_root`, `data/calendars`, the content file, and the manifest;
2. require every resolved path to remain beneath `project_root`;
3. reject an existing calendar file or manifest when it is a symlink or has `st_nlink != 1`;
4. write a new calendar through a sibling temporary file opened with exclusive creation;
5. flush and `fsync` the file, then publish it with an atomic rename;
6. hash the published file, reopen it without following symlinks, hash it again, and require both hashes to match `calendar_id`;
7. parse the canonical JSON and recompute the ID from its dates instead of trusting manifest metadata;
8. append the manifest record only after the content file passes verification.

Store canonical files at `data/calendars/<calendar_id>.json`; append exact records to `data/calendars/manifest.jsonl`.

- [ ] **Step 4: Add traversal, symlink, corruption, duplicate-record, and forged-metadata tests**

```python
def test_calendar_loader_rejects_content_tampering(tmp_path):
    record = stored_calendar_record(tmp_path)
    path = tmp_path / record.relative_path
    path.chmod(0o644)
    path.write_text('{"schema_version":"1.0","dates":[]}\n')
    with pytest.raises(CalendarError, match="content verification"):
        load_verified_calendar(tmp_path, record)


def test_verified_calendar_cannot_be_constructed_by_callers():
    with pytest.raises(TypeError):
        VerifiedTradingCalendar((), "a" * 64, "a" * 64)
```

- [ ] **Step 5: Run calendar tests and the existing snapshot security suite**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q \
  tests/unit/test_calendar_snapshot.py \
  tests/unit/test_snapshot_manifest.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/aquant/data/calendar_snapshot.py src/aquant/data/__init__.py \
  tests/unit/test_calendar_snapshot.py
git commit -m "feat: add immutable trading calendar snapshots"
```

---

### Task 2: Publish calendar evidence and retain market gaps

**Files:**
- Modify: `src/aquant/data/ingestion.py`
- Modify: `src/aquant/cli.py`
- Modify: `tests/unit/test_ingestion_cli.py`

- [ ] **Step 1: Replace the old missing-date rejection test with explicit calendar evidence**

```python
def test_ingestion_publishes_calendar_and_records_symbol_gaps(tmp_path):
    dates = ("2017-12-29", "2018-01-02", "2018-01-03", "2026-07-17", "2026-07-20")
    gap_dates = ("2017-12-29", "2018-01-02", "2026-07-17", "2026-07-20")
    results = tuple(
        _result(item, frame=_raw_sina(gap_dates if item.symbol == "600519" else dates))
        for item in INSTRUMENTS
    )
    calendar = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                [
                    "2018-01-02",
                    "2018-01-03",
                    "2026-07-17",
                    "2026-07-20",
                    "2026-07-21",
                    "2026-12-31",
                ]
            )
        }
    )

    result = run_ingestion(
        CONFIG,
        client=FakeClient(results),
        clock=lambda: NOW,
        trade_calendar_provider=lambda: calendar,
        snapshot_store=RawSnapshotStore(tmp_path),
        manifest_writer=ManifestWriter(tmp_path / "data/manifests/manifest.jsonl"),
        calendar_store=CalendarSnapshotStore(tmp_path),
        akshare_version="1.18.64",
    )

    assert len(result.items) == 4
    assert result.calendar_record.last_complete_date == date(2026, 7, 17)
    assert result.missing_sessions == (
        SymbolMissingSessions("600519", (date(2018, 1, 3),)),
    )
```

- [ ] **Step 2: Run the test and confirm RED**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q \
  tests/unit/test_ingestion_cli.py::test_ingestion_publishes_calendar_and_records_symbol_gaps
```

Expected: `run_ingestion` does not accept `calendar_store` and still raises `missing_trading_date`.

- [ ] **Step 3: Extend the ingestion result without coupling symbol manifest hashes to calendar**

```python
@dataclass(frozen=True)
class SymbolMissingSessions:
    symbol: str
    dates: tuple[date, ...]


@dataclass(frozen=True)
class RunResult:
    requested_start: date
    requested_end: date
    fetched_at_utc: datetime
    items: tuple[RunItemResult, ...]
    calendar_record: CalendarRecord
    missing_sessions: tuple[SymbolMissingSessions, ...]
```

Keep rejecting market dates outside the verified calendar. Replace `expected_calendar_dates - actual_dates` failure with a deterministic, sorted tuple stored in `RunResult`. Publish the calendar artifact independently; do not add calendar fields to `ManifestRecord` or to the market snapshot bytes.

- [ ] **Step 4: Prove old market snapshots remain unchanged**

```python
def test_new_calendar_does_not_change_existing_market_snapshot_hash(tmp_path):
    first = _run(tmp_path)
    market_hashes = {item.snapshot_id: item.snapshot_sha256 for item in first.items}

    extended = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                [
                    "2018-01-02",
                    "2026-07-17",
                    "2026-07-20",
                    "2026-07-21",
                    "2026-07-22",
                    "2026-12-31",
                ]
            )
        }
    )
    second = run_ingestion(
        CONFIG,
        client=FakeClient(),
        clock=lambda: NOW,
        trade_calendar_provider=lambda: extended,
        snapshot_store=RawSnapshotStore(tmp_path),
        manifest_writer=ManifestWriter(tmp_path / "data/manifests/manifest.jsonl"),
        calendar_store=CalendarSnapshotStore(tmp_path),
        akshare_version="1.18.64",
    )
    assert all(
        item.snapshot_sha256 == market_hashes[item.snapshot_id]
        for item in second.items
        if item.snapshot_id in market_hashes
    )
    assert first.calendar_record.calendar_id != second.calendar_record.calendar_id
```

Update the existing `_run()` helper in `tests/unit/test_ingestion_cli.py` to pass
`calendar_store=CalendarSnapshotStore(tmp_path)` so every pre-existing test exercises
the new publication boundary.

- [ ] **Step 5: Run ingestion and calendar suites**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q \
  tests/unit/test_ingestion_cli.py \
  tests/unit/test_calendar_snapshot.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/aquant/data/ingestion.py src/aquant/cli.py \
  tests/unit/test_ingestion_cli.py
git commit -m "feat: preserve trading-calendar gap evidence"
```

---

### Task 3: Define exact rule and fee contracts

**Files:**
- Create: `src/aquant/rules/__init__.py`
- Create: `src/aquant/rules/models.py`
- Create: `src/aquant/rules/fees.py`
- Create: `tests/unit/test_a_share_rules.py`

- [ ] **Step 1: Write failing fee schedule and fee-breakdown tests**

```python
def test_stock_fee_schedule_switches_on_effective_dates():
    policy = default_fee_policy()
    before = calculate_fees(
        policy,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        side=OrderSide.SELL,
        execution_date=date(2023, 8, 25),
        notional=Decimal("10000.00"),
    )
    after = calculate_fees(
        policy,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        side=OrderSide.SELL,
        execution_date=date(2023, 8, 28),
        notional=Decimal("10000.00"),
    )
    assert before.stamp_duty_fen == 1000
    assert after.stamp_duty_fen == 500


def test_etf_has_commission_but_no_stamp_or_transfer_fee():
    fees = calculate_fees(
        default_fee_policy(),
        instrument_kind=InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF,
        side=OrderSide.SELL,
        execution_date=date(2026, 7, 22),
        notional=Decimal("10000.00"),
    )
    assert fees.commission_fen == 500
    assert fees.stamp_duty_fen == 0
    assert fees.transfer_fee_fen == 0
```

- [ ] **Step 2: Run and confirm RED**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q tests/unit/test_a_share_rules.py
```

Expected: import failure for `aquant.rules`.

- [ ] **Step 3: Implement immutable enums and decisions**

```python
class InstrumentKind(StrEnum):
    MAIN_BOARD_STOCK = "main_board_stock"
    DOMESTIC_EQUITY_BROAD_BASED_ETF = "domestic_equity_broad_based_etf"


class RejectionReason(StrEnum):
    UNSUPPORTED_INSTRUMENT = "unsupported_instrument"
    MISSING_CALENDAR_COVERAGE = "missing_calendar_coverage"
    NO_NEXT_SESSION_IN_RANGE = "no_next_session_in_range"
    SUSPENDED_NO_BAR = "suspended_no_bar"
    MISSING_PREVIOUS_CLOSE = "missing_previous_close"
    PRICE_LIMIT_OPEN = "price_limit_open"
    INVALID_LOT_SIZE = "invalid_lot_size"
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_SELLABLE_POSITION = "insufficient_sellable_position"
    MISSING_FEE_SCHEDULE = "missing_fee_schedule"
    INVALID_FEE_CONFIGURATION = "invalid_fee_configuration"


@dataclass(frozen=True)
class RuleDecision:
    allowed: bool
    reason: RejectionReason | None
    fees: FeeBreakdown | None
```

Define the validated fee boundary as a frozen exact type whose public constructor is
disabled:

```python
@dataclass(frozen=True, init=False)
class VerifiedFeePolicy:
    stock_commission: CommissionAssumption
    etf_commission: CommissionAssumption
    stamp_duty_schedule: tuple[tuple[date, Decimal], ...]
    transfer_fee_schedule: tuple[tuple[date, Decimal], ...]
    policy_digest: str
```

Only `make_fee_policy()` and `default_fee_policy()` may create it after validating
exact built-in types, canonical schedule order, effective-date uniqueness, positive
rates/minimums, and its SHA-256 policy digest. Formal runners reject subclasses and
caller-forged instances.

- [ ] **Step 4: Implement exact fee policy and integer-fen output**

```python
def latest_effective_rate(
    schedule: tuple[tuple[date, Decimal], ...],
    execution_date: date,
) -> tuple[date, Decimal]:
    matches = [item for item in schedule if item[0] <= execution_date]
    if not matches:
        raise FeePolicyError("missing_fee_schedule")
    return matches[-1]


def to_fen(value: Decimal) -> int:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(rounded * 100)
```

Build exact-type validation into `FeePolicy`; use stock commission and ETF commission as separate `CommissionAssumption` values. Default stock and ETF commission are `0.00025`, minimum `5.00`, both directions. Stock stamp duty and transfer schedules match the design file.

- [ ] **Step 5: Add forged policy, duplicate date, unsorted date, float, and pre-schedule tests**

```python
@pytest.mark.parametrize(
    "schedule",
    [
        ((date(2023, 8, 28), 0.0005),),
        ((date(2023, 8, 28), Decimal("0.0005")),
         (date(2008, 9, 19), Decimal("0.001"))),
        ((date(2023, 8, 28), Decimal("0.0005")),
         (date(2023, 8, 28), Decimal("0.0004"))),
    ],
)
def test_fee_policy_rejects_non_decimal_unsorted_or_duplicate_schedule(schedule):
    with pytest.raises(FeePolicyError):
        make_fee_policy(stamp_duty_schedule=schedule)
```

- [ ] **Step 6: Run tests and commit**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q tests/unit/test_a_share_rules.py
```

Expected: all fee and model tests pass.

Commit:

```bash
git add src/aquant/rules tests/unit/test_a_share_rules.py
git commit -m "feat: add exact A-share fee contracts"
```

---

### Task 4: Implement calendar, price-limit, lot, and rule-engine pure functions

**Files:**
- Create: `src/aquant/rules/calendar.py`
- Create: `src/aquant/rules/price_limits.py`
- Create: `src/aquant/rules/lots.py`
- Create: `src/aquant/rules/engine.py`
- Modify: `src/aquant/rules/__init__.py`
- Modify: `tests/unit/test_a_share_rules.py`

- [ ] **Step 1: Write the failing T+1, odd-lot, and price-limit matrix**

```python
def _stored_calendar(tmp_path, *values: str) -> VerifiedTradingCalendar:
    record = CalendarSnapshotStore(tmp_path).write(
        tuple(date.fromisoformat(value) for value in values),
        source_provider="synthetic",
        source_function="pytest_fixture",
        source_version="1",
        fetched_at_utc=datetime(2026, 7, 16, tzinfo=UTC),
    )
    return load_verified_calendar(tmp_path, record)


def test_t_plus_one_uses_calendar_and_does_not_wait_for_symbol_bar(tmp_path):
    calendar = _stored_calendar(tmp_path, "2026-07-13", "2026-07-14", "2026-07-15")
    lot = create_buy_lot(
        lot_id="lot-0001",
        symbol="600519",
        acquired_date=date(2026, 7, 13),
        size=100,
        unit_cost=Decimal("10.00"),
        calendar=calendar,
    )
    assert lot.available_date == date(2026, 7, 14)
    assert sellable_size((lot,), date(2026, 7, 13)) == 0
    assert sellable_size((lot,), date(2026, 7, 14)) == 100


@pytest.mark.parametrize(
    ("position", "requested", "expected"),
    [(250, 100, True), (250, 150, False), (250, 250, True), (50, 50, True)],
)
def test_sell_lot_policy_allows_only_board_lots_or_full_liquidation(
    position, requested, expected
):
    assert validate_sell_size(position, requested) is expected


def test_stock_and_etf_use_different_ticks_for_limit_rounding():
    assert price_limits(Decimal("10.01"), InstrumentKind.MAIN_BOARD_STOCK) == (
        Decimal("9.01"),
        Decimal("11.01"),
    )
    assert price_limits(
        Decimal("3.333"),
        InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF,
    ) == (Decimal("3.000"), Decimal("3.666"))
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q tests/unit/test_a_share_rules.py \
  -k "t_plus_one or lot_policy or limit_rounding"
```

Expected: missing pure-rule functions.

- [ ] **Step 3: Implement session lookup, lots, and FIFO consumption**

```python
def create_buy_lot(
    *,
    lot_id: str,
    symbol: str,
    acquired_date: date,
    size: int,
    unit_cost: Decimal,
    calendar: VerifiedTradingCalendar,
) -> PositionLot:
    available = calendar.next_session(acquired_date)
    if available is None:
        raise RuleInputError("no_next_session_in_range")
    return PositionLot(
        lot_id=lot_id,
        symbol=symbol,
        acquired_date=acquired_date,
        available_date=available,
        original_size=size,
        remaining_size=size,
        unit_cost=unit_cost,
    )


def consume_fifo(
    lots: tuple[PositionLot, ...],
    *,
    execution_date: date,
    requested_size: int,
) -> tuple[PositionLot, ...]:
    if sellable_size(lots, execution_date) < requested_size:
        raise RuleInputError("insufficient_sellable_position")
    remaining = requested_size
    updated = []
    for lot in lots:
        take = min(lot.remaining_size, remaining) if lot.available_date <= execution_date else 0
        updated.append(replace(lot, remaining_size=lot.remaining_size - take))
        remaining -= take
    return tuple(updated)
```

- [ ] **Step 4: Implement tick-aware limit prices**

```python
def price_limits(
    previous_close: Decimal,
    instrument_kind: InstrumentKind,
) -> tuple[Decimal, Decimal]:
    tick = (
        Decimal("0.01")
        if instrument_kind is InstrumentKind.MAIN_BOARD_STOCK
        else Decimal("0.001")
    )
    lower = (previous_close * Decimal("0.90")).quantize(tick, rounding=ROUND_HALF_UP)
    upper = (previous_close * Decimal("1.10")).quantize(tick, rounding=ROUND_HALF_UP)
    return lower, upper
```

- [ ] **Step 5: Compose one deterministic pre-trade decision**

`evaluate_order()` must validate in this order so one input yields one stable reason:

1. exact supported symbol/kind pair;
2. target calendar session exists;
3. target market bar exists;
4. previous close and open are valid decimals;
5. lot size;
6. price-limit open;
7. sellable T+1 position for sells;
8. fee policy;
9. cash for buys.

```python
decision = evaluate_order(
    intent=OrderIntent(
        order_id="order-0001",
        symbol="600519",
        signal_date=date(2026, 7, 13),
        side=OrderSide.BUY,
        requested_size=100,
    ),
    instrument=InstrumentRule(
        symbol="600519",
        kind=InstrumentKind.MAIN_BOARD_STOCK,
    ),
    calendar=calendar,
    available_bar_dates=frozenset({date(2026, 7, 13), date(2026, 7, 14)}),
    previous_close=Decimal("10.00"),
    execution_open=Decimal("10.20"),
    cash_fen=1_000_000,
    lots=(),
    fee_policy=verified_fee_policy,
)
assert decision == RuleDecision(allowed=True, reason=None, fees=expected_fees)
```

- [ ] **Step 6: Run all pure-rule tests and commit**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q tests/unit/test_a_share_rules.py
```

Expected: all tests pass.

Commit:

```bash
git add src/aquant/rules tests/unit/test_a_share_rules.py
git commit -m "feat: add deterministic A-share rule engine"
```

---

### Task 5: Enforce rules in the shared Backtrader execution path

**Files:**
- Create: `src/aquant/backtest/execution.py`
- Create: `tests/unit/test_a_share_execution.py`
- Modify: `src/aquant/backtest/models.py`
- Modify: `src/aquant/backtest/strategies.py`
- Modify: `src/aquant/backtest/runner.py`
- Modify: `src/aquant/backtest/__init__.py`

- [ ] **Step 1: Write failing integration tests for next-open rules**

```python
def test_missing_target_session_is_rejected_and_not_filled_on_reopen(tmp_path):
    frame = _market_frame_for_dates(
        ("2026-07-13", "2026-07-15"),
        opens=(10.0, 12.0),
        highs=(10.5, 12.5),
        lows=(9.5, 11.5),
        closes=(10.0, 12.0),
    )
    calendar = _stored_calendar(
        tmp_path, "2026-07-13", "2026-07-14", "2026-07-15"
    )
    result = run_synthetic_backtest(
        frame,
        calendar=calendar,
        fee_policy=default_fee_policy(),
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=10_000.0,
            stake=100,
        ),
        symbol="600519",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
    )
    assert result.orders[0].target_execution_date == date(2026, 7, 14)
    assert result.orders[0].final_status == "rejected"
    assert result.orders[0].rejection_reason == "suspended_no_bar"
    assert result.fills == ()


def test_buy_at_limit_open_is_rejected_but_intraday_touch_after_open_is_allowed(tmp_path):
    rejected = _run_buy_fixture(
        tmp_path, execution_open=11.00, execution_high=11.00, previous_close=10.00
    )
    allowed = _run_buy_fixture(
        tmp_path, execution_open=10.50, execution_high=11.00, previous_close=10.00
    )
    assert rejected.orders[0].rejection_reason == "price_limit_open"
    assert allowed.fills[0].price == 10.50
```

Before these tests, add the following local helpers to
`tests/unit/test_a_share_execution.py`; do not import helpers from another test module:

```python
def _stored_calendar(tmp_path, *values: str) -> VerifiedTradingCalendar:
    record = CalendarSnapshotStore(tmp_path).write(
        tuple(date.fromisoformat(value) for value in values),
        source_provider="synthetic",
        source_function="pytest_fixture",
        source_version="1",
        fetched_at_utc=datetime(2026, 7, 31, tzinfo=UTC),
    )
    return load_verified_calendar(tmp_path, record)


def _market_frame_for_dates(
    dates: tuple[str, ...],
    *,
    opens: tuple[float, ...],
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    closes: tuple[float, ...],
) -> pd.DataFrame:
    size = len(dates)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": (10_000,) * size,
            "amount": (100_000.0,) * size,
        }
    )


def _run_buy_fixture(
    tmp_path,
    *,
    execution_open: float,
    execution_high: float,
    previous_close: float,
    initial_cash: float = 10_000.0,
) -> BacktestResult:
    frame = _market_frame_for_dates(
        ("2026-07-13", "2026-07-14"),
        opens=(previous_close, execution_open),
        highs=(previous_close, execution_high),
        lows=(previous_close, min(previous_close, execution_open)),
        closes=(previous_close, execution_open),
    )
    return run_synthetic_backtest(
        frame,
        calendar=_stored_calendar(tmp_path, "2026-07-13", "2026-07-14"),
        fee_policy=default_fee_policy(),
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=initial_cash,
            stake=100,
        ),
        symbol="600519",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
    )
```

Extend the existing test-only `run_synthetic_backtest()` boundary with the exact
keyword-only arguments `calendar`, `fee_policy`, and `instrument_kind`. It remains
labelled synthetic and must still reject caller-built formal production inputs.

- [ ] **Step 2: Run integration tests and confirm RED**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q tests/unit/test_a_share_execution.py
```

Expected: missing execution adapter and formal inputs.

- [ ] **Step 3: Extend result models without removing Week 2 audit fields**

```python
@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    signal_date: date
    target_execution_date: date | None
    side: str
    requested_size: int
    final_status: str
    rejection_reason: str | None


@dataclass(frozen=True)
class FillRecord:
    order_id: str
    execution_date: date
    side: str
    size: int
    price: float
    value: float
    commission_fen: int
    stamp_duty_fen: int
    transfer_fee_fen: int
    total_fees_fen: int
```

Add `PositionLotRecord`, `missing_market_sessions`, `calendar_provenance`, and `touched_fee_rates` to `BacktestResult`.

- [ ] **Step 4: Implement a shared execution adapter**

Implement `RuleAwareBackBroker(bt.brokers.BackBroker)` against the pinned
Backtrader `1.9.78.123` private hook
`_try_exec_market(self, order, popen, phigh, plow)`.

`ExecutionGateway.submit_intent()` computes the official target session, creates
the stable local order ID, submits the engine market order, and attaches the local
ID and target date through `order.addinfo()`. Only `execution.py` may call
Backtrader `buy()` or `sell()`; strategies call the gateway.

At `_try_exec_market`, the broker reads the current bar date and:

1. rejects `current_date > target_execution_date` as `suspended_no_bar`;
2. returns without execution when `current_date < target_execution_date`;
3. on equality, calls `evaluate_order()` using the actual opening price, previous
   unadjusted close, exact cash, and current lot ledger;
4. records a rejected terminal order and returns without calling `super()` when
   denied;
5. sets the exact fee breakdown on a single-order commission context, calls
   `super()._try_exec_market()`, then clears the context in `finally`;
6. reconciles broker position/cash, FIFO lots, and the completed Fill before the
   next order can be processed.

Disable Backtrader's submit-time pseudo cash check with `checksubmit=False`; the
rule engine performs the authoritative full-order cash check at the actual open,
including every fee. `RuleAwareCommissionInfo.getcommission()` returns the active
order's `total_fees_fen / 100` only during the real execution call, so Backtrader
cash and the integer-fen audit ledger use the same charge.

After `cerebro.run()`, `ExecutionGateway.finalize_pending()` closes any still
submitted order. If its official target session exists within the verified calendar
but has no bar before the dataset ends, use `suspended_no_bar`; if no next official
session exists in calendar coverage, use `no_next_session_in_range`. No pending
order may remain in the exported result.

Do not add checks inside `BuyAndHoldStrategy` or `SmaStrategy`; both continue to emit only intents.

Add a compatibility test that asserts the pinned
`BackBroker._try_exec_market` signature is exactly
`(self, order, popen, phigh, plow)`. A dependency upgrade that changes the private
hook must fail closed until this adapter is reviewed.

- [ ] **Step 5: Add T+1, FIFO, cash, fee, and bypass tests**

```python
def test_strategy_cannot_fill_without_shared_rule_adapter(monkeypatch, tmp_path):
    monkeypatch.setattr(AuditedBaselineStrategy, "_execution_adapter", None)
    with pytest.raises(RuntimeError, match="execution adapter is required"):
        _run_buy_fixture(tmp_path, execution_open=10.20, execution_high=10.50,
                         previous_close=10.00)


def test_cash_must_cover_notional_and_all_fees_without_shrinking(tmp_path):
    result = _run_buy_fixture(
        tmp_path,
        initial_cash=10_004.99,
        execution_open=100.00,
        execution_high=100.00,
        previous_close=100.00,
    )
    assert result.orders[0].rejection_reason == "insufficient_cash"
    assert result.fills == ()
```

- [ ] **Step 6: Run integration plus Week 2 regression tests**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q \
  tests/unit/test_a_share_execution.py \
  tests/unit/test_backtest_baselines.py
```

Expected: all selected tests pass; close-signal/next-open examples remain unchanged except explicit fees and richer fields.

- [ ] **Step 7: Commit**

```bash
git add src/aquant/backtest tests/unit/test_a_share_execution.py \
  tests/unit/test_backtest_baselines.py
git commit -m "feat: enforce A-share rules before Backtrader fills"
```

---

### Task 6: Bind formal inputs, CLI, run identity, and atomic exports

**Files:**
- Modify: `src/aquant/backtest/data_access.py`
- Modify: `src/aquant/backtest/runner.py`
- Modify: `src/aquant/backtest/export.py`
- Modify: `src/aquant/backtest_cli.py`
- Modify: `tests/unit/test_backtest_baselines.py`
- Modify: `tests/unit/test_a_share_execution.py`

- [ ] **Step 1: Write failing formal-boundary tests**

```python
def test_cli_requires_exact_calendar_id_and_fee_assumptions(capsys):
    exit_code = main(
        [
            "run",
            "--project-root", ".",
            "--symbol", "600519",
            "--snapshot-id", "a" * 64,
            "--strategy", "buy_and_hold",
        ]
    )
    assert exit_code == 1
    assert '"error_code":"invalid_arguments"' in capsys.readouterr().err


def test_run_id_changes_when_calendar_or_fee_policy_changes(tmp_path):
    first = _run_identity_fixture(
        tmp_path, calendar_dates=("2026-07-13", "2026-07-14"),
        stock_commission="0.00025"
    )
    second = _run_identity_fixture(
        tmp_path, calendar_dates=("2026-07-13", "2026-07-14", "2026-07-15"),
        stock_commission="0.00025"
    )
    third = _run_identity_fixture(
        tmp_path, calendar_dates=("2026-07-13", "2026-07-14"),
        stock_commission="0.00030"
    )
    assert len({first.run_id, second.run_id, third.run_id}) == 3
```

Define `_run_identity_fixture()` in `tests/unit/test_a_share_execution.py`:

```python
def _run_identity_fixture(
    tmp_path,
    *,
    calendar_dates: tuple[str, ...],
    stock_commission: str,
) -> BacktestResult:
    return run_synthetic_backtest(
        _market_frame_for_dates(
            ("2026-07-13", "2026-07-14"),
            opens=(10.0, 10.2),
            highs=(10.5, 10.5),
            lows=(9.5, 9.8),
            closes=(10.0, 10.2),
        ),
        calendar=_stored_calendar(tmp_path, *calendar_dates),
        fee_policy=make_fee_policy(
            stock_commission=CommissionAssumption(
                rate=Decimal(stock_commission),
                minimum_yuan=Decimal("5.00"),
            ),
            etf_commission=CommissionAssumption(
                rate=Decimal("0.00025"),
                minimum_yuan=Decimal("5.00"),
            ),
        ),
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=10_000.0,
            stake=100,
        ),
        symbol="600519",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
    )
```

The frame, strategy, cash, stake, symbol, and instrument kind are identical across
calls; only the named calendar or commission input changes.

- [ ] **Step 2: Run and confirm RED**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q \
  tests/unit/test_backtest_baselines.py \
  tests/unit/test_a_share_execution.py \
  -k "calendar_id or fee_policy or requires_exact_calendar"
```

Expected: CLI accepts runs without calendar ID and run identity lacks new inputs.

- [ ] **Step 3: Require exact verified inputs**

Change formal signature to:

```python
def run_backtest(
    market_data: VerifiedMarketData,
    *,
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
    config: BacktestConfig,
) -> BacktestResult:
```

Reject subclasses and caller-built objects exactly as Week 2 rejects unverified market frames. Carry `instrument_kind` from the exact manifest record into `DataProvenance`.

- [ ] **Step 4: Extend CLI with explicit calendar and commission fields**

Add required `--calendar-id` and exact decimal-string options:

```text
--stock-commission-rate 0.00025
--stock-minimum-commission 5.00
--etf-commission-rate 0.00025
--etf-minimum-commission 5.00
```

Reject floats, scientific notation, signs, commas, whitespace, duplicate calendar records, unsafe paths, and incomplete fee settings. Parse with `Decimal` from canonical strings.

- [ ] **Step 5: Extend atomic export and artifact manifest**

Write these payload files before atomic publication:

```text
run.json
orders.csv
fills.csv
positions.csv
cash.csv
equity.csv
lots.csv
missing_sessions.json
artifact_manifest.json
```

`artifact_manifest.json` must list SHA-256 for the preceding eight payload files and `status="complete"`. Existing partial-directory and conflict behavior remains fail-closed.

- [ ] **Step 6: Add touched-rate and accounting tests**

```python
def test_run_metadata_contains_every_touched_stamp_duty_rate(tmp_path):
    result = _run_sell_fixture_across_stamp_duty_change(tmp_path)
    assert [fill.execution_date for fill in result.fills if fill.side == "sell"] == [
        date(2023, 8, 25),
        date(2023, 8, 29),
    ]
    assert [
        (item.effective_date.isoformat(), item.rate)
        for item in result.touched_fee_rates
        if item.fee_name == "stamp_duty"
    ] == [("2008-09-19", "0.001"), ("2023-08-28", "0.0005")]


def test_daily_position_identity_includes_available_and_locked(tmp_path):
    result = _run_buy_fixture(
        tmp_path,
        execution_open=10.20,
        execution_high=10.50,
        previous_close=10.00,
    )
    for row in result.positions:
        assert row.total_size == row.available_size + row.locked_size
```

Define the helper in the same test module; it must generate the two sells through
the strategy and adapter rather than injecting touched rates:

```python
def _run_sell_fixture_across_stamp_duty_change(tmp_path) -> BacktestResult:
    dates = (
        "2023-08-21",
        "2023-08-22",
        "2023-08-23",
        "2023-08-24",
        "2023-08-25",
        "2023-08-28",
        "2023-08-29",
    )
    closes = (10.0, 9.0, 11.0, 8.0, 12.0, 7.0, 7.0)
    return run_synthetic_backtest(
        _market_frame_for_dates(
            dates,
            opens=(10.0,) * len(dates),
            highs=(12.5,) * len(dates),
            lows=(6.5,) * len(dates),
            closes=closes,
        ),
        calendar=_stored_calendar(tmp_path, *dates),
        fee_policy=default_fee_policy(),
        config=BacktestConfig(
            strategy=StrategyName.SMA,
            initial_cash=100_000.0,
            stake=100,
            sma_period=2,
        ),
        symbol="600519",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
    )
```

- [ ] **Step 7: Run all backtest, rules, and export tests**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q \
  tests/unit/test_a_share_rules.py \
  tests/unit/test_a_share_execution.py \
  tests/unit/test_backtest_baselines.py
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/aquant/backtest src/aquant/backtest_cli.py \
  tests/unit/test_backtest_baselines.py tests/unit/test_a_share_execution.py
git commit -m "feat: bind A-share rules to audited backtest artifacts"
```

---

### Task 7: Full verification, real snapshots, documentation, and review gate

**Files:**
- Create: `docs/a_share_execution_rules.md`
- Create: `outputs/Claude复核清单_第3周.md`
- Create: `outputs/A股量化项目_第3周交付与验收.md`
- Modify: `outputs/A股量化项目_6周执行路线图.md`

- [ ] **Step 1: Run the complete automated gate**

Run:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run pytest -q
UV_PROJECT_ENVIRONMENT=venv uv run ruff check src tests
uv lock --check
UV_PROJECT_ENVIRONMENT=venv uv build
git diff --check
```

Expected: every command exits 0; record the exact test count instead of predicting it.

- [ ] **Step 2: Create and verify the latest calendar artifact**

Run the project ingestion command to fetch the calendar, then resolve the exact `calendar_id` from `data/calendars/manifest.jsonl`. Verify:

```bash
UV_PROJECT_ENVIRONMENT=venv uv run aquant-data fetch --project-root .
```

Expected: one exact complete calendar record; its file SHA-256, row count, first date, and last complete date match the stored canonical file.

- [ ] **Step 3: Re-run four symbols and two baselines**

Resolve IDs from the verified manifests, fail if either lookup is empty, and then
run both strategies with the same calendar and explicit fee assumptions:

```bash
WEEK3_CALENDAR_ID=$(jq -r '.calendar_id' data/calendars/manifest.jsonl | tail -n 1)
WEEK3_SNAPSHOT_ID=$(jq -r 'select(.symbol == "600519") | .snapshot_id' \
  data/manifests/manifest.jsonl | tail -n 1)
test -n "$WEEK3_CALENDAR_ID"
test "$WEEK3_CALENDAR_ID" != "null"
test -n "$WEEK3_SNAPSHOT_ID"
test "$WEEK3_SNAPSHOT_ID" != "null"

UV_PROJECT_ENVIRONMENT=venv uv run aquant-backtest run \
  --project-root . \
  --symbol 600519 \
  --snapshot-id "$WEEK3_SNAPSHOT_ID" \
  --calendar-id "$WEEK3_CALENDAR_ID" \
  --strategy buy_and_hold \
  --initial-cash 1000000 \
  --stake 100 \
  --stock-commission-rate 0.00025 \
  --stock-minimum-commission 5.00 \
  --etf-commission-rate 0.00025 \
  --etf-minimum-commission 5.00
```

Repeat for SMA(20) and all four symbols, resolving each symbol's snapshot ID from
the verified local manifest immediately before use.

- [ ] **Step 4: Independently recompute artifacts**

For all eight runs verify:

- every completed order executes on its target official session;
- no rejected order has a Fill;
- every buy/sell amount and fee component matches integer-fen recomputation;
- total position equals available plus locked;
- cash plus marked position equals equity for every row;
- all artifact-manifest hashes match;
- same-input reruns reuse the same run ID and bundle.

- [ ] **Step 5: Write operator and delivery documents**

`docs/a_share_execution_rules.md` must state:

- exact supported symbols and kinds;
- T+1 for all four;
- conservative open-limit rejection;
- calendar-gap meaning and limitation;
- fee schedules, explicit commission assumptions, and rounding;
- no partial fills, auto-shrink, live trading, or strategy claim.

`outputs/Claude复核清单_第3周.md` must require source inspection, complete tests, exact run IDs, and P0/P1/P2 findings. It must explicitly test the 24-item matrix from the design.

- [ ] **Step 6: Run Codex self-review**

Inspect the complete diff from commit `15dbd8b`, challenge:

- public APIs with caller-built verified objects;
- direct Backtrader `buy`/`sell` bypasses;
- calendar hash/path/symlink races;
- fee schedule type tricks and duplicate dates;
- missing-session orders filling at reopen;
- same-day lot sales;
- float/Decimal drift;
- partial export and silent overwrite.

Fix each validated issue with a failing regression test before production code.

- [ ] **Step 7: Submit to Claude Code**

Claude Code must inspect the repository and run the same commands. The Week 3 gate is:

```text
P0=0
P1=0
```

P2 findings are evaluated technically and either fixed or documented. Do not mark Week 3 accepted from document-only review.

- [ ] **Step 8: Commit verified delivery evidence**

```bash
git add docs/a_share_execution_rules.md \
  outputs/Claude复核清单_第3周.md \
  outputs/A股量化项目_第3周交付与验收.md \
  outputs/A股量化项目_6周执行路线图.md
git commit -m "docs: record Week 3 A-share rule evidence"
```

---

## Plan self-review checklist

- Every design section maps to at least one task.
- Calendar and market manifests remain independent.
- T+1 availability is based on the market calendar, not symbol bars.
- Missing target bars reject orders without postponing lot availability.
- Full odd-lot liquidation remains allowed.
- Decimal fee calculations are audited as integer fen.
- No implementation task adds unsupported symbols, boards, live trading, Qlib, or agents.
- No task claims strategy profitability.
