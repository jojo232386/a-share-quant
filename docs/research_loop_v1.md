# Research Loop v1

Research Loop v1 是日线、真实快照驱动的最小研究闭环；原路径保持单标的，A4-3
仅增加冻结的 510300 / 510500 双标的 research-layer 路径：

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
- 单标的 Benchmark 在首个收盘生成一次买入目标，下一交易日开盘成交后持有。
- A4-3 Benchmark 在 `2019-01-31` 生成 510300=47.5%、510500=47.5%、现金=5%
  的唯一初始目标，`2019-02-01` 开盘执行；之后不主动再平衡，真实权重只因整手、费用、
  价格、分红与成交约束自然漂移。

## 命令行

`project-root` 提供标的域配置和输出位置；`data-root` 可指向独立的真实数据快照根目录。所有输入均以精确 ID 选择，不使用“最新文件”隐式规则。

正式运行通过 `scripts/formal_research_runtime.sh run --` 启动。该脚本从干净 Git
树构建并校验 wheel，在工作区外按 `uv.lock` 安装依赖，再非 editable 安装该 wheel，
最后调用 wheel 提供的官方 `aquant-experiment` 入口。开发环境仍可使用 editable 安装，
但不得作为 formal research runtime。

2026-08-14 的启动故障已确认到直接机制：当时工作区 `.venv` 内的安装文件带有
macOS `UF_HIDDEN` 文件标志，CPython 3.11.15 的 `site.py` 因该标志跳过 `.pth`。
下划线开头的文件名不是跳过条件；在工作区外新建的同版本 uv editable 环境没有该标志
并可正常导入。现有证据不能确认是谁在原环境创建后追加了该标志，因此 formal runtime
不修改或依赖 `.pth`，而是使用隔离的非 editable wheel 安装。

正式运行还必须通过 `--preregistration` 指向仓库内的 JSON 研究预注册文件。该文件必须在运行前已提交，且工作树必须干净；程序会自动绑定完整 Git HEAD、该文件最后修改的 commit 和内容 SHA-256。预注册必须绑定与策略匹配的指标、判断门槛、参数和真实输入身份。原 SMA 路径保留原有预注册口径；A4-1、A4-2 与 A4-3 分别冻结自己的策略语义，并共用已预先固定的年化收益、Sharpe、最大回撤和毛换手率门槛。A4-3 还逐标的绑定两组行情和公司行动快照，并冻结首个信号/执行日、静态 benchmark、换手单位及只运行一次的控制字段。

- `hypothesis`；
- 与当次单标的运行一致的 `universe` 和 `evaluation_period`；
- `primary_metrics`：`total_return`、`sharpe_zero_rate`、`max_drawdown`；
- `benchmark`：`buy_and_hold`；
- 与现有 assessment 规则一致的 `pass_criteria` 和 `reject_criteria`；
- 与当次 CLI 实际配置一致的 `strategy_parameters`。

任一内容不匹配、未提交或运行前被修改，都会在产生正式 result artifact 前失败。A4-1
与 A4-2 会核对行情、公司行动、交易日历和 universe 的四个精确 ID；A4-3 会核对两只
ETF 的全部六个输入 ID（两组行情、两组公司行动、日历和 universe）。

```bash
./scripts/formal_research_runtime.sh run -- research-loop \
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
./scripts/formal_research_runtime.sh run -- research-loop \
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

A4-2 同样只增加一个 research-layer Signal 实现。其参数固定为 252 个交易期的绝对时间序列动量、零阈值和 95% ACTIVE 权重：`indicator_close[t] / indicator_close[t-252] - 1` 严格大于零时 ACTIVE，小于或等于零时 FLAT。252 个交易期需要 253 个因果可用的 `indicator_close`；不足时为 NO_DECISION。Signal 在 T 日收盘后决定目标，仍由既有 Planner 在下一交易日开盘执行。

```bash
./scripts/formal_research_runtime.sh run -- research-loop \
  --project-root . \
  --data-root /path/to/a-share-quant-data \
  --universe-id bba6760fa738a829bb09a72f0c90919aeba02429018b8fd189c65e2d6c82a20e \
  --calendar-id fb24e5167d11fee3a58869f8de7910a0ea979d55d3481698bc5baf18cd508983 \
  --snapshot-id 904e594e09d5baad4e70c626129b88bef1a596b755a0731ca234d240b02a8071 \
  --corporate-action-snapshot-id b16ca276bf8d76637c47a1ae68c85a498f87fb17985262bb529338420903e370 \
  --preregistration configs/research/a4_2_510300_absolute_momentum_252.json \
  --symbol 510300 \
  --strategy absolute_momentum_252 \
  --lookback-sessions 252 \
  --initial-cash-yuan 1000000.00 \
  --active-weight 0.95
