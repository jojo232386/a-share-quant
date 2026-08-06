from collections import defaultdict
from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from aquant.config import InstrumentConfig
from aquant.data.akshare_client import AkshareClient, FetchError, SourceContractError
from aquant.data.normalize import SourceSchema

INSTRUMENTS = (
    InstrumentConfig("510300", "domestic_equity_broad_based_etf"),
    InstrumentConfig("600519", "main_board_stock"),
    InstrumentConfig("601318", "main_board_stock"),
    InstrumentConfig("000001", "main_board_stock"),
)


def _em_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": ["2026-07-17"],
            "开盘": [10.0],
            "最高": [11.0],
            "最低": [9.0],
            "收盘": [10.5],
            "成交量": [10],
            "成交额": [10_500.0],
        }
    )


def _sina_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [date(2026, 7, 17)],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [1000],
            "amount": [10_500.0],
        }
    )


class FakeAkshare:
    def __init__(self, failures=()):
        self.failures = set(failures)
        self.calls = []
        self.counts = defaultdict(int)

    def _call(self, name, kwargs, frame):
        self.calls.append((name, kwargs))
        self.counts[name] += 1
        key = (name, self.counts[name])
        if name in self.failures or key in self.failures:
            raise RuntimeError("secret upstream detail")
        return frame.copy()

    def fund_etf_hist_em(self, **kwargs):
        return self._call("fund_etf_hist_em", kwargs, _em_frame())

    def fund_etf_hist_sina(self, **kwargs):
        return self._call("fund_etf_hist_sina", kwargs, _sina_frame())

    def stock_zh_a_hist(self, **kwargs):
        return self._call("stock_zh_a_hist", kwargs, _em_frame())

    def stock_zh_a_daily(self, **kwargs):
        return self._call("stock_zh_a_daily", kwargs, _sina_frame())

    def stock_dividend_cninfo(self, **kwargs):
        frame = pd.DataFrame(
            {
                "实施方案公告日期": ["2024-06-12"],
                "分红类型": ["年度分红"],
                "送股比例": [None],
                "转增比例": [None],
                "派息比例": [308.76],
                "股权登记日": ["2024-06-18"],
                "除权日": ["2024-06-19"],
                "派息日": ["2024-06-19"],
                "股份到账日": [None],
                "实施方案分红说明": ["10派308.76元"],
                "报告时间": ["2023年报"],
            }
        )
        return self._call("stock_dividend_cninfo", kwargs, frame)

    def fund_etf_dividend_sina(self, **kwargs):
        frame = pd.DataFrame(
            {
                "日期": ["2022-01-19", "2023-01-16", "2024-01-18"],
                "累计分红": [0.536, 0.600, 0.669],
            }
        )
        return self._call("fund_etf_dividend_sina", kwargs, frame)


class FakeResponse:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code


def _etf_detail_html():
    return """
    <table><thead><tr>
      <th>权益登记日</th><th>红利发放日</th><th>每份分红(元)</th>
    </tr></thead><tbody>
      <tr><td>2023/1/13</td><td>2023/1/19</td><td>0.064</td></tr>
      <tr><td>2024/1/17</td><td>2024/1/23</td><td>0.069</td></tr>
    </tbody></table>
    """.encode("gb18030")


def _fetch(fake):
    return AkshareClient(fake).fetch_batch(
        INSTRUMENTS, start=date(2018, 1, 1), end=date(2026, 7, 17)
    )


def test_eastmoney_success_uses_actual_signatures_and_one_source_per_group():
    fake = FakeAkshare()

    results = _fetch(fake)

    assert [item.provider_symbol for item in results] == [
        "sh510300",
        "sh600519",
        "sh601318",
        "sz000001",
    ]
    assert {item.provider for item in results} == {"eastmoney"}
    assert {item.raw_volume_unit for item in results} == {"lot"}
    assert {item.volume_multiplier_to_canonical for item in results} == {100}
    assert all(not item.full_history_download and not item.local_date_slice for item in results)
    assert fake.calls[0] == (
        "fund_etf_hist_em",
        {
            "symbol": "510300",
            "period": "daily",
            "start_date": "20180101",
            "end_date": "20260717",
            "adjust": "",
        },
    )
    assert fake.calls[1][0] == "stock_zh_a_hist"
    assert fake.calls[1][1]["symbol"] == "600519"
    assert fake.calls[1][1]["timeout"] == 15


def test_etf_probe_failure_short_circuits_group_to_sina_full_history():
    fake = FakeAkshare({"fund_etf_hist_em"})

    results = _fetch(fake)
    etf = results[0]

    assert [name for name, _ in fake.calls if name.startswith("fund_")] == [
        "fund_etf_hist_em",
        "fund_etf_hist_sina",
    ]
    assert etf.provider == "sina"
    assert etf.source_schema == SourceSchema.ETF_SINA
    assert etf.full_history_download is True
    assert etf.local_date_slice is True
    assert etf.raw_volume_unit == "unit"
    assert fake.calls[1] == ("fund_etf_hist_sina", {"symbol": "sh510300"})


def test_stock_probe_failure_uses_sina_for_all_stocks():
    fake = FakeAkshare({("stock_zh_a_hist", 1)})

    results = _fetch(fake)

    stocks = results[1:]
    assert all(item.provider == "sina" for item in stocks)
    assert all(item.full_history_download and item.local_date_slice for item in stocks)
    sina_calls = [kwargs for name, kwargs in fake.calls if name == "stock_zh_a_daily"]
    assert [kwargs["symbol"] for kwargs in sina_calls] == [
        "sh600519",
        "sh601318",
        "sz000001",
    ]
    assert all(
        kwargs
        == {
            "symbol": kwargs["symbol"],
            "start_date": "20180101",
            "end_date": "20260717",
            "adjust": "",
        }
        for kwargs in sina_calls
    )


