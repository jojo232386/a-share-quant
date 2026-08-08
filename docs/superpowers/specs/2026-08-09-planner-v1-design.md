# 滚动目标权重规划器（planner-v1）设计冻结候选

日期：2026-08-09
状态：DESIGN ONLY — 未实现、未合并，等待独立架构复核
基线：`origin/main` = `43759c64aa247bcbb9b48b0bdcbc676739d7a81f`（含已合并 PR #9 / A1 signal contract，HEAD `918a537`）
隔离现场：`.worktrees/target-weights-planner-v1`，分支 `feat/target-weights-planner-v1`，从上述 `origin/main` 新建，working tree clean（0 untracked）

对话中曾用代号 "A1"（signal contract，已封板）/"A2"（本设计）。仓库自身的分期词汇是
**Gate A–E**（v0.2 共享现金组合，已全部冻结），与 "A1/A2" 不是同一件事，两套代号不要混用。
本文档正文一律使用仓库既有词汇或 `planner-v1`，不使用 "A2"。

---

## 0. 前提事实核对（本轮独立验证，非转述交接）

在设计任何契约之前，先确认下游能不能接住。以下均为本轮在 `origin/main` 上直接读代码得到的
事实，不是假设：

1. **`src/aquant/portfolio/` 已经是一台冻结的组合引擎**（Gate A–E 全部通过），
   `contracts.py` 里 `PORTFOLIO_SCHEMA_VERSION = "0.2.0"`。
2. **`PortfolioConfig` 是单次运行结构**：一个 `signal_date` + 一个 `end_date`，
   `gross_target_weight: Decimal` 是全组合一个标量，通过 `allocate_equal_targets` 按
   `1/N` 等额切给每个 symbol（`models.py:105-126`）。没有"第二个信号日"的概念。
3. **`coordinator.py`（966 行）只有 BUY，没有 SELL。** 全文 grep `side=` 只命中两处，
   都是 `OrderSide.BUY`（`coordinator.py:780,835`）。`sellable_size` 只用于估值时计算
   T+1 可卖数量，不触发下单。
4. **未成交目标会过期作废，不会重试或转移预算**：`_record_rejection` 在
   `attempts_used == max_attempts` 时把状态置为 `EXPIRED_UNFILLED`（`coordinator.py:437`），
   之后该 symbol 的目标永久终止，`docs/superpowers/specs/2026-07-27-shared-cash-portfolio-design.md`
   §2.2 也明确写着"不把拒单标的预算重新分配给其他标的"。
5. **现金不能为负**：`_valid_fen(ledger.cash_fen)` 默认 `positive=False` 即只要求
   `>= 0`（`accounting.py:18-22`），且没有任何 margin/borrow/short 原语存在于
   `models.py` / `accounting.py` / `coordinator.py`。杠杆和做空不是"策略上不做"，
   而是**当前账本结构做不到**——这是结构事实，不是本设计新引入的限制。
6. **`target_weights` 这个标识符已经被占用**，含义是"单次运行的静态派生量"：
   `metrics.py:378`、`verify.py:2181`、`gate_e/audit.py:1000` 都用
   `Decimal(target_notional_fen) / Decimal(initial_cash_fen)` 反算一次性目标权重，
   用于审计偏离度，不是输入契约。
7. **仓库里零现存的"再平衡/滚动/多期"先例**（grep `rebalanc|rolling|multi_period` 只命中
   `signals.py` 里一句 docstring："never ... rebalances portfolios"）。

**结论（0 号问题的答案）：**

planner-v1 与已冻结的 v0.2 coordinator **不是扩展关系，是并列关系**：

> **planner-v1 只负责"给定当期 Signal 输出 + 上一期已生效目标，产出并验证下一期完整的
> effective target weights"。它不调用、不修改、不扩展 `src/aquant/portfolio/coordinator.py`，
> 也不触碰 `PORTFOLIO_SCHEMA_VERSION`。把 effective target weights 转成委托、处理 T+1/
> 涨跌停/整手/重试/退出，是后续阶段（暂称 planner-execution，不在本设计范围）的职责，
> 依赖但不修改 v0.2 冻结引擎的会计/规则原语。**

