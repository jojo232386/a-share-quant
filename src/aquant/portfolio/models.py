"""Immutable contracts for the shared-cash A-share portfolio engine."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from aquant.backtest.data_access import VerifiedMarketData
from aquant.backtest.feed import BacktestDataError, canonical_market_digest
from aquant.data.corporate_actions import (
    CorporateActionError,
    VerifiedCorporateActions,
    verify_verified_corporate_actions,
)
from aquant.rules import InstrumentKind
from aquant.universe import UniverseError, VerifiedUniverse, verify_universe


class PortfolioError(ValueError):
    """Raised when a portfolio contract or accounting invariant is invalid."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class PortfolioStrategy(StrEnum):
    """Deliberately narrow v0.2 strategy surface."""

    BUY_AND_HOLD = "buy_and_hold"


@dataclass(frozen=True)
class PortfolioConfig:
    """Exact deterministic settings for one shared-cash research run."""

    strategy: PortfolioStrategy
    initial_cash_fen: int
    gross_target_weight: Decimal
    signal_date: date
    end_date: date
    max_entry_attempts: int

    def __post_init__(self) -> None:
        if (
            type(self.strategy) is not PortfolioStrategy
            or type(self.initial_cash_fen) is not int
            or self.initial_cash_fen <= 0
            or type(self.gross_target_weight) is not Decimal
            or not self.gross_target_weight.is_finite()
            or not Decimal("0") < self.gross_target_weight <= Decimal("1")
            or type(self.signal_date) is not date
            or type(self.end_date) is not date
            or self.end_date <= self.signal_date
            or type(self.max_entry_attempts) is not int
            or not 1 <= self.max_entry_attempts <= 20
        ):
            raise PortfolioError("invalid_config", "portfolio configuration is invalid")


@dataclass(frozen=True)
class TargetAllocation:
    """Equal target notionals plus separately disclosed cash reserves."""

    member_count: int
    gross_target_notional_fen: int
    per_symbol_target_notional_fen: int
    planned_cash_reserve_fen: int
    allocation_rounding_remainder_fen: int


@dataclass(frozen=True)
class PortfolioInstrumentInput:
    """One exact verified market/actions pair entering the portfolio engine."""

    market_data: VerifiedMarketData
    corporate_actions: VerifiedCorporateActions
    _bound_market_contract_digest: str = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _verify_instrument_input(self, check_bound_digest=False)
        object.__setattr__(
            self,
            "_bound_market_contract_digest",
            _market_contract_digest(self.market_data),
        )

    @property
    def symbol(self) -> str:
        return self.market_data.provenance.symbol

    @property
    def instrument_kind(self) -> InstrumentKind:
        return self.market_data.provenance.instrument_kind


def allocate_equal_targets(
    config: PortfolioConfig,
    member_count: int,
) -> TargetAllocation:
    """Allocate a fixed target notional without treating fees as holdings."""
    if type(config) is not PortfolioConfig:
        raise TypeError("config must be an exact PortfolioConfig")
    if type(member_count) is not int or not 1 <= member_count <= 100:
        raise PortfolioError("invalid_member_count", "member count is invalid")
    gross_target = int(
        (Decimal(config.initial_cash_fen) * config.gross_target_weight).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    per_symbol = gross_target // member_count
    return TargetAllocation(
        member_count=member_count,
        gross_target_notional_fen=gross_target,
        per_symbol_target_notional_fen=per_symbol,
        planned_cash_reserve_fen=config.initial_cash_fen - gross_target,
        allocation_rounding_remainder_fen=gross_target - per_symbol * member_count,
    )


def _market_contract_digest(value: VerifiedMarketData) -> str:
    provenance = value.provenance
    payload = {
        "adjustment": provenance.adjustment,
        "file_sha256": provenance.file_sha256,
        "input_digest": value.input_digest,
        "instrument_kind": provenance.instrument_kind.value,
        "snapshot_id": provenance.snapshot_id,
        "symbol": provenance.symbol,
        "verification_method": provenance.verification_method,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _verify_instrument_input(
    value: PortfolioInstrumentInput,
    *,
    check_bound_digest: bool = True,
) -> None:
    if type(value.market_data) is not VerifiedMarketData:
        raise TypeError("market_data must be an exact VerifiedMarketData")
    if type(value.corporate_actions) is not VerifiedCorporateActions:
        raise TypeError(
            "corporate_actions must be an exact VerifiedCorporateActions"
        )
    frame = value.market_data.frame
    try:
        digest = canonical_market_digest(frame)
    except BacktestDataError as exc:
        raise PortfolioError(
            "verified_market_data_modified",
            "verified market data no longer passes its canonical quality gate",
        ) from exc
    if digest != value.market_data.input_digest:
        raise PortfolioError(
            "verified_market_data_modified",
            "verified market data no longer matches its canonical digest",
        )
    if (
        check_bound_digest
        and (
            type(value._bound_market_contract_digest) is not str
            or value._bound_market_contract_digest
            != _market_contract_digest(value.market_data)
        )
    ):
        raise PortfolioError(
            "verified_market_data_modified",
            "verified market provenance changed after input binding",
        )
    try:
        verify_verified_corporate_actions(value.corporate_actions)
    except (AttributeError, CorporateActionError, TypeError, ValueError) as exc:
        raise PortfolioError(
            "verified_corporate_actions_modified",
            "verified corporate actions no longer match their loader identity",
        ) from exc
    provenance = value.market_data.provenance
    action_provenance = value.corporate_actions.provenance
    if action_provenance is None:
        raise PortfolioError(
            "input_contract_mismatch",
            "corporate-action provenance is required",
        )
    frame_start = frame["date"].dt.date.iloc[0]
    frame_end = frame["date"].dt.date.iloc[-1]
    if (
        provenance.adjustment != ""
        or type(provenance.instrument_kind) is not InstrumentKind
        or action_provenance.symbol != provenance.symbol
        or action_provenance.instrument_kind is not provenance.instrument_kind
        or action_provenance.coverage_start > frame_start
        or action_provenance.coverage_end < frame_end
    ):
        raise PortfolioError(
            "input_contract_mismatch",
            "market and corporate-action contracts do not match",
        )


def validate_portfolio_inputs(
    inputs: tuple[PortfolioInstrumentInput, ...],
    *,
    universe: VerifiedUniverse,
) -> tuple[PortfolioInstrumentInput, ...]:
    """Recheck, complete, deduplicate, and deterministically order inputs."""
    if type(inputs) is not tuple:
        raise TypeError("inputs must be an exact tuple")
    if not inputs:
        raise PortfolioError("invalid_inputs", "portfolio inputs must not be empty")
    if any(type(item) is not PortfolioInstrumentInput for item in inputs):
        raise TypeError("each input must be an exact PortfolioInstrumentInput")
    try:
        verify_universe(universe)
    except UniverseError as exc:
        raise PortfolioError(
            "unverified_universe",
            "portfolio requires an exact verified universe",
        ) from exc
    for item in inputs:
        _verify_instrument_input(item)
    symbols = tuple(item.symbol for item in inputs)
    if len(symbols) != len(set(symbols)):
        raise PortfolioError("duplicate_input", "portfolio input symbols are duplicated")
    actual = {(item.symbol, item.instrument_kind.value) for item in inputs}
    expected = {(member.symbol, member.kind) for member in universe.members}
    if actual != expected:
        raise PortfolioError(
            "universe_contract_mismatch",
            "portfolio inputs must exactly match the verified universe",
        )
    return tuple(sorted(inputs, key=lambda item: item.symbol))
