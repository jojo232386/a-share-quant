# Week 5 Restricted Experiment and Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在冻结规则下完成 SMA 单参数敏感性选择、保留期单次评估和10个交易日操作流程回放。

**Architecture:** 继续使用现有正式回测包，不改成交引擎和费用口径。实验层只读取已通过
SHA-256与会计校验的 run bundle，从训练期指标中按固定规则选择一个周期；随后只消费选中周期
和两个基准的保留期流水。回放层从已审计订单、成交和行情日历生成逐日流程证据，不把历史回放
伪装成实时交易或盈利验证。

**Tech Stack:** Python 3.11、现有 Backtrader run bundle、pytest、SHA-256、JSON/Markdown。

---

### Task 1: 读取并切分已验证运行包

**Files:**
- Create: `src/aquant/research/__init__.py`
- Create: `src/aquant/research/week5.py`
- Test: `tests/unit/test_week5_experiment.py`

- [ ] **Step 1: 写失败测试**

```python
def test_split_run_uses_training_only_for_selection(tmp_path):
    series = make_verified_series(tmp_path)
    training = split_series(series, train_end=date(2023, 12, 29), holdout_start=date(2024, 1, 2))
    assert training.training[-1].date == date(2023, 12, 29)
    assert training.holdout[0].date == date(2024, 1, 2)
```

- [ ] **Step 2: 运行并确认函数不存在**

Run: `uv run pytest -q tests/unit/test_week5_experiment.py -k split`
Expected: FAIL，`split_series` 尚不存在。

- [ ] **Step 3: 最小实现**

`load_verified_run_series` 先调用 `load_audited_run_metrics` 完成包验真，再读取已验真的
`equity.csv`、`positions.csv`、`fills.csv`、`missing_sessions.json`；`split_series` 要求
训练结束日早于保留期开始日，并拒绝空区间、日期倒序和跨越之外的记录。

- [ ] **Step 4: 运行目标测试**

Run: `uv run pytest -q tests/unit/test_week5_experiment.py -k split`
Expected: PASS。

- [ ] **Step 5: 提交**

```text
feat: add verified week five run series
```

### Task 2: 训练期选型与保留期单次评估

**Files:**
- Modify: `src/aquant/research/week5.py`
- Test: `tests/unit/test_week5_experiment.py`

- [ ] **Step 1: 写失败测试**

```python
def test_selection_score_does_not_read_holdout(tmp_path):
    candidates = make_candidate_series(tmp_path, periods=(10, 20, 60))
    selected = select_training_period(candidates, train_end=date(2023, 12, 29), holdout_start=date(2024, 1, 2))
    assert selected.period in {10, 20, 60}
    assert selected.selection_basis == "training_calmar_then_return_then_smaller_period"
```

- [ ] **Step 2: 运行确认选择器不存在**

Run: `uv run pytest -q tests/unit/test_week5_experiment.py -k selection`
Expected: FAIL。

- [ ] **Step 3: 最小实现**

对每个周期只计算训练区间 `RiskMetrics`，按 `(Calmar, total_return, -period)` 降序选择；
Calmar 无定义时按负无穷处理。保留期只对选中周期、Buy & Hold、冻结 SMA(20) 计算并保存，
不读取未选周期的保留期指标。报告必须保存全部候选训练结果、选中周期和三个保留期结果。

- [ ] **Step 4: 运行目标测试**

Run: `uv run pytest -q tests/unit/test_week5_experiment.py -k selection`
Expected: PASS。

- [ ] **Step 5: 提交**

```text
feat: select one sma period on training data
```

### Task 3: 10 日操作流程回放与原子报告

**Files:**
- Modify: `src/aquant/research/week5.py`
- Create: `src/aquant/experiment_cli.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/test_week5_experiment.py`
- Modify: `docs/backtest_baselines.md`

- [ ] **Step 1: 写失败测试**

```python
def test_replay_has_ten_calendar_rows_and_machine_readable_orders(tmp_path):
    replay_source = make_replay_source(tmp_path)
    result = build_week5_replay(replay_source, replay_days=10)
    assert len(result.rows) == 10
    assert {"date", "data_available", "orders", "fills"} <= result.rows[0]
```

- [ ] **Step 2: 运行确认回放器不存在**

Run: `uv run pytest -q tests/unit/test_week5_experiment.py -k replay`
Expected: FAIL。

- [ ] **Step 3: 最小实现**

回放从保留期起始交易日开始，按已验证交易日历取10个官方交易日；每行记录数据是否存在、
信号对应订单、拒单原因和成交。输出 `experiment.json`、`replay.json`、`report.md` 和
`artifact_manifest.json`，以规范 JSON 身份生成内容地址 ID，并以临时目录加 `os.replace` 原子发布。

- [ ] **Step 4: 增加 CLI**

新增 `aquant-experiment run`，参数固定包含项目根目录、universe ID、候选包目录、基准包目录、
输出目录、训练结束日、保留期开始日、候选周期和回放天数；成功 stdout 只输出 JSON，失败 stderr
只输出脱敏 JSON。

- [ ] **Step 5: 运行全量检查**

Run: `uv run pytest -q && uv run ruff check . && uv lock --check`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```text
feat: deliver frozen week five experiment and replay
```
