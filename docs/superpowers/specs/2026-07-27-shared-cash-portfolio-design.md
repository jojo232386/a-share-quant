# A 股共享现金多标的组合 v0.2 设计

日期：2026-07-27
状态：历史 Claude Code（DeepSeek API）摘要设计复核已完成；Gate A/B 已实现
当前审查策略（2026-07-28）：Codex 实现与自验，Work Buddy 负责独立只读复核；不再调用
Claude Code

## 1. 目标与决策

v0.2 在已经冻结的 `v0.1-research` 之上增加一个真正的共享现金组合研究引擎。它回答的是：

> 10 个标的使用同一个账户时，现金、持仓、费用、公司行为和组合净值能否在 A 股日线约束下
> 被确定、复现和逐日审计？

已批准的架构决策是：新增独立的组合协调器，复用现有验真数据、交易日历、公司行为、费用和
纯规则模块，不把现有单标的 `RuleAwareBackBroker` 直接改造成多标的 broker。

这样做的原因是：

- `v0.1-research` 已经发布，现有单标的结果必须保持可复现；
- `aquant.rules.evaluate_order()` 已经是 symbol-aware 的纯规则入口，可以被组合层复用；
- 共享现金、跨标的顺序和组合账本与单个 Backtrader feed 的职责不同；
- 单标的 Backtrader 运行继续作为时序参考和等价性校验，不被废弃。

v0.2 的正式验收仍使用固定 `pilot-10` universe。公共接口按任意已验证 universe 设计，避免未来
扩展到 30 个标的时推倒重来，但本轮不增加证券种类或实际股票数量。

## 2. 研究边界与非目标

### 2.1 本轮支持

- 固定 8 只主板普通股票和 2 只境内股票型宽基 ETF；
- 一个共享现金账户；
- `buy_and_hold` 唯一策略；
- 初始总目标仓位 `0.95`，每个标的固定目标名义金额为总目标名义金额的 `1 / N`；
- 收盘后生成目标，下一官方交易日开盘尝试成交；
- 每个未完成目标最多尝试 5 个官方交易日；
- T+1、100 股/份整手、日期生效费用、保守涨跌停拒单；
- 现金红利的应收和到账会计；
- 组合级现金、持仓、市值、应收、权益、暴露和基础风险指标；
- 离线、确定性、内容寻址的审计包。

### 2.2 本轮明确不做

- 不增加 SMA、轮动、再平衡、择时或优化器；
- 不把拒单标的预算重新分配给其他标的；
- 不支持创业板、科创板、北交所、ST、新股特殊阶段和退市整理期；
- 不支持送转、拆并股、配股和证券变更；
- 不实现部分成交、滑点、盘口排队、成交量限制和市场冲击；
- 不连接券商，不发送订单，不使用真实资金；
- 不引入 Qlib、多 Agent 编排或新的行情框架；
- 不把组合回测、测试通过或审计包完整解释成策略有效、可实盘成交或能够盈利。

## 3. 模块边界

新增包：

```text
src/aquant/portfolio/
├── __init__.py
├── models.py       # 不可变输入、配置和结果契约
├── accounting.py   # 共享现金、分标的批次、应收和逐笔对账
├── coordinator.py  # 官方交易日循环、目标和成交尝试
├── identity.py     # 输入闭包、实现指纹和确定性 run ID
├── metrics.py      # 组合基础风险和暴露摘要
└── export.py       # 原子、幂等、内容寻址的组合审计包
```

新增 CLI：

```text
src/aquant/portfolio_cli.py
```

现有模块职责保持不变：

- `aquant.backtest`：单标的 Backtrader 参考实现；
- `aquant.backtest.data_access`：只从 manifest-pinned 快照产生
  `VerifiedMarketData`；
- `aquant.data.calendar_snapshot`：全市场共享的独立交易日历证据；
- `aquant.data.corporate_actions`：每个标的独立、验真的公司行为证据；
- `aquant.rules`：T+1、整手、涨跌停、费用和成交前门禁；
- `aquant.universe`：允许的 symbol 与 instrument kind 组合。

