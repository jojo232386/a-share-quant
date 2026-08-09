"""Immutable accounting for a rolling portfolio with isolated SELL support."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from aquant.portfolio import (
    BuyPosting,
    CashEventKind,
    CashLedgerEvent,
    CashReceivable,
    DailyAccountSnapshot,
    PortfolioError,
    PortfolioLedger,
    SymbolValuation,
    cash_after_fill,
    notional_fen,
    post_buy,
    verify_portfolio_ledger,
)
from aquant.rules import (
    FeeBreakdown,
    OrderSide,
    PositionLot,
    consume_fifo,
    sellable_size,
    validate_sell_size,
)

ROLLING_ACCOUNTING_SCHEMA_VERSION = "1.0.0"

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SYMBOL_RE = re.compile(r"[0-9]{6}")


@dataclass(frozen=True)
class LotConsumption:
    """One exact quantity consumed from an original BUY lot."""

    lot_id: str
    size: int


@dataclass(frozen=True)
class SellPosting:
    """One already-authorized SELL fill ready for rolling posting."""

    event_id: str
    execution_date: date
    symbol: str
    size: int
    unit_price: Decimal
    fees: FeeBreakdown


@dataclass(frozen=True)
class SellFillEvent:
    """One replayable rolling SELL cash and FIFO transition."""

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

    @property
    def total_fees_fen(self) -> int:
        return self.commission_fen + self.stamp_duty_fen + self.transfer_fee_fen


@dataclass(frozen=True)
class RollingPortfolioLedger:
    """Current immutable rolling state, separate from the legacy schema."""

    initial_cash_fen: int
    cash_fen: int
    lots: tuple[PositionLot, ...] = ()
    cash_events: tuple[CashLedgerEvent | SellFillEvent, ...] = ()
    receivables: tuple[CashReceivable, ...] = ()
    daily_snapshots: tuple[DailyAccountSnapshot, ...] = ()


def promote_portfolio_ledger(ledger: PortfolioLedger) -> RollingPortfolioLedger:
    """Copy one exact verified legacy ledger into the rolling sibling schema."""
    if type(ledger) is not PortfolioLedger:
        raise TypeError("ledger must be an exact PortfolioLedger")
    verify_portfolio_ledger(ledger)
    return RollingPortfolioLedger(
        initial_cash_fen=ledger.initial_cash_fen,
        cash_fen=ledger.cash_fen,
        lots=ledger.lots,
        cash_events=ledger.cash_events,
        receivables=ledger.receivables,
        daily_snapshots=ledger.daily_snapshots,
    )


def create_rolling_ledger(initial_cash_fen: int) -> RollingPortfolioLedger:
    """Create a rolling ledger through the verified pristine legacy path."""
    from aquant.portfolio import create_portfolio_ledger

    legacy = create_portfolio_ledger(initial_cash_fen)
    verify_portfolio_ledger(legacy)
    return promote_portfolio_ledger(legacy)


def post_rolling_buy(
    ledger: RollingPortfolioLedger,
    posting: BuyPosting,
) -> RollingPortfolioLedger:
    """Validate one BUY through legacy posting and append its exact lot/event."""
    verify_rolling_ledger(ledger)
    temporary = PortfolioLedger(
        initial_cash_fen=ledger.cash_fen,
        cash_fen=ledger.cash_fen,
    )
    posted = post_buy(temporary, posting)
    updated = RollingPortfolioLedger(
        initial_cash_fen=ledger.initial_cash_fen,
        cash_fen=posted.cash_fen,
        lots=ledger.lots + posted.lots,
        cash_events=ledger.cash_events + posted.cash_events,
        receivables=ledger.receivables,
        daily_snapshots=ledger.daily_snapshots,
    )
    verify_rolling_ledger(updated)
    return updated


def post_rolling_sell(
    ledger: RollingPortfolioLedger,
    posting: SellPosting,
) -> RollingPortfolioLedger:
    """Post one SELL with same-symbol FIFO consumption and exact shared cash."""
    verify_rolling_ledger(ledger)
    if type(posting) is not SellPosting:
        raise TypeError("posting must be an exact SellPosting")
    if (
        type(posting.event_id) is not str
        or _IDENTIFIER_RE.fullmatch(posting.event_id) is None
        or type(posting.execution_date) is not date
        or type(posting.symbol) is not str
        or _SYMBOL_RE.fullmatch(posting.symbol) is None
        or type(posting.size) is not int
        or posting.size <= 0
        or type(posting.unit_price) is not Decimal
        or not posting.unit_price.is_finite()
        or posting.unit_price <= 0
    ):
        raise PortfolioError(
            "invalid_sell_posting",
            "sell posting fields are invalid",
        )
    value_fen = notional_fen(posting.unit_price, posting.size)
    cash_after = cash_after_fill(
        cash_before_fen=ledger.cash_fen,
        side=OrderSide.SELL,
        notional_amount_fen=value_fen,
        fees=posting.fees,
    )
    if posting.event_id in {event.event_id for event in ledger.cash_events}:
        raise PortfolioError("duplicate_event", "sell event ID already exists")
    if ledger.cash_events and posting.execution_date < ledger.cash_events[-1].session:
        raise PortfolioError("invalid_event_order", "cash event dates are out of order")

    symbol_lots = tuple(lot for lot in ledger.lots if lot.symbol == posting.symbol)
    total_size = sum(lot.remaining_size for lot in symbol_lots)
    available_size = sellable_size(symbol_lots, posting.execution_date)
    if posting.size > available_size:
        raise PortfolioError(
            "insufficient_sellable_size",
            "sell size exceeds same-symbol sellable position",
        )
    if not validate_sell_size(total_size, posting.size):
        raise PortfolioError("invalid_sell_size", "sell size violates lot policy")

    consumed_lots = consume_fifo(
        symbol_lots,
        execution_date=posting.execution_date,
        requested_size=posting.size,
    )
    consumptions = tuple(
        LotConsumption(
            lot_id=before.lot_id,
            size=before.remaining_size - after.remaining_size,
        )
        for before, after in zip(symbol_lots, consumed_lots, strict=True)
        if before.remaining_size != after.remaining_size
    )
    replacements = iter(consumed_lots)
    updated_lots = tuple(
        next(replacements) if lot.symbol == posting.symbol else lot for lot in ledger.lots
    )
    event = SellFillEvent(
        event_id=posting.event_id,
        session=posting.execution_date,
        symbol=posting.symbol,
        size=posting.size,
        unit_price=posting.unit_price,
        notional_fen=value_fen,
        commission_fen=posting.fees.commission_fen,
        stamp_duty_fen=posting.fees.stamp_duty_fen,
        transfer_fee_fen=posting.fees.transfer_fee_fen,
        cash_before_fen=ledger.cash_fen,
        cash_after_fen=cash_after,
        consumptions=consumptions,
    )
    updated = RollingPortfolioLedger(
        initial_cash_fen=ledger.initial_cash_fen,
        cash_fen=cash_after,
        lots=updated_lots,
        cash_events=ledger.cash_events + (event,),
        receivables=ledger.receivables,
        daily_snapshots=ledger.daily_snapshots,
    )
    verify_rolling_ledger(updated)
    return updated


def _verify_rolling_lot(lot: object) -> PositionLot:
    if (
        type(lot) is not PositionLot
        or type(lot.lot_id) is not str
        or _IDENTIFIER_RE.fullmatch(lot.lot_id) is None
        or type(lot.symbol) is not str
        or _SYMBOL_RE.fullmatch(lot.symbol) is None
        or type(lot.acquired_date) is not date
        or type(lot.available_date) is not date
        or lot.available_date <= lot.acquired_date
        or type(lot.original_size) is not int
        or lot.original_size <= 0
        or lot.original_size % 100 != 0
        or type(lot.remaining_size) is not int
        or not 0 <= lot.remaining_size <= lot.original_size
        or type(lot.unit_cost) is not Decimal
        or not lot.unit_cost.is_finite()
        or lot.unit_cost <= 0
    ):
        raise PortfolioError("invalid_lot", "rolling position lot is invalid")
    return lot


def _event_fees(
    *,
    commission_fen: int,
    stamp_duty_fen: int,
    transfer_fee_fen: int,
) -> FeeBreakdown:
    return FeeBreakdown(
        commission_fen=commission_fen,
        stamp_duty_fen=stamp_duty_fen,
        transfer_fee_fen=transfer_fee_fen,
        touched_rates=(),
    )


def _verify_receivable(value: object) -> CashReceivable:
    if (
        type(value) is not CashReceivable
        or type(value.event_id) is not str
        or _IDENTIFIER_RE.fullmatch(value.event_id) is None
        or type(value.symbol) is not str
        or _SYMBOL_RE.fullmatch(value.symbol) is None
        or type(value.registered_date) is not date
        or type(value.source_payable_date) is not date
        or type(value.actual_cash_date) is not date
        or value.registered_date > value.source_payable_date
        or value.actual_cash_date < value.source_payable_date
        or type(value.amount_fen) is not int
        or value.amount_fen <= 0
        or value.paid_date is not None
        and (type(value.paid_date) is not date or value.paid_date != value.actual_cash_date)
    ):
        raise PortfolioError("invalid_receivable", "cash receivable is invalid")
    return value


def _outstanding_receivable_fen(
    receivables: tuple[CashReceivable, ...],
    session: date,
) -> int:
    return sum(
        item.amount_fen
        for item in receivables
        if item.registered_date <= session and (item.paid_date is None or item.paid_date > session)
    )


def _remaining_sizes_at_session(
    ledger: RollingPortfolioLedger,
    session: date,
) -> dict[str, int]:
    remaining = {
        lot.lot_id: lot.original_size for lot in ledger.lots if lot.acquired_date <= session
    }
    for event in ledger.cash_events:
        if event.session > session:
            break
        if type(event) is SellFillEvent:
            for consumption in event.consumptions:
                if consumption.lot_id in remaining:
                    remaining[consumption.lot_id] -= consumption.size
    return remaining


def _position_sizes_at_session(
    ledger: RollingPortfolioLedger,
    session: date,
) -> dict[str, tuple[int, int, int]]:
    remaining = _remaining_sizes_at_session(ledger, session)
    values: dict[str, list[int]] = {}
    for lot in ledger.lots:
        size = remaining.get(lot.lot_id, 0)
        if size == 0:
            continue
        current = values.setdefault(lot.symbol, [0, 0, 0])
        current[0] += size
        if lot.available_date <= session:
            current[1] += size
        else:
            current[2] += size
    return {symbol: tuple(sizes) for symbol, sizes in values.items()}


def _cash_at_session(ledger: RollingPortfolioLedger, session: date) -> int:
    cash = ledger.initial_cash_fen
    for event in ledger.cash_events:
        if event.session > session:
            break
        cash = event.cash_after_fen
    return cash


def _verify_valuation(value: object) -> SymbolValuation:
    if (
        type(value) is not SymbolValuation
        or type(value.symbol) is not str
        or _SYMBOL_RE.fullmatch(value.symbol) is None
        or type(value.total_size) is not int
        or value.total_size < 0
        or type(value.available_size) is not int
        or value.available_size < 0
        or type(value.locked_size) is not int
        or value.locked_size < 0
        or value.available_size + value.locked_size != value.total_size
        or type(value.mark_price) is not Decimal
        or not value.mark_price.is_finite()
        or value.mark_price <= 0
    ):
        raise PortfolioError("invalid_valuation", "symbol valuation is invalid")
    expected = 0 if value.total_size == 0 else notional_fen(value.mark_price, value.total_size)
    if type(value.market_value_fen) is not int or value.market_value_fen != expected:
        raise PortfolioError("invalid_valuation", "market value is invalid")
    return value


def verify_rolling_ledger(ledger: RollingPortfolioLedger) -> None:
    """Replay original BUY quantities, SELL FIFO transitions, and shared cash."""
    if (
        type(ledger) is not RollingPortfolioLedger
        or type(ledger.initial_cash_fen) is not int
        or ledger.initial_cash_fen <= 0
        or type(ledger.cash_fen) is not int
        or ledger.cash_fen < 0
        or type(ledger.lots) is not tuple
        or type(ledger.cash_events) is not tuple
        or type(ledger.receivables) is not tuple
        or type(ledger.daily_snapshots) is not tuple
    ):
        raise PortfolioError("invalid_ledger", "rolling portfolio ledger is invalid")

    checked_lots = tuple(_verify_rolling_lot(lot) for lot in ledger.lots)
    lot_ids = tuple(lot.lot_id for lot in checked_lots)
    if len(lot_ids) != len(set(lot_ids)):
        raise PortfolioError("duplicate_lot", "position lot IDs are duplicated")
    lots_by_id = {lot.lot_id: lot for lot in checked_lots}
    remaining = {lot.lot_id: lot.original_size for lot in checked_lots}
    buy_lot_ids: set[str] = set()
    canonical_lot_ids: list[str] = []
    event_ids: set[str] = set()
    dividend_reference_ids: set[str] = set()
    dividend_events: dict[str, CashLedgerEvent] = {}
    running_cash = ledger.initial_cash_fen
    previous_session: date | None = None

    for event in ledger.cash_events:
        if type(event) not in (CashLedgerEvent, SellFillEvent):
            raise PortfolioError("invalid_cash_event", "rolling cash event is invalid")
        if (
            type(event.event_id) is not str
            or _IDENTIFIER_RE.fullmatch(event.event_id) is None
            or event.event_id in event_ids
            or type(event.session) is not date
            or previous_session is not None
            and event.session < previous_session
            or type(event.cash_before_fen) is not int
            or type(event.cash_after_fen) is not int
            or event.cash_before_fen < 0
            or event.cash_after_fen < 0
        ):
            raise PortfolioError("invalid_cash_event", "rolling cash event is invalid")
        event_ids.add(event.event_id)
        previous_session = event.session
        if event.cash_before_fen != running_cash:
            raise PortfolioError(
                "cash_reconciliation_failed",
                "cash event opening cash is wrong",
            )

        if type(event) is CashLedgerEvent:
            if event.event_kind is CashEventKind.DIVIDEND_PAYMENT:
                if (
                    event.side is not None
                    or type(event.symbol) is not str
                    or _SYMBOL_RE.fullmatch(event.symbol) is None
                    or type(event.reference_id) is not str
                    or _IDENTIFIER_RE.fullmatch(event.reference_id) is None
                    or event.reference_id in dividend_reference_ids
                    or type(event.notional_fen) is not int
                    or event.notional_fen <= 0
                    or event.commission_fen != 0
                    or event.stamp_duty_fen != 0
                    or event.transfer_fee_fen != 0
                ):
                    raise PortfolioError(
                        "invalid_cash_event",
                        "dividend cash event is invalid",
                    )
                dividend_reference_ids.add(event.reference_id)
                dividend_events[event.reference_id] = event
                expected_cash = running_cash + event.notional_fen
            elif event.event_kind is CashEventKind.FILL and event.side is OrderSide.BUY:
                lot = lots_by_id.get(event.reference_id)
                if (
                    lot is None
                    or event.reference_id in buy_lot_ids
                    or event.symbol != lot.symbol
                    or event.session != lot.acquired_date
                    or event.notional_fen != notional_fen(lot.unit_cost, lot.original_size)
                ):
                    raise PortfolioError(
                        "lot_reconciliation_failed",
                        "buy cash event does not match its original position lot",
                    )
                buy_lot_ids.add(event.reference_id)
                canonical_lot_ids.append(event.reference_id)
                expected_cash = cash_after_fill(
                    cash_before_fen=running_cash,
                    side=OrderSide.BUY,
                    notional_amount_fen=event.notional_fen,
                    fees=_event_fees(
                        commission_fen=event.commission_fen,
                        stamp_duty_fen=event.stamp_duty_fen,
                        transfer_fee_fen=event.transfer_fee_fen,
                    ),
                )
            else:
                raise PortfolioError(
                    "invalid_cash_event",
                    "legacy event in rolling ledger is unsupported",
                )
        else:
            current_lots = tuple(
                replace(
                    lots_by_id[lot_id],
                    remaining_size=remaining[lot_id],
                )
                for lot_id in canonical_lot_ids
                if lots_by_id[lot_id].symbol == event.symbol
            )
            total_size = sum(lot.remaining_size for lot in current_lots)
            if (
                type(event.symbol) is not str
                or _SYMBOL_RE.fullmatch(event.symbol) is None
                or type(event.size) is not int
                or event.size <= 0
                or type(event.unit_price) is not Decimal
                or not event.unit_price.is_finite()
                or event.unit_price <= 0
                or type(event.consumptions) is not tuple
                or event.size > sellable_size(current_lots, event.session)
                or not validate_sell_size(total_size, event.size)
            ):
                raise PortfolioError("invalid_sell_event", "sell event is invalid")
            if (
                any(
                    type(consumption) is not LotConsumption
                    or type(consumption.lot_id) is not str
                    or _IDENTIFIER_RE.fullmatch(consumption.lot_id) is None
                    or type(consumption.size) is not int
                    or consumption.size <= 0
                    for consumption in event.consumptions
                )
                or sum(consumption.size for consumption in event.consumptions) != event.size
            ):
                raise PortfolioError(
                    "invalid_sell_event",
                    "sell consumptions are invalid",
                )
            replayed = consume_fifo(
                current_lots,
                execution_date=event.session,
                requested_size=event.size,
            )
            expected_consumptions = tuple(
                LotConsumption(
                    before.lot_id,
                    before.remaining_size - after.remaining_size,
                )
                for before, after in zip(current_lots, replayed, strict=True)
                if before.remaining_size != after.remaining_size
            )
            if event.consumptions != expected_consumptions:
                raise PortfolioError(
                    "lot_reconciliation_failed",
                    "sell consumptions do not replay through FIFO",
                )
            for lot in replayed:
                remaining[lot.lot_id] = lot.remaining_size
            expected_notional = notional_fen(event.unit_price, event.size)
            if event.notional_fen != expected_notional:
                raise PortfolioError(
                    "cash_reconciliation_failed",
                    "sell notional does not replay",
                )
            expected_cash = cash_after_fill(
                cash_before_fen=running_cash,
                side=OrderSide.SELL,
                notional_amount_fen=expected_notional,
                fees=_event_fees(
                    commission_fen=event.commission_fen,
                    stamp_duty_fen=event.stamp_duty_fen,
                    transfer_fee_fen=event.transfer_fee_fen,
                ),
            )
        if event.cash_after_fen != expected_cash:
            raise PortfolioError(
                "cash_reconciliation_failed",
                "cash event closing cash is wrong",
            )
        running_cash = expected_cash

    if running_cash != ledger.cash_fen:
        raise PortfolioError("cash_reconciliation_failed", "ledger cash does not replay")
    if tuple(canonical_lot_ids) != lot_ids:
        raise PortfolioError(
            "lot_reconciliation_failed",
            "ledger lots do not match canonical BUY event order",
        )
    if any(lot.remaining_size != remaining[lot.lot_id] for lot in checked_lots):
        raise PortfolioError(
            "lot_reconciliation_failed",
            "terminal lot quantities do not replay",
        )

    receivable_ids: set[str] = set()
    paid_receivable_ids: set[str] = set()
    for item in ledger.receivables:
        checked = _verify_receivable(item)
        if checked.event_id in receivable_ids:
            raise PortfolioError(
                "duplicate_receivable",
                "cash receivable event IDs are duplicated",
            )
        receivable_ids.add(checked.event_id)
        if checked.paid_date is not None:
            paid_receivable_ids.add(checked.event_id)
            event = dividend_events.get(checked.event_id)
            if (
                event is None
                or event.symbol != checked.symbol
                or event.session != checked.actual_cash_date
                or event.session != checked.paid_date
                or event.notional_fen != checked.amount_fen
            ):
                raise PortfolioError(
                    "receivable_reconciliation_failed",
                    "dividend cash event does not match its receivable",
                )
    if dividend_reference_ids != paid_receivable_ids:
        raise PortfolioError(
            "receivable_reconciliation_failed",
            "paid receivables and dividend events do not match",
        )

    previous_snapshot: date | None = None
    for snapshot in ledger.daily_snapshots:
        if (
            type(snapshot) is not DailyAccountSnapshot
            or type(snapshot.session) is not date
            or previous_snapshot is not None
            and snapshot.session <= previous_snapshot
            or type(snapshot.valuations) is not tuple
            or type(snapshot.cash_fen) is not int
            or snapshot.cash_fen < 0
            or type(snapshot.position_market_value_fen) is not int
            or snapshot.position_market_value_fen < 0
            or type(snapshot.receivable_fen) is not int
            or snapshot.receivable_fen < 0
            or type(snapshot.equity_fen) is not int
            or snapshot.equity_fen < 0
        ):
            raise PortfolioError("invalid_snapshot", "daily snapshot is invalid")
        previous_snapshot = snapshot.session
        if any(
            item.paid_date is None and item.actual_cash_date <= snapshot.session
            for item in ledger.receivables
        ):
            raise PortfolioError(
                "overdue_receivable",
                "daily close contains an unpaid due receivable",
            )
        symbols = tuple(item.symbol for item in snapshot.valuations)
        if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
            raise PortfolioError(
                "invalid_valuation_order",
                "valuations are not unique and sorted",
            )
        for valuation in snapshot.valuations:
            _verify_valuation(valuation)
        expected_sizes = _position_sizes_at_session(ledger, snapshot.session)
        actual_sizes = {
            item.symbol: (
                item.total_size,
                item.available_size,
                item.locked_size,
            )
            for item in snapshot.valuations
        }
        if actual_sizes != expected_sizes:
            raise PortfolioError(
                "position_reconciliation_failed",
                "snapshot positions do not replay at its session",
            )
        expected_cash = _cash_at_session(ledger, snapshot.session)
        expected_market = sum(item.market_value_fen for item in snapshot.valuations)
        expected_receivable = _outstanding_receivable_fen(
            ledger.receivables,
            snapshot.session,
        )
        expected_equity = expected_cash + expected_market + expected_receivable
        if (
            snapshot.cash_fen != expected_cash
            or snapshot.position_market_value_fen != expected_market
            or snapshot.receivable_fen != expected_receivable
            or snapshot.equity_fen != expected_equity
        ):
            raise PortfolioError(
                "daily_accounting_identity_failed",
                "daily cash plus market value plus receivables does not equal equity",
            )


def close_rolling_session(
    ledger: RollingPortfolioLedger,
    session: date,
    valuations: tuple[SymbolValuation, ...],
) -> RollingPortfolioLedger:
    """Append one close reconstructed from dated rolling events."""
    verify_rolling_ledger(ledger)
    if type(session) is not date:
        raise PortfolioError("invalid_session", "valuation session is invalid")
    if type(valuations) is not tuple or any(
        type(item) is not SymbolValuation for item in valuations
    ):
        raise TypeError("valuations must be an exact tuple of SymbolValuation")
    if ledger.daily_snapshots and session <= ledger.daily_snapshots[-1].session:
        raise PortfolioError(
            "invalid_snapshot_order",
            "daily snapshot sessions must strictly increase",
        )
    if ledger.cash_events and ledger.cash_events[-1].session > session:
        raise PortfolioError(
            "invalid_snapshot_order",
            "cannot close a session before an existing cash event",
        )
    if any(
        item.paid_date is None and item.actual_cash_date <= session for item in ledger.receivables
    ):
        raise PortfolioError(
            "overdue_receivable",
            "due receivables must be paid before daily close",
        )
    symbols = tuple(item.symbol for item in valuations)
    if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
        raise PortfolioError(
            "invalid_valuation_order",
            "valuations must be unique and sorted by symbol",
        )
    for valuation in valuations:
        _verify_valuation(valuation)
    expected_positions = _position_sizes_at_session(ledger, session)
    actual_positions = {
        item.symbol: (
            item.total_size,
            item.available_size,
            item.locked_size,
        )
        for item in valuations
    }
    if set(actual_positions) != set(expected_positions):
        raise PortfolioError(
            "position_reconciliation_failed",
            "valuations must cover all and only current positions",
        )
    if actual_positions != expected_positions:
        raise PortfolioError(
            "position_size_mismatch",
            "valuation sizes do not match replayed lots",
        )
    market_value = sum(item.market_value_fen for item in valuations)
    receivable_fen = _outstanding_receivable_fen(ledger.receivables, session)
    cash_fen = _cash_at_session(ledger, session)
    snapshot = DailyAccountSnapshot(
        session=session,
        cash_fen=cash_fen,
        position_market_value_fen=market_value,
        receivable_fen=receivable_fen,
        equity_fen=cash_fen + market_value + receivable_fen,
        valuations=valuations,
    )
    updated = replace(
        ledger,
        daily_snapshots=ledger.daily_snapshots + (snapshot,),
    )
    verify_rolling_ledger(updated)
    return updated
