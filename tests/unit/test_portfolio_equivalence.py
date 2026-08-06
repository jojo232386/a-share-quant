from __future__ import annotations

import importlib
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from aquant.portfolio import (
    AttemptStatus,
    AvailabilityStatus,
    CashEventKind,
    TargetStatus,
    decimal_yuan_to_fen,
)

TESTS_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(TESTS_ROOT))
gate_d_support = importlib.import_module("portfolio_gate_d_support")
sys.path.pop(0)


def _fen(value: float) -> int:
    return decimal_yuan_to_fen(Decimal(str(value)))


def _fee_touch_tuple(value) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.fee_name,
            item.effective_date,
            Decimal(str(item.rate)),
            (
                None
                if item.minimum_yuan is None
                else Decimal(str(item.minimum_yuan))
            ),
        )
        for item in value
    )


def _assert_same_verified_inputs(pair) -> None:
    v01 = pair.v01
    identity = pair.v02.identity
    closure = json.loads(identity.input_closure_json)
    market = closure["market_data"]
    actions = closure["corporate_actions"]

    assert len(market) == len(actions) == 1
    assert market[0]["symbol"] == pair.scenario.symbol
    assert market[0]["snapshot_id"] == v01.provenance.snapshot_id
    assert market[0]["input_digest"] == v01.input_digest
    assert market[0]["file_sha256"] == v01.provenance.file_sha256
    assert market[0]["verification_method"] == "manifest_sha256"

    assert v01.corporate_action_provenance is not None
    assert actions[0]["snapshot_id"] == (
        v01.corporate_action_provenance.snapshot_id
    )
    assert actions[0]["file_sha256"] == (
        v01.corporate_action_provenance.file_sha256
    )
    assert actions[0]["verification_method"] == "manifest_sha256"

    assert v01.rule_provenance is not None
    assert v01.rule_provenance.calendar_id == identity.calendar_id
    assert v01.rule_provenance.calendar_sha256 == identity.calendar_sha256
    assert v01.rule_provenance.fee_policy_digest == (
        identity.fee_policy_digest
    )
    assert v01.universe_id == identity.universe_id
    assert _fen(v01.config.initial_cash) == pair.scenario.initial_cash_fen
    assert v01.config.target_weight == pair.scenario.target_weight
    v02_config = pair.v02.result.config
    assert v02_config.initial_cash_fen == pair.scenario.initial_cash_fen
    assert v02_config.gross_target_weight == pair.scenario.target_weight
    assert v02_config.strategy.value == "buy_and_hold"
    assert v02_config.signal_date == pair.scenario.official_dates[0]
    assert v02_config.end_date == pair.scenario.official_dates[-2]
    assert v01.missing_market_sessions == ()


def _assert_signal_day_initial_state(pair) -> None:
    v01 = pair.v01
    signal_date = pair.scenario.official_dates[0]

    assert v01.positions[0].date == signal_date
    assert v01.cash_ledger[0].date == signal_date
    assert v01.receivables[0].date == signal_date
    assert v01.equity_curve[0].date == signal_date
    assert v01.positions[0].size == 0
    assert v01.positions[0].available_size == 0
    assert v01.positions[0].locked_size == 0
    assert _fen(v01.positions[0].market_value) == 0
    assert _fen(v01.cash_ledger[0].cash) == pair.scenario.initial_cash_fen
    assert v01.receivables[0].balance_fen == 0
    assert _fen(v01.equity_curve[0].equity) == (
        pair.scenario.initial_cash_fen
    )
    assert all(fill.execution_date != signal_date for fill in v01.fills)
    assert all(
        snapshot.session != signal_date
        for snapshot in pair.v02.result.ledger.daily_snapshots
    )