理由：

- 唯一不需要动 Gate A–E 冻结产物或重开审计的选项；
- 与 A1 `SmaSignal.compute()` docstring 里"portfolio-level multi-symbol sizing 属于后续
  planner 层"的既有措辞一致；
- v0.2 coordinator 是单期 buy-and-hold，若要塞入滚动多期语义，等价于重写 966 行文件的核心
  循环，那是一个新版本（0.3.0），不是本轮范围；
- 把执行（下单/T+1/拒单重试/卖出）单独留一个阶段，能让 planner-v1 保持"纯函数：输入目标
  历史 + 信号 → 输出下一期目标"，可独立单测，不依赖账本状态机。

这条结论是本设计冻结的地基。如果复核者不同意，后面所有契约都要重新推导，请优先在这一条
上给反馈。

---

## 1. Scope

- 定义 `PlannedTargets`：单期滚动目标权重的正式数据契约（输入/输出，不含执行）。
- 定义 NO_DECISION 与上一期已生效目标的合并语义（carry-forward）。
- 定义合并后 effective target 的验证规则（fail closed）。
- 定义 Signal 的 cardinality/capability 最小声明方式（供 planner-v1 在装配期读取，
  不依赖运行时捕获异常）。
- 定义 registry → constructor 参数的配置层职责边界。
- 定义 date 归一化边界（谁负责把 `datetime`/`Timestamp` 转成 `date`）。
- 定义 failure 语义分类（configuration error vs runtime data error）。
- 定义确定性、测试矩阵、验收标准、STOP 条件。

## 2. Non-goals（本轮明确不做）

- **不实现执行层**：不下单、不处理 T+1、不处理涨跌停拒单重试、不处理整手取整、不产生
  `EntryTarget`/`EntryAttempt`。这些已经在 v0.2 coordinator 里用另一套模型冻结了，
  执行层如何消费 `PlannedTargets` 是下一份独立设计的范围。
- **不实现卖出/清仓的具体委托逻辑**。本设计只规定 FLAT 必须在 effective target 里显式
  体现为 `Decimal("0")`，不规定谁、何时、如何把它变成一笔 SELL 委托。
- **不修改 `src/aquant/research/signals.py`**，不修改 `SmaSignal` 的 single-symbol 限制。
- **不修改 `src/aquant/portfolio/` 任何文件**，不改 `PORTFOLIO_SCHEMA_VERSION`。
- **不引入杠杆或做空**：账本现金不能为负是结构事实（见 §0.5），本设计不新增任何突破它的
  路径，`gross exposure` 硬上限为 1。
- **不对 `SignalInput` 做性能重构**（dict 索引 / session bisect 留给未来，不在本轮）。
- **不产出实现代码、不开 PR、不合并**。

## 3. Data model

新增一个独立模块（命名建议，不在本轮创建）：`src/aquant/planner/`，与 `research/`、
`portfolio/` 平级，不从属于任何一个。原因：它同时消费 `research.signals` 的输出和（未来）
被 `portfolio` 执行层消费，放进任何一边都会造成反向依赖。

核心类型（设计层面描述字段和约束，不写实现）：

```
PlannedTargets                       # planner 输出契约，见 §4
  as_of: date
  targets: Mapping[str, Decimal]     # symbol -> effective weight, 0 <= w <= 1

PreviousTargets                      # carry-forward 的输入，见 §5
  as_of: date
  targets: Mapping[str, Decimal]     # 上一期 PlannedTargets.targets 原样传入

PlannerError(ValueError)             # 参照 SignalError 的 code+message 模式
  code: str
```