组合模块不得复制费率表、价格限制公式、T+1 判断或公司行为解析逻辑。可以直接复用且不会改变
`v0.1-research` 实现指纹的现有纯函数继续复用；组合新增的状态转换放在 `aquant.portfolio`
内部。不得为了组合功能改动现有 Backtrader broker，或让冻结版单标的路径导入新增组合模块。

这是有意的版本隔离：v0.1 的 run ID 绑定现有回测、日历、公司行为和规则源码。若为了“共用”
新函数而修改这些文件，即使正式十标的结果数值不变，实现指纹和预期 run ID 也会变化，当前
开发分支便无法继续用冻结清单逐字节重放 v0.1。共享抽象只有在未来新建版本化单标的引擎时才
考虑，不回写到 `v0.1-research` 路径。

## 4. 不可变输入契约

### 4.1 `PortfolioConfig`

配置使用精确类型，不接受可隐式转换的字符串、浮点权重或布尔整数：

```text
strategy = buy_and_hold
initial_cash_fen: positive int
gross_target_weight: exact Decimal in (0, 1]
signal_date: exact official session
end_date: exact official session, later than signal_date
max_entry_attempts: int in [1, 20]，正式 v0.2 固定为 5
```

正式验收固定：

```text
initial_cash_fen = 100_000_000    # 1,000,000 元研究账户
gross_target_weight = Decimal("0.95")
max_entry_attempts = 5
strategy = buy_and_hold
```

初始资金只是工程参数，不代表建议资金规模。实际仓位会受整手、价格和费用影响，不能假定一定
达到 95%。

### 4.2 `PortfolioInstrumentInput`

每个成员必须同时提供：

- 一个精确的 `VerifiedMarketData`；
- 一个精确的 `VerifiedCorporateActions`；
- 与 universe 完全一致的 symbol 和 instrument kind；
- 覆盖 `signal_date` 至 `end_date` 的公司行为证据；
- 覆盖信号日、至少一个目标执行日和完整估值区间的行情证据。

交易日历必须至少覆盖到 `end_date` 的下一官方 session。这个额外 session 只用于证明最后一个
买入批次的 T+1 可卖日期和规则覆盖，不进入配置范围外的收益或权益计算。

公共运行入口只接受 `tuple[PortfolioInstrumentInput, ...]`。入口验证后按 symbol 排序，并拒绝：

- 裸 DataFrame、裸 dict、子类或伪造 provenance；
- 重复 symbol、遗漏成员或 universe 之外的成员；
- 行情与公司行为 symbol/kind 不一致；
- 不复权原始执行价之外的行情；
- 修改后与 canonical digest 不一致的对象；
- 日期覆盖不足、日历未验真或费率策略未验真；
- 任何未支持证券或未支持公司行为。

传入 tuple 的排列不得影响运行身份和结果。

### 4.3 离线边界

`run_portfolio_backtest()` 只接收已经加载和验真的对象，不允许访问网络。正式 CLI 只解析项目
目录下内容寻址的 manifest、universe、日历、行情和公司行为文件。

下载或更新 AKShare 数据仍是独立命令，不能从组合运行路径隐式触发。正式验收直接使用
`release/v0.1-research/inputs/` 的冻结输入闭包，并在禁止出站网络的条件下重算。

## 5. 共享预算和订单语义

### 5.1 固定目标名义金额与现金储备

设初始现金为 `C`，总目标权重为 `W`，universe 有 `N` 个成员：

```text
gross_target_notional_fen = floor(C * W)
per_symbol_target_notional_fen = floor(gross_target_notional_fen / N)
planned_cash_reserve_fen = C - gross_target_notional_fen
allocation_rounding_remainder_fen
    = gross_target_notional_fen - per_symbol_target_notional_fen * N
```

正式 10 标的配置因此得到 `gross_target_notional_fen = 95_000_000`、
`per_symbol_target_notional_fen = 9_500_000`，即每个标的固定 95,000 元目标成交名义金额；
`planned_cash_reserve_fen = 5_000_000`，即计划保留 50,000 元现金用于费用和未投资缓冲；
该配置的分配舍入余数为 0。

