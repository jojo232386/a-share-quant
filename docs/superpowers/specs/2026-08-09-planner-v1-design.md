# 滚动目标权重规划器（planner-v1）设计冻结候选

日期：2026-08-09
状态：DESIGN ONLY — DESIGN FREEZE CANDIDATE，未批准，等待独立复核
基线：`origin/main` = `43759c64aa247bcbb9b48b0bdcbc676739d7a81f`（含已合并 PR #9 / A1 Signal contract）
设计分支：`feat/target-weights-planner-v1`

本文是对 Opus Planner v1 Architecture Review 的修订版。本轮只修改本文档，不创建
production code，不修改 A1、v0.2 coordinator、portfolio schema 或 Gate E/replay 审计资产。

本文中的“冻结”只表示**提交给独立 reviewer 的候选契约文字**，不表示 Codex 已批准 Design
Freeze。只有后续独立复核可以给出批准或退回结论。

---

## A. Codex 对 Opus P0/P1 的独立裁决

裁决词义：

- `ACCEPT`：finding 与建议解法均有仓库证据支持，候选版采用；
- `MODIFY`：finding 成立，但 Opus 建议需要收窄或改解法；
- `REJECT`：finding 或建议与已冻结仓库契约冲突，不进入候选版。

这些裁决只决定**候选文档采用什么语义**，不是对 Design Freeze 的批准。

### A.1 P0-1：weight 分母

先比较三个候选语义，再裁决：

| 候选 | 语义 | 结果 |
|---|---|---|
| A. v0.2 `initial_cash` 分母 | `0.6` 长期对应初始本金的 60% 固定名义金额 | **REJECT**：这是 frozen v0.2 单次 allocation 的语义。滚动期 NAV 变化后，它不再表达 60% 目标权重，carry-forward 也会把比例目标错误变成固定 notional |
| B. plan 应用时 total equity / NAV 分母 | `0.6` 始终表示应用 plan 时组合 NAV 的 60% | **SELECT**：保持 target-weight 的比例含义，允许 carry-forward 维持同一目标比例，同时把 valuation 和交易决定留给 execution integration |
| C. planner 规划时读取实时/实际 NAV | planner 自己读账本、选择价格和 timestamp 后计算比例 | **REJECT**：会破坏 pure planner，并把 planned 与 feasible/realized 层耦合 |

Codex 裁决：**ACCEPT P0-1**。Opus 指出的分母歧义成立；在独立比较后采用候选 B，而不是因为
reviewer 提出便默认接受。正式候选语义见 §3。

### A.2 P1 逐项裁决

| Finding | Codex 裁决 | 独立判断与采用方案 |
|---|---|---|
| P1-1 首期 `None` 歧义 | **ACCEPT** | `None` 无法区分首次、reset、丢失、路径错误或恢复失败。采用必传 `NoPreviousState(FIRST_PERIOD/EXPLICIT_RESET)`；任何隐式缺失 fail closed |
| P1-2 previous 时序 | **ACCEPT** | Planner v1 是 daily-only contract，date 没有 timestamp/sequence 可区分同日先后。采用严格 `previous.as_of < as_of`；同日或未来 previous 均 fail closed，同日 re-plan 留给新契约 |
| P1-3 planned-state owner/schema | **ACCEPT** | pure planner 不应 I/O，但滚动状态必须有 owner 和独立 schema。采用 orchestration owner + `PLANNER_SCHEMA_VERSION`；拒绝复用 portfolio schema |
| P1-4 Signal/sizing ownership | **MODIFY** | finding 成立，但明确拒绝 Opus 方案 (a)“正数仅是 ACTIVE marker、planner 覆盖权重”。采用方案 (b)：保留 A1 explicit Decimal target weights；长期 classification-only contract 版本化 deferred |
| P1-5 universe drift | **ACCEPT** | 静默 carry-forward ineligible symbol 会产生不可解释状态。采用 caller-supplied eligible set + `universe_mismatch` fail closed；拒绝自动删除/清仓/置 0，也拒绝 planner 反向依赖 universe 模块 |
| P1-6 Planner risk boundary | **MODIFY** | frozen route 确实要求风险检查，但仓库没有 `RiskState` 契约。采用必传、无状态、最小 `PlannerLimits`；拒绝发明状态型风险机，也拒绝完全没有 limits 输入 |
| P1-7 planner validator ownership | **ACCEPT** | A1 validator 看不到 previous/universe/limits。采用 planner-owned 独立 validation；只对齐错误码风格，不共享实现、不修改 A1 |
| P1-8 effective-state validation | **ACCEPT** | current output 单独合法不能证明 merge 后 gross 合法。采用对完整 effective state 重验；拒绝只验 current output |
| P1-9 planned-state 真不可变性 | **ACCEPT** | `dataclass(frozen=True)` 只冻结属性赋值，不能阻止 caller-owned dict alias。采用构造时 validation + defensive copy + deterministic normalization + immutable representation/view |