`PlannedTargets` 和 `PreviousTargets` 在字段形状上相同，但类型不同——防止调用方把"本期
Signal 原始输出"误当成"上一期已验证的 effective target"直接传回。

## 4. `PlannedTargets` 契约（问题 1）

| 维度 | 决定 | 理由 |
|---|---|---|
| key 类型 | `str`，非空，复用 A1 `SignalInput` 的 symbol 校验规则 | 与 Signal 层保持同一套 symbol 合法性定义，不重新发明 |
| value 类型 | 精确 `Decimal`，非 float | 与 A1 `validate_signal_output` 一致；float 在 §0.6 的
  `metrics.py` 派生场景里已经证明会引入舍入分歧 |
| 精度 | 不做小数位数限制，只要求 `is_finite()` | 与 A1 `_require_decimal_weight` 一致；具体舍入到 fen
  是**执行层**的职责，不在 planner 输出契约里 |
| 允许范围 | `0 <= weight <= 1`（单标的） | 与 A1 一致；上限 1 呼应账本无法为负、`gross_target_weight`
  硬上限 1（`models.py:55`） |
| 空 mapping 语义 | **合法**，代表"本期没有任何标的有 effective 仓位"（例如全部 symbol 都是
  NO_DECISION 且历史 previous 也是空） | 与 A1 `validate_signal_output({}, ...)` 语义一致，不能拒绝空组合 |
| symbol 不存在时行为 | `PlannedTargets` 本身不做"symbol 是否存在于 universe"校验——这是
  上游 `VerifiedUniverse` 的职责。planner 只保证内部一致（key 出现在 targets 里必须有合法
  weight），不做跨模块 universe membership 检查 | 避免 planner 反向依赖
  `aquant.universe`，保持职责单一 |
| 是否允许显式 0 | **允许，且与"缺省"含义不同**：`Decimal("0")` = 显式 FLAT（保留在
  targets 里的 key，值为 0）；key 完全不出现 = "本 symbol 从 effective target 集合里移除"
  （见 §5，只在 previous 从未持有且本期也未被提及时发生） | 直接复用 A1 冻结的三态编码，
  不重新解释（不变量 F） |
| 部分 symbol 缺省 | 允许。`PlannedTargets.targets` 不要求覆盖 universe 全集，只覆盖当前
  effective 非零或显式 FLAT 的 symbol；对 "从未被任何 Signal 提及过" 的 symbol，视为一直
  没有仓位，不需要用 0 占位 | 避免每期都要枚举全 universe，输出体积和 universe 规模解耦 |
| fail-closed 错误语义 | 构造时校验：非 Decimal → `invalid_weight_type`；负数 →
  `negative_weight`；`> 1` → `weight_above_one`；`sum > 1` → `total_weight_above_one`；
  key 非法 → `invalid_symbol` | 错误码风格与 A1 `SignalError.code` 对齐，便于下游统一处理 |

`PlannedTargets` **不**在构造时检查"是不是刚从 carry-forward 合并出来的"——它是一个通用
的"一组已验证的、和为 ≤1 的权重"容器，构造校验和 A1 的 `validate_signal_output` 几乎同构，
可以复用同一段校验逻辑（设计层面建议复用，不建议复制粘贴）。

## 5. Previous-target / NO_DECISION 合并语义（问题 2）

### 5.1 "previous" 指什么——本设计的关键决定

`previous` 必须是**上一期 planner 输出的 effective `PlannedTargets`**（即上一次合并后
通过验证的目标），**不是执行层实际达成的持仓**。

理由（这是本设计与原始交接指令分歧最大、必须让复核者明确表态的一条）：

- 执行层受 T+1、涨跌停拒单、整手取整、5 次重试上限等约束（v0.2 `coordinator.py` 已冻结），
  `planned target ≠ realized position` 是常态而非异常；
- 如果 planner 拿"实际持仓"当 previous，NO_DECISION 的语义会被污染成"如果昨天没买成，
  今天也不算持有"——这等于让**执行层的随机失败**改写 A1 冻结的"omit = 保留先前目标态"
  语义，直接违反不变量 F；
