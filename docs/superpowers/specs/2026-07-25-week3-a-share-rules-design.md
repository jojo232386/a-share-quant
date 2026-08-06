# 第 3 周最小 A 股交易规则设计

## 1. 目标与边界

第 3 周把第 2 周的“工程回测闭环”升级为保守的 A 股日线成交模拟。目标是拒绝日线数据无法证明可成交的订单，并让每次成交、拒单、费用和持仓可用数量都能复算。

本阶段仍然只用于研究和模拟，不连接券商、不自动下单，也不证明 Buy & Hold、SMA 或其他策略有效。

固定支持范围不扩张：

| 代码 | 类型 | 交易制度 |
| --- | --- | --- |
| 510300 | `domestic_equity_broad_based_etf` | 股票 ETF，T+1 |
| 600519 | `main_board_stock` | 普通主板股票，T+1 |
| 601318 | `main_board_stock` | 普通主板股票，T+1 |
| 000001 | `main_board_stock` | 普通主板股票，T+1 |

ST、新股特殊阶段、退市整理期、创业板、科创板、北交所和其他 ETF 均继续关闭运行。债券、黄金、跨境和货币 ETF 的 T+0 规则留到后续版本。

## 2. 已确定的保守口径

1. 信号只使用当日及以前的收盘数据。
2. 订单只在信号日的下一官方交易日开盘尝试成交。
3. 买入时开盘价等于涨停价，拒单。
4. 卖出时开盘价等于跌停价，拒单。
5. 日线不能证明开盘排队顺序，因此即使当天后来打开涨跌停，也不回填成交。
6. 信号日下一官方交易日缺少该证券 K 线时，订单以 `suspended_no_bar` 拒绝，不自动带到复牌日。
7. “缺 bar”只表示保守模拟中的不可交易证据，不声称已经通过历史状态数据证明真实停牌。
8. 买入数量必须为 100 股或 100 份的整数倍。
9. 普通部分卖出必须为 100 的整数倍；一次性卖出全部持仓时允许清理不足 100 的剩余数量。
10. 资金不足、可卖数量不足或规则证据缺失时整单拒绝，不自动缩量、不部分成交。

## 3. 架构

采用“独立纯规则引擎＋薄 Backtrader 执行适配层”，不把规则分散到各个策略中。

### 3.1 纯规则引擎

新增 `src/aquant/rules/`，按职责拆分：

- `models.py`：不可变的订单意图、证券规则、持仓批次、费用明细、允许/拒绝结果和原因枚举；
- `calendar.py`：已验证交易日历的下一交易日查找和覆盖检查；
- `price_limits.py`：涨跌停价和价格最小变动单位计算；
- `lots.py`：整手、零股收尾、T+1 可卖批次和 FIFO 扣减；
- `fees.py`：历史法定费率、佣金假设、逐项舍入和触达费率记录；
- `engine.py`：按固定顺序组合上述纯函数，返回唯一决策。

规则函数不导入 Backtrader，不读取全局配置，不访问网络和文件。相同输入必须得到相同输出。

### 3.2 Backtrader 适配层

新增薄执行适配层，所有策略仍只负责产生 `buy` 或 `sell` 意图。适配层在 Backtrader 尝试执行市价单时统一完成：

1. 从订单信号日和已验证日历得到目标执行日；
2. 比较实际到达的 bar 日期和目标执行日；
3. 若跨过目标执行日，拒绝为 `suspended_no_bar`，不得在复牌 bar 成交；
4. 在目标执行日开盘取得原始开盘价和前收盘价；
5. 调用纯规则引擎检查证券范围、价格限制、手数、T+1、可用现金和费用；
6. 只有 `ALLOW` 才进入 Backtrader 成交路径；
7. 成交后以实际成交结果更新持仓批次和审计流水。

规则门禁必须位于所有策略共享的执行入口。新增策略不能通过直接调用 `buy()` 或 `sell()` 绕过；测试需要覆盖绕过尝试。