目标权重表示持仓市值占初始权益的比例，不把费用伪装成持仓。每个标的的目标名义金额从信号日
开始固定，不随其他标的涨跌、拒单或延迟成交而改变。买入数量先按目标执行日真实开盘价和
100 股/份整手向下取整，并满足：

```text
notional_fen <= per_symbol_target_notional_fen
```

随后用共享现金支付成交额和全部买入费用：

```text
notional_fen + commission_fen + transfer_fee_fen <= shared_cash_before_fen
```

费用从计划现金储备和其他未投资现金中支付，不减少已经由目标权重确定的名义金额上限。若共享
现金确实不足，按现有 v0.1 规则每次减少一整手直至可支付；正式 0.95 权重配置若触发该分支，
必须在审计包中记录原因。目标名义金额不足一手时记录明确拒单，不向其他标的借用目标金额。

该语义与 v0.1 的目标权重一致。例如单标的账户 10,000 元、目标权重 0.95、ETF 开盘 95 元时，
目标名义金额为 9,500 元，应买入 100 份并另付 5 元最低佣金，而不是因总支出 9,505 元超过
9,500 元目标名义金额而拒单。

### 5.2 确定顺序

同一交易日的尝试按六位 symbol 升序处理。固定目标名义金额使这个顺序只决定审计日志顺序，
不决定哪个标的可以抢走其他标的目标额度。

组合层必须先按 symbol 升序构造全部候选数量，再按同一顺序执行共享现金检查。若共享现金不足，
只允许按现有 v0.1 规则从当前标的候选数量逐手减少，不得占用其他标的目标名义金额，不得使现金
变负，也不得回头重算已经成交的标的。

### 5.3 有限重试

信号日只创建一次根目标 `target_id`。第一次尝试发生在下一官方交易日。若因为无 bar、开盘位于
涨跌停价、目标名义金额不足一手或其他可审计的市场约束未成交，组合层可以在后续官方交易日生成新的
`attempt_id`，但必须：

- 每个 attempt 关联同一个根 `target_id` 和原始 `signal_date`；
- 每个 attempt 使用前一官方交易日作为本次订单意图日期，使现有规则引擎只检查下一交易日；
- 不读取未来开盘来决定是否生成尝试；
- 固定使用原始 `per_symbol_target_notional_fen`；
- 每个 attempt 一经创建就消耗一次计数；无 bar、涨跌停和目标名义金额不足一手都不豁免；
- 最多创建 5 次，之后状态变为 `expired_unfilled`；
- 不把未成交目标名义金额分配给其他标的；
- 不在同一 symbol、同一交易日产生两个有效 attempt。

`buy_and_hold` 在首次完整成交后不再产生订单。本轮没有卖出策略；买入批次仍记录 T+1 可卖日，
为账本正确性和以后扩展保留证据。

## 6. 官方交易日数据流

### 6.1 信号日

`signal_date` 必须由配置显式提供，且属于验真的交易日历。所有 10 个标的在信号日必须有验真
bar；否则运行在产生目标前失败，不能自动选择一个对结果更有利的起点。

信号在该日收盘之后产生。由于策略只有初始 Buy & Hold，信号只读取 universe、固定权重和已经
结束的 signal-date bar，不读取下一日价格。

`end_date` 也必须属于验真的交易日历，并且日历至少覆盖 `end_date` 之后一个官方 session。
这个额外 session 不进入收益区间，只用于证明在 `end_date` 买入的批次拥有确定的 T+1
`available_date`；缺少该覆盖属于整体输入失败，不得降级成某一标的的普通拒单。

### 6.2 每个交易日的固定顺序

从信号日的下一官方交易日至 `end_date`，每日按以下顺序执行：

1. 验证当前 session 属于冻结日历；
2. 按公司行为证据中的 `record_date` 日终持仓登记当日 ex-date
   现金红利应收；`record_date` 必须早于 `ex_date`，登记日后买入不得获得该次红利；
3. 将 payable date 已到的应收转入共享现金；
4. 为尚未成交且未过期的根目标创建本日 attempt；
5. 对有 bar 的标的使用真实原始开盘价、参考价、整手、共享现金和费用运行
   `evaluate_order()`；