- 用"上一期 planner 目标"当 previous，是一个**执行层无法感知的纯函数链**：
  `targets[t] = merge(signal_output[t], targets[t-1])`，可独立于账本状态单测，
  确定性可完全由输入序列复现。

**代价（必须写明，不能藏起来）**：这意味着 `PlannedTargets` 描述的是"策略意图"，不是
"账户实际敞口"。执行层如果连续多期无法成交，`PlannedTargets` 和真实持仓会持续偏离。
这个偏离度的监控/告警属于执行层设计范围，本设计只负责在文档里显式声明这个已知落差，
不在 planner-v1 内部尝试弥合它（弥合需要读账本状态，就违反了"纯函数、不依赖执行状态"
的设计目标）。

### 5.2 合并规则

对 universe 中每个 symbol，三种情况：

1. **本期 Signal 输出中出现该 symbol**（无论值是 0 还是 >0）→ effective = 本期值，
   丢弃 previous 值（显式覆盖）。
2. **本期 Signal 输出未提及该 symbol，但 previous 中存在**（值 >0 或 =0）→
   effective = previous 值原样保留（这就是 NO_DECISION 的实际效果）。
3. **本期未提及，previous 中也不存在**（symbol 从未被持有/提及过）→
   effective 中不出现该 key（等价于两次都是"从未涉及"，不产生 0 占位）。

多个 Signal 同时参与同一期的情况（例如未来多策略混合）**不在本设计范围**——合并规则只处理
"单一 Signal 输出 + 单一 previous"两路输入。如果未来需要多 Signal 混合，需要新的设计，
不能假设本规则自动推广。

### 5.3 与 A1 不变量的关系

这条合并规则**不修改** A1 冻结的三态编码（不变量 F）：`validate_signal_output` 仍然只
校验"本次显式 Signal 输出"，合并发生在 planner 层，在 Signal 返回之后、effective 验证
之前，`SmaSignal`/`TopKMomentumSignal` 本身完全不感知 carry-forward 的存在。

## 6. Effective portfolio validation（问题 3）

合并后的 effective `PlannedTargets` 必须重新过一次独立校验，规则如下：

| 项 | 规则 | 依据 |
|---|---|---|
| 单 symbol target 范围 | `0 <= weight <= 1`，与 §4 一致 | 复用 §4 |
| total / gross target | `sum(weights) <= 1`，硬上限，**在合并之后重新求和**，不能假设
  "本期输出已经 ≤1 所以合并后也 ≤1"——这正是原始交接指令里第 2 条要强调的坑：
  `validate_signal_output(current_output)` 通过不代表 effective 通过 | 呼应
  `PortfolioConfig.gross_target_weight` 上限为 1（`models.py:55`），是仓库既有的硬约束，
  不是本设计新发明的 |
| 现金残余 | **允许，且是默认状态**。`sum(weights) < 1` 时的差额视为现金；这与 v0.2
  `allocate_equal_targets` 的 `planned_cash_reserve_fen` 完全同构（§0 事实 5） | 与冻结引擎的
  既有约定保持一致 |
| leverage | **不允许**。`sum(weights) > 1` fail closed，不提供任何配置开关放宽这条 | 现金
  不能为负是账本结构事实（§0.5），允许 leverage 会产出一个执行层永远无法满足的目标，等于
  制造不可达状态 |
| short | **不允许**。`weight < 0` fail closed，不存在负权重语义 | 同上，账本无
  short position 原语 |

**关键澄清（呼应问题 3 的隐含风险）**：这一层校验只保证"目标本身在数学上是可行的资金
分配"，**不保证目标在 T+1/涨跌停/整手约束下可执行**。"目标可行"与"目标可达"是两个不同
的验证层，本设计只做前者；后者属于执行层，执行层拒单/延迟是正常业务结果，不是 planner
的校验失败。

