from __future__ import annotations

import importlib
import sys
from dataclasses import fields
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, Inexact, Rounded, localcontext

import pytest

from aquant.portfolio import (
    PortfolioError,
    PortfolioMetrics,
    VerifiedPortfolioRun,
    compute_portfolio_metrics,
    run_verified_portfolio,
)

TESTS_ROOT = __import__("pathlib").Path(__file__).parents[1]
sys.path.insert(0, str(TESTS_ROOT))
gate_c_support = importlib.import_module("portfolio_gate_c_support")
sys.path.pop(0)
make_portfolio_case = gate_c_support.make_portfolio_case


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)


def test_metrics_are_part_of_the_public_portfolio_contract():
    assert PortfolioMetrics.__module__ == "aquant.portfolio.metrics"


def test_metrics_use_daily_integer_fen_equity_and_fixed_252_session_basis(
    tmp_path,
):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))

    metrics = compute_portfolio_metrics(run)

    assert metrics.observation_count == 1
    assert metrics.observed_return_count == 1
    assert metrics.annual_sessions == 252
    assert metrics.risk_free_rate == Decimal("0")
    assert metrics.total_return == Decimal("-0.0005095")
    assert metrics.annualized_return == _q(
        (Decimal(1_998_981) / Decimal(2_000_000)) ** 252 - 1
    )
    assert metrics.annualized_volatility is None
    assert metrics.sharpe_zero_rate is None
    assert metrics.max_drawdown == Decimal("-0.0005095")
    assert metrics.turnover == _q(
        Decimal(1_900_000) / Decimal(1_998_981)
    )
    assert metrics.trade_count == 2
    assert metrics.rejected_attempt_count == 0
    assert metrics.total_paid_fees_fen == 1_019
    assert metrics.planned_cash_reserve_fen == 0
    assert metrics.allocation_rounding_remainder_fen == 0
    assert metrics.ordinary_lot_rounding_fen == 0
    assert metrics.fee_lot_reduction_fen == 100_000
    assert metrics.expired_uninvested_fen == 0
    assert metrics.rejected_uninvested_fen == 0
    assert metrics.research_only is True
    assert metrics.live_trading is False
    assert metrics.profit_claim is False


def test_multi_session_metrics_match_exact_daily_equity_formulas(tmp_path):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
        date(2026, 7, 21),
        date(2026, 7, 22),
    )
    run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path,
            calendar_dates=official_dates,
            market_dates=official_dates[:4],
            market_opens=(Decimal("10"),) * 4,
            market_closes=(
                Decimal("10"),
                Decimal("10"),
                Decimal("9"),
                Decimal("11"),
            ),
            signal_date=official_dates[0],
            end_date=official_dates[3],
        )
    )

    metrics = compute_portfolio_metrics(run)

    equities = tuple(
        Decimal(value) for value in (1_998_981, 1_808_981, 2_188_981)
    )
    daily_returns = (
        equities[0] / Decimal(2_000_000) - 1,
        equities[1] / equities[0] - 1,
        equities[2] / equities[1] - 1,
    )
    mean_return = sum(daily_returns, start=Decimal(0)) / 3
    sample_variance = (
        sum(
            ((item - mean_return) ** 2 for item in daily_returns),
            start=Decimal(0),
        )
        / 2
    )
    sample_std = sample_variance.sqrt()

    assert metrics.observation_count == 3
    assert metrics.observed_return_count == 3
    assert metrics.total_return == Decimal("0.0944905")
    assert metrics.annualized_return == _q(
        (equities[-1] / Decimal(2_000_000)) ** 84 - 1
    )
    assert metrics.annualized_volatility == _q(
        sample_std * Decimal(252).sqrt()
    )
    assert metrics.sharpe_zero_rate == _q(
        mean_return / sample_std * Decimal(252).sqrt()
    )
    assert metrics.max_drawdown == _q(equities[1] / Decimal(2_000_000) - 1)
    assert metrics.turnover == _q(
        Decimal(1_900_000) / (sum(equities) / 3)
    )
    assert metrics.daily_gross_exposure == (
        (official_dates[1], _q(Decimal(1_900_000) / equities[0])),
        (official_dates[2], _q(Decimal(1_710_000) / equities[1])),
        (official_dates[3], _q(Decimal(2_090_000) / equities[2])),
    )
    assert metrics.max_gross_exposure == max(
        value for _, value in metrics.daily_gross_exposure
    )
    assert metrics.max_symbol_weight == _q(
        Decimal(1_100_000) / equities[2]
    )
    assert metrics.max_target_weight_deviation == _q(
        abs(Decimal(810_000) / equities[1] - Decimal("0.5"))
    )
    assert metrics.final_symbol_weight_deviations == (
        (
            "600000",
            _q(Decimal(1_100_000) / equities[2] - Decimal("0.5")),
        ),
        (
            "600001",
            _q(Decimal(990_000) / equities[2] - Decimal("0.5")),
        ),
    )


