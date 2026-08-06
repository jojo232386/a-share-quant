import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from aquant.cli import CliServices, main
from aquant.config import DataConfig, InstrumentConfig
from aquant.data.akshare_client import FetchError, RawFetchResult
from aquant.data.calendar_snapshot import CalendarSnapshotStore
from aquant.data.corporate_actions import (
    CorporateActionEvent,
    load_verified_corporate_actions,
    read_corporate_action_manifest,
)
from aquant.data.ingestion import IngestionError, SymbolMissingSessions, run_ingestion
from aquant.data.manifest import ManifestWriter
from aquant.data.normalize import SourceSchema
from aquant.data.snapshot import RawSnapshotStore, SnapshotError
from aquant.rules import InstrumentKind
from aquant.universe import load_verified_universe

UNIVERSE_ID = "bba6760fa738a829bb09a72f0c90919aeba02429018b8fd189c65e2d6c82a20e"
UNIVERSE = load_verified_universe(
    Path(__file__).parents[2] / "configs" / "universes" / f"{UNIVERSE_ID}.json",
    expected_id=UNIVERSE_ID,
)
INSTRUMENTS = tuple(
    InstrumentConfig(item.symbol, item.kind)
    for item in UNIVERSE.members
)
CONFIG = DataConfig(
    adjust="",
    mode="research_approx",
    start=date(2018, 1, 1),
    end="latest_complete_trading_day",
    universe=UNIVERSE,
)
NOW = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)  # Shanghai 2026-07-20 16:00


def _raw_sina(dates=("2017-12-29", "2018-01-02", "2026-07-17", "2026-07-20")):
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": [10.0] * len(dates),
            "high": [11.0] * len(dates),
            "low": [9.0] * len(dates),
            "close": [10.5] * len(dates),
            "volume": [1000] * len(dates),
            "amount": [10_500.0] * len(dates),
            "vendor_extra": list(range(len(dates))),
        }
    )


def _raw_eastmoney(dates=("2017-12-29", "2018-01-02", "2026-07-17", "2026-07-20")):
    return pd.DataFrame(
        {
            "日期": list(dates),
            "开盘": [10.0] * len(dates),
            "最高": [11.0] * len(dates),
            "最低": [9.0] * len(dates),
            "收盘": [10.5] * len(dates),
            "成交量": [10] * len(dates),
            "成交额": [10_500.0] * len(dates),
        }
    )


def _result(instrument, frame=None):
    is_etf = instrument.kind.endswith("_etf")
    return RawFetchResult(
        symbol=instrument.symbol,
        instrument_kind=instrument.kind,
        frame=_raw_sina() if frame is None else frame,
        provider="sina",
        source_function="fund_etf_hist_sina" if is_etf else "stock_zh_a_daily",
        source_schema=SourceSchema.ETF_SINA if is_etf else SourceSchema.STOCK_SINA,
        endpoint_host="finance.sina.com.cn",
        provider_symbol=("sh" if instrument.symbol.startswith(("5", "6")) else "sz")
        + instrument.symbol,
        raw_volume_unit="unit" if is_etf else "share",
        volume_multiplier_to_canonical=1,
        full_history_download=True,
        local_date_slice=True,
    )


def _eastmoney_result(instrument, frame=None):
    is_etf = instrument.kind.endswith("_etf")
    return RawFetchResult(
        symbol=instrument.symbol,
        instrument_kind=instrument.kind,
        frame=_raw_eastmoney() if frame is None else frame,
        provider="eastmoney",
        source_function="fund_etf_hist_em" if is_etf else "stock_zh_a_hist",
        source_schema=SourceSchema.ETF_EASTMONEY if is_etf else SourceSchema.STOCK_EASTMONEY,
        endpoint_host="push2his.eastmoney.com",
        provider_symbol=("sh" if instrument.symbol.startswith(("5", "6")) else "sz")
        + instrument.symbol,
        raw_volume_unit="lot",
        volume_multiplier_to_canonical=100,
        full_history_download=False,
        local_date_slice=False,
    )