## 7. Signal 与 sizing 的职责边界（问题 4）

- `SmaSignal.active_weight` 保持不动，继续作为**唯一一处**历史兼容产物存在，不删除、
  不迁移、不给它加"是否要弃用"的时间表（那是另一个决定，不在本设计范围）。
- **新增 Signal 一律不得携带 sizing 参数**（不允许出现第二个 `active_weight` 式字段）。
  新 Signal 只输出三态分类（ACTIVE 用什么正数表示，由 planner 层的配置决定，不由 Signal
  自己决定具体数值）。
- Sizing/normalization/allocation 的唯一归属是 planner 层（本设计）+（未来）执行层，
  不做二次下放。
- `0.95` 目前有三处独立副本（`SmaSignal.active_weight` 默认值、`BacktestConfig.target_weight`
  默认值、`shared-cash-portfolio-design.md` §2.1 的 `gross_target_weight` 建议值）。
  **本设计不合并这三处**——它们分属三个已冻结或独立演进的模块（signal 兼容层 / 单标的
  backtest 配置 / v0.2 组合配置），强行合并会牵连到已封板的 A1 和已冻结的 Gate A-E。
  记录为已知重复，留待未来一次显式的"统一 sizing 默认值"变更单独处理，不在本轮动。

## 8. Signal capability / cardinality 契约（问题 5）

最小设计，不做成通用能力协商框架：

```
class SignalCardinality(StrEnum):
    SINGLE_SYMBOL = "single_symbol"
    MULTI_SYMBOL = "multi_symbol"
```

在 `Signal` Protocol 上新增一个**只读属性**（不是方法，不需要运行时状态）：

```
cardinality: SignalCardinality
```

`SmaSignal.cardinality` 恒为 `SINGLE_SYMBOL`，`TopKMomentumSignal.cardinality` 恒为
`MULTI_SYMBOL`。装配期（config 解析、registry 实例化之后）直接读这个属性做校验，
不再需要"调用一次 `compute()` 抓 `SignalError` 猜能力"。

**明确排除的做法**：不引入通用 capability 位图、不引入版本协商、不引入"这个 Signal 支持
哪些 as_of 频率"之类的扩展点——原始交接指令里"不要过度工程化"这条是对的，一个二值枚举
足够覆盖当前唯一存在的两个实现。若未来出现第三种 cardinality（比如"固定 symbol 集合"），
到时候再扩枚举，不提前设计。

这一条**不修改 `src/aquant/research/signals.py`**——属性的添加是对 A1 已封板文件的改动，
需要单独走一次对 A1 的变更评审（哪怕只是加一个只读属性），不能在 planner-v1 的名义下顺手
改掉。本设计只冻结**这个属性应该长什么样**，落地时机和是否需要重新触发 A1 复核，留给
实现前的单独决定。

## 9. Config / registry 构造模型（问题 6）

`SIGNAL_REGISTRY: Mapping[str, type[Signal]]` 保持不变（不修改 A1 文件）。

新增（在 planner-v1 模块内，不在 `research/signals.py` 内）一层**配置到构造参数**的映射，
形状上是显式的、每个 Signal 名字对应一个已知参数 schema，例如（示意，不是最终字段名）：

```
SIGNAL_CONSTRUCTOR_CONFIG = {
    "sma": {"period": int, "active_weight": Decimal},           # active_weight 可选
    "top_k_momentum": {"lookback": int, "k": int},
}
```

**不假设** `SIGNAL_REGISTRY[name](**config)` 能统一调用——因为字段名和类型因 Signal
而异，config 层必须显式知道每个 Signal 需要什么参数，用一个显式的 per-name 构造函数
或参数校验层去调用，而不是盲目 kwargs 展开（`type[Signal]` 不是 `Callable[[], Signal]`
这件事在 A1 终审里已经验证过：两个 registry 条目都无法无参构造）。

