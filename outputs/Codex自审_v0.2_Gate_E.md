# Codex 自审报告（v0.2 Gate E）

- 项目：a-share-quant
- 版本：v0.2
- Gate：E
- 审查类型：Codex 最终自审
- 审查日期：2026-08-06
- 结论：PASS（P0=0 / P1=0 / P2=0）

## 一、审查范围

本次自审针对公开重建 Gate E 的实现与完整证据链，覆盖：

- implementation commit `ae317a01c5c36a7a59836665917afec4a7377125`；
- Candidate A review commit `bb49a6d1ede126fe1098944d7efd7bbdb6dd386c`；
- trust anchor commit `cc0a7c69c6a46cd86d8c42cdfe52efe64a20bfe6`；
- post-approval commit `e2eef971a6b42e0a9e2ae172da5ceac646f431ef`；
- Candidate A 和 Candidate B 的外部正式证据；
- A/B 隔离性、确定性、会计闭包与公开安全；
- 聚焦测试、全仓测试、v0.1 显式重建、静态检查、锁文件检查和 wheel 重建。

自审未将测试通过等同于策略有效，也未将本地可运行等同于可实盘交易。

## 二、身份与信任链

信任链顺序和绑定关系核查通过：

1. 固定实现：`ae317a01c5c36a7a59836665917afec4a7377125`；
2. Candidate A 只读复核进入提交 `bb49a6d1ede126fe1098944d7efd7bbdb6dd386c`；
3. trust manifest 在提交 `cc0a7c69c6a46cd86d8c42cdfe52efe64a20bfe6` 中锚定；
4. 信任锚复核通过后，以提交 `e2eef971a6b42e0a9e2ae172da5ceac646f431ef` 完成后置审批；
5. Candidate B 仅在上述闭包成立后执行受信重放。

关键绑定值：

- run ID：`8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084`；
- Candidate A evidence SHA-256：`cdfe0602f4f551351adc222f536346bec89f2d7222c8880d85a7d6dcbfabb940`；
- Candidate B evidence SHA-256：`5564272fb38333f44f140511e7f8ed28ab715a4756cdb6c3c91920a197071365`；
- project wheel SHA-256：`49d574515bdc46b8cc96f0fd9c9f1f2dbf3d9f48790fe01c662e58e3ef43144e`。

未发现信任顺序倒置、候选证据复用错误、实现提交漂移或以 Candidate B 反向创建信任的情况。

## 三、双环境复现与隔离审查

自审确认：

- Candidate A 与 Candidate B 使用不同工作根、虚拟环境、项目副本和输出目录；
- 两个环境均安装 37 个相同名称和版本的包；
- Candidate B 未复用 Candidate A 的可变中间产物、缓存或工作目录；
- 仓库冻结输入、Candidate A 输入和 Candidate B 输入三方共 25 个文件的 SHA-256 相同，设备号与 inode 组合独立；
- Candidate B 全流程未改变 Candidate A 的文件、目录、内容或时间状态；
- Candidate A 与 Candidate B 的 run ID 相同；
- 双方固定的 13 个正式文件集合完全相同且逐字节一致，无额外未声明输出。

因此，复演证据支持“独立环境可确定性重建”的工程结论，未发现通过共享 inode、隐藏缓存或触碰 Candidate A 来制造一致性的路径。

## 四、数量、业务证据与会计闭包

两个候选的语义证据一致：

- 10 个标的；
- 2072 个 sessions；
- 20720 行 positions；
- 25 个冻结输入文件；
- 13 个正式产物文件；
- 28 个 no-bar，且未被共同日期交集或结果筛选抹除；
- available 20692，no_bar_unavailable 28；
- 10 个目标与 10 笔成交订单的证据保留完整。

以分为单位复核，会计闭包成立：

- 现金：`100,000,000 - 94,075,000 - 25,028 + 314,000 = 6,213,972`；
- 净值：`6,213,972 + 168,992,200 + 0 = 175,206,172`；
- 配置：`94,075,000 + 0 + 925,000 + 0 + 0 + 0 = 95,000,000`。

`cash_identity_verified`、`net_asset_identity_verified` 和 `allocation_identity_verified` 均为 true。未发现现金、市值、应收或配置残差被静默忽略。

2026-07-24 仅存在于冻结输入的 provenance 覆盖边界；正式产物中的信号、
权重、账本、指标窗口和绩效均截止于 2026-07-23。公开源、Candidate A 与
Candidate B 始终各自包含精确 25 份输入，不存在私有基线曾出现的偏差文件，
`public_input_deviation_lock_count = 0`。

## 五、验证门结果

| 验证门 | 新鲜结果 | 自审判断 |
|---|---:|---|
| Gate E 聚焦测试 | 455 passed | PASS |
| 全仓测试 | 1095 passed, 1 skipped | PASS；跳过项已显式报告 |
| v0.1 显式重建 | 1 passed | PASS |
| Ruff | PASS | PASS |
| `uv lock --check` | PASS | PASS |
| 已提交内容空白检查 | PASS | PASS |
| 源码边界与公开敏感信息检查 | PASS | PASS |
| wheel 两次确定性重建 | SHA-256 均为 `49d574515bdc46b8cc96f0fd9c9f1f2dbf3d9f48790fe01c662e58e3ef43144e` | 与正式 wheel 一致，PASS |

## 六、A 股规则与边界判断

本次 Gate E 的目标是验证研究基础设施的可复现性，不是重新证明全部 A 股交易规则。现有产物保留 no-bar、现金拖累、整手取整、费用、公司行为和共享现金证据，没有因结果不利而过滤记录。

正式输入为公开确定性合成夹具（synthetic public fixture），并非真实行情。由此只能得出工程复现结论，不能得出以下结论：

- 策略有效、未来盈利或存在稳定 Alpha；
- 可在真实开盘盘口按模型价格成交；
- 日线 OHLC 足以证明排队顺序、容量或冲击成本；
- 系统已达到券商接入、自动下单或真实资金运行标准；
- 10 个标的可以代表 A 股全市场。

系统边界维持 `research_only=true`、`simulation_only=true`、`live_trading=false`、`profit_claim=false`。

## 七、公开安全自审

- 本报告仅使用仓库相对路径、提交标识和设备无关占位符；
- 未记录本机绝对路径、设备身份、私人账号或认证信息；
- 未纳入 Candidate A/B 工作区、缓存、虚拟环境、构建目录或其他临时文件；
- 正式公开输入为 synthetic public fixture，不包含受限制的原始行情数据；
- 源码边界与公开敏感信息扫描通过。

## 八、分级结论

- P0：0
- P1：0
- P2：0

未发现会阻止 Gate E 通过的实现、证据、隔离、会计、复现或公开安全缺陷。Codex 自审结论为 **PASS**。

本结论只对上述固定实现与证据链有效。任何后续代码、依赖、配置、冻结输入或信任文件变化，都必须作为新基线重新评估，不能静默沿用本结论。

---

project = a-share-quant
version = v0.2
gate = E
review_kind = codex_self_review
decision = PASS
P0 = 0
P1 = 0
P2 = 0
implementation_commit = ae317a01c5c36a7a59836665917afec4a7377125
candidate_a_evidence_sha256 = cdfe0602f4f551351adc222f536346bec89f2d7222c8880d85a7d6dcbfabb940
candidate_b_evidence_sha256 = 5564272fb38333f44f140511e7f8ed28ab715a4756cdb6c3c91920a197071365
expected_run_id = 8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084