### 3.3 数据流

```text
行情 manifest + snapshot_id ──> VerifiedMarketData ─┐
                                                    ├─> BacktestContext
日历 manifest + calendar_id ──> VerifiedCalendar ──┤
                                                    ├─> RuleEngine
显式费用配置 ───────────────────────────────────────┘

策略收盘信号 -> 订单意图 -> 次日开盘规则决策
                         ├─ ALLOW  -> Backtrader 成交 -> 批次/现金/净值
                         └─ REJECT -> 机器可读原因   -> 无 Fill
```

## 4. 不可变交易日历

交易日历是全市场共享证据，不进入按证券记录的 `data/manifests/manifest.jsonl`。

新增：

```text
data/calendars/
├── <calendar_id>.json
└── manifest.jsonl
```

`<calendar_id>.json` 使用稳定 schema 保存按升序排列、无重复、无周末的 ISO 日期。`calendar_id` 是规范化内容的 SHA-256；抓取时间不进入内容哈希。

独立 calendar manifest 至少记录：

- `schema_version`
- `calendar_id`
- `file_sha256`
- `source_provider`
- `source_function`
- `source_version`
- `fetched_at_utc`
- `first_date`
- `last_complete_date`
- `row_count`
- `relative_path`

正式加载器返回不能由调用方直接构造的 `VerifiedTradingCalendar`，并在读取前后复核文件哈希、schema、日期顺序、重复、周末、行数和日期范围。

回测 CLI 必须显式提供完整的 `--calendar-id`。默认 calendar manifest 路径为 `data/calendars/manifest.jsonl`，路径仍受项目根目录约束。

日历更新产生新的 `calendar_id`，不会改变既有行情快照或其哈希。回测 `run_id` 同时绑定行情快照 ID、日历 ID 和两者的文件哈希。

日历覆盖必须包含行情起止区间。若日历没有覆盖信号日之后的下一交易日，订单使用 `no_next_session_in_range`，不能误判为停牌。

## 5. 行情缺口与停牌

第 1 周“请求区间缺一交易日就整批失败”的门禁调整为两层：

1. 行情中出现不属于已验证日历的日期，继续关闭运行；
2. 已验证日历中存在、单标的行情中缺失的日期，保存为该标的的 `missing_market_sessions`，允许进入第 3 周保守撮合。

缺口不能静默消失。新增 `missing_sessions.json`，按升序保存本次行情区间内的全部缺失日期；`run.json` 保存其行数和 SHA-256，`artifact_manifest.json` 覆盖该文件。缺口只用于判定不可交易，绝不使用停牌前收盘价填充 OHLC。

如果订单的目标执行日是缺失日期：

- 订单终态为 `rejected`；
- `rejection_reason=suspended_no_bar`；
- `target_execution_date` 保留该缺失交易日；
- 不产生 Fill；
- 不携带到下一根有效 bar；
- 复牌后的新订单必须来自复牌后新的有效收盘信号。

## 6. T+1 与持仓批次

持仓不能只保存一个总数量。每笔买入成交生成：

```text
PositionLot(
  lot_id,
  symbol,
  acquired_date,
  available_date,
  original_size,
  remaining_size,
  unit_cost
)
```

`available_date` 是买入成交日之后的下一官方交易日。它由交易日历决定，与该证券在该日是否停牌无关：停牌会阻止卖出成交，但不会延后证券交收后的可卖状态。

卖出检查执行日满足 `available_date <= execution_date` 的批次，按 FIFO 扣减：

- 可卖批次总量不足时整单拒绝为 `insufficient_sellable_position`；
- 不自动缩量；
- 买入当日批次不能卖出；
- 成交后批次余额、总持仓和 Backtrader 持仓必须一致；
- 每日导出的总持仓、可卖持仓和锁定持仓必须满足恒等式。

## 7. 手数和零股

规则如下：