6. 逐笔写入现金、费用、持仓批次和成交事件；
7. 用当日原始收盘价或明确的无 bar 估值规则计算分标的市值；
8. 汇总应收、共享现金、分标的市值和组合权益；
9. 运行逐笔对账、逐日恒等式和非负约束；
10. 只有整日全部校验通过，才把该日状态加入不可变结果。

付款日不是官方交易日时，实际现金日为日历中第一个不早于 payable date 的 session，并同时记录
源 payable date 与 actual cash date。红利现金到账只依赖验真的全市场交易日历，不要求该证券在
actual cash date 存在行情 bar；即使标的当天无 bar，也必须先完成应收转现金，再按无 bar 估值规则
计算组合权益。

冻结的 `v0.1-research` 十标的公司行为中，区间内 payable date 都有对应行情 bar，因此上述语义
差异不改变标签中的正式结果。v0.2 在 `aquant.portfolio` 内实现“第一个不早于源 payable date
的官方 session 入账”，并在单标的等价测试中把“源 payable date 非交易日”列为明确的版本语义
差异；现行 v0.1 单标的路径保持不变。不得移动或重建 `v0.1-research` 标签，也不得更新其预期
run ID 来掩盖实现变化。

### 6.3 无 bar 语义

日线缺 bar 不能证明一定停牌，也可能是免费源缺失。组合审计层统一记录：

```text
availability_status = no_bar_unavailable
source_rule_reason = suspended_no_bar
```

它表达“无法成交”，不把数据缺失伪装成已证实停牌。

第 6.2 节的现金红利应收登记对有 bar 和无 bar 的 ex-date 都无条件执行。本节只定义无 bar 时的
估值 mark 如何调整，不负责决定是否登记应收。

无 bar 门禁必须调用公共可用性检查，不得给 `evaluate_order()` 伪造开盘价来获得拒单，也不得
在组合协调器中内联一份“目标 session 是否存在 bar”的规则。由于 `v0.1-research` 已冻结，
Gate B 不修改已进入冻结实现指纹的 `aquant.rules`；v0.2 在
`aquant.portfolio.availability` 提供唯一公共门禁，组合协调器只能调用该入口。以后若解除
v0.1 冻结并统一规则层，必须以显式版本迁移完成，不能静默改变旧 run ID。

估值规则：

- 已有可信历史收盘价时沿用最后可信 mark；
- 当日存在已验证现金分红 ex-date 事件时，沿用 mark 先扣除每股/份现金分红，再加应收股息，
  防止权益被双计；
- 没有任何历史可信 mark 时立即失败；
- 非现金公司行为仍然 fail-closed。

无 bar 沿用价、沿用天数和调整原因必须进入逐日持仓审计。该估值属于研究近似，不是可交易价格。

## 7. 精确金额与会计

### 7.1 金额表示

- 现金、费用、应收和已实现现金流使用整数分；
- 价格使用 `Decimal`，保留输入的规范精度；
- 成交额和市值在进入账本时按既有 A 股规则 `ROUND_HALF_UP` 到分；
- CSV/JSON 使用规范十进制字符串，不输出不可复现的二进制浮点表示；
- 基础收益指标只从整数分权益序列派生。

组合账本不得把 Backtrader 的 float cash 作为最终会计真相。

### 7.2 逐笔对账

每次买入必须满足：

```text
cash_after
= cash_before
- notional_fen
- commission_fen
- transfer_fee_fen
```

本轮无卖出；共享会计函数仍需用合成测试覆盖卖出：

```text
cash_after
= cash_before
+ notional_fen
- commission_fen
- stamp_duty_fen
- transfer_fee_fen
```

公司行为必须满足：

```text
ex-date: receivable increases, cash unchanged
actual cash date: cash increases by the same amount, receivable decreases
```

### 7.3 逐日恒等式

每个官方 session 必须满足：

```text
portfolio_equity_fen
= shared_cash_fen
+ total_position_market_value_fen
+ total_receivable_fen
```

并同时满足：

- `shared_cash_fen >= 0`；
- 每个 position size、available size、locked size 均为非负整数；
- `available_size + locked_size = total_size`；
- 每个 lot 的 remaining size 不超过 original size；
- 所有账本日期完全一致且严格递增；
- 当日组合市值等于所有 symbol 当日市值之和。

