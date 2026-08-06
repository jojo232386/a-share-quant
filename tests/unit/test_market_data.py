from datetime import date

import pandas as pd
import pytest

from aquant.data.normalize import (
    NormalizationError,
    SourceSchema,
    normalize_market_frame,
)
from aquant.data.quality import DataQualityError, validate_market_frame

REQUIRED_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


def _valid_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-07-15", "2026-07-16", "2026-07-17"]),
            "open": [10.0, 10.5, 10.8],
            "high": [10.8, 11.0, 11.2],
            "low": [9.9, 10.2, 10.6],
            "close": [10.5, 10.8, 11.0],
            "volume": [1000, 1200, 900],
            "amount": [10_400.0, 12_900.0, 9_900.0],
        }
    )


def test_normalizes_chinese_akshare_columns_and_preserves_extras():
    source = pd.DataFrame(
        {
            "日期": ["2026-07-17"],
            "开盘": ["10.10"],
            "最高": ["10.60"],
            "最低": ["9.90"],
            "收盘": ["10.50"],
            "成交量": ["1000"],
            "成交额": ["10500.25"],
            "涨跌幅": ["1.2"],
        }
    )

    result = normalize_market_frame(source, source_schema=SourceSchema.STOCK_EASTMONEY)

    assert result.columns.tolist() == [*REQUIRED_COLUMNS, "涨跌幅"]
    assert result.loc[0, "date"] == pd.Timestamp("2026-07-17")
    assert result.loc[0, "date"].tzinfo is None
    assert result.loc[0, "close"] == pytest.approx(10.5)
    assert result.loc[0, "amount"] == pytest.approx(10_500.25)
    assert result.loc[0, "volume"] == 100_000
    assert result.loc[0, "涨跌幅"] == "1.2"


def test_normalizes_english_sina_columns():
    source = pd.DataFrame(
        {
            "date": [date(2026, 7, 17)],
            "open": ["10.10"],
            "high": ["10.60"],
            "low": ["9.90"],
            "close": ["10.50"],
            "volume": ["1000"],
            "amount": ["10500.25"],
            "postVol": [0],
        }
    )

    result = normalize_market_frame(source, source_schema=SourceSchema.STOCK_SINA)

    assert result.columns.tolist() == [*REQUIRED_COLUMNS, "postVol"]
    assert result.loc[0, "date"] == pd.Timestamp("2026-07-17")
    assert all(pd.api.types.is_numeric_dtype(result[column]) for column in REQUIRED_COLUMNS[1:])


@pytest.mark.parametrize(
    ("column", "bad_value", "error_code"),
    [
        ("date", "not-a-date", "invalid_date"),
        ("close", "not-a-number", "invalid_numeric"),
    ],
)
def test_normalization_rejects_bad_date_or_numeric_without_echoing_value(
    column, bad_value, error_code
):
    source = _valid_frame()
    source[column] = source[column].astype(object)
    source.loc[1, column] = bad_value

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(source, source_schema=SourceSchema.STOCK_SINA)

    assert error.value.code == error_code
    assert column in str(error.value)
    assert bad_value not in str(error.value)


def test_normalization_rejects_missing_column_with_exact_schema_diff():
    source = _valid_frame().drop(columns=["amount"])

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(source, source_schema=SourceSchema.STOCK_SINA)

    assert error.value.code == "missing_columns"
    assert error.value.details == {
        "expected": tuple(REQUIRED_COLUMNS),
        "actual": ("date", "open", "high", "low", "close", "volume"),
        "missing": ("amount",),
    }
    assert "missing=('amount',)" in str(error.value)


def test_normalization_rejects_two_source_columns_mapping_to_same_field():
    source = _valid_frame()
    source["日期"] = source["date"]

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(source, source_schema=SourceSchema.STOCK_SINA)

    assert error.value.code == "duplicate_mapping"
    assert error.value.details["canonical"] == "date"
    assert error.value.details["source_columns"] == ("date", "日期")


