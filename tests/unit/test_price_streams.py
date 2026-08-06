from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from aquant.backtest.feed import (
    BacktestDataError,
    make_research_feed,
    prepare_research_feed_frame,
)
from aquant.backtest.price_streams import (
    PRICE_STREAM_VERSION,
    derive_price_streams,
)
from aquant.data.corporate_actions import (
    CorporateActionError,
    CorporateActionEvent,
    load_verified_corporate_actions,
    publish_corporate_actions,
)
from aquant.rules import InstrumentKind


def _raw_frame(closes=(100.0, 98.0, 99.0)) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "open": list(closes),
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": list(closes),
            "volume": [10_000, 10_000, 10_000],
            "amount": [value * 10_000 for value in closes],
        }
    )


def _event(*, ex_date=date(2024, 1, 3), cash=Decimal("2")):
    return CorporateActionEvent.create(
        symbol="600519",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        announcement_date=date(2023, 12, 20),
        record_date=date(2024, 1, 2),
        ex_date=ex_date,
        payable_date=max(ex_date, date(2024, 1, 4)),
        cash_dividend_per_unit=cash,
        stock_dividend_ratio=Decimal("0"),
        capitalization_ratio=Decimal("0"),
        rights_ratio=Decimal("0"),
        rights_price=None,
        source_schema="synthetic.cash.v1",
        source_url="https://example.invalid/corporate-actions",
    )


def _verified(tmp_path, events=None):
    chosen = (_event(),) if events is None else events
    coverage_end = max(
        [date(2024, 1, 4)]
        + [max(event.ex_date, event.payable_date) for event in chosen]
    )
    record = publish_corporate_actions(
        tmp_path,
        chosen,
        symbol="600519",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        provider="synthetic",
        source_schema="synthetic.cash.v1",
        normalization_version="cash-only-v1",
        coverage_start=date(2024, 1, 2),
        coverage_end=coverage_end,
    )
    return load_verified_corporate_actions(tmp_path, record)


def test_cash_dividend_derives_causal_indicator_and_daily_reference_prices(tmp_path):
    enriched = derive_price_streams(_raw_frame(), _verified(tmp_path))

    assert PRICE_STREAM_VERSION == "causal-cash-v1"
    assert pd.isna(enriched.loc[0, "reference_price"])
    assert enriched.loc[1, "reference_price"] == pytest.approx(98.0)
    assert enriched.loc[2, "reference_price"] == pytest.approx(98.0)
    assert enriched.loc[0, "indicator_close"] == pytest.approx(100.0)
    assert enriched.loc[1, "indicator_close"] == pytest.approx(100.0)
    assert enriched.loc[2, "indicator_close"] == pytest.approx(99.0 * 100.0 / 98.0)
    assert enriched["close"].tolist() == [100.0, 98.0, 99.0]


def test_no_actions_preserve_raw_indicator_prices(tmp_path):
    enriched = derive_price_streams(_raw_frame(), _verified(tmp_path, events=()))

    assert enriched["indicator_close"].tolist() == [100.0, 98.0, 99.0]
    assert enriched["reference_price"].iloc[1:].tolist() == [100.0, 98.0]


@pytest.mark.parametrize(
    ("events", "frame", "code"),
        [
            (
                (_event(ex_date=date(2024, 1, 3)),),
                _raw_frame().assign(
                    date=pd.to_datetime(["2024-01-02", "2024-01-04", "2024-01-05"])
                ),
                "corporate_action_date_missing_market_bar",
            ),
        (
            (_event(ex_date=date(2024, 1, 2)),),
            _raw_frame(),
            "missing_previous_close",
        ),
        (
            (_event(cash=Decimal("100")),),
            _raw_frame(),
            "nonpositive_reference_price",
        ),
        (
            (
                _event(
                    ex_date=date(2024, 1, 3),
                    cash=Decimal("2"),
                ),
            ),
            _raw_frame().assign(
                date=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-05"])
            ),
            "corporate_action_payable_date_missing_market_bar",
        ),
    ],
)
def test_price_streams_fail_closed_on_unsafe_event_alignment(
    tmp_path, events, frame, code
):
    verified = _verified(tmp_path, events=events)

    with pytest.raises(CorporateActionError) as captured:
        derive_price_streams(frame, verified)

    assert captured.value.code == code


def test_price_streams_detect_modified_verified_actions(tmp_path):
    verified = _verified(tmp_path)
    object.__setattr__(verified, "_events", ())

    with pytest.raises(CorporateActionError) as captured:
        derive_price_streams(_raw_frame(), verified)

    assert captured.value.code == "verified_corporate_actions_modified"


def test_research_feed_requires_both_derived_lines():
    with pytest.raises(BacktestDataError) as captured:
        make_research_feed(_raw_frame(), name="600519")

    assert captured.value.code == "missing_price_streams"


def test_research_feed_keeps_raw_close_and_exposes_derived_lines(tmp_path):
    feed = make_research_feed(
        derive_price_streams(_raw_frame(), _verified(tmp_path)),
        name="600519",
    )

    assert feed.p.name == "600519"
    assert feed.lines.getlinealiases()[-2:] == (
        "indicator_close",
        "reference_price",
    )


@pytest.mark.parametrize("column", ["indicator_close", "reference_price"])
def test_research_feed_rejects_nonfinite_derived_prices(tmp_path, column):
    enriched = derive_price_streams(_raw_frame(), _verified(tmp_path))
    enriched.loc[1, column] = float("inf")

    with pytest.raises(BacktestDataError) as captured:
        prepare_research_feed_frame(enriched)

    assert captured.value.code == "invalid_price_streams"
