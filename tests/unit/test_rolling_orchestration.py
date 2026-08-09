from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import pytest

from aquant.data.calendar_snapshot import CalendarSnapshotStore, load_verified_calendar
from aquant.planner import (
    PlannedTargets,
    PlannerLimits,
    PreviousTargets,
    plan_targets,
)
from aquant.portfolio import (
    BuyPosting,
    CashReceivable,
    PortfolioError,
    SymbolValuation,
    close_session,
    create_portfolio_ledger,
    register_receivable,
)
from aquant.rolling import (
    RebalanceAttempt,
    RollingAttemptStatus,
    RollingConfig,
    RollingExecutionInput,
    RollingPortfolioLedger,
    RollingRebalanceResult,
    TargetRealization,
    close_rolling_session,
    create_rolling_ledger,
    post_rolling_buy,
    promote_portfolio_ledger,
    rebalance_to_plan,
)
from aquant.rules import (
    FeeBreakdown,
    InstrumentKind,
    OrderSide,
    PositionLot,
    RejectionReason,
    default_fee_policy,
)

T = date(2026, 7, 14)
T1 = date(2026, 7, 15)
SEED_SESSION = date(2026, 7, 13)


def _calendar(tmp_path, *sessions: date):
    if SEED_SESSION not in sessions:
        sessions = (SEED_SESSION, *sessions)
    record = CalendarSnapshotStore(tmp_path).write(
        sessions,
        source_provider="synthetic",
        source_function="rolling_orchestration_fixture",
        source_version="1",
        fetched_at_utc=datetime(2026, 7, 16, tzinfo=UTC),
    )
    return load_verified_calendar(tmp_path, record)


def _input(
    symbol: str,
    *,
    intent_session: date = T,
    execution_session: date = T1,
    previous_close: Decimal | None = Decimal("10"),
    execution_open: Decimal | None = Decimal("10"),
) -> RollingExecutionInput:
    return RollingExecutionInput(
        symbol=symbol,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        intent_session=intent_session,
        execution_session=execution_session,
        previous_close=previous_close,
        execution_open=execution_open,
    )


def _run(
    *,
    planned: PlannedTargets,
    ledger,
    execution_inputs: tuple[RollingExecutionInput, ...],
    calendar,
    limits: PlannerLimits | None = None,
):
    return rebalance_to_plan(
        config=RollingConfig(limits=PlannerLimits() if limits is None else limits),
        planned=planned,
        ledger=ledger,
        execution_inputs=execution_inputs,
        calendar=calendar,
        fee_policy=default_fee_policy(),
    )


def _zero_fees() -> FeeBreakdown:
    return FeeBreakdown(0, 0, 0, ())


def _ledger_with_position(*, symbol: str = "600519", size: int = 100):
    ledger = create_rolling_ledger(2_000_000)
    return post_rolling_buy(
        ledger,
        BuyPosting(
            event_id=f"seed-buy:{symbol}",
            execution_date=date(2026, 7, 13),
            lot=PositionLot(
                lot_id=f"seed-lot:{symbol}",
                symbol=symbol,
                acquired_date=date(2026, 7, 13),
                available_date=T,
                original_size=size,
                remaining_size=size,
                unit_cost=Decimal("10"),
            ),
            fees=_zero_fees(),
        ),
    )


def _add_position(
    ledger,
    *,
    symbol: str,
    size: int,
    acquired_date: date = date(2026, 7, 13),
    available_date: date = T,
    unit_cost: Decimal = Decimal("10"),
):
    suffix = len(ledger.cash_events)
    return post_rolling_buy(
        ledger,
        BuyPosting(
            event_id=f"seed-buy:{symbol}:{suffix}",
            execution_date=acquired_date,
            lot=PositionLot(
                lot_id=f"seed-lot:{symbol}:{suffix}",
                symbol=symbol,
                acquired_date=acquired_date,
                available_date=available_date,
                original_size=size,
                remaining_size=size,
                unit_cost=unit_cost,
            ),
            fees=_zero_fees(),
        ),
    )


