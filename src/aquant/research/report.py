"""Deterministic Research Loop v1 report and artifact bundle."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from aquant.research.loop import ResearchLoopResult, ResearchPathResult

REPORT_SCHEMA_VERSION = "1.0.0"


class ResearchReportError(RuntimeError):
    """Raised when a report is invalid or conflicts with an existing bundle."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ResearchReport:
    run_id: str
    assessment: str
    payload: dict[str, bytes]


def _metrics(path: ResearchPathResult) -> dict[str, object]:
    return {
        **asdict(path.metrics),
        "initial_assets_yuan": path.ledger.initial_cash_fen / 100.0,
        "final_assets_yuan": path.ledger.daily_snapshots[-1].equity_fen / 100.0,
        "transaction_count": path.transaction_count,
        "rejected_attempt_count": sum(item.status.value == "rejected" for item in path.attempts),
        "missing_market_session_count": len(path.missing_market_sessions),
    }


def _assessment(result: ResearchLoopResult) -> str:
    strategy = result.strategy.metrics
    benchmark = result.benchmark.metrics
    sharpe_better = (
        strategy.sharpe_zero_rate is not None
        and benchmark.sharpe_zero_rate is not None
        and strategy.sharpe_zero_rate > benchmark.sharpe_zero_rate
    )
    if (
        strategy.total_return > benchmark.total_return
        and sharpe_better
        and strategy.max_drawdown <= benchmark.max_drawdown
    ):
        return "continue_validation"
    return "insufficient_preliminary_evidence"