本轮高严重度裁决结果：`ACCEPT = 8`，`MODIFY = 2`，`REJECT = 0`（P0/P1 finding 级）；
方案级明确拒绝包括 P0 候选 A/C、P1-4 方案 (a)、隐式 state recovery、自动 universe migration
和自造 `RiskState`。`REJECT = 0` 不表示未拒绝任何方案，而是没有整项驳回一个 P0/P1 finding。

---

## 0. 候选架构边界

以下方向由用户明确要求保留，并作为本候选的前提：

1. Planner v1 是 pure planner；
2. 不修改 frozen v0.2 portfolio/coordinator；
3. 不 bump `PORTFOLIO_SCHEMA_VERSION`；
4. 不实现 SELL、rebalance、T+1 或 T+0 execution；
5. `previous` 是上一期 planner planned state，不是 realized position；
6. planned / feasible / realized 三层模型保留，Planner v1 只拥有 planned 层。

三层边界如下：

| 层 | 含义 | v1 owner |
|---|---|---|
| planned | 策略在本期希望维持的完整目标权重状态 | Planner v1 |
| feasible | 在 NAV、价格、A 股制度、整手、流动性和可卖数量约束下可形成的执行意图 | future Execution / Rebalance Integration |
| realized | 实际委托、成交、现金与库存 | future execution/accounting integration |

`planned != feasible != realized` 是正常状态，不是 Planner v1 可以通过读取账本或偷偷调整权重
消除的异常。

### 0.1 仓库事实与兼容边界

- `src/aquant/portfolio/contracts.py` 的 `PORTFOLIO_SCHEMA_VERSION` 仍为 `"0.2.0"`；
- frozen v0.2 `PortfolioConfig.gross_target_weight` 是单次运行配置，
  `allocate_equal_targets()` 使用 `initial_cash_fen` 计算固定名义目标；
- frozen coordinator 只有 BUY 路径，没有 SELL/rebalance 状态机；
- A1 `Signal` 的 public `compute()` contract、`SIGNAL_REGISTRY`、`SmaSignal` 和
  `TopKMomentumSignal` 已封板；
- `0.95` 存在于多处 legacy、配置、测试、文档、release/replay 和审计副本，不是“三处”。

因此，v0.2 的 `initial_cash_fen × gross_target_weight` 语义不得外推为长期滚动 planner
权重语义。Planner v1 与 v0.2 coordinator 平级；未来 integration 可以复用已冻结的会计和
A 股规则原语，但不得假装 v0.2 已经实现滚动执行。

### 0.2 风控路线核对

冻结路线明确要求每日回放在“生成次日模拟候选”之后“执行风险检查”；仓库已有现金不得为负、
long-only 和 gross 不得高于 1 的结构边界，也有独立的事后风险报告 limits。仓库中没有
`RiskState` / `RiskLimits` 的 planner 输入契约。

裁决：Planner v1 接受一个最小、显式、无状态的 `PlannerLimits`；不发明 `RiskState`，不读取
回撤、实际持仓、成交或账户状态。状态型风险检查留给 future paper-trading / execution layer。

---

## 1. Scope

Planner v1 只负责：

1. 接收当前 `as_of`、A1 显式 Signal output、显式 previous-state 输入、当前 eligible symbols
   和 `PlannerLimits`；
2. 校验 current output、previous planned state、universe 和时间关系；
3. 按 key-preserving override/carry-forward 规则形成完整 effective planned state；
4. 对完整 effective state 重新执行结构不变量与 limits 校验；
5. 返回新的 `PlannedTargets`，无副作用。

本设计同时冻结 planner-local `SignalSpec` 装配契约和独立 planned-state serialization
contract，但本轮不实现它们。

## 2. Non-goals

- 不计算 NAV，不读取现金或实际持仓；
- 不把 weight 转换成 notional fen、股数/份额或订单；
- 不选择估值价格或 valuation timestamp；
- 不做 normalization、equal-weight、risk-parity 或其他 sizing；
- 不做 SELL、rebalance、T+1、T+0、涨跌停、停牌、整手、滑点、手续费或成交判断；
- 不处理 realized inventory reconciliation；
- 不修改 `src/aquant/research/signals.py`、`Signal` Protocol 或 `SIGNAL_REGISTRY`；
- 不修改 `src/aquant/portfolio/`、coordinator、`PORTFOLIO_SCHEMA_VERSION` 或 Gate E/replay
  审计冻结资产；
- 不实现持久化系统、CLI、paper-trading 或 production wiring；
- 不开始 implementation，不开 PR，不合并。

---

## 3. `weight` 的正式语义

`PlannedTargets.targets[symbol]` 中的 `Decimal weight` 正式定义为：

> **目标资产价值占应用该 plan 时组合总权益 / NAV 的比例。**

例如，`Decimal("0.6")` 表示：未来执行层应用该 plan 时，该资产的目标价值为当时组合
总权益 / NAV 的 60%。它不表示初始本金的 60%，也不冻结一个跨期不变的名义金额。

carry-forward `Decimal("0.6")` 表示 planned target ratio 继续保持 60%。它**不意味着“不交易”**：
价格、现金、公司行动或实际成交造成的漂移，可能使 future Execution / Rebalance Integration
需要交易，也可能因 policy/tolerance 不交易。Planner v1 不作这个判断。

