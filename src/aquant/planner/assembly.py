"""Planner-local assembly of frozen A1 signal implementations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import NoReturn

from aquant.planner.core import PlannerError
from aquant.research.signals import (
    SIGNAL_REGISTRY,
    Signal,
    SignalError,
    SmaSignal,
    TopKMomentumSignal,
)


class SignalCardinality(StrEnum):
    """The eligible-symbol scope supported by a signal implementation."""

    SINGLE_SYMBOL = "single_symbol"
    MULTI_SYMBOL = "multi_symbol"


@dataclass(frozen=True)
class SignalSpec:
    """The planner's colocated construction and capability description."""

    name: str
    builder: Callable[[Mapping[str, object]], Signal]
    cardinality: SignalCardinality


def _invalid_signal_config() -> NoReturn:
    raise PlannerError("invalid_signal_config")


def _validated_config(
    config: object, *, required: frozenset[str], optional: frozenset[str]
) -> dict[str, object]:
    if not isinstance(config, Mapping):
        _invalid_signal_config()
    snapshot: dict[str, object] | None = None
    try:
        snapshot = dict(config)
    except Exception:
        pass
    if snapshot is None:
        raise PlannerError("invalid_signal_config")
    keys = set(snapshot)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        _invalid_signal_config()
    return snapshot


def _build_sma_signal(config: Mapping[str, object]) -> Signal:
    """Build the A1 SMA signal from its explicit planner configuration."""

    snapshot = _validated_config(
        config,
        required=frozenset({"period"}),
        optional=frozenset({"active_weight"}),
    )
    try:
        if "active_weight" in snapshot:
            return SmaSignal(period=snapshot["period"], active_weight=snapshot["active_weight"])
        return SmaSignal(period=snapshot["period"])
    except (KeyError, SignalError, TypeError) as error:
        raise PlannerError("invalid_signal_config") from error


def _build_top_k_momentum_signal(config: Mapping[str, object]) -> Signal:
    """Build the A1 top-k momentum signal from its explicit configuration."""

    snapshot = _validated_config(
        config,
        required=frozenset({"lookback", "k"}),
        optional=frozenset(),
    )
    try:
        return TopKMomentumSignal(lookback=snapshot["lookback"], k=snapshot["k"])
    except (KeyError, SignalError, TypeError) as error:
        raise PlannerError("invalid_signal_config") from error


SIGNAL_SPECS: Mapping[str, SignalSpec] = MappingProxyType(
    {
        "sma": SignalSpec(
            name="sma",
            builder=_build_sma_signal,
            cardinality=SignalCardinality.SINGLE_SYMBOL,
        ),
        "top_k_momentum": SignalSpec(
            name="top_k_momentum",
            builder=_build_top_k_momentum_signal,
            cardinality=SignalCardinality.MULTI_SYMBOL,
        ),
    }
)


def _validated_eligible_symbols(value: object) -> frozenset[str]:
    if type(value) is not frozenset or not value:
        raise PlannerError("invalid_eligible_symbols")
    if any(type(symbol) is not str or not symbol for symbol in value):
        raise PlannerError("invalid_eligible_symbols")
    return value


def _specs_match_registry() -> bool:
    if not isinstance(SIGNAL_SPECS, Mapping) or set(SIGNAL_SPECS) != set(SIGNAL_REGISTRY):
        return False
    return all(
        type(spec) is SignalSpec and key == spec.name for key, spec in SIGNAL_SPECS.items()
    )


def build_signal(
    *, name: str, config: Mapping[str, object], eligible_symbols: frozenset[str]
) -> Signal:
    """Assemble one frozen A1 signal without evaluating its output."""

    eligible = _validated_eligible_symbols(eligible_symbols)
    if not _specs_match_registry():
        raise PlannerError("signal_spec_registry_mismatch")
    if type(name) is not str or name not in SIGNAL_SPECS:
        raise PlannerError("unknown_signal_spec")

    spec = SIGNAL_SPECS[name]
    if spec.cardinality is SignalCardinality.SINGLE_SYMBOL:
        if len(eligible) != 1:
            raise PlannerError("unsupported_cardinality")
    elif spec.cardinality is not SignalCardinality.MULTI_SYMBOL:
        raise PlannerError("planner_invariant_violation")

    signal = spec.builder(config)
    if type(signal) is not SIGNAL_REGISTRY[name]:
        raise PlannerError("planner_invariant_violation")
    return signal
