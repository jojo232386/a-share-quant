"""Backtrader broker hook enforcing the pure A-share rules at the real open."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import backtrader as bt

from aquant.backtest.models import CorporateActionRecord
from aquant.data.calendar_snapshot import VerifiedTradingCalendar
from aquant.data.corporate_actions import verify_verified_corporate_actions
from aquant.rules import (
    InstrumentKind,
    InstrumentRule,
    OrderIntent,
    OrderSide,
    PositionLot,
    RejectionReason,
    VerifiedFeePolicy,
    consume_fifo,
    create_buy_lot,
    evaluate_order,
    sellable_size,
)
from aquant.rules.models import FeeBreakdown, FeeRateTouch


@dataclass(frozen=True)
class _PendingReceivable:
    event_id: str
    payable_date: date
    entitled_size: int
    cash_dividend_per_unit: Decimal
    amount_fen: int


class RuleAwareCommissionInfo(bt.CommInfoBase):
    """Expose one already-calculated exact fee to Backtrader's cash engine."""

    params = (("stocklike", True),)

    def __init__(self) -> None:
        super().__init__()
        self.active_fees: FeeBreakdown | None = None

    def _getcommission(self, size, price, pseudoexec):
        if self.active_fees is None:
            return 0.0
        return self.active_fees.total_fees_fen / 100.0


