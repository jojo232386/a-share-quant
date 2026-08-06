"""Public contracts for the conservative A-share rule engine."""

from aquant.rules.engine import evaluate_order
from aquant.rules.fees import (
    FeePolicyError,
    VerifiedFeePolicy,
    calculate_fees,
    default_fee_policy,
    make_fee_policy,
    verify_fee_policy,
)
from aquant.rules.lots import (
    RuleInputError,
    consume_fifo,
    create_buy_lot,
    sellable_size,
    validate_sell_size,
)
from aquant.rules.models import (
    CommissionAssumption,
    FeeBreakdown,
    FeeRateTouch,
    InstrumentKind,
    InstrumentRule,
    OrderIntent,
    OrderSide,
    PositionLot,
    RejectionReason,
    RuleDecision,
)
from aquant.rules.price_limits import price_limits

__all__ = [
    "CommissionAssumption",
    "FeeBreakdown",
    "FeePolicyError",
    "FeeRateTouch",
    "InstrumentKind",
    "InstrumentRule",
    "OrderIntent",
    "OrderSide",
    "PositionLot",
    "RejectionReason",
    "RuleDecision",
    "RuleInputError",
    "VerifiedFeePolicy",
    "calculate_fees",
    "default_fee_policy",
    "evaluate_order",
    "make_fee_policy",
    "verify_fee_policy",
    "consume_fifo",
    "create_buy_lot",
    "price_limits",
    "sellable_size",
    "validate_sell_size",
]
