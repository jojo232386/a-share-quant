"""Loader-backed synthetic inputs shared by Gate C identity tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from aquant.backtest import load_verified_snapshot
from aquant.data.calendar_snapshot import CalendarSnapshotStore, load_verified_calendar
from aquant.data.corporate_actions import (
    CorporateActionEvent,
    load_verified_corporate_actions,
    make_synthetic_corporate_actions,
    publish_corporate_actions,
)
from aquant.data.manifest import ManifestRecord, ManifestWriter
from aquant.data.snapshot import RawSnapshotStore
from aquant.gate_e.config import canonical_config_bytes
from aquant.portfolio import (
    PortfolioConfig,
    PortfolioInstrumentInput,
    PortfolioStrategy,
    run_verified_portfolio,
)
from aquant.rules import (
    CommissionAssumption,
    InstrumentKind,
    make_fee_policy,
)
from aquant.universe import (
    UniverseMember,
    canonical_universe_bytes,
    load_verified_universe,
)

OFFICIAL_DATES = (
    date(2026, 7, 16),
    date(2026, 7, 17),
    date(2026, 7, 20),
)
FIXED_FETCHED_AT = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)


def _calendar(root: Path, dates: tuple[date, ...]):
    record = CalendarSnapshotStore(root).write(
        dates,
        source_provider="synthetic",
        source_function="gate_c_calendar",
        source_version="1",
        fetched_at_utc=FIXED_FETCHED_AT,
    )
    return load_verified_calendar(root, record)


def _raw_market_frame(
    dates: tuple[date, ...],
    *,
    opens: tuple[Decimal, ...],
    closes: tuple[Decimal, ...],
) -> pd.DataFrame:
    if len(opens) != len(dates) or len(closes) != len(dates):
        raise ValueError("market prices must align with dates")
    return pd.DataFrame(
        {
            "日期": [item.isoformat() for item in dates],
            "开盘": [float(item) for item in opens],
            "最高": [
                float(max(open_value, close_value) + Decimal("0.10"))
                for open_value, close_value in zip(opens, closes, strict=True)
            ],
            "最低": [
                float(min(open_value, close_value) - Decimal("0.10"))
                for open_value, close_value in zip(opens, closes, strict=True)
            ],
            "收盘": [float(item) for item in closes],
            "成交量": [10_000 for _ in dates],
            "成交额": [100_000.0 for _ in dates],
        }
    )


def _verified_market(
    root: Path,
    symbol: str,
    *,
    dates: tuple[date, ...],
    opens: tuple[Decimal, ...],
    closes: tuple[Decimal, ...],
):
    artifact = RawSnapshotStore(root).write(
        _raw_market_frame(dates, opens=opens, closes=closes),
        symbol=symbol,
        source_slug="eastmoney",
        snapshot_date=date(2026, 7, 22),
    )
    record = ManifestRecord.create(
        schema_version="1.0",
        symbol=symbol,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK.value,
        provider="eastmoney",
        source_function="stock_zh_a_hist",
        source_schema="akshare.stock_zh_a_hist",
        endpoint_host="push2his.eastmoney.com",
        provider_symbol="sh" + symbol,
        fetched_at_utc=FIXED_FETCHED_AT,
        requested_start=dates[0],
        requested_end=dates[-1],
        actual_start=dates[0],
        actual_end=dates[-1],
        row_count=artifact.row_count,
        snapshot_relative_path=artifact.relative_path,
        file_sha256=artifact.sha256,
        adjustment="",
        factor_source=None,
        latest_market_date=dates[-1],
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


def _verified_universe(root: Path, symbols: tuple[str, ...]):
    members = tuple(
        UniverseMember(symbol, InstrumentKind.MAIN_BOARD_STOCK.value) for symbol in sorted(symbols)
    )
    content = canonical_universe_bytes("gate-c-test", members)
    universe_id = hashlib.sha256(content).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{universe_id}.json"
    path.write_bytes(content)
    return load_verified_universe(path, expected_id=universe_id)


def make_portfolio_case(
    root: Path,
    *,
    symbols: tuple[str, ...] = ("600000", "600001"),
    initial_cash_fen: int = 2_000_000,
    gross_target_weight: Decimal = Decimal("1"),
    calendar_dates: tuple[date, ...] = OFFICIAL_DATES,
    final_close: Decimal = Decimal("10.00"),
    market_dates: tuple[date, ...] | None = None,
    market_opens: tuple[Decimal, ...] | None = None,
    market_closes: tuple[Decimal, ...] | None = None,
    signal_date: date = OFFICIAL_DATES[0],
    end_date: date = OFFICIAL_DATES[1],
    max_entry_attempts: int = 5,
    action_coverage_start: date = OFFICIAL_DATES[0],
    stock_commission_rate: Decimal = Decimal("0.00025"),
    corporate_action_events_by_symbol: (dict[str, tuple[CorporateActionEvent, ...]] | None) = None,
) -> dict[str, object]:
    """Build a small formal portfolio case without private verification tokens."""
    resolved_market_dates = OFFICIAL_DATES[:2] if market_dates is None else market_dates
    resolved_opens = (
        tuple(Decimal("10.00") for _ in resolved_market_dates)
        if market_opens is None
        else market_opens
    )
    resolved_closes = (
        (*resolved_opens[:-1], final_close) if market_closes is None else market_closes
    )
    inputs = tuple(
        PortfolioInstrumentInput(
            market_data=_verified_market(
                root / "market" / symbol,
                symbol,
                dates=resolved_market_dates,
                opens=resolved_opens,
                closes=resolved_closes,
            ),
            corporate_actions=make_synthetic_corporate_actions(
                (
                    ()
                    if corporate_action_events_by_symbol is None
                    else corporate_action_events_by_symbol.get(symbol, ())
                ),
                symbol=symbol,
                instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
                coverage_start=action_coverage_start,
                coverage_end=calendar_dates[-1],
            ),
        )
        for symbol in symbols
    )
    commission = CommissionAssumption(
        rate=stock_commission_rate,
        minimum_yuan=Decimal("5.00"),
    )
    return {
        "config": PortfolioConfig(
            strategy=PortfolioStrategy.BUY_AND_HOLD,
            initial_cash_fen=initial_cash_fen,
            gross_target_weight=gross_target_weight,
            signal_date=signal_date,
            end_date=end_date,
            max_entry_attempts=max_entry_attempts,
        ),
        "inputs": inputs,
        "universe": _verified_universe(root / "universe", symbols),
        "calendar": _calendar(root / "calendar", calendar_dates),
        "fee_policy": make_fee_policy(
            stock_commission=commission,
            etf_commission=CommissionAssumption(
                rate=Decimal("0.00025"),
                minimum_yuan=Decimal("5.00"),
            ),
        ),
    }


@dataclass(frozen=True)
class MaterializedPortfolioCliCase:
    project_root: Path
    arguments: tuple[str, ...]
    expected_run_id: str
    expected_relative_artifact: str
    market_snapshot_ids: tuple[tuple[str, str], ...]
    action_snapshot_ids: tuple[tuple[str, str], ...]


def write_gate_e_cli_config(
    project_root: Path,
    payload: dict[str, object],
) -> Path:
    """Write one canonical test config under the formal relative path."""
    path = project_root / "configs/releases/v0.2_gate_e.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_config_bytes(payload))
    return path


def materialize_portfolio_cli_case(
    project_root: Path,
    *,
    symbols: tuple[str, ...] = ("600000", "600001"),
) -> MaterializedPortfolioCliCase:
    """Publish a complete two-symbol offline CLI fixture."""
    project_root = project_root.resolve()
    market_records: dict[str, ManifestRecord] = {}
    market_inputs = []
    for symbol in sorted(symbols):
        artifact = RawSnapshotStore(project_root).write(
            _raw_market_frame(
                OFFICIAL_DATES[:2],
                opens=(Decimal("10"), Decimal("10")),
                closes=(Decimal("10"), Decimal("10")),
            ),
            symbol=symbol,
            source_slug="eastmoney",
            snapshot_date=date(2026, 7, 22),
        )
        record = ManifestRecord.create(
            schema_version="1.0",
            symbol=symbol,
            instrument_kind=InstrumentKind.MAIN_BOARD_STOCK.value,
            provider="eastmoney",
            source_function="stock_zh_a_hist",
            source_schema="akshare.stock_zh_a_hist",
            endpoint_host="push2his.eastmoney.com",
            provider_symbol="sh" + symbol,
            fetched_at_utc=FIXED_FETCHED_AT,
            requested_start=OFFICIAL_DATES[0],
            requested_end=OFFICIAL_DATES[1],
            actual_start=OFFICIAL_DATES[0],
            actual_end=OFFICIAL_DATES[1],
            row_count=artifact.row_count,
            snapshot_relative_path=artifact.relative_path,
            file_sha256=artifact.sha256,
            adjustment="",
            factor_source=None,
            latest_market_date=OFFICIAL_DATES[1],
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
        ManifestWriter(project_root / "data/manifests/manifest.jsonl").append(record)
        market_records[symbol] = record
        market_inputs.append(load_verified_snapshot(project_root, record))

    action_records = {
        symbol: publish_corporate_actions(
            project_root,
            (),
            symbol=symbol,
            instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
            provider="synthetic",
            source_schema="synthetic.cash.v1",
            normalization_version="cash-only-v1",
            coverage_start=OFFICIAL_DATES[0],
            coverage_end=OFFICIAL_DATES[-1],
        )
        for symbol in sorted(symbols)
    }
    actions = {
        symbol: load_verified_corporate_actions(
            project_root,
            action_records[symbol],
        )
        for symbol in sorted(symbols)
    }
    calendar = _calendar(project_root, OFFICIAL_DATES)
    universe = _verified_universe(
        project_root / "configs/universes",
        symbols,
    )
    fee_policy = make_fee_policy(
        stock_commission=CommissionAssumption(
            rate=Decimal("0.00025"),
            minimum_yuan=Decimal("5"),
        ),
        etf_commission=CommissionAssumption(
            rate=Decimal("0.00025"),
            minimum_yuan=Decimal("5"),
        ),
    )
    config = PortfolioConfig(
        strategy=PortfolioStrategy.BUY_AND_HOLD,
        initial_cash_fen=2_000_000,
        gross_target_weight=Decimal("1"),
        signal_date=OFFICIAL_DATES[0],
        end_date=OFFICIAL_DATES[1],
        max_entry_attempts=5,
    )
    expected = run_verified_portfolio(
        config=config,
        inputs=tuple(
            PortfolioInstrumentInput(
                market_data=market,
                corporate_actions=actions[market.provenance.symbol],
            )
            for market in market_inputs
        ),
        universe=universe,
        calendar=calendar,
        fee_policy=fee_policy,
    )
    market_ids = tuple((symbol, market_records[symbol].snapshot_id) for symbol in sorted(symbols))
    action_ids = tuple((symbol, action_records[symbol].snapshot_id) for symbol in sorted(symbols))
    arguments = (
        "run",
        "--project-root",
        str(project_root),
        "--manifest",
        "data/manifests/manifest.jsonl",
        "--corporate-action-manifest",
        "data/corporate_actions/manifest.jsonl",
        "--output",
        "outputs/portfolios",
        "--calendar-id",
        calendar.calendar_id,
        "--universe-id",
        universe.universe_id,
        *(
            value
            for symbol, snapshot_id in reversed(market_ids)
            for value in (
                "--market-snapshot",
                f"{symbol}={snapshot_id}",
            )
        ),
        *(
            value
            for symbol, snapshot_id in reversed(action_ids)
            for value in (
                "--corporate-action-snapshot",
                f"{symbol}={snapshot_id}",
            )
        ),
        "--initial-cash-fen",
        "2000000",
        "--gross-target-weight",
        "1",
        "--signal-date",
        OFFICIAL_DATES[0].isoformat(),
        "--end-date",
        OFFICIAL_DATES[1].isoformat(),
        "--max-entry-attempts",
        "5",
        "--stock-commission-rate",
        "0.00025",
        "--stock-minimum-commission",
        "5",
        "--etf-commission-rate",
        "0.00025",
        "--etf-minimum-commission",
        "5",
    )
    return MaterializedPortfolioCliCase(
        project_root=project_root,
        arguments=arguments,
        expected_run_id=expected.identity.run_id,
        expected_relative_artifact=(f"outputs/portfolios/{expected.identity.run_id}"),
        market_snapshot_ids=market_ids,
        action_snapshot_ids=action_ids,
    )
