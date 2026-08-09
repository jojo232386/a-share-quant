# ASQ 风险治理与 Blocker/Deferred 登记册

本文件是 `CLAUDE.md` 中 blocker-vs-deferred 准则的执行载体，仅适用于 ASQ（本仓库）。GMAQ 使用同一套方法，但维护它自己的登记册，两者不得合并或互相引用结论。

## 1. 判定流程

对每个未解决项按顺序执行：

1. **Risk**：一句话描述问题本身，不描述解决方案。
2. **Severity**：P0 / P1 / P2 / P3（见下表）。
3. **Owner Stage**：由哪个阶段负责关闭它。
4. **Blocks at**：它在哪个阶段的**入口或结论处**变为 Blocker。
5. **Acceptance Evidence**：关闭它需要出示的可检验证据（测试、报告、清单、哈希、门禁结论）。
6. **当前判定**：相对**下一阶段**的 Blocker / Deferred。

判定问句：**不解决它，下一阶段结果是否不可信 / 不可恢复 / 不可控？** 是 → Blocker；否 → Deferred。

### Severity 定义

| 等级 | 含义 | 是否可覆盖 80/20 配比 |
|---|---|---|
| P0 | 资金安全、数据或 Git 历史不可恢复丢失、审计对象漂移 | 是，直接阻断 |
| P1 | 研究有效性受损（前视、幸存者、复权口径、执行口径不一致），或结论会被错误外推为已验证能力 | 是，在其 Blocks at 阶段直接阻断 |
| P2 | 工程质量、可维护性、可观测性缺口；不改变当前阶段结论的正确性 | 否 |
| P3 | 表述、文档、便利性改进 | 否 |

### 阶段相对性（关键约束）

**Severity 不等于 Blocker。P1 也不等于“立刻阻断”。** 判定始终相对下一阶段：

- **A2 / 工程实现阶段** 不得被只影响 Gate F 或 Live Readiness 的风险阻断。
- **Gate F / 研究有效性阶段** 不得被只在 Live Readiness 才成立的券商级细节阻断；Gate F 不需要生产级撮合模拟器。
- 关键债务必须在**它第一次使结果不可信 / 不可恢复 / 不可控的那个阶段**清除，不得更早（过度工程），也不得更晚（结论不安全）。

每条 P1 都必须写明它的 Blocks at 阶段；只写 Severity 而不写 Blocks at 的条目视为未完成登记。

### 阶段口径

| 阶段 | 含义 |
|---|---|
| A2 / Planner / Portfolio | 当前工程实现阶段，含组合规划器与共享现金账本 |
| Gate F | 研究有效性与数据有效性验证；产出可被称为“研究结论”的材料 |
| Paper Trading / Live Readiness | 纸面交易与实盘就绪；券商级真实性要求在此归属 |

## 2. 状态词

- **Open / Deferred**：已登记，指派到未来阶段，主线继续。
- **Blocker (active)**：当前阶段不得推进，直到关闭。
- **Blocker (armed)**：当前阶段不阻断，但在其 Blocks at 阶段自动转为 active，无需再次讨论。
  - 生命周期规则：进入该阶段时，Blocker (armed) 默认激活。只有当**当前的显式证据**表明该阶段不再依赖其底层风险时，才可标记为 Not Applicable；理由与证据必须回写登记册。
- **Closed**：已出示验收证据，附证据位置。

## 3. 登记册

初始条目从 `docs/known_limitations.md` 和 `HANDOFF.md` 的既有声明整理而来。这些条目是**风险登记**，不是新发现的缺陷；它们此前已在文档中声明，此处赋予 Severity、Owner Stage、Blocks at 与验收证据，以免在后续阶段中被遗忘。