```

A4-3 是冻结的最小双标的 research-layer 扩展，不进入 `SIGNAL_REGISTRY`，也不改变
Planner、Portfolio、accounting、费用或成交规则。只在每个自然月最后一个官方交易日计算
510300 与 510500 的 prior 2-12 相对动量：在月 `t-1` 月末使用
`indicator_close(end t-2) / indicator_close(end t-13) - 1`。较高者目标 95%，另一只显式为
0，现金目标 5%；并列时按代码升序由 510300 获胜，两者均为负时仍持有相对领先者。
非月末为 `NO_NEW_DECISION`，任一标的排名端点缺失或无效时整组 `NO_DECISION`，不得缩小
universe。verified calendar 与两组快照机械固定首个信号日为 `2019-01-31`，次一官方交易日
`2019-02-01` 执行。

```bash
./scripts/formal_research_runtime.sh run -- research-loop \
  --project-root . \
  --data-root /path/to/a-share-quant-data \
  --universe-id bba6760fa738a829bb09a72f0c90919aeba02429018b8fd189c65e2d6c82a20e \
  --calendar-id fb24e5167d11fee3a58869f8de7910a0ea979d55d3481698bc5baf18cd508983 \
  --snapshot-id 904e594e09d5baad4e70c626129b88bef1a596b755a0731ca234d240b02a8071 \
  --corporate-action-snapshot-id b16ca276bf8d76637c47a1ae68c85a498f87fb17985262bb529338420903e370 \
  --secondary-snapshot-id fbcdeb600549cf8b8dc70b213a0afc9ed1af5b20f84612a67ca7f995c9a9fff2 \
  --secondary-corporate-action-snapshot-id da6d7b9bf0badb316dd1dd90323af74e1906416dcdc7d557dee42bb3d2d197f3 \
  --preregistration configs/research/a4_3_510300_510500_monthly_relative_momentum_2_12.json \
  --symbol 510300 \
  --secondary-symbol 510500 \
  --strategy monthly_relative_momentum_2_12 \
  --lookback-start-month 2 \
  --lookback-end-month 12 \
  --initial-cash-yuan 1000000.00 \
  --active-weight 0.95
```

只验证正式 wheel、非 editable 安装和官方 CLI 启动，不进入数据、策略或指标阶段：

```bash
./tests/scripts/test_formal_research_runtime.sh
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

指标固定使用 252 个年化交易日、无风险利率 0；换手率为成交名义金额绝对值之和除以平均
每日权益。正式门槛 `strategy_gross_turnover <= 100.0` 中的 `100.0` 是 raw ratio，等价
10,000%；`annualized_gross_turnover` 仅为 secondary diagnostic，不参与 PASS/REJECT。

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

## A4-1 formal research closeout

2026-08-14 在所有工程验收通过后，使用锁定的非 editable wheel 完成了唯一一次
fresh formal run。正式运行绑定 Git HEAD
`88220efd9f35ef5a0552b5949919072fb4cf1585`、tree
`9ea88056652418d654315cf2ea6927ee2e22bdfe` 与 wheel SHA-256
`4dd25d8ccf8ac5828120be049e9906df560fdeb72974b2ba3e2b605780216fb7`。预注册绑定
commit `0596352933b617adbb12df881f195fd59264c1a9` 与内容 SHA-256
`89ec8ede2c4b0cbb1cdb505cfce396601bbb41d596d12e824288c6fac86ad6da`。

- run ID：`a34ff2f0420b0e405089f19bb41a137c977ef1af0366902f45656223d600afc0`；
- artifact manifest SHA-256：`600dadfd82c1972afe326761fadd469a6996896ffd98bf9a46ae834dc98ed3f8`；
- `run.json` SHA-256：`efdea7088db7e2e6360670fe114ffd927089ce159499664d45e3a82210bd893e`；
- `metrics.json` SHA-256：`c94df9b3492138ccb01c59e4099d8bcd5c04fceaa6babe3eece0039d4f96e37d`；
- 策略：总收益 11.87%，年化收益 1.37%，最大回撤 40.42%，Sharpe 0.166，毛换手率
  4,124.57%，46 笔成交；
- benchmark：总收益 30.03%，年化收益 3.24%，最大回撤 38.49%，Sharpe 0.269；
- 预注册判定：`REJECT`。年化收益、Sharpe 和最大回撤门槛失败，毛换手率门槛通过。

该 hypothesis 在 A4-1 止步；不改参、不重跑、不启动 A4-2。该结论仅是本次预注册的单标的
全样本研究判定，不证明实盘可行性或收益保证。

## A4-2 formal research closeout

2026-08-14 在刷新后的 `origin/main`
`dde769bde9f46210b8d75a1bfa39bf918eb828aa` 上独立完成 A4-2。预注册先以 commit
`abeb48db12637d4eec1d2058ebfaaf9658b7938a` 冻结，内容 SHA-256 为
`9863a433df141f08f8839fb818cc5da31b5d4d524efb01d708b4836844195b42`。所有工程验收通过后，
唯一一次 fresh formal run 使用锁定的非 editable wheel，绑定 Git HEAD
`56eff0ac5aee6d2db644150d6ec651e092b0a1ac`、tree
`53a76b2b07ece07c62946cfb3b1bfb790409e973` 与 wheel SHA-256
`f5fca956ac79ec63f59c91aab9256c3856f3a0f6821c088b7f9f8101e4fb7b3e`。

