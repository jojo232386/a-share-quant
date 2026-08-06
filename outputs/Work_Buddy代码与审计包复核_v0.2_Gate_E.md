# Work Buddy 最终发布复核报告（v0.2 Gate E）

- 复核对象：a-share-quant v0.2 Gate E 最终发布（final release）
- 复核类型：最终独立只读代码与审计包复核（final_release）
- 复核日期：2026-08-06
- 复核方：Work Buddy（独立复核）
- 结论：PASS（P0/P1/P2 均为零）

## 一、检查范围与方法

对公开重建仓库（以下简称 `<repository-root>`）与正式根目录（以下简称 `<formal-root>`）进行只读复核，覆盖：Git 信任链、正式 wheel 与 wheelhouse、候选 A/B 证据与产物逐字节比较、输入与时间边界、会计复算、测试门、Codex 控制器报告的事实准确性。全部数据来自真实文件与 Git 对象的独立读取与计算，未联网、未下载、未修改依赖，未重跑 Candidate A/B，未创建信任锚，未推送；唯一写入为本报告文件。

方法说明（环境约束披露）：Work Buddy 的审查执行环境拒绝了嵌套 `sandbox-exec`（`sandbox_apply: Operation not permitted`）。Codex 控制器在其正常 shell 验证同一合法 profile 可成功执行（退出码 0），因此这是审查方运行环境的限制，不是主机级 macOS 限制，也不是候选 A/B 或信任链的缺陷。由此产生的测试覆盖差异已如实记录于本报告测试门章节，未计入 P0/P1/P2。

## 二、Git 信任链核查

- 仓库 HEAD 精确等于后置审批提交 `e2eef971a6b42e0a9e2ae172da5ceac646f431ef`（提交信息：review: approve public v02 Gate E trust anchor），写报告前工作树无已跟踪文件变更。
- 信任链祖先关系成立：实现提交 `ae317a01c5c36a7a59836665917afec4a7377125` → 候选 A 复核记录提交 `bb49a6d1ede126fe1098944d7efd7bbdb6dd386c`（新增候选 A 复核报告 95 行）→ 信任锚提交 `cc0a7c69c6a46cd86d8c42cdfe52efe64a20bfe6`（新增 trust_manifest.json 1 行）→ 后置审批提交 `e2eef971a6b42e0a9e2ae172da5ceac646f431ef`（新增信任锚复核报告 86 行）；实现与信任锚均为审批提交的祖先，无顺序倒置。
- 锚定 blob 与工作树字节绑定：`cc0a7c69` 树中 trust_manifest.json 内容 SHA-256 为 `fa770a6e65fc456c028c2f1bdd5b180b1b30556d4a6a3119ee2170b6fceb8d0f`；`bb49a6d` 树中候选 A 复核报告内容 SHA-256 为 `962c54a5265a709f370f840d757766a421b556a30754634c4e9ebc03790331be`；`e2eef97` 树中信任锚复核报告内容 SHA-256 为 `b34da03a1b4bc970a17712157861d1ce33e5d1e6d3e5c05eacbb441f5257eb61`，三者均与当前工作树逐字节一致。
- git 对象库完整：无 replace refs（0 条）、无 grafts、非 shallow 克隆，`git fsck --no-dangling` 通过。
- 信任清单确定性重建：从固定候选 A 证据与已批准候选 A 复核报告调用 `gate_e_trust_bytes` 重建，与已发布 `release/v0.2-gate-e/trust_manifest.json` 逐字节一致（17883 字节），SHA-256 均为 `fa770a6e…`；`verify_gate_e_trust` 全量只读验证通过（13 正式文件、12 payload、expected_run_id 与 trust_sha256 均一致）。信任清单绑定实现提交、预期 run ID、候选复核报告、产物、配置、v0.1 冻结输入、项目 wheel、wheelhouse、Python、uv、uv.lock 与研究/模拟边界，逐项核对一致。

## 三、Candidate A/B 证据与双环境逐字节比较

