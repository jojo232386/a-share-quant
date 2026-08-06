"""Exact immutable shared-cash accounting primitives."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from enum import StrEnum

from aquant.portfolio.models import PortfolioError
from aquant.rules import FeeBreakdown, FeeRateTouch, OrderSide, PositionLot

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SYMBOL_RE = re.compile(r"[0-9]{6}")


def _valid_fen(value: object, *, positive: bool = False) -> bool:
    return (
        type(value) is int
        and (value > 0 if positive else value >= 0)
    )


def _verify_fees(value: object) -> FeeBreakdown:
    if type(value) is not FeeBreakdown:
        raise PortfolioError("invalid_fees", "fees must be an exact FeeBreakdown")
    if (
        not _valid_fen(value.commission_fen)
        or not _valid_fen(value.stamp_duty_fen)
        or not _valid_fen(value.transfer_fee_fen)
        or type(value.touched_rates) is not tuple
    ):
        raise PortfolioError("invalid_fees", "fee components are invalid")
    for touch in value.touched_rates:
        if (
            type(touch) is not FeeRateTouch
            or type(touch.fee_name) is not str
            or not touch.fee_name
            or touch.effective_date is not None
            and type(touch.effective_date) is not date
            or type(touch.rate) is not Decimal
            or not touch.rate.is_finite()
            or touch.rate < 0
            or touch.minimum_yuan is not None
            and (
                type(touch.minimum_yuan) is not Decimal
                or not touch.minimum_yuan.is_finite()
                or touch.minimum_yuan < 0
            )
        ):
            raise PortfolioError("invalid_fees", "fee rate evidence is invalid")
    return value


def decimal_yuan_to_fen(value: Decimal) -> int:
    """Convert an exact non-negative yuan amount to integer fen."""
    if (
        type(value) is not Decimal
        or not value.is_finite()
        or value < 0
    ):
        raise PortfolioError(
            "invalid_decimal_amount",
            "yuan amount must be a finite non-negative Decimal",
        )
    try:
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise PortfolioError(
            "invalid_decimal_amount",
            "yuan amount cannot be represented at fen precision",
        ) from exc
    return int(rounded * 100)


def notional_fen(unit_price: Decimal, size: int) -> int:
    """Return a positive fill or mark notional in integer fen."""
    if (
        type(unit_price) is not Decimal
        or not unit_price.is_finite()
        or unit_price <= 0
    ):
        raise PortfolioError("invalid_unit_price", "unit price is invalid")
    if type(size) is not int or size <= 0:
        raise PortfolioError("invalid_position_size", "position size is invalid")
    return decimal_yuan_to_fen(unit_price * size)


def cash_after_fill(
    *,
    cash_before_fen: int,
    side: OrderSide,
    notional_amount_fen: int,
    fees: FeeBreakdown,
) -> int:
    """Apply the independently auditable cash formula for one fill."""
    if not _valid_fen(cash_before_fen):
        raise PortfolioError("invalid_cash", "cash must be non-negative integer fen")
    if type(side) is not OrderSide:
        raise PortfolioError("invalid_order_side", "order side is invalid")
    if not _valid_fen(notional_amount_fen, positive=True):
        raise PortfolioError("invalid_notional", "notional must be positive integer fen")
    checked_fees = _verify_fees(fees)
    direction = -1 if side is OrderSide.BUY else 1
    result = (
        cash_before_fen
        + direction * notional_amount_fen
        - checked_fees.total_fees_fen
    )
    if result < 0:
        raise PortfolioError(
            "insufficient_cash",
            "fill would make shared cash negative",
        )
    return result


@dataclass(frozen=True)
class BuyPosting:
    """One already-authorized buy fill ready for ledger posting."""

    event_id: str
    execution_date: date
    lot: PositionLot
    fees: FeeBreakdown


class CashEventKind(StrEnum):
    """Cash transition categories understood by the Gate A replay."""

    FILL = "fill"
    DIVIDEND_PAYMENT = "dividend_payment"


@dataclass(frozen=True)
class CashLedgerEvent:
    """One exact cash transition retained for independent replay."""

    event_id: str
    event_kind: CashEventKind
    session: date
    side: OrderSide | None
    symbol: str
    reference_id: str
    notional_fen: int
    commission_fen: int
    stamp_duty_fen: int
    transfer_fee_fen: int
    cash_before_fen: int
    cash_after_fen: int

    @property
    def total_fees_fen(self) -> int:
        return (
            self.commission_fen
            + self.stamp_duty_fen
            + self.transfer_fee_fen
        )


@dataclass(frozen=True)
class CashReceivable:
    """A dividend entitlement with source and actual cash dates."""

    event_id: str
    symbol: str
    registered_date: date
    source_payable_date: date
    actual_cash_date: date
    amount_fen: int
    paid_date: date | None = None


@dataclass(frozen=True)
class SymbolValuation:
    """One symbol's same-session position mark."""

    symbol: str
    total_size: int
    available_size: int
    locked_size: int
    mark_price: Decimal
    market_value_fen: int = field(init=False)

    def __post_init__(self) -> None:
        _verify_symbol_valuation(self, recompute=False)
        value = (
            0
            if self.total_size == 0
            else notional_fen(self.mark_price, self.total_size)
        )
        object.__setattr__(self, "market_value_fen", value)