def _assert_single_fill_equivalence(pair) -> None:
    v01 = pair.v01
    v02 = pair.v02.result
    signal_date = pair.scenario.official_dates[0]
    execution_date = pair.shared.calendar.next_session(signal_date)

    assert execution_date is not None
    assert len(v01.orders) == len(v01.fills) == len(v01.lots) == 1
    assert len(v02.targets) == len(v02.attempts) == len(v02.ledger.lots) == 1
    fill_cash_events = tuple(
        event
        for event in v02.ledger.cash_events
        if event.event_kind is CashEventKind.FILL
    )
    assert len(fill_cash_events) == 1

    order = v01.orders[0]
    fill = v01.fills[0]
    target = v02.targets[0]
    attempt = v02.attempts[0]
    event = fill_cash_events[0]
    v01_lot = v01.lots[0]
    v02_lot = v02.ledger.lots[0]

    assert order.signal_date == target.signal_date == signal_date
    assert attempt.original_signal_date == signal_date
    assert attempt.intent_session == signal_date
    assert order.target_execution_date == execution_date
    assert fill.execution_date == execution_date
    assert attempt.execution_session == execution_date
    assert event.session == execution_date
    assert v02_lot.acquired_date == execution_date
    assert order.side == fill.side == event.side.value == "buy"

    assert order.final_status == "completed"
    assert order.rejection_reason is None
    assert target.status is TargetStatus.FILLED
    assert target.attempts_used == 1
    assert attempt.status is AttemptStatus.FILLED
    assert attempt.attempt_number == 1
    assert attempt.availability_status is AvailabilityStatus.AVAILABLE
    assert attempt.rejection_reason is None

    assert order.requested_size == fill.size
    assert fill.size == attempt.requested_size
    assert attempt.requested_size == v01_lot.original_size
    assert v01_lot.original_size == v02_lot.original_size
    assert v01_lot.remaining_size == v02_lot.remaining_size
    assert v01_lot.remaining_size == v01_lot.original_size
    assert v02_lot.remaining_size == v02_lot.original_size
    assert v02_lot.remaining_size == fill.size
    assert Decimal(str(fill.price)) == Decimal(v01_lot.unit_cost)
    assert Decimal(v01_lot.unit_cost) == v02_lot.unit_cost
    assert _fen(fill.value) == event.notional_fen

    assert attempt.fees is not None
    assert fill.commission_fen == attempt.fees.commission_fen
    assert fill.commission_fen == event.commission_fen
    assert fill.stamp_duty_fen == attempt.fees.stamp_duty_fen
    assert fill.stamp_duty_fen == event.stamp_duty_fen
    assert fill.transfer_fee_fen == attempt.fees.transfer_fee_fen
    assert fill.transfer_fee_fen == event.transfer_fee_fen
    assert fill.total_fees_fen == attempt.fees.total_fees_fen
    assert fill.total_fees_fen == event.total_fees_fen
    assert _fen(fill.commission) == fill.total_fees_fen
    assert _fee_touch_tuple(v01.touched_fee_rates) == (
        _fee_touch_tuple(attempt.fees.touched_rates)
    )

    assert event.cash_before_fen == pair.scenario.initial_cash_fen
    assert event.cash_after_fen == (
        pair.scenario.initial_cash_fen
        - event.notional_fen
        - event.total_fees_fen
    )
    assert v01_lot.symbol == v02_lot.symbol == pair.scenario.symbol
    assert v01_lot.acquired_date == v02_lot.acquired_date
    assert v01_lot.available_date == v02_lot.available_date
    assert v01_lot.available_date == (
        pair.shared.calendar.next_session(execution_date)
    )