- 候选 A 证据 SHA-256 `cdfe0602f4f551351adc222f536346bec89f2d7222c8880d85a7d6dcbfabb940`、候选 B 证据 SHA-256 `5564272fb38333f44f140511e7f8ed28ab715a4756cdb6c3c91920a197071365`，与给定值一致；两份证据均通过库内全量核心审计（身份、运行时、信任根、wheel、输入、产物、守卫）。
- 两份证据 hash_seed 不同（A 为 101、B 为 909）；工作根、venv、HOME、缓存、project 根与输出根彼此独立；B 证据路径不含任何候选 A 路径引用。
- 两个候选 run ID 相同，均为 `8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084`。
- 两个产物目录各恰好 13 个正式文件（12 个 payload 加 artifact_manifest.json），文件集合完全相同，13 个文件原始字节逐个一致（逐文件 SHA-256 相同）；双方输出根均无额外未声明文件；双方 37 个已安装包名称与版本集合一致。
- 25 份冻结输入在仓库、候选 A、候选 B 三方的内容 SHA-256 逐项相同，且三方设备号与 inode 组合彼此独立（无硬链接共享）。
- 候选 A 零触碰：候选 A 工作区全部对象最新修改时间为证据生成时刻（早于候选 B 生成与全部后续流程），候选 B 流程未在候选 A 工作区创建、修改或清理任何文件。
- 结论：`a_b_byte_identical = true`，双环境独立确定性复现成立。

## 四、输入、时间边界与 A 股边界核查

- 运行配置固定，恰好 25 份输入，25 项均为完整 SHA-256 绑定，独立复算全部一致；公开源、候选 A、候选 B 始终各含精确 25 份输入，无偏差文件或需豁免的 lock。
- 日历覆盖 2074 个日期（2018-01-02 至 2026-07-24），正式组合区间 2072 个 sessions，正式绩效截止 2026-07-23。2026-07-24 仅在冻结输入的 provenance 覆盖边界内，独立检查确认该日未出现在 equity、positions、targets、orders、fills、cash、receivables、corporate_actions、lots、availability 任一正式产物中，即未进入信号、权重、指标窗口、成交、现金、持仓、权益或绩效。
- 10 个标的（8 只主板股票加 2 只宽基 ETF）；20720 行 positions；28 个 no-bar（按标的 3/3/2/2/3/3/3/3/3/3）未被共同日期交集或结果筛选抹除，available 20692、no_bar_unavailable 28；10 个目标与 10 笔订单（含失败类别）如实保留在正式证据中。
- 输入为公开确定性合成夹具（synthetic public fixture），非真实行情；系统边界维持 research_only、simulation_only，不连接券商、不提交订单、不执行自动交易，不证明策略有效、可盈利或可实盘；日线数据无法还原盘口排队与真实可成交性。

## 五、会计复算

以分为单位从原始账本独立复算，全部成立：

- 现金恒等式：期末现金 = 初始现金 − 已投入名义 − 已付费用 + 已付红利现金，即 100,000,000 − 94,075,000 − 25,028 + 314,000 = 6,213,972；现金账本 14 条事件（10 笔买入、4 笔红利）逐条复核，费用合计 25,028、红利合计 314,000 与账本一致。
- 净值恒等式：期末净值 = 期末现金 + 期末持仓市值 + 期末应收，即 6,213,972 + 168,992,200 + 0 = 175,206,172。
- 配置恒等式：总目标名义 = 已投入 + 配置取整 + 整手取整 + 费用扣手 + 待投 + 过期未投，即 94,075,000 + 0 + 925,000 + 0 + 0 + 0 = 95,000,000（对应 gross_target_weight 0.95）。
- 实际权重独立复算：期末 10 标的权重加现金权重合计为 1，与证据 final_symbol_weights、final_cash_weight 逐位一致；证据 cash_identity_verified、net_asset_identity_verified、allocation_identity_verified 均为 true。

## 六、测试门核查

在实现提交上以零写入模式（禁用 pycache 与 pytest cache）独立运行：