@dataclass(frozen=True)
class DailyAccountSnapshot:
    """One official session's independently checkable portfolio identity."""

    session: date
    cash_fen: int
    position_market_value_fen: int
    receivable_fen: int
    equity_fen: int
    valuations: tuple[SymbolValuation, ...]


@dataclass(frozen=True)
class PortfolioLedger:
    """Current immutable account state plus replayable cash evidence."""

    initial_cash_fen: int
    cash_fen: int
    lots: tuple[PositionLot, ...] = ()
    cash_events: tuple[CashLedgerEvent, ...] = ()
    receivables: tuple[CashReceivable, ...] = ()
    daily_snapshots: tuple[DailyAccountSnapshot, ...] = ()


def _verify_lot(lot: object, *, execution_date: date | None = None) -> PositionLot:
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
        or lot.remaining_size != lot.original_size
        or type(lot.unit_cost) is not Decimal
        or not lot.unit_cost.is_finite()
        or lot.unit_cost <= 0
        or execution_date is not None
        and lot.acquired_date != execution_date
    ):
        raise PortfolioError("invalid_lot", "position lot is invalid")
    return lot


def _verify_cash_event(event: object) -> CashLedgerEvent:
    if (
        type(event) is not CashLedgerEvent
        or type(event.event_id) is not str
        or _IDENTIFIER_RE.fullmatch(event.event_id) is None
        or type(event.event_kind) is not CashEventKind
        or type(event.session) is not date
        or type(event.symbol) is not str
        or _SYMBOL_RE.fullmatch(event.symbol) is None
        or type(event.reference_id) is not str
        or _IDENTIFIER_RE.fullmatch(event.reference_id) is None
        or not _valid_fen(event.notional_fen, positive=True)
        or not _valid_fen(event.commission_fen)
        or not _valid_fen(event.stamp_duty_fen)
        or not _valid_fen(event.transfer_fee_fen)
        or not _valid_fen(event.cash_before_fen)
        or not _valid_fen(event.cash_after_fen)
    ):
        raise PortfolioError("invalid_cash_event", "cash event is invalid")
    if event.event_kind is CashEventKind.FILL:
        if event.side is not OrderSide.BUY:
            raise PortfolioError(
                "invalid_cash_event",
                "Gate A ledger supports buy fill postings only",
            )
    elif (
        event.side is not None
        or event.commission_fen != 0
        or event.stamp_duty_fen != 0
        or event.transfer_fee_fen != 0
    ):
        raise PortfolioError("invalid_cash_event", "dividend cash event is invalid")
    return event


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
        or not _valid_fen(value.amount_fen, positive=True)
        or value.paid_date is not None
        and (
            type(value.paid_date) is not date
            or value.paid_date != value.actual_cash_date
        )
    ):
        raise PortfolioError("invalid_receivable", "cash receivable is invalid")
    return value


def _verify_symbol_valuation(
    value: object,
    *,
    recompute: bool = True,
) -> SymbolValuation:
    if (
        type(value) is not SymbolValuation
        or type(value.symbol) is not str
        or _SYMBOL_RE.fullmatch(value.symbol) is None
        or not _valid_fen(value.total_size)
        or not _valid_fen(value.available_size)
        or not _valid_fen(value.locked_size)
        or value.available_size + value.locked_size != value.total_size
        or type(value.mark_price) is not Decimal
        or not value.mark_price.is_finite()
        or value.mark_price <= 0
    ):
        raise PortfolioError("invalid_valuation", "symbol valuation is invalid")
    if recompute:
        expected = (
            0
            if value.total_size == 0
            else notional_fen(value.mark_price, value.total_size)
        )
        if not _valid_fen(value.market_value_fen) or value.market_value_fen != expected:
            raise PortfolioError("invalid_valuation", "market value is invalid")
    return value


