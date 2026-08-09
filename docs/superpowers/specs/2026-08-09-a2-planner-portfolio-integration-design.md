# A2 Planner → Shared-Cash Rolling Portfolio 集成设计冻结

日期：2026-08-09
状态：正式重新冻结；上一轮不可达 T+1 residual 场景已由本设计替换
基线：`772c5d08141b25ebe8a32e24e09f5c4f3bd58e88`
实施分支：`feat/a2-planner-portfolio-integration`

本设计只冻结 Planner v1 到共享现金 rolling portfolio 的最小集成。它不修改 Planner、A1、
legacy v0.2 portfolio、Gate E 或历史审计对象。

---

## 1. 目标与风险裁决

A2 消费已经完成 carry-forward 的 `PlannedTargets`，在 A 股日线约束下把目标权重转换成下一
官方交易日的可执行 BUY/SELL，并把 desired、realized 和 residual gap 保持为可复算证据。

风险五元组：

```text
R-007 → P1 → A2 / Planner / Portfolio
→ effective targets、共享现金、gross、确定性多标的状态与 residual 测试
→ A2 工程可继续；组合结果作为研究证据前必须具备验收证据
```

重新冻结前的独立窄范围裁决为 `P0=0 / P1=0 / PASS_TO_REFREEZE`。实现和测试存在不等于
alpha、盈利、稳健性、可交易性或实盘就绪。

## 2. 非目标

- 不修改或重新实现 Planner carry-forward；
- 不修改 A1 Signal 或 `SIGNAL_REGISTRY`；
- 不改变 legacy coordinator 的固定名义、BUY-and-hold 语义；
- 不修改旧 `PortfolioLedger`、`CashLedgerEvent`、portfolio artifact、identity、metrics、
  export、verify 或 Gate E schema；
- 不引入 tolerance、rebalance band、优化器、预算重分配或 projected-risk 引擎；
- 不扩展日内 timestamp、同日多计划、部分成交市场模拟或 broker/live；
- 不为 rolling 结果增加历史 Gate E 兼容导出；
- 不新增 persistent retry/residual target 对象；
- 不新增 rolling artifact、identity hash、export 或 replay schema；
- 不新增第二套公司行为引擎。

## 3. 已冻结的估值与时序

```text
decision session = PlannedTargets.as_of = T
execution session = calendar.next_session(T) = T+1
valuation timestamp = T close
equity denominator = T DailyAccountSnapshot.equity_fen
target_notional_fen = Decimal(weight) * Decimal(equity_fen)
target_shares = floor_to_100(target_notional_fen / 100 / execution_open(T+1))
delta_shares = target_shares - realized_shares_at_T_close
```

`target_notional_fen` 保持精确 `Decimal`，不得先向下取整到整数分。唯一 sizing 取整发生在
股数空间：先向下取整到整数股，再 `// 100 * 100`。目标金额在 T 已确定，T+1 信息只能用于
金额到股数的换算。

`equity_fen` 包含 cash、position market value 和 receivables。不得使用 T+1 开盘权益或把
可用现金另造为分母。

### 3.1 首日 pristine fallback（D5）

只有以下条件全部成立时，才允许用 `initial_cash_fen` 代替快照权益：

```text
lots == ()
cash_events == ()
receivables == ()
daily_snapshots == ()
cash_fen == initial_cash_fen
```

同时必须能通过未修改的 legacy `verify_portfolio_ledger()`。只有“没有快照”或只有 rolling
verifier 通过都不足以证明 pristine。

`calendar.next_session(T) is None` 是日历覆盖末端错误；不得伪装成 residual 或 retry 成功。

## 4. Desired、Realized 与对账

- Desired 是传入的精确 `PlannedTargets.targets`；它已经是 Planner 完整有效状态。
- A2 不保存第二套 carry-forward、旧目标或 shadow residual target。
- `Decimal("0")` 是存在的显式 FLAT key；缺失 key 不是零。
- 任一实际非零持仓 symbol 不在 `PlannedTargets.targets` 中时立即 fail closed。
- 每次调用都从本期 effective plan 与本期实际账本重新计算 delta。
- 失败或部分执行后，result 明确保留 desired、realized 与 residual；账本未成交部分不消失。

下一期由新的 effective `PlannedTargets`、新的 T 收盘快照和新的 T+1 执行输入重新计算，不能
沿用旧 target notional 或用 realized state 改写 Planner state。

## 5. Refrozen acceptance 9 与 10

### 5.1 Acceptance 9：合法执行失败后的 residual retry

正式废弃“T 日收盘已有持仓在本次 T+1 仍因 T+1 锁定”的不可达场景。