具体是"每个 Signal 名字配一个小 builder 函数"还是"每个 Signal 名字配一个 TypedDict + 统一
kwargs 展开"，是实现阶段的选择，本设计只冻结**不能假设统一签名**这一条硬约束。

## 10. Planner 职责边界

Planner-v1 的输入输出边界（纯函数视角）：

```
输入:
  - as_of: date
  - signal: Signal（已装配好参数的实例）
  - signal_input: SignalInput（当期因果历史）
  - previous: PreviousTargets | None（None 表示第一期，无历史）

输出:
  - PlannedTargets（已通过 effective validation）

副作用: 无。不写文件、不连网络、不读账本、不依赖 wall clock。
```

Planner-v1 **不**负责：

- 决定用哪个 Signal（那是更上层的策略选择/编排，不在本设计范围）；
- 把 `PlannedTargets` 落盘或转换成执行指令（执行层范围）；
- 维护跨期状态（`previous` 必须由调用方显式传入，planner 本身无状态、不缓存历史）。

## 11. Date normalization 边界（问题 7）

**归一化必须在数据适配层完成，在 `SignalInput` 构造之前**，也在 planner 接收 `as_of`
参数之前。planner-v1 和 A1 `SignalInput` 一样，只接受严格的 `datetime.date`
（`type(value) is date`），拒绝 `datetime.datetime`（含 pandas `Timestamp`，它是
`datetime` 子类）。

不在 planner 内部做"宽容转换"（例如自动 `.date()`）——原因：A1 已经验证过"严格拒绝"是
故意的 fail-closed 设计（终审报告里的约束 4），planner 层如果自己悄悄放宽，等于在下游
重新引入 A1 特意堵死的口子，且会造成"同一个 `datetime` 值在 Signal 层报错、在 planner
层不报错"的不一致体验。数据适配层（未来读行情/日历的那一层）是唯一允许做
`.date()` 转换的地方。

## 12. Failure / error model（问题 8）

| 场景 | 分类 | 说明 |
|---|---|---|
| unknown symbol（`PlannedTargets` 里出现不在装配期已知 symbol 集合中的 key） | runtime data error | 数据问题，运行时才能发现 |
| invalid target（weight 类型/范围非法） | configuration error 或 runtime data error，视来源 | 若来自 Signal 输出 → runtime data error（Signal 计算出的问题）；若来自静态配置的默认参数 → configuration error |
| total exposure violation（合并后 sum > 1） | runtime data error | 只有在有具体 previous + 具体 signal 输出时才能算出来，不能在装配期发现 |
| malformed Signal output（非 Mapping、非 Decimal 值等） | runtime data error | 复用 A1 `SignalError` 的对应错误码，不重新发明 |
| unsupported capability/cardinality（配置了一个 Signal 用于不匹配的场景，例如把
  single-symbol Signal 配置到多标的场景） | **configuration error**，且必须在装配期
  （§8 的 `cardinality` 属性读取时）发现，不允许运行到 `compute()` 才报错 | 这是引入
  cardinality 属性的核心目的：把原本的 runtime error 提前成 configuration error |
| missing previous target state（`previous=None` 但调用方以为有历史）| configuration error（调用方编排错误，不是数据问题）|
| invalid date（非 `date` 类型，或晚于 `signal_input.as_of`）| runtime data error，复用 A1 对应错误码 |
| impossible planner state（例如 merge 逻辑本身产生的内部不一致，理论上不应发生）| 视为
  configuration/implementation bug，fail closed 并携带足够上下文（symbol、as_of、
  previous/current 快照）用于事后排查，**不吞异常、不静默降级、不跳过校验继续跑** |

所有错误统一继承同一个 `PlannedTargetsError`/`PlannerError` 基类（参照 A1
`SignalError` 的 `code + message` 模式），不新造一套异常层级风格。

## 13. Determinism 要求