def _run_json(result: ResearchLoopResult, assessment: str) -> bytes:
    values = {
        "schema_version": result.schema_version,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "run_id": result.run_id,
        "implementation_digest": result.implementation_digest,
        "input_digest": result.input_digest,
        "input_identity": {
            "market_snapshot_id": result.market_snapshot_id,
            "market_file_sha256": result.market_file_sha256,
            "corporate_action_snapshot_id": result.corporate_action_snapshot_id,
            "corporate_action_file_sha256": result.corporate_action_file_sha256,
            "calendar_id": result.calendar_id,
            "calendar_file_sha256": result.calendar_file_sha256,
            "universe_id": result.universe_id,
            "fee_policy_digest": result.fee_policy_digest,
            "price_stream_version": result.price_stream_version,
        },
        "config": {
            "symbol": result.config.symbol,
            "initial_cash_fen": result.config.initial_cash_fen,
            "sma_period": result.config.sma_period,
            "active_weight": str(result.config.active_weight),
            "limits": {key: str(value) for key, value in asdict(result.config.limits).items()},
        },
        "instrument_kind": result.instrument_kind.value,
        "simulation_start": result.simulation_start.isoformat(),
        "simulation_end": result.simulation_end.isoformat(),
        "settlement_buffer_session": result.settlement_buffer_session.isoformat(),
        "assessment": assessment,
        "assessment_rule": (
            "strategy total return and zero-rate Sharpe must exceed benchmark, "
            "and strategy max drawdown must not exceed benchmark"
        ),
        "execution_policy": {
            "signal": "verified indicator close after session close",
            "planner": "frozen A2 effective target state",
            "rebalance_trigger": "effective target-state transition only",
            "fill": "next official session open",
            "lot_size": "100 shares",
            "t_plus_one": True,
            "fees": "verified date-effective fee policy",
            "dividends": ("ex-date entitlement; same-session cash posted after open rebalance"),
            "benchmark": "one initial target-weight buy, then hold",
        },
        "row_counts": {
            "strategy_sessions": len(result.strategy.equity_curve),
            "strategy_plans": len(result.strategy.plans),
            "strategy_attempts": len(result.strategy.attempts),
            "strategy_fills": len(result.strategy.fills),
            "benchmark_sessions": len(result.benchmark.equity_curve),
            "benchmark_fills": len(result.benchmark.fills),
        },
    }
    return (
        json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _metrics_json(result: ResearchLoopResult, assessment: str) -> bytes:
    strategy = _metrics(result.strategy)
    benchmark = _metrics(result.benchmark)
    values = {
        "assessment": assessment,
        "benchmark": benchmark,
        "comparison": {
            "annualized_return_difference": (
                result.strategy.metrics.annualized_return
                - result.benchmark.metrics.annualized_return
            ),
            "max_drawdown_difference": (
                result.strategy.metrics.max_drawdown - result.benchmark.metrics.max_drawdown
            ),
            "sharpe_zero_rate_difference": (
                result.strategy.metrics.sharpe_zero_rate - result.benchmark.metrics.sharpe_zero_rate
                if result.strategy.metrics.sharpe_zero_rate is not None
                and result.benchmark.metrics.sharpe_zero_rate is not None
                else None
            ),
            "total_return_difference": (
                result.strategy.metrics.total_return - result.benchmark.metrics.total_return
            ),
        },
        "definitions": {
            "annual_sessions": 252,
            "risk_free_rate": 0.0,
            "turnover": "gross traded notional divided by average daily equity",
            "transaction_count": "completed buy and sell fills",
        },
        "strategy": strategy,
    }
    return (
        json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _csv_bytes(fieldnames: tuple[str, ...], rows: list[dict[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _drawdowns(values: tuple[float, ...]) -> tuple[float, ...]:
    peak = values[0]
    result: list[float] = []
    for value in values:
        peak = max(peak, value)
        result.append(value / peak - 1.0)
    return tuple(result)


def _equity_csv(result: ResearchLoopResult) -> bytes:
    strategy = result.strategy.equity_curve
    benchmark = result.benchmark.equity_curve
    strategy_drawdown = _drawdowns(tuple(item.equity for item in strategy))
    benchmark_drawdown = _drawdowns(tuple(item.equity for item in benchmark))
    rows = [
        {
            "session": left.date.isoformat(),
            "strategy_equity_yuan": f"{left.equity:.2f}",
            "benchmark_equity_yuan": f"{right.equity:.2f}",
            "strategy_drawdown": f"{left_drawdown:.12f}",
            "benchmark_drawdown": f"{right_drawdown:.12f}",
        }
        for left, right, left_drawdown, right_drawdown in zip(
            strategy,
            benchmark,
            strategy_drawdown,
            benchmark_drawdown,
            strict=True,
        )
    ]
    return _csv_bytes(
        (
            "session",
            "strategy_equity_yuan",
            "benchmark_equity_yuan",
            "strategy_drawdown",
            "benchmark_drawdown",
        ),
        rows,
    )


def _trades_csv(result: ResearchLoopResult) -> bytes:
    rows: list[dict[str, object]] = []
    for path in (result.strategy, result.benchmark):
        rows.extend(
            {
                "portfolio": path.label,
                "order_id": fill.order_id,
                "execution_date": fill.execution_date.isoformat(),
                "side": fill.side,
                "size": fill.size,
                "price": f"{fill.price:.6f}",
                "value_yuan": f"{fill.value:.2f}",
                "total_fees_fen": fill.total_fees_fen,
            }
            for fill in path.fills
        )
    return _csv_bytes(
        (
            "portfolio",
            "order_id",
            "execution_date",
            "side",
            "size",
            "price",
            "value_yuan",
            "total_fees_fen",
        ),
        rows,
    )


def _targets_csv(result: ResearchLoopResult) -> bytes:
    decisions = {item.session: item for item in result.strategy.decisions}
    rows: list[dict[str, object]] = []
    for plan in result.strategy.plans:
        decision = decisions[plan.as_of]
        output = dict(decision.output)
        emitted = output.get(result.config.symbol)
        rows.append(
            {
                "as_of": plan.as_of.isoformat(),
                "data_available": str(decision.data_available).lower(),
                "signal_state": (
                    "no_decision" if emitted is None else "flat" if emitted == 0 else "active"
                ),
                "signal_weight": "" if emitted is None else str(emitted),
                "effective_target_weight": str(plan.targets.get(result.config.symbol, 0)),
            }
        )
    return _csv_bytes(
        (
            "as_of",
            "data_available",
            "signal_state",
            "signal_weight",
            "effective_target_weight",
        ),
        rows,
    )


def _attempts_csv(result: ResearchLoopResult) -> bytes:
    rows = [
        {
            "attempt_id": item.attempt_id,
            "plan_as_of": item.plan_as_of.isoformat(),
            "execution_session": item.execution_session.isoformat(),
            "side": item.side.value,
            "requested_size": item.requested_size,
            "filled_size": item.filled_size,
            "status": item.status.value,
            "rejection_reason": (
                "" if item.rejection_reason is None else item.rejection_reason.value
            ),
            "quantity_adjustment_reason": item.quantity_adjustment_reason or "",
        }
        for item in result.strategy.attempts
    ]
    return _csv_bytes(
        (
            "attempt_id",
            "plan_as_of",
            "execution_session",
            "side",
            "requested_size",
            "filled_size",
            "status",
            "rejection_reason",
            "quantity_adjustment_reason",
        ),
        rows,
    )


def _dividends_csv(result: ResearchLoopResult) -> bytes:
    rows = [
        {
            "portfolio": path.label,
            "event_id": item.event_id,
            "ex_date": item.ex_date.isoformat(),
            "source_payable_date": item.source_payable_date.isoformat(),
            "actual_cash_date": item.actual_cash_date.isoformat(),
            "entitled_size": item.entitled_size,
            "cash_dividend_per_unit": str(item.cash_dividend_per_unit),
            "amount_fen": item.amount_fen,
        }
        for path in (result.strategy, result.benchmark)
        for item in path.dividends
    ]
    return _csv_bytes(
        (
            "portfolio",
            "event_id",
            "ex_date",
            "source_payable_date",
            "actual_cash_date",
            "entitled_size",
            "cash_dividend_per_unit",
            "amount_fen",
        ),
        rows,
    )


def _percent(value: float) -> str:
    return f"{value:.2%}"


def _ratio(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def _markdown(result: ResearchLoopResult, assessment: str) -> str:
    strategy = result.strategy.metrics
    benchmark = result.benchmark.metrics
    verdict = (
        "达到继续验证门槛：值得进入样本外与参数敏感性检验。"
        if assessment == "continue_validation"
        else "未达到继续验证门槛：当前真实样本不足以支持进一步投入。"
    )
    lines = [
        "# Research Loop v1 研究报告",
        "",
        f"- 标的：`{result.config.symbol}`",
        f"- 区间：`{result.simulation_start}` 至 `{result.simulation_end}`",
        f"- 初始资金：{result.config.initial_cash_fen / 100:,.2f} 元",
        f"- 策略：SMA({result.config.sma_period}) → 冻结 Planner → 滚动组合",
        "- Benchmark：首个收盘生成一次目标仓位，次日开盘买入后持有",
        "",
        "## 核心结果",
        "",
        "| 指标 | 策略 | Benchmark |",
        "|---|---:|---:|",
        (
            f"| 最终资产 | {result.strategy.ledger.daily_snapshots[-1].equity_fen / 100:,.2f} | "
            f"{result.benchmark.ledger.daily_snapshots[-1].equity_fen / 100:,.2f} |"
        ),
        f"| 收益率 | {_percent(strategy.total_return)} | {_percent(benchmark.total_return)} |",
        (
            f"| 年化收益 | {_percent(strategy.annualized_return)} | "
            f"{_percent(benchmark.annualized_return)} |"
        ),
        f"| 最大回撤 | {_percent(strategy.max_drawdown)} | {_percent(benchmark.max_drawdown)} |",
        (
            f"| Sharpe（无风险利率 0） | {_ratio(strategy.sharpe_zero_rate)} | "
            f"{_ratio(benchmark.sharpe_zero_rate)} |"
        ),
        (
            f"| 毛换手率 | {_percent(strategy.gross_turnover)} | "
            f"{_percent(benchmark.gross_turnover)} |"
        ),
        (
            f"| 交易次数 | {result.strategy.transaction_count} | "
            f"{result.benchmark.transaction_count} |"
        ),
        "",
        "## 初步判断",
        "",
        verdict,
        "",
        (
            "门槛预先固定为：策略总收益与 Sharpe 均高于 benchmark，且最大回撤不高于 "
            "benchmark。它只是研究优先级判断，不证明 Alpha、稳健性或实盘可交易性。"
        ),
        "",
        "## 审计边界",
        "",
        f"- 行情快照：`{result.market_snapshot_id}`",
        f"- 公司行动快照：`{result.corporate_action_snapshot_id}`",
        f"- 交易日历：`{result.calendar_id}`",
        f"- 费用规则：`{result.fee_policy_digest}`",
        f"- 实现摘要：`{result.implementation_digest}`",
        (
            f"- 结算缓冲交易日：`{result.settlement_buffer_session}`；该日用于确保最后一次买入"
            "具有可验证 T+1 可用日，不进入绩效区间。"
        ),
        "- 同日应付分红在开盘再平衡后入账，不能为该次开盘买入提供资金。",
        "- 收益包含现有日期有效费用假设，未包含滑点、冲击成本、容量约束和税后分红差异。",
        "- 本报告是单标的全样本观察；样本外、参数敏感性和替代 benchmark 均未完成。",
        "",
    ]
    return "\n".join(lines)


def build_research_report(result: ResearchLoopResult) -> ResearchReport:
    """Build deterministic bytes for every P0 research artifact."""
    if type(result) is not ResearchLoopResult:
        raise TypeError("result must be an exact ResearchLoopResult")
    assessment = _assessment(result)
    payload = {
        "run.json": _run_json(result, assessment),
        "metrics.json": _metrics_json(result, assessment),
        "equity.csv": _equity_csv(result),
        "trades.csv": _trades_csv(result),
        "targets.csv": _targets_csv(result),
        "attempts.csv": _attempts_csv(result),
        "dividends.csv": _dividends_csv(result),
        "report.md": _markdown(result, assessment).encode("utf-8"),
    }
    return ResearchReport(
        run_id=result.run_id,
        assessment=assessment,
        payload=payload,
    )


def _complete_payload(report: ResearchReport) -> dict[str, bytes]:
    payload = dict(report.payload)
    manifest = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "complete",
        "run_id": report.run_id,
        "files": {
            name: hashlib.sha256(content).hexdigest() for name, content in sorted(payload.items())
        },
    }
    payload["artifact_manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return payload


def _verify_existing(directory: Path, expected: dict[str, bytes]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ResearchReportError("unsafe_output", "existing report path is unsafe")
    files = {path.name: path for path in directory.iterdir()}
    if set(files) != set(expected):
        raise ResearchReportError("bundle_conflict", "existing report bundle is incomplete")
    for name, content in expected.items():
        metadata = files[name].lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or files[name].read_bytes() != content
        ):
            raise ResearchReportError(
                "bundle_conflict",
                "existing report bundle conflicts with deterministic output",
            )


def publish_research_report(
    report: ResearchReport,
    output_root: str | Path,
) -> Path:
    """Atomically publish or verify one content-bound Research Loop report."""
    if type(report) is not ResearchReport or len(report.run_id) != 64:
        raise ResearchReportError("invalid_report", "research report is invalid")
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ResearchReportError("unsafe_output", "report output root is unsafe")
    expected = _complete_payload(report)
    target = root / report.run_id
    lock_path = root / f".{report.run_id}.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    temporary: Path | None = None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ResearchReportError("unsafe_output", "report lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if target.exists() or target.is_symlink():
            _verify_existing(target, expected)
            return target
        temporary = Path(tempfile.mkdtemp(prefix=f".{report.run_id}.", dir=root))
        for name, content in expected.items():
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