- 聚焦组合（Gate E 加 portfolio 相关 13 个文件共 660 项）：642 passed；18 项失败全部位于 test_gate_e_environment.py 且原因统一为审查环境无法执行嵌套 sandbox-exec 导致 venv 创建失败（对照实验：裸 `python -m venv` 创建成功、`sandbox-exec` 退出码 71），属环境限制而非代码缺陷；Codex 控制器在其正常 shell 报告聚焦门 455 passed。
- 全仓套件（1096 项）：1077 passed、1 skipped（v0.1 显式重建测试的默认 skip 条件）、18 项环境受限失败；Codex 控制器在其正常 shell 报告 1095 passed、1 skipped，差异 18 项与上述环境限制一一对应。
- v0.1 显式重建：以 `AQUANT_RUN_RELEASE_INTEGRATION=1` 运行，1 passed（166 秒）。
- Ruff：All checks passed。
- `uv lock --check`：通过（Resolved 44 packages）。
- 已提交内容空白检查：对 `6ff6f85…` 至 `e2eef97…` 范围通过（退出码 0）。
- wheel 确定性重建：独立 `uv build` 重建 wheel 的 SHA-256 为 `49d574515bdc46b8cc96f0fd9c9f1f2dbf3d9f48790fe01c662e58e3ef43144e`，与正式项目 wheel 完全一致。
- pytest node IDs 清单（`outputs/pytest_nodeids_v0.2_Gate_E.txt`）共 1096 行，与全仓套件规模一致。
- 公开敏感信息扫描：已跟踪源码与文档无本机用户目录路径、无私人身份或真实凭据（仅测试用例中的 example.com 占位 URL），公开安全达标。

## 七、Codex 控制器报告事实核查

- Codex 自审报告（`outputs/Codex自审_v0.2_Gate_E.md`）：身份与信任链、37 包一致、25 输入三份 SHA 相同且 inode 独立、A/B 13 文件逐字节一致、会计闭包、2026-07-24 provenance 边界、测试门数字与 wheel 重建哈希——与本次独立复核结果一致，未发现事实性错误或遗漏。
- Codex 双环境复演报告（`outputs/Codex双环境复演_v0.2_Gate_E.md`）：run ID 相同、37 包一致、25 输入内容一致且物理独立、13 文件逐字节一致、候选 A 零触碰、会计与业务证据一致——与本次独立复核结果一致；其 `artifact_files_byte_identical = true`、`candidate_a_zero_touch = true` 断言均获独立证据支持。
- 两份报告均未声称策略有效、可盈利或可实盘，边界声明准确。

## 八、分级结论

- P0：0（无阻止性缺陷）
- P1：0（无严重缺陷）
- P2：0（无轻微缺陷）
- P3 披露（不计决策）：本审查环境无法执行嵌套 sandbox-exec，18 项依赖 sandbox 的环境测试在本环境不可复现（Codex 控制器正常环境通过，根因经对照实验确认属环境限制）；该限制不影响任何正式产物、证据、信任链或其余 1077 项全仓测试。

基于以上核查，Git 信任链完整且绑定正确，A/B 独立重放逐字节一致且零触碰，输入与时间边界正确，会计闭包成立，测试门（除审查环境限制项）与 wheel 重建全部通过，Codex 报告事实准确，公开安全达标。复核结论为 **PASS**。本结论不证明策略盈利、Alpha 有效、可实盘成交或可接券商。

---

project = a-share-quant
version = v0.2
gate = E
review_kind = final_release
decision = PASS
P0 = 0
P1 = 0
P2 = 0
implementation_commit = ae317a01c5c36a7a59836665917afec4a7377125
trust_anchor_commit = cc0a7c69c6a46cd86d8c42cdfe52efe64a20bfe6
approval_commit = e2eef971a6b42e0a9e2ae172da5ceac646f431ef
expected_run_id = 8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084
candidate_a_evidence_sha256 = cdfe0602f4f551351adc222f536346bec89f2d7222c8880d85a7d6dcbfabb940
candidate_b_evidence_sha256 = 5564272fb38333f44f140511e7f8ed28ab715a4756cdb6c3c91920a197071365
a_b_byte_identical = true
