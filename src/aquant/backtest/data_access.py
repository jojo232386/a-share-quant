"""Verified bridge from Week 1 raw snapshots into Week 2 backtests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from aquant.backtest.feed import BacktestDataError, canonical_market_digest
from aquant.backtest.models import DataProvenance
from aquant.data import (
    DataQualityError,
    NormalizationError,
    SourceSchema,
    normalize_market_frame,
    validate_market_frame,
)
from aquant.data.akshare_client import SourceContractError, validate_source_contract
from aquant.data.manifest import ManifestRecord
from aquant.data.snapshot import RawSnapshotStore, SnapshotError
from aquant.rules import InstrumentKind

_VERIFIED_MARKET_DATA_TOKEN = object()


@dataclass(frozen=True, init=False)
class VerifiedMarketData:
    """Copy-on-read canonical data created only by the verified snapshot loader."""

    _frame: pd.DataFrame = field(repr=False, compare=False)
    provenance: DataProvenance
    input_digest: str

    def __init__(
        self,
        frame: pd.DataFrame,
        provenance: DataProvenance,
        input_digest: str,
        *,
        _token: object,
    ) -> None:
        if _token is not _VERIFIED_MARKET_DATA_TOKEN:
            raise TypeError("VerifiedMarketData must be created by load_verified_snapshot")
        object.__setattr__(self, "_frame", frame.copy(deep=True))
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "input_digest", input_digest)

    @property
    def frame(self) -> pd.DataFrame:
        """Return an isolated copy so callers cannot mutate verified contents."""
        return self._frame.copy(deep=True)


def _validate_manifest_for_execution(record: ManifestRecord) -> SourceSchema:
    if record.adjustment != "" or record.factor_source is not None:
        raise BacktestDataError(
            "adjusted_price_forbidden",
            "only unadjusted manifest records may enter execution and accounting",
        )
    if any(record.quality_issue_counts.values()):
        raise BacktestDataError(
            "manifest_quality_violations",
            "manifest records with data-quality violations cannot enter a backtest",
        )
    try:
        source_schema = SourceSchema(record.source_schema)
        validate_source_contract(
            symbol=record.symbol,
            instrument_kind=record.instrument_kind,
            provider=record.provider,
            source_function=record.source_function,
            source_schema=source_schema,
            endpoint_host=record.endpoint_host,
            provider_symbol=record.provider_symbol,
            raw_volume_unit=record.raw_volume_unit,
            volume_multiplier_to_canonical=record.volume_multiplier_to_canonical,
            full_history_download=record.full_history_download,
            local_date_slice=record.local_date_slice,
        )
    except SourceContractError as exc:
        raise BacktestDataError(
            "source_contract_violation",
            "manifest provenance does not match a supported v0.1 source contract",
        ) from exc
    except ValueError as exc:
        raise BacktestDataError(
            "unsupported_source_schema",
            "manifest source schema is unsupported by the canonical adapter",
        ) from exc
    return source_schema


def load_verified_snapshot(
    project_root: str | Path,
    record: ManifestRecord,
) -> VerifiedMarketData:
    """Verify content hash, normalize source units, and re-run the quality gate."""
    if type(record) is not ManifestRecord:
        raise TypeError("record must be an exact ManifestRecord")
    root = Path(project_root)
    source_schema = _validate_manifest_for_execution(record)
    store = RawSnapshotStore(root)
    try:
        store.verify(record.snapshot_relative_path, expected_hash=record.file_sha256)
        raw = pd.read_parquet(root / record.snapshot_relative_path)
        store.verify(record.snapshot_relative_path, expected_hash=record.file_sha256)
        canonical = normalize_market_frame(raw, source_schema=source_schema)
        report = validate_market_frame(canonical)
    except SnapshotError as exc:
        raise BacktestDataError(
            "snapshot_verification_failed",
            "manifest-referenced snapshot failed content verification",
        ) from exc
    except (OSError, ValueError) as exc:
        if isinstance(exc, (NormalizationError, DataQualityError)):
            code = exc.code
        else:
            code = "unreadable_snapshot"
        raise BacktestDataError(
            "snapshot_data_invalid",
            f"verified snapshot cannot be converted to canonical market data: {code}",
        ) from exc

    if (
        report.row_count != record.row_count
        or report.start_date != record.actual_start
        or report.end_date != record.actual_end
    ):
        raise BacktestDataError(
            "manifest_content_mismatch",
            "canonical snapshot range or row count does not match its manifest",
        )

    return VerifiedMarketData(
        frame=canonical,
        provenance=DataProvenance(
            symbol=record.symbol,
            snapshot_id=record.snapshot_id,
            file_sha256=record.file_sha256,
            adjustment=record.adjustment,
            verification_method="manifest_sha256",
            instrument_kind=InstrumentKind(record.instrument_kind),
        ),
        input_digest=canonical_market_digest(canonical),
        _token=_VERIFIED_MARKET_DATA_TOKEN,
    )
