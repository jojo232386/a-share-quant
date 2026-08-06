"""Deterministic risk metrics over audited daily ledgers."""

from __future__ import annotations

import math
from dataclasses import dataclass

from aquant.backtest.models import EquityRecord, FillRecord, PositionRecord

_ANNUAL_SESSIONS = 252


class RiskMetricError(ValueError):
    """Raised when ledgers cannot support an honest metric calculation."""


@dataclass(frozen=True)
class RiskMetrics:
    """One explicit set of return, drawdown, turnover, and exposure metrics."""

    observation_count: int
    observed_interval_count: int
    interval_count: int
    annual_sessions: int
    risk_free_rate: float
    total_return: float
    annualized_return: float
    annualized_volatility: float | None
    sharpe_zero_rate: float | None
    max_drawdown: float
    calmar: float | None
    gross_turnover: float
    annualized_gross_turnover: float
    max_gross_exposure: float


def _sample_standard_deviation(values: tuple[float, ...]) -> float | None:
    if len(values) < 2:
        return None
    mean = math.fsum(values) / len(values)
    variance = math.fsum((value - mean) ** 2 for value in values) / (
        len(values) - 1
    )
    return math.sqrt(max(variance, 0.0))


def compute_risk_metrics(
    *,
    equity_curve: tuple[EquityRecord, ...],
    positions: tuple[PositionRecord, ...],
    fills: tuple[FillRecord, ...],
    missing_session_count: int = 0,
) -> RiskMetrics:
    """Compute fixed-definition metrics without inferring missing ledger data."""
    if (
        type(equity_curve) is not tuple
        or len(equity_curve) < 2
        or type(positions) is not tuple
        or len(positions) != len(equity_curve)
        or type(fills) is not tuple
        or type(missing_session_count) is not int
        or isinstance(missing_session_count, bool)
        or missing_session_count < 0
    ):
        raise RiskMetricError(
            "risk metrics require two aligned daily observations"
        )
    if (
        any(type(item) is not EquityRecord for item in equity_curve)
        or any(type(item) is not PositionRecord for item in positions)
        or any(type(item) is not FillRecord for item in fills)
    ):
        raise RiskMetricError("risk metric ledger contains an invalid record")
    dates = tuple(item.date for item in equity_curve)
    if (
        any(left >= right for left, right in zip(dates, dates[1:], strict=False))
        or tuple(item.date for item in positions) != dates
    ):
        raise RiskMetricError("equity and position ledgers must align by date")

    equity = tuple(float(item.equity) for item in equity_curve)
    market_values = tuple(float(item.market_value) for item in positions)
    if any(not math.isfinite(value) or value <= 0 for value in equity):
        raise RiskMetricError("daily equity must be finite and positive")
    if any(
        not math.isfinite(value) or value < 0
        for value in market_values
    ):
        raise RiskMetricError("daily market value must be finite and non-negative")
    fill_values = tuple(float(item.value) for item in fills)
    if any(
        not math.isfinite(value) or value < 0
        for value in fill_values
    ):
        raise RiskMetricError("fill value must be finite and non-negative")

    observed_daily_returns = tuple(
        current / previous - 1.0
        for previous, current in zip(
            equity[:-1],
            equity[1:],
            strict=True,
        )
    )
    daily_returns = observed_daily_returns + (0.0,) * missing_session_count
    observed_interval_count = len(observed_daily_returns)
    interval_count = len(daily_returns)
    total_return = equity[-1] / equity[0] - 1.0
    annualized_return = (equity[-1] / equity[0]) ** (
        _ANNUAL_SESSIONS / interval_count
    ) - 1.0
    daily_standard_deviation = _sample_standard_deviation(daily_returns)
    annualized_volatility = (
        daily_standard_deviation * math.sqrt(_ANNUAL_SESSIONS)
        if daily_standard_deviation is not None
        else None
    )
    daily_mean = math.fsum(daily_returns) / interval_count
    sharpe = (
        daily_mean / daily_standard_deviation * math.sqrt(_ANNUAL_SESSIONS)
        if daily_standard_deviation not in {None, 0.0}
        else None
    )

    peak = equity[0]
    max_drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, (peak - value) / peak)
    calmar = (
        annualized_return / max_drawdown
        if max_drawdown > 0
        else None
    )
    average_equity = math.fsum(equity) / len(equity)
    gross_turnover = math.fsum(fill_values) / average_equity
    annualized_gross_turnover = (
        gross_turnover * _ANNUAL_SESSIONS / interval_count
    )
    max_gross_exposure = max(
        market_value / equity_value
        for market_value, equity_value in zip(
            market_values,
            equity,
            strict=True,
        )
    )
    return RiskMetrics(
        observation_count=len(equity),
        observed_interval_count=observed_interval_count,
        interval_count=interval_count,
        annual_sessions=_ANNUAL_SESSIONS,
        risk_free_rate=0.0,
        total_return=total_return,
        annualized_return=annualized_return,
        annualized_volatility=annualized_volatility,
        sharpe_zero_rate=sharpe,
        max_drawdown=max_drawdown,
        calmar=calmar,
        gross_turnover=gross_turnover,
        annualized_gross_turnover=annualized_gross_turnover,
        max_gross_exposure=max_gross_exposure,
    )
