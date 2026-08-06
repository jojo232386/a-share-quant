"""Verify completed run bundles and build deterministic risk reports."""

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

from aquant.backtest.models import EquityRecord, FillRecord, PositionRecord
from aquant.risk import RiskMetrics, compute_risk_metrics

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_STRATEGIES = frozenset({"buy_and_hold", "sma"})
_REQUIRED_PAYLOADS = frozenset(
    {
        "run.json",
        "orders.csv",
        "fills.csv",
        "positions.csv",
        "cash.csv",
        "equity.csv",
        "lots.csv",
        "corporate_actions.csv",
        "receivables.csv",
        "missing_sessions.json",
    }
)


class RiskReportError(RuntimeError):
    """Raised when a report input or output contract is incomplete."""


@dataclass(frozen=True)
class AuditedRunMetrics:
    run_id: str
    artifact_manifest_sha256: str
    symbol: str
    strategy: str
    universe_id: str | None
    implementation_digest: str
    snapshot_id: str
    corporate_action_snapshot_id: str
    calendar_id: str | None
    observation_start: date
    observation_end: date
    missing_session_count: int
    rejection_count: int
    metrics: RiskMetrics


@dataclass(frozen=True)
class RiskReport:
    report_id: str
    json_bytes: bytes
    markdown: str


@dataclass(frozen=True)
class RiskReportVerification:
    report_id: str
    universe_id: str
    run_count: int


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RiskReportError("report input JSON is unreadable") from exc