Planner v1 不拥有以下量：

- plan 应用时的 NAV valuation snapshot；
- valuation timestamp；
- weight → notional fen；
- price selection 与 lot rounding；
- rebalance tolerance/policy。

这些量必须由 future Execution / Rebalance Integration 在独立契约中冻结。文中任何 v0.2
`initial_cash_fen` 示例只说明 frozen v0.2 的单次运行行为，不定义 Planner v1 权重。

---

## 4. Public data contract

以下是设计层候选类型；名字与字段在进入 implementation 前不得被静默改义。

```python
PLANNER_SCHEMA_VERSION = "1.0.0"


class NoPreviousStateReason(StrEnum):
    FIRST_PERIOD = "first_period"
    EXPLICIT_RESET = "explicit_reset"


@dataclass(frozen=True)
class NoPreviousState:
    reason: NoPreviousStateReason


@dataclass(frozen=True)
class PreviousTargets:
    as_of: date
    targets: Mapping[str, Decimal]


@dataclass(frozen=True)
class PlannedTargets:
    as_of: date
    targets: Mapping[str, Decimal]


@dataclass(frozen=True)
class PlannerLimits:
    max_single_weight: Decimal = Decimal("1")
    max_gross: Decimal = Decimal("1")
    min_cash_ratio: Decimal = Decimal("0")
```

`PlannedTargets` 与 `PreviousTargets` 字段形状相同但类型不同，防止把 current Signal output
或 realized positions 当成已验证的 previous planned state。

### 4.1 真不可变 value-object 契约

`@dataclass(frozen=True)` 只禁止字段重新赋值，不足以冻结字段指向的 mutable mapping。
`PreviousTargets` 与 `PlannedTargets` 的构造边界必须执行以下步骤：

1. 对 caller 提供的 mapping 完成 planner-owned key/value/invariant validation；
2. 将校验后的 `(symbol, Decimal weight)` 按 symbol 排序并复制到 planner-owned storage；
3. 只保存不可变表示，例如 `tuple[tuple[str, Decimal], ...]`，或保存
   `MappingProxyType(dict(caller_mapping))` 的**新 dict defensive copy**；
4. public `targets` 只暴露只读 view/immutable mapping，不暴露任何内部 mutable dict；
5. 禁止直接 `MappingProxyType(caller_mapping)`，因为 proxy 会继续观察 caller 后续修改；
6. 禁止把 caller 的 mapping 原样存入 frozen dataclass；
7. 不把 float/int/bool 自动转换成 Decimal。“normalize”只统一顺序/容器表示，不改变已验证的
   symbol 或 Decimal 数值语义。

因此，下列行为必须成立：

- 构造 state 后修改、清空或删除原始 dict 的 key，不会改变 state；
- 对原始 dict 中既有 symbol 重新赋值，不会改变 state；
- 通过 `state.targets[...] = ...` 修改会失败；
- 相同内容、不同 caller mapping 顺序会得到相等且确定性一致的 state；
- `PreviousTargets`、`PlannedTargets` 和 serialization loader 重建的 state 遵守同一规则。

允许的内部实现包括 sorted tuple、基于新 defensive copy 的 `MappingProxyType` 或语义等价的
持久不可变 mapping；本设计不锁定具体容器，只锁定不可 alias、不可变和确定性。

`PlannerLimits()` 的默认字段值保留当前 long-only / unlevered 行为；`limits` 在函数签名中仍是
必传参数，调用方必须显式传入 `PlannerLimits()` 或更严格的已冻结配置，不能依赖隐藏默认值。

### 4.2 Public function/API 候选签名

Planner core 直接消费 A1 已产生的显式 output，不在 core 内重新运行 Signal：

```python
def plan_targets(
    *,
    as_of: date,
    signal_output: Mapping[str, Decimal],
    previous: PreviousTargets | NoPreviousState,
    eligible_symbols: frozenset[str],
    limits: PlannerLimits,
) -> PlannedTargets:
    ...
```

约束：

- 所有参数均 keyword-only；
- `previous` 无缺省值；省略会由 Python 签名拒绝，显式传 `None` 由 planner 以
  `invalid_previous_state` fail closed；
- `eligible_symbols` 是调用方提供的 current eligible universe 快照，不是 planner 自己读取
  `aquant.universe`；
- core 是确定性纯函数，不写文件、不连网络、不读 wall clock、不读账本；
- A1 `SignalError` 在 Signal 计算阶段结束，planner 对收到的 mapping 再做一套独立校验，
  不调用 `validate_signal_output()` 作为 planner validator。

Signal 的配置装配是同包中的独立边界，不混入 merge core：

```python
def build_signal(
    *,
    name: str,
    config: Mapping[str, object],
    eligible_symbols: frozenset[str],
) -> Signal:
    ...
```

`build_signal()` 从 `SIGNAL_SPECS` 选择 builder，并在 assembly 阶段按 cardinality 检查场景；
它不通过试跑 `compute()` 或捕获 `SignalError` 探测能力。

---

## 5. 首期状态与 previous 时序

### 5.1 显式首期哨兵

`previous: PreviousTargets | None` 被禁止。调用方必须主动构造：

