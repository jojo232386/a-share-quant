# v0.2 Gate D：单成员跨引擎经济等价实施计划

> 状态：设计门和 Gate C 已通过；用户于 2026-07-29 批准方案 A；
> Gate D 已通过最终双审。Gate E 尚未通过，只可进入独立设计门。

## 目标

用同一份经过 manifest 和 SHA-256 验真的单标的输入，分别运行冻结的
v0.1 Backtrader 路径和 v0.2 单成员组合路径，逐字段证明经济结果一致。

本阶段只验证工程等价，不证明策略有效、可实盘成交或能够盈利；不运行
Gate E 的冻结十标的组合。

## 不可破坏边界

- 不修改 `v0.1-research` 标签、发布清单或冻结预期身份；
- 不修改 `src/aquant/backtest/`、`src/aquant/data/`、
  `src/aquant/rules/` 和 v0.1 CLI 的发布语义；
- 不用 `pytest.approx` 或扩大容差掩盖一分钱差异；
- 不比较 run ID、内部 ID、schema 和文件字节；
- 主等价集合不得包含拒单、无 bar、重试、未支持公司行为或源 payable
  date 非交易日；
- 只允许用户于 2026-07-29 批准的
  `known_v01_record_date_entitlement_defect` 命名边界；若再发现其他
  v0.1 语义缺陷，仍须先记录证据并停在设计决策门，不在组合提交中静默
  修正或扩大排除范围。

## 规范化规则

v0.1 的 float 元金额统一经
`decimal_yuan_to_fen(Decimal(str(value)))` 转为整数分，再与 v0.2
精确比较。

v0.1 多出的 signal-date 日终行必须先单独证明为：

- 初始现金；
- 零持仓；
- 零市值；
- 零应收；
- 权益等于初始现金。

随后按官方 session 键对齐执行日起的序列，不按数组位置或宽松日期交集
静默丢行。

允许不比较：

- engine、run ID、implementation digest 和 schema；
- order/attempt/fill/lot 的内部 ID 名称；
- v0.2 独有的 target、attempt、availability 审计字段。

## 最小验收矩阵

| 场景 | 输入边界 | 必须精确一致 |
| --- | --- | --- |
| A 主板基础买入 | 0.95 权重、价格变化、比例佣金、过户费 | 信号、成交、费用、逐日全账本 |
| B ETF 最低佣金 | 10,000 元、0.95、开盘 95.000 | 100 份、最低佣金、无股票过户费 |
| C 满仓费用缩手 | 权重 1、费用使候选数量不可支付 | 两边缩至同一整手和同一现金 |
| D 正常现金红利 | 买入不晚于 record date；ex/payable 均为有 bar 的交易日 | 资格、应收、到账、权益 |
| E T+1 与估值 | 覆盖成交日、下一交易日和不同收盘价 | available/locked、lot、市值 |
| F 红利资格红灯 | `acquired_date > record_date` 且 `acquired_date < ex_date` | 永久、精确暴露已批准的 v0.1 版本边界；不计入等价通过 |

## Task 1：建立正式同源 fixture

文件：

- 新增 `tests/portfolio_gate_d_support.py`

任务：

1. 一次性发布并加载 manifest 验真的 `VerifiedMarketData`；
2. 一次性发布并加载 manifest 验真的 `VerifiedCorporateActions`；
3. 创建 singleton `VerifiedUniverse`、同一 `VerifiedTradingCalendar` 和
   `VerifiedFeePolicy`；
4. 用同一组对象、初始现金、目标权重、signal date 和 end date 分别调用
   `run_backtest()` 与 `run_verified_portfolio()`；
5. 提供只做格式规范化、不改变经济语义的比较视图。

验收：

```bash
UV_OFFLINE=1 uv run --no-sync ruff check tests/portfolio_gate_d_support.py
```

## Task 2：基础、ETF、缩手和 T+1 精确等价

文件：

- 新增 `tests/unit/test_portfolio_equivalence.py`

测试必须断言：

- v0.1 signal-date 初始空仓行；
- signal date、execution date、side、成交数量、Decimal 成交价、
  notional_fen；
- commission、stamp duty、transfer fee、total fees；
- 每个 v0.2 session 都有且只有一个 v0.1 对应 session；
- cash、total/available/locked size、mark、market value、receivable、
  equity；