def _outstanding_receivable_fen(
    receivables: tuple[CashReceivable, ...],
    session: date,
) -> int:
    return sum(
        item.amount_fen
        for item in receivables
        if item.registered_date <= session
        and (item.paid_date is None or item.paid_date > session)
    )


def _position_sizes(
    lots: tuple[PositionLot, ...],
    session: date,
) -> dict[str, tuple[int, int, int]]:
    values: dict[str, list[int]] = {}
    for lot in lots:
        if lot.acquired_date > session or lot.remaining_size == 0:
            continue
        current = values.setdefault(lot.symbol, [0, 0, 0])
        current[0] += lot.remaining_size
        if lot.available_date <= session:
            current[1] += lot.remaining_size
        else:
            current[2] += lot.remaining_size
    return {symbol: tuple(value) for symbol, value in values.items()}


def create_portfolio_ledger(initial_cash_fen: int) -> PortfolioLedger:
    if not _valid_fen(initial_cash_fen, positive=True):
        raise PortfolioError(
            "invalid_initial_cash",
            "initial cash must be positive integer fen",
        )
    return PortfolioLedger(
        initial_cash_fen=initial_cash_fen,
        cash_fen=initial_cash_fen,
    )


def verify_portfolio_ledger(ledger: PortfolioLedger) -> None:
    """Replay every cash event and independently verify current state."""
    if (
        type(ledger) is not PortfolioLedger
        or not _valid_fen(ledger.initial_cash_fen, positive=True)
        or not _valid_fen(ledger.cash_fen)
        or type(ledger.lots) is not tuple
        or type(ledger.cash_events) is not tuple
        or type(ledger.receivables) is not tuple
        or type(ledger.daily_snapshots) is not tuple
    ):
        raise PortfolioError("invalid_ledger", "portfolio ledger is invalid")
    for lot in ledger.lots:
        _verify_lot(lot)
    lot_ids = tuple(lot.lot_id for lot in ledger.lots)
    if len(lot_ids) != len(set(lot_ids)):
        raise PortfolioError("duplicate_lot", "position lot IDs are duplicated")
    lots_by_id = {lot.lot_id: lot for lot in ledger.lots}
    event_ids: set[str] = set()
    event_lot_ids: set[str] = set()
    dividend_reference_ids: set[str] = set()
    dividend_events: dict[str, CashLedgerEvent] = {}
    running_cash = ledger.initial_cash_fen
    previous_session: date | None = None
    for event in ledger.cash_events:
        checked = _verify_cash_event(event)
        if checked.event_id in event_ids:
            raise PortfolioError("duplicate_event", "cash event IDs are duplicated")
        event_ids.add(checked.event_id)
        if previous_session is not None and checked.session < previous_session:
            raise PortfolioError("invalid_event_order", "cash event dates are out of order")
        previous_session = checked.session
        if checked.cash_before_fen != running_cash:
            raise PortfolioError("cash_reconciliation_failed", "cash event opening cash is wrong")
        if checked.event_kind is CashEventKind.FILL:
            if checked.reference_id in event_lot_ids:
                raise PortfolioError("duplicate_lot", "buy event lot IDs are duplicated")
            event_lot_ids.add(checked.reference_id)
            lot = lots_by_id.get(checked.reference_id)
            if (
                lot is None
                or checked.symbol != lot.symbol
                or checked.session != lot.acquired_date
                or checked.notional_fen
                != notional_fen(lot.unit_cost, lot.original_size)
            ):
                raise PortfolioError(
                    "lot_reconciliation_failed",
                    "buy cash event does not match its position lot",
                )
            fees = FeeBreakdown(
                commission_fen=checked.commission_fen,
                stamp_duty_fen=checked.stamp_duty_fen,
                transfer_fee_fen=checked.transfer_fee_fen,
                touched_rates=(),
            )
            assert checked.side is not None
            expected = cash_after_fill(
                cash_before_fen=running_cash,
                side=checked.side,
                notional_amount_fen=checked.notional_fen,
                fees=fees,
            )
        else:
            if checked.reference_id in dividend_reference_ids:
                raise PortfolioError(
                    "duplicate_receivable_payment",
                    "receivable payment is duplicated",
                )
            dividend_reference_ids.add(checked.reference_id)
            dividend_events[checked.reference_id] = checked
            expected = running_cash + checked.notional_fen
        if checked.cash_after_fen != expected:
            raise PortfolioError("cash_reconciliation_failed", "cash event closing cash is wrong")
        running_cash = expected
    if running_cash != ledger.cash_fen:
        raise PortfolioError("cash_reconciliation_failed", "ledger cash does not replay")
    if event_lot_ids != set(lot_ids):
        raise PortfolioError("lot_reconciliation_failed", "cash events and lots do not match")
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
            "paid receivables and cash events do not match",
        )
    previous_snapshot: date | None = None
    for snapshot in ledger.daily_snapshots:
        if (
            type(snapshot) is not DailyAccountSnapshot
            or type(snapshot.session) is not date
            or previous_snapshot is not None
            and snapshot.session <= previous_snapshot
            or type(snapshot.valuations) is not tuple
            or not _valid_fen(snapshot.cash_fen)
            or not _valid_fen(snapshot.position_market_value_fen)
            or not _valid_fen(snapshot.receivable_fen)
            or not _valid_fen(snapshot.equity_fen)
        ):
            raise PortfolioError("invalid_snapshot", "daily account snapshot is invalid")
        previous_snapshot = snapshot.session
        if any(
            item.paid_date is None
            and item.actual_cash_date <= snapshot.session
            for item in ledger.receivables
        ):
            raise PortfolioError(
                "overdue_receivable",
                "daily close contains an unpaid due receivable",
            )
        symbols = tuple(item.symbol for item in snapshot.valuations)
        if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
            raise PortfolioError("invalid_valuation_order", "valuations are not unique and sorted")
        for valuation in snapshot.valuations:
            _verify_symbol_valuation(valuation)
        position_sizes = _position_sizes(ledger.lots, snapshot.session)
        snapshot_sizes = {
            item.symbol: (
                item.total_size,
                item.available_size,
                item.locked_size,
            )
            for item in snapshot.valuations
        }
        if snapshot_sizes != position_sizes:
            raise PortfolioError(
                "position_reconciliation_failed",
                "snapshot position sizes do not match lots",
            )
        expected_cash = ledger.initial_cash_fen
        for event in ledger.cash_events:
            if event.session <= snapshot.session:
                expected_cash = event.cash_after_fen
            else:
                break
        expected_market = sum(
            item.market_value_fen for item in snapshot.valuations
        )
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