def test_mid_group_eastmoney_failure_discards_results_and_refetches_whole_group():
    fake = FakeAkshare({("stock_zh_a_hist", 2)})

    results = _fetch(fake)

    assert [name for name, _ in fake.calls].count("stock_zh_a_hist") == 2
    assert [name for name, _ in fake.calls].count("stock_zh_a_daily") == 3
    assert all(item.provider == "sina" for item in results[1:])


@pytest.mark.parametrize("failure", ["stock_zh_a_daily", "fund_etf_hist_sina"])
def test_sina_failure_is_typed_and_does_not_leak_upstream_message(failure):
    primary = "stock_zh_a_hist" if failure.startswith("stock") else "fund_etf_hist_em"
    fake = FakeAkshare({primary, failure})

    with pytest.raises(FetchError) as error:
        _fetch(fake)

    assert error.value.code == "source_group_failed"
    assert "secret upstream detail" not in str(error.value)
    assert error.value.attempts
    assert all(attempt.exception_type in {None, "RuntimeError"} for attempt in error.value.attempts)


def test_empty_frame_is_a_failed_attempt_and_triggers_fallback():
    fake = FakeAkshare()
    fake.fund_etf_hist_em = lambda **kwargs: pd.DataFrame()

    results = _fetch(fake)

    assert results[0].provider == "sina"


def test_client_accepts_new_verified_universe_members_without_code_whitelist():
    fake = FakeAkshare()
    instruments = (
        InstrumentConfig("510500", "domestic_equity_broad_based_etf"),
        InstrumentConfig("600036", "main_board_stock"),
    )

    results = AkshareClient(fake).fetch_batch(
        instruments,
        start=date(2018, 1, 1),
        end=date(2026, 7, 17),
    )

    assert tuple(item.symbol for item in results) == ("510500", "600036")
    assert tuple(item.instrument_kind for item in results) == (
        "domestic_equity_broad_based_etf",
        "main_board_stock",
    )


@pytest.mark.parametrize(
    "instrument",
    [
        InstrumentConfig("not-six", "main_board_stock"),
        InstrumentConfig("000002", "unsupported_kind"),
    ],
)
def test_client_rejects_invalid_instrument_identity_before_provider_call(instrument):
    fake = FakeAkshare()

    with pytest.raises(FetchError, match="unsupported instrument"):
        AkshareClient(fake).fetch_batch(
            (instrument,),
            start=date(2018, 1, 1),
            end=date(2026, 7, 17),
        )

    assert fake.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"instrument_kind": "domestic_equity_broad_based_etf"},
        {"provider": "sina"},
        {"source_function": "stock_zh_a_daily"},
        {"source_schema": SourceSchema.STOCK_SINA},
        {"endpoint_host": "finance.sina.com.cn"},
        {"raw_volume_unit": "share"},
        {"volume_multiplier_to_canonical": 1},
        {"volume_multiplier_to_canonical": 100.0},
        {"full_history_download": 0},
        {"full_history_download": True, "local_date_slice": True},
        {"provider_symbol": "sz600519"},
    ],
)
def test_raw_fetch_result_rejects_mixed_or_forged_source_contract(changes):
    eastmoney_stock = _fetch(FakeAkshare())[1]

    with pytest.raises(SourceContractError):
        replace(eastmoney_stock, **changes)


def test_client_fetches_and_normalizes_stock_corporate_actions():
    fake = FakeAkshare()
    client = AkshareClient(fake, http_get=lambda *args, **kwargs: None)

    events = client.fetch_corporate_actions(
        INSTRUMENTS[1],
        start=date(2018, 1, 1),
        end=date(2026, 7, 17),
    )

    assert len(events) == 1
    assert str(events[0].cash_dividend_per_unit) == "30.876"
    assert fake.calls[-1] == ("stock_dividend_cninfo", {"symbol": "600519"})


def test_client_fetches_etf_detail_with_timeout_and_no_secret_error_leak():
    calls = []

    def http_get(url, *, timeout):
        calls.append((url, timeout))
        return FakeResponse(_etf_detail_html())

    fake = FakeAkshare()
    client = AkshareClient(fake, http_get=http_get)
    events = client.fetch_corporate_actions(
        INSTRUMENTS[0],
        start=date(2023, 1, 1),
        end=date(2024, 12, 31),
    )

    assert [str(event.cash_dividend_per_unit) for event in events] == [
        "0.064",
        "0.069",
    ]
    assert calls == [
        (
            "https://stock.finance.sina.com.cn/fundInfo/view/"
            "FundInfo_JJFH.php?symbol=510300",
            15,
        )
    ]


@pytest.mark.parametrize("status_code", [403, 500])
def test_client_corporate_action_http_failure_is_typed(status_code):
    client = AkshareClient(
        FakeAkshare(),
        http_get=lambda *args, **kwargs: FakeResponse(
            b"secret upstream body",
            status_code=status_code,
        ),
    )

    with pytest.raises(FetchError) as captured:
        client.fetch_corporate_actions(
            INSTRUMENTS[0],
            start=date(2023, 1, 1),
            end=date(2024, 12, 31),
        )

    assert captured.value.code == "corporate_action_source_failed"
    assert "secret upstream body" not in str(captured.value)