- lot 的 symbol、acquired/available date、original/remaining size 和
  unit cost；
- v0.1 `completed` 只映射为 v0.2 `filled`，不比较内部 ID。

验收矩阵覆盖 A、B、C、E。

## Task 3：现金红利正常路径

同一测试文件增加场景 D：

- 买入日不晚于 record date；
- ex-date 与 payable date 都是官方交易日并有 bar；
- 比较 event identity、ex-date、source payable date、actual cash date、
  entitled size、每单位红利和 amount_fen；
- 逐日分别比较现金和应收，不能只比较权益；
- 验证公司行为只应用一次。

保留现有 v0.2 专项测试：

`test_nontrading_payable_date_pays_on_next_session_without_symbol_bar`

该专项不是跨引擎等价证据。

## Task 4：红利资格红灯探针

新增一个不修改生产代码的诊断测试：

- record date 在买入日前；
- 买入发生在 record date 后、ex-date 前；
- 证明冻结 v0.1 按 ex-date 持仓给出红利，而 v0.2 按 record-date
  持仓不给红利；
- 测试名称和断言必须写明这是已观察到的语义差异，不得称为等价通过。

差异已成立并完成决策：

1. 原始阻断与精确数字保留在
   `outputs/Gate_D阻断_红利资格语义差异.md`；
2. 用户于 2026-07-29 批准方案 A，将差异命名为
   `known_v01_record_date_entitlement_defect`；
3. 不修改冻结 v0.1，不改变 v0.2 的 record-date 正确口径；
4. F 必须永久断言 v0.1 资格 100、金额 `20000` 分，v0.2 资格和金额
   均为 0，最终权益差额恰为 `20000` 分；
5. F 是 approved known v0.1 version boundary，不是 A–E 经济等价证据；
6. 最终双审和全部离线门禁现已完成；Gate D 可标为 PASS，Gate E 仍须
   先完成独立实施计划和设计门，不得把 Gate D 结果冒充 Gate E 证据。

## Task 5：离线门禁与冻结边界

```bash
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q \
  tests/unit/test_portfolio_equivalence.py \
  tests/unit/test_portfolio_coordinator.py
PYTHONPATH=src UV_OFFLINE=1 uv run --no-sync pytest -q
UV_OFFLINE=1 uv run --no-sync ruff check .
uv lock --check
git diff --check
git diff --exit-code v0.1-research -- \
  src/aquant/backtest src/aquant/data src/aquant/rules \
  src/aquant/backtest_cli.py
```

所有命令离线运行；不修改用户当前 VPN、代理、DNS 或 macOS 网络设置。

## Task 6：审查与发布门

Codex 自审必须记录：

- 实际场景、规范化方法和逐字段比较范围；
- Gate D 聚焦与全仓测试结果；
- 冻结 v0.1 边界；
- 发现的每个差异及其是否属于已批准排除项；
- `P0/P1/P2` 和真实 Gate D 状态。

Work Buddy 独立复核必须尝试：

- 一分钱现金差；
- 一个交易日错位；
- T+1 available/locked 对调；
- 漏比较费用分项；
- 将 signal-date 行静默丢弃；
- record-date 红利资格差异；
- 把非交易日 payable 专项冒充等价证据。

只有 A–E 全部精确一致、F 持续精确复现且只作为已批准命名版本边界、
无其他未批准语义差异、`P0=0 / P1=0 / P2=0` 时，Gate D 才能标为
PASS。否则保留测试和证据，停止进入 Gate E。

## 最终验收记录

2026-07-29，全部核心门禁离线完成：

```text
Gate D A-F: 6 passed
Gate D + coordinator: 26 passed
Full repository: 702 passed, 1 skipped
Ruff: All checks passed
uv lock --check: passed
git diff --check: passed
frozen v0.1 source boundary: empty
SPEC_REVIEW: PASS (P0=0 / P1=0 / P2=0)
QUALITY_REVIEW: PASS (P0=0 / P1=0 / P2=0)
WORK_BUDDY_FINAL_REVIEW: PASS (P0=0 / P1=0 / P2=0)
GATE_D_STATUS: PASS
```

这些结果只关闭单成员跨引擎经济等价门，不证明策略有效、可实盘成交或
能够盈利，也不代表冻结十标的 Gate E 已通过。