def _close_with_marks(ledger, *, session: date = T, mark: Decimal = Decimal("10")):
    sizes: dict[str, tuple[int, int]] = {}
    for lot in ledger.lots:
        if lot.acquired_date > session or lot.remaining_size == 0:
            continue
        total, available = sizes.get(lot.symbol, (0, 0))
        sizes[lot.symbol] = (
            total + lot.remaining_size,
            available + (lot.remaining_size if lot.available_date <= session else 0),
        )
    return close_rolling_session(
        ledger,
        session,
        tuple(
            SymbolValuation(
                symbol=symbol,
                total_size=total,
                available_size=available,
                locked_size=total - available,
                mark_price=mark,
            )
            for symbol, (total, available) in sorted(sizes.items())
        ),
    )


def test_rebalance_requires_exact_planned_targets_and_exact_next_session(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = create_rolling_ledger(1_000_000)

    with pytest.raises((TypeError, PortfolioError, ValueError)):
        _run(
            planned=object(),  # type: ignore[arg-type]
            ledger=ledger,
            execution_inputs=(_input("600519"),),
            calendar=calendar,
        )
    with pytest.raises((TypeError, PortfolioError, ValueError)):
        _run(
            planned=PlannedTargets(as_of=T, targets={"600519": Decimal("0.1")}),
            ledger=ledger,
            execution_inputs=(_input("600519", execution_session=date(2026, 7, 16)),),
            calendar=calendar,
        )

    assert ledger == create_rolling_ledger(1_000_000)


def test_rebalance_uses_T_close_equity_including_receivable(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    legacy = register_receivable(
        create_portfolio_ledger(1_000_000),
        CashReceivable(
            event_id="dividend:600519:T",
            symbol="600519",
            registered_date=T,
            source_payable_date=T1,
            actual_cash_date=date(2026, 7, 16),
            amount_fen=12_345,
        ),
    )
    ledger = promote_portfolio_ledger(close_session(legacy, T, ()))

    result = _run(
        planned=PlannedTargets(as_of=T, targets={"600519": Decimal("0.5")}),
        ledger=ledger,
        execution_inputs=(_input("600519"),),
        calendar=calendar,
    )

    assert result.equity_fen == 1_012_345
    assert result.targets[0].target_notional_fen == Decimal("506172.5")


def test_pristine_fallback_requires_every_D5_condition_and_legacy_verification(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    planned = PlannedTargets(as_of=T, targets={"600519": Decimal("0")})
    pristine = create_rolling_ledger(1_000_000)

    result = _run(
        planned=planned,
        ledger=pristine,
        execution_inputs=(_input("600519"),),
        calendar=calendar,
    )
    assert result.equity_fen == pristine.initial_cash_fen

    non_pristine = _ledger_with_position()
    with pytest.raises(PortfolioError):
        _run(
            planned=planned,
            ledger=non_pristine,
            execution_inputs=(_input("600519"),),
            calendar=calendar,
        )
    with pytest.raises(PortfolioError):
        _run(
            planned=planned,
            ledger=replace(pristine, cash_fen=pristine.cash_fen - 1),
            execution_inputs=(_input("600519"),),
            calendar=calendar,
        )


def test_target_notional_remains_exact_until_one_share_floor(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    weight = Decimal("0.3333333333333333333333333333333333333333")
    with localcontext() as context:
        context.prec = 80
        exact_notional = weight * Decimal(100_001)
    result = _run(
        planned=PlannedTargets(as_of=T, targets={"600519": weight}),
        ledger=create_rolling_ledger(100_001),
        execution_inputs=(
            _input(
                "600519",
                previous_close=Decimal("1.23"),
                execution_open=Decimal("1.23"),
            ),
        ),
        calendar=calendar,
    )

    target = result.targets[0]
    assert target.target_notional_fen == exact_notional
    assert target.target_shares == 200


def test_held_symbol_missing_from_effective_plan_fails_atomically(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = _ledger_with_position()
    ledger = close_rolling_session(
        ledger,
        T,
        (
            SymbolValuation(
                symbol="600519",
                total_size=100,
                available_size=100,
                locked_size=0,
                mark_price=Decimal("10"),
            ),
        ),
    )
    before = ledger

    with pytest.raises(PortfolioError):
        _run(
            planned=PlannedTargets(as_of=T, targets={"601318": Decimal("0")}),
            ledger=ledger,
            execution_inputs=(_input("601318"),),
            calendar=calendar,
        )

    assert ledger is before
    assert ledger == before


def test_total_target_notional_uses_same_equity_and_respects_max_gross(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = create_rolling_ledger(1_000_000)

    with pytest.raises(PortfolioError):
        _run(
            planned=PlannedTargets(as_of=T, targets={"600519": Decimal("0.6")}),
            ledger=ledger,
            execution_inputs=(_input("600519"),),
            calendar=calendar,
            limits=PlannerLimits(max_gross=Decimal("0.5")),
        )

    assert ledger == create_rolling_ledger(1_000_000)


def test_calendar_end_without_next_session_fails_not_residuals(tmp_path):
    calendar = _calendar(tmp_path, T)

    with pytest.raises(PortfolioError):
        _run(
            planned=PlannedTargets(as_of=T, targets={"600519": Decimal("0")}),
            ledger=create_rolling_ledger(1_000_000),
            execution_inputs=(),
            calendar=calendar,
        )


def test_all_sells_run_before_all_buys_and_each_side_is_symbol_sorted(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = create_rolling_ledger(10_000_000)
    ledger = _add_position(ledger, symbol="601318", size=100)
    ledger = _add_position(ledger, symbol="600519", size=100)
    ledger = _close_with_marks(ledger)
    planned = PlannedTargets(
        as_of=T,
        targets={
            "601318": Decimal("0"),
            "600519": Decimal("0"),
            "000002": Decimal("0.1"),
            "000001": Decimal("0.1"),
        },
    )

    result = _run(
        planned=planned,
        ledger=ledger,
        execution_inputs=tuple(reversed(tuple(_input(symbol) for symbol in planned.targets))),
        calendar=calendar,
    )

    assert tuple((item.side, item.symbol) for item in result.attempts) == (
        (OrderSide.SELL, "600519"),
        (OrderSide.SELL, "601318"),
        (OrderSide.BUY, "000001"),
        (OrderSide.BUY, "000002"),
    )


def test_sell_proceeds_are_available_to_later_buy_in_one_shared_cash_account(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = _add_position(
        create_rolling_ledger(1_000_000),
        symbol="600519",
        size=900,
    )
    ledger = _close_with_marks(ledger)

    result = _run(
        planned=PlannedTargets(
            as_of=T,
            targets={"600519": Decimal("0"), "601318": Decimal("0.9")},
        ),
        ledger=ledger,
        execution_inputs=(_input("601318"), _input("600519")),
        calendar=calendar,
    )

    sell, buy = result.attempts
    assert sell.side is OrderSide.SELL
    assert buy.side is OrderSide.BUY
    assert buy.cash_before_fen == sell.cash_after_fen
    assert result.ledger.cash_fen == buy.cash_after_fen


def test_buy_affordability_decrements_exactly_100_shares_including_fees(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))

    result = _run(
        planned=PlannedTargets(as_of=T, targets={"600519": Decimal("1")}),
        ledger=create_rolling_ledger(1_000_000),
        execution_inputs=(_input("600519"),),
        calendar=calendar,
    )

    attempt = result.attempts[0]
    assert attempt.status is RollingAttemptStatus.FILLED
    assert attempt.requested_size == 1_000
    assert attempt.feasible_size == attempt.filled_size == 900
    assert attempt.quantity_adjustment_reason == "insufficient_cash_including_fees"
    assert result.targets[0].residual_shares == 100
    assert result.ledger.cash_fen >= 0


def test_target_up_down_and_already_aligned_use_realized_share_delta(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = create_rolling_ledger(10_000_000)
    ledger = _add_position(ledger, symbol="600001", size=100)
    ledger = _add_position(ledger, symbol="600002", size=300)
    ledger = _add_position(ledger, symbol="600003", size=200)
    ledger = _close_with_marks(ledger)

    result = _run(
        planned=PlannedTargets(
            as_of=T,
            targets={
                "600001": Decimal("0.02"),
                "600002": Decimal("0.01"),
                "600003": Decimal("0.02"),
            },
        ),
        ledger=ledger,
        execution_inputs=tuple(_input(symbol) for symbol in ("600003", "600001", "600002")),
        calendar=calendar,
    )

    assert tuple((item.side, item.symbol, item.requested_size) for item in result.attempts) == (
        (OrderSide.SELL, "600002", 200),
        (OrderSide.BUY, "600001", 100),
    )
    assert tuple(
        (item.symbol, item.realized_shares, item.is_aligned) for item in result.targets
    ) == (
        ("600001", 200, True),
        ("600002", 100, True),
        ("600003", 200, True),
    )


@pytest.mark.parametrize("future_state", ["buy_event_and_lot", "receivable"])
def test_rebalance_rejects_post_plan_ledger_state_atomically(tmp_path, future_state):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = _close_with_marks(_ledger_with_position())
    if future_state == "buy_event_and_lot":
        ledger = _add_position(
            ledger,
            symbol="600519",
            size=100,
            acquired_date=T1,
            available_date=date(2026, 7, 16),
        )
        assert ledger.cash_events[-1].session > T
        assert ledger.lots[-1].acquired_date > T
    else:
        legacy = register_receivable(
            close_session(create_portfolio_ledger(1_000_000), T, ()),
            CashReceivable(
                event_id="future-receivable:600519",
                symbol="600519",
                registered_date=T1,
                source_payable_date=date(2026, 7, 16),
                actual_cash_date=date(2026, 7, 16),
                amount_fen=1_000,
            ),
        )
        ledger = promote_portfolio_ledger(legacy)
        assert ledger.receivables[-1].registered_date > T
    before = ledger

    with pytest.raises(PortfolioError):
        _run(
            planned=PlannedTargets(as_of=T, targets={"600519": Decimal("0")}),
            ledger=ledger,
            execution_inputs=(_input("600519"),),
            calendar=calendar,
        )

    assert ledger is before
    assert ledger == before


@pytest.mark.parametrize(
    ("acquired_date", "available_date", "expected_code"),
    [
        (
            SEED_SESSION,
            date(2026, 7, 16),
            "lot_availability_binding_mismatch",
        ),
        (
            date(2026, 7, 12),
            SEED_SESSION,
            "lot_acquired_date_outside_calendar",
        ),
    ],
)
def test_rebalance_rejects_lot_without_official_t_plus_one_binding_atomically(
    tmp_path,
    acquired_date,
    available_date,
    expected_code,
):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = _add_position(
        create_rolling_ledger(2_000_000),
        symbol="600519",
        size=100,
        acquired_date=acquired_date,
        available_date=available_date,
    )
    ledger = _close_with_marks(ledger)
    before = ledger

    with pytest.raises(PortfolioError) as captured:
        _run(
            planned=PlannedTargets(as_of=T, targets={"600519": Decimal("0")}),
            ledger=ledger,
            execution_inputs=(_input("600519"),),
            calendar=calendar,
        )

    assert captured.value.code == expected_code
    assert ledger is before
    assert ledger == before


def test_explicit_zero_no_bar_keeps_desired_realized_and_residual_visible(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = _close_with_marks(_ledger_with_position())

    result = _run(
        planned=PlannedTargets(as_of=T, targets={"600519": Decimal("0")}),
        ledger=ledger,
        execution_inputs=(_input("600519", previous_close=None, execution_open=None),),
        calendar=calendar,
    )

    attempt = result.attempts[0]
    target = result.targets[0]
    assert attempt.side is OrderSide.SELL
    assert attempt.status is RollingAttemptStatus.REJECTED
    assert attempt.rejection_reason.value == "suspended_no_bar"
    assert result.ledger is ledger
    assert target.desired_weight == Decimal("0")
    assert target.target_shares == 0
    assert target.realized_shares == target.residual_shares == 100
    assert target.is_aligned is False


def test_explicit_zero_price_limit_keeps_residual_visible(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = _close_with_marks(_ledger_with_position())

    result = _run(
        planned=PlannedTargets(as_of=T, targets={"600519": Decimal("0")}),
        ledger=ledger,
        execution_inputs=(
            _input(
                "600519",
                previous_close=Decimal("10"),
                execution_open=Decimal("9"),
            ),
        ),
        calendar=calendar,
    )

    assert result.attempts[0].status is RollingAttemptStatus.REJECTED
    assert result.attempts[0].rejection_reason.value == "price_limit_open"
    assert result.ledger == ledger
    assert result.targets[0].residual_shares == 100
    assert result.targets[0].is_aligned is False


def test_later_effective_zero_plan_and_legal_session_recomputes_and_converges(tmp_path):
    T2 = date(2026, 7, 16)
    calendar = _calendar(tmp_path, T, T1, T2, date(2026, 7, 17))
    ledger = _close_with_marks(_ledger_with_position())
    first = _run(
        planned=PlannedTargets(as_of=T, targets={"600519": Decimal("0")}),
        ledger=ledger,
        execution_inputs=(_input("600519", previous_close=None, execution_open=None),),
        calendar=calendar,
    )
    next_ledger = _close_with_marks(first.ledger, session=T1)

    second = _run(
        planned=PlannedTargets(as_of=T1, targets={"600519": Decimal("0")}),
        ledger=next_ledger,
        execution_inputs=(
            _input(
                "600519",
                intent_session=T1,
                execution_session=T2,
            ),
        ),
        calendar=calendar,
    )

    assert first.targets[0].residual_shares == 100
    assert second.attempts[0].status is RollingAttemptStatus.FILLED
    assert second.targets[0].realized_shares == 0
    assert second.targets[0].residual_shares == 0
    assert second.targets[0].is_aligned is True


def test_residual_does_not_create_a_shadow_target_or_mark_failure_achieved(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = _close_with_marks(_ledger_with_position())
    planned = PlannedTargets(as_of=T, targets={"600519": Decimal("0")})
    execution_inputs = (_input("600519", previous_close=None, execution_open=None),)

    first = _run(
        planned=planned,
        ledger=ledger,
        execution_inputs=execution_inputs,
        calendar=calendar,
    )
    repeated = _run(
        planned=planned,
        ledger=ledger,
        execution_inputs=execution_inputs,
        calendar=calendar,
    )

    assert first == repeated
    assert first.planned is planned
    assert first.ledger is ledger
    assert first.targets[0].residual_shares == 100
    assert first.targets[0].is_aligned is False
    assert "residual" not in RollingPortfolioLedger.__dataclass_fields__
    assert "residual" not in RebalanceAttempt.__dataclass_fields__


def test_positive_target_no_bar_has_unknown_sizing_and_no_attempt(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))

    result = _run(
        planned=PlannedTargets(as_of=T, targets={"600519": Decimal("0.5")}),
        ledger=create_rolling_ledger(1_000_000),
        execution_inputs=(_input("600519", previous_close=None, execution_open=None),),
        calendar=calendar,
    )

    assert result.attempts == ()
    assert result.targets[0].target_shares is None
    assert result.targets[0].residual_shares is None
    assert result.targets[0].is_aligned is False


def test_rebalance_consumes_complete_effective_planner_state_without_second_carry_forward(
    tmp_path,
):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    planned = plan_targets(
        as_of=T,
        signal_output={"601318": Decimal("0.2")},
        previous=PreviousTargets(
            as_of=date(2026, 7, 13),
            targets={"600519": Decimal("0.1")},
        ),
        eligible_symbols=frozenset({"600519", "601318"}),
        limits=PlannerLimits(),
    )

    result = _run(
        planned=planned,
        ledger=create_rolling_ledger(10_000_000),
        execution_inputs=(_input("601318"), _input("600519")),
        calendar=calendar,
    )

    assert tuple((item.symbol, item.desired_weight) for item in result.targets) == (
        ("600519", Decimal("0.1")),
        ("601318", Decimal("0.2")),
    )


@pytest.mark.parametrize(
    ("previous_close", "execution_open"),
    [
        (None, Decimal("10")),
        (Decimal("10"), None),
        (Decimal("0"), Decimal("10")),
        (Decimal("10"), Decimal("NaN")),
        (10, 10),
    ],
)
def test_execution_prices_are_both_positive_exact_decimals_or_both_none(
    tmp_path,
    previous_close,
    execution_open,
):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))

    with pytest.raises(PortfolioError):
        _run(
            planned=PlannedTargets(as_of=T, targets={"600519": Decimal("0")}),
            ledger=create_rolling_ledger(1_000_000),
            execution_inputs=(
                _input(
                    "600519",
                    previous_close=previous_close,
                    execution_open=execution_open,
                ),
            ),
            calendar=calendar,
        )


def test_reversed_execution_inputs_produce_structurally_identical_result(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    ledger = create_rolling_ledger(10_000_000)
    planned = PlannedTargets(
        as_of=T,
        targets={"600519": Decimal("0.1"), "601318": Decimal("0.2")},
    )
    inputs = (_input("600519"), _input("601318"))

    forward = _run(
        planned=planned,
        ledger=ledger,
        execution_inputs=inputs,
        calendar=calendar,
    )
    reverse = _run(
        planned=planned,
        ledger=ledger,
        execution_inputs=tuple(reversed(inputs)),
        calendar=calendar,
    )

    assert forward == reverse
    assert tuple(item.attempt_id for item in forward.attempts) == (
        f"{T.isoformat()}:{T1.isoformat()}:buy:600519",
        f"{T.isoformat()}:{T1.isoformat()}:buy:601318",
    )


def test_public_orchestration_contract_fields_are_exact_and_frozen():
    assert tuple(item.name for item in fields(RollingConfig)) == ("limits",)
    assert tuple(item.name for item in fields(RollingExecutionInput)) == (
        "symbol",
        "instrument_kind",
        "intent_session",
        "execution_session",
        "previous_close",
        "execution_open",
    )
    assert tuple(item.name for item in fields(RebalanceAttempt)) == (
        "attempt_id",
        "plan_as_of",
        "execution_session",
        "symbol",
        "side",
        "target_weight",
        "target_notional_fen",
        "target_shares",
        "realized_before",
        "requested_size",
        "feasible_size",
        "filled_size",
        "status",
        "rejection_reason",
        "fees",
        "cash_before_fen",
        "cash_after_fen",
        "quantity_adjustment_reason",
    )
    assert tuple(item.name for item in fields(TargetRealization)) == (
        "symbol",
        "desired_weight",
        "target_notional_fen",
        "target_shares",
        "realized_shares",
        "residual_shares",
        "is_aligned",
    )
    assert tuple(item.name for item in fields(RollingRebalanceResult)) == (
        "planned",
        "execution_session",
        "equity_fen",
        "attempts",
        "targets",
        "ledger",
    )
    config = RollingConfig(PlannerLimits())
    with pytest.raises(FrozenInstanceError):
        config.limits = PlannerLimits(max_gross=Decimal("0.5"))  # type: ignore[misc]


def test_public_evidence_types_reject_internally_inconsistent_states():
    with pytest.raises(PortfolioError):
        RollingExecutionInput(
            symbol="600519",
            instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
            intent_session=T,
            execution_session=T1,
            previous_close=Decimal("10"),
            execution_open=None,
        )

    with pytest.raises(PortfolioError):
        RebalanceAttempt(
            attempt_id=f"{T.isoformat()}:{T1.isoformat()}:buy:600519",
            plan_as_of=T,
            execution_session=T1,
            symbol="600519",
            side=OrderSide.BUY,
            target_weight=Decimal("0.1"),
            target_notional_fen=Decimal("100000"),
            target_shares=100,
            realized_before=0,
            requested_size=0,
            feasible_size=100,
            filled_size=100,
            status=RollingAttemptStatus.FILLED,
            rejection_reason=None,
            fees=_zero_fees(),
            cash_before_fen=1_000_000,
            cash_after_fen=900_000,
            quantity_adjustment_reason=None,
        )

    with pytest.raises(PortfolioError):
        TargetRealization(
            symbol="600519",
            desired_weight=Decimal("0"),
            target_notional_fen=Decimal("0"),
            target_shares=0,
            realized_shares=100,
            residual_shares=0,
            is_aligned=True,
        )


def test_unsupported_instrument_identity_fails_even_when_target_is_aligned(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))

    with pytest.raises(PortfolioError):
        _run(
            planned=PlannedTargets(as_of=T, targets={"bad": Decimal("0")}),
            ledger=create_rolling_ledger(1_000_000),
            execution_inputs=(_input("bad"),),
            calendar=calendar,
        )


def test_rebalance_revalidates_unsupported_identity_even_when_aligned(tmp_path):
    calendar = _calendar(tmp_path, T, T1, date(2026, 7, 16))
    execution_input = _input("600519")
    object.__setattr__(execution_input, "symbol", "bad")

    with pytest.raises(PortfolioError):
        _run(
            planned=PlannedTargets(as_of=T, targets={"bad": Decimal("0")}),
            ledger=create_rolling_ledger(1_000_000),
            execution_inputs=(execution_input,),
            calendar=calendar,
        )


@pytest.mark.parametrize(
    "inconsistency",
    [
        "unknown_target",
        "wrong_phase",
        "wrong_requested_delta",
        "rejected_cash_changed",
        "cash_adjustment_on_sell",
        "partial_adjustment_on_buy",
        "adjustment_without_reduction",
    ],
)
def test_public_attempt_rejects_phase_delta_and_cash_inconsistency(inconsistency):
    values = {
        "attempt_id": f"{T.isoformat()}:{T1.isoformat()}:buy:600519",
        "plan_as_of": T,
        "execution_session": T1,
        "symbol": "600519",
        "side": OrderSide.BUY,
        "target_weight": Decimal("0.1"),
        "target_notional_fen": Decimal("100000"),
        "target_shares": 100,
        "realized_before": 0,
        "requested_size": 100,
        "feasible_size": 100,
        "filled_size": 100,
        "status": RollingAttemptStatus.FILLED,
        "rejection_reason": None,
        "fees": _zero_fees(),
        "cash_before_fen": 1_000_000,
        "cash_after_fen": 900_000,
        "quantity_adjustment_reason": None,
    }
    if inconsistency == "unknown_target":
        values["target_shares"] = None
    elif inconsistency == "wrong_phase":
        values.update(
            target_shares=0,
            realized_before=100,
        )
    elif inconsistency == "wrong_requested_delta":
        values.update(
            requested_size=200,
            feasible_size=200,
            filled_size=200,
        )
    elif inconsistency == "rejected_cash_changed":
        values.update(
            attempt_id=f"{T.isoformat()}:{T1.isoformat()}:sell:600519",
            side=OrderSide.SELL,
            target_shares=0,
            realized_before=100,
            feasible_size=0,
            filled_size=0,
            status=RollingAttemptStatus.REJECTED,
            rejection_reason=RejectionReason.SUSPENDED_NO_BAR,
            fees=None,
            cash_after_fen=999_999,
        )
    elif inconsistency == "cash_adjustment_on_sell":
        values.update(
            attempt_id=f"{T.isoformat()}:{T1.isoformat()}:sell:600519",
            side=OrderSide.SELL,
            target_shares=0,
            realized_before=200,
            requested_size=200,
            feasible_size=100,
            filled_size=100,
            quantity_adjustment_reason="insufficient_cash_including_fees",
        )
    elif inconsistency == "partial_adjustment_on_buy":
        values.update(
            target_shares=200,
            requested_size=200,
            feasible_size=100,
            filled_size=100,
            quantity_adjustment_reason="partial_sellable_position",
        )
    else:
        values["quantity_adjustment_reason"] = "insufficient_cash_including_fees"

    with pytest.raises(PortfolioError):
        RebalanceAttempt(**values)


@pytest.mark.parametrize(
    "inconsistency",
    [
        "invalid_planned",
        "negative_equity",
        "list_attempts",
        "missing_targets",
        "wrong_session",
        "wrong_target_weight",
        "wrong_terminal_realized",
        "broken_attempt_cash_chain",
    ],
)
def test_public_rebalance_result_rejects_inconsistent_evidence(inconsistency):
    ledger = create_rolling_ledger(1_000_000)
    planned = PlannedTargets(as_of=T, targets={"600519": Decimal("0")})
    target = TargetRealization(
        symbol="600519",
        desired_weight=Decimal("0"),
        target_notional_fen=Decimal("0"),
        target_shares=0,
        realized_shares=0,
        residual_shares=0,
        is_aligned=True,
    )
    values = {
        "planned": planned,
        "execution_session": T1,
        "equity_fen": 1_000_000,
        "attempts": (),
        "targets": (target,),
        "ledger": ledger,
    }
    if inconsistency == "invalid_planned":
        values["planned"] = object()
    elif inconsistency == "negative_equity":
        values["equity_fen"] = -1
    elif inconsistency == "list_attempts":
        values["attempts"] = []
    elif inconsistency == "missing_targets":
        values["targets"] = ()
    elif inconsistency == "wrong_session":
        values["execution_session"] = T
    elif inconsistency == "wrong_target_weight":
        values["targets"] = (
            TargetRealization(
                symbol="600519",
                desired_weight=Decimal("0.1"),
                target_notional_fen=Decimal("100000"),
                target_shares=0,
                realized_shares=0,
                residual_shares=0,
                is_aligned=True,
            ),
        )
    elif inconsistency == "wrong_terminal_realized":
        values["targets"] = (
            TargetRealization(
                symbol="600519",
                desired_weight=Decimal("0"),
                target_notional_fen=Decimal("0"),
                target_shares=0,
                realized_shares=100,
                residual_shares=100,
                is_aligned=False,
            ),
        )
    else:
        planned = PlannedTargets(as_of=T, targets={"600519": Decimal("0.1")})
        target = TargetRealization(
            symbol="600519",
            desired_weight=Decimal("0.1"),
            target_notional_fen=Decimal("100000"),
            target_shares=100,
            realized_shares=0,
            residual_shares=100,
            is_aligned=False,
        )
        values.update(
            planned=planned,
            targets=(target,),
            attempts=(
                RebalanceAttempt(
                    attempt_id=f"{T.isoformat()}:{T1.isoformat()}:buy:600519",
                    plan_as_of=T,
                    execution_session=T1,
                    symbol="600519",
                    side=OrderSide.BUY,
                    target_weight=Decimal("0.1"),
                    target_notional_fen=Decimal("100000"),
                    target_shares=100,
                    realized_before=0,
                    requested_size=100,
                    feasible_size=0,
                    filled_size=0,
                    status=RollingAttemptStatus.REJECTED,
                    rejection_reason=RejectionReason.SUSPENDED_NO_BAR,
                    fees=None,
                    cash_before_fen=999_999,
                    cash_after_fen=999_999,
                    quantity_adjustment_reason=None,
                ),
            ),
        )

    with pytest.raises(PortfolioError):
        RollingRebalanceResult(**values)


def test_public_rebalance_result_rejects_unreported_current_execution_event():
    event_id = f"{T.isoformat()}:{T1.isoformat()}:buy:600519"
    ledger = post_rolling_buy(
        create_rolling_ledger(1_000_000),
        BuyPosting(
            event_id=event_id,
            execution_date=T1,
            lot=PositionLot(
                lot_id=event_id,
                symbol="600519",
                acquired_date=T1,
                available_date=date(2026, 7, 16),
                original_size=100,
                remaining_size=100,
                unit_cost=Decimal("10"),
            ),
            fees=_zero_fees(),
        ),
    )

    with pytest.raises(PortfolioError):
        RollingRebalanceResult(
            planned=PlannedTargets(
                as_of=T,
                targets={"600519": Decimal("0.1")},
            ),
            execution_session=T1,
            equity_fen=1_000_000,
            attempts=(),
            targets=(
                TargetRealization(
                    symbol="600519",
                    desired_weight=Decimal("0.1"),
                    target_notional_fen=Decimal("100000"),
                    target_shares=100,
                    realized_shares=100,
                    residual_shares=0,
                    is_aligned=True,
                ),
            ),
            ledger=ledger,
        )
