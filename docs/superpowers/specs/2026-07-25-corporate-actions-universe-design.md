# A 股公司行为正确性与 10 标的试点设计

日期：2026-07-25
状态：Claude Code（DeepSeek API）第二次设计门通过（P0=0、P1=0）

## 1. 目标

在不更换 AKShare + Backtrader 技术栈的前提下，修复三项会使收益和风险指标失真的语义：

1. 分红等公司行为未进入持仓与现金；
2. 除权日用上一根原始收盘价计算涨跌停；
3. Buy & Hold 和 SMA 固定买 100 股，导致不同标的暴露不可比。

正确性门通过后，把固定四标的扩展成 10 标的试点。10 标的是 10 次共享规则但互相独立的
单标的回测，不是共享现金组合。

## 2. 非目标

- 不接券商，不发送真实订单；
- 不证明策略有效，不给收益承诺；
- 不支持创业板、科创板、北交所、ST、退市整理、新股特殊阶段；
- 不支持送股、转增、拆股、配股、合并、证券代码变更；
- 不处理投资者差异化红利税，现金分红按公开的税前每股/每份金额记账并明确披露；
- 不做多标的共享现金组合；
- 不引入 Qlib、多 Agent 或强化学习；
- 不把当前筛选的 10 个标的称为历史时点安全股票池。

## 3. 数据和语义边界

### 3.1 三条价格流

正式回测必须同时存在三条不同语义的价格：

| 字段 | 用途 | 禁止用途 |
|---|---|---|
| `raw_open/high/low/close` | 成交、持仓市值、现金和净值 | 不能直接作为跨除权日的 SMA 输入 |
| `indicator_close` | SMA 等技术指标 | 不能成交，不能计算真实市值 |
| `reference_price` | 当日涨跌停上下限基准 | 不能成交，不能计算净值 |

`indicator_close` 采用因果连续化：只使用当日及此前已发生的公司行为。设前一交易日原始收盘为
`P_prev`，当日除权参考价为 `P_ref`，上一日累计连续化因子为 `F_prev`，则当日起：

```text
F_today = F_prev * P_prev / P_ref
indicator_close_today = raw_close_today * F_today
```

无公司行为时 `F_today = F_prev`。这个设计不会把未来公司行为提前放进过去的信号。

### 3.2 公司行为快照

公司行为独立于行情和交易日历，使用：

```text
data/corporate_actions/
├── manifest.jsonl
└── <symbol>/<content_sha256>.json
```

每个规范化事件至少含：

```text
symbol
instrument_kind
announcement_date
record_date
ex_date
payable_date
cash_dividend_per_unit
stock_dividend_ratio
capitalization_ratio
rights_ratio
rights_price
source_schema
source_url
```

快照 manifest 必须绑定文件 SHA-256、symbol、provider、source schema、日期覆盖、行数和
规范化版本。正式回测只接收由加载器产生的精确 `VerifiedCorporateActions` 对象；裸
DataFrame、裸 dict、修改后的对象和伪造 provenance 全部拒绝。

数据来源：

- 普通主板股票：AKShare `stock_dividend_cninfo`；
- 境内股票宽基 ETF：AKShare `fund_etf_dividend_sina` 的除息日/累计分红，与新浪基金
  历史分红页的权益登记日、付款日和每份分红交叉匹配；
- 同一事件两源金额、日期无法匹配时拒绝发布快照。

ETF 路径已在设计阶段完成最小可行性验证，不是把未知网页抓取留给实现阶段：

- `fund_etf_dividend_sina("sh510300")` 可返回 `日期、累计分红`，其中日期与除息日对应；
- `https://stock.finance.sina.com.cn/fundInfo/view/FundInfo_JJFH.php?symbol=510300`
  在 2026-07-25 返回 HTTP 200，页面编码为 GB2312/GBK；
- 页面历史分红表可解析出 `权益登记日、红利发放日、每份分红(元)`；
- 例如 2024 年记录为权益登记日 2024-01-17、除息日 2024-01-18、付款日
  2024-01-23、每份 0.069 元，两条路径金额一致。

实现使用 GB18030 解码、固定表头白名单和严格日期/金额匹配。HTTP 错误、编码错误、表头变化、
重复事件、差分为负或两表匹配失败都返回结构化错误，不发布半成品。该双路径验证不是为了宣称
两个独立来源，而是为了补齐同一 provider 的不同页面分别提供的除息日和付款日。