只验证恒等式还不够；逐笔现金变化、成交日期、估值日期和公司行为事件也必须独立断言，防止
“所有表一起错一天仍然自洽”。

## 8. 运行身份

组合 `run_id` 是以下规范 JSON 的 SHA-256，不含墙钟时间、PID、临时目录、对象地址或随机遍历
顺序：

- portfolio schema version；
- engine name 和 version；
- 组合实现指纹；
- 排序后的 universe 成员及 `universe_id`；
- calendar ID 和 SHA-256；
- fee policy digest；
- 排序后的每个行情 snapshot ID、文件 SHA-256 和 canonical input digest；
- 排序后的每个公司行为 snapshot ID、文件 SHA-256、规范化版本和覆盖区间；
- 完整 `PortfolioConfig`；
- 价格流版本、分红税模式、无 bar 估值模式、预算模式和重试模式。
- 持久化结果的规范语义摘要 `result_digest`。摘要覆盖配置、目标、尝试、实际触达费率、
  公司行为、可用性、现金事件、lot、应收、每日权益和逐标的估值；先按稳定主键排序，
  Decimal 再规范为不带无意义尾零的定点字符串。

因此最终身份采用两层确定性摘要：

```text
input_closure_digest = SHA-256(canonical input closure)
result_digest = SHA-256(canonical persisted semantic result)
run_id = SHA-256(schema + engine + implementation_digest
                + input_closure_digest + result_digest)
```

实现指纹至少覆盖：

- 新增的全部 `aquant.portfolio` 文件和 CLI；
- 实际调用的 data access、calendar、corporate actions、universe；
- 复用的 rules、fees、lots、price limits；
- 价格流派生和原子导出代码。

任何绑定字段或上述实现文件改变，都必须改变 run ID。相同输入、不同 tuple 排列、不同当前
时间或不同临时目录必须产生相同 run ID 和字节一致的审计文件。

反向验收器独立解析持久化文件、重建 `result_digest` 和 `run_id`，不得导入导出器、运行身份
构造器或生产指标模块。未提供外部可信的 `expected_run_id` 时，它只证明审计包内部一致，
不能证明来源真实性；需要证明“就是当时批准的那次运行”时，必须由发布清单、只读存储或其他
外部信任锚提供预期 run ID。v0.2 不把本地 SHA-256 冒充数字签名或可信时间戳。

本审计包保存源快照的身份摘要而不是源快照本体，因此单独拿到审计包时，验收器也不能重新证明
某日期属于官方交易日、某估值来自原始行情 bar、某费率来自外部可信政策。它能够证明这些身份
已被运行闭包绑定，并独立验证包内实际触达费率和会计结果；源内容真实性仍由冻结输入包及其
外部验收流程负责。Gate E 的正式运行必须同时保留冻结输入和受信 run ID。

## 9. 审计包和指标

输出路径：

```text
outputs/portfolios/<run_id>/
```

完整包至少包含：

```text
artifact_manifest.json
run.json
targets.csv
orders.csv
fills.csv
positions.csv
lots.csv
cash.csv
equity.csv
receivables.csv
corporate_actions.csv
availability.csv
metrics.json
```

`artifact_manifest.json` 必须枚举除自身外全部文件的 SHA-256、row count、schema version、
`run_id` 和 `status=complete`。目录使用临时同级目录写完、逐文件 fsync、目录 fsync 后原子发布。

若目标目录已经存在：

- 文件集合和每个字节完全相同：幂等成功；
- 少文件、多文件、软链接、硬链接或任意内容冲突：拒绝；
- 不补写、不覆盖、不发布部分目录。

CLI 的 manifest、输入和输出路径必须限制在 project root 下，并拒绝绝对路径越界、`..`、
软链接和危险硬链接。错误输出只包含稳定错误码与错误类型，不回显敏感原始参数。

`metrics.json` 至少报告：