- 相同的 `(as_of, signal, signal_input, previous)` 输入必须产出逐字节相同的
  `PlannedTargets`（Decimal 值完全一致，不允许因浮点或 Decimal context 差异漂移）；
- 求和校验必须像 A1 `validate_signal_output` 一样在固定精度的 `localcontext` 内进行，
  不依赖调用方的全局 Decimal context；
- 不引入任何 wall clock、随机数、集合遍历顺序依赖（`Mapping` 的迭代顺序不能影响输出，
  合并逻辑按 symbol 排序处理）。

## 14. Performance 约束

- 本轮不优化。`SignalInput` 当前的线性历史过滤（A1 终审实测 300 symbols/500 bars
  ≈ 4.4ms/次）已知会在全市场规模变慢，记录为已知项，留给未来一次不改变 public Signal
  contract 的内部优化（dict 索引 + session bisect）。
- Planner-v1 自身的合并逻辑是 `O(symbols)`，不会成为瓶颈来源；如果未来 universe
  规模显著增长，重新评估的应该是 `SignalInput`，不是 planner 的合并算法。

## 15. Test matrix（设计阶段的清单，非本轮要写的代码）

- **契约测试**：`PlannedTargets` 构造校验（类型/范围/求和/空 mapping/显式 0 vs 缺省），
  镜像 A1 `test_signals.py` 的 A 段结构。
- **合并语义测试**：
  - 本期覆盖 previous（三种覆盖组合：previous 有/无 × 本期 ACTIVE/FLAT/未提及）；
  - 连续多期 NO_DECISION 保持不变；
  - previous 中从未提及的 symbol 保持不出现（不产生幽灵 0）。
- **Effective validation 测试**：
  - 合并后 sum 恰好为 1 边界；
  - 合并后 sum 略超 1（例如 previous 遗留 0.6 + 本期新增 ACTIVE 0.6）必须 fail closed，
    且必须是这里新增的校验路径失败，不是复用 A1 `validate_signal_output` 的路径（因为
    A1 那层根本看不到 previous）；
  - 现金残余合法（sum < 1）不报错。
- **Cardinality 契约测试**：`SmaSignal.cardinality == SINGLE_SYMBOL`、
  `TopKMomentumSignal.cardinality == MULTI_SYMBOL`，装配期误配组合必须在实例化/装配阶段
  报 configuration error，不依赖跑一次 `compute()` 才发现。
- **Date 边界测试**：`datetime.datetime`/`Timestamp` 输入到 planner 层同样被拒绝，
  错误码与 A1 一致。
- **确定性测试**：相同输入重复调用两次结果相等；不同 Decimal 全局 context 下结果不变
  （镜像 A1 `test_top_k_deterministic_under_changed_caller_decimal_context`）。
- **不改变 A1 的回归测试**：本设计落地后，A1 现有 74 个测试（`test_signals.py` +
  `test_backtest_baselines.py`）必须继续全部通过，且 62,136 点 oracle 等价性测试不受
  影响——因为 planner-v1 不修改 `signals.py`。

## 16. Backward compatibility 要求

- `SIGNAL_REGISTRY`、`Signal` Protocol 的 `compute()` 签名、`SignalInput`、
  `validate_signal_output` **全部保持不变**（若 §8 的 cardinality 属性最终决定要加，
  是对 A1 的一次独立、显式标注的小变更，不在本设计的落地范围内隐式捆绑）。
- v0.2 `src/aquant/portfolio/` 全部文件、`PORTFOLIO_SCHEMA_VERSION`、Gate A-E 审计产物
  **不变**。
- `metrics.py` / `verify.py` / `gate_e/audit.py` 里已有的 `target_weights` 局部变量语义
  不变，不与本设计的 `PlannedTargets` 混用同一个名字（见 §0.6，已选择 `PlannedTargets`
  作为区分度更高的名字）。

## 17. A1 不变量：本设计必须遵守、不得重新解释

（对照原始交接第五节 A–F，逐条确认本设计是否遵守）