def _safe_files(directory: Path) -> dict[str, Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise RiskReportError("run bundle must be a safe directory")
    result: dict[str, Path] = {}
    for path in directory.iterdir():
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RiskReportError("run bundle contains an unsafe file")
        result[path.name] = path
    return result


def _verify_bundle(directory: Path) -> tuple[dict[str, object], str]:
    files = _safe_files(directory)
    expected_names = _REQUIRED_PAYLOADS | {"artifact_manifest.json"}
    if set(files) != expected_names:
        raise RiskReportError("run bundle file set is incomplete")
    manifest = _load_json(files["artifact_manifest.json"])
    if (
        type(manifest) is not dict
        or manifest.get("status") != "complete"
        or manifest.get("run_id") != directory.name
        or type(manifest.get("files")) is not dict
        or set(manifest["files"]) != _REQUIRED_PAYLOADS
    ):
        raise RiskReportError("artifact manifest contract is invalid")
    for name in _REQUIRED_PAYLOADS:
        expected = manifest["files"].get(name)
        actual = hashlib.sha256(files[name].read_bytes()).hexdigest()
        if (
            type(expected) is not str
            or _HASH_RE.fullmatch(expected) is None
            or expected != actual
        ):
            raise RiskReportError("artifact SHA-256 verification failed")
    metadata = _load_json(files["run.json"])
    if type(metadata) is not dict or metadata.get("run_id") != directory.name:
        raise RiskReportError("run metadata identity is invalid")
    return (
        metadata,
        hashlib.sha256(
            files["artifact_manifest.json"].read_bytes()
        ).hexdigest(),
    )


def _csv_rows(path: Path) -> tuple[dict[str, str], ...]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return tuple(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RiskReportError("run ledger CSV is unreadable") from exc


def _finite_float(value: object, *, field: str) -> float:
    if type(value) is not str:
        raise RiskReportError(f"{field} is not a canonical CSV value")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RiskReportError(f"{field} is not numeric") from exc
    if not math.isfinite(parsed):
        raise RiskReportError(f"{field} must be finite")
    return parsed


def _integer(value: object, *, field: str) -> int:
    if type(value) is not str or re.fullmatch(r"-?[0-9]+", value) is None:
        raise RiskReportError(f"{field} is not an integer")
    return int(value)


def _ledger_records(
    directory: Path,
) -> tuple[
    tuple[EquityRecord, ...],
    tuple[PositionRecord, ...],
    tuple[FillRecord, ...],
    int,
]:
    try:
        equity = tuple(
            EquityRecord(
                date=date.fromisoformat(row["date"]),
                equity=_finite_float(row["equity"], field="equity"),
            )
            for row in _csv_rows(directory / "equity.csv")
        )
        positions = tuple(
            PositionRecord(
                date=date.fromisoformat(row["date"]),
                size=_integer(row["size"], field="position size"),
                close=_finite_float(row["close"], field="position close"),
                market_value=_finite_float(
                    row["market_value"],
                    field="market value",
                ),
                available_size=_integer(
                    row["available_size"],
                    field="available size",
                ),
                locked_size=_integer(row["locked_size"], field="locked size"),
            )
            for row in _csv_rows(directory / "positions.csv")
        )
        fills = tuple(
            FillRecord(
                order_id=row["order_id"],
                execution_date=date.fromisoformat(row["execution_date"]),
                side=row["side"],
                size=_integer(row["size"], field="fill size"),
                price=_finite_float(row["price"], field="fill price"),
                value=_finite_float(row["value"], field="fill value"),
                commission=_finite_float(
                    row["commission"],
                    field="commission",
                ),
                commission_fen=_integer(
                    row["commission_fen"],
                    field="commission fen",
                ),
                stamp_duty_fen=_integer(
                    row["stamp_duty_fen"],
                    field="stamp duty fen",
                ),
                transfer_fee_fen=_integer(
                    row["transfer_fee_fen"],
                    field="transfer fee fen",
                ),
                total_fees_fen=_integer(
                    row["total_fees_fen"],
                    field="total fees fen",
                ),
            )
            for row in _csv_rows(directory / "fills.csv")
        )
        orders = _csv_rows(directory / "orders.csv")
        cash_rows = _csv_rows(directory / "cash.csv")
        receivable_rows = _csv_rows(directory / "receivables.csv")
    except (KeyError, ValueError) as exc:
        raise RiskReportError("run ledger schema is invalid") from exc
    if not (
        len(equity)
        == len(positions)
        == len(cash_rows)
        == len(receivable_rows)
    ):
        raise RiskReportError("accounting ledgers have inconsistent row counts")
    for equity_row, position, cash_row, receivable_row in zip(
        equity,
        positions,
        cash_rows,
        receivable_rows,
        strict=True,
    ):
        try:
            cash_date = date.fromisoformat(cash_row["date"])
            receivable_date = date.fromisoformat(receivable_row["date"])
            cash_value = _finite_float(cash_row["cash"], field="cash")
            receivable = _finite_float(
                receivable_row["balance"],
                field="receivable",
            )
        except (KeyError, ValueError) as exc:
            raise RiskReportError("accounting ledger schema is invalid") from exc
        expected = cash_value + position.market_value + receivable
        if (
            equity_row.date != cash_date
            or cash_date != receivable_date
            or not math.isclose(
                equity_row.equity,
                expected,
                rel_tol=1e-12,
                abs_tol=1e-8,
            )
        ):
            raise RiskReportError("daily accounting identity does not hold")
    rejections = sum(
        row.get("final_status") == "rejected"
        for row in orders
    )
    return equity, positions, fills, rejections


def load_audited_run_metrics(path: str | Path) -> AuditedRunMetrics:
    """Reopen one immutable run bundle and recompute metrics from its ledgers."""
    directory = Path(path)
    metadata, artifact_manifest_sha256 = _verify_bundle(directory)
    equity, positions, fills, rejection_count = _ledger_records(directory)
    provenance = metadata.get("provenance")
    config = metadata.get("config")
    actions = metadata.get("corporate_action_provenance")
    rules = metadata.get("rule_provenance")
    row_counts = metadata.get("row_counts")
    if not all(
        type(value) is dict
        for value in (provenance, config, actions, row_counts)
    ):
        raise RiskReportError("run metadata provenance is incomplete")
    if (
        row_counts.get("equity") != len(equity)
        or row_counts.get("positions") != len(positions)
        or row_counts.get("fills") != len(fills)
        or row_counts.get("cash") != len(equity)
        or row_counts.get("receivables") != len(equity)
        or not equity
    ):
        raise RiskReportError("run metadata row counts do not match ledgers")
    run_id = metadata.get("run_id")
    symbol = provenance.get("symbol")
    strategy = config.get("strategy")
    implementation_digest = metadata.get("implementation_digest")
    universe_id = metadata.get("universe_id")
    if (
        type(run_id) is not str
        or _HASH_RE.fullmatch(run_id) is None
        or type(symbol) is not str
        or re.fullmatch(r"[0-9]{6}", symbol) is None
        or strategy not in _STRATEGIES
        or type(implementation_digest) is not str
        or _HASH_RE.fullmatch(implementation_digest) is None
        or universe_id is not None
        and (
            type(universe_id) is not str
            or _HASH_RE.fullmatch(universe_id) is None
        )
    ):
        raise RiskReportError("run metadata identity fields are invalid")
    missing_count = row_counts.get("missing_sessions")
    if type(missing_count) is not int or isinstance(missing_count, bool):
        raise RiskReportError("missing-session count is invalid")
    return AuditedRunMetrics(
        run_id=run_id,
        artifact_manifest_sha256=artifact_manifest_sha256,
        symbol=symbol,
        strategy=strategy,
        universe_id=universe_id,
        implementation_digest=implementation_digest,
        snapshot_id=provenance["snapshot_id"],
        corporate_action_snapshot_id=actions["snapshot_id"],
        calendar_id=(
            rules.get("calendar_id")
            if type(rules) is dict
            else None
        ),
        observation_start=equity[0].date,
        observation_end=equity[-1].date,
        missing_session_count=missing_count,
        rejection_count=rejection_count,
        metrics=compute_risk_metrics(
            equity_curve=equity,
            positions=positions,
            fills=fills,
            missing_session_count=missing_count,
        ),
    )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 12)


def _metric_dict(metrics: RiskMetrics) -> dict[str, object]:
    values = asdict(metrics)
    return {
        key: (
            _rounded(value)
            if type(value) is float
            else value
        )
        for key, value in values.items()
    }


def _display_metric(metrics: dict[str, object], key: str) -> str:
    value = metrics[key]
    return "N/A" if value is None else f"{value:.6f}"


def build_independent_batch_report(
    runs: tuple[AuditedRunMetrics, ...],
    *,
    expected_universe_id: str,
    expected_symbols: tuple[str, ...],
    max_drawdown_limit: float = 0.50,
    max_exposure_limit: float = 1.00,
) -> RiskReport:
    """Build a stable report for independent runs, never a shared portfolio."""
    if (
        type(runs) is not tuple
        or not runs
        or any(type(item) is not AuditedRunMetrics for item in runs)
        or type(expected_universe_id) is not str
        or _HASH_RE.fullmatch(expected_universe_id) is None
        or type(expected_symbols) is not tuple
        or not expected_symbols
        or len(expected_symbols) != len(set(expected_symbols))
    ):
        raise RiskReportError("batch report input contract is invalid")
    if any(item.universe_id != expected_universe_id for item in runs):
        raise RiskReportError("run universe identity does not match report")
    pairs = tuple((item.symbol, item.strategy) for item in runs)
    if len(pairs) != len(set(pairs)):
        raise RiskReportError("batch contains a duplicate symbol-strategy pair")
    if set(item.symbol for item in runs) != set(expected_symbols):
        raise RiskReportError("batch symbols do not match the verified universe")
    if any(
        {item.strategy for item in runs if item.symbol == symbol}
        != _STRATEGIES
        for symbol in expected_symbols
    ):
        raise RiskReportError("each symbol requires both baseline strategies")
    implementations = {item.implementation_digest for item in runs}
    if len(implementations) != 1:
        raise RiskReportError("batch mixes implementation fingerprints")
    if (
        type(max_drawdown_limit) not in {int, float}
        or not 0 <= max_drawdown_limit <= 1
        or type(max_exposure_limit) not in {int, float}
        or not 0 < max_exposure_limit <= 1
    ):
        raise RiskReportError("risk limits are invalid")

    rows: list[dict[str, object]] = []
    for item in sorted(runs, key=lambda value: (value.symbol, value.strategy)):
        breach = (
            item.metrics.max_drawdown > max_drawdown_limit
            or item.metrics.max_gross_exposure > max_exposure_limit
        )
        interpretation = (
            "risk_limit_breach"
            if breach
            else "strategy_loss"
            if item.metrics.total_return < 0
            else "observed_positive_return_not_validated_alpha"
        )
        rows.append(
            {
                "artifact_manifest_sha256": (
                    item.artifact_manifest_sha256
                ),
                "calendar_id": item.calendar_id,
                "corporate_action_snapshot_id": (
                    item.corporate_action_snapshot_id
                ),
                "implementation_digest": item.implementation_digest,
                "interpretation": interpretation,
                "metrics": _metric_dict(item.metrics),
                "missing_session_count": item.missing_session_count,
                "observation_end": item.observation_end.isoformat(),
                "observation_start": item.observation_start.isoformat(),
                "rejection_count": item.rejection_count,
                "run_id": item.run_id,
                "snapshot_id": item.snapshot_id,
                "strategy": item.strategy,
                "symbol": item.symbol,
            }
        )
    identity = {
        "annualization": "252_trading_sessions",
        "implementation_digest": next(iter(implementations)),
        "max_drawdown_limit": max_drawdown_limit,
        "max_exposure_limit": max_exposure_limit,
        "renderer_version": "2",
        "report_kind": "independent_single_instrument_batch",
        "risk_free_rate": 0.0,
        "runs": rows,
        "schema_version": "1.1",
        "universe_id": expected_universe_id,
    }
    canonical_identity = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    report_id = hashlib.sha256(canonical_identity.encode()).hexdigest()
    payload = {"report_id": report_id, **identity}
    json_bytes = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    lines = [
        "# A 股 10 标的独立基准风险报告",
        "",
        f"- report_id: `{report_id}`",
        f"- universe_id: `{expected_universe_id}`",
        "- 口径：252 个交易日年化、零无风险利率、日简单收益",
        "- 边界：本报告是独立单标的批次，不构成共享现金组合，不证明策略有效。",
        "",
        (
            "| 标的 | 策略 | 总收益 | 年化收益 | 年化波动 | 最大回撤 | "
            "Sharpe | Calmar | 累计毛换手 | 年化毛换手 | 最大敞口 | 解释 |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        metrics = row["metrics"]
        assert type(metrics) is dict

        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["symbol"]),
                    str(row["strategy"]),
                    _display_metric(metrics, "total_return"),
                    _display_metric(metrics, "annualized_return"),
                    _display_metric(metrics, "annualized_volatility"),
                    _display_metric(metrics, "max_drawdown"),
                    _display_metric(metrics, "sharpe_zero_rate"),
                    _display_metric(metrics, "calmar"),
                    _display_metric(metrics, "gross_turnover"),
                    _display_metric(metrics, "annualized_gross_turnover"),
                    _display_metric(metrics, "max_gross_exposure"),
                    str(row["interpretation"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "正收益只表示这段已冻结历史样本中的观测结果；负收益与风险越限均原样保留。",
            "",
            "## 源回测包验真清单",
            "",
            "| 标的 | 策略 | run ID | artifact manifest SHA-256 |",
            "|---|---|---|---|",
        ]
    )
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["symbol"]),
                    str(row["strategy"]),
                    f"`{row['run_id']}`",
                    f"`{row['artifact_manifest_sha256']}`",
                ]
            )
            + " |"
        )
    lines.append("")
    return RiskReport(
        report_id=report_id,
        json_bytes=json_bytes,
        markdown="\n".join(lines),
    )