- 总收益；
- 年化收益和年化波动率；
- 最大回撤；
- Sharpe（无风险利率固定为 0，并明确口径）；
- 成交次数和换手率；
- 每日总暴露、最高单标的实际权重；
- 目标权重与实际权重偏差；
- 计划现金储备、目标金额分配舍入余数、整手舍入余数、拒单和过期分别对应的未投资现金；
- 实际已付费用，以及“费用导致减少整手”这一反事实差额（若未触发则为 0）；
- `research_only=true`、`live_trading=false`、`profit_claim=false`。

这些指标用于组合工程验收，不作为 Alpha 结论。

指标口径固定为：

```text
daily_return_t = equity_t / equity_(t-1) - 1
total_return = final_equity / initial_cash - 1
annualized_return = (1 + total_return) ** (252 / observed_return_count) - 1
annualized_volatility = sample_std(daily_return) * sqrt(252)
sharpe_0rf = mean(daily_return) / sample_std(daily_return) * sqrt(252)
turnover = sum(abs(fill_notional_fen)) / mean(daily_equity_fen)
```

最大回撤从逐日权益相对历史峰值计算。少于 2 个日收益或日收益样本标准差为 0 时，
`annualized_volatility` 或 `sharpe_0rf` 输出规范 JSON `null`，不得输出无穷大、NaN 或伪造的
零值。实际权重使用同一 session 的分标的收盘市值除以组合权益。

## 10. 错误处理

### 10.1 整体运行失败

以下情况立即停止，不导出正式包：

- 验真对象、输入闭包、universe、日历、费率或公司行为不一致；
- 起止日期覆盖不足；
- 未支持证券或公司行为；
- 共享现金变负；
- 同一订单、成交或公司行为应用两次；
- 逐笔对账或逐日恒等式失败；
- 共享现金状态与逐笔账本不一致；
- 缺少首次估值价格；
- 输出路径不安全或已有冲突包。

### 10.2 可审计拒单

以下情况只拒绝本次 attempt，组合继续：

- `no_bar_unavailable`；
- 开盘位于保守涨跌停拒单区间；
- 固定目标名义金额不足一手；
- 在规则范围内的费用导致可买数量降到零。

所有 attempt 用尽后根目标变成 `expired_unfilled`。运行成功不代表每个标的都已成交；报告必须
列出未成交标的、尝试次数、原因和未投资现金。

## 11. 测试设计

所有生产代码按照测试驱动开发；每个行为先看到失败测试，再写最小实现。

### 11.1 契约和身份

- 拒绝裸对象、子类、重复 symbol、遗漏成员、范围外成员和被修改对象；
- tuple 正序、逆序和随机排列得到相同 run ID 和结果；
- 增删任一输入或改变配置/实现指纹必须改变 run ID；
- 墙钟时间、PID、临时目录和 dict/set 顺序不能改变身份；
- 正式运行期间网络函数被调用即测试失败。

### 11.2 预算和共享现金

- 2、3、10 标的合成价格验证固定等权目标名义金额；
- 高价标的不足一手时明确拒单，目标名义金额不转给其他标的；
- 单标的 10,000 元、0.95 权重、ETF 开盘 95 元时成交 100 份，佣金从现金储备支付；
- 开盘跳空后成交名义金额不超单标的目标，成交额与费用不超共享现金；
- 两只 10 元标的、20,000 元共享现金、满仓目标时，第一只成交 1,000 股后，第二只初始
  1,000 股候选因成交额和费用超过剩余现金而只缩减当前标的至 900 股；两种输入排列必须得到
  相同成交、费用和 989.81 元剩余现金；
- 任意 symbol 输入排列不改变成交、现金和实际权重；
- 同日多标的成交后共享现金不为负；
- 计划储备、分配舍入、整手舍入、拒单和过期未投资现金之和不重复计数；
- 人为破坏一个现金事件时逐笔对账必须失败。

### 11.3 时序和规则

- T 日收盘目标只能在 T+1 官方 session 开盘尝试；
- 周末和节假日不算重试次数；
- 目标最多 5 次，成功后不再尝试，过期后不复活；
- 连续 5 个官方 session 无 bar 时产生 5 个 `no_bar_unavailable` attempt，第 6 个 session 不再
  创建 attempt，根目标为 `expired_unfilled`；
