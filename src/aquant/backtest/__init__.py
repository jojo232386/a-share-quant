"""Backtrader feed, baselines, and deterministic audit records."""

from aquant.backtest.data_access import VerifiedMarketData, load_verified_snapshot
from aquant.backtest.export import (
    BacktestExportError,
    export_backtest_result,
)
from aquant.backtest.feed import (
    BacktestDataError,
    canonical_market_digest,
    make_backtrader_feed,
    prepare_feed_frame,
)
from aquant.backtest.models import (
    BacktestConfig,
    BacktestResult,
    CashRecord,
    CorporateActionRecord,
    CorporateActionRunProvenance,
    DataProvenance,
    EquityRecord,
    FeeRateRecord,
    FillRecord,
    OrderRecord,
    PositionLotRecord,
    PositionRecord,
    ReceivableRecord,
    RuleProvenance,
    StrategyName,
)
from aquant.backtest.runner import run_backtest, run_synthetic_backtest

__all__ = [
    "BacktestConfig",
    "BacktestDataError",
    "BacktestExportError",
    "BacktestResult",
    "CashRecord",
    "CorporateActionRecord",
    "CorporateActionRunProvenance",
    "DataProvenance",
    "EquityRecord",
    "FillRecord",
    "FeeRateRecord",
    "OrderRecord",
    "PositionLotRecord",
    "PositionRecord",
    "ReceivableRecord",
    "RuleProvenance",
    "StrategyName",
    "VerifiedMarketData",
    "canonical_market_digest",
    "export_backtest_result",
    "load_verified_snapshot",
    "make_backtrader_feed",
    "prepare_feed_frame",
    "run_backtest",
    "run_synthetic_backtest",
]
