from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from aquant.data.corporate_actions import (
    CorporateActionError,
    CorporateActionSnapshotStore,
    VerifiedCorporateActions,
    load_verified_corporate_actions,
    normalize_etf_dividends,
    normalize_stock_dividends,
    parse_sina_etf_dividend_detail,
    publish_corporate_actions,
)
from aquant.rules import InstrumentKind


def _stock_frame(**overrides) -> pd.DataFrame:
    values = {
        "实施方案公告日期": ["2018-05-31", "2018-05-31"],
        "分红类型": ["年度分红", "年度分红"],
        "送股比例": [None, None],
        "转增比例": [None, None],
        "派息比例": [10.0, 2.0],
        "股权登记日": ["2018-06-06", "2018-06-06"],
        "除权日": ["2018-06-07", "2018-06-07"],
        "派息日": ["2018-06-07", "2018-06-07"],
        "股份到账日": [None, None],
        "实施方案分红说明": ["10派10元", "10派2元"],
        "报告时间": ["2017年报", "2017年报"],
    }
    values.update(overrides)
    return pd.DataFrame(values)


def _normalize(frame: pd.DataFrame):
    return normalize_stock_dividends(
        frame,
        symbol="601318",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        coverage_start=date(2018, 1, 1),
        coverage_end=date(2018, 12, 31),
    )


def test_stock_cash_rows_on_same_ex_date_are_aggregated_exactly():
    events = _normalize(_stock_frame())

    assert len(events) == 1
    event = events[0]
    assert event.symbol == "601318"
    assert event.instrument_kind is InstrumentKind.MAIN_BOARD_STOCK
    assert event.announcement_date == date(2018, 5, 31)
    assert event.record_date == date(2018, 6, 6)
    assert event.ex_date == date(2018, 6, 7)
    assert event.payable_date == date(2018, 6, 7)
    assert event.cash_dividend_per_unit == Decimal("1.2")
    assert event.stock_dividend_ratio == Decimal("0")
    assert event.capitalization_ratio == Decimal("0")
    assert event.rights_ratio == Decimal("0")
    assert event.rights_price is None
    assert len(event.event_id) == 64


def test_stock_normalization_is_deterministic():
    first = _normalize(_stock_frame())
    second = _normalize(_stock_frame())

    assert first == second
    assert first[0].event_id == second[0].event_id


def test_stock_normalization_ignores_provably_precoverage_missing_ex_date():
    old = _stock_frame().iloc[[0]].copy()
    old["实施方案公告日期"] = "2005-08-09"
    old["股权登记日"] = "2005-08-11"
    old["除权日"] = pd.NaT
    old["派息日"] = "2005-08-17"
    current = _stock_frame().iloc[[1]].copy()

    events = _normalize(pd.concat([old, current], ignore_index=True))

    assert len(events) == 1
    assert events[0].ex_date == date(2018, 6, 7)


def test_stock_normalization_rejects_missing_ex_date_not_provably_precoverage():
    frame = _stock_frame()
    frame.loc[0, "除权日"] = pd.NaT

    with pytest.raises(CorporateActionError) as captured:
        _normalize(frame)

    assert captured.value.code == "missing_required_date"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"送股比例": [1.0, None]}, "unsupported_corporate_action"),
        ({"转增比例": [None, 1.0]}, "unsupported_corporate_action"),
        ({"派息比例": [-1.0, 2.0]}, "invalid_cash_dividend"),
        ({"派息比例": [True, 2.0]}, "invalid_numeric_value"),
        (
            {"派息日": ["2018-06-05", "2018-06-07"]},
            "payment_before_ex_date",
        ),
        (
            {"除权日": ["not-a-date", "2018-06-07"]},
            "invalid_date",
        ),
    ],
)
def test_stock_normalization_rejects_unsafe_values(overrides, code):
    with pytest.raises(CorporateActionError) as captured:
        _normalize(_stock_frame(**overrides))

    assert captured.value.code == code


def test_stock_normalization_rejects_unknown_columns():
    frame = _stock_frame()
    frame["恶意列"] = "unexpected"

    with pytest.raises(CorporateActionError) as captured:
        _normalize(frame)

    assert captured.value.code == "source_schema_changed"


def test_stock_normalization_rejects_conflicting_same_day_metadata():
    with pytest.raises(CorporateActionError) as captured:
        _normalize(
            _stock_frame(
                股权登记日=["2018-06-06", "2018-06-05"],
            )
        )

    assert captured.value.code == "conflicting_same_day_events"


def test_stock_normalization_slices_to_coverage():
    frame = _stock_frame(
        实施方案公告日期=["2017-05-31", "2018-05-31"],
        股权登记日=["2017-06-06", "2018-06-06"],
        除权日=["2017-06-07", "2018-06-07"],
        派息日=["2017-06-07", "2018-06-07"],
        派息比例=[5.0, 2.0],
    )

    events = _normalize(frame)

    assert len(events) == 1
    assert events[0].cash_dividend_per_unit == Decimal("0.2")