- `NoPreviousState(FIRST_PERIOD)`：状态 epoch 的真正首次运行；
- `NoPreviousState(EXPLICIT_RESET)`：经显式授权开始一个新 state epoch。

两者在 merge 时都提供空 previous targets，但审计语义不同。`EXPLICIT_RESET` 不是普通运行的
容错分支；orchestration 必须记录操作者/原因，并为持久化 envelope 生成新的 `epoch_id`。

以下情形不得转换为 `FIRST_PERIOD`：

- 状态文件不存在；
- 路径错误或权限错误；
- 读取、解析、schema 校验或恢复失败；
- 调用方省略参数或传入 `None`。

这些情形全部 fail closed。只有调用方明确知道自己正在启动首期或执行已授权 reset 时，才能
构造 `NoPreviousState`。

### 5.2 previous 时间关系

冻结以下不变量：

```text
previous.as_of < current as_of
```

`previous.as_of >= as_of` 以 `non_ascending_previous_state` fail closed。equal-date 与 future
previous 都非法；调用方不得用输入顺序、文件 mtime 或执行先后来弥补 date 缺少 sequence 的
问题。

Planner v1 正式仍是 **daily plan contract**：只接受严格 `date`，不接受 `datetime` 或
`Timestamp`，也不实现日内多次 re-plan。同日 re-plan / T+0 必须通过未来带 intraday
timestamp 与显式 sequence/order 的新版本契约实现，不能复用 Planner v1 的 date-only state。

---

## 6. Effective planned state 与 key semantics

状态转移只有一条：

```text
previous planned state
  + current explicit Signal output
  -> new effective planned state
```

逐 key 规则：

1. current output 中存在 key：current `Decimal` 值覆盖 previous；
2. current output 中该 key 的值为 `Decimal("0")`：保留 key，并形成显式 FLAT planned target；
3. current output 缺少 key、previous 有 key：previous 值原样 carry-forward，包括显式 0；
4. current 和 previous 都从未包含 key：effective 中不凭空创建该 key。

因此 key 存在性本身具有状态意义：

- `symbol -> Decimal("0")` 是显式 FLAT；
- key 缺失是 NO_DECISION/从未进入该 state epoch 的记录，不是 FLAT；
- 在连续 state epoch 内，历史出现过的 key 不得因普通 NO_DECISION 静默消失；
- 只有显式 reset 开启新 epoch，或 future universe migration 契约成功处理后，历史 key 集合才
  可以改变。

合并后必须对**完整 effective state**重新执行 §8 校验。只验证 current explicit output 不足以
保证安全，例如 previous `0.6` + current 新 symbol `0.6` 的两路输入可分别合法，但 effective
gross 为 `1.2`，必须失败。

Planner v1 不把 planned key 与 realized inventory 对账。future Execution Integration 必须将：

> realized inventory 存在，但 planned state key 缺失

冻结为 reconciliation error，不得静默继续持仓，也不得把缺失 key 擅自解释为 0。

---

## 7. Universe drift 与 unknown symbol

Planner 不依赖 `aquant.universe`。调用方必须把当前 eligible symbols 作为不可变、已验证的
`frozenset[str]` 显式传入。

唯一规则如下，全文不再保留另一套 unknown-symbol 解释：

1. `eligible_symbols` 必须是合法、非空、无重复的 symbol 集合；
2. current Signal output 的每个 key 必须属于 `eligible_symbols`；
3. `PreviousTargets.targets` 的每个 key（包括值为 0 的 key）必须属于
   `eligible_symbols`；
4. 任一差集非空都以 `universe_mismatch` fail closed，并报告 source 与差集；
5. planner 不自动删除、清仓、置 0 或迁移任何 symbol。

尤其是 previous planned state 中存在当期已不 eligible 的 symbol 时，即使 current output
省略它或显式给 0，也不能借本次 merge 偷偷完成 migration。orchestration / future Execution
Integration 必须先执行独立、显式、可审计的 universe migration/reconciliation 契约，再开始
新的合法 planner state epoch。

---

## 8. Planner-owned validation 与 `PlannerLimits`

Planner 自己实现 `signal_output`、`PreviousTargets` 和 effective `PlannedTargets` 校验。
实现可以对齐 A1 `SignalError.code` 的命名风格，但不得 import/call A1
`validate_signal_output()` 作为 planner validator，也不得把 planner 的 carry-forward、universe、
limits 或 previous-state 规则塞回 A1。

### 8.1 结构不变量

以下规则不可由配置放宽：

- symbol 是非空合法 `str`；
- weight 的 exact runtime type 是 `Decimal`，不是 float/bool/int；
- weight 必须 finite；
- `weight >= 0`，不允许 short；
- 单标的 `weight <= 1`；
- long-only 下 gross = `sum(weights)`；
- hard gross ceiling = `1`，不允许 leverage；
- 求和在固定高精度 local Decimal context 中完成；
- current、previous 和 effective 都必须逐层校验，不能信任类型标注或上游对象名。
- `PreviousTargets` 与 `PlannedTargets` 构造时必须 defensive copy/normalize，不能 alias
  caller-owned mutable mapping；构造后的 public targets 必须只读且内容稳定。