def _assert_daily_account_equivalence(pair) -> None:
    v01 = pair.v01
    v02 = pair.v02.result
    expected_v01_dates = pair.scenario.official_dates[:-1]
    expected_v02_dates = expected_v01_dates[1:]

    assert tuple(item.date for item in v01.positions) == expected_v01_dates
    assert tuple(item.date for item in v01.cash_ledger) == expected_v01_dates
    assert tuple(item.date for item in v01.receivables) == expected_v01_dates
    assert tuple(item.date for item in v01.equity_curve) == expected_v01_dates
    assert tuple(
        item.session for item in v02.ledger.daily_snapshots
    ) == expected_v02_dates
    assert tuple(item.session for item in v02.availability) == (
        expected_v02_dates
    )
    assert all(
        item.status is AvailabilityStatus.AVAILABLE
        and item.adjustment_reason == "bar_close"
        and item.carried_sessions == 0
        for item in v02.availability
    )

    positions = {item.date: item for item in v01.positions}
    cash = {item.date: item for item in v01.cash_ledger}
    receivables = {item.date: item for item in v01.receivables}
    equity = {item.date: item for item in v01.equity_curve}
    availability = {
        item.session: item for item in v02.availability
    }
    for snapshot in v02.ledger.daily_snapshots:
        assert len(snapshot.valuations) == 1
        valuation = snapshot.valuations[0]
        v01_position = positions[snapshot.session]

        assert valuation.symbol == pair.scenario.symbol
        assert v01_position.size == valuation.total_size
        assert v01_position.available_size == valuation.available_size
        assert v01_position.locked_size == valuation.locked_size
        assert Decimal(str(v01_position.close)) == valuation.mark_price
        assert _fen(v01_position.market_value) == (
            valuation.market_value_fen
        )
        assert valuation.market_value_fen == (
            snapshot.position_market_value_fen
        )
        assert _fen(cash[snapshot.session].cash) == snapshot.cash_fen
        assert receivables[snapshot.session].balance_fen == (
            snapshot.receivable_fen
        )
        assert _fen(equity[snapshot.session].equity) == (
            snapshot.equity_fen
        )
        assert availability[snapshot.session].mark_price == (
            valuation.mark_price
        )

    execution_date = v01.fills[0].execution_date
    available_date = v01.lots[0].available_date
    assert positions[execution_date].available_size == 0
    assert positions[execution_date].locked_size == v01.fills[0].size
    assert positions[available_date].available_size == v01.fills[0].size
    assert positions[available_date].locked_size == 0


def _assert_complete_economic_equivalence(pair) -> None:
    _assert_same_verified_inputs(pair)
    _assert_signal_day_initial_state(pair)
    _assert_single_fill_equivalence(pair)
    _assert_daily_account_equivalence(pair)