股票字段按公开接口“每 10 股”口径除以 10。相同 symbol、ex_date 的多条现金方案先逐条
验证，再按每股金额求和。ETF 累计分红先按日期排序，相邻差分得到单次每份分红，并与详情页
金额一致后才发布。

### 3.3 支持和拒绝策略

v0.1 只允许：

```text
cash_dividend_per_unit >= 0
stock_dividend_ratio = 0
capitalization_ratio = 0
rights_ratio = 0
rights_price = null
```

日期覆盖内出现任何非零送转、拆分或配股字段时，正式回测以
`unsupported_corporate_action` 失败，不跳过、不近似、不静默降级。

若公司行为快照缺失、上游不可用、超出覆盖期、校验失败或证券种类不一致，正式回测停止。
上游不可用使用 `unavailable_corporate_actions`，数据存在但含未支持事件使用
`unsupported_corporate_action`；两者都不允许降级成忽略公司行为的正式回测。合成测试可使用
明确标记的 synthetic corporate actions，但不能导出为正式研究包。

### 3.4 除权参考价

普通现金分红事件：

```text
reference_price = previous_raw_close - cash_dividend_per_unit
```

实现保留完整公式的字段边界，但 v0.1 不执行未支持分支：

```text
(previous_raw_close - cash_dividend + rights_price * rights_ratio)
/
(1 + stock_dividend_ratio + capitalization_ratio + rights_ratio)
```

结果必须为正数，并按规则引擎既有价格步长计算涨跌停价。非除权日：

```text
reference_price = previous_raw_close
```

第一根 bar 没有前收时不得产生可成交订单。broker 不再读取 `close[-1]` 作为规则输入，而读取
当前 bar 已验证的 `reference_price`。

### 3.5 股息会计

在 ex_date 开盘前：

1. 以当时持仓数量登记 `dividend_receivable = size * cash_dividend_per_unit`；
2. 应收股息立即计入权益，但不计入可用现金；
3. 在 payable_date（若非交易日则下一交易日）转入现金并冲减应收股息；
4. 每个事件只能应用一次；
5. 当日收盘恒等式变为：

```text
cash + raw_close * position_size + dividend_receivable = equity
```

输出新增：

- `corporate_actions.csv`：事件、持有数量、应收金额、计划和实际入账日期；
- `receivables.csv`：逐日应收股息余额；
- `run.json`：公司行为快照 ID/SHA-256、事件数、触达事件、未支持事件数、
  `dividend_tax_mode = gross_before_personal_tax`。

### 3.6 Backtrader 日内调用顺序

公司行为不能放在 `_try_exec_market` 中，因为没有挂单的交易日不会调用成交钩子。正式实现固定
使用以下顺序：

1. feed 进入交易日 T，Cerebro 先调用 `RuleAwareBackBroker.next()`；
2. broker 在 `super().next()` **之前**读取绑定 feed 的当前日期：
   - 登记 T 日 ex-date 应收，持仓数量取 T-1 日收盘后的持仓；
   - 将 payable_date 已到期的应收转成 broker 可用现金，使现金可以参加 T 日开盘订单；
3. `super().next()` 处理挂单，市场单在 `_try_exec_market` 使用 T 日真实 `popen`；
4. `super().next()` 以 T 日原始 close 更新内部组合价值；
5. strategy 的 `next()` 才运行，使用 T 日 `indicator_close` 生成下一交易日订单并记录 T 日账本。

feed 在加入 Cerebro 后显式绑定给 broker，不通过“是否已有持仓”猜当前日期。股息现金在
`super().next()` 前直接进入 broker 现金状态，同时写入一次性 event application ledger；
不能只调用会在日末才排空的 `add_cash()` 队列。

broker 对外 `getvalue()` 返回 Backtrader 原始现金与市值加未付应收股息。Backtrader 内部
`_value` 仍只保存现金和原始市值，避免应收被重复计入。策略账本使用独立
`ReceivableRecord` 与相同日期的 cash、position、equity 四表联结验证恒等式。

`cheat_on_open=False` 保持不变。策略在 T 日收盘只能提交“目标仓位意图”，不能读取 T+1
开盘；真实股数只能在 T+1 的 broker `_try_exec_market` 中计算。

