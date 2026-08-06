# 基准回测说明

## 当前能做什么

当前支持单标的、日线、研究与模拟。正式回测必须同时消费四类已验证输入：

1. 行情 manifest 和原始不复权 snapshot；
2. 独立公司行为 manifest 和 snapshot；
3. 独立交易日历 snapshot。
4. 内容寻址的 VerifiedUniverse。

四个对象都会在消费前重新验真，并与费用政策、参数、Backtrader 版本和执行源码指纹一起绑定到
`run_id`。裸 DataFrame、自填哈希、未验证公司行为对象、非 universe 成员和不完整覆盖范围均
不能进入正式入口。

当前有两个基准：

- `buy_and_hold`：第一根日线收盘后发出目标仓位买入意图；
- `sma`：因果连续指标价高于 SMA 时持有，低于时空仓，唯一策略参数是 `sma_period`。

两者使用 `target_weight`，默认 `0.95`。实际数量在下一官方交易日开盘时按当时权益、开盘价、100 股/份整手和全部费用计算。不同股价不再固定买 100 股；订单流水会同时保存目标比例、开盘权益、实际数量和实际暴露比例。

## 时间和公司行为顺序

每个有日线的交易日按以下顺序处理：

1. 开盘前，用当时已有持仓登记当日除息事件的应收红利；
2. 将当日到期应收红利转入现金；
3. 尝试执行上一交易日收盘后的订单；
4. 收盘后用 `indicator_close` 计算信号；
5. 用原始收盘价记录持仓市值，并记录现金、应收和净值。

因此，除息日开盘卖出仍保留已登记权利，当日开盘买入不获得本次红利。`cash + position market value + receivable = equity` 每日强制校验。

v0.1 对现金和应收红利都不计利息，也不假设存在无风险收益；这与未建模个人红利税一样属于显式研究边界。

系统没有启用 `cheat_on_open` 或 `cheat_on_close`。目标交易日无 K 线时按 `suspended_no_bar` 拒单，不顺延到复牌日；开盘位于涨停/跌停限制价时保守拒单。

## 如何运行

公司行为下载和回测严格分开。下载一个公司行为快照：

```bash
uv run aquant-data corporate-actions \
  --project-root . \
  --config configs/data.yaml \
  --symbol 600519
```

随后从两个 manifest 复制行情与公司行为的完整 snapshot ID：

```bash
uv run aquant-backtest run \
  --project-root . \
  --symbol 600519 \
  --snapshot-id <行情 snapshot_id> \
  --corporate-action-snapshot-id <公司行为 snapshot_id> \
  --calendar-id <交易日历 ID> \
  --universe-id <股票池 ID> \
  --strategy buy_and_hold \
  --initial-cash 1000000 \
  --target-weight 0.95 \
  --stock-commission-rate 0.00025 \
  --stock-minimum-commission 5.00 \
  --etf-commission-rate 0.00025 \
  --etf-minimum-commission 5.00
```

SMA 只需改为 `--strategy sma --sma-period 20`。回测命令不下载网络数据。

20 个同 universe、同实现指纹的正式包完成后，可生成确定性风险报告：

```bash
uv run aquant-report build \
  --project-root . \
  --universe-id <股票池 ID> \
  --max-drawdown-limit 0.50 \
  --max-exposure-limit 1.00
```

报告会重新验证每包 SHA-256 和逐日会计恒等式，不读取预填收益指标。指标定义见
`docs/risk_metrics.md`。

生成后可以从报告反向核对当前全部源包：

```bash
uv run aquant-report verify \
  --project-root . \
  --report-id <报告 ID>
```

验证命令不修改回测包或报告。

## 第 5 周受限实验

候选周期只允许 `10,20,60`，先使用训练期（截至 `2023-12-29`）按
`training_calmar_then_return_then_smaller_period` 选择一个周期，再只读取选中周期、Buy & Hold
和冻结 `SMA(20)` 的保留期流水。实验命令不下载数据，也不修改候选包：

```bash
uv run aquant-experiment run \
  --project-root . \
  --universe-id <股票池 ID> \
  --calendar-id <交易日历 ID> \
  --candidate-root outputs/experiments/week5/candidates \
  --baseline-root outputs/backtests \
  --output outputs/experiments/week5 \
  --train-end 2023-12-29 \
  --holdout-start 2024-01-02 \
  --periods 10,20,60 \
  --replay-days 10
```

候选包必须先通过现有 `aquant-backtest run --strategy sma --sma-period <周期>` 生成并保存在
`candidate-root`；缺少标的、周期重复、来源 universe 不一致或路径越界时，实验整批拒绝。输出
包包含 `experiment.json`、`replay.json`、`report.md` 和 `artifact_manifest.json`。

10 日回放逐标的记录官方交易日、是否有行情 bar、订单状态、拒单原因和成交；它只验证流程链路，
不验证长期收益，也不构成实盘成交证据。

## 审计包

每次运行原子写入 `outputs/backtests/<run_id>/`：

| 文件 | 内容 |
| --- | --- |
| `run.json` | 四类输入身份、参数、价格流、税口径、引擎和实现指纹 |
| `orders.csv` | 信号日、目标比例、开盘权益、实际数量、状态和拒单原因 |
| `fills.csv` | 成交日期、价格、数量、金额和逐项费用 |
| `positions.csv` | 原始收盘价口径的每日持仓 |
| `cash.csv` | 每日现金 |
| `receivables.csv` | 每日应收红利 |
| `equity.csv` | 每日净值 |
| `lots.csv` | T+1 持仓批次 |
| `corporate_actions.csv` | 红利登记与付款流水 |
| `missing_sessions.json` | 官方交易日缺失记录 |
| `artifact_manifest.json` | 全部载荷文件 SHA-256 |

相同输入、参数和实现得到同一 `run_id`。整包不完整或已有内容冲突时关闭运行，不静默补写或覆盖。

## 边界

当前结果只验证研究工程链路，不证明真实可成交或策略有效。尚不支持 ST、新股特殊阶段、创业板、科创板、北交所、非现金公司行为、个人红利税、盘中排队、成交量约束、部分成交、滑点、多标的共享现金组合、券商连接和自动交易。