### 8.2 可配置 risk limits

`PlannerLimits` 只表达 planned 层的无状态上限：

| 字段 | 合法范围 | effective-state 规则 | 默认值 |
|---|---|---|---|
| `max_single_weight` | finite Decimal，`0 < x <= 1` | 每个 `weight <= x` | `1` |
| `max_gross` | finite Decimal，`0 < x <= 1` | `gross <= x` | `1` |
| `min_cash_ratio` | finite Decimal，`0 <= x < 1` | `gross <= 1 - x` | `0` |

`min_cash_ratio` 只表示 planned cash residual，不声称账户实际现金一定达到该比例。
`max_gross` 与 `min_cash_ratio` 是两个独立上限；两者同时适用时取更严格者。若多项同时违反，
固定校验顺序为：字段/结构 → hard gross → max single → max gross → min cash，以保证确定错误码。

不引入 `RiskState`，不根据 realized exposure、drawdown、pending orders 或 sellable inventory
改变计划。这些数据不属于 pure planner。

---

## 9. Signal explicit weight 与 sizing ownership

Planner v1 冻结 A1 已有数值语义：

- Signal 显式输出的正 `Decimal weight` 原值成为对应 symbol 的新 planned target weight；
- omitted symbol carry-forward previous planned target；
- `Decimal("0")` 成为显式 FLAT planned target；
- Planner v1 不归一化、不等权重化、不覆盖、不把正值降级成 ACTIVE marker。

这对 `TopKMomentumSignal` 尤其关键：它的正数输出承载多标的 target-weight 信息。若把所有
`weight > 0` 解释成无权重 ACTIVE marker，会静默破坏已冻结的 A1 Signal output contract。

`SmaSignal.active_weight = Decimal("0.95")` 暂时保留为 A1 legacy compatibility sizing debt。
本轮不统一仓库中的多处 `0.95`，尤其不改 Gate E/replay/audit copies。

准确的 ownership 表述是：

> **Planner v1 不新增 Signal 内部 sizing；它消费 A1 已冻结的 explicit target weights。
> 长期 sizing ownership 重构 deferred。**

未来若采用 classification-only Signal + planner-owned sizing，必须通过独立、版本化的 Signal
contract/capability upgrade 实现，不能在 Planner v1 内静默重新解释数值。

---

## 10. Planner-local `SignalSpec`

不修改已封板 `src/aquant/research/signals.py`，不给 `Signal` Protocol 增加 cardinality，也不
维护独立的 constructor table 与 capability table。

Planner package 只维护一个 spec 真相源：

```python
class SignalCardinality(StrEnum):
    SINGLE_SYMBOL = "single_symbol"
    MULTI_SYMBOL = "multi_symbol"


@dataclass(frozen=True)
class SignalSpec:
    name: str
    builder: Callable[[Mapping[str, object]], Signal]
    cardinality: SignalCardinality


SIGNAL_SPECS: Mapping[str, SignalSpec] = {
    "sma": SignalSpec(
        name="sma",
        builder=build_sma_signal,
        cardinality=SignalCardinality.SINGLE_SYMBOL,
    ),
    "top_k_momentum": SignalSpec(
        name="top_k_momentum",
        builder=build_top_k_momentum_signal,
        cardinality=SignalCardinality.MULTI_SYMBOL,
    ),
}
```

每个 builder 显式解析自己已知的参数并构造 A1 class；不假设
`SIGNAL_REGISTRY[name](**config)` 有统一签名。construction + capability 必须同处一个
`SignalSpec`，不得拆成两个手工 mapping。

冻结以下契约测试：

```python
assert set(SIGNAL_SPECS) == set(SIGNAL_REGISTRY)
assert all(key == spec.name for key, spec in SIGNAL_SPECS.items())
```

并逐项断言 builder 构造出的实例类型与 `SIGNAL_REGISTRY[name]` 一致。assembly 阶段读取
`spec.cardinality`：`SINGLE_SYMBOL` 要求 exactly one eligible symbol；`MULTI_SYMBOL` 允许
one-or-more eligible symbols。能力不通过调用 `compute()` 后捕获 `SignalError` 探测。

`TopKMomentumSignal` 仍只是 A1 contract demonstration。出现在 planner 研究装配表不等于获准
进入 paper/live execution；future execution integration 必须另设 production eligibility gate。

---

## 11. Error model

所有 planner 错误继承 `PlannerError(ValueError)`，公开稳定 `code` 与不含敏感数据的 message；
可用子类区分 configuration、runtime invariant 和 state serialization，但消费者以稳定 code
做机器判断。

### 11.1 Planner core / assembly codes