def test_zero_daily_return_sample_does_not_publish_fake_risk_values(tmp_path):
    official_dates = (
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
        date(2026, 7, 21),
    )
    run = run_verified_portfolio(
        **make_portfolio_case(
            tmp_path,
            initial_cash_fen=2_001_020,
            calendar_dates=official_dates,
            market_dates=official_dates[:3],
            market_opens=(Decimal("10"),) * 3,
            market_closes=(Decimal("10.0051"),) * 3,
            signal_date=official_dates[0],
            end_date=official_dates[2],
        )
    )

    metrics = compute_portfolio_metrics(run)

    assert metrics.observed_return_count == 2
    assert metrics.annualized_volatility == Decimal("0")
    assert metrics.sharpe_zero_rate is None


def test_metrics_are_independent_of_callers_decimal_context(tmp_path):
    run = run_verified_portfolio(**make_portfolio_case(tmp_path))
    results = [compute_portfolio_metrics(run)]
    for precision in (10, 28, 80):
        with localcontext() as context:
            context.prec = precision
            results.append(compute_portfolio_metrics(run))
    with localcontext() as context:
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        results.append(compute_portfolio_metrics(run))

    assert all(item == results[0] for item in results[1:])


@pytest.mark.parametrize("max_entry_attempts,field", ((5, "rejected"), (1, "expired")))
def test_unfilled_target_notional_is_counted_once_by_final_target_status(
    tmp_path,
    max_entry_attempts,
    field,
):
    case = make_portfolio_case(
        tmp_path,
        market_opens=(Decimal("10"), Decimal("100000")),
        market_closes=(Decimal("10"), Decimal("100000")),
        max_entry_attempts=max_entry_attempts,
    )
    run = run_verified_portfolio(**case)

    metrics = compute_portfolio_metrics(run)

    assert metrics.trade_count == 0
    assert metrics.rejected_attempt_count == 2
    assert metrics.invested_notional_fen == 0
    assert metrics.ordinary_lot_rounding_fen == 0
    assert metrics.fee_lot_reduction_fen == 0
    assert metrics.rejected_uninvested_fen == (
        2_000_000 if field == "rejected" else 0
    )
    assert metrics.expired_uninvested_fen == (
        2_000_000 if field == "expired" else 0
    )
    assert metrics.gross_target_notional_fen == (
        metrics.allocation_rounding_remainder_fen
        + metrics.invested_notional_fen
        + metrics.ordinary_lot_rounding_fen
        + metrics.fee_lot_reduction_fen
        + metrics.rejected_uninvested_fen
        + metrics.expired_uninvested_fen
    )


def test_metrics_reject_tampered_or_unverified_runs(tmp_path):
    tampered = run_verified_portfolio(
        **make_portfolio_case(tmp_path / "tampered")
    )
    object.__setattr__(tampered.result.ledger, "cash_fen", 0)
    with pytest.raises(PortfolioError) as tampered_error:
        compute_portfolio_metrics(tampered)
    assert tampered_error.value.code == "verified_portfolio_run_modified"

    valid = run_verified_portfolio(**make_portfolio_case(tmp_path / "valid"))
    forged = object.__new__(VerifiedPortfolioRun)
    object.__setattr__(forged, "identity", valid.identity)
    object.__setattr__(forged, "result", valid.result)
    with pytest.raises(PortfolioError) as forged_error:
        compute_portfolio_metrics(forged)
    assert forged_error.value.code == "unverified_portfolio_run"


def test_every_published_decimal_is_finite(tmp_path):
    metrics = compute_portfolio_metrics(
        run_verified_portfolio(**make_portfolio_case(tmp_path))
    )

    decimal_values = [
        getattr(metrics, item.name)
        for item in fields(metrics)
        if type(getattr(metrics, item.name)) is Decimal
    ]
    decimal_values.extend(
        value for _, value in metrics.daily_gross_exposure
    )
    decimal_values.extend(
        value for _, value in metrics.final_symbol_weight_deviations
    )
    assert decimal_values
    assert all(item.is_finite() for item in decimal_values)
