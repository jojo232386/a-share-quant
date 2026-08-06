"""Verified run-series loading and train/holdout boundaries for Week 5."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Generic, TypeVar

from aquant.backtest.models import (
    EquityRecord,
    FillRecord,
    PositionRecord,
)
from aquant.reporting.risk_report import load_audited_run_metrics
from aquant.risk import RiskMetrics, compute_risk_metrics

_HASH_RE = re.compile(r"[0-9a-f]{64}")


class Week5Error(RuntimeError):
    """Raised when an experiment input cannot support a bounded research run."""


@dataclass(frozen=True)
class RunSeries:
    """One run bundle after the reporting verifier has checked every payload."""

    run_id: str
    symbol: str
    strategy: str
    equity: tuple[EquityRecord, ...]
    positions: tuple[PositionRecord, ...]
    fills: tuple[FillRecord, ...]
    orders: tuple[dict[str, str], ...]
    missing_sessions: tuple[date, ...]
    artifact_manifest_sha256: str
    implementation_digest: str


T = TypeVar("T")


@dataclass(frozen=True)
class RunSeriesWindow(Generic[T]):
    """A non-empty date-bounded portion of one verified run."""

    equity: tuple[EquityRecord, ...]
    positions: tuple[PositionRecord, ...]
    fills: tuple[FillRecord, ...]
    missing_session_count: int

    def __len__(self) -> int:
        return len(self.equity)

    def __getitem__(self, index: int) -> EquityRecord:
        return self.equity[index]


@dataclass(frozen=True)
class SeriesSplit:
    """Training and holdout windows separated by an explicit date boundary."""

    training: RunSeriesWindow[EquityRecord]
    holdout: RunSeriesWindow[EquityRecord]


@dataclass(frozen=True)
class TrainingSelection:
    """One deterministic period choice made without inspecting holdout metrics."""

    period: int
    selection_basis: str
    training_metrics: tuple[tuple[int, RiskMetrics], ...]


@dataclass(frozen=True)
class Week5Report:
    """Deterministic experiment, replay, and human-readable report payloads."""

    experiment_id: str
    json_bytes: bytes
    replay_bytes: bytes
    markdown: str


def _rows(path: Path) -> tuple[dict[str, str], ...]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return tuple(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise Week5Error("run series CSV cannot be read") from exc


def _float(value: str, field: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise Week5Error(f"{field} is not numeric") from exc
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise Week5Error(f"{field} is not finite")
    return parsed


def _integer(value: str, field: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise Week5Error(f"{field} is not an integer") from exc


def load_verified_run_series(path: str | Path) -> RunSeries:
    """Verify one immutable bundle, then load all ledgers needed for window metrics."""
    directory = Path(path)
    audited = load_audited_run_metrics(directory)
    try:
        equity = tuple(
            EquityRecord(
                date=date.fromisoformat(row["date"]),
                equity=_float(row["equity"], "equity"),
            )
            for row in _rows(directory / "equity.csv")
        )
        positions = tuple(
            PositionRecord(
                date=date.fromisoformat(row["date"]),
                size=_integer(row["size"], "position size"),
                close=_float(row["close"], "position close"),
                market_value=_float(row["market_value"], "market value"),
                available_size=_integer(row["available_size"], "available size"),
                locked_size=_integer(row["locked_size"], "locked size"),
            )
            for row in _rows(directory / "positions.csv")
        )
        fills = tuple(
            FillRecord(
                order_id=row["order_id"],
                execution_date=date.fromisoformat(row["execution_date"]),
                side=row["side"],
                size=_integer(row["size"], "fill size"),
                price=_float(row["price"], "fill price"),
                value=_float(row["value"], "fill value"),
                commission=_float(row["commission"], "commission"),
                commission_fen=_integer(row["commission_fen"], "commission fen"),
                stamp_duty_fen=_integer(row["stamp_duty_fen"], "stamp duty fen"),
                transfer_fee_fen=_integer(row["transfer_fee_fen"], "transfer fee fen"),
                total_fees_fen=_integer(row["total_fees_fen"], "total fees fen"),
            )
            for row in _rows(directory / "fills.csv")
        )
        orders = _rows(directory / "orders.csv")
        missing_payload = json.loads(
            (directory / "missing_sessions.json").read_text(encoding="utf-8")
        )
        missing_sessions = tuple(date.fromisoformat(value) for value in missing_payload["dates"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Week5Error("run series ledger schema is invalid") from exc
    if len(equity) != len(positions) or not equity:
        raise Week5Error("run series equity and positions are not aligned")
    if tuple(item.date for item in equity) != tuple(item.date for item in positions):
        raise Week5Error("run series dates are not aligned")
    return RunSeries(
        run_id=audited.run_id,
        symbol=audited.symbol,
        strategy=audited.strategy,
        equity=equity,
        positions=positions,
        fills=fills,
        orders=orders,
        missing_sessions=missing_sessions,
        artifact_manifest_sha256=audited.artifact_manifest_sha256,
        implementation_digest=audited.implementation_digest,
    )


def _window(
    series: RunSeries,
    *,
    start: date,
    end: date,
) -> RunSeriesWindow[EquityRecord]:
    dates = tuple(item.date for item in series.equity)
    if any(left >= right for left, right in zip(dates, dates[1:], strict=False)):
        raise Week5Error("run series dates must be strictly increasing")
    indexes = tuple(
        index
        for index, item in enumerate(series.equity)
        if start <= item.date <= end
    )
    if len(indexes) < 2:
        raise Week5Error("experiment window requires at least two daily bars")
    first, last = indexes[0], indexes[-1]
    equity = series.equity[first : last + 1]
    positions = series.positions[first : last + 1]
    if any(item.date < start or item.date > end for item in equity):
        raise Week5Error("run series window contains an out-of-range bar")
    fills = tuple(
        item for item in series.fills if start <= item.execution_date <= end
    )
    missing_count = sum(start <= item <= end for item in series.missing_sessions)
    return RunSeriesWindow(
        equity=equity,
        positions=positions,
        fills=fills,
        missing_session_count=missing_count,
    )


def split_series(
    series: RunSeries,
    *,
    train_end: date,
    holdout_start: date,
) -> SeriesSplit:
    """Split one verified run without allowing an overlap at the boundary."""
    if (
        type(series) is not RunSeries
        or type(train_end) is not date
        or type(holdout_start) is not date
        or train_end >= holdout_start
    ):
        raise Week5Error("training end must precede holdout start")
    observed_start = series.equity[0].date
    observed_end = series.equity[-1].date
    if train_end < observed_start or holdout_start > observed_end:
        raise Week5Error("experiment split falls outside observed data")
    return SeriesSplit(
        training=_window(series, start=observed_start, end=train_end),
        holdout=_window(series, start=holdout_start, end=observed_end),
    )


def select_training_period(
    candidates: dict[int, RunSeries],
    *,
    train_end: date,
    holdout_start: date,
) -> TrainingSelection:
    """Select one SMA period from training metrics only."""
    if (
        type(candidates) is not dict
        or not candidates
        or any(type(period) is not int or period < 2 for period in candidates)
        or any(type(series) is not RunSeries for series in candidates.values())
    ):
        raise Week5Error("candidate period mapping is invalid")
    metrics: list[tuple[int, RiskMetrics]] = []
    for period, series in sorted(candidates.items()):
        split = split_series(
            series,
            train_end=train_end,
            holdout_start=holdout_start,
        )
        metrics.append(
            (
                period,
                compute_risk_metrics(
                    equity_curve=split.training.equity,
                    positions=split.training.positions,
                    fills=split.training.fills,
                    missing_session_count=split.training.missing_session_count,
                ),
            )
        )
    def score(item: tuple[int, RiskMetrics]) -> tuple[float, float, int]:
        period, value = item
        calmar = value.calmar if value.calmar is not None else float("-inf")
        return calmar, value.total_return, -period

    selected_period = max(metrics, key=score)[0]
    return TrainingSelection(
        period=selected_period,
        selection_basis="training_calmar_then_return_then_smaller_period",
        training_metrics=tuple(metrics),
    )


def _fill_payload(fill: FillRecord) -> dict[str, object]:
    values = asdict(fill)
    values["execution_date"] = fill.execution_date.isoformat()
    return values


def build_week5_replay(
    series: RunSeries,
    *,
    calendar_dates: tuple[date, ...],
    replay_start: date,
    replay_days: int = 10,
) -> tuple[dict[str, object], ...]:
    """Return ten official-session rows for a previously audited run."""
    if (
        type(series) is not RunSeries
        or type(calendar_dates) is not tuple
        or not calendar_dates
        or any(type(item) is not date for item in calendar_dates)
        or any(
            left >= right
            for left, right in zip(calendar_dates, calendar_dates[1:], strict=False)
        )
        or any(item.weekday() >= 5 for item in calendar_dates)
        or type(replay_start) is not date
        or type(replay_days) is not int
        or isinstance(replay_days, bool)
        or not 1 <= replay_days <= 10
    ):
        raise Week5Error("replay calendar contract is invalid")
    replay_dates = tuple(
        item for item in calendar_dates if item >= replay_start
    )[:replay_days]
    if len(replay_dates) != replay_days:
        raise Week5Error("replay calendar has fewer than requested sessions")
    rows: list[dict[str, object]] = []
    for current in replay_dates:
        orders = [
            dict(order)
            for order in series.orders
            if order.get("signal_date") == current.isoformat()
        ]
        fills = [
            _fill_payload(fill)
            for fill in series.fills
            if fill.execution_date == current
        ]
        rows.append(
            {
                "date": current.isoformat(),
                "data_available": any(item.date == current for item in series.equity),
                "orders": orders,
                "fills": fills,
            }
        )
    return tuple(rows)


def _metric_payload(metrics: RiskMetrics) -> dict[str, object]:
    values = asdict(metrics)
    return {
        key: round(value, 12) if type(value) is float and math.isfinite(value) else value
        for key, value in values.items()
    }


def _holdout_interpretation(metrics: RiskMetrics) -> str:
    return (
        "holdout_loss_not_validated_alpha"
        if metrics.total_return < 0
        else "holdout_observation_not_validated_alpha"
    )


def build_week5_report(
    candidate_runs: dict[str, dict[int, RunSeries]],
    baseline_runs: dict[str, dict[str, RunSeries]],
    *,
    expected_universe_id: str,
    expected_symbols: tuple[str, ...],
    calendar_id: str,
    calendar_dates: tuple[date, ...],
    train_end: date,
    holdout_start: date,
    candidate_periods: tuple[int, ...] = (10, 20, 60),
    replay_days: int = 10,
) -> Week5Report:
    """Build one frozen SMA-family report from verified source bundles."""
    if (
        type(expected_universe_id) is not str
        or _HASH_RE.fullmatch(expected_universe_id) is None
        or type(calendar_id) is not str
        or _HASH_RE.fullmatch(calendar_id) is None
        or type(expected_symbols) is not tuple
        or not expected_symbols
        or len(expected_symbols) != len(set(expected_symbols))
        or set(candidate_runs) != set(expected_symbols)
        or set(baseline_runs) != set(expected_symbols)
        or type(candidate_periods) is not tuple
        or not candidate_periods
        or tuple(sorted(set(candidate_periods))) != candidate_periods
    ):
        raise Week5Error("week five experiment input contract is invalid")
    entries: list[dict[str, object]] = []
    replay_rows: list[dict[str, object]] = []
    implementation_digests: set[str] = set()
    for symbol in sorted(expected_symbols):
        candidates = candidate_runs[symbol]
        if set(candidates) != set(candidate_periods):
            raise Week5Error("candidate periods do not match the experiment contract")
        if set(baseline_runs[symbol]) != {"buy_and_hold", "sma20"}:
            raise Week5Error("two fixed baselines are required")
        if any(series.symbol != symbol for series in candidates.values()):
            raise Week5Error("candidate symbol does not match its universe member")
        if any(series.symbol != symbol for series in baseline_runs[symbol].values()):
            raise Week5Error("baseline symbol does not match its universe member")
        implementation_digests.update(
            series.implementation_digest
            for series in (*candidates.values(), *baseline_runs[symbol].values())
        )
        selection = select_training_period(
            candidates,
            train_end=train_end,
            holdout_start=holdout_start,
        )
        selected_training: dict[str, object] | None = None
        selected = candidates[selection.period]
        selected_split = split_series(
            selected,
            train_end=train_end,
            holdout_start=holdout_start,
        )
        holdout_results: list[dict[str, object]] = [
            {
                "label": "selected_sma",
                "period": selection.period,
                "run_id": selected.run_id,
                "artifact_manifest_sha256": selected.artifact_manifest_sha256,
                "metrics": _metric_payload(
                    compute_risk_metrics(
                        equity_curve=selected_split.holdout.equity,
                        positions=selected_split.holdout.positions,
                        fills=selected_split.holdout.fills,
                        missing_session_count=selected_split.holdout.missing_session_count,
                    )
                ),
            }
        ]
        for label in ("buy_and_hold", "sma20"):
            baseline = baseline_runs[symbol][label]
            split = split_series(
                baseline,
                train_end=train_end,
                holdout_start=holdout_start,
            )
            holdout_results.append(
                {
                    "label": label,
                    "period": 20 if label == "sma20" else None,
                    "run_id": baseline.run_id,
                    "artifact_manifest_sha256": baseline.artifact_manifest_sha256,
                    "metrics": _metric_payload(
                        compute_risk_metrics(
                            equity_curve=split.holdout.equity,
                            positions=split.holdout.positions,
                            fills=split.holdout.fills,
                            missing_session_count=split.holdout.missing_session_count,
                        )
                    ),
                }
            )
        for period, metrics in selection.training_metrics:
            run = candidates[period]
            entries_for_replay = {
                "period": period,
                "run_id": run.run_id,
                "artifact_manifest_sha256": run.artifact_manifest_sha256,
                "metrics": _metric_payload(metrics),
            }
            if period == selection.period:
                selected_training = entries_for_replay
        if selected_training is None:
            raise Week5Error("selected period has no training evidence")
        replay_rows.extend(
            {
                "symbol": symbol,
                "strategy": "selected_sma",
                "selected_period": selection.period,
                **row,
            }
            for row in build_week5_replay(
                selected,
                calendar_dates=calendar_dates,
                replay_start=holdout_start,
                replay_days=replay_days,
            )
        )
        entries.append(
            {
                "symbol": symbol,
                "selection_basis": selection.selection_basis,
                "selected_period": selection.period,
                "training_candidates": [
                    {
                        "period": period,
                        "run_id": candidates[period].run_id,
                        "artifact_manifest_sha256": candidates[period].artifact_manifest_sha256,
                        "metrics": _metric_payload(metrics),
                    }
                    for period, metrics in selection.training_metrics
                ],
                "selected_training": selected_training,
                "holdout": holdout_results,
            }
        )
    if len(implementation_digests) != 1:
        raise Week5Error("week five experiment mixes implementation fingerprints")
    identity = {
        "candidate_periods": list(candidate_periods),
        "experiment_kind": "week5_restricted_sma",
        "holdout_start": holdout_start.isoformat(),
        "implementation_digest": next(iter(implementation_digests)),
        "replay_days": replay_days,
        "schema_version": "1.0",
        "selection_basis": "training_calmar_then_return_then_smaller_period",
        "symbols": entries,
        "train_end": train_end.isoformat(),
        "universe_id": expected_universe_id,
        "calendar_id": calendar_id,
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    experiment_id = hashlib.sha256(canonical.encode()).hexdigest()
    payload = {"experiment_id": experiment_id, **identity}
    json_bytes = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    replay_payload = {
        "experiment_id": experiment_id,
        "replay_kind": "ten_official_session_process_replay",
        "rows": replay_rows,
        "schema_version": "1.0",
    }
    replay_bytes = (
        json.dumps(replay_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    lines = [
        "# 第 5 周受限 SMA 实验与 10 日操作流程回放",
        "",
        f"- experiment_id: `{experiment_id}`",
        f"- universe_id: `{expected_universe_id}`",
        f"- calendar_id: `{calendar_id}`",
        f"- implementation_digest: `{identity['implementation_digest']}`",
        f"- 训练期截止：`{train_end.isoformat()}`",
        f"- 保留期开始：`{holdout_start.isoformat()}`",
        f"- 候选周期：{', '.join(str(value) for value in candidate_periods)}",
        "- 选择规则：只看训练期 Calmar、总收益，再以更小周期破平局",
        "- 边界：保留期只运行选中周期、Buy & Hold 和冻结 SMA(20)；不证明 Alpha 或实盘盈利。",
        "",
        "| 标的 | 选中周期 | 选中 SMA 保留期收益 | Buy & Hold 保留期收益 | SMA(20) 保留期收益 |",
        "|---|---:|---:|---:|---:|",
    ]
    for entry in entries:
        holdout = {item["label"]: item for item in entry["holdout"]}
        lines.append(
            "| "
            + " | ".join(
                [
                    str(entry["symbol"]),
                    str(entry["selected_period"]),
                    f"{holdout['selected_sma']['metrics']['total_return']:.6f}",
                    f"{holdout['buy_and_hold']['metrics']['total_return']:.6f}",
                    f"{holdout['sma20']['metrics']['total_return']:.6f}",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            (
                "训练期所有候选指标和未选周期均保留；10 日回放只验证数据、信号、订单、"
                "拒单和成交日志链路。"
            ),
            "保留期正负结果均为冻结样本观察，不能跨越为长期收益证据。",
            "",
        ]
    )
    return Week5Report(
        experiment_id=experiment_id,
        json_bytes=json_bytes,
        replay_bytes=replay_bytes,
        markdown="\n".join(lines),
    )


def publish_week5_report(report: Week5Report, output_root: str | Path) -> Path:
    """Atomically publish one deterministic Week 5 artifact package."""
    if type(report) is not Week5Report or _HASH_RE.fullmatch(report.experiment_id) is None:
        raise Week5Error("week five report identity is invalid")
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise Week5Error("week five output root is unsafe")
    payload = {
        "experiment.json": report.json_bytes,
        "replay.json": report.replay_bytes,
        "report.md": report.markdown.encode("utf-8"),
    }
    manifest = {
        "experiment_id": report.experiment_id,
        "files": {
            name: hashlib.sha256(value).hexdigest()
            for name, value in sorted(payload.items())
        },
        "schema_version": "1.0",
        "status": "complete",
    }
    payload["artifact_manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    target = root / report.experiment_id
    lock_path = root / f".{report.experiment_id}.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    temporary: Path | None = None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise Week5Error("week five lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_dir():
                raise Week5Error("existing week five report is unsafe")
            files = {path.name: path for path in target.iterdir()}
            if set(files) != set(payload) or any(
                files[name].read_bytes() != value
                for name, value in payload.items()
            ):
                raise Week5Error("existing week five report conflicts with deterministic output")
            return target
        temporary = Path(tempfile.mkdtemp(prefix=f".{report.experiment_id}.", dir=root))
        for name, content in payload.items():
            with (temporary / name).open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        return target
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
