from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from aquant.backtest import load_verified_snapshot
from aquant.data.corporate_actions import (
    load_verified_corporate_actions,
    publish_corporate_actions,
)
from aquant.data.manifest import ManifestRecord
from aquant.data.snapshot import RawSnapshotStore
from aquant.portfolio import (
    PortfolioConfig,
    PortfolioError,
    PortfolioInstrumentInput,
    PortfolioStrategy,
    allocate_equal_targets,
    validate_portfolio_inputs,
)
from aquant.rules import InstrumentKind
from aquant.universe import (
    UniverseMember,
    canonical_universe_bytes,
    load_verified_universe,
)


def valid_config(**changes: object) -> PortfolioConfig:
    values = {
        "strategy": PortfolioStrategy.BUY_AND_HOLD,
        "initial_cash_fen": 100_000_000,
        "gross_target_weight": Decimal("0.95"),
        "signal_date": date(2025, 1, 2),
        "end_date": date(2026, 7, 22),
        "max_entry_attempts": 5,
    }
    values.update(changes)
    return PortfolioConfig(**values)


def test_formal_portfolio_config_is_exact_and_immutable():
    config = valid_config()

    assert config.initial_cash_fen == 100_000_000
    assert config.gross_target_weight == Decimal("0.95")
    assert config.max_entry_attempts == 5
    with pytest.raises(AttributeError):
        config.initial_cash_fen = 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strategy", "buy_and_hold"),
        ("initial_cash_fen", True),
        ("initial_cash_fen", 0),
        ("initial_cash_fen", -1),
        ("gross_target_weight", 0.95),
        ("gross_target_weight", Decimal("NaN")),
        ("gross_target_weight", Decimal("0")),
        ("gross_target_weight", Decimal("1.0001")),
        ("signal_date", datetime(2025, 1, 2)),
        ("end_date", "2026-07-22"),
        ("max_entry_attempts", True),
        ("max_entry_attempts", 0),
        ("max_entry_attempts", 21),
    ],
)
def test_portfolio_config_rejects_implicit_or_out_of_range_values(field, value):
    with pytest.raises(PortfolioError) as captured:
        valid_config(**{field: value})

    assert captured.value.code == "invalid_config"


def test_portfolio_config_requires_end_after_signal():
    with pytest.raises(PortfolioError) as captured:
        valid_config(end_date=date(2025, 1, 2))

    assert captured.value.code == "invalid_config"


@pytest.mark.parametrize("member_count", [1, 2, 3, 10])
def test_equal_target_allocation_conserves_every_fen(member_count):
    config = valid_config(initial_cash_fen=100_000_003)

    allocation = allocate_equal_targets(config, member_count)

    assert (
        allocation.gross_target_notional_fen
        == allocation.per_symbol_target_notional_fen * member_count
        + allocation.allocation_rounding_remainder_fen
    )
    assert (
        allocation.gross_target_notional_fen
        + allocation.planned_cash_reserve_fen
        == config.initial_cash_fen
    )


def test_formal_ten_member_allocation_uses_target_notional_not_cost_budget():
    allocation = allocate_equal_targets(valid_config(), 10)

    assert allocation.gross_target_notional_fen == 95_000_000
    assert allocation.per_symbol_target_notional_fen == 9_500_000
    assert allocation.planned_cash_reserve_fen == 5_000_000
    assert allocation.allocation_rounding_remainder_fen == 0


def test_allocation_exposes_nonzero_member_rounding_remainder():
    allocation = allocate_equal_targets(
        valid_config(initial_cash_fen=101, gross_target_weight=Decimal("0.95")),
        3,
    )

    assert allocation.gross_target_notional_fen == 95
    assert allocation.per_symbol_target_notional_fen == 31
    assert allocation.allocation_rounding_remainder_fen == 2
    assert allocation.planned_cash_reserve_fen == 6


