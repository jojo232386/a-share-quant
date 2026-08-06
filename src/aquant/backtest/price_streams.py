"""Derive causal indicator and exchange-reference prices from raw daily bars."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from aquant.data import DataQualityError, validate_market_frame
from aquant.data.corporate_actions import (
    CorporateActionError,
    VerifiedCorporateActions,
    verify_verified_corporate_actions,
)

PRICE_STREAM_VERSION = "causal-cash-v1"


def derive_price_streams(
    raw_frame: pd.DataFrame,
    actions: VerifiedCorporateActions,
) -> pd.DataFrame:
    """Return raw bars plus two derived lines without future corporate actions."""
    try:
        validate_market_frame(raw_frame)
    except DataQualityError as exc:
        raise CorporateActionError(
            "invalid_market_data",
            "price streams require canonical market data",
        ) from exc
    verify_verified_corporate_actions(actions)
    provenance = actions.provenance
    assert provenance is not None
    dates = tuple(raw_frame["date"].dt.date)
    date_set = frozenset(dates)
    if actions.events and provenance.symbol != actions.events[0].symbol:
        raise CorporateActionError(
            "corporate_action_identity_mismatch",
            "corporate-action provenance and events disagree",
        )
    for event in actions.events:
        if event.ex_date < dates[0] or event.ex_date > dates[-1]:
            continue
        if event.ex_date not in date_set:
            raise CorporateActionError(
                "corporate_action_date_missing_market_bar",
                "corporate-action ex-date has no market bar",
            )
        if (
            dates[0] <= event.payable_date <= dates[-1]
            and event.payable_date not in date_set
        ):
            raise CorporateActionError(
                "corporate_action_payable_date_missing_market_bar",
                "corporate-action payable date has no auditable market bar",
            )
        if (
            event.stock_dividend_ratio != 0
            or event.capitalization_ratio != 0
            or event.rights_ratio != 0
            or event.rights_price is not None
        ):
            raise CorporateActionError(
                "unsupported_corporate_action",
                "price-stream v1 supports cash dividends only",
            )

    cash_by_date: dict[object, Decimal] = {}
    for event in actions.events:
        if event.ex_date < dates[0] or event.ex_date > dates[-1]:
            continue
        cash_by_date[event.ex_date] = (
            cash_by_date.get(event.ex_date, Decimal("0"))
            + event.cash_dividend_per_unit
        )

    references: list[float] = [float("nan")]
    indicators: list[float] = [float(raw_frame["close"].iloc[0])]
    factor = Decimal("1")
    for position in range(1, len(raw_frame)):
        current_date = dates[position]
        previous_close = Decimal(str(raw_frame["close"].iloc[position - 1]))
        cash = cash_by_date.get(current_date, Decimal("0"))
        reference = previous_close - cash
        if reference <= 0:
            raise CorporateActionError(
                "nonpositive_reference_price",
                "corporate action produced a non-positive reference price",
            )
        if cash:
            factor *= previous_close / reference
        references.append(float(reference))
        indicators.append(
            float(Decimal(str(raw_frame["close"].iloc[position])) * factor)
        )
    first_date = dates[0]
    if first_date in cash_by_date:
        raise CorporateActionError(
            "missing_previous_close",
            "first-bar corporate action has no previous raw close",
        )

    enriched = raw_frame.copy(deep=True)
    enriched["indicator_close"] = pd.Series(indicators, dtype="float64")
    enriched["reference_price"] = pd.Series(references, dtype="float64")
    return enriched