| code | 分类 | 触发条件 |
|---|---|---|
| `invalid_as_of` | runtime invariant | `as_of` 不是 exact `date` |
| `invalid_previous_state` | runtime invariant | `previous` 缺省、为 `None` 或类型错误 |
| `non_ascending_previous_state` | runtime invariant | `previous.as_of >= as_of`；同日与未来 previous 都非法 |
| `invalid_eligible_symbols` | configuration | eligible set 类型、symbol 或空集合非法 |
| `universe_mismatch` | runtime invariant | current/previous key 不属于 eligible symbols |
| `invalid_output_type` | runtime invariant | Signal output 不是 Mapping |
| `invalid_symbol` | runtime invariant | target key 不是合法非空 string |
| `non_decimal_weight` | runtime invariant | weight 不是 exact Decimal |
| `non_finite_weight` | runtime invariant | NaN/Infinity |
| `negative_weight` | runtime invariant | weight < 0 |
| `weight_above_one` | runtime invariant | 单 symbol weight > 1 |
| `hard_gross_ceiling_exceeded` | runtime invariant | effective gross > 1 |
| `max_single_weight_exceeded` | risk limit | effective 单标的超过 limit |
| `max_gross_exceeded` | risk limit | effective gross 超过 configured max |
| `min_cash_ratio_violated` | risk limit | planned residual cash 低于 configured min |
| `invalid_limits` | configuration | `PlannerLimits` 类型、Decimal 或范围非法 |
| `unknown_signal_spec` | configuration | assembly 名称不在 `SIGNAL_SPECS` |
| `invalid_signal_config` | configuration | per-Signal builder 参数非法 |
| `unsupported_cardinality` | configuration | spec 与 eligible-symbol 场景不匹配 |
| `signal_spec_registry_mismatch` | configuration | spec 与 frozen A1 registry 不一致 |
| `planner_invariant_violation` | implementation bug | 理论不可达的内部不一致；不得吞掉或降级 |

A1 Signal 计算本身失败时仍由 A1 `SignalError` 报告；planner 不将其伪装成首期、NO_DECISION
或空 output。planner 接到成功返回的 mapping 后仍独立复验。

### 11.2 State loading/serialization codes

以下错误由 orchestration/state adapter 抛出，不由 pure planner 做 I/O：

| code | 触发条件 |
|---|---|
| `planner_state_missing` | 应有历史但文件/对象不存在 |
| `planner_state_corrupt` | JSON、字段、日期、Decimal 或 invariant 校验失败 |
| `planner_schema_unsupported` | schema 缺失或不是受支持的 `PLANNER_SCHEMA_VERSION` |
| `planner_state_epoch_mismatch` | 加载记录与预期 state epoch 不一致 |

这些错误不得自动回退到 `NoPreviousState(FIRST_PERIOD)`。

---

## 12. Planned-state ownership 与 serialization schema

Planner 保持无状态纯函数。跨期 planned state 的 owner 是：

> **orchestration / future paper-trading integration layer**

owner 负责加载、校验、将 state 传给 planner、在成功规划后原子持久化、恢复和显式 reset。
本轮只冻结格式，不实现文件系统、数据库或锁。

独立 planner state envelope：

```json
{
  "planner_schema_version": "1.0.0",
  "epoch_id": "caller-owned-non-empty-id",
  "epoch_start_reason": "first_period",
  "as_of": "2026-08-09",
  "targets": {
    "000001": "0.6",
    "510300": "0"
  }
}
```

冻结规则：

- 使用独立 `PLANNER_SCHEMA_VERSION`；绝不复用或 bump `PORTFOLIO_SCHEMA_VERSION`；
- `epoch_id` 由 orchestration 生成并在连续 epoch 内保持稳定；explicit reset 必须生成新 ID；
- `epoch_start_reason` 只能是 `first_period` 或 `explicit_reset`，在 epoch 内保持不变；
- `as_of` 使用 ISO date；
- Decimal 使用字符串，禁止 JSON number/float；canonical text 使用普通十进制定点形式、去除
  无意义尾零，零统一写 `"0"`，禁止指数形式；
- `targets` 按 symbol 排序序列化；显式 0 key 必须保留；
- 未知字段、缺字段、重复 key、非法 Decimal、非法 symbol、错误 schema 或 universe mismatch
  都 fail closed；
- loader 必须通过 §4.1 的 defensive immutable constructor 重建并复验 `PreviousTargets`，
  不能把“能 parse JSON”当成 state 合法，也不能保留 decoded mutable dict alias；
- 丢失/损坏/恢复失败不等于 first period；
- state 写入原子性、锁、备份、hash/manifest 与 storage backend 属于未来实现设计。

---

## 13. Determinism 与 daily contract

- 相同 `(as_of, signal_output, previous, eligible_symbols, limits)` 必须得到结构相同的
  `PlannedTargets`；
- validation 和求和使用固定 local Decimal context，不受调用方 global context 影响；
- merge 与 serialization 按 symbol 排序，不依赖 Mapping/set 迭代顺序；
- state 构造时 defensive copy/normalize；caller 后续修改输入 mapping 不得改变 state、merge
  结果或 serialized bytes；
- 不使用 wall clock、随机数、网络或实际账本；
- planner 的 `as_of` 是日级 `date`，不含日内 timestamp；
- `previous.as_of < as_of`；同日多次 re-plan 不属于 v1；
- serialized bytes 的确定性由 §12 canonical rules 保证，不能用 Decimal 原始展示形式或插入
  顺序制造不同 state bytes。

---

