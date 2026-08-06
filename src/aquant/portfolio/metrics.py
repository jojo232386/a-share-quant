"""Exact integer-fen metrics for verified shared-cash portfolio runs."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date
from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    DecimalException,
    localcontext,
)

from aquant.portfolio.accounting import CashEventKind, notional_fen
from aquant.portfolio.coordinator import AttemptStatus, EntryAttempt, TargetStatus
from aquant.portfolio.identity import VerifiedPortfolioRun, verify_portfolio_run
from aquant.portfolio.models import PortfolioError

_ANNUAL_SESSIONS = 252
_METRIC_QUANTUM = Decimal("0.000000000001")
_ZERO = Decimal(0)
_ONE = Decimal(1)


def _published_decimal(value: Decimal) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise PortfolioError(
            "nonfinite_portfolio_metric",
            "portfolio metrics must be finite decimals",
        )
    try:
        with localcontext() as context:
            context.prec = max(80, len(value.as_tuple().digits) + 20)
            return value.quantize(_METRIC_QUANTUM, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise PortfolioError(
            "invalid_portfolio_metric",
            "portfolio metric cannot be published at fixed precision",
        ) from exc


@dataclass(frozen=True)
class PortfolioMetrics:
    """Formal research-only metrics derived from a verified integer-fen ledger."""

    observation_count: int
    observed_return_count: int
    annual_sessions: int
    risk_free_rate: Decimal
    total_return: Decimal
    annualized_return: Decimal
    annualized_volatility: Decimal | None
    sharpe_zero_rate: Decimal | None
    max_drawdown: Decimal
    turnover: Decimal
    trade_count: int
    rejected_attempt_count: int
    max_gross_exposure: Decimal
    max_symbol_weight: Decimal
    max_target_weight_deviation: Decimal
    daily_gross_exposure: tuple[tuple[date, Decimal], ...]
    final_symbol_weight_deviations: tuple[tuple[str, Decimal], ...]
    gross_target_notional_fen: int
    planned_cash_reserve_fen: int
    allocation_rounding_remainder_fen: int
    invested_notional_fen: int
    ordinary_lot_rounding_fen: int
    fee_lot_reduction_fen: int
    expired_uninvested_fen: int
    rejected_uninvested_fen: int
    total_paid_fees_fen: int
    research_only: bool = True
    live_trading: bool = False
    profit_claim: bool = False

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is Decimal and not value.is_finite():
                raise PortfolioError(
                    "nonfinite_portfolio_metric",
                    "portfolio metrics must be finite decimals",
                )
        if (
            type(self.observation_count) is not int
            or self.observation_count <= 0
            or type(self.observed_return_count) is not int
            or self.observed_return_count != self.observation_count
            or self.annual_sessions != _ANNUAL_SESSIONS
            or self.risk_free_rate != _ZERO
            or type(self.daily_gross_exposure) is not tuple
            or len(self.daily_gross_exposure) != self.observation_count
            or type(self.final_symbol_weight_deviations) is not tuple
            or self.research_only is not True
            or self.live_trading is not False
            or self.profit_claim is not False
        ):
            raise PortfolioError(
                "invalid_portfolio_metrics",
                "portfolio metric contract is invalid",
            )


def _exact_returns(
    *,
    initial_cash_fen: int,
    daily_equities: tuple[int, ...],
) -> tuple[Decimal, ...]:
    previous = Decimal(initial_cash_fen)
    result: list[Decimal] = []
    for value in daily_equities:
        current = Decimal(value)
        if current <= 0 or previous <= 0:
            raise PortfolioError(
                "invalid_metric_equity",
                "portfolio metric equity must remain positive",
            )
        result.append(current / previous - _ONE)
        previous = current
    return tuple(result)


def _risk_metrics(
    daily_returns: tuple[Decimal, ...],
) -> tuple[Decimal | None, Decimal | None]:
    if len(daily_returns) < 2:
        return None, None
    mean_return = sum(daily_returns, start=_ZERO) / len(daily_returns)
    variance = (
        sum(
            ((item - mean_return) ** 2 for item in daily_returns),
            start=_ZERO,
        )
        / (len(daily_returns) - 1)
    )
    standard_deviation = variance.sqrt()
    if standard_deviation == 0:
        return _published_decimal(_ZERO), None
    annualizer = Decimal(_ANNUAL_SESSIONS).sqrt()
    return (
        _published_decimal(standard_deviation * annualizer),
        _published_decimal(
            mean_return / standard_deviation * annualizer
        ),
    )


def _max_drawdown(
    *,
    initial_cash_fen: int,
    daily_equities: tuple[int, ...],
) -> Decimal:
    peak = Decimal(initial_cash_fen)
    worst = _ZERO
    for value in daily_equities:
        current = Decimal(value)
        if current <= 0:
            raise PortfolioError(
                "invalid_metric_equity",
                "portfolio metric equity must remain positive",
            )
        if current > peak:
            peak = current
        drawdown = current / peak - _ONE
        if drawdown < worst:
            worst = drawdown
    return _published_decimal(worst)


def _allocation_components(
    run: VerifiedPortfolioRun,
) -> tuple[int, int, int, int, int]:
    result = run.result
    events_by_id = {
        item.event_id: item
        for item in result.ledger.cash_events
        if item.event_kind is CashEventKind.FILL
    }
    lots_by_id = {item.lot_id: item for item in result.ledger.lots}
    attempts_by_target: dict[str, list[EntryAttempt]] = {}
    for attempt in result.attempts:
        attempts_by_target.setdefault(attempt.target_id, []).append(attempt)

    invested = 0
    ordinary_rounding = 0
    fee_reduction = 0
    expired = 0
    rejected = 0
    known_target_ids = {target.target_id for target in result.targets}
    if set(attempts_by_target) - known_target_ids:
        raise PortfolioError(
            "metric_target_reconciliation_failed",
            "portfolio attempts contain an unknown root target",
        )

    for target in result.targets:
        target_attempts = attempts_by_target.get(target.target_id, [])
        if (
            len(target_attempts) != target.attempts_used
            or tuple(item.attempt_number for item in target_attempts)
            != tuple(range(1, target.attempts_used + 1))
            or any(
                item.target_id != target.target_id
                or item.symbol != target.symbol
                or item.original_signal_date != target.signal_date
                for item in target_attempts
            )
        ):
            raise PortfolioError(
                "metric_target_reconciliation_failed",
                "portfolio attempts do not reconcile to their root target",
            )
        filled_attempts = [
            item
            for item in target_attempts
            if item.status is AttemptStatus.FILLED
        ]
        if target.status is TargetStatus.EXPIRED_UNFILLED:
            if filled_attempts:
                raise PortfolioError(
                    "metric_target_reconciliation_failed",
                    "expired target contains a fill",
                )
            expired += target.target_notional_fen
            continue
        if target.status is TargetStatus.PENDING:
            if target.attempts_used <= 0 or filled_attempts:
                raise PortfolioError(
                    "metric_target_reconciliation_failed",
                    "pending target lacks auditable rejected attempts",
                )
            rejected += target.target_notional_fen
            continue
        if (
            target.status is not TargetStatus.FILLED
            or len(filled_attempts) != 1
            or target.fill_event_id is None
        ):
            raise PortfolioError(
                "metric_target_reconciliation_failed",
                "filled target does not map to exactly one fill",
            )
        attempt = filled_attempts[0]
        event = events_by_id.get(target.fill_event_id)
        if (
            event is None
            or attempt.fill_event_id != event.event_id
            or event.symbol != target.symbol
            or attempt.fees is None
            or event.commission_fen != attempt.fees.commission_fen
            or event.stamp_duty_fen != attempt.fees.stamp_duty_fen
            or event.transfer_fee_fen != attempt.fees.transfer_fee_fen
        ):
            raise PortfolioError(
                "metric_fill_reconciliation_failed",
                "filled target and exact cash event do not reconcile",
            )
        lot = lots_by_id.get(event.reference_id)
        if (
            lot is None
            or lot.symbol != target.symbol
            or lot.original_size != attempt.requested_size
            or notional_fen(lot.unit_cost, lot.original_size)
            != event.notional_fen
            or attempt.cash_available_before_fen != event.cash_before_fen
            or attempt.requested_cash_required_fen
            != event.notional_fen + event.total_fees_fen
        ):
            raise PortfolioError(
                "metric_fill_reconciliation_failed",
                "filled attempt, lot, and cash event do not reconcile",
            )
        initial_notional = notional_fen(
            lot.unit_cost,
            attempt.initial_candidate_size,
        )
        initial_required = attempt.initial_candidate_cash_required_fen
        if (
            type(initial_required) is not int
            or initial_required < initial_notional
        ):
            raise PortfolioError(
                "metric_fee_counterfactual_failed",
                "initial candidate cash evidence is invalid",
            )
        reduced_notional = initial_notional - event.notional_fen
        if attempt.initial_candidate_size == attempt.requested_size:
            if reduced_notional != 0 or attempt.quantity_adjustment_reason is not None:
                raise PortfolioError(
                    "metric_fee_counterfactual_failed",
                    "unadjusted fill contains adjustment evidence",
                )
        else:
            expected_reduction = notional_fen(
                lot.unit_cost,
                attempt.initial_candidate_size - attempt.requested_size,
            )
            if (
                reduced_notional != expected_reduction
                or attempt.quantity_adjustment_reason
                != "insufficient_cash_including_fees"
                or attempt.cash_available_before_fen is None
                or attempt.cash_available_before_fen >= initial_required
                or attempt.requested_cash_required_fen is None
                or attempt.cash_available_before_fen
                < attempt.requested_cash_required_fen
            ):
                raise PortfolioError(
                    "metric_fee_counterfactual_failed",
                    "fee-aware lot reduction lacks exact cash evidence",
                )
        ordinary = target.target_notional_fen - initial_notional
        if ordinary < 0 or reduced_notional < 0:
            raise PortfolioError(
                "metric_allocation_conservation_failed",
                "filled target exceeds its fixed target notional",
            )
        invested += event.notional_fen
        ordinary_rounding += ordinary
        fee_reduction += reduced_notional

    if set(events_by_id) != {
        target.fill_event_id
        for target in result.targets
        if target.status is TargetStatus.FILLED
    }:
        raise PortfolioError(
            "metric_fill_reconciliation_failed",
            "cash fill events and filled targets do not match",
        )
    return invested, ordinary_rounding, fee_reduction, expired, rejected


def _compute_portfolio_metrics(run: VerifiedPortfolioRun) -> PortfolioMetrics:
    verify_portfolio_run(run)
    result = run.result
    snapshots = result.ledger.daily_snapshots
    if not snapshots:
        raise PortfolioError(
            "missing_metric_observations",
            "formal portfolio metrics require at least one daily snapshot",
        )
    daily_equities = tuple(item.equity_fen for item in snapshots)
    daily_returns = _exact_returns(
        initial_cash_fen=result.config.initial_cash_fen,
        daily_equities=daily_equities,
    )
    total_return = (
        Decimal(daily_equities[-1])
        / Decimal(result.config.initial_cash_fen)
        - _ONE
    )
    try:
        with localcontext() as context:
            context.prec = 80
            annualized_return = (
                (_ONE + total_return)
                ** (
                    Decimal(_ANNUAL_SESSIONS)
                    / len(daily_returns)
                )
                - _ONE
            )
    except DecimalException as exc:
        raise PortfolioError(
            "invalid_portfolio_metric",
            "annualized return cannot be computed exactly enough",
        ) from exc
    annualized_volatility, sharpe = _risk_metrics(daily_returns)

    symbols = tuple(sorted(target.symbol for target in result.targets))
    if len(symbols) != result.allocation.member_count:
        raise PortfolioError(
            "metric_target_reconciliation_failed",
            "portfolio target membership is invalid",
        )
    target_weights = {
        target.symbol: (
            Decimal(target.target_notional_fen)
            / Decimal(result.config.initial_cash_fen)
        )
        for target in result.targets
    }
    gross_exposures: list[tuple[date, Decimal]] = []
    max_symbol_weight = _ZERO
    max_target_deviation = _ZERO
    final_deviations: tuple[tuple[str, Decimal], ...] = ()
    for snapshot in snapshots:
        equity = Decimal(snapshot.equity_fen)
        if equity <= 0:
            raise PortfolioError(
                "invalid_metric_equity",
                "portfolio metric equity must remain positive",
            )
        gross = Decimal(snapshot.position_market_value_fen) / equity
        gross_exposures.append(
            (snapshot.session, _published_decimal(gross))
        )
        values = {
            item.symbol: item.market_value_fen
            for item in snapshot.valuations
        }
        deviations: list[tuple[str, Decimal]] = []
        for symbol in symbols:
            actual_weight = Decimal(values.get(symbol, 0)) / equity
            deviation = actual_weight - target_weights[symbol]
            max_symbol_weight = max(max_symbol_weight, abs(actual_weight))
            max_target_deviation = max(
                max_target_deviation,
                abs(deviation),
            )
            deviations.append((symbol, _published_decimal(deviation)))
        final_deviations = tuple(deviations)

    (
        invested_notional,
        ordinary_rounding,
        fee_reduction,
        expired_uninvested,
        rejected_uninvested,
    ) = _allocation_components(run)
    allocation = result.allocation
    conservation = (
        allocation.allocation_rounding_remainder_fen
        + invested_notional
        + ordinary_rounding
        + fee_reduction
        + rejected_uninvested
        + expired_uninvested
    )
    if conservation != allocation.gross_target_notional_fen:
        raise PortfolioError(
            "metric_allocation_conservation_failed",
            "fixed target notional categories do not conserve exactly",
        )

    fill_events = tuple(
        item
        for item in result.ledger.cash_events
        if item.event_kind is CashEventKind.FILL
    )
    average_equity = (
        sum((Decimal(item) for item in daily_equities), start=_ZERO)
        / len(daily_equities)
    )
    if average_equity <= 0:
        raise PortfolioError(
            "invalid_metric_equity",
            "mean daily equity must remain positive",
        )
    turnover = (
        Decimal(sum(item.notional_fen for item in fill_events))
        / average_equity
    )

    return PortfolioMetrics(
        observation_count=len(snapshots),
        observed_return_count=len(daily_returns),
        annual_sessions=_ANNUAL_SESSIONS,
        risk_free_rate=_ZERO,
        total_return=_published_decimal(total_return),
        annualized_return=_published_decimal(annualized_return),
        annualized_volatility=annualized_volatility,
        sharpe_zero_rate=sharpe,
        max_drawdown=_max_drawdown(
            initial_cash_fen=result.config.initial_cash_fen,
            daily_equities=daily_equities,
        ),
        turnover=_published_decimal(turnover),
        trade_count=len(fill_events),
        rejected_attempt_count=sum(
            item.status is AttemptStatus.REJECTED
            for item in result.attempts
        ),
        max_gross_exposure=max(
            (value for _, value in gross_exposures),
            default=_ZERO,
        ),
        max_symbol_weight=_published_decimal(max_symbol_weight),
        max_target_weight_deviation=_published_decimal(
            max_target_deviation
        ),
        daily_gross_exposure=tuple(gross_exposures),
        final_symbol_weight_deviations=final_deviations,
        gross_target_notional_fen=allocation.gross_target_notional_fen,
        planned_cash_reserve_fen=allocation.planned_cash_reserve_fen,
        allocation_rounding_remainder_fen=(
            allocation.allocation_rounding_remainder_fen
        ),
        invested_notional_fen=invested_notional,
        ordinary_lot_rounding_fen=ordinary_rounding,
        fee_lot_reduction_fen=fee_reduction,
        expired_uninvested_fen=expired_uninvested,
        rejected_uninvested_fen=rejected_uninvested,
        total_paid_fees_fen=sum(
            item.total_fees_fen for item in fill_events
        ),
    )


def compute_portfolio_metrics(run: VerifiedPortfolioRun) -> PortfolioMetrics:
    """Compute deterministic research metrics from an exact verified run."""
    with localcontext(Context(prec=80, rounding=ROUND_HALF_UP)):
        return _compute_portfolio_metrics(run)