def test_equivalent_eastmoney_hands_and_sina_shares_normalize_to_same_volume():
    eastmoney = pd.DataFrame(
        {
            "日期": ["2026-07-17"],
            "开盘": [10.1],
            "最高": [10.6],
            "最低": [9.9],
            "收盘": [10.5],
            "成交量": [10],
            "成交额": [10_500],
        }
    )
    sina = pd.DataFrame(
        {
            "date": ["2026-07-17"],
            "open": [10.1],
            "high": [10.6],
            "low": [9.9],
            "close": [10.5],
            "volume": [1000],
            "amount": [10_500],
        }
    )

    em_result = normalize_market_frame(eastmoney, source_schema=SourceSchema.STOCK_EASTMONEY)
    sina_result = normalize_market_frame(sina, source_schema=SourceSchema.STOCK_SINA)

    assert em_result.loc[0, "volume"] == sina_result.loc[0, "volume"] == 1000
    assert em_result.loc[0, "amount"] == sina_result.loc[0, "amount"] == 10_500


@pytest.mark.parametrize(
    "bad_volume",
    [(2**63 - 1) // 100 + 1, -(2**63) // 100],
)
def test_eastmoney_volume_conversion_rejects_int64_overflow(bad_volume):
    source = pd.DataFrame(
        {
            "日期": ["2026-07-17"],
            "开盘": [10.1],
            "最高": [10.6],
            "最低": [9.9],
            "收盘": [10.5],
            "成交量": [bad_volume],
            "成交额": [10_500],
        }
    )

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(source, source_schema=SourceSchema.STOCK_EASTMONEY)

    assert error.value.code == "numeric_overflow"
    assert error.value.details == {"column": "volume", "multiplier": 100}


def test_equivalent_etf_eastmoney_lots_and_sina_units_normalize_to_same_volume():
    eastmoney = pd.DataFrame(
        {
            "日期": ["2026-07-17"],
            "开盘": [4.60],
            "最高": [4.65],
            "最低": [4.55],
            "收盘": [4.63],
            "成交量": [12],
            "成交额": [5_556],
        }
    )
    sina = pd.DataFrame(
        {
            "date": ["2026-07-17"],
            "open": [4.60],
            "high": [4.65],
            "low": [4.55],
            "close": [4.63],
            "volume": [1200],
            "amount": [5_556],
        }
    )

    em_result = normalize_market_frame(eastmoney, source_schema=SourceSchema.ETF_EASTMONEY)
    sina_result = normalize_market_frame(sina, source_schema=SourceSchema.ETF_SINA)

    assert em_result.loc[0, "volume"] == sina_result.loc[0, "volume"] == 1200
    assert em_result.loc[0, "amount"] == sina_result.loc[0, "amount"] == 5_556


def test_normalization_rejects_missing_source_profile():
    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(_valid_frame())

    assert error.value.code == "missing_source_profile"


def test_normalization_rejects_unknown_source_profile():
    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(_valid_frame(), source_schema="unknown.provider")

    assert error.value.code == "unknown_source_profile"
    assert "unknown.provider" not in str(error.value)


def test_normalization_rejects_known_profile_with_wrong_source_layout():
    chinese_source = pd.DataFrame(
        {
            "日期": ["2026-07-17"],
            "开盘": [10.1],
            "最高": [10.6],
            "最低": [9.9],
            "收盘": [10.5],
            "成交量": [10],
            "成交额": [10_500],
        }
    )

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(chinese_source, source_schema=SourceSchema.STOCK_SINA)

    assert error.value.code == "missing_columns"
    assert error.value.details["missing"] == tuple(REQUIRED_COLUMNS)


def test_normalization_wraps_non_scalar_date_as_typed_error():
    source = _valid_frame()
    source["date"] = source["date"].astype(object)
    source.at[1, "date"] = ["2026-07-16", "2026-07-17"]

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(source, source_schema=SourceSchema.STOCK_SINA)

    assert error.value.code == "invalid_date"
    assert "2026-07-16" not in str(error.value)


def test_normalization_rejects_numpy_integer_date_instead_of_treating_it_as_epoch_time():
    source = _valid_frame()
    source["date"] = source["date"].astype(object)
    source.at[1, "date"] = pd.Series([20260717], dtype="int64").iloc[0]

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(source, source_schema=SourceSchema.STOCK_SINA)

    assert error.value.code == "invalid_date"
    assert "20260717" not in str(error.value)


def test_normalization_rejects_timezone_aware_daily_date_as_ambiguous():
    source = _valid_frame()
    source["date"] = source["date"].astype(object)
    source.at[1, "date"] = pd.Timestamp("2026-07-16 16:30:00", tz="UTC")

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(source, source_schema=SourceSchema.STOCK_SINA)

    assert error.value.code == "invalid_date_timezone"
    assert "+00:00" not in str(error.value)


def test_normalization_rejects_non_midnight_source_timestamp_instead_of_truncating():
    source = _valid_frame()
    source.loc[1, "date"] = pd.Timestamp("2026-07-16 15:00:00")

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(source, source_schema=SourceSchema.STOCK_SINA)

    assert error.value.code == "invalid_date_granularity"
    assert "15:00" not in str(error.value)


@pytest.mark.parametrize("column", ["high", "volume", "amount"])
@pytest.mark.parametrize(
    "temporal_value",
    [pd.Timestamp("2026-07-17"), pd.Timedelta(days=1)],
)
def test_normalization_rejects_temporal_values_in_numeric_columns(column, temporal_value):
    source = _valid_frame()
    source[column] = source[column].astype(object)
    source.at[1, column] = temporal_value

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(source, source_schema=SourceSchema.STOCK_SINA)

    assert error.value.code == "invalid_numeric"
    assert error.value.details["column"] == column


@pytest.mark.parametrize(
    ("column", "temporal_series"),
    [
        ("high", pd.to_datetime(["2026-07-15", "2026-07-16", "2026-07-17"])),
        ("volume", pd.to_timedelta([1, 2, 3], unit="D")),
        ("amount", pd.to_datetime(["2026-07-15", "2026-07-16", "2026-07-17"])),
    ],
)
def test_normalization_rejects_temporal_numeric_column_dtypes(column, temporal_series):
    source = _valid_frame()
    source[column] = temporal_series

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(source, source_schema=SourceSchema.STOCK_SINA)

    assert error.value.code == "invalid_numeric"
    assert error.value.details["column"] == column


def test_normalization_rejects_numpy_boolean_in_numeric_column():
    source = _valid_frame()
    source["volume"] = source["volume"].astype(object)
    source.at[1, "volume"] = pd.Series([True], dtype="boolean").iloc[0]

    with pytest.raises(NormalizationError) as error:
        normalize_market_frame(source, source_schema=SourceSchema.STOCK_SINA)

    assert error.value.code == "invalid_numeric"
    assert error.value.details == {"column": "volume", "position": 1}


def test_quality_reports_exact_missing_column_diff():
    frame = _valid_frame().drop(columns=["amount"])

    with pytest.raises(DataQualityError) as error:
        validate_market_frame(frame)

    assert error.value.code == "missing_columns"
    assert error.value.details == {
        "expected": tuple(REQUIRED_COLUMNS),
        "actual": ("date", "open", "high", "low", "close", "volume"),
        "missing": ("amount",),
    }


def test_quality_rejects_complex_numeric_dtype_as_typed_error():
    frame = _valid_frame()
    frame["close"] = frame["close"].astype(complex)

    with pytest.raises(DataQualityError) as error:
        validate_market_frame(frame)

    assert error.value.code == "invalid_dtypes"
    assert error.value.details == {"columns": ("close",)}


def test_quality_rejects_non_midnight_daily_timestamps():
    frame = _valid_frame()
    frame.loc[1, "date"] = pd.Timestamp("2026-07-16 12:00:00")

    with pytest.raises(DataQualityError) as error:
        validate_market_frame(frame)

    assert error.value.code == "invalid_date_granularity"
    assert error.value.details == {"count": 1}


def test_quality_rejects_duplicate_dates():
    frame = _valid_frame()
    frame.loc[2, "date"] = frame.loc[1, "date"]

    with pytest.raises(DataQualityError) as error:
        validate_market_frame(frame)

    assert error.value.code == "quality_violations"
    assert error.value.report.duplicate_date_count == 1


def test_quality_rejects_out_of_order_dates():
    frame = _valid_frame().iloc[[0, 2, 1]].reset_index(drop=True)

    with pytest.raises(DataQualityError) as error:
        validate_market_frame(frame)

    assert error.value.code == "quality_violations"
    assert error.value.report.out_of_order_date_count == 1
    assert error.value.report.duplicate_date_count == 0


@pytest.mark.parametrize("column", REQUIRED_COLUMNS)
def test_quality_rejects_null_in_every_required_column(column):
    frame = _valid_frame()
    frame.loc[1, column] = pd.NaT if column == "date" else None

    with pytest.raises(DataQualityError) as error:
        validate_market_frame(frame)

    assert error.value.code == "quality_violations"
    assert error.value.report.null_count == 1


@pytest.mark.parametrize("column", ["open", "high", "low", "close"])
@pytest.mark.parametrize("bad_value", [0.0, -0.01])
def test_quality_rejects_non_positive_ohlc(column, bad_value):
    frame = _valid_frame()
    frame.loc[1, column] = bad_value

    with pytest.raises(DataQualityError) as error:
        validate_market_frame(frame)

    assert error.value.report.non_positive_price_count >= 1


@pytest.mark.parametrize("column", ["volume", "amount"])
def test_quality_rejects_negative_volume_or_amount(column):
    frame = _valid_frame()
    frame.loc[1, column] = -1

    with pytest.raises(DataQualityError) as error:
        validate_market_frame(frame)

    count_field = "negative_volume_count" if column == "volume" else "negative_amount_count"
    assert getattr(error.value.report, count_field) == 1


@pytest.mark.parametrize(
    ("column", "bad_value", "count_field"),
    [
        ("high", 10.1, "invalid_high_count"),
        ("low", 10.7, "invalid_low_count"),
    ],
)
def test_quality_rejects_invalid_ohlc_envelope(column, bad_value, count_field):
    frame = _valid_frame()
    frame.loc[1, column] = bad_value

    with pytest.raises(DataQualityError) as error:
        validate_market_frame(frame)

    assert getattr(error.value.report, count_field) == 1


def test_quality_rejects_empty_frame():
    frame = _valid_frame().iloc[0:0]

    with pytest.raises(DataQualityError) as error:
        validate_market_frame(frame)

    assert error.value.report.empty_frame_count == 1


def test_quality_rejects_non_finite_numeric_value():
    frame = _valid_frame()
    frame.loc[1, "amount"] = float("inf")

    with pytest.raises(DataQualityError) as error:
        validate_market_frame(frame)

    assert error.value.report.non_finite_numeric_count == 1


def test_quality_allows_zero_volume_and_amount():
    frame = _valid_frame()
    frame.loc[1, ["volume", "amount"]] = 0

    report = validate_market_frame(frame)

    assert report.anomaly_count == 0


def test_valid_frame_returns_structured_zero_anomaly_report():
    report = validate_market_frame(_valid_frame())

    assert report.row_count == 3
    assert report.start_date == date(2026, 7, 15)
    assert report.end_date == date(2026, 7, 17)
    assert report.null_count == 0
    assert report.duplicate_date_count == 0
    assert report.out_of_order_date_count == 0
    assert report.non_positive_price_count == 0
    assert report.negative_volume_count == 0
    assert report.negative_amount_count == 0
    assert report.invalid_high_count == 0
    assert report.invalid_low_count == 0
    assert report.anomaly_count == 0