## 14. Deferred Execution / Rebalance Integration 强制议题

下一阶段必须独立设计并冻结以下内容，不能从本设计中默认推断：

1. plan 应用时的 total-equity / NAV valuation snapshot 与 provenance；
2. valuation timestamp、行情频率、可用性与 price selection；
3. weight → target notional fen；
4. fees、lot rounding、零股/整手和 rounding residual；
5. rebalance policy、tolerance、minimum trade 与“carry-forward 是否触发交易”；
6. planned → feasible 的 A 股涨跌停、停牌、T+1 sellable inventory、流动性与容量判断；
7. SELL/清仓、order sequencing、partial fills、rejections、retry/cancel；
8. feasible → realized 的 fills、cash、inventory 与账本 reconciliation；
9. realized inventory 存在但 planned key 缺失时的 hard reconciliation error；
10. universe removal/migration 的显式策略，禁止 planner 自动删除/清仓/置 0；
11. planned/feasible/realized 偏离的监控、告警和停止条件；
12. stateful `RiskState`（若未来需要）的数据来源、时序与 ownership；
13. execution/paper state 的独立 schema/version，不得借用 planner 或 portfolio schema。
14. same-day multiple plans 的 timestamp、sequence/order、幂等和冲突处理；不得用 date-only
    `PreviousTargets` 表达日内顺序。

现有 `PositionLot.available_date` / `sellable_size` 是未来 T+0 capability 的良好扩展点，但不代表
“T+0 只需改 available_date”。真正的 T+0 还需要 intraday timestamp、same-day multiple
plans、order sequencing、fills、inventory reconciliation、market-data granularity 和
instrument capability 的独立设计。Planner v1 保持 daily plan contract。

---

## 15. Implementation acceptance matrix（本轮不实现）

未来 implementation 至少需要以下测试，当前只冻结验收要求：

### 15.1 Public contract / sentinel

- `previous` 省略与 `None` 均 fail closed；
- `FIRST_PERIOD` 与 `EXPLICIT_RESET` 均需显式构造，且 serialization provenance 不同；
- missing/corrupt state 不会退化成 first period；
- equal-date 与 future previous 都以 `non_ascending_previous_state` fail closed；
- `datetime`/`Timestamp` 被拒绝。

### 15.2 Merge / key semantics

- current 正值覆盖 previous，且 Decimal 数值完全不变；
- current 显式 0 覆盖并保留 key；
- current omit 对 previous 正值/0 都 carry-forward；
- 连续多期 NO_DECISION 不丢失历史 key；
- first period + empty current output 返回合法空 planned state；
- previous `0.6` + current 新 symbol `0.6` 在 effective validation 失败。

### 15.3 Immutable state ownership

- 用 mutable dict 分别构造 `PreviousTargets` 与 `PlannedTargets`，随后修改、删除、清空原 dict，
  state 的 keys/weights/equality/serialization 均不变；
- 构造后对原 dict 的既有 symbol 重新赋值，state 不变；
- 对 `state.targets` 做 item assignment/deletion 失败；
- 两个插入顺序不同但内容相同的 mapping 产生相等、排序一致的 state；
- implementation 若使用 `MappingProxyType`，测试必须证明 proxy 包装的是新 defensive copy，
  不是 caller-owned dict；
- loader 从 decoded mutable mapping 构造 state 后，修改 decoded mapping 不影响 state；
- explicit zero key 在 defensive copy/normalize 后仍保留，Decimal 不被 coercion 或量化。

### 15.4 Universe / validation / limits

- current unknown symbol 和 previous universe drift 均以 `universe_mismatch` 失败；
- previous 的 ineligible 显式 0 也不能静默删除；
- planner validation 不 import/call A1 `validate_signal_output()`；
- Decimal type、finite、nonnegative、single <= 1、hard gross <= 1 全部覆盖边界；
- `PlannerLimits()` 默认 long-only/unlevered，三项收紧限制分别生效；
- invalid limits 在 assembly/configuration 阶段失败；
- 不同 global Decimal context 下结果相同。

### 15.5 SignalSpec / compatibility

- `set(SIGNAL_SPECS) == set(SIGNAL_REGISTRY)`；
- key == `spec.name`，builder 结果类型与 registry 一致；
- Sma exactly-one 与 TopK one-or-more cardinality 在 assembly 阶段检查；
- capability 不通过捕获 `SignalError` 探测；
- A1 `signals.py`、A1 existing tests、v0.2 portfolio/coordinator 和 Gate E audit assets 无 diff；
- planner state 使用 `PLANNER_SCHEMA_VERSION`，v0.2 仍使用 `PORTFOLIO_SCHEMA_VERSION`。

### 15.6 Serialization

- explicit zero round-trip 后 key 仍存在；
- Decimal 不经过 float；
- sorted/canonical output 在不同输入 mapping 顺序下逐字节相同；
- unsupported schema、unknown/missing field、duplicate key、bad epoch、bad date、bad Decimal 和
  universe mismatch 全部 fail closed；
- reset 产生新 epoch，普通 carry-forward 保持 epoch。

---

## 16. Backward compatibility 与修改白名单