| ID | Risk | Severity | Owner Stage | Blocks at | Acceptance Evidence | 当前判定 |
|---|---|---|---|---|---|---|
| R-001 | 10 个试点标的不是历史成分股，存在幸存者与选择偏差 | P1 | Gate F / 研究有效性验证 | Gate F 中任何声称策略有效性、稳健性、可迁移性或可交易性的结论 | 时点有效（point-in-time）的标的域构建，或显式界定边界的替代方案；以及与固定试点样本的结论差异对比，说明结论如何随标的域变化 | Deferred（A2 期间可保持；Blocker (armed) at Gate F） |
| R-002 | 上游（AKShare 等）可能回写历史；快照哈希只能固定本次输入 | P1 | Gate F / 数据有效性与时点正确性 | Gate F 中任何依赖历史数据稳定性或可追溯性的结论 | 多次时间点重取同一区间的差异报告；差异触发 fail-closed 的测试；快照与上游版本的可追溯记录 | Deferred（A2 期间可保持；Blocker (armed) at Gate F） |
| R-003 | 复权价为 `research_approx`，无可验证的历史调整因子时点快照 | P1 | Gate F / 数据有效性与时点正确性 | Gate F 中任何其有效性依赖复权价历史的结论 | 时点安全的复权因子来源与证据，并量化其与当前口径的回测差异；或在研究范围中显式声明该结论不依赖复权价历史，并有检查阻止越界使用 | Deferred（A2 期间可保持；Blocker (armed) at Gate F。不得顺延至实盘阶段） |
| R-004 | 公司行为仅完整支持现金红利；送转、拆并股、配股、证券变更会关闭运行 | P2 | 公司行为扩展阶段 | 标的域扩大后出现未支持公司行为的时段 | 每类公司行为的夹具与执行规则测试；未支持类型继续 fail-closed | Deferred |
| R-005 | 日线 OHLC 无法证明开盘瞬间盘口、排队与可成交量 | P2 | Paper Trading / Live Readiness（验证）；文档假设由当前阶段维护 | 不阻断 Gate F | 保守成交假设的显式文档；证据不足时 fail-closed 的路径测试；后续纸面交易中对这些假设的实测校验。**这是日线数据分辨率的固有限制，不是必须“解决”的缺陷**，验收目标是假设可审计而非消除限制 | Deferred |
| R-006a | Gate F 层执行真实性：交易成本、滑点、以及证据允许范围内的流动性/容量假设未做压力测试 | P1 | Gate F / 研究有效性验证 | Gate F 中任何声称收益或稳健性的结论 | 交易成本压力测试与滑点压力测试（含参数区间与结论敏感性）；在证据允许处给出合理的流动性与容量假设及其上限说明 | Deferred（A2 期间可保持；Blocker (armed) at Gate F） |
| R-006b | 券商级执行真实性：部分成交、盘口冲击、通道延迟、券商结算行为未建模 | P1 | Paper Trading / Live Readiness | 实盘接入 | 部分成交与盘口效应建模或实测；通道延迟测量；券商结算行为对账 | Deferred（**明确不属于 Gate F**；Gate F 不得为此建设生产级撮合模拟器） |
| R-007 | 共享现金 / 多标的组合语义：20 个基准是独立单标的账户，净值不可相加 | P1 | A2 / Planner / Portfolio | 任何把多标的组合结果作为有效研究证据使用的时点 | NO_DECISION 结转后的有效敞口口径；共享现金约束；总敞口上限；确定性组合状态测试（同输入同状态） | Closed（证据见 §3.1） |
| R-008 | SMA 候选仅验证流程；局部正收益不得解释为已验证 Alpha | P1 | 研究结论表述（持续） | 任何对外结论 | 所有报告显式标注结论边界；发布检查阻止越界表述 | Deferred（表述纪律持续生效） |
| R-009 | 内部一致性校验（提交、标签、SHA-256）不是第三方签名 | P2 | 供应链完整性阶段 | 不阻断 Gate F | 签名标签或外部时间戳证明 | Deferred |
| R-010 | 首次依赖安装可能访问包索引；仅安装后的重算承诺离线 | P3 | 环境固化阶段 | 不阻断 | 离线安装路径验证记录 | Deferred |
| R-011 | 固定审计对象（trust anchor / audit commit / audit tag）若被移动或重建，审计链不可恢复 | P0 | 持续（每次治理变更） | 立即 | 每次治理变更后重新核验 `HANDOFF.md` 中的哈希与标签指向 | Blocker (active on trigger)（一旦发生漂移即阻断） |
| R-012 | 无券商连接、自动下单与持续监控；不得表述为已完成实盘验证 | P1 | Paper Trading / Live Readiness（未授权） | 实盘接入 | 明确授权 + 独立任务书 + 资金安全门禁 | Deferred（在获得授权前不进入范围） |
| R-013 | GitHub Dependabot 告警未分诊（push 时由远端报告，本任务未调查） | 待定 | 依赖与供应链分诊（独立任务） | 待分诊后确定 | 分诊记录：受影响依赖、是否进入运行路径、升级或豁免决定及理由 | Deferred（仅登记为分诊候选；本任务不调查、不修复） |
| R-014 | 冻结基线中 79 个 Python 文件未满足 Ruff format；把全仓 format 直接作为 A2 门禁会与保护范围冲突 | P2 | Repository hygiene / 独立格式规范化任务 | 未来正式启用 repository-wide format CI 或验收门禁之前；不阻断 A2 closeout | 单独授权的纯格式 diff；确认不改变运行语义；全仓 `ruff format --check .`、lint、测试与 build 全部通过；保护范围经过独立复核 | Deferred（不属于 A2 blocker；基线与交集证据见 A2 implementation plan 的 FORMAT GATE AMENDMENT） |

### 3.1 R-007 A2 closure evidence

