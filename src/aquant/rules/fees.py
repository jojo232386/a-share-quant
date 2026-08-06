"""Date-effective statutory fees and explicit commission assumptions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from aquant.rules.models import (
    CommissionAssumption,
    FeeBreakdown,
    FeeRateTouch,
    InstrumentKind,
    OrderSide,
)

_STAMP_DUTY = (
    (date(2008, 9, 19), Decimal("0.001")),
    (date(2023, 8, 28), Decimal("0.0005")),
)
_TRANSFER_FEE = (
    (date(2015, 8, 1), Decimal("0.00002")),
    (date(2022, 4, 29), Decimal("0.00001")),
)
_DEFAULT_COMMISSION = CommissionAssumption(
    rate=Decimal("0.00025"),
    minimum_yuan=Decimal("5.00"),
)
_VERIFIED_FEE_POLICY_REGISTRY: dict[int, tuple[VerifiedFeePolicy, str]] = {}


class FeePolicyError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, init=False)
class VerifiedFeePolicy:
    stock_commission: CommissionAssumption
    etf_commission: CommissionAssumption
    stamp_duty_schedule: tuple[tuple[date, Decimal], ...]
    transfer_fee_schedule: tuple[tuple[date, Decimal], ...]
    policy_digest: str


def _validate_commission(value: object) -> None:
    if type(value) is not CommissionAssumption:
        raise FeePolicyError("invalid_fee_configuration", "commission assumption is invalid")
    try:
        CommissionAssumption(value.rate, value.minimum_yuan)
    except ValueError as exc:
        raise FeePolicyError(
            "invalid_fee_configuration", "commission assumption is invalid"
        ) from exc


def _validate_schedule(
    value: object, *, name: str
) -> tuple[tuple[date, Decimal], ...]:
    if type(value) is not tuple or not value:
        raise FeePolicyError("invalid_fee_configuration", f"{name} schedule is invalid")
    checked: list[tuple[date, Decimal]] = []
    for item in value:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not date
            or type(item[1]) is not Decimal
            or not item[1].is_finite()
            or item[1] < 0
        ):
            raise FeePolicyError("invalid_fee_configuration", f"{name} schedule is invalid")
        checked.append(item)
    if any(left[0] >= right[0] for left, right in zip(checked, checked[1:], strict=False)):
        raise FeePolicyError("invalid_fee_configuration", f"{name} schedule is invalid")
    return tuple(checked)


def make_fee_policy(
    *,
    stock_commission: CommissionAssumption = _DEFAULT_COMMISSION,
    etf_commission: CommissionAssumption = _DEFAULT_COMMISSION,
    stamp_duty_schedule: tuple[tuple[date, Decimal], ...] = _STAMP_DUTY,
    transfer_fee_schedule: tuple[tuple[date, Decimal], ...] = _TRANSFER_FEE,
) -> VerifiedFeePolicy:
    _validate_commission(stock_commission)
    _validate_commission(etf_commission)
    stamp = _validate_schedule(stamp_duty_schedule, name="stamp duty")
    transfer = _validate_schedule(transfer_fee_schedule, name="transfer fee")
    payload = {
        "etf_commission": {
            "minimum_yuan": str(etf_commission.minimum_yuan),
            "rate": str(etf_commission.rate),
        },
        "stamp_duty": [(item[0].isoformat(), str(item[1])) for item in stamp],
        "stock_commission": {
            "minimum_yuan": str(stock_commission.minimum_yuan),
            "rate": str(stock_commission.rate),
        },
        "transfer_fee": [(item[0].isoformat(), str(item[1])) for item in transfer],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    policy = object.__new__(VerifiedFeePolicy)
    object.__setattr__(policy, "stock_commission", stock_commission)
    object.__setattr__(policy, "etf_commission", etf_commission)
    object.__setattr__(policy, "stamp_duty_schedule", stamp)
    object.__setattr__(policy, "transfer_fee_schedule", transfer)
    object.__setattr__(policy, "policy_digest", digest)
    _VERIFIED_FEE_POLICY_REGISTRY[id(policy)] = (policy, digest)
    return policy


def default_fee_policy() -> VerifiedFeePolicy:
    return make_fee_policy()


def verify_fee_policy(policy: VerifiedFeePolicy) -> None:
    """Recompute policy identity and require the exact factory-created object."""
    if type(policy) is not VerifiedFeePolicy:
        raise FeePolicyError("invalid_fee_configuration", "fee policy is invalid")
    registered = _VERIFIED_FEE_POLICY_REGISTRY.get(id(policy))
    if registered is None or registered[0] is not policy:
        raise FeePolicyError("invalid_fee_configuration", "fee policy is invalid")
    expected = make_fee_policy(
        stock_commission=policy.stock_commission,
        etf_commission=policy.etf_commission,
        stamp_duty_schedule=policy.stamp_duty_schedule,
        transfer_fee_schedule=policy.transfer_fee_schedule,
    )
    _VERIFIED_FEE_POLICY_REGISTRY.pop(id(expected), None)
    if policy.policy_digest != expected.policy_digest or policy.policy_digest != registered[1]:
        raise FeePolicyError("invalid_fee_configuration", "fee policy is invalid")


def _latest_effective_rate(
    schedule: tuple[tuple[date, Decimal], ...], execution_date: date
) -> tuple[date, Decimal]:
    matches = [item for item in schedule if item[0] <= execution_date]
    if not matches:
        raise FeePolicyError("missing_fee_schedule", "missing fee schedule")
    return matches[-1]


def _to_fen(value: Decimal) -> int:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(rounded * 100)


def calculate_fees(
    policy: VerifiedFeePolicy,
    *,
    instrument_kind: InstrumentKind,
    side: OrderSide,
    execution_date: date,
    notional: Decimal,
) -> FeeBreakdown:
    if type(policy) is not VerifiedFeePolicy:
        raise FeePolicyError("invalid_fee_configuration", "fee policy is invalid")
    if (
        type(instrument_kind) is not InstrumentKind
        or type(side) is not OrderSide
        or type(execution_date) is not date
        or type(notional) is not Decimal
        or not notional.is_finite()
        or notional <= 0
    ):
        raise FeePolicyError("invalid_fee_input", "fee calculation input is invalid")
    commission = (
        policy.stock_commission
        if instrument_kind is InstrumentKind.MAIN_BOARD_STOCK
        else policy.etf_commission
    )
    commission_fen = _to_fen(max(notional * commission.rate, commission.minimum_yuan))
    touches = [
        FeeRateTouch("commission", None, commission.rate, commission.minimum_yuan)
    ]
    stamp_duty_fen = 0
    transfer_fee_fen = 0
    if instrument_kind is InstrumentKind.MAIN_BOARD_STOCK:
        transfer_date, transfer_rate = _latest_effective_rate(
            policy.transfer_fee_schedule, execution_date
        )
        transfer_fee_fen = _to_fen(notional * transfer_rate)
        touches.append(
            FeeRateTouch("transfer_fee", transfer_date, transfer_rate, None)
        )
        if side is OrderSide.SELL:
            stamp_date, stamp_rate = _latest_effective_rate(
                policy.stamp_duty_schedule, execution_date
            )
            stamp_duty_fen = _to_fen(notional * stamp_rate)
            touches.append(FeeRateTouch("stamp_duty", stamp_date, stamp_rate, None))
    return FeeBreakdown(
        commission_fen=commission_fen,
        stamp_duty_fen=stamp_duty_fen,
        transfer_fee_fen=transfer_fee_fen,
        touched_rates=tuple(touches),
    )
