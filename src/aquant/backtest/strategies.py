"""Audited Backtrader strategies with close-signal/next-open execution."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import backtrader as bt

from aquant.backtest.models import (
    CashRecord,
    EquityRecord,
    FillRecord,
    OrderRecord,
    PositionRecord,
    ReceivableRecord,
)


class AuditedBaselineStrategy(bt.Strategy):
    """Common order and daily-ledger capture for Week 2 baselines."""

    params = (("target_weight", Decimal("0.95")), ("sma_period", None))

    def __init__(self) -> None:
        self._pending_order: bt.Order | None = None
        self._engine_ref_to_local_id: dict[int, str] = {}
        self._order_state: dict[str, dict[str, Any]] = {}
        self._fills: list[FillRecord] = []
        self._positions: list[PositionRecord] = []
        self._cash_ledger: list[CashRecord] = []
        self._equity_curve: list[EquityRecord] = []
        self._receivables: list[ReceivableRecord] = []

    def _current_date(self):
        return bt.num2date(self.data.datetime[0]).date()

    def _submit(self, side: str) -> None:
        if self._pending_order is not None:
            return
        if side == "buy":
            engine_order = self.buy(size=100)
            requested_size = 0
            target_weight = str(self.p.target_weight)
        else:
            requested_size = int(self.position.size)
            engine_order = self.sell(size=requested_size)
            target_weight = None

        local_id = f"order-{len(self._order_state) + 1:04d}"
        self._engine_ref_to_local_id[engine_order.ref] = local_id
        self._order_state[local_id] = {
            "order_id": local_id,
            "signal_date": self._current_date(),
            "target_execution_date": None,
            "side": side,
            "requested_size": requested_size,
            "final_status": "submitted",
            "rejection_reason": None,
            "target_weight": target_weight,
            "open_equity": None,
            "actual_weight": None,
        }
        engine_order.addinfo(
            local_order_id=local_id,
            signal_date=self._current_date(),
            target_weight=self.p.target_weight if side == "buy" else None,
        )
        self._pending_order = engine_order

    def notify_order(self, order: bt.Order) -> None:
        local_id = self._engine_ref_to_local_id.get(order.ref)
        if local_id is None:
            return

        status = order.getstatusname().lower()
        previous_status = self._order_state[local_id]["final_status"]
        self._order_state[local_id]["final_status"] = status
        order_info = getattr(order, "info", {})
        self._order_state[local_id]["target_execution_date"] = order_info.get(
            "target_execution_date"
        )
        self._order_state[local_id]["rejection_reason"] = order_info.get(
            "rejection_reason"
        )
        if order_info.get("requested_size") is not None:
            self._order_state[local_id]["requested_size"] = int(
                order_info["requested_size"]
            )
        for field in ("open_equity", "actual_weight"):
            if order_info.get(field) is not None:
                self._order_state[local_id][field] = str(order_info[field])
        if order.status == order.Completed and previous_status != "completed":
            side = "buy" if order.isbuy() else "sell"
            executed_size = abs(int(order.executed.size))
            executed_price = round(float(order.executed.price), 3)
            self._fills.append(
                FillRecord(
                    order_id=local_id,
                    execution_date=bt.num2date(order.executed.dt).date(),
                    side=side,
                    size=executed_size,
                    price=executed_price,
                    value=round(executed_size * executed_price, 2),
                    commission=float(order.executed.comm),
                    commission_fen=int(order_info.get("commission_fen", 0)),
                    stamp_duty_fen=int(order_info.get("stamp_duty_fen", 0)),
                    transfer_fee_fen=int(order_info.get("transfer_fee_fen", 0)),
                    total_fees_fen=int(order_info.get("total_fees_fen", 0)),
                )
            )
        if order.status in {order.Completed, order.Canceled, order.Margin, order.Rejected}:
            self._pending_order = None

    def _record_daily_ledgers(self) -> None:
        current_date = self._current_date()
        close = float(self.data.close[0])
        size = int(self.position.size)
        market_value = size * close
        if hasattr(self.broker, "position_sizes"):
            total_size, available_size, locked_size = self.broker.position_sizes(
                current_date
            )
            if total_size != size:
                raise RuntimeError("broker position and A-share lots are inconsistent")
        else:
            available_size = size
            locked_size = 0
        self._positions.append(
            PositionRecord(
                date=current_date,
                size=size,
                close=close,
                market_value=market_value,
                available_size=available_size,
                locked_size=locked_size,
            )
        )
        self._cash_ledger.append(
            CashRecord(date=current_date, cash=float(self.broker.getcash()))
        )
        self._equity_curve.append(
            EquityRecord(date=current_date, equity=float(self.broker.getvalue()))
        )
        balance_fen = (
            self.broker.receivable_balance_fen()
            if hasattr(self.broker, "receivable_balance_fen")
            else 0
        )
        self._receivables.append(
            ReceivableRecord(
                date=current_date,
                balance=balance_fen / 100.0,
                balance_fen=balance_fen,
            )
        )

    def _apply_signal(self) -> None:
        raise NotImplementedError

    def next(self) -> None:
        self._apply_signal()
        self._record_daily_ledgers()

    def audited_orders(self) -> tuple[OrderRecord, ...]:
        return tuple(OrderRecord(**values) for values in self._order_state.values())

    def audited_fills(self) -> tuple[FillRecord, ...]:
        return tuple(self._fills)

    def audited_positions(self) -> tuple[PositionRecord, ...]:
        return tuple(self._positions)

    def audited_cash(self) -> tuple[CashRecord, ...]:
        return tuple(self._cash_ledger)

    def audited_equity(self) -> tuple[EquityRecord, ...]:
        return tuple(self._equity_curve)

    def audited_receivables(self) -> tuple[ReceivableRecord, ...]:
        return tuple(self._receivables)


class BuyAndHoldStrategy(AuditedBaselineStrategy):
    """Submit one target-weight purchase after the first observed close."""

    def _apply_signal(self) -> None:
        if len(self) == 1 and not self.position:
            self._submit("buy")


class SmaStrategy(AuditedBaselineStrategy):
    """Hold when the close is above its one-parameter simple moving average."""

    def _apply_signal(self) -> None:
        period = int(self.p.sma_period)
        if len(self) < period or self._pending_order is not None:
            return
        indicator = (
            self.data.indicator_close
            if hasattr(self.data, "indicator_close")
            else self.data.close
        )
        closes = [float(indicator[-offset]) for offset in range(period)]
        sma = sum(closes) / period
        close = float(indicator[0])
        if not self.position and close > sma:
            self._submit("buy")
        elif self.position and close < sma:
            self._submit("sell")