def _assert_normal_dividend_equivalence(pair) -> None:
    v01 = pair.v01
    v02 = pair.v02.result

    assert len(v01.corporate_action_ledger) == 2
    registered, paid = v01.corporate_action_ledger
    assert registered.event_type == "receivable_registered"
    assert paid.event_type == "cash_paid"
    assert registered.event_id == paid.event_id

    assert len(v02.dividends) == 1
    assert len(v02.ledger.receivables) == 1
    dividend = v02.dividends[0]
    receivable = v02.ledger.receivables[0]
    payment_events = tuple(
        event
        for event in v02.ledger.cash_events
        if event.event_kind is CashEventKind.DIVIDEND_PAYMENT
    )
    assert len(payment_events) == 1
    payment = payment_events[0]

    assert registered.event_id == dividend.event_id
    assert registered.event_id == receivable.event_id
    assert registered.date == dividend.ex_date
    assert registered.date == receivable.registered_date
    assert paid.date == dividend.source_payable_date
    assert paid.date == dividend.actual_cash_date
    assert paid.date == receivable.actual_cash_date
    assert paid.date == receivable.paid_date
    assert paid.date == payment.session
    assert registered.entitled_size == dividend.entitled_size
    assert paid.entitled_size == registered.entitled_size
    assert registered.cash_dividend_per_unit == (
        str(dividend.cash_dividend_per_unit)
    )
    assert paid.cash_dividend_per_unit == (
        registered.cash_dividend_per_unit
    )
    assert registered.amount_fen == paid.amount_fen
    assert registered.amount_fen == dividend.amount_fen
    assert registered.amount_fen == receivable.amount_fen
    assert registered.amount_fen == payment.notional_fen
    assert registered.amount_fen > 0
    source_event = pair.shared.corporate_actions.events[0]
    entitled_from_lots = sum(
        lot.remaining_size
        for lot in v02.ledger.lots
        if lot.acquired_date <= source_event.record_date
    )
    assert dividend.entitled_size == entitled_from_lots
    assert dividend.amount_fen == decimal_yuan_to_fen(
        dividend.cash_dividend_per_unit * dividend.entitled_size
    )
    assert payment.symbol == pair.scenario.symbol
    assert payment.reference_id == dividend.event_id
    assert payment.side is None
    assert payment.commission_fen == 0
    assert payment.stamp_duty_fen == 0
    assert payment.transfer_fee_fen == 0
    assert payment.total_fees_fen == 0
    assert payment.cash_after_fen - payment.cash_before_fen == (
        dividend.amount_fen
    )

    closure = json.loads(pair.v02.identity.input_closure_json)
    assert pair.v01.dividend_tax_mode == "gross_before_personal_tax"
    assert closure["behavior_modes"]["dividend_tax_mode"] == (
        "gross-before-personal-tax-v1"
    )

    ex_date = dividend.ex_date
    payable_date = dividend.actual_cash_date
    v01_receivables = {
        item.date: item.balance_fen for item in v01.receivables
    }
    v01_cash = {
        item.date: _fen(item.cash) for item in v01.cash_ledger
    }
    v02_snapshots = {
        item.session: item for item in v02.ledger.daily_snapshots
    }
    official_dates = pair.scenario.official_dates
    ex_previous = official_dates[official_dates.index(ex_date) - 1]
    payable_previous = official_dates[
        official_dates.index(payable_date) - 1
    ]
    assert v01_cash[ex_date] == v01_cash[ex_previous]
    assert v02_snapshots[ex_date].cash_fen == (
        v02_snapshots[ex_previous].cash_fen
    )
    assert payment.cash_before_fen == (
        v02_snapshots[payable_previous].cash_fen
    )
    assert payment.cash_after_fen == (
        v02_snapshots[payable_date].cash_fen
    )
    assert v01_receivables[ex_date] == registered.amount_fen
    assert v02_snapshots[ex_date].receivable_fen == registered.amount_fen
    assert v01_cash[payable_date] - v01_cash[payable_previous] == (
        registered.amount_fen
    )
    assert (
        v02_snapshots[payable_date].cash_fen
        - v02_snapshots[payable_previous].cash_fen
        == registered.amount_fen
    )
    assert v01_receivables[payable_date] == 0
    assert v02_snapshots[payable_date].receivable_fen == 0


def test_main_board_buy_t1_and_daily_account_are_exactly_equivalent(
    tmp_path,
):
    pair = gate_d_support.run_equivalence_case(
        tmp_path,
        gate_d_support.base_stock_scenario(),
    )

    _assert_complete_economic_equivalence(pair)

    assert pair.v01.fills[0].transfer_fee_fen > 0
    assert {
        item.fee_name for item in pair.v01.touched_fee_rates
    } == {"commission", "transfer_fee"}


def test_etf_minimum_commission_is_exactly_equivalent(tmp_path):
    pair = gate_d_support.run_equivalence_case(
        tmp_path,
        gate_d_support.etf_minimum_commission_scenario(),
    )

    _assert_complete_economic_equivalence(pair)

    assert pair.v01.fills[0].size == 100
    assert pair.v01.fills[0].commission_fen == 500
    assert pair.v01.fills[0].stamp_duty_fen == 0
    assert pair.v01.fills[0].transfer_fee_fen == 0
    assert pair.v01.fills[0].total_fees_fen == 500
    assert tuple(
        item.fee_name for item in pair.v01.touched_fee_rates
    ) == ("commission",)


