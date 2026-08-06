"""Fail-closed AKShare adapter with group-consistent source fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from types import ModuleType

import pandas as pd
import requests

from aquant.config import InstrumentConfig
from aquant.data.corporate_actions import (
    CorporateActionEvent,
    normalize_etf_dividends,
    normalize_stock_dividends,
    parse_sina_etf_dividend_detail,
)
from aquant.data.normalize import SourceSchema
from aquant.logging import log_event
from aquant.rules import InstrumentKind
from aquant.universe import is_supported_instrument_identity


@dataclass(frozen=True)
class Attempt:
    """Safe metadata for one provider call; never stores exception text."""

    group: str
    symbol: str
    provider: str
    source_function: str
    provider_symbol: str
    status: str
    exception_type: str | None = None


class FetchError(RuntimeError):
    """Raised when a complete source-consistent batch cannot be fetched."""

    def __init__(self, code: str, message: str, attempts: tuple[Attempt, ...]):
        self.code = code
        self.attempts = attempts
        super().__init__(message)


class SourceContractError(ValueError):
    """Raised when fetch provenance does not match a supported source contract."""

    code = "source_contract_violation"


@dataclass(frozen=True)
class RawFetchResult:
    """An upstream frame plus the provenance needed for normalization."""

    symbol: str
    instrument_kind: str
    frame: pd.DataFrame
    provider: str
    source_function: str
    source_schema: SourceSchema
    endpoint_host: str
    provider_symbol: str
    raw_volume_unit: str
    volume_multiplier_to_canonical: int
    full_history_download: bool
    local_date_slice: bool

    def __post_init__(self) -> None:
        validate_raw_fetch_result(self)


class _EmptyFrameError(ValueError):
    pass


@dataclass(frozen=True)
class _Source:
    provider: str
    source_function: str
    source_schema: SourceSchema
    endpoint_host: str
    raw_volume_unit: str
    volume_multiplier: int
    full_history_download: bool
    local_date_slice: bool


_SOURCES = {
    ("etf", "eastmoney"): _Source(
        "eastmoney",
        "fund_etf_hist_em",
        SourceSchema.ETF_EASTMONEY,
        "push2his.eastmoney.com",
        "lot",
        100,
        False,
        False,
    ),
    ("etf", "sina"): _Source(
        "sina",
        "fund_etf_hist_sina",
        SourceSchema.ETF_SINA,
        "finance.sina.com.cn",
        "unit",
        1,
        True,
        True,
    ),
    ("stock", "eastmoney"): _Source(
        "eastmoney",
        "stock_zh_a_hist",
        SourceSchema.STOCK_EASTMONEY,
        "push2his.eastmoney.com",
        "lot",
        100,
        False,
        False,
    ),
    ("stock", "sina"): _Source(
        "sina",
        "stock_zh_a_daily",
        SourceSchema.STOCK_SINA,
        "finance.sina.com.cn",
        "share",
        1,
        True,
        True,
    ),
}


def _provider_symbol(symbol: str) -> str:
    prefix = "sh" if symbol.startswith(("5", "6")) else "sz"
    return prefix + symbol


def validate_source_contract(
    *,
    symbol: object,
    instrument_kind: object,
    provider: object,
    source_function: object,
    source_schema: object,
    endpoint_host: object,
    provider_symbol: object,
    raw_volume_unit: object,
    volume_multiplier_to_canonical: object,
    full_history_download: object,
    local_date_slice: object,
) -> None:
    """Validate the complete fixed-universe source identity at every trust boundary."""
    if (
        type(symbol) is not str
        or type(instrument_kind) is not str
        or type(provider) is not str
    ):
        raise SourceContractError("fetch result identity contract is invalid")
    if not is_supported_instrument_identity(symbol, instrument_kind):
        raise SourceContractError("fetch result instrument contract is invalid")
    if provider == SourceSchema.SYNTHETIC_PUBLIC_FIXTURE.value:
        expected = (
            "deterministic_ohlcv_v1",
            SourceSchema.SYNTHETIC_PUBLIC_FIXTURE,
            "synthetic-public-fixture.invalid",
            f"fixture-{symbol}",
            "unit",
            1,
            True,
            False,
        )
        actual = (
            source_function,
            source_schema,
            endpoint_host,
            provider_symbol,
            raw_volume_unit,
            volume_multiplier_to_canonical,
            full_history_download,
            local_date_slice,
        )
        if actual != expected or any(
            type(actual_value) is not type(expected_value)
            for actual_value, expected_value in zip(actual, expected, strict=True)
        ):
            raise SourceContractError("synthetic fixture source contract is invalid")
        return
    group = "etf" if instrument_kind.endswith("_etf") else "stock"
    source = _SOURCES.get((group, provider))
    if source is None:
        raise SourceContractError("fetch result provider contract is invalid")
    expected = (
        source.source_function,
        source.source_schema,
        source.endpoint_host,
        _provider_symbol(symbol),
        source.raw_volume_unit,
        source.volume_multiplier,
        source.full_history_download,
        source.local_date_slice,
    )
    actual = (
        source_function,
        source_schema,
        endpoint_host,
        provider_symbol,
        raw_volume_unit,
        volume_multiplier_to_canonical,
        full_history_download,
        local_date_slice,
    )
    if actual != expected or any(
        type(actual_value) is not type(expected_value)
        for actual_value, expected_value in zip(actual, expected, strict=True)
    ):
        raise SourceContractError("fetch result source contract is invalid")


def validate_raw_fetch_result(result: object) -> None:
    """Validate provenance without dispatching through an untrusted result object."""
    if type(result) is not RawFetchResult:
        raise SourceContractError("fetch result object contract is invalid")
    validate_source_contract(
        symbol=result.symbol,
        instrument_kind=result.instrument_kind,
        provider=result.provider,
        source_function=result.source_function,
        source_schema=result.source_schema,
        endpoint_host=result.endpoint_host,
        provider_symbol=result.provider_symbol,
        raw_volume_unit=result.raw_volume_unit,
        volume_multiplier_to_canonical=result.volume_multiplier_to_canonical,
        full_history_download=result.full_history_download,
        local_date_slice=result.local_date_slice,
    )


class AkshareClient:
    """Fetch one verified universe with one provider per asset group."""

    def __init__(
        self,
        akshare: ModuleType | object,
        *,
        logger: logging.Logger | None = None,
        http_get=None,
    ):
        self.akshare = akshare
        self.logger = logger
        self.http_get = http_get or requests.get

    def fetch_corporate_actions(
        self,
        instrument: InstrumentConfig,
        *,
        start: date,
        end: date,
    ) -> tuple[CorporateActionEvent, ...]:
        """Fetch and strictly normalize one supported instrument's cash events."""
        if (
            type(instrument) is not InstrumentConfig
            or not is_supported_instrument_identity(
                instrument.symbol,
                instrument.kind,
            )
            or type(start) is not date
            or type(end) is not date
            or start > end
        ):
            raise FetchError(
                "unsupported_instrument",
                "unsupported corporate-action instrument or range",
                (),
            )
        kind = InstrumentKind(instrument.kind)
        attempts: list[Attempt] = []
        try:
            if kind is InstrumentKind.MAIN_BOARD_STOCK:
                frame = self.akshare.stock_dividend_cninfo(symbol=instrument.symbol)
                attempts.append(
                    Attempt(
                        "corporate_actions",
                        instrument.symbol,
                        "cninfo",
                        "stock_dividend_cninfo",
                        instrument.symbol,
                        "success",
                    )
                )
                return normalize_stock_dividends(
                    frame,
                    symbol=instrument.symbol,
                    instrument_kind=kind,
                    coverage_start=start,
                    coverage_end=end,
                )

            cumulative = self.akshare.fund_etf_dividend_sina(
                symbol=_provider_symbol(instrument.symbol)
            )
            url = (
                "https://stock.finance.sina.com.cn/fundInfo/view/"
                f"FundInfo_JJFH.php?symbol={instrument.symbol}"
            )
            response = self.http_get(url, timeout=15)
            if type(response.status_code) is not int or response.status_code != 200:
                raise RuntimeError("ETF detail HTTP status is not successful")
            detail = parse_sina_etf_dividend_detail(response.content)
            attempts.append(
                Attempt(
                    "corporate_actions",
                    instrument.symbol,
                    "sina",
                    "fund_etf_dividend_sina+FundInfo_JJFH",
                    _provider_symbol(instrument.symbol),
                    "success",
                )
            )
            return normalize_etf_dividends(
                cumulative,
                detail,
                symbol=instrument.symbol,
                instrument_kind=kind,
                coverage_start=start,
                coverage_end=end,
            )
        except Exception as exc:
            if not attempts or attempts[-1].status == "success":
                attempts.append(
                    Attempt(
                        "corporate_actions",
                        instrument.symbol,
                        "sina" if instrument.kind.endswith("_etf") else "cninfo",
                        (
                            "fund_etf_dividend_sina+FundInfo_JJFH"
                            if instrument.kind.endswith("_etf")
                            else "stock_dividend_cninfo"
                        ),
                        _provider_symbol(instrument.symbol),
                        "failed",
                        type(exc).__name__,
                    )
                )
            raise FetchError(
                "corporate_action_source_failed",
                "corporate-action acquisition or validation failed",
                tuple(attempts),
            ) from exc

    def fetch_batch(
        self,
        instruments: tuple[InstrumentConfig, ...],
        *,
        start: date,
        end: date,
    ) -> tuple[RawFetchResult, ...]:
        if type(start) is not date or type(end) is not date or start > end:
            raise FetchError("invalid_range", "fetch date range is invalid", ())
        checked = tuple(instruments)
        for item in checked:
            if (
                type(item) is not InstrumentConfig
                or not is_supported_instrument_identity(item.symbol, item.kind)
            ):
                raise FetchError("unsupported_instrument", "unsupported instrument", ())

        groups = (
            ("etf", tuple(item for item in checked if item.kind.endswith("_etf"))),
            ("stock", tuple(item for item in checked if item.kind == "main_board_stock")),
        )
        attempts: list[Attempt] = []
        by_symbol: dict[str, RawFetchResult] = {}
        for group_name, group_items in groups:
            if not group_items:
                continue
            primary_results, primary_ok = self._fetch_group(
                group_name,
                group_items,
                "eastmoney",
                start=start,
                end=end,
                attempts=attempts,
            )
            if primary_ok:
                by_symbol.update((item.symbol, item) for item in primary_results)
                continue
            fallback_results, fallback_ok = self._fetch_group(
                group_name,
                group_items,
                "sina",
                start=start,
                end=end,
                attempts=attempts,
            )
            if not fallback_ok:
                raise FetchError(
                    "source_group_failed",
                    "market data source group failed",
                    tuple(attempts),
                )
            by_symbol.update((item.symbol, item) for item in fallback_results)
        return tuple(by_symbol[item.symbol] for item in checked)

    def _fetch_group(
        self,
        group: str,
        instruments: tuple[InstrumentConfig, ...],
        provider: str,
        *,
        start: date,
        end: date,
        attempts: list[Attempt],
    ) -> tuple[tuple[RawFetchResult, ...], bool]:
        source = _SOURCES[(group, provider)]
        results: list[RawFetchResult] = []
        for instrument in instruments:
            provider_symbol = _provider_symbol(instrument.symbol)
            try:
                frame = self._call(
                    source,
                    symbol=instrument.symbol,
                    provider_symbol=provider_symbol,
                    start=start,
                    end=end,
                )
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    raise _EmptyFrameError()
            except Exception as exc:  # provider libraries expose heterogeneous exceptions
                attempt = Attempt(
                    group=group,
                    symbol=instrument.symbol,
                    provider=provider,
                    source_function=source.source_function,
                    provider_symbol=provider_symbol,
                    status="failed",
                    exception_type=type(exc).__name__,
                )
                attempts.append(attempt)
                log_event(
                    self.logger,
                    logging.WARNING,
                    "market_data_fetch_failed",
                    group=group,
                    symbol=instrument.symbol,
                    provider=provider,
                    source_function=source.source_function,
                    exception_type=type(exc).__name__,
                )
                return (), False
            attempts.append(
                Attempt(
                    group=group,
                    symbol=instrument.symbol,
                    provider=provider,
                    source_function=source.source_function,
                    provider_symbol=provider_symbol,
                    status="succeeded",
                )
            )
            results.append(
                RawFetchResult(
                    symbol=instrument.symbol,
                    instrument_kind=instrument.kind,
                    frame=frame.copy(),
                    provider=source.provider,
                    source_function=source.source_function,
                    source_schema=source.source_schema,
                    endpoint_host=source.endpoint_host,
                    provider_symbol=provider_symbol,
                    raw_volume_unit=source.raw_volume_unit,
                    volume_multiplier_to_canonical=source.volume_multiplier,
                    full_history_download=source.full_history_download,
                    local_date_slice=source.local_date_slice,
                )
            )
        return tuple(results), True

    def _call(
        self,
        source: _Source,
        *,
        symbol: str,
        provider_symbol: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        function = getattr(self.akshare, source.source_function)
        start_text = start.strftime("%Y%m%d")
        end_text = end.strftime("%Y%m%d")
        if source.source_function == "fund_etf_hist_sina":
            return function(symbol=provider_symbol)
        if source.source_function == "stock_zh_a_daily":
            return function(
                symbol=provider_symbol,
                start_date=start_text,
                end_date=end_text,
                adjust="",
            )
        kwargs = {
            "symbol": symbol,
            "period": "daily",
            "start_date": start_text,
            "end_date": end_text,
            "adjust": "",
        }
        if source.source_function == "stock_zh_a_hist":
            kwargs["timeout"] = 15
        return function(
            **kwargs,
        )
