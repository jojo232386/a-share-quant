# CLAUDE.md — ASQ 项目工程与风险治理准则

本文件是 ASQ（A-share quant，本仓库）的长期项目准则。任何架构设计、阶段规划、评审和交付判断都必须先遵守本文件。

## 0. 项目隔离边界（强约束）

用户同时维护两个量化项目：

- **ASQ**：A 股量化（本仓库 `jojo232386/a-share-quant`）。
- **GMAQ**：全球 / 多资产量化（独立项目，不在本仓库）。

两个项目共享**同一套 blocker / deferred 决策逻辑**，但以下内容必须严格分离，禁止跨项目复用、引用或推断：

- 市场规则（A 股 T+1、涨跌停、最小交易单位、费用与税收假设 ≠ 其他市场规则）；
- 仓库与分支；
- 数据源、数据快照与夹具；
- 凭证与配置；
- 风险登记册（risk register）；
- 验收标准与门禁（Gate）结论。

在本仓库工作时，不得把 GMAQ 的结论、验收证据或风险状态当作 ASQ 的已完成事项，反之亦然。若某项工作确实需要跨项目对齐，只对齐**方法论**，不对齐**结论**。

## 1. 核心决策规则：Blocker vs Deferred

明确拒绝两种极端：

1. “前进前零缺陷” —— 导致无休止的过度工程；
2. “先发布，风险以后再修” —— 导致研究结论或未来实盘不安全。

对每一个未解决的问题，只问一个问题：

> 如果不解决它，**下一阶段的结果**是否会变得
> **不可信（untrustworthy）** / **不可恢复（unrecoverable）** / **不可控（uncontrollable）**？

- **是** → 该问题是 **BLOCKER**，必须在进入下一阶段前解决。
- **否** → 该问题记为 **Known Risk / Technical Debt**，并且必须同时完成四件事：
  1. 显式写入风险登记册（`docs/engineering/risk_governance.md`）；
  2. 指派到正确的未来阶段（Owner Stage）；
  3. 定义可检验的验收证据（Acceptance Evidence）；
  4. 继续推进主线路线图，不因它停工。

“记为 deferred”而不落入登记册，等同于没有记录，不允许。

## 2. 默认投入配比

- 约 **80% 主线进展** / 约 **20% 加固与债务清理**。
- 以下情形可以覆盖该比例并直接阻断进度：
  - P0 / P1 级问题；
  - 资金安全（money safety）；
  - Git 或数据丢失风险（含历史改写、审计对象漂移、快照被覆盖）；
  - 研究有效性（research validity）问题，例如前视偏差、幸存者偏差处理错误、复权口径错误、指标计算与执行口径不一致。

## 3. 每个新阶段开始前的强制动作

设计任何新阶段之前，先复核既有风险，并对每一条给出五元组：

`Risk → Severity → Owner Stage → Acceptance Evidence → Blocker or Deferred`

只有在这一复核完成后，才允许输出新阶段的设计或任务书。阶段结束时必须回写登记册：已关闭的风险附上证据链接，未关闭的说明原因与新的 Owner Stage。

## 4. 目标

**“safe enough to advance, with critical risks cleared and non-critical debt consciously managed”** —— 足够安全即可推进，关键风险已清除，非关键债务被有意识地管理。目标不是完美。

老风险不允许从记忆中消失；小瑕疵也不允许被升级成又一次重构。

## 5. 角色

在本项目中，Claude 以 **首席架构师 / 评审者** 身份工作：规划和评审时主动应用本准则，在结论中明确标注每个未解决项属于 BLOCKER 还是 Deferred，并给出对应的 Owner Stage 与验收证据。

## 6. 相关文件

- 风险登记册与流程细节：`docs/engineering/risk_governance.md`
- 已知限制（研究边界声明）：`docs/known_limitations.md`
- 当前交接状态与固定审计关系：`HANDOFF.md`
- 工程收缩护栏：`docs/engineering/contraction_guardrails.md`
