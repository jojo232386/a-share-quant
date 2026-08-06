"""Formal same-input fixtures for Gate D cross-engine equivalence tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from aquant.backtest import (
    BacktestConfig,
    BacktestResult,
    StrategyName,
    load_verified_snapshot,
    run_backtest,
)
from aquant.backtest.data_access import VerifiedMarketData
from aquant.data.calendar_snapshot import (
    CalendarSnapshotStore,
    VerifiedTradingCalendar,
    load_verified_calendar,
)
from aquant.data.corporate_actions import (
    CorporateActionEvent,
    VerifiedCorporateActions,
    load_verified_corporate_actions,
    publish_corporate_actions,
)
from aquant.data.manifest import ManifestRecord
from aquant.data.snapshot import RawSnapshotStore
from aquant.portfolio import (
    PortfolioConfig,
    PortfolioInstrumentInput,
    PortfolioStrategy,
    VerifiedPortfolioRun,
    run_verified_portfolio,
)
from aquant.rules import InstrumentKind, VerifiedFeePolicy, default_fee_policy
from aquant.universe import (
    UniverseMember,
    VerifiedUniverse,
    canonical_universe_bytes,
    load_verified_universe,
)

_FETCHED_AT = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)


@dataclass(frozen=True)
class EquivalenceScenario:
    """One deliberately supported Gate D economic-equivalence scenario."""

    name: str
    symbol: str
    instrument_kind: InstrumentKind
    official_dates: tuple[date, ...]
    opens: tuple[Decimal, ...]
    closes: tuple[Decimal, ...]
    initial_cash_fen: int
    target_weight: Decimal
    corporate_action_events: tuple[CorporateActionEvent, ...] = ()


@dataclass(frozen=True)
class SharedVerifiedInputs:
    market_data: VerifiedMarketData
    corporate_actions: VerifiedCorporateActions
    calendar: VerifiedTradingCalendar
    universe: VerifiedUniverse
    fee_policy: VerifiedFeePolicy


@dataclass(frozen=True)
class EnginePair:
    scenario: EquivalenceScenario
    shared: SharedVerifiedInputs
    v01: BacktestResult
    v02: VerifiedPortfolioRun


def base_stock_scenario() -> EquivalenceScenario:
    """Return the minimal main-board Gate D scenario."""
    dates = (
        date(2026, 7, 13),
        date(2026, 7, 14),
        date(2026, 7, 15),
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
    )
    return EquivalenceScenario(
        name="main_board_base",
        symbol="600000",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        official_dates=dates,
        opens=(
            Decimal("10.20"),
            Decimal("10.60"),
            Decimal("10.70"),
            Decimal("10.80"),
            Decimal("10.90"),
        ),
        closes=(
            Decimal("10.20"),
            Decimal("10.50"),
            Decimal("10.90"),
            Decimal("10.40"),
            Decimal("11.00"),
        ),
        initial_cash_fen=100_000_000,
        target_weight=Decimal("0.95"),
    )


def etf_minimum_commission_scenario() -> EquivalenceScenario:
    """Return the 100-unit ETF minimum-commission boundary."""
    return replace(
        base_stock_scenario(),
        name="etf_minimum_commission",
        symbol="510300",
        instrument_kind=(
            InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF
        ),
        opens=(
            Decimal("94.000"),
            Decimal("95.000"),
            Decimal("95.500"),
            Decimal("96.000"),
            Decimal("96.500"),
        ),
        closes=(
            Decimal("94.000"),
            Decimal("95.200"),
            Decimal("95.700"),
            Decimal("96.200"),
            Decimal("96.700"),
        ),
        initial_cash_fen=1_000_000,
    )


def full_weight_fee_shrink_scenario() -> EquivalenceScenario:
    """Return a full-weight stock target that must shrink by one lot."""
    return replace(
        base_stock_scenario(),
        name="full_weight_fee_shrink",
        opens=(
            Decimal("9.90"),
            Decimal("10.00"),
            Decimal("10.10"),
            Decimal("10.20"),
            Decimal("10.30"),
        ),
        closes=(
            Decimal("9.90"),
            Decimal("10.00"),
            Decimal("10.10"),
            Decimal("10.20"),
            Decimal("10.30"),
        ),
        initial_cash_fen=1_000_000,
        target_weight=Decimal("1"),
    )


def weekend_t1_scenario() -> EquivalenceScenario:
    """Return a Friday fill whose official T+1 session is Monday."""
    return replace(
        base_stock_scenario(),
        name="weekend_t1",
        official_dates=(
            date(2026, 7, 16),
            date(2026, 7, 17),
            date(2026, 7, 20),
            date(2026, 7, 21),
            date(2026, 7, 22),
            date(2026, 7, 23),
        ),
    )


def dividend_scenario(
    *,
    record_date: date,
    name: str,
) -> EquivalenceScenario:
    """Return a trading-day cash-dividend scenario."""
    base = base_stock_scenario()
    event = CorporateActionEvent.create(
        symbol=base.symbol,
        instrument_kind=base.instrument_kind,
        announcement_date=date(2026, 7, 1),
        record_date=record_date,
        ex_date=date(2026, 7, 16),
        payable_date=date(2026, 7, 17),
        cash_dividend_per_unit=Decimal("2.00"),
        stock_dividend_ratio=Decimal("0"),
        capitalization_ratio=Decimal("0"),
        rights_ratio=Decimal("0"),
        rights_price=None,
        source_schema="synthetic.cash.v1",
        source_url="https://example.invalid/gate-d-dividend",
    )
    return replace(
        base,
        name=name,
        opens=(
            Decimal("100.00"),
            Decimal("100.00"),
            Decimal("100.00"),
            Decimal("98.00"),
            Decimal("98.00"),
        ),
        closes=(
            Decimal("100.00"),
            Decimal("100.00"),
            Decimal("100.00"),
            Decimal("98.00"),
            Decimal("98.00"),
        ),
        initial_cash_fen=1_100_000,
        corporate_action_events=(event,),
    )


def _raw_frame(scenario: EquivalenceScenario) -> pd.DataFrame:
    market_dates = scenario.official_dates[:-1]
    return pd.DataFrame(
        {
            "日期": [item.isoformat() for item in market_dates],
            "开盘": [float(item) for item in scenario.opens],
            "最高": [
                float(max(open_price, close_price) + Decimal("0.10"))
                for open_price, close_price in zip(
                    scenario.opens,
                    scenario.closes,
                    strict=True,
                )
            ],
            "最低": [
                float(min(open_price, close_price) - Decimal("0.10"))
                for open_price, close_price in zip(
                    scenario.opens,
                    scenario.closes,
                    strict=True,
                )
            ],
            "收盘": [float(item) for item in scenario.closes],
            "成交量": [100_000 for _ in market_dates],
            "成交额": [1_000_000.0 for _ in market_dates],
        }
    )


def _verified_market(root: Path, scenario: EquivalenceScenario) -> VerifiedMarketData:
    market_dates = scenario.official_dates[:-1]
    artifact = RawSnapshotStore(root).write(
        _raw_frame(scenario),
        symbol=scenario.symbol,
        source_slug="eastmoney",
        snapshot_date=date(2026, 7, 21),
    )
    is_etf = (
        scenario.instrument_kind
        is InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF
    )
    record = ManifestRecord.create(
        schema_version="1.0",
        symbol=scenario.symbol,
        instrument_kind=scenario.instrument_kind.value,
        provider="eastmoney",
        source_function="fund_etf_hist_em" if is_etf else "stock_zh_a_hist",
        source_schema=(
            "akshare.fund_etf_hist_em"
            if is_etf
            else "akshare.stock_zh_a_hist"
        ),
        endpoint_host="push2his.eastmoney.com",
        provider_symbol="sh" + scenario.symbol,
        fetched_at_utc=_FETCHED_AT,
        requested_start=market_dates[0],
        requested_end=market_dates[-1],
        actual_start=market_dates[0],
        actual_end=market_dates[-1],
        row_count=artifact.row_count,
        snapshot_relative_path=artifact.relative_path,
        file_sha256=artifact.sha256,
        adjustment="",
        factor_source=None,
        latest_market_date=market_dates[-1],
        akshare_version="1.18.64",
        raw_volume_unit="lot",
        volume_multiplier_to_canonical=100,
        full_history_download=False,
        local_date_slice=False,
        quality_issue_counts={
            "empty_frame": 0,
            "null": 0,
            "duplicate_date": 0,
            "out_of_order_date": 0,
            "non_finite_numeric": 0,
            "non_positive_price": 0,
            "negative_volume": 0,
            "negative_amount": 0,
            "invalid_high": 0,
            "invalid_low": 0,
        },
    )
    return load_verified_snapshot(root, record)


def _verified_universe(root: Path, scenario: EquivalenceScenario) -> VerifiedUniverse:
    content = canonical_universe_bytes(
        "gate-d-singleton",
        (UniverseMember(scenario.symbol, scenario.instrument_kind.value),),
    )
    universe_id = hashlib.sha256(content).hexdigest()
    directory = root / "configs" / "universes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{universe_id}.json"
    path.write_bytes(content)
    return load_verified_universe(path, expected_id=universe_id)


def run_equivalence_case(root: Path, scenario: EquivalenceScenario) -> EnginePair:
    """Run the same loader-backed evidence through both engines."""
    market_dates = scenario.official_dates[:-1]
    if (
        len(scenario.official_dates) < 3
        or len(scenario.opens) != len(market_dates)
        or len(scenario.closes) != len(market_dates)
    ):
        raise ValueError("scenario dates and prices must align")
    market_data = _verified_market(root, scenario)
    action_record = publish_corporate_actions(
        root,
        scenario.corporate_action_events,
        symbol=scenario.symbol,
        instrument_kind=scenario.instrument_kind,
        provider="synthetic",
        source_schema="synthetic.cash.v1",
        normalization_version="cash-only-v1",
        coverage_start=market_dates[0],
        coverage_end=scenario.official_dates[-1],
    )
    corporate_actions = load_verified_corporate_actions(root, action_record)
    calendar_record = CalendarSnapshotStore(root).write(
        scenario.official_dates,
        source_provider="synthetic",
        source_function="gate_d_calendar",
        source_version="1",
        fetched_at_utc=_FETCHED_AT,
    )
    calendar = load_verified_calendar(root, calendar_record)
    universe = _verified_universe(root, scenario)
    fee_policy = default_fee_policy()
    shared = SharedVerifiedInputs(
        market_data=market_data,
        corporate_actions=corporate_actions,
        calendar=calendar,
        universe=universe,
        fee_policy=fee_policy,
    )
    v01 = run_backtest(
        market_data,
        universe=universe,
        corporate_actions=corporate_actions,
        calendar=calendar,
        fee_policy=fee_policy,
        config=BacktestConfig(
            strategy=StrategyName.BUY_AND_HOLD,
            initial_cash=float(
                Decimal(scenario.initial_cash_fen) / Decimal("100")
            ),
            target_weight=scenario.target_weight,
        ),
    )
    v02 = run_verified_portfolio(
        config=PortfolioConfig(
            strategy=PortfolioStrategy.BUY_AND_HOLD,
            initial_cash_fen=scenario.initial_cash_fen,
            gross_target_weight=scenario.target_weight,
            signal_date=market_dates[0],
            end_date=market_dates[-1],
            max_entry_attempts=5,
        ),
        inputs=(
            PortfolioInstrumentInput(
                market_data=market_data,
                corporate_actions=corporate_actions,
            ),
        ),
        universe=universe,
        calendar=calendar,
        fee_policy=fee_policy,
    )
    return EnginePair(
        scenario=scenario,
        shared=shared,
        v01=v01,
        v02=v02,
    )