本关闭证据绑定本地提交 `33efc27`（rolling SELL accounting）、`626a774`（Planner → rolling
shared-cash orchestration）与 `c9516f8`（多标的 gross 聚合证据），并逐项映射到以下测试：

1. **NO_DECISION / carry-forward 有效状态直接消费**：
   `tests/unit/test_rolling_orchestration.py::test_rebalance_consumes_complete_effective_planner_state_without_second_carry_forward`
   证明 A2 直接消费 Planner 已完成结转的完整 `PlannedTargets`，不维护第二套 carry-forward。
2. **真实共享现金**：
   `tests/unit/test_rolling_orchestration.py::test_all_sells_run_before_all_buys_and_each_side_is_symbol_sorted`、
   `tests/unit/test_rolling_orchestration.py::test_sell_proceeds_are_available_to_later_buy_in_one_shared_cash_account`
   与
   `tests/unit/test_rolling_orchestration.py::test_buy_affordability_decrements_exactly_100_shares_including_fees`
   分别覆盖 SELL-before-BUY、卖出所得进入同一现金账户供后续 BUY 使用，以及含费用可负担性按 100 股递减。
3. **同 T 权益分母与总敞口**：
   `tests/unit/test_rolling_orchestration.py::test_rebalance_uses_T_close_equity_including_receivable` 与
   `tests/unit/test_rolling_orchestration.py::test_total_target_notional_uses_same_equity_and_respects_max_gross`、
   `tests/unit/test_rolling_orchestration.py::test_total_target_notional_aggregates_across_symbols_for_max_gross`
   覆盖同一 T 收盘权益分母（含 receivable）、单标的边界，以及每只分别低于上限但多标的合计超过
   `max_gross` 时的原子 fail-closed。
4. **确定性输入顺序 / hash seed**：
   `tests/unit/test_rolling_orchestration.py::test_reversed_execution_inputs_produce_structurally_identical_result`
   覆盖反转输入仍产生结构相同且稳定排序的结果；同一测试分别在 `PYTHONHASHSEED=11` 与
   `PYTHONHASHSEED=97` 下验证。

补充边界证据：

- 显式零与缺失 key 不等价：
  `tests/unit/test_rolling_orchestration.py::test_explicit_zero_no_bar_keeps_desired_realized_and_residual_visible`
  保留显式 FLAT，
  `tests/unit/test_rolling_orchestration.py::test_held_symbol_missing_from_effective_plan_fails_atomically`
  对缺失持仓 key 原子 fail-closed。
- desired / realized residual 只通过合法执行失败重试：
  `tests/unit/test_rolling_orchestration.py::test_explicit_zero_no_bar_keeps_desired_realized_and_residual_visible`、
  `tests/unit/test_rolling_orchestration.py::test_explicit_zero_price_limit_keeps_residual_visible`、
  `tests/unit/test_rolling_orchestration.py::test_later_effective_zero_plan_and_legal_session_recomputes_and_converges`
  与
  `tests/unit/test_rolling_orchestration.py::test_residual_does_not_create_a_shadow_target_or_mark_failure_achieved`
  覆盖 no-bar / price-limit 后保留差额、后续合法交易日重算收敛，且 residual 不成为影子目标。
- T+1 继续复用独立规则原语：
  `tests/unit/test_a_share_rules.py::test_t_plus_one_uses_calendar_and_does_not_wait_for_symbol_bar`；
  `tests/unit/test_rolling_orchestration.py::test_rebalance_rejects_lot_without_official_t_plus_one_binding_atomically`
  对伪造或无法绑定到官方日历的 availability 原子 fail-closed。

该关闭仅证明 **A2 工程层的 Planner → shared-cash 组合语义风险** 已具备验收证据；不证明
alpha、回测稳健性、数据有效性、Gate F、paper trading 或 live readiness。20 个历史基准仍是
独立单标的账户，其净值仍不可相加，也不会因本次 A2 关闭而被追溯解释为共享现金组合。

## 4. 维护规则

- 每个新阶段设计前：完整复核本表，更新判定，并在阶段任务书中引用受影响的 ID。
- 进入某阶段时：把该阶段的所有 Blocker (armed) 条目转为 active，并在阶段任务书中列出。
- 每个阶段结束时：回写状态；关闭的条目补充证据位置；未关闭的说明原因与新的 Owner Stage。
- 新发现的问题：当场登记，不允许只存在于对话或提交说明中。
- 已登记条目不得静默删除；只能被标记为 Closed 或明确标注为“不再适用”并写明理由。

## 5. 待决项（需用户确认，本次未擅自变更）

- R-004 的触发条件依赖 R-001 的标的域扩展；若 Gate F 采用时点标的域，未支持的公司行为类型出现概率显著上升，届时其 Severity 可能需从 P2 上调。
- R-008 与 R-012 的 Owner Stage 措辞早于本次阶段口径统一，建议对齐到 §1 的阶段表；内容判定不变。
