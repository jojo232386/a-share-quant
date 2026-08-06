# Close Risk Report P2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让已发布风险报告能反向核对当前源回测包，并在人类可读报告中显示每个源包的完整 SHA-256。

**Architecture:** 风险报告构建器增加渲染版本并输出独立的源包清单；验证器重新验真报告自身，
再按报告中的 run ID 加载当前回测包并重建报告，要求 JSON 与 Markdown 字节完全一致。CLI
只接受项目根目录内的内容寻址路径，成功时输出机器可读 JSON。

**Tech Stack:** Python 3.11、argparse、SHA-256、pytest、现有 `aquant-report` CLI。

---

### Task 1: Markdown 源包哈希

**Files:**
- Modify: `src/aquant/reporting/risk_report.py`
- Test: `tests/unit/test_risk_reporting.py`

- [x] **Step 1: 写失败测试**

```python
def test_markdown_lists_each_source_artifact_manifest_hash(tmp_path):
    _, buy_directory = _bundle(tmp_path)
    _, sma_directory = _bundle(tmp_path, StrategyName.SMA)
    buy = replace(load_audited_run_metrics(buy_directory), universe_id="a" * 64)
    sma = replace(load_audited_run_metrics(sma_directory), universe_id="a" * 64)
    report = build_independent_batch_report(
        (buy, sma),
        expected_universe_id="a" * 64,
        expected_symbols=("600519",),
    )
    assert "## 源回测包验真清单" in report.markdown
    assert buy.artifact_manifest_sha256 in report.markdown
    assert sma.artifact_manifest_sha256 in report.markdown
```

- [x] **Step 2: 运行测试并确认因缺少清单而失败**

Run: `uv run pytest -q tests/unit/test_risk_reporting.py -k source_artifact`
Expected: FAIL，Markdown 中找不到标题或哈希。

- [x] **Step 3: 最小实现**

在报告 identity 中增加 `renderer_version="2"`，将 schema 升为 `1.1`，并在指标表后追加：

```python
[
    "## 源回测包验真清单",
    "",
    "| 标的 | 策略 | run ID | artifact manifest SHA-256 |",
]
```

每行输出完整 run ID 和完整 `artifact_manifest_sha256`。

- [x] **Step 4: 运行目标测试和风险报告测试**

Run: `uv run pytest -q tests/unit/test_risk_reporting.py`
Expected: PASS。

- [x] **Step 5: 提交**

```text
feat: expose source hashes in risk reports
```

### Task 2: 已发布报告反向验证器

**Files:**
- Modify: `src/aquant/reporting/risk_report.py`
- Modify: `src/aquant/reporting/__init__.py`
- Test: `tests/unit/test_risk_reporting.py`

- [x] **Step 1: 写成功与篡改失败测试**

```python
verified = verify_published_risk_report(report_dir, backtest_root)
assert verified.report_id == report.report_id

(run_dir / "cash.csv").write_text("tampered")
with pytest.raises(RiskReportError):
    verify_published_risk_report(report_dir, backtest_root)
```

- [x] **Step 2: 运行并确认因函数不存在而失败**

Run: `uv run pytest -q tests/unit/test_risk_reporting.py -k published`
Expected: FAIL，验证器尚不存在。

- [x] **Step 3: 最小实现**

验证器必须：

```python
1. 要求报告目录名为 64 位小写 SHA-256；
2. 要求目录只含 report.json、report.md、artifact_manifest.json；
3. 验证报告 manifest 中两个文件的 SHA-256；
4. 删除 report_id 后规范序列化 JSON，复算 report ID；
5. 按 runs[*].run_id 打开当前源包并调用 load_audited_run_metrics；
6. 使用报告中的 universe、阈值和标的集合重建报告；
7. 要求重建 JSON、Markdown 与已发布内容逐字节一致。
```

- [x] **Step 4: 运行目标测试和全文件测试**

Run: `uv run pytest -q tests/unit/test_risk_reporting.py`
Expected: PASS。

- [x] **Step 5: 提交**

```text
feat: verify published reports against source runs
```

### Task 3: `aquant-report verify` CLI

**Files:**
- Modify: `src/aquant/report_cli.py`
- Test: `tests/unit/test_risk_reporting.py`
- Modify: `docs/risk_metrics.md`

- [x] **Step 1: 写 CLI 失败测试**

```python
exit_code = report_cli_main([
    "verify", "--project-root", str(tmp_path),
    "--report-id", report.report_id,
])
assert exit_code == 0
assert json.loads(captured.out)["status"] == "verified"
```

- [x] **Step 2: 运行并确认子命令不存在**

Run: `uv run pytest -q tests/unit/test_risk_reporting.py -k verify_cli`
Expected: FAIL，命令参数无效。

- [x] **Step 3: 最小实现**

新增 `verify` 子命令参数 `--project-root`、`--report-id`、`--backtests`、`--reports`；复用
`_path_beneath`，调用 `verify_published_risk_report`，成功输出 report ID、run 数和
`status="verified"`。

- [x] **Step 4: 全量验证**

Run: `uv run pytest -q && uv run ruff check . && uv lock --check`
Expected: 全部通过。

- [x] **Step 5: 提交**

```text
docs: close week four report verification p2
```