def test_full_weight_fee_shrink_is_exactly_equivalent(tmp_path):
    pair = gate_d_support.run_equivalence_case(
        tmp_path,
        gate_d_support.full_weight_fee_shrink_scenario(),
    )

    _assert_complete_economic_equivalence(pair)

    attempt = pair.v02.result.attempts[0]
    assert attempt.initial_candidate_size == 1_000
    assert attempt.requested_size == pair.v01.fills[0].size == 900
    assert attempt.quantity_adjustment_reason == (
        "insufficient_cash_including_fees"
    )
    assert attempt.cash_available_before_fen is not None
    assert attempt.initial_candidate_cash_required_fen is not None
    assert attempt.requested_cash_required_fen is not None
    assert attempt.initial_candidate_cash_required_fen > (
        attempt.cash_available_before_fen
    )
    assert attempt.requested_cash_required_fen <= (
        attempt.cash_available_before_fen
    )


def test_t1_uses_next_official_session_across_weekend(tmp_path):
    pair = gate_d_support.run_equivalence_case(
        tmp_path,
        gate_d_support.weekend_t1_scenario(),
    )

    _assert_complete_economic_equivalence(pair)

    assert pair.v01.lots[0].acquired_date == date(2026, 7, 17)
    assert pair.v01.lots[0].available_date == date(2026, 7, 20)
    assert (
        pair.v01.lots[0].available_date
        - pair.v01.lots[0].acquired_date
    ).days == 3


def test_trading_day_cash_dividend_is_exactly_equivalent(tmp_path):
    pair = gate_d_support.run_equivalence_case(
        tmp_path,
        gate_d_support.dividend_scenario(
            record_date=date(2026, 7, 14),
            name="normal_dividend",
        ),
    )

    _assert_complete_economic_equivalence(pair)
    _assert_normal_dividend_equivalence(pair)


def test_approved_v01_record_date_entitlement_defect_remains_explicit(
    tmp_path,
):
    pair = gate_d_support.run_equivalence_case(
        tmp_path,
        gate_d_support.dividend_scenario(
            record_date=date(2026, 7, 13),
            name="record_date_red_light",
        ),
    )

    _assert_same_verified_inputs(pair)
    _assert_signal_day_initial_state(pair)
    _assert_single_fill_equivalence(pair)

    source_event = pair.shared.corporate_actions.events[0]
    fill = pair.v01.fills[0]
    assert pair.scenario.official_dates[0] == date(2026, 7, 13)
    assert fill.execution_date == date(2026, 7, 14)
    assert source_event.record_date == date(2026, 7, 13)
    assert source_event.ex_date == date(2026, 7, 16)
    assert source_event.payable_date == date(2026, 7, 17)
    assert source_event.record_date < fill.execution_date
    assert fill.execution_date < source_event.ex_date
    assert fill.size == 100

    v01_registered = tuple(
        item
        for item in pair.v01.corporate_action_ledger
        if item.event_type == "receivable_registered"
    )
    assert len(v01_registered) == 1
    assert v01_registered[0].entitled_size == fill.size == 100
    assert v01_registered[0].amount_fen == 20_000

    assert len(pair.v02.result.dividends) == 1
    v02_dividend = pair.v02.result.dividends[0]
    assert v02_dividend.entitled_size == 0
    assert v02_dividend.amount_fen == 0
    assert pair.v02.result.ledger.receivables == ()
    assert all(
        event.event_kind is not CashEventKind.DIVIDEND_PAYMENT
        for event in pair.v02.result.ledger.cash_events
    )

    v01_final_equity_fen = _fen(pair.v01.equity_curve[-1].equity)
    v02_final_equity_fen = (
        pair.v02.result.ledger.daily_snapshots[-1].equity_fen
    )
    assert v01_final_equity_fen == 1_099_490
    assert v02_final_equity_fen == 1_079_490
    assert v01_final_equity_fen - v02_final_equity_fen == 20_000

    # This permanent regression proves the approved version boundary remains
    # visible. It is not A-E economic-equivalence evidence.
    assert v01_registered[0].amount_fen - v02_dividend.amount_fen == 20_000