若异常上游数据给出 `payable_date <= ex_date`，实现仍按“先登记应收、再处理到期应收”的顺序，
允许同一 broker 日内把该笔应收转为现金，但必须在事件审计中记录
`same_day_or_early_payment = true`。付款日早于除息日属于源数据冲突，快照发布阶段直接拒绝；
付款日等于除息日是合法边界并有专项测试。

### 3.7 目标仓位

`BacktestConfig.stake` 从正式 CLI 移除，改为：

```text
target_weight: Decimal，默认 0.95，范围 (0, 1]
lot_size: 100，由 instrument rule 提供
```

策略提交买单时只携带 `target_weight`，并使用一个内部占位量让 Backtrader 建立订单状态；
占位量不进入审计请求数量。买单在目标执行日由 broker 使用真实开盘价重新写入正式请求数量，
而不是在信号日猜测：

1. 开盘权益 = 可用现金 + 应收股息 + 当前持仓 × 当前原始开盘价；
2. 目标市值 = 开盘权益 × target_weight；
3. 目标股数 = floor(目标市值 / 开盘价 / 100) × 100；
4. 扣除已有持仓得到订单股数；
5. 买入时考虑佣金、最低佣金、印花税/过户费适用方向和现金上限；
6. 如现金不足，按 100 股逐档缩减到最大可成交数量；
7. 小于 100 股则明确拒绝；
8. 卖出信号仍卖出全部可卖持仓。

`_try_exec_market` 必须先检查目标执行日期和涨跌停，再计算/缩减数量，最后调用 Backtrader
真实成交路径。订单导出中的 `requested_size` 记录 broker 在开盘确定的正式数量，并另存
`target_weight`，不能导出占位量。这个钩子可以使用 T+1 的 `popen`，因为订单已经在 T 日
收盘提交，且策略直到 broker 完成 T+1 开盘处理后才看到 T+1 bar。

实现首先用合成订单测试验证 Backtrader 1.9.78.123 的内部契约：在真实成交前同时更新
`order.size`、`order.created.size` 和 `order.executed.remsize`，再调用
`super()._try_exec_market()`。若该契约测试不成立，停止实现并重新设计为 broker 内部重建订单；
不在未验证的情况下继续依赖私有字段。

Buy & Hold 的定义变为“首次可执行日将组合调整到目标仓位并持有”。SMA 也使用相同目标仓位，
保证同一标的两个策略的初始风险预算一致。不同标的实际暴露率必须输出，不能只输出目标值。

## 4. 10 标的试点

### 4.1 固定四标的回归层

任何扩展前必须先重跑：

- 510300；
- 600519；
- 601318；
- 000001。

四者 2018-01-01 至最新完整交易日的公司行为快照必须完整；当前只观察到现金分红，正式实现
仍须以下载后的快照门禁为准。

### 4.2 10 标的试点层

通过四标的门后，增加：

| symbol | kind | 选择用途 |
|---|---|---|
| 510500 | domestic_equity_broad_based_etf | 第二只宽基 ETF，验证 ETF 事件路径不是单标的特例 |
| 600036 | main_board_stock | 银行、沪市 |
| 600900 | main_board_stock | 公用事业、沪市 |
| 600030 | main_board_stock | 券商、沪市 |
| 000858 | main_board_stock | 消费、深市 |
| 601166 | main_board_stock | 银行、沪市 |

加上原四标的共 10 个。它们是 2026 年事后选择的高流动性工程样本，存在幸存者偏差。试点只
验证数据和回测管道的扩展性，不用于证明策略在 A 股整体有效。

设计阶段已通过当前 AKShare 公司行为接口预检新增 5 只股票：2018-01-01 之后
`600036、600900、600030、000858、601166` 分别有 10、11、11、11、10 条事件，未观察到
非零送股或转增。`510500` 的 ETF 累计分红路径也可返回记录。这个预检只证明接口当前可读，
不替代实施阶段的行情覆盖、双路径匹配、快照验真和未支持事件正式门禁。

### 4.3 Universe manifest

移除 `config.py`、AKShare client 和规则引擎中的 symbol 白名单。新增内容寻址 universe：

```text
configs/universes/
├── regression-4.json
└── pilot-10.json
```

每项至少含 `symbol`、`instrument_kind`、`selection_role`。规范化内容的 SHA-256 即
`universe_id`。正式 CLI 使用 `--universe-id` 或单个 `--symbol` + 已验证 universe 成员，
不能靠任意六位数字绕过证券种类和范围门。