class FakeClient:
    def __init__(self, results=None, error=None):
        self.results = tuple(results or (_result(item) for item in INSTRUMENTS))
        self.error = error
        self.calls = []

    def fetch_batch(self, instruments, *, start, end):
        self.calls.append((instruments, start, end))
        if self.error:
            raise self.error
        return self.results


class ForgedRawFetchResult(RawFetchResult):
    def __post_init__(self) -> None:
        pass


class ForgedProvider(str):
    pass


class UncheckedDataConfig(DataConfig):
    def __post_init__(self) -> None:
        pass


def _calendar():
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                ["2018-01-02", "2026-07-17", "2026-07-20", "2026-07-21", "2026-12-31"]
            )
        }
    )


def _copy_real_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        Path("configs/data.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    universe_directory = tmp_path / "universes"
    universe_directory.mkdir()
    universe_path = Path("configs/universes") / f"{UNIVERSE_ID}.json"
    (universe_directory / universe_path.name).write_bytes(universe_path.read_bytes())
    return config_path


def _run(tmp_path, client=None, clock=lambda: NOW):
    return run_ingestion(
        CONFIG,
        client=client or FakeClient(),
        clock=clock,
        trade_calendar_provider=_calendar,
        snapshot_store=RawSnapshotStore(tmp_path),
        manifest_writer=ManifestWriter(tmp_path / "data/manifests/manifest.jsonl"),
        calendar_store=CalendarSnapshotStore(tmp_path),
        akshare_version="1.18.64",
    )


def test_latest_complete_trading_day_excludes_today_and_future_calendar_rows(tmp_path):
    client = FakeClient()

    result = _run(tmp_path, client)

    assert result.requested_end == date(2026, 7, 17)
    assert client.calls[0][2] == date(2026, 7, 17)


def test_success_writes_ten_sliced_raw_snapshots_and_exact_manifests(tmp_path):
    result = _run(tmp_path)

    assert len(result.items) == 10
    assert {item.manifest_status for item in result.items} == {"appended"}
    records = ManifestWriter(tmp_path / "data/manifests/manifest.jsonl").read_all()
    assert len(records) == 10
    assert {record.symbol for record in records} == {item.symbol for item in INSTRUMENTS}
    assert all(record.requested_start == date(2018, 1, 1) for record in records)
    assert all(record.requested_end == date(2026, 7, 17) for record in records)
    assert all(record.actual_start == date(2018, 1, 2) for record in records)
    assert all(record.actual_end == date(2026, 7, 17) for record in records)
    assert all(
        record.quality_issue_counts and not any(record.quality_issue_counts.values())
        for record in records
    )
    assert all(record.raw_volume_unit in {"share", "unit"} for record in records)
    for item in result.items:
        stored = pd.read_parquet(tmp_path / item.snapshot_relative_path)
        assert stored.index.equals(pd.RangeIndex(2))
        assert stored["date"].dt.date.tolist() == [date(2018, 1, 2), date(2026, 7, 17)]
        assert stored["vendor_extra"].tolist() == [1, 2]


def test_repeat_run_reuses_snapshots_and_manifest_identity_ignores_fetch_time(tmp_path):
    first = _run(tmp_path, clock=lambda: NOW)
    second = _run(tmp_path, clock=lambda: NOW.replace(minute=1))

    assert all(not item.snapshot_reused for item in first.items)
    assert all(item.snapshot_reused for item in second.items)
    assert {item.manifest_status for item in second.items} == {"duplicate"}
    assert len(ManifestWriter(tmp_path / "data/manifests/manifest.jsonl").read_all()) == 10


def test_quality_or_schema_failure_in_any_symbol_writes_nothing(tmp_path):
    results = [_result(item) for item in INSTRUMENTS]
    results[2] = replace(results[2], frame=_raw_sina().drop(columns=["amount"]))

    with pytest.raises(IngestionError, match="batch validation failed"):
        _run(tmp_path, FakeClient(results))

    assert not (tmp_path / "data/raw").exists()
    assert not (tmp_path / "data/manifests/manifest.jsonl").exists()


def test_unusable_date_anywhere_in_full_source_frame_blocks_whole_batch_before_write(tmp_path):
    results = [_result(item) for item in INSTRUMENTS]
    bad_frame = _raw_sina().copy()
    bad_frame.loc[0, "date"] = pd.NaT
    results[2] = replace(results[2], frame=bad_frame)

    with pytest.raises(IngestionError, match="batch validation failed") as error:
        _run(tmp_path, FakeClient(results))

    assert error.value.code == "batch_validation_failed"
    assert not (tmp_path / "data/raw").exists()
    assert not (tmp_path / "data/manifests/manifest.jsonl").exists()


def test_explicit_range_source_cannot_silently_drop_out_of_range_rows(tmp_path):
    results = [_result(item) for item in INSTRUMENTS]
    results[1] = _eastmoney_result(
        INSTRUMENTS[1],
        _raw_eastmoney(("2018-01-02", "2026-07-17", "2026-07-20")),
    )

    with pytest.raises(IngestionError) as error:
        _run(tmp_path, FakeClient(results))

    assert error.value.code == "source_range_violation"
    assert not (tmp_path / "data/raw").exists()


def test_ingestion_revalidates_source_contract_after_object_tampering(tmp_path):
    results = [_result(item) for item in INSTRUMENTS]
    object.__setattr__(results[1], "source_function", "stock_zh_a_hist")

    with pytest.raises(IngestionError) as error:
        _run(tmp_path, FakeClient(results))

    assert error.value.code == "source_contract_violation"
    assert not (tmp_path / "data/raw").exists()


def test_ingestion_rejects_fetch_result_subclass_that_overrides_validation(tmp_path):
    valid = _result(INSTRUMENTS[1])
    forged = ForgedRawFetchResult(**{**valid.__dict__, "provider": "eastmoney"})
    results = [_result(item) for item in INSTRUMENTS]
    results[1] = forged

    with pytest.raises(IngestionError) as error:
        _run(tmp_path, FakeClient(results))

    assert error.value.code == "source_contract_violation"
    assert not (tmp_path / "data/raw").exists()


def test_ingestion_rejects_provider_str_subclass_before_writing_snapshot(tmp_path):
    valid = _result(INSTRUMENTS[1])
    object.__setattr__(valid, "provider", ForgedProvider("sina"))
    results = [_result(item) for item in INSTRUMENTS]
    results[1] = valid

    with pytest.raises(IngestionError) as error:
        _run(tmp_path, FakeClient(results))

    assert error.value.code == "source_contract_violation"
    assert not (tmp_path / "data/raw").exists()


def test_ingestion_rejects_data_config_subclass_before_fetch_or_write(tmp_path):
    unchecked = UncheckedDataConfig(
        adjust="qfq",
        mode="research_approx",
        start=date(2018, 1, 1),
        end="latest_complete_trading_day",
        universe=UNIVERSE,
    )
    client = FakeClient()

    with pytest.raises(IngestionError) as error:
        run_ingestion(
            unchecked,
            client=client,
            clock=lambda: NOW,
            trade_calendar_provider=_calendar,
            snapshot_store=RawSnapshotStore(tmp_path),
            manifest_writer=ManifestWriter(tmp_path / "data/manifests/manifest.jsonl"),
            akshare_version="1.18.64",
        )

    assert error.value.code == "invalid_config"
    assert client.calls == []
    assert not (tmp_path / "data/raw").exists()


def test_ingestion_revalidates_data_config_after_object_tampering(tmp_path):
    config = DataConfig(
        adjust="",
        mode="research_approx",
        start=date(2018, 1, 1),
        end="latest_complete_trading_day",
        universe=UNIVERSE,
    )
    object.__setattr__(config, "adjust", "qfq")
    client = FakeClient()

    with pytest.raises(IngestionError) as error:
        run_ingestion(
            config,
            client=client,
            clock=lambda: NOW,
            trade_calendar_provider=_calendar,
            snapshot_store=RawSnapshotStore(tmp_path),
            manifest_writer=ManifestWriter(tmp_path / "data/manifests/manifest.jsonl"),
            akshare_version="1.18.64",
        )

    assert error.value.code == "invalid_config"
    assert client.calls == []
    assert not (tmp_path / "data/raw").exists()


def test_market_dates_must_belong_to_validated_trade_calendar(tmp_path):
    frame = _raw_sina(("2018-01-02", "2026-07-16", "2026-07-17"))
    results = [_result(item, frame) for item in INSTRUMENTS]

    with pytest.raises(IngestionError) as error:
        _run(tmp_path, FakeClient(results))

    assert error.value.code == "non_trading_date"
    assert not (tmp_path / "data/raw").exists()


def test_ingestion_publishes_calendar_and_records_symbol_gaps(tmp_path):
    complete = ("2017-12-29", "2018-01-02", "2018-01-03", "2026-07-17", "2026-07-20")
    gap = ("2017-12-29", "2018-01-02", "2026-07-17", "2026-07-20")
    results = [
        _result(item, _raw_sina(gap if item.symbol == "600519" else complete))
        for item in INSTRUMENTS
    ]

    def calendar_with_missing_market_row():
        return pd.DataFrame(
            {
                "trade_date": pd.to_datetime(
                    [
                        "2018-01-02",
                        "2018-01-03",
                        "2026-07-17",
                        "2026-07-20",
                        "2026-07-21",
                    ]
                )
            }
        )

    result = run_ingestion(
        CONFIG,
        client=FakeClient(results),
        clock=lambda: NOW,
        trade_calendar_provider=calendar_with_missing_market_row,
        snapshot_store=RawSnapshotStore(tmp_path),
        manifest_writer=ManifestWriter(tmp_path / "data/manifests/manifest.jsonl"),
        calendar_store=CalendarSnapshotStore(tmp_path),
        akshare_version="1.18.64",
    )

    assert len(result.items) == 10
    assert result.calendar_record.last_complete_date == date(2026, 7, 17)
    assert result.missing_sessions == (
        SymbolMissingSessions("600519", (date(2018, 1, 3),)),
    )


def test_new_calendar_does_not_change_existing_market_snapshot_hash(tmp_path):
    first = _run(tmp_path)
    first_market_artifacts = tuple(
        (item.snapshot_relative_path, item.snapshot_sha256) for item in first.items
    )

    def extended_calendar():
        return pd.DataFrame(
            {
                "trade_date": pd.to_datetime(
                    [
                        "2018-01-02",
                        "2026-07-17",
                        "2026-07-20",
                        "2026-07-21",
                        "2026-07-22",
                        "2026-12-31",
                    ]
                )
            }
        )

    second = run_ingestion(
        CONFIG,
        client=FakeClient(),
        clock=lambda: NOW + timedelta(days=1),
        trade_calendar_provider=extended_calendar,
        snapshot_store=RawSnapshotStore(tmp_path),
        manifest_writer=ManifestWriter(tmp_path / "data/manifests/manifest.jsonl"),
        calendar_store=CalendarSnapshotStore(tmp_path),
        akshare_version="1.18.64",
    )

    for relative_path, expected_hash in first_market_artifacts:
        RawSnapshotStore(tmp_path).verify(relative_path, expected_hash=expected_hash)
    assert first.calendar_record.calendar_id != second.calendar_record.calendar_id


def test_every_symbol_must_reach_requested_end_without_stale_data_policy(tmp_path):
    results = [_result(item) for item in INSTRUMENTS]
    results[2] = replace(results[2], frame=_raw_sina(("2018-01-02",)))

    with pytest.raises(IngestionError) as error:
        _run(tmp_path, FakeClient(results))

    assert error.value.code == "stale_market_data"
    assert not (tmp_path / "data/raw").exists()


def test_empty_requested_interval_fails_before_any_write(tmp_path):
    results = [_result(item, _raw_sina(("2017-01-01",))) for item in INSTRUMENTS]

    with pytest.raises(IngestionError, match="empty requested interval"):
        _run(tmp_path, FakeClient(results))

    assert not (tmp_path / "data/raw").exists()


def test_calendar_with_no_prior_trading_day_fails_closed(tmp_path):
    def calendar():
        return pd.DataFrame({"trade_date": pd.to_datetime(["2026-07-20"])})

    with pytest.raises(IngestionError, match="complete trading day"):
        run_ingestion(
            CONFIG,
            client=FakeClient(),
            clock=lambda: NOW,
            trade_calendar_provider=calendar,
            snapshot_store=RawSnapshotStore(tmp_path),
            manifest_writer=ManifestWriter(tmp_path / "manifest.jsonl"),
            akshare_version="1.18.64",
        )


@pytest.mark.parametrize(
    "calendar_values",
    [
        [],
        ["2026-07-17", "2026-07-17", "2026-07-21"],
        ["2026-07-20", "2026-07-17", "2026-07-21"],
    ],
)
def test_calendar_must_be_nonempty_unique_and_strictly_increasing(tmp_path, calendar_values):
    def invalid_calendar():
        return pd.DataFrame({"trade_date": pd.to_datetime(calendar_values)})

    with pytest.raises(IngestionError) as error:
        run_ingestion(
            CONFIG,
            client=FakeClient(),
            clock=lambda: NOW,
            trade_calendar_provider=invalid_calendar,
            snapshot_store=RawSnapshotStore(tmp_path),
            manifest_writer=ManifestWriter(tmp_path / "manifest.jsonl"),
            akshare_version="1.18.64",
        )

    assert error.value.code == "invalid_calendar"
    assert not (tmp_path / "data/raw").exists()


def test_trade_calendar_rejects_weekend_rows(tmp_path):
    def weekend_calendar():
        return pd.DataFrame(
            {"trade_date": pd.to_datetime(["2018-01-02", "2026-07-17", "2026-07-18", "2026-07-21"])}
        )

    with pytest.raises(IngestionError) as error:
        run_ingestion(
            CONFIG,
            client=FakeClient(),
            clock=lambda: NOW,
            trade_calendar_provider=weekend_calendar,
            snapshot_store=RawSnapshotStore(tmp_path),
            manifest_writer=ManifestWriter(tmp_path / "data/manifests/manifest.jsonl"),
            akshare_version="1.18.64",
        )

    assert error.value.code == "invalid_calendar"
    assert not (tmp_path / "data/raw").exists()


def test_manifest_batch_failure_publishes_no_partial_records(tmp_path):
    class FailingBatchWriter(ManifestWriter):
        def append_batch(self, records):
            raise RuntimeError("simulated batch publication failure")

    writer = FailingBatchWriter(tmp_path / "data/manifests/manifest.jsonl")

    with pytest.raises(RuntimeError, match="batch publication"):
        run_ingestion(
            CONFIG,
            client=FakeClient(),
            clock=lambda: NOW,
            trade_calendar_provider=_calendar,
            snapshot_store=RawSnapshotStore(tmp_path),
            manifest_writer=writer,
            akshare_version="1.18.64",
        )

    assert not writer.path.exists()
    assert len(list((tmp_path / "data/raw").rglob("*.parquet"))) == 10


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4])
def test_snapshot_failure_at_any_batch_position_never_publishes_manifest(tmp_path, fail_at):
    class FailingSnapshotStore(RawSnapshotStore):
        def __init__(self, root):
            super().__init__(root)
            self.calls = 0

        def write(self, *args, **kwargs):
            self.calls += 1
            if self.calls == fail_at:
                raise SnapshotError("simulated_failure", "simulated snapshot failure")
            return super().write(*args, **kwargs)

    store = FailingSnapshotStore(tmp_path)

    with pytest.raises(SnapshotError, match="simulated snapshot failure"):
        run_ingestion(
            CONFIG,
            client=FakeClient(),
            clock=lambda: NOW,
            trade_calendar_provider=_calendar,
            snapshot_store=store,
            manifest_writer=ManifestWriter(tmp_path / "data/manifests/manifest.jsonl"),
            akshare_version="1.18.64",
        )

    assert not (tmp_path / "data/manifests/manifest.jsonl").exists()
    assert len(list((tmp_path / "data/raw").rglob("*.parquet"))) == fail_at - 1