def post_buy(ledger: PortfolioLedger, posting: BuyPosting) -> PortfolioLedger:
    """Post one buy atomically and return a new ledger."""
    verify_portfolio_ledger(ledger)
    if type(posting) is not BuyPosting:
        raise TypeError("posting must be an exact BuyPosting")
    if (
        type(posting.event_id) is not str
        or _IDENTIFIER_RE.fullmatch(posting.event_id) is None
        or type(posting.execution_date) is not date
    ):
        raise PortfolioError("invalid_buy_posting", "buy posting is invalid")
    lot = _verify_lot(posting.lot, execution_date=posting.execution_date)
    fees = _verify_fees(posting.fees)
    if posting.event_id in {item.event_id for item in ledger.cash_events}:
        raise PortfolioError("duplicate_event", "buy event ID already exists")
    if lot.lot_id in {item.lot_id for item in ledger.lots}:
        raise PortfolioError("duplicate_lot", "position lot ID already exists")
    value_fen = notional_fen(lot.unit_cost, lot.original_size)
    cash_after = cash_after_fill(
        cash_before_fen=ledger.cash_fen,
        side=OrderSide.BUY,
        notional_amount_fen=value_fen,
        fees=fees,
    )
    event = CashLedgerEvent(
        event_id=posting.event_id,
        event_kind=CashEventKind.FILL,
        session=posting.execution_date,
        side=OrderSide.BUY,
        symbol=lot.symbol,
        reference_id=lot.lot_id,
        notional_fen=value_fen,
        commission_fen=fees.commission_fen,
        stamp_duty_fen=fees.stamp_duty_fen,
        transfer_fee_fen=fees.transfer_fee_fen,
        cash_before_fen=ledger.cash_fen,
        cash_after_fen=cash_after,
    )
    updated = PortfolioLedger(
        initial_cash_fen=ledger.initial_cash_fen,
        cash_fen=cash_after,
        lots=ledger.lots + (lot,),
        cash_events=ledger.cash_events + (event,),
        receivables=ledger.receivables,
        daily_snapshots=ledger.daily_snapshots,
    )
    verify_portfolio_ledger(updated)
    return updated