- 同 symbol、同 session 不产生重复 attempt；
- 无 bar、涨跌停开盘、末端日历和费用缺失按预期拒绝；
- 持仓批次的 T+1 可卖日正确；
- 公司行为在 ex-date 登记、actual cash date 入账且只应用一次。
- 红利资格按 `record_date` 日终持仓判断；登记日后买入不享有该次红利，登记日当天买入仍享有；
- `record_date` 不早于 `ex_date` 时在账本变更前整体失败；
- payable date 落在非交易日时下一官方 session 入账；该标的当天无 bar 也不影响现金到账。

### 11.4 估值和会计

- 每日现金、分标的市值、应收与权益使用同一 session；
- 无 bar 沿用最后 mark；
- 无 bar 且 ex-date 时调整 mark 并登记应收，权益不被双计；
- 没有首次 mark 时 fail-closed；
- 整数分与 Decimal 舍入边界，包括 ETF 三位价格和最低佣金；
- 日频恒等式和逐笔现金公式分别测试。

### 11.5 单标的等价测试

选择不含拒单、无 bar、源 payable date 非交易日、未支持公司行为和下述命名版本边界的合成
单标的场景，使用同一：

- 原始行情；
- 交易日历；
- 公司行为；
- 费率策略；
- 初始现金和目标权重。

分别运行 `v0.1` Backtrader 路径和 `v0.2` 单成员组合路径，断言：

- 信号日、执行日、成交价和成交数量；
- 各费用分项；
- 每日现金、持仓、市值、应收和权益；
- 最终批次状态。

run ID 和文件 schema 不要求相同，因为两条引擎身份不同。若经济结果不一致，必须解释并修复
公共规则边界，不能用调整测试容差掩盖。

另增加一个 v0.2 专项测试，证明付款日落在非交易日时使用下一官方 session，且该标的现金到账
日没有 bar 也不妨碍入账。这个场景不进入 v0.1/v0.2 经济等价集合；测试和审计报告必须把它
标为 v0.2 的显式语义增强，而不是假装冻结版已经支持。

2026-07-29，用户批准方案 A，将冻结 v0.1 的红利资格旧缺陷命名为
`known_v01_record_date_entitlement_defect`，并作为明确的版本边界处理：

- 冻结 v0.1 在 ex-date 开盘前按当时持仓判断资格；v0.2 保持按 record date 日终持仓判断的
  A 股正确口径；
- 只排除 `acquired_date > record_date` 且 `acquired_date < ex_date` 的特定旧版边缘，
  不得把其他红利、公司行为或日期差异并入该排除项；
- 正常现金红利等价场景仍要求 `acquired_date <= record_date`，并继续逐项比较资格、应收、
  到账、现金和权益；
- 永久保留红灯回归测试，精确证明 v0.1 与 v0.2 在该边缘上的差异；该测试是版本边界证据，
  不是经济等价通过证据；
- 不修改、移动或重建 `v0.1-research` 标签及其冻结生产路径，也不让 v0.2 模仿旧版缺陷。

### 11.6 正式 10 标的验收

Gate E 的正式隔离复演以
`docs/superpowers/specs/2026-07-29-v02-gate-e-isolated-release-replay-design.md`
为唯一详细合同。至少要求：

- 使用清单声明的 25 个冻结文件，零字节 lock 偏差单独审计，禁止宽泛忽略额外文件；
- 机器可读配置固定 10 标的、日期、初始现金、权重、重试、费用与全部快照 ID；
- 构建并安装同一份 `a-share-quant 0.2.0` wheel，不使用 `PYTHONPATH`；
- A/B 使用独立 venv、HOME、缓存、输入、项目根和输出根，并在双层断网下完整重算；
- 2026-07-24 只作 `next_session(end_date)` 覆盖，不进入决策、账本或绩效；
- 每个 run-ID 目录恰好 13 个文件，A/B run ID 和原始字节全部一致；
- 外部 trust manifest 绑定 commit、wheel、锁文件、配置、25 文件、expected run ID
  和13文件哈希，反向验证 A/B；