def _services(client):
    return CliServices(
        client=client,
        clock=lambda: NOW,
        trade_calendar_provider=_calendar,
        akshare_version="1.18.64",
    )


def test_cli_success_emits_one_safe_json_summary(tmp_path, capsys):
    config_path = _copy_real_config(tmp_path)

    exit_code = main(
        ["fetch", "--config", str(config_path), "--project-root", str(tmp_path)],
        services=_services(FakeClient()),
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert payload["status"] == "ok"
    assert len(payload["calendar_id"]) == 64
    assert payload["missing_session_count"] == 0
    assert len(payload["instruments"]) == 10
    assert {item["manifest_status"] for item in payload["instruments"]} == {"appended"}
    assert "secret" not in captured.out.casefold()
    assert captured.out.count("\n") == 1


def test_cli_failure_is_nonzero_safe_json_on_stderr(tmp_path, capsys):
    config_path = _copy_real_config(tmp_path)
    upstream = FetchError("source_group_failed", "market data source group failed", ())

    exit_code = main(
        ["fetch", "--config", str(config_path), "--project-root", str(tmp_path)],
        services=_services(FakeClient(error=upstream)),
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert exit_code != 0
    assert captured.out == ""
    assert payload == {
        "status": "error",
        "error_code": "source_group_failed",
        "error_type": "FetchError",
    }
    assert "upstream" not in captured.err


def test_cli_fetches_and_reopens_one_corporate_action_snapshot(tmp_path, capsys):
    config_path = _copy_real_config(tmp_path)
    event = CorporateActionEvent.create(
        symbol="600519",
        instrument_kind=InstrumentKind.MAIN_BOARD_STOCK,
        announcement_date=date(2026, 6, 1),
        record_date=date(2026, 7, 16),
        ex_date=date(2026, 7, 17),
        payable_date=date(2026, 7, 17),
        cash_dividend_per_unit=Decimal("2"),
        stock_dividend_ratio=Decimal("0"),
        capitalization_ratio=Decimal("0"),
        rights_ratio=Decimal("0"),
        rights_price=None,
        source_schema="akshare.stock_dividend_cninfo.v1",
        source_url="https://webapi.cninfo.com.cn/",
    )

    class CorporateClient(FakeClient):
        def fetch_corporate_actions(self, instrument, *, start, end):
            self.calls.append((instrument, start, end))
            return (event,)

    exit_code = main(
        [
            "corporate-actions",
            "--config",
            str(config_path),
            "--project-root",
            str(tmp_path),
            "--symbol",
            "600519",
        ],
        services=_services(CorporateClient()),
    )

    payload = json.loads(capsys.readouterr().out)
    records = read_corporate_action_manifest(tmp_path)
    loaded = load_verified_corporate_actions(tmp_path, records[0])
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["symbol"] == "600519"
    assert payload["event_count"] == 1
    assert payload["snapshot_id"] == records[0].snapshot_id
    assert loaded.events == (event,)
