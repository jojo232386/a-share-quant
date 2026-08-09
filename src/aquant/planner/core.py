"""Immutable rolling target-state planning primitives."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Clamped, Context, Decimal, Inexact, Rounded, Underflow, localcontext
from enum import StrEnum
from types import MappingProxyType

PLANNER_SCHEMA_VERSION = "1.0.0"
_ZERO = Decimal("0")
_ONE = Decimal("1")
_CONTEXT = Context(prec=60)


class PlannerError(ValueError):
    """A planner input or invariant error with a stable public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class NoPreviousStateReason(StrEnum):
    FIRST_PERIOD = "first_period"
    EXPLICIT_RESET = "explicit_reset"


@dataclass(frozen=True)
class NoPreviousState:
    reason: NoPreviousStateReason

    def __post_init__(self) -> None:
        if type(self.reason) is not NoPreviousStateReason:
            raise PlannerError("invalid_previous_state")


def _validated_as_of(value: object) -> date:
    if type(value) is not date:
        raise PlannerError("invalid_as_of")
    return value


def _validated_targets(value: object) -> dict[str, Decimal]:
    if not isinstance(value, Mapping):
        raise PlannerError("invalid_output_type")

    validated: dict[str, Decimal] = {}
    for symbol, weight in value.items():
        if type(symbol) is not str or not symbol:
            raise PlannerError("invalid_symbol")
        if type(weight) is not Decimal:
            raise PlannerError("non_decimal_weight")
        if not weight.is_finite():
            raise PlannerError("non_finite_weight")
        if weight < _ZERO:
            raise PlannerError("negative_weight")
        if weight > _ONE:
            raise PlannerError("weight_above_one")
        validated[symbol] = weight
    return dict(sorted(validated.items()))


def _exact_nonnegative_sum(values: Iterable[Decimal]) -> Decimal:
    operands = tuple(value for value in values if value != _ZERO)
    if not operands:
        return _ZERO

    min_exponent = min(int(value.as_tuple().exponent) for value in operands)
    max_adjusted = max(value.adjusted() for value in operands)
    min_adjusted = min(value.adjusted() for value in operands)
    precision = max(
        _CONTEXT.prec,
        max_adjusted - min_exponent + 1 + len(str(len(operands))),
    )
    context = Context(
        prec=precision,
        Emin=min(_CONTEXT.Emin, min_adjusted),
        Emax=max(_CONTEXT.Emax, max_adjusted + len(str(len(operands)))),
    )
    for signal in (Inexact, Rounded, Underflow, Clamped):
        context.traps[signal] = True
    try:
        with localcontext(context):
            total = _ZERO
            for value in operands:
                total += value
            return total
    except (Clamped, Inexact, Rounded, Underflow, ValueError) as error:
        raise PlannerError("planner_invariant_violation") from error


def _gross(targets: Mapping[str, Decimal]) -> Decimal:
    return _exact_nonnegative_sum(targets.values())


def _validate_hard_gross(targets: Mapping[str, Decimal]) -> None:
    if _gross(targets) > _ONE:
        raise PlannerError("hard_gross_ceiling_exceeded")


@dataclass(frozen=True, init=False)
class PreviousTargets:
    as_of: date
    targets: Mapping[str, Decimal]

    def __init__(self, *, as_of: date, targets: Mapping[str, Decimal]) -> None:
        object.__setattr__(self, "as_of", _validated_as_of(as_of))
        copied = _validated_targets(targets)
        _validate_hard_gross(copied)
        object.__setattr__(self, "targets", MappingProxyType(copied))


@dataclass(frozen=True, init=False)
class PlannedTargets:
    as_of: date
    targets: Mapping[str, Decimal]

    def __init__(self, *, as_of: date, targets: Mapping[str, Decimal]) -> None:
        object.__setattr__(self, "as_of", _validated_as_of(as_of))
        copied = _validated_targets(targets)
        _validate_hard_gross(copied)
        object.__setattr__(self, "targets", MappingProxyType(copied))


@dataclass(frozen=True)
class PlannerLimits:
    max_single_weight: Decimal = Decimal("1")
    max_gross: Decimal = Decimal("1")
    min_cash_ratio: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if (
            type(self.max_single_weight) is not Decimal
            or type(self.max_gross) is not Decimal
            or type(self.min_cash_ratio) is not Decimal
            or not self.max_single_weight.is_finite()
            or not self.max_gross.is_finite()
            or not self.min_cash_ratio.is_finite()
            or not (_ZERO < self.max_single_weight <= _ONE)
            or not (_ZERO < self.max_gross <= _ONE)
            or not (_ZERO <= self.min_cash_ratio < _ONE)
        ):
            raise PlannerError("invalid_limits")


def _validated_limits(value: object) -> PlannerLimits:
    if type(value) is not PlannerLimits:
        raise PlannerError("invalid_limits")
    try:
        PlannerLimits(
            max_single_weight=value.max_single_weight,
            max_gross=value.max_gross,
            min_cash_ratio=value.min_cash_ratio,
        )
    except (AttributeError, PlannerError) as error:
        raise PlannerError("invalid_limits") from error
    return value


def _validated_eligible_symbols(value: object) -> frozenset[str]:
    if type(value) is not frozenset or not value:
        raise PlannerError("invalid_eligible_symbols")
    if any(type(symbol) is not str or not symbol for symbol in value):
        raise PlannerError("invalid_eligible_symbols")
    return value


def _validated_previous(value: object, current_as_of: date) -> Mapping[str, Decimal]:
    if type(value) is NoPreviousState:
        if type(value.reason) is not NoPreviousStateReason:
            raise PlannerError("invalid_previous_state")
        return {}
    if type(value) is not PreviousTargets:
        raise PlannerError("invalid_previous_state")

    prior_as_of = _validated_as_of(value.as_of)
    targets = _validated_targets(value.targets)
    _validate_hard_gross(targets)
    if prior_as_of >= current_as_of:
        raise PlannerError("non_ascending_previous_state")
    return targets


def _validate_effective_state(
    targets: Mapping[str, Decimal], limits: PlannerLimits
) -> None:
    normalized = _validated_targets(targets)
    gross = _gross(normalized)
    if gross > _ONE:
        raise PlannerError("hard_gross_ceiling_exceeded")
    if any(weight > limits.max_single_weight for weight in normalized.values()):
        raise PlannerError("max_single_weight_exceeded")
    if gross > limits.max_gross:
        raise PlannerError("max_gross_exceeded")
    if _exact_nonnegative_sum((gross, limits.min_cash_ratio)) > _ONE:
        raise PlannerError("min_cash_ratio_violated")


def plan_targets(
    *,
    as_of: date,
    signal_output: Mapping[str, Decimal],
    previous: PreviousTargets | NoPreviousState,
    eligible_symbols: frozenset[str],
    limits: PlannerLimits,
) -> PlannedTargets:
    """Merge current signals with the preceding state under fixed constraints."""

    current_as_of = _validated_as_of(as_of)
    validated_limits = _validated_limits(limits)
    current = _validated_targets(signal_output)
    prior = _validated_previous(previous, current_as_of)
    eligible = _validated_eligible_symbols(eligible_symbols)

    if set(current).difference(eligible) or set(prior).difference(eligible):
        raise PlannerError("universe_mismatch")

    effective = dict(prior)
    effective.update(current)
    effective = _validated_targets(effective)
    _validate_effective_state(effective, validated_limits)
    return PlannedTargets(as_of=current_as_of, targets=effective)