def _etf_cumulative_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": [
                "2022-01-19",
                "2023-01-16",
                "2024-01-18",
            ],
            "累计分红": [0.536, 0.600, 0.669],
        }
    )


def _etf_detail_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "权益登记日": [
                "2022-01-18",
                "2023-01-13",
                "2024-01-17",
            ],
            "红利发放日": [
                "2022-01-24",
                "2023-01-19",
                "2024-01-23",
            ],
            "每份分红(元)": [0.075, 0.064, 0.069],
        }
    )


def _normalize_etf(cumulative=None, detail=None):
    return normalize_etf_dividends(
        cumulative if cumulative is not None else _etf_cumulative_frame(),
        detail if detail is not None else _etf_detail_frame(),
        symbol="510300",
        instrument_kind=InstrumentKind.DOMESTIC_EQUITY_BROAD_BASED_ETF,
        coverage_start=date(2023, 1, 1),
        coverage_end=date(2024, 12, 31),
    )


def test_etf_cumulative_dividends_match_detail_dates_and_amounts():
    events = _normalize_etf()

    assert [event.ex_date for event in events] == [
        date(2023, 1, 16),
        date(2024, 1, 18),
    ]
    assert [event.cash_dividend_per_unit for event in events] == [
        Decimal("0.064"),
        Decimal("0.069"),
    ]
    assert events[-1].record_date == date(2024, 1, 17)
    assert events[-1].payable_date == date(2024, 1, 23)
    assert events[-1].source_schema == "akshare.fund_etf_dividend_sina+sina.detail.v1"


def test_etf_zero_cumulative_rows_do_not_require_fake_detail_records():
    cumulative = pd.concat(
        [
            pd.DataFrame({"日期": ["2021-01-01"], "累计分红": [0.0]}),
            _etf_cumulative_frame(),
        ],
        ignore_index=True,
    )

    assert _normalize_etf(cumulative=cumulative) == _normalize_etf()


@pytest.mark.parametrize(
    ("cumulative", "detail", "code"),
    [
        (
            pd.DataFrame(
                {
                    "日期": ["2023-01-16", "2024-01-18"],
                    "累计分红": [0.600, 0.590],
                }
            ),
            _etf_detail_frame(),
            "negative_cumulative_difference",
        ),
        (
            _etf_cumulative_frame(),
            _etf_detail_frame().assign(**{"每份分红(元)": [0.075, 0.064, 0.068]}),
            "dividend_amount_mismatch",
        ),
        (
            _etf_cumulative_frame(),
            _etf_detail_frame().iloc[:2].copy(),
            "missing_etf_dividend_detail",
        ),
        (
            _etf_cumulative_frame(),
            pd.concat([_etf_detail_frame(), _etf_detail_frame().iloc[[-1]]]),
            "duplicate_etf_dividend_detail",
        ),
    ],
)
def test_etf_normalization_fails_closed(cumulative, detail, code):
    with pytest.raises(CorporateActionError) as captured:
        _normalize_etf(cumulative, detail)

    assert captured.value.code == code


def test_parse_sina_etf_detail_uses_pinned_headers():
    html = """
    <html><body><table>
      <thead><tr>
        <th>权益登记日</th><th>红利发放日</th><th>每份分红(元)</th>
      </tr></thead>
      <tbody><tr><td>2024/1/17</td><td>2024/1/23</td><td>0.069</td></tr></tbody>
    </table></body></html>
    """.encode("gb18030")

    frame = parse_sina_etf_dividend_detail(html)

    assert frame.to_dict(orient="records") == [
        {
            "权益登记日": "2024/1/17",
            "红利发放日": "2024/1/23",
            "每份分红(元)": 0.069,
        }
    ]


def test_parse_sina_etf_detail_accepts_title_row_then_pinned_header_row():
    html = """
    <html><body><table>
      <thead><tr>
        <th>华泰柏瑞沪深300ETF历史分红</th>
        <th>华泰柏瑞沪深300ETF历史分红.1</th>
        <th>华泰柏瑞沪深300ETF历史分红.2</th>
        <th>华泰柏瑞沪深300ETF历史分红.3</th>
      </tr></thead>
      <tbody>
        <tr><td>权益登记日</td><td>红利发放日</td><td>每份分红(元)</td><td></td></tr>
        <tr><td>2024/1/17</td><td>2024/1/23</td><td>0.069</td><td></td></tr>
        <tr><td></td><td></td><td>合计:0.069</td><td></td></tr>
      </tbody>
    </table></body></html>
    """.encode("gb18030")

    frame = parse_sina_etf_dividend_detail(html)

    assert frame.to_dict(orient="records") == [
        {
            "权益登记日": "2024/1/17",
            "红利发放日": "2024/1/23",
            "每份分红(元)": "0.069",
        }
    ]