@pytest.mark.parametrize("member_count", [True, 0, -1, 101, 1.0])
def test_allocation_rejects_invalid_member_count(member_count):
    with pytest.raises(PortfolioError) as captured:
        allocate_equal_targets(valid_config(), member_count)

    assert captured.value.code == "invalid_member_count"


def test_allocation_rejects_config_subclass():
    class ConfigSubclass(PortfolioConfig):
        pass

    forged = ConfigSubclass(
        strategy=PortfolioStrategy.BUY_AND_HOLD,
        initial_cash_fen=100_000_000,
        gross_target_weight=Decimal("0.95"),
        signal_date=date(2025, 1, 2),
        end_date=date(2026, 7, 22),
        max_entry_attempts=5,
    )

    with pytest.raises(TypeError, match="exact PortfolioConfig"):
        allocate_equal_targets(forged, 10)


def test_replace_still_revalidates_config():
    with pytest.raises(PortfolioError):
        replace(valid_config(), gross_target_weight=Decimal("0"))


def _raw_market_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": ["2026-07-13", "2026-07-14"],
            "开盘": [10.0, 11.0],
            "最高": [10.8, 11.8],
            "最低": [9.8, 10.8],
            "收盘": [10.5, 11.5],
            "成交量": [100, 120],
            "成交额": [105_000.0, 138_000.0],
        }
    )