- 买入数量必须大于零且能被 100 整除；
- 非清仓卖出数量必须能被 100 整除；
- 卖出数量等于全部可卖持仓时，允许包含不足 100 的尾数；
- 持仓少于 100 时，只允许一次性全部卖出；
- 不支持任意零股部分卖出。

违反规则时整单拒绝为 `invalid_lot_size`。

## 8. 涨跌停

只支持普通主板股票和 510300 的 10% 涨跌幅限制。计算使用未复权前收盘价：

```text
limit_price = previous_close × (1 ± 0.10)
```

价格按证券最小变动单位使用十进制定点数和 `ROUND_HALF_UP`：

- 主板股票：`0.01` 元；
- 510300：`0.001` 元。

执行适配层只在目标执行日开盘检查：

- 买入且 `open >= upper_limit`：`price_limit_open`；
- 卖出且 `open <= lower_limit`：`price_limit_open`。

正常数据中开盘价不应越过涨跌停价；使用不等式可以让异常数据保守拒单。缺少前收盘价、价格不能按最小单位解释或证券类型不支持时关闭该订单，不猜测涨跌停价。

## 9. 历史费用

所有费率使用 `date -> rate` 的生效日映射。查询规则统一为“小于等于成交日期的最新生效日期”。成交日期早于第一条费率或映射字段/类型不合法时关闭运行。

### 9.1 法定费率

项目回测从 2018 年开始，内置且版本化的最低历史表为：

```text
股票卖出印花税：
2008-09-19 -> 0.001
2023-08-28 -> 0.0005

沪深 A 股过户费，买卖双向：
2015-08-01 -> 0.00002
2022-04-29 -> 0.00001
```

510300 不收股票印花税和 A 股过户费。

### 9.2 券商佣金研究假设

佣金不是统一市场规则，配置必须显式给出，并区分股票与 ETF。v0.1 推荐研究默认值：

```text
股票：rate=0.00025, minimum=5.00, 双向
510300：rate=0.00025, minimum=5.00, 双向
```

该默认值是保守研究假设，不声称代表用户真实账户。佣金假设视为已包含券商通常合并收取的经手和监管费用；v0.1 不再重复拆收这些费用。

### 9.3 舍入和现金

费用使用 `Decimal`，禁止二进制浮点参与费率查表和舍入。每个费用分量独立计算：

1. 佣金先取 `max(notional × rate, minimum)`；
2. 每个分量分别按 `0.01` 元、`ROUND_HALF_UP` 量化；
3. 再把佣金、印花税和过户费相加。

审计模型内部保存整数分，导出为固定两位小数字符串。Backtrader 边界可以接收总费用的浮点副本，但审计结果和会计复算以整数分为准。

买入可用现金必须覆盖成交金额和全部费用，否则整单拒绝为 `insufficient_cash`，不自动缩量。卖出所得现金为成交金额减全部费用。

### 9.4 触达费率

`run.json` 记录本次运行实际触达的全部生效条目，而不是只记录配置文件：

- 费用名称；
- 适用证券类型和方向；
- 生效日期；
- 费率；
- 最低费用；
- 舍入单位与舍入模式；
- 首次和最后一次触达成交日期。

跨越两个印花税或过户费时代时，两条记录必须同时出现。

## 10. 订单状态、拒单和导出

订单使用一个稳定终态和独立原因字段，避免把原因编码进状态：

```text
final_status: submitted | accepted | completed | rejected | canceled
rejection_reason: null | <ReasonCode>
```

最低原因集合：

- `unsupported_instrument`
- `missing_calendar_coverage`
- `no_next_session_in_range`
- `suspended_no_bar`
- `missing_previous_close`
- `price_limit_open`
- `invalid_lot_size`
- `insufficient_cash`
- `insufficient_sellable_position`
- `missing_fee_schedule`
- `invalid_fee_configuration`

所有 `rejected` 都是终态，不进入 Fill、不自动重试。重复通知必须幂等。

扩展审计输出：