def register_receivable(
    ledger: PortfolioLedger,
    receivable: CashReceivable,
) -> PortfolioLedger:
    """Register a dividend entitlement without changing cash."""
    verify_portfolio_ledger(ledger)
    checked = _verify_receivable(receivable)
    if checked.paid_date is not None:
        raise PortfolioError("invalid_receivable", "new receivable is already paid")
    if checked.event_id in {item.event_id for item in ledger.receivables}:
        raise PortfolioError(
            "duplicate_receivable",
            "corporate-action event is already registered",
        )
    if (
        ledger.daily_snapshots
        and checked.registered_date <= ledger.daily_snapshots[-1].session
    ):
        raise PortfolioError(
            "invalid_receivable_order",
            "receivable registration must precede its daily close",
        )
    updated = replace(
        ledger,
        receivables=ledger.receivables + (checked,),
    )
    verify_portfolio_ledger(updated)
    return updated


def pay_receivables(
    ledger: PortfolioLedger,
    session: date,
) -> PortfolioLedger:
    """Pay each due receivable exactly once in stable symbol/event order."""
    verify_portfolio_ledger(ledger)
    if type(session) is not date:
        raise PortfolioError("invalid_session", "cash session is invalid")
    if ledger.daily_snapshots and session <= ledger.daily_snapshots[-1].session:
        raise PortfolioError(
            "invalid_cash_event_order",
            "cash payment must precede the session close",
        )
    if any(
        item.paid_date is None and item.actual_cash_date < session
        for item in ledger.receivables
    ):
        raise PortfolioError(
            "overdue_receivable",
            "an earlier receivable payment session was skipped",
        )
    due = sorted(
        (
            item
            for item in ledger.receivables
            if item.paid_date is None and item.actual_cash_date == session
        ),
        key=lambda item: (item.symbol, item.event_id),
    )
    if not due:
        return ledger
    cash = ledger.cash_fen
    events = list(ledger.cash_events)
    paid_ids: set[str] = set()
    existing_event_ids = {item.event_id for item in events}
    for item in due:
        event_id = f"cash:{item.event_id}"
        if event_id in existing_event_ids:
            raise PortfolioError(
                "duplicate_event",
                "dividend cash event ID already exists",
            )
        event = CashLedgerEvent(
            event_id=event_id,
            event_kind=CashEventKind.DIVIDEND_PAYMENT,
            session=session,
            side=None,
            symbol=item.symbol,
            reference_id=item.event_id,
            notional_fen=item.amount_fen,
            commission_fen=0,
            stamp_duty_fen=0,
            transfer_fee_fen=0,
            cash_before_fen=cash,
            cash_after_fen=cash + item.amount_fen,
        )
        events.append(event)
        existing_event_ids.add(event_id)
        paid_ids.add(item.event_id)
        cash = event.cash_after_fen
    receivables = tuple(
        replace(item, paid_date=session)
        if item.event_id in paid_ids
        else item
        for item in ledger.receivables
    )
    updated = replace(
        ledger,
        cash_fen=cash,
        cash_events=tuple(events),
        receivables=receivables,
    )
    verify_portfolio_ledger(updated)
    return updated


def close_session(
    ledger: PortfolioLedger,
    session: date,
    valuations: tuple[SymbolValuation, ...],
) -> PortfolioLedger:
    """Append one daily identity after independently checking positions."""
    verify_portfolio_ledger(ledger)
    if type(session) is not date:
        raise PortfolioError("invalid_session", "valuation session is invalid")
    if type(valuations) is not tuple or any(
        type(item) is not SymbolValuation for item in valuations
    ):
        raise TypeError("valuations must be an exact tuple of SymbolValuation")
    if (
        ledger.daily_snapshots
        and session <= ledger.daily_snapshots[-1].session
    ):
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
        item.paid_date is None and item.actual_cash_date <= session
        for item in ledger.receivables
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
        _verify_symbol_valuation(valuation)
    expected_positions = _position_sizes(ledger.lots, session)
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
            "valuation sizes do not match position lots",
        )
    market_value = sum(item.market_value_fen for item in valuations)
    receivable_fen = _outstanding_receivable_fen(ledger.receivables, session)
    snapshot = DailyAccountSnapshot(
        session=session,
        cash_fen=ledger.cash_fen,
        position_market_value_fen=market_value,
        receivable_fen=receivable_fen,
        equity_fen=ledger.cash_fen + market_value + receivable_fen,
        valuations=valuations,
    )
    updated = replace(
        ledger,
        daily_snapshots=ledger.daily_snapshots + (snapshot,),
    )
    verify_portfolio_ledger(updated)
    return updated