def _verified_market(root: Path, symbol: str):
    artifact = RawSnapshotStore(root).write(
        _raw_market_frame(),
        symbol=symbol,
        source_slug="eastmoney",
        snapshot_date=date(2026, 7, 15),
    )
    record = ManifestRecord.create(
        schema_version="1.0",
        symbol=symbol,
        instrument_kind="main_board_stock",
        provider="eastmoney",
        source_function="stock_zh_a_hist",
        source_schema="akshare.stock_zh_a_hist",
        endpoint_host="push2his.eastmoney.com",
        provider_symbol="sh" + symbol,
        fetched_at_utc=datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
        requested_start=date(2026, 7, 13),
        requested_end=date(2026, 7, 14),
        actual_start=date(2026, 7, 13),
        actual_end=date(2026, 7, 14),
        row_count=artifact.row_count,
        snapshot_relative_path=artifact.relative_path,
        file_sha256=artifact.sha256,
        adjustment="",
        factor_source=None,
        latest_market_date=date(2026, 7, 14),
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


def _verified_actions(root: Path, symbol: str):
    root.mkdir(parents=True, exist_ok=True)
    record = publish_corporate_actions(
        root,
        (),
        symbol=symbol,
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        provider="synthetic",
        source_schema="synthetic.cash.v1",
        normalization_version="cash-only-v1",
        coverage_start=date(2026, 7, 13),
        coverage_end=date(2026, 7, 14),
    )
    return load_verified_corporate_actions(root, record)


def _verified_universe(root: Path, symbols: tuple[str, ...]):
    members = tuple(UniverseMember(symbol, "main_board_stock") for symbol in symbols)
    content = canonical_universe_bytes("portfolio-test", members)
    universe_id = hashlib.sha256(content).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{universe_id}.json"
    path.write_bytes(content)
    return load_verified_universe(path, expected_id=universe_id)


def _instrument_input(root: Path, symbol: str) -> PortfolioInstrumentInput:
    return PortfolioInstrumentInput(
        market_data=_verified_market(root / "market", symbol),
        corporate_actions=_verified_actions(root / "actions", symbol),
    )


def test_portfolio_instrument_input_accepts_only_matching_verified_objects(tmp_path):
    item = _instrument_input(tmp_path, "600519")

    assert item.symbol == "600519"
    assert item.instrument_kind is InstrumentKind.MAIN_BOARD_STOCK


def test_portfolio_instrument_input_rejects_naked_or_mismatched_objects(tmp_path):
    market = _verified_market(tmp_path / "market", "600519")
    actions = _verified_actions(tmp_path / "actions", "601318")

    with pytest.raises(TypeError, match="VerifiedMarketData"):
        PortfolioInstrumentInput(
            market_data=market.frame,
            corporate_actions=actions,
        )
    with pytest.raises(TypeError, match="VerifiedCorporateActions"):
        PortfolioInstrumentInput(
            market_data=market,
            corporate_actions={},
        )
    with pytest.raises(PortfolioError) as captured:
        PortfolioInstrumentInput(
            market_data=market,
            corporate_actions=actions,
        )
    assert captured.value.code == "input_contract_mismatch"


def test_portfolio_instrument_input_rejects_kind_or_coverage_mismatch(tmp_path):
    market = _verified_market(tmp_path / "market", "600519")
    actions = _verified_actions(tmp_path / "actions", "600519")
    object.__setattr__(
        market.provenance,
        "instrument_kind",
        InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF,
    )

    with pytest.raises(PortfolioError) as captured:
        PortfolioInstrumentInput(market_data=market, corporate_actions=actions)

    assert captured.value.code == "input_contract_mismatch"


def test_public_input_boundary_rechecks_mutated_market_digest(tmp_path):
    item = _instrument_input(tmp_path / "item", "600519")
    universe = _verified_universe(tmp_path / "universe", ("600519",))
    damaged = item.market_data.frame
    damaged.loc[0, "close"] = 999.0
    damaged.loc[0, "high"] = 1000.0
    object.__setattr__(item.market_data, "_frame", damaged)

    with pytest.raises(PortfolioError) as captured:
        validate_portfolio_inputs((item,), universe=universe)

    assert captured.value.code == "verified_market_data_modified"


def test_public_input_boundary_rechecks_mutated_market_provenance(tmp_path):
    item = _instrument_input(tmp_path / "item", "600519")
    universe = _verified_universe(tmp_path / "universe", ("600519",))
    object.__setattr__(item.market_data.provenance, "snapshot_id", "f" * 64)

    with pytest.raises(PortfolioError) as captured:
        validate_portfolio_inputs((item,), universe=universe)

    assert captured.value.code == "verified_market_data_modified"


def test_public_input_boundary_rechecks_mutated_corporate_actions(tmp_path):
    item = _instrument_input(tmp_path / "item", "600519")
    universe = _verified_universe(tmp_path / "universe", ("600519",))
    item.corporate_actions._events = ("forged",)

    with pytest.raises(PortfolioError) as captured:
        validate_portfolio_inputs((item,), universe=universe)

    assert captured.value.code == "verified_corporate_actions_modified"


def test_portfolio_inputs_are_complete_unique_and_sorted_by_symbol(tmp_path):
    first = _instrument_input(tmp_path / "first", "600519")
    second = _instrument_input(tmp_path / "second", "601318")
    universe = _verified_universe(tmp_path / "universe", ("600519", "601318"))

    validated = validate_portfolio_inputs((second, first), universe=universe)

    assert tuple(item.symbol for item in validated) == ("600519", "601318")
    with pytest.raises(PortfolioError, match="duplicate"):
        validate_portfolio_inputs((first, first), universe=universe)
    with pytest.raises(PortfolioError, match="exactly match"):
        validate_portfolio_inputs((first,), universe=universe)


def test_portfolio_inputs_reject_naked_container_item_and_forged_universe(tmp_path):
    item = _instrument_input(tmp_path / "item", "600519")
    universe = _verified_universe(tmp_path / "universe", ("600519",))

    with pytest.raises(TypeError, match="exact tuple"):
        validate_portfolio_inputs([item], universe=universe)
    with pytest.raises(TypeError, match="PortfolioInstrumentInput"):
        validate_portfolio_inputs((item.market_data,), universe=universe)
    with pytest.raises(PortfolioError) as captured:
        validate_portfolio_inputs((item,), universe=object())
    assert captured.value.code == "unverified_universe"