def _report_contents(report: RiskReport) -> dict[str, bytes]:
    payload = {
        "report.json": report.json_bytes,
        "report.md": report.markdown.encode("utf-8"),
    }
    manifest = {
        "files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(payload.items())
        },
        "report_id": report.report_id,
        "schema_version": "1.0",
        "status": "complete",
    }
    payload["artifact_manifest.json"] = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    return payload


def _verify_existing_report(
    directory: Path,
    expected: dict[str, bytes],
) -> None:
    files = _safe_files(directory)
    if set(files) != set(expected):
        raise RiskReportError("existing report bundle is incomplete")
    if any(files[name].read_bytes() != content for name, content in expected.items()):
        raise RiskReportError("existing report conflicts with deterministic output")


def verify_published_risk_report(
    report_directory: str | Path,
    backtest_root: str | Path,
) -> RiskReportVerification:
    """Rebuild one published report from its current immutable source runs."""
    directory = Path(report_directory)
    if _HASH_RE.fullmatch(directory.name) is None:
        raise RiskReportError("report directory identity is invalid")
    files = _safe_files(directory)
    if set(files) != {
        "artifact_manifest.json",
        "report.json",
        "report.md",
    }:
        raise RiskReportError("published report file set is incomplete")
    manifest = _load_json(files["artifact_manifest.json"])
    if (
        type(manifest) is not dict
        or manifest.get("status") != "complete"
        or manifest.get("report_id") != directory.name
        or type(manifest.get("files")) is not dict
        or set(manifest["files"]) != {"report.json", "report.md"}
    ):
        raise RiskReportError("published report manifest contract is invalid")
    for name in ("report.json", "report.md"):
        expected_hash = manifest["files"].get(name)
        actual_hash = hashlib.sha256(files[name].read_bytes()).hexdigest()
        if (
            type(expected_hash) is not str
            or _HASH_RE.fullmatch(expected_hash) is None
            or expected_hash != actual_hash
        ):
            raise RiskReportError("published report SHA-256 verification failed")

    payload = _load_json(files["report.json"])
    if type(payload) is not dict:
        raise RiskReportError("published report JSON contract is invalid")
    report_id = payload.get("report_id")
    universe_id = payload.get("universe_id")
    rows = payload.get("runs")
    if (
        type(report_id) is not str
        or report_id != directory.name
        or _HASH_RE.fullmatch(report_id) is None
        or type(universe_id) is not str
        or _HASH_RE.fullmatch(universe_id) is None
        or type(rows) is not list
        or not rows
        or any(type(row) is not dict for row in rows)
    ):
        raise RiskReportError("published report identity is invalid")
    identity = dict(payload)
    del identity["report_id"]
    canonical_identity = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if hashlib.sha256(canonical_identity.encode()).hexdigest() != report_id:
        raise RiskReportError("published report ID verification failed")

    run_ids = tuple(row.get("run_id") for row in rows)
    symbols = tuple(row.get("symbol") for row in rows)
    if (
        any(type(run_id) is not str or _HASH_RE.fullmatch(run_id) is None for run_id in run_ids)
        or len(run_ids) != len(set(run_ids))
        or any(
            type(symbol) is not str
            or re.fullmatch(r"[0-9]{6}", symbol) is None
            for symbol in symbols
        )
    ):
        raise RiskReportError("published report source identities are invalid")
    source_root = Path(backtest_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise RiskReportError("backtest root must be a safe directory")
    runs = tuple(
        load_audited_run_metrics(source_root / run_id)
        for run_id in run_ids
    )
    rebuilt = build_independent_batch_report(
        runs,
        expected_universe_id=universe_id,
        expected_symbols=tuple(sorted(set(symbols))),
        max_drawdown_limit=payload.get("max_drawdown_limit"),
        max_exposure_limit=payload.get("max_exposure_limit"),
    )
    if (
        rebuilt.report_id != report_id
        or rebuilt.json_bytes != files["report.json"].read_bytes()
        or rebuilt.markdown.encode("utf-8") != files["report.md"].read_bytes()
    ):
        raise RiskReportError("published report does not match current source runs")
    return RiskReportVerification(
        report_id=report_id,
        universe_id=universe_id,
        run_count=len(runs),
    )


def publish_risk_report(
    report: RiskReport,
    output_root: str | Path,
) -> Path:
    """Atomically publish or verify one deterministic report bundle."""
    if (
        type(report) is not RiskReport
        or _HASH_RE.fullmatch(report.report_id) is None
    ):
        raise RiskReportError("risk report object is invalid")
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise RiskReportError("report output root must be a safe directory")
    contents = _report_contents(report)
    target = root / report.report_id
    lock_path = root / f".{report.report_id}.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise RiskReportError("report lock cannot be opened safely") from exc
    temporary: Path | None = None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RiskReportError("report lock is not a safe regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if target.exists() or target.is_symlink():
            _verify_existing_report(target, contents)
            return target
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{report.report_id}.",
                dir=root,
            )
        )
        for name, content in contents.items():
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
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