| 不变量 | 本设计是否遵守 |
|---|---|
| A. NO_DECISION carry-forward 之后必须验证完整 effective portfolio | 遵守，见 §6 |
| B. `validate_signal_output()` 只验证当前 explicit Signal output，不是 leverage 保证 | 遵守，§6 显式指出这正是原交接第 2 条要强调的坑，effective 校验是本设计新增的、独立的一层 |
| C. `SmaSignal` 永远保持 single-symbol compatibility semantics | 遵守，§2 明确不修改 signals.py |
| D. portfolio-level multi-symbol sizing 属于 planner 层 | 遵守，本设计就是在实现这一层 |
| E. 不破坏 A1 已冻结的 SMA 分类等价性 | 遵守，不触碰 signals.py，62,136 点测试不受影响 |
| F. 不重新解释三态编码（omit/0/>0） | 遵守，§5.2 合并规则完全基于三态原始定义推导，未引入第四态 |

## 18. Acceptance criteria（设计冻结本身的验收，不是实现验收）

- [ ] 复核者认可 §0 的"planner-v1 不扩展 v0.2 coordinator"这一架构判断；
- [ ] 复核者认可 §5.1 "previous = 上一期 planner 目标而非实际持仓"这一关键决定；
- [ ] 复核者确认 §17 的六条不变量映射无遗漏、无曲解；
- [ ] 复核者对 §8 cardinality 属性是否需要现在就改 `signals.py`（还是留到实现阶段单独
      对 A1 发起一次小变更评审）给出明确意见；
- [ ] 无 P0/P1 级别的架构分歧。

## 19. STOP conditions

- 复核者不同意 §0 的架构判断 → 停止，重新讨论 planner 与 v0.2 coordinator 的关系，
  本设计其余部分可能需要整体重做；
- 复核者认为 §5.1 的 previous 定义有安全隐患 → 停止，不得直接改用"实际持仓"作为
  previous 后继续往下走，需要重新评估对不变量 F 的影响；
- 任何一条 §17 的不变量被判定为不遵守 → 停止，禁止进入实现阶段。

## 20. Open questions

1. §8 的 `cardinality` 属性最终是否要加进 `src/aquant/research/signals.py`，还是通过
   一个 planner-v1 侧的外部映射表（`{SmaSignal: SINGLE_SYMBOL, ...}`）实现，避免触碰
   A1 文件？两种方案都满足"装配期发现、不依赖运行时异常"的目标，取舍在于：改
   `signals.py` 更符合"能力是 Signal 自身属性"的直觉，但要求重新触发一次对已封板文件的
   变更评审；外部映射表零风险但需要手工与 registry 保持同步。**建议复核时二选一。**
2. `PlannedTargets` 与执行层之间的边界，本设计只画到"输出一个已验证的 Decimal 权重
   mapping"为止。执行层如何把 FLAT 变成 SELL 委托、如何处理"目标可行但不可达"的持续
   偏离，需要一份独立设计，本文档不预判其结构。
3. `0.95` 三处重复（§7）目前搁置不处理，是否需要单独立项统一，留给产品/架构决定。

## 21. 实现分解建议（仅在设计通过后生效，本轮不执行）

1. `PlannedTargets` / `PreviousTargets` / `PlannerError` 契约实现 + 契约测试（§15 第一块）。
2. 合并逻辑（§5.2）+ 合并语义测试。
3. Effective validation（§6）+ 边界测试。
4. Cardinality 属性方案落地（依赖 §20 问题 1 的复核结论）+ 装配期校验测试。
5. Registry constructor config 映射（§9）+ 误配测试。
6. 全量回归：确认 A1 74 个测试仍然通过，确认 v0.2 Gate A-E 相关测试不受影响（预期不受
   影响，因为本设计不改动其源文件，但仍需实际跑一遍确认）。

每一步都应该是独立可 review 的小改动，不在一个 PR 里全部塞完。