class RuleAwareBackBroker(bt.brokers.BackBroker):
    """Reject unsafe daily-bar executions before Backtrader can fill them."""

    params = (
        ("verified_calendar", None),
        ("verified_fee_policy", None),
        ("instrument_symbol", None),
        ("instrument_kind", None),
        ("available_bar_dates", None),
        ("verified_corporate_actions", None),
    )

    def __init__(self) -> None:
        super().__init__()
        if type(self.p.verified_calendar) is not VerifiedTradingCalendar:
            raise TypeError("verified calendar is required")
        if type(self.p.verified_fee_policy) is not VerifiedFeePolicy:
            raise TypeError("verified fee policy is required")
        if type(self.p.instrument_kind) is not InstrumentKind:
            raise TypeError("instrument kind is required")
        self._calendar = self.p.verified_calendar
        self._fee_policy = self.p.verified_fee_policy
        self._instrument = InstrumentRule(
            self.p.instrument_symbol,
            self.p.instrument_kind,
        )
        self._available_bar_dates = frozenset(self.p.available_bar_dates)
        self._actions = self.p.verified_corporate_actions
        if self._actions is not None:
            verify_verified_corporate_actions(self._actions)
            action_provenance = self._actions.provenance
            assert action_provenance is not None
            if (
                action_provenance.symbol != self._instrument.symbol
                or action_provenance.instrument_kind is not self._instrument.kind
            ):
                raise TypeError("corporate actions do not match the instrument")
        self._lots: tuple[PositionLot, ...] = ()
        self._next_lot_number = 1
        self._touched_rates: list[FeeRateTouch] = []
        self._rule_commission = RuleAwareCommissionInfo()
        self.addcommissioninfo(self._rule_commission)
        self._market_data = None
        self._pending_receivables: tuple[_PendingReceivable, ...] = ()
        self._corporate_action_ledger: list[CorporateActionRecord] = []
        self._processed_action_dates: set[date] = set()

    @staticmethod
    def _fen(value: float) -> int:
        return int(
            Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            * 100
        )

    def _reject(self, order, reason: RejectionReason, target: date | None) -> None:
        order.addinfo(
            target_execution_date=target,
            rejection_reason=reason.value,
        )
        if order.reject(self):
            self.notify(order)
            self._ococheck(order)
            self._bracketize(order, cancel=True)

    def bind_market_data(self, data) -> None:
        if self._market_data is not None:
            raise RuntimeError("market data is already bound")
        self._market_data = data

    def receivable_balance_fen(self) -> int:
        return sum(item.amount_fen for item in self._pending_receivables)

    def getvalue(self, datas=None, mkt=False, lever=False):
        value = super().getvalue(datas=datas, mkt=mkt, lever=lever)
        if datas is None:
            value += self.receivable_balance_fen() / 100.0
        return value

    def next(self) -> None:
        if self._market_data is None:
            raise RuntimeError("market data must be bound before execution")
        current_date = bt.num2date(self._market_data.datetime[0]).date()
        if current_date in self._processed_action_dates:
            raise RuntimeError("corporate actions were processed twice for one date")
        self._processed_action_dates.add(current_date)

        events = (
            tuple(
                event
                for event in self._actions.events
                if event.ex_date == current_date
            )
            if self._actions is not None
            else ()
        )
        entitled_size = int(self.getposition(self._market_data).size)
        for event in events:
            if entitled_size <= 0 or event.cash_dividend_per_unit == 0:
                continue
            amount_fen = int(
                (
                    event.cash_dividend_per_unit * entitled_size
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                * 100
            )
            receivable = _PendingReceivable(
                event_id=event.event_id,
                payable_date=event.payable_date,
                entitled_size=entitled_size,
                cash_dividend_per_unit=event.cash_dividend_per_unit,
                amount_fen=amount_fen,
            )
            self._pending_receivables = (*self._pending_receivables, receivable)
            self._corporate_action_ledger.append(
                CorporateActionRecord(
                    date=current_date,
                    event_id=event.event_id,
                    event_type="receivable_registered",
                    entitled_size=entitled_size,
                    cash_dividend_per_unit=str(event.cash_dividend_per_unit),
                    amount_fen=amount_fen,
                )
            )

        due = tuple(
            item
            for item in self._pending_receivables
            if item.payable_date == current_date
        )
        if due:
            self.cash += sum(item.amount_fen for item in due) / 100.0
            self._pending_receivables = tuple(
                item
                for item in self._pending_receivables
                if item.payable_date != current_date
            )
            self._corporate_action_ledger.extend(
                CorporateActionRecord(
                    date=current_date,
                    event_id=item.event_id,
                    event_type="cash_paid",
                    entitled_size=item.entitled_size,
                    cash_dividend_per_unit=str(item.cash_dividend_per_unit),
                    amount_fen=item.amount_fen,
                )
                for item in due
            )
        super().next()

    def _try_exec_market(self, order, popen, phigh, plow):
        signal_date = order.info.get("signal_date")
        if type(signal_date) is not date:
            self._reject(order, RejectionReason.MISSING_CALENDAR_COVERAGE, None)
            return
        target = self._calendar.next_session(signal_date)
        if target is None:
            self._reject(order, RejectionReason.NO_NEXT_SESSION_IN_RANGE, None)
            return
        current_date = bt.num2date(order.data.datetime[0]).date()
        order.addinfo(target_execution_date=target)
        if current_date < target:
            return
        if current_date > target:
            self._reject(order, RejectionReason.SUSPENDED_NO_BAR, target)
            return
        side = OrderSide.BUY if order.isbuy() else OrderSide.SELL
        reference_line = getattr(order.data, "reference_price", None)
        previous_close = Decimal(
            str(
                float(
                    reference_line[0]
                    if reference_line is not None
                    else order.data.close[-1]
                )
            )
        )
        execution_open = Decimal(str(float(popen)))
        requested_size = abs(int(order.created.size))
        open_equity = None
        if side is OrderSide.BUY:
            target_weight = order.info.get("target_weight")
            if (
                type(target_weight) is not Decimal
                or not target_weight.is_finite()
                or not Decimal("0") < target_weight <= Decimal("1")
            ):
                order.addinfo(requested_size=0)
                self._reject(order, RejectionReason.INVALID_LOT_SIZE, target)
                return
            current_size = int(self.getposition(order.data).size)
            open_equity = (
                Decimal(str(self.getcash()))
                + Decimal(self.receivable_balance_fen()) / Decimal("100")
                + Decimal(current_size) * execution_open
            )
            target_size = (
                int(open_equity * target_weight / execution_open) // 100 * 100
            )
            requested_size = max(target_size - current_size, 0)
            if requested_size == 0:
                order.addinfo(requested_size=0)
                self._reject(order, RejectionReason.INVALID_LOT_SIZE, target)
                return

        decision = None
        candidate_size = requested_size
        while candidate_size > 0:
            decision = evaluate_order(
                intent=OrderIntent(
                    order.info.get("local_order_id"),
                    self._instrument.symbol,
                    signal_date,
                    side,
                    candidate_size,
                ),
                instrument=self._instrument,
                calendar=self._calendar,
                available_bar_dates=self._available_bar_dates,
                previous_close=previous_close,
                execution_open=execution_open,
                cash_fen=self._fen(self.getcash()),
                lots=self._lots,
                fee_policy=self._fee_policy,
            )
            if decision.allowed or decision.reason is not RejectionReason.INSUFFICIENT_CASH:
                break
            if side is not OrderSide.BUY:
                break
            candidate_size -= 100
        assert decision is not None
        if not decision.allowed:
            assert decision.reason is not None
            order.addinfo(requested_size=0)
            self._reject(order, decision.reason, decision.target_execution_date)
            return
        requested_size = candidate_size
        order_info = {"requested_size": requested_size}
        if side is OrderSide.BUY:
            assert open_equity is not None
            order_info.update(
                open_equity=str(open_equity),
                actual_weight=str(
                    execution_open * requested_size / open_equity
                ),
            )
        order.addinfo(**order_info)
        if side is OrderSide.BUY:
            order.size = requested_size
            order.created.size = requested_size
            order.executed.remsize = requested_size
        assert decision.fees is not None
        fees = decision.fees
        for touch in fees.touched_rates:
            if touch not in self._touched_rates:
                self._touched_rates.append(touch)
        order.addinfo(
            commission_fen=fees.commission_fen,
            stamp_duty_fen=fees.stamp_duty_fen,
            transfer_fee_fen=fees.transfer_fee_fen,
            total_fees_fen=fees.total_fees_fen,
        )
        self._rule_commission.active_fees = fees
        try:
            super()._try_exec_market(order, popen, phigh, plow)
        finally:
            self._rule_commission.active_fees = None
        if order.status != order.Completed:
            return
        executed_size = abs(int(order.executed.size))
        if order.isbuy():
            lot = create_buy_lot(
                lot_id=f"lot-{self._next_lot_number:04d}",
                symbol=self._instrument.symbol,
                acquired_date=current_date,
                size=executed_size,
                unit_cost=Decimal(str(float(order.executed.price))),
                calendar=self._calendar,
            )
            self._lots = (*self._lots, lot)
            self._next_lot_number += 1
        else:
            self._lots = consume_fifo(
                self._lots,
                execution_date=current_date,
                requested_size=executed_size,
            )

    def audited_lots(self) -> tuple[PositionLot, ...]:
        return self._lots

    def audited_touched_rates(self) -> tuple[FeeRateTouch, ...]:
        return tuple(self._touched_rates)

    def audited_corporate_actions(self) -> tuple[CorporateActionRecord, ...]:
        return tuple(self._corporate_action_ledger)

    def position_sizes(self, execution_date: date) -> tuple[int, int, int]:
        total = sum(lot.remaining_size for lot in self._lots)
        available = sellable_size(self._lots, execution_date)
        return total, available, total - available

    def finalize_pending_orders(self) -> tuple[bt.Order, ...]:
        """Close end-of-data orders so exported ledgers have no pending state."""
        rejected: list[bt.Order] = []
        for order in tuple(self.pending):
            try:
                self.pending.remove(order)
            except ValueError:
                continue
            signal_date = order.info.get("signal_date")
            target = (
                self._calendar.next_session(signal_date)
                if type(signal_date) is date
                else None
            )
            reason = (
                RejectionReason.NO_NEXT_SESSION_IN_RANGE
                if target is None
                else RejectionReason.SUSPENDED_NO_BAR
            )
            self._reject(order, reason, target)
            rejected.append(order)
        return tuple(rejected)