- run ID：`4dbc0db47cdb3d1aa8ec399f0265c7abc31dd433137f4c0a3f610384ce18d532`；
- artifact manifest SHA-256：`1d0f41009fb33aa45f777b6b887813c067534cc34691f98d53365a44aa74385d`；
- `run.json` SHA-256：`4f36da149c29f80c3b64413434ebec4b808e1f420add71b5b0f7f49c5f987579`；
- `metrics.json` SHA-256：`e1ee08ed629da5426f8f7c2cc5f6a2ac4ac2792b32c10b3d0715b30bacb33b2f`；
- 策略：总收益 31.79%，年化收益 3.41%，最大回撤 23.59%，Sharpe 0.319，毛换手率
  3,058.77%，35 笔成交；
- benchmark：总收益 30.03%，年化收益 3.24%，最大回撤 38.49%，Sharpe 0.269；
- 预注册判定：`REJECT`。年化收益、最大回撤和毛换手率门槛通过；Sharpe 相对 benchmark
  仅提高 0.050，未达到预注册要求的 0.10，因此唯一失败项为 `sharpe_zero_rate`；
- artifact manifest 的 8 个文件已逐一重算 SHA-256；信号状态为 1,071 个 ACTIVE、752 个
  FLAT、252 个 NO_DECISION，符合 253 个收盘才产生首个决策的冻结语义。

该 hypothesis 在 A4-2 止步。`FORMAL_RUN_COUNT=1`、`PARAMETER_RESCUE=FALSE`、
`FORMAL_RERUN=FALSE`；Planner、Portfolio、accounting 与成交语义均未改变，且未启动 A4-3。
该结论仅是本次预注册的单标的全样本研究判定，不证明实盘可行性或收益保证。

## A4-3 formal research closeout

2026-08-14 在精确 `origin/main`
`e65fdaa071ca3febfe853e61036ea036ed52c50d` 上独立完成 A4-3。只读 preflight 使用 verified
calendar 与两只 ETF 的快照机械确认首个信号日 `2019-01-31`、首个执行日
`2019-02-01`。预注册先以 commit `a72461ef80dda2d5e06e396576cb2c7410340371`
冻结，内容 SHA-256 为
`95515c87ee2cedbbf52545e4798b5c8b7bcc5b8af13af5d769cdd917d37c6c1b`。

所有工程验收通过后，唯一一次 fresh formal run 使用锁定的非 editable wheel，绑定 Git HEAD
`7cbc490cc88147d829566346b4a492bd5a3f731e`、tree
`f6e71a77770ff0c2fe1574904b23124245695296` 与 wheel SHA-256
`18e43529fc7d8e800b9a18bcaac2afa555f869b9a77ab1482fd0214bade9e613`。

- run ID：`0b072ba64e424c28b59f05b0a70c478c0de38c350d64e2adc5d75f4ec6bfb0cf`；
- artifact manifest SHA-256：`346b9c1360660325a30c450c33c7940691c55e4e764c744a92b8115ca3122f32`；
- `run.json` SHA-256：`c99faaa336fe3a18c8913a9b9fabf8ffdcee17ab5f99c67cc42fbfc5efb3499b`；
- `metrics.json` SHA-256：`88432dea4d6ad8e23d27a0fd369b7fe9d3f1aec8fb78f46f1db587173fbdc5eb`；
- 策略：总收益 37.97%，年化收益 4.58%，最大回撤 52.85%，Sharpe 0.320，毛换手率
  raw ratio `16.513773`（1,651.38%），年化毛换手率 secondary diagnostic `2.299155`，
  19 笔成交；
- static benchmark：总收益 69.59%，年化收益 7.63%，最大回撤 38.94%，Sharpe 0.483，
  仅 `2019-02-01` 两笔初始买入，之后没有主动再平衡；
- 预注册判定：`REJECT`。年化收益低于 benchmark 70% 门槛，Sharpe 未达到 benchmark +
  0.10，最大回撤高于 benchmark 的 80%；raw turnover `16.513773 <= 100.0` 单项通过；
- 绩效区间共有 1,811 个官方交易日、零缺失行情；post-run 只读审计重算 manifest 内 8 个
  artifact 哈希，确认 90 个且仅月末的有效排名日、所有非月末目标 carry-forward、共享现金
  rotation 的 SELL-before-BUY 顺序，以及 benchmark 只存在两笔初始成交。

该 hypothesis 在 A4-3 止步。`FORMAL_RUN_COUNT=1`、`PARAMETER_RESCUE=FALSE`、
`FORMAL_RERUN=FALSE`；研究语义、Planner、Portfolio、accounting 与成交语义在 formal run 后
均未改变，且 `A4_4_STARTED=FALSE`。该结论仅是冻结双 ETF 全样本研究判定，不证明实盘
可行性或收益保证。

## 明确延后

- 多标的策略研究与跨标的 benchmark；
- 参数扫描、样本外切分和滚动窗口；
- 滑点、冲击成本和容量压力；
- 实盘、Broker adapter、凭据和自动交易循环。