- 保留全部 28 个 no-bar、失败、实际权重和现金拖累证据，不取共同日期交集；
- Gate E 指定测试文件显式全绿；全仓无失败且只允许既有具名 v0.1
  完整复演测试跳过；保存规范化测试收集清单及 SHA-256；
- Work Buddy 完成独立设计、代码和审计包复核，最终
  `P0=0 / P1=0 / P2=0`。

## 12. 分阶段交付门

### Gate A：不可变契约、精确金额和共享账本

- 配置和输入对象无法绕过验真边界；
- 固定目标名义金额、计划现金储备和整数分现金成立；
- 逐笔与逐日对账均能抓住故意错误；
- 旧 401 项测试继续通过。

### Gate B：多标的日历、尝试和 A 股规则

- 2/3/10 标的合成场景通过；
- 无未来函数、无同日重复订单；
- 5 次官方 session 重试和过期行为明确；
- no-bar、涨跌停、费用、T+1 和现金红利通过。

### Gate C：身份、原子导出和反向验证

- run ID 输入闭包完整；
- 排列、时间和临时目录不影响结果；
- 完整包原子、幂等、冲突时拒绝；
- 反向校验能够发现任一字节损坏。

### Gate D：单标的经济等价

- v0.1 Backtrader 与 v0.2 单成员组合在 A–E 获批等价集合中的经济结果精确一致；
- 等价场景明确排除源 payable date 非交易日这一已知版本差异，以及用户于 2026-07-29
  批准的 `known_v01_record_date_entitlement_defect`；
- 后一边界只覆盖 `acquired_date > record_date` 且 `acquired_date < ex_date`，正常现金红利
  场景仍要求买入日不晚于 record date；
- F 红灯回归必须永久、精确、可复现地证明该差异，且不得计入 A–E 等价证据；
- A–E 精确一致、F 持续可复现、冻结 v0.1 未变且最终审查
  `P0=0 / P1=0 / P2=0` 后，Gate D 才可通过；
- 若再发现其他 v0.1 语义缺陷，单独记录并先取得用户批准，不在组合提交中静默改写发布语义
  或扩大排除范围。

### Gate E：冻结 10 标的共享现金运行

- 同一正式 wheel 在两个真正隔离、禁止出站网络的环境中安装和重算成功；
- 一个账户、一个组合权益序列、10 个分标的持仓序列；
- run ID、13 文件精确集合和原始字节一致，外部信任锚反验通过；
- 全部 28 个 no-bar、实际权重、现金拖累和失败证据原样输出；
- 2026-07-24 不参与正式决策、账本或绩效；
- 所有对账、冻结输入、环境隔离、wheel 和审计门通过；
- 不声称策略有效或实盘可行。

## 13. 已知限制

- 固定 10 个标的是 2026 年工程样本，存在幸存者偏差和选择偏差；
- `research_approx` 指标与免费源历史回写风险仍存在；本策略虽然不使用 SMA，数据来源限制仍需
  披露；
- no-bar 沿用价不能证明真实可变现价值；
- 日线不能证明开盘排队和成交量；
- 无滑点、部分成交和市场冲击会高估成交可行性；
- 固定等权 Buy & Hold 不验证选股、择时或 Alpha；
- 组合引擎正确不等于纸面模拟稳定，更不等于实盘安全。

## 14. 实施与审查顺序

1. 提交本设计文档；
2. 独立审查者只读审查设计；自 2026-07-28 起该角色由 Work Buddy 承担；
3. 核验并修复有效 P0/P1；P2 修复或明确延期；
4. 用户复核落盘规格；
5. Codex 完整规格自审，先关闭预算语义与付款日语义冲突；
6. 编写逐文件、逐测试的实施计划；
7. 在当前隔离 v0.2 分支按 TDD 完成 Gate A；
8. 每个自然检查点运行专项测试和原有回归；
9. 依次完成 Gate B、C、D、E；
10. Work Buddy 做独立代码和审计包复核；
11. Codex 独立运行全量测试、Ruff、锁文件、构建和冻结重算；
12. 只在所有门通过后推送 GitHub 分支并申请合并；
13. `v0.2-research` 标签必须另行满足干净环境发布门，不能因功能分支完成而自动创建。
