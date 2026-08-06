from datetime import date

import pytest

from aquant.backtest.models import EquityRecord, FillRecord, PositionRecord
from aquant.risk.metrics import RiskMetricError, compute_risk_metrics


def _equity(*values):
    return tuple(
        EquityRecord(date(2026, 7, 13 + index), value)
        for index, value in enumerate(values)
    )


def _positions(*values):
    return tuple(
        PositionRecord(
            date=date(2026, 7, 13 + index),
            size=0 if value == 0 else 100,
            close=0 if value == 0 else value / 100,
            market_value=value,
        )
        for index, value in enumerate(values)
    )


def _fill(value):
    return FillRecord(
        order_id="order-0001",
        execution_date=date(2026, 7, 14),
        side="buy",
        size=100,
        price=value / 100,
        value=value,
        commission=0,
    )


def test_metrics_match_hand_recomputed_returns_drawdown_turnover_and_exposure():
    metrics = compute_risk_metrics(
        equity_curve=_equity(100, 110, 88, 99),
        positions=_positions(0, 90, 80, 0),
        fills=(_fill(50), _fill(50)),
    )

    assert metrics.observation_count == 4
    assert metrics.observed_interval_count == 3
    assert metrics.interval_count == 3
    assert metrics.total_return == pytest.approx(-0.01)
    assert metrics.max_drawdown == pytest.approx(0.2)
    assert metrics.gross_turnover == pytest.approx(100 / 99.25)
    assert metrics.annualized_gross_turnover == pytest.approx(
        (100 / 99.25) * 252 / 3
    )
    assert metrics.max_gross_exposure == pytest.approx(80 / 88)
    assert metrics.annualized_volatility > 0
    assert metrics.sharpe_zero_rate is not None
    assert metrics.calmar is not None


def test_missing_official_sessions_are_zero_return_annualization_intervals():
    without_gap = compute_risk_metrics(
        equity_curve=_equity(100, 110, 99),
        positions=_positions(0, 90, 0),
        fills=(_fill(50),),
    )
    with_gap = compute_risk_metrics(
        equity_curve=_equity(100, 110, 99),
        positions=_positions(0, 90, 0),
        fills=(_fill(50),),
        missing_session_count=2,
    )

    assert with_gap.observed_interval_count == 2
    assert with_gap.interval_count == 4
    assert with_gap.total_return == without_gap.total_return
    assert with_gap.max_drawdown == without_gap.max_drawdown
    assert with_gap.annualized_return > without_gap.annualized_return
    assert with_gap.annualized_gross_turnover < (
        without_gap.annualized_gross_turnover
    )


def test_constant_equity_has_zero_volatility_and_undefined_ratios():
    metrics = compute_risk_metrics(
        equity_curve=_equity(100, 100, 100),
        positions=_positions(0, 0, 0),
        fills=(),
    )

    assert metrics.total_return == 0
    assert metrics.annualized_return == 0
    assert metrics.annualized_volatility == 0
    assert metrics.max_drawdown == 0
    assert metrics.sharpe_zero_rate is None
    assert metrics.calmar is None
    assert metrics.gross_turnover == 0
    assert metrics.annualized_gross_turnover == 0
    assert metrics.max_gross_exposure == 0


def test_metrics_reject_nonpositive_equity_or_misaligned_positions():
    with pytest.raises(RiskMetricError, match="positive"):
        compute_risk_metrics(
            equity_curve=_equity(100, 0),
            positions=_positions(0, 0),
            fills=(),
        )

    shifted = (
        PositionRecord(
            date=date(2026, 7, 14),
            size=0,
            close=0,
            market_value=0,
        ),
        PositionRecord(
            date=date(2026, 7, 15),
            size=0,
            close=0,
            market_value=0,
        ),
    )
    with pytest.raises(RiskMetricError, match="align"):
        compute_risk_metrics(
            equity_curve=_equity(100, 101),
            positions=shifted,
            fills=(),
        )


def test_metrics_require_at_least_two_observations_and_exact_record_types():
    with pytest.raises(RiskMetricError, match="two"):
        compute_risk_metrics(
            equity_curve=_equity(100),
            positions=_positions(0),
            fills=(),
        )

    with pytest.raises(RiskMetricError, match="record"):
        compute_risk_metrics(
            equity_curve=(object(), object()),
            positions=_positions(0, 0),
            fills=(),
        )