验收使用生产可达的 `SUSPENDED_NO_BAR` 或 `PRICE_LIMIT_OPEN`：

1. T 日 effective plan 对持仓 symbol 给出显式 0；
2. T+1 执行失败，账本持仓不变；
3. result 显示 desired shares=0、realized shares>0、residual>0；
4. 后续 T 日 effective plan 仍为 0，下一合法执行日重新计算并清仓；
5. 禁止伪造 `available_date > T+1`，禁止扩展日内模型。

### 5.2 Acceptance 10：独立 T+1 原语

继续复用既有规则测试：买入当日不可卖；从 `next_session(acquired_date)` 起可卖；
`available_date == execution_date` 计入 `sellable_size`。A2 不复制第二套 T+1 测试体系，也不
修改既有业务语义。

## 6. 新增包与隔离边界（D6）

只新增：

```text
src/aquant/rolling/
├── __init__.py
├── accounting.py
└── orchestration.py
```

### 6.1 Rolling accounting

`RollingPortfolioLedger` 是 sibling schema，字段为：

```text
initial_cash_fen
cash_fen
lots
cash_events       # exact legacy BUY/dividend CashLedgerEvent | SellFillEvent
receivables
daily_snapshots
```

新增 `LotConsumption`、`SellPosting`、`SellFillEvent`，以及：

```text
create_rolling_ledger
promote_portfolio_ledger
post_rolling_buy
post_rolling_sell
close_rolling_session
verify_rolling_ledger
```

BUY 使用临时单事件 legacy ledger 调用现有 `post_buy()`，再把其精确 lot 和
`CashLedgerEvent` 追加到 rolling ledger。BUY fill 与 lot 的原始一一关系保持不变。

SELL 只在 rolling schema 中表达。它必须按 symbol 过滤 lots 后复用 `sellable_size`、
`validate_sell_size`、`consume_fifo`，并复用 `notional_fen` 与
`cash_after_fill(side=SELL)`。`SellFillEvent` 记录有序 per-lot consumption。

独立 verifier 从原始 BUY quantities 重放全部 BUY/SELL、FIFO、费用和共享现金，核对终态
`remaining_size`。零余额 lot 保留以支持重放。历史快照按事件日期重建仓位，避免使用最终
remaining size 污染过去快照；receivables 与权益恒等式继续独立复算。现有 receivables 和
snapshots 被携带和验证，但 A2 不创建第二套公司行为编排。

旧 `PortfolioLedger`、`CashLedgerEvent` 和 legacy verifier 均不放宽，rolling 对象也不得进入
旧 export、identity、verify、metrics 或 Gate E。

### 6.2 Rolling orchestration

公开 orchestration contract 精确冻结为：

```python
class RollingAttemptStatus(StrEnum):
    FILLED = "filled"
    REJECTED = "rejected"

@dataclass(frozen=True)
class RollingConfig:
    limits: PlannerLimits

@dataclass(frozen=True)
class RollingExecutionInput:
    symbol: str
    instrument_kind: InstrumentKind
    intent_session: date
    execution_session: date
    previous_close: Decimal | None
    execution_open: Decimal | None

@dataclass(frozen=True)
class RebalanceAttempt:
    attempt_id: str
    plan_as_of: date
    execution_session: date
    symbol: str
    side: OrderSide | None
    target_weight: Decimal
    target_notional_fen: Decimal
    target_shares: int | None
    realized_before: int
    requested_size: int
    feasible_size: int
    filled_size: int
    status: RollingAttemptStatus
    rejection_reason: RejectionReason | None
    fees: FeeBreakdown | None
    cash_before_fen: int
    cash_after_fen: int
    quantity_adjustment_reason: str | None

@dataclass(frozen=True)
class TargetRealization:
    symbol: str
    desired_weight: Decimal
    target_notional_fen: Decimal
    target_shares: int | None
    realized_shares: int
    residual_shares: int | None
    is_aligned: bool

@dataclass(frozen=True)
class RollingRebalanceResult:
    planned: PlannedTargets
    execution_session: date
    equity_fen: int
    attempts: tuple[RebalanceAttempt, ...]
    targets: tuple[TargetRealization, ...]
    ledger: RollingPortfolioLedger

def rebalance_to_plan(
    *,
    config: RollingConfig,
    planned: PlannedTargets,
    ledger: RollingPortfolioLedger,
    execution_inputs: tuple[RollingExecutionInput, ...],
    calendar: VerifiedTradingCalendar,
    fee_policy: VerifiedFeePolicy,
) -> RollingRebalanceResult: ...
```

