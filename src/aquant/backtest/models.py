"""Immutable contracts for deterministic baseline backtests."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from aquant.rules import InstrumentKind

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SYMBOL_RE = re.compile(r"[0-9]{6}")


class StrategyName(StrEnum):
    """The two deliberately small Week 2 baseline strategies."""

    BUY_AND_HOLD = "buy_and_hold"
    SMA = "sma"


@dataclass(frozen=True)
class DataProvenance:
    """Minimum immutable provenance required before prices enter the broker."""

    symbol: str
    snapshot_id: str
    file_sha256: str
    adjustment: str
    verification_method: str
    instrument_kind: InstrumentKind = InstrumentKind.MAIN_BOARD_STOCK

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or _SYMBOL_RE.fullmatch(self.symbol) is None:
            raise ValueError("symbol must be exactly six digits")
        for field in ("snapshot_id", "file_sha256"):
            value = getattr(self, field)
            if type(value) is not str or _HASH_RE.fullmatch(value) is None:
                raise ValueError(f"{field} must be a lowercase SHA-256 value")
        if type(self.adjustment) is not str or self.adjustment not in {"", "qfq", "hfq"}:
            raise ValueError("adjustment is unsupported")
        if type(self.verification_method) is not str or self.verification_method not in {
            "manifest_sha256",
            "synthetic_digest",
        }:
            raise ValueError("verification_method is unsupported")
        if type(self.instrument_kind) is not InstrumentKind:
            raise ValueError("instrument_kind is unsupported")


@dataclass(frozen=True)
class BacktestConfig:
    """Validated, single-instrument settings for a Week 2 baseline run."""

    strategy: StrategyName
    initial_cash: float
    target_weight: Decimal
    sma_period: int | None = None
    random_seed: int = 0

    def __post_init__(self) -> None:
        if type(self.strategy) is not StrategyName:
            raise ValueError("strategy must be a StrategyName")
        if (
            type(self.initial_cash) not in {int, float}
            or not math.isfinite(self.initial_cash)
            or self.initial_cash <= 0
        ):
            raise ValueError("initial_cash must be a positive finite number")
        object.__setattr__(self, "initial_cash", float(self.initial_cash))
        if (
            type(self.target_weight) is not Decimal
            or not self.target_weight.is_finite()
            or not Decimal("0") < self.target_weight <= Decimal("1")
        ):
            raise ValueError("target_weight must be an exact Decimal in (0, 1]")
        if type(self.random_seed) is not int or self.random_seed < 0:
            raise ValueError("random_seed must be a non-negative integer")
        if self.strategy is StrategyName.BUY_AND_HOLD and self.sma_period is not None:
            raise ValueError("buy_and_hold does not accept sma_period")
        if self.strategy is StrategyName.SMA and (
            type(self.sma_period) is not int or self.sma_period < 2
        ):
            raise ValueError("sma strategy requires an integer sma_period of at least 2")


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    signal_date: date
    target_execution_date: date | None
    side: str
    requested_size: int
    final_status: str
    rejection_reason: str | None
    target_weight: str | None = None
    open_equity: str | None = None
    actual_weight: str | None = None


@dataclass(frozen=True)
class FillRecord:
    order_id: str
    execution_date: date
    side: str
    size: int
    price: float
    value: float
    commission: float
    commission_fen: int = 0
    stamp_duty_fen: int = 0
    transfer_fee_fen: int = 0
    total_fees_fen: int = 0


@dataclass(frozen=True)
class PositionRecord:
    date: date
    size: int
    close: float
    market_value: float
    available_size: int = 0
    locked_size: int = 0


@dataclass(frozen=True)
class CashRecord:
    date: date
    cash: float


@dataclass(frozen=True)
class EquityRecord:
    date: date
    equity: float


@dataclass(frozen=True)
class ReceivableRecord:
    date: date
    balance: float
    balance_fen: int


@dataclass(frozen=True)
class CorporateActionRecord:
    date: date
    event_id: str
    event_type: str
    entitled_size: int
    cash_dividend_per_unit: str
    amount_fen: int


@dataclass(frozen=True)
class RuleProvenance:
    calendar_id: str
    calendar_sha256: str
    fee_policy_digest: str
    instrument_kind: str


@dataclass(frozen=True)
class CorporateActionRunProvenance:
    snapshot_id: str
    file_sha256: str
    symbol: str
    instrument_kind: str
    provider: str
    source_schema: str
    normalization_version: str
    coverage_start: date
    coverage_end: date
    row_count: int
    verification_method: str


@dataclass(frozen=True)
class PositionLotRecord:
    lot_id: str
    symbol: str
    acquired_date: date
    available_date: date
    original_size: int
    remaining_size: int
    unit_cost: str


@dataclass(frozen=True)
class FeeRateRecord:
    fee_name: str
    effective_date: date | None
    rate: str
    minimum_yuan: str | None


@dataclass(frozen=True)
class BacktestResult:
    """Stable result with no wall-clock fields or engine-global order IDs."""

    schema_version: str
    run_id: str
    engine: str
    implementation_digest: str
    input_digest: str
    provenance: DataProvenance
    config: BacktestConfig
    orders: tuple[OrderRecord, ...]
    fills: tuple[FillRecord, ...]
    positions: tuple[PositionRecord, ...]
    cash_ledger: tuple[CashRecord, ...]
    equity_curve: tuple[EquityRecord, ...]
    rule_provenance: RuleProvenance | None = None
    lots: tuple[PositionLotRecord, ...] = ()
    missing_market_sessions: tuple[date, ...] = ()
    touched_fee_rates: tuple[FeeRateRecord, ...] = ()
    receivables: tuple[ReceivableRecord, ...] = ()
    corporate_action_ledger: tuple[CorporateActionRecord, ...] = ()
    corporate_action_provenance: CorporateActionRunProvenance | None = None
    price_stream_version: str = "raw-only-legacy"
    dividend_tax_mode: str = "gross_before_personal_tax"
    universe_id: str | None = None