def test_parse_sina_etf_detail_ignores_pinned_zero_dividend_placeholder():
    html = """
    <html><body><table>
      <thead><tr>
        <th>权益登记日</th><th>红利发放日</th><th>每份分红(元)</th>
      </tr></thead>
      <tbody>
        <tr><td>2022/8/26</td><td>1970/1/1</td><td></td></tr>
        <tr><td>2024/5/16</td><td>2024/5/22</td><td>0.087</td></tr>
      </tbody>
    </table></body></html>
    """.encode("gb18030")

    frame = parse_sina_etf_dividend_detail(html)

    assert frame.to_dict(orient="records") == [
        {
            "权益登记日": "2024/5/16",
            "红利发放日": "2024/5/22",
            "每份分红(元)": 0.087,
        }
    ]


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"\x81", "etf_detail_decode_failed"),
        (
            "<table><tr><th>日期漂移</th></tr><tr><td>2024</td></tr></table>".encode(
                "gb18030"
            ),
            "etf_detail_schema_changed",
        ),
    ],
)
def test_parse_sina_etf_detail_rejects_decode_and_schema_changes(content, code):
    with pytest.raises(CorporateActionError) as captured:
        parse_sina_etf_dividend_detail(content)

    assert captured.value.code == code


def _publish(tmp_path, events=None):
    chosen = _normalize(_stock_frame()) if events is None else events
    return publish_corporate_actions(
        tmp_path,
        chosen,
        symbol="601318",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        provider="cninfo",
        source_schema="akshare.stock_dividend_cninfo.v1",
        normalization_version="cash-only-v1",
        coverage_start=date(2018, 1, 1),
        coverage_end=date(2018, 12, 31),
    )


def test_action_snapshot_is_content_addressed_idempotent_and_verified(tmp_path):
    first = _publish(tmp_path)
    second = _publish(tmp_path)

    assert first == second
    assert first.snapshot_relative_path.as_posix().startswith(
        "data/corporate_actions/601318/"
    )
    assert first.snapshot_relative_path.name == f"{first.file_sha256}.json"
    manifest_path = tmp_path / "data/corporate_actions/manifest.jsonl"
    assert len(manifest_path.read_text(encoding="utf-8").splitlines()) == 1

    verified = load_verified_corporate_actions(tmp_path, first)

    assert type(verified) is VerifiedCorporateActions
    assert verified.events == _normalize(_stock_frame())
    assert verified.provenance.snapshot_id == first.snapshot_id


def test_empty_but_complete_action_snapshot_is_allowed(tmp_path):
    record = _publish(tmp_path, events=())

    assert record.row_count == 0
    assert load_verified_corporate_actions(tmp_path, record).events == ()


def test_action_snapshot_bytes_are_canonical_and_read_only(tmp_path):
    record = _publish(tmp_path)
    path = tmp_path / record.snapshot_relative_path
    content = path.read_bytes()

    assert content.endswith(b"\n")
    assert b" " not in content
    assert (path.stat().st_mode & 0o222) == 0


def test_verified_actions_cannot_be_constructed_with_a_public_token():
    with pytest.raises(CorporateActionError) as captured:
        VerifiedCorporateActions(
            events=(),
            provenance=None,
            _token=object(),
        )

    assert captured.value.code == "unverified_corporate_actions"


@pytest.mark.parametrize(
    "mutation",
    ["bytes", "manifest_hash", "row_count", "symbol"],
)
def test_action_snapshot_tampering_is_rejected(tmp_path, mutation):
    record = _publish(tmp_path)
    if mutation == "bytes":
        path = tmp_path / record.snapshot_relative_path
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b"x")
    elif mutation == "manifest_hash":
        object.__setattr__(record, "file_sha256", "0" * 64)
    elif mutation == "row_count":
        object.__setattr__(record, "row_count", 99)
    else:
        object.__setattr__(record, "symbol", "600519")

    with pytest.raises(CorporateActionError):
        load_verified_corporate_actions(tmp_path, record)


def test_action_store_rejects_symlinked_data_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "data").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CorporateActionError) as captured:
        _publish(tmp_path)

    assert captured.value.code == "unsafe_snapshot_parent"


def test_action_store_rejects_conflicting_existing_content(tmp_path):
    record = _publish(tmp_path)
    path = tmp_path / record.snapshot_relative_path
    path.chmod(0o644)
    path.write_text("conflict\n", encoding="utf-8")

    with pytest.raises(CorporateActionError) as captured:
        CorporateActionSnapshotStore(tmp_path).write(
            _normalize(_stock_frame()),
            symbol="601318",
        )

    assert captured.value.code == "snapshot_conflict"
