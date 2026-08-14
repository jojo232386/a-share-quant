# Research Loop v1

Research Loop v1 是单标的、日线、真实快照驱动的最小研究闭环：

```text
Verified Market Snapshot
  -> causal indicator_close
  -> frozen SmaSignal
  -> frozen Planner
  -> frozen rolling portfolio orchestration
  -> daily accounting and metrics
  -> deterministic research report
```

它的目标是判断一个已有投资假设是否值得继续验证，不是证明 Alpha，也不包含实盘、券商、凭据或自动下单能力。

## P0 运行语义

- 输入必须是现有 manifest + SHA-256 加载器验证的未复权行情、公司行动与交易日历。
- Signal 在 T 日收盘后只读取 T 及之前的 `indicator_close`。
- Planner 每个模拟交易日生成有效目标状态；只有当有效目标状态发生变化时，才在下一官方交易日开盘再平衡。
- 成交复用现有 A 股规则：T+1、100 股整手、涨跌停、共享现金、日期有效费用。
- 现金分红在除权日登记应收；为保持 A2 的 T 收盘到 T+1 开盘契约完整，同日到账现金在开盘再平衡后入账，不能资助该次开盘买入。
- 若日历在行情末日后无下一交易日，行情末日作为 T+1 结算缓冲，不进入绩效区间。
- Benchmark 在首个收盘生成一次同权重买入目标，下一交易日开盘成交后持有，不再平衡。

## 命令行

`project-root` 提供标的域配置和输出位置；`data-root` 可指向独立的真实数据快照根目录。所有输入均以精确 ID 选择，不使用“最新文件”隐式规则。

正式运行还必须通过 `--preregistration` 指向仓库内的 JSON 研究预注册文件。该文件必须在运行前已提交，且工作树必须干净；程序会自动绑定完整 Git HEAD、该文件最后修改的 commit 和内容 SHA-256。预注册必须绑定与策略匹配的指标、判断门槛、参数和真实输入身份。原 SMA 路径保留原有预注册口径；A4-1 另行冻结年化收益、Sharpe、最大回撤和毛换手率门槛。

- `hypothesis`；
- 与当次单标的运行一致的 `universe` 和 `evaluation_period`；
- `primary_metrics`：`total_return`、`sharpe_zero_rate`、`max_drawdown`；
- `benchmark`：`buy_and_hold`；
- 与现有 assessment 规则一致的 `pass_criteria` 和 `reject_criteria`；
- 与当次 CLI 实际配置一致的 `strategy_parameters`。

任一内容不匹配、未提交或运行前被修改，都会在产生正式 result artifact 前失败。A4-1 还会核对行情、公司行动、交易日历和 universe 的四个精确 ID。

```bash
uv run --no-sync aquant-experiment research-loop \
  --project-root . \
  --data-root /path/to/a-share-quant-data \
  --universe-id <sha256> \
  --calendar-id <sha256> \
  --snapshot-id <market-snapshot-sha256> \
  --corporate-action-snapshot-id <corporate-action-snapshot-sha256> \
  --preregistration configs/research/<hypothesis>.json \
  --symbol 510300 \
  --sma-period 20 \
  --initial-cash-yuan 1000000.00 \
  --active-weight 0.95
```

A4-1 只增加一个 Signal 实现；Planner、Portfolio、成交、费用和 artifact 路径保持不变。其正式参数固定为 20 个简单 close-to-close 收益、样本标准差 `ddof=1`、252 年化、25% 阈值和 95% ACTIVE 权重。20 个收益需要 21 个有效 `indicator_close`；不足时为 NO_DECISION，波动率等于阈值时为 ACTIVE，高于阈值时为 FLAT。

```bash
uv run --no-sync aquant-experiment research-loop \
  --project-root . \
  --data-root /path/to/a-share-quant-data \
  --universe-id bba6760fa738a829bb09a72f0c90919aeba02429018b8fd189c65e2d6c82a20e \
  --calendar-id fb24e5167d11fee3a58869f8de7910a0ea979d55d3481698bc5baf18cd508983 \
  --snapshot-id 904e594e09d5baad4e70c626129b88bef1a596b755a0731ca234d240b02a8071 \
  --corporate-action-snapshot-id b16ca276bf8d76637c47a1ae68c85a498f87fb17985262bb529338420903e370 \
  --preregistration configs/research/a4_1_510300_volatility_regime_defense.json \
  --symbol 510300 \
  --strategy volatility_regime_defense \
  --lookback-returns 20 \
  --annualization 252 \
  --volatility-threshold 0.25 \
  --initial-cash-yuan 1000000.00 \
  --active-weight 0.95
```

## 输出

`outputs/research_loop/<run_id>/` 包含：

- `run.json`：Git HEAD、预注册 commit/内容 SHA-256、真实输入身份、参数、执行口径和行数；
- `metrics.json`：策略、benchmark 及差异；
- `equity.csv`：每日净值与回撤；
- `targets.csv`：每日 Signal 三态与 Planner 有效目标；
- `attempts.csv` / `trades.csv`：尝试、拒单与实际成交；
- `dividends.csv`：分红权益与现金日期；
- `report.md`：人可读研究结论；
- `artifact_manifest.json`：所有输出文件的 SHA-256 清单。

指标固定使用 252 个年化交易日、无风险利率 0；换手率为成交名义金额绝对值之和除以平均每日权益。

## 真实数据 P0 证据

2026-08-14 使用真实 510300 快照运行：

- 行情快照：`904e594e09d5baad4e70c626129b88bef1a596b755a0731ca234d240b02a8071`；
- 公司行动快照：`b16ca276bf8d76637c47a1ae68c85a498f87fb17985262bb529338420903e370`；
- 交易日历：`fb24e5167d11fee3a58869f8de7910a0ea979d55d3481698bc5baf18cd508983`；
- 绩效区间：2018-01-02 至 2026-07-23，2,075 个交易日；
- run ID：`e81d80da3b0de44fd1777b2c6eca558294e0907988f8e5f5982f60f71317cea8`。

SMA(20) 在费后总收益 -10.83%、年化 -1.38%、最大回撤 32.76%、Sharpe -0.045；买入持有 benchmark 总收益 30.03%、年化 3.24%、最大回撤 38.49%、Sharpe 0.269。该假设未达到“继续验证”预设门槛。

这只是单标的全样本初筛。在任何更强研究结论前，仍需完成样本外测试、参数敏感性、成本/滑点压力和数据时点有效性检查。

上述 SMA(20) 结果产生于本次预注册约束之前；本次加固不重跑、不调参，也不改变其 `insufficient_preliminary_evidence` 结论。

## 明确延后

- 多标的策略研究与跨标的 benchmark；
- 参数扫描、样本外切分和滚动窗口；
- 滑点、冲击成本和容量压力；
- 实盘、Broker adapter、凭据和自动交易循环。