本设计未来实现仍受以下白名单约束；本轮实际只修改本文档：

- 可新增独立 `src/aquant/planner/` 和对应 tests（需下一轮明确授权）；
- 不修改 A1 `src/aquant/research/signals.py`；
- 不修改 `Signal` Protocol、`SIGNAL_REGISTRY` 或 A1 explicit output 数值语义；
- 不修改 frozen `src/aquant/portfolio/coordinator.py` 或任何 v0.2 portfolio public contract；
- 不修改 `PORTFOLIO_SCHEMA_VERSION`；
- 不修改 frozen Gate E/replay configs、trust manifests、audit copies 或 release evidence；
- 不因本设计统一任何 legacy `0.95`；
- 不开始 execution、SELL、rebalance、T+1 或 T+0。

---

## 17. Opus P0/P1/P2 candidate-closure matrix

本表只记录 Planner v1 Architecture Review；已封板 A1 的历史 P3 不计入本轮 finding count。
`CLOSED` 只表示 Codex 自审认为候选文字已覆盖该 finding，不表示 Design Freeze 已获批准。

| Finding | P0/P1 裁决 | 候选状态 | 关闭证据 |
|---|---|---|---|
| P0-1 weight 被错误绑定 initial cash | **ACCEPT** | **CLOSED** | §A.1 比较三种分母后，§3 采用 plan 应用时 NAV 比例，并把 NAV/valuation/fen/rounding/rebalance 移交 future integration |
| P1-1 首期 `None` 歧义 | **ACCEPT** | **CLOSED** | §4–5 使用必传 `NoPreviousState(FIRST_PERIOD/EXPLICIT_RESET)`，缺失/恢复失败不降级 |
| P1-2 previous 时间关系 | **ACCEPT** | **CLOSED** | §5.2 采用严格 `previous.as_of < as_of`；equal/future previous fail closed，同日 re-plan deferred |
| P1-3 planned-state owner/schema 缺失 | **ACCEPT** | **CLOSED** | §12 采用 orchestration owner、独立 `PLANNER_SCHEMA_VERSION` 与 envelope |
| P1-4 Signal weight/sizing 被 planner 覆盖 | **MODIFY** | **CLOSED** | §9 拒绝 marker 方案 (a)，保留 A1 explicit target-weight 数值，不归一化 |
| P1-5 universe drift 静默 carry-forward | **ACCEPT** | **CLOSED** | §7 对 previous/current 统一 `universe_mismatch` fail closed，不删、不清仓、不置 0 |
| P1-6 风控边界不清 | **MODIFY** | **CLOSED** | §0.2/§8 采用最小 `PlannerLimits`，拒绝无仓库契约的 `RiskState` |
| P1-7 planner 复用 A1 validator | **ACCEPT** | **CLOSED** | §8 采用 planner-owned validation，不调用 A1 validator |
| P1-8 只校验 current output | **ACCEPT** | **CLOSED** | §6/§8 对 merge 后完整 effective state 重验 exposure/limits |
| P1-9 shallow-frozen state 可被 caller dict 修改 | **ACCEPT** | **CLOSED** | §4.1/§15.3 要求 defensive copy、deterministic normalization、immutable view 与 alias regression tests |
| P2-1 explicit zero / missing key 矛盾 | n/a | **CLOSED** | §6 使用 key-preserving state machine，0 与 omit 永不合并 |
| P2-2 Signal construction/cardinality 双表或探测 | n/a | **CLOSED** | §10 单一 `SignalSpec` 真相源 + registry key-set contract + assembly check |
| P2-3 `0.95` 数量事实错误 | n/a | **CLOSED** | §0.1/§9 改为多处 legacy/audit copies，明确不触碰冻结资产 |
| P2-4 T+0 过度承诺 | n/a | **CLOSED** | §14 列出完整 intraday/execution/reconciliation 议题，v1 保持 daily |
| P2-5 unknown symbol/A1 validator/文档内在矛盾 | n/a | **CLOSED** | §7 单一 universe 规则，§8 独立 validator，§6 单一 key 语义 |

### 17.1 Self-review result

- placeholder scan：未发现占位符或未决二选一；
- internal consistency：weight、zero/omit、universe、strict previous chronology、immutable state、
  schema 与 error code 使用单一语义；
- scope check：所有 execution/reconciliation/T+0 内容均为 deferred contract，不含实现；
- compatibility check：A1、coordinator、portfolio schema 与 Gate E/replay 均在修改禁区；
- open findings：P0 = 0，P1 = 0，P2 = 0，P3 = 0。

## 18. Candidate readiness declaration

最终 Planner v1 public contract、候选 API、错误模型、state ownership/schema、SignalSpec 和
deferred Execution Integration 强制议题均已在 §3–§14 形成候选文字。以下计数是 Codex
自审后的**待独立复核计数**，不是 Freeze approval：

**P0 = 0 / P1 = 0 / P2 = 0 / P3 = 0**

**DESIGN FREEZE CANDIDATE READY FOR INDEPENDENT REVIEW**

STOP：禁止开始 implementation，禁止修改 coordinator，禁止修改 A1，禁止开始 T+0。
