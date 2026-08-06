"""Fail-closed adapter from canonical daily data to Backtrader."""

from __future__ import annotations

import hashlib
import math

import backtrader as bt
import pandas as pd

from aquant.data import DataQualityError, validate_market_frame
from aquant.data.normalize import REQUIRED_MARKET_COLUMNS

RESEARCH_PRICE_COLUMNS = ("indicator_close", "reference_price")


class BacktestDataError(ValueError):
    """Raised when market data is unsafe for the Week 2 broker."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def prepare_feed_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate without repair, then return an isolated Backtrader view."""
    try:
        validate_market_frame(frame)
    except DataQualityError as exc:
        raise BacktestDataError(
            "invalid_market_data",
            f"canonical market data failed the quality gate: {exc.code}",
        ) from exc

    prepared = frame.loc[:, REQUIRED_MARKET_COLUMNS].copy()
    prepared = prepared.set_index("date", drop=True)
    prepared.index.name = "date"
    return prepared


def canonical_market_digest(frame: pd.DataFrame) -> str:
    """Hash the validated canonical values and dates consumed by Backtrader."""
    prepared = prepare_feed_frame(frame)
    row_hashes = pd.util.hash_pandas_object(prepared, index=True)
    return hashlib.sha256(row_hashes.to_numpy().tobytes()).hexdigest()


def make_backtrader_feed(frame: pd.DataFrame, *, name: str) -> bt.feeds.PandasData:
    """Build a daily feed with explicit canonical-column mappings."""
    prepared = prepare_feed_frame(frame)
    return bt.feeds.PandasData(
        dataname=prepared,
        name=name,
        timeframe=bt.TimeFrame.Days,
        datetime=None,
        open="open",
        high="high",
        low="low",
        close="close",
        volume="volume",
        openinterest=-1,
    )


class ResearchPandasData(bt.feeds.PandasData):
    """Raw execution bars with separate indicator and price-limit lines."""

    lines = RESEARCH_PRICE_COLUMNS
    params = (
        ("indicator_close", "indicator_close"),
        ("reference_price", "reference_price"),
    )


def prepare_research_feed_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if not all(column in frame.columns for column in RESEARCH_PRICE_COLUMNS):
        raise BacktestDataError(
            "missing_price_streams",
            "formal research feeds require indicator and reference prices",
        )
    prepared = prepare_feed_frame(frame)
    for column in RESEARCH_PRICE_COLUMNS:
        values = pd.to_numeric(frame[column], errors="coerce")
        if column == "reference_price":
            valid = values.iloc[0:].copy()
            if not pd.isna(valid.iloc[0]):
                raise BacktestDataError(
                    "invalid_price_streams",
                    "the first reference price must be missing",
                )
            valid = valid.iloc[1:]
        else:
            valid = values
        if (
            valid.isna().any()
            or not valid.map(math.isfinite).all()
            or (valid <= 0).any()
        ):
            raise BacktestDataError(
                "invalid_price_streams",
                "derived price streams must be finite and positive",
            )
        prepared[column] = values.to_numpy()
    return prepared


def make_research_feed(frame: pd.DataFrame, *, name: str) -> ResearchPandasData:
    prepared = prepare_research_feed_frame(frame)
    return ResearchPandasData(
        dataname=prepared,
        name=name,
        timeframe=bt.TimeFrame.Days,
        datetime=None,
        open="open",
        high="high",
        low="low",
        close="close",
        volume="volume",
        openinterest=-1,
        indicator_close="indicator_close",
        reference_price="reference_price",
    )