`RollingConfig` 只包含精确 `PlannerLimits`。初始现金属于 ledger；日历和费用策略是经验证的
运行输入；计划日期属于 `PlannedTargets`，不得复用 `PortfolioConfig` 的单次运行字段。

每个 `RollingExecutionInput` 绑定 symbol、instrument kind、T、T+1 以及：

- 有 bar：精确 `previous_close` 与 `execution_open`；
- 无 bar：两者都为 `None`。

`RollingExecutionInput` 的两个价格必须同时为精确正 `Decimal`，或同时为 `None` 表示无 bar。
`intent_session` 必须等于 plan T，`execution_session` 必须等于精确 T+1。无 bar 且正权重时
`target_shares`/`residual_shares` 可为 `None`，`is_aligned=False`；显式 0 的 target shares 始终
可确定为 0。attempt 的 `quantity_adjustment_reason` 只允许
`insufficient_cash_including_fees`、`partial_sellable_position` 或 `None`。FILLED 要求正
`filled_size`、fees 非空、rejection 为空；REJECTED 要求 `filled_size=0`、fees 为空和明确
rejection。无动作 symbol 不生成 attempt。

公开入口只接受上述精确类型，返回不可变 attempts、per-symbol target realization、最终
rolling ledger 和 residual evidence。它先校验快照/pristine、交易日和价格日期绑定，再做
reconciliation 和 sizing。

## 7. 执行顺序与共享现金

1. 先推导全部 deltas；
2. 全部 SELL 按 symbol 升序；
3. 全部 BUY 按 symbol 升序；
4. 每笔成交立即更新同一个 `cash_fen`；
5. BUY 现金不足时包含费用每次减少 100 股，直到可执行或 0；
6. 不把未使用预算分配给其他 symbol。

SELL 可用库存小于请求量时，只尝试规则允许的可卖部分并留下 residual。无 bar 必须先通过
`check_bar_availability` 拒绝；有 bar 的订单继续由 `evaluate_order` 处理 T+1、整手、涨跌停、
费用和现金。

唯一组合侧防御性敞口检查是：使用同一个 T 权益分母，验证总 target notional/equity 不超过
`PlannerLimits.max_gross`。不得增加执行时 projected gross、优先级或二次优化模型。

## 8. 确定性与错误处理

- 输入先转成唯一、按 symbol 排序的 tuple；
- attempts 固定 SELL phase 后 BUY phase；同侧 symbol 升序；
- ID 只由 plan date、execution date、side 和 symbol 派生；
- 不使用 wall clock、随机数、`hash()`、进程到达顺序或 dict/set 迭代顺序；
- 所有状态不可变；失败不得改变输入 ledger；
- 日历、费用、价格、计划类型或对账契约错误 fail closed；
- execution failure 只能保留真实未成交 residual，不能标记目标已达成。

## 9. 验收证据

新增聚焦测试必须覆盖：

- rolling BUY 与 legacy BUY 事件等价；SELL cash/FIFO/费用/篡改检测；
- pristine fallback 的全部条件；T 收盘权益包含 receivable；
- Planner carry-forward 后完整 effective plan 的直接消费；
- 显式 0 与 missing-key reconciliation；
- 正权重、上调、下调、已对齐无交易；
- SELL-before-BUY、同侧 symbol 排序与真实共享现金；
- 含费的 100 股 BUY 下调和现金不为负；
- gross 防御性边界；
- no-bar/price-limit residual 与后续合法 session 重新计算收敛；
- calendar 末端错误；输入顺序和两个 `PYTHONHASHSEED` 下结果相同；
- Planner、A1、legacy portfolio、Gate E 回归与保护区零 diff。

R-007 只有在上述任务范围内证据具备并实际运行后，才可提交精确证据更新；不能宣称研究或
策略有效。

## 10. 修改白名单、STOP 与 self-review

实现白名单：新 `aquant.rolling` 包、新 rolling 聚焦测试、本设计与实施计划，以及证据齐备后
精确的 R-007 文档更新。

以下任一情况立即 STOP：基线漂移；必须修改 Planner/A1/legacy coordinator；必须改变旧 ledger
字段或 verifier；必须进入旧 Gate E artifact；必须扩展日内模型；必须伪造 lot；必须弱化旧
测试；出现新的 P0/P1 语义矛盾。

Self-review：本设计没有 placeholder；refrozen acceptance 9/10、D5、D6、calendar end、一次
share rounding、无 shadow target、最小 gross 检查和 frozen scope 均有唯一表述。设计状态为
`READY_FOR_TDD_IMPLEMENTATION`，不是发布或研究有效性结论。