- `orders.csv`：增加目标执行日和拒单原因；
- `fills.csv`：增加佣金、印花税、过户费和总费用；
- `positions.csv`：增加总持仓、可卖持仓和锁定持仓；
- `lots.csv`：逐批次取得日、可卖日、原始数量和剩余数量；
- `run.json`：增加日历 provenance、规则版本、费用配置和触达费率；
- `artifact_manifest.json`：覆盖新增载荷文件及其 SHA-256。

## 11. 身份和不可绕过门禁

`run_id` 必须新增绑定：

- `calendar_id` 和日历文件 SHA-256；
- `instrument_kind` 以及固定代码与类型的映射；
- 证券规则配置；
- 历史费率表；
- 券商佣金假设；
- 舍入政策；
- 缺失行情日期证据；
- 新增规则和适配层源码指纹。

正式 `run_backtest` 只接收加载器生成的精确类型：

- `VerifiedMarketData`
- `VerifiedTradingCalendar`
- `VerifiedFeePolicy`
- `BacktestConfig`

裸 DataFrame、自填 calendar ID、伪造费率对象、子类对象和范围外证券全部关闭运行。

## 12. 测试与验收

实现必须使用 TDD。最低验收矩阵：

1. 股票和 510300 买入均为 T+1，当日批次不可卖；
2. 多日买入形成多个批次，卖出按 FIFO 消耗可卖批次；
3. 个股停牌不延后批次的 `available_date`；
4. 买入非 100 整数倍拒单；
5. 普通部分卖出非 100 整数倍拒单；
6. 全仓卖出允许零股尾数；
7. 开盘涨停买入拒单，开盘跌停卖出拒单；
8. 开盘未触板、盘中触板仍按开盘规则处理；
9. 下一官方交易日缺 bar 时拒绝，节假日不误判；
10. 缺 bar 订单不在复牌日自动成交；
11. 日历哈希、schema、顺序、重复、周末、覆盖和路径逃逸测试；
12. 日历更新不改变既有行情文件哈希；
13. 印花税和过户费跨生效日自动切换；
14. 股票与 ETF 费用分流；
15. 每项费用按分位半入舍入，最低佣金可人工复算；
16. 资金不足整单拒绝，不缩量；
17. 可卖数量不足整单拒绝，不部分成交；
18. 订单、成交、批次、现金和净值逐日恒等；
19. 所有拒单都有稳定原因且没有 Fill；
20. 相同数据、日历、规则和参数重复运行的 `run_id` 与全部文件一致；
21. 任一输入、费率、规则实现或日历变化都会改变 `run_id`；
22. 策略不能绕过共享规则入口直接成交；
23. 正式入口拒绝裸对象、伪造 provenance 和范围外证券；
24. 全部新增文件原子发布，冲突时不补写或覆盖。

第 3 周通过条件：

- 全部现有测试继续通过；
- 新增规则矩阵全部通过；
- Ruff、锁文件、构建和差异检查通过；
- 四个真实快照使用显式 calendar ID 重新运行；
- 费用、拒单和会计流水可人工复算；
- Codex 自审和 Claude Code（DeepSeek API）代码级复核均为 `P0=0、P1=0`；
- 报告继续声明没有策略有效性或可实盘结论。

## 13. 官方依据

- 上海证券交易所 ETF 常见问题：股票 ETF 实施 T+1，ETF 最低 100 份，最小价格变动单位 0.001 元：<https://etf.sse.com.cn/fund/quertion/>
- 上海证券交易所交易规则（2026 年修订）：股票、基金涨跌幅限制比例及价格计算、A 股与基金最小价格变动单位：<https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml>
- 中国结算 2022 年过户费调整通知转载全文：沪深 A 股按成交金额双向收取，2022-04-29 起由 0.02‰ 降至 0.01‰：<https://finance.sina.cn/2022-04-28/detail-imcwipii6993883.d.html>

实施前应把使用到的费率规则来源、抓取日期和原文摘要固定在项目文档中。若官方规则后续变化，只新增生效日条目，不重写既有历史费率。