规则支持由 `InstrumentKind` 决定，试点成员资格由 VerifiedUniverse 决定。二者职责分开。

## 5. 运行身份与导出

`run_id` 新增绑定：

- corporate action snapshot ID 和 SHA-256；
- corporate action normalization version；
- target_weight；
- universe_id（批量任务时）；
- 三价格流的派生算法版本；
- dividend tax mode。

任何一项变化都必须产生新 run ID。旧第 3 周运行包保留为历史工程证据，但在
`outputs/backtests/index.json` 中标记：

```text
status = superseded_semantic_bug
reasons = [
  missing_corporate_actions,
  ex_right_reference_price,
  fixed_one_lot_baseline
]
```

不得删除或覆盖旧包。

`index.json` 使用与运行包相同的临时目录 + 原子替换发布，不移动旧目录。索引只增加状态和
原因，不修改旧包内任何文件，因此旧 SHA-256 仍可复核。新运行包与旧包并存；报告默认只列
`active`，但可显式查看 `superseded_semantic_bug`。

## 6. 分阶段验收

### Gate A：公司行为与价格语义

- 合成现金分红例子证明 ex-date 权益不断裂、payable date 才增加现金；
- 原价成交/估值、指标价算 SMA、参考价算涨跌停分别有失败测试；
- 除权日不能再读取上一根原始 close 直接计算涨跌停；
- 非现金公司行为、缺失快照、伪造/修改快照均拒绝；
- 会计恒等式逐日成立；
- 全量旧测试无回归。

### Gate B：目标仓位

- 10 元、100 元、1,000 元三种价格下初始实际暴露接近同一 target_weight；
- 所有买单为 100 股整数倍且费用后现金不为负；
- Buy & Hold 与 SMA 使用同一仓位算法；
- CLI 和 run.json 不再把固定 100 股称为正式基准。

### Gate C：固定四标的真实数据

- 每个标的公司行为事件数、总现金分红和日期范围可复算；
- Buy & Hold 与 SMA(20) 各一包，共 8 个新运行包；
- 所有包含 8 个核心文件（原 6 文件加 corporate_actions/receivables）及完整 SHA-256；
- Claude Code 对源码、测试和 8 个包复核，门槛 `P0=0、P1=0`。

### Gate D：10 标的试点

- 10 个标的数据、日历、公司行为和 universe 全部验真；
- 每个标的运行 Buy & Hold 与 SMA(20)，共 20 个独立运行包；
- 明确输出失败标的和原因，不用另一只股票静默替换；
- 不汇总成“组合收益”；
- Gate D 通过后才开始第 4 周风险指标。

## 7. 明显风险和处理

| 风险 | 处理 |
|---|---|
| 免费接口历史记录被上游改写 | 快照内容寻址、SHA-256、provider/schema 固定；旧运行始终绑定旧快照 |
| ETF 两张表日期或金额对不上 | 拒绝发布该 symbol 的公司行为快照 |
| 差异化分红或特殊除权公式 | v0.1 拒绝，不用普通现金公式硬算 |
| 红利税依赖持有期和投资者身份 | v0.1 使用税前金额并在每份报告醒目标注，不冒充税后净收益 |
| 目标仓位在涨停附近受开盘跳空影响 | 在真实开盘价按费用和整手重新求最大可成交数量 |
| 10 标的仍有幸存者偏差 | 仅称工程试点；历史股票池留到后续增强 |
| 单标的批量被误解为组合 | CLI、run.json 和报告统一写 `independent_single_instrument_batch` |

## 8. 推进顺序

1. Claude Code 只读审核本设计；
2. 修复其确认的 P0/P1 设计问题；
3. 写两份独立实施计划：正确性修复、10 标的试点；
4. 在隔离 worktree 中按测试驱动执行正确性修复；
5. 固定四标的真实数据门；
6. 再执行 universe 解耦和 10 标的试点；
7. Claude 代码级复核；
8. 进入第 4 周风险指标和可复现报告。

## 9. 起始日前事件

正式策略不接收初始持仓，第一笔持仓只能由数据区间内的订单产生。因此起始日前的公司行为不会
改变起始持仓成本或数量。公司行为快照仍保留完整来源记录，但派生 `indicator_close` 在第一根
bar 将累计因子初始化为 1，只在区间内 ex_date 当天起应用事件。若未来支持初始持仓，必须另行
设计起始成本、历史应收股息和起始前复权因子，不能复用当前假设。
