# Work Buddy 候选 A 复核报告（v0.2 Gate E）

- 复核对象：Candidate A（候选 A）
- 复核类型：最终只读复核（Candidate A）
- 复核日期：2026-08-06
- 复核方：Work Buddy（独立复核）
- 结论：PASS（P0/P1/P2 均为零）

## 一、复核范围与方法

对公开重建仓库（以下简称 `<repository-root>`，即本仓库根目录）、正式证据与产物目录（以下简称 `<formal-root>`，存放 candidate/project-wheel/sealed 的正式根目录）与候选 A 工作区（以下简称 `<candidate-a-root>`）进行只读复核。未创建信任锚，未运行 Candidate B，未执行 commit/push，未修改任何代码、测试、配置、冻结输入、候选 A 工作区或正式产物；唯一写入为本报告文件。

复核方式为项目自带的只读审计链路加独立复算：先阅读源码确认审计与安全逻辑，再调用已安装库内的核心验证函数对证据、输入与产物逐项独立复算，并与候选证据声明的统计量、恒等式逐一对账。

方法说明（环境约束披露）：Work Buddy 的审查执行环境拒绝了嵌套 `sandbox-exec`，报错 `sandbox_apply: Operation not permitted`。Codex 控制器在其正常 shell 中验证了同一合法 `sandbox-exec` profile（`(version 1)(allow default)`）可以成功执行（退出码 0），因此这是审查方运行环境的限制，不是主机级 macOS 限制，也不是候选 A 的缺陷。候选 A 于生成当日完成全部七个阶段，其生成链路与项目自带 CLI 审计（内部硬编码调用 sandbox-exec）在生成时刻可正常执行；本次审查环境无法再次执行嵌套 sandbox-exec。该限制不影响对候选 A 证据与产物的静态与计算复核；本报告中所有统计量、恒等式与哈希均为不依赖 sandbox 的独立复算结果，已全部通过。此环境限制不构成候选 A 的缺陷，未计入 P0/P1/P2。

## 二、身份闭包核查

以下项目均独立核对通过：

- 仓库 HEAD 与实现提交一致：HEAD 指向实现提交 `ae317a01c5c36a7a59836665917afec4a7377125`，工作树干净，候选证据中的 implementation_commit 字段与此一致。
- 候选证据 SHA-256 与任务提供的预期值一致，为 `cdfe0602f4f551351adc222f536346bec89f2d7222c8880d85a7d6dcbfabb940`。
- 正式项目 wheel SHA-256 与预期值一致，为 `49d574515bdc46b8cc96f0fd9c9f1f2dbf3d9f48790fe01c662e58e3ef43144e`（库函数 `inspect_project_wheel` 独立复算一致）。
- 产物清单（artifact manifest）SHA-256 与预期值一致，为 `2f2e749403bec620687fd6849bd4f92b0731e9b6dc4594ab5fbe180afdabb110`。
- sealed wheelhouse manifest SHA-256 复算为 `3ffd3ffd12b5136193aba33350e77e8598126c7092d2a57c9a47ca71a4f63bc0`，与候选证据 wheelhouse.manifest_sha256 一致；install lock SHA-256 为 `11a643bb9c4d9dee1934b732c6649f99133ab6f459c789995e89ee4a36bbf29b`，与证据一致。
- 运行配置 SHA-256 复算为 `1794ed454604d77dacdd9bb87b778721afd12cdbe7354f90a6b5d38dadd49935`，与证据 config_sha256 一致。
- v0.1 冻结输入标签 `v0.1-research` 指向提交 `6ff6f85849c35e6475cff69f2b3caef5bf5f07f7`，与证据 v01_tag_commit 一致；`<repository-root>/release/v0.1-research` 即冻结输入发布根，共 25 个输入文件。
- 候选证据结构与固定键集合完全匹配，canonical JSON 校验通过，trust_created 为 false，未创建任何信任链。

## 三、只读审计复算（必查统计量）

调用库内核心验证函数对证据、输入与产物逐项复算，全部通过：

- 正式文件数：13（12 个 payload 文件加 artifact_manifest.json），与证据 counts.artifact_files 一致；13 个文件的 SHA-256 全部与 artifact manifest 记录逐字节一致（独立复算）。
- 输入文件数：25，与证据 counts.input_files 及运行配置 input_files 数量一致；`<repository-root>/release/v0.1-research/inputs` 下实存 25 个文件。
- 标的数：10（8 只主板股票加 2 只宽基 ETF），与 universe 成员、证据 counts.symbols 及审计预期一致。
- sessions：2072，证据 counts.sessions、equity.csv 行数与输入审计 session_count 三者一致。
- no-bar：总计 28，按标的分布为 000001/3、000858/3、510300/2、510500/2、600030/3、600036/3、600519/3、600900/3、601166/3、601318/3，与证据及审计预期完全一致。
- 现金恒等式：期末现金 = 初始现金 − 已投入名义 − 已付费用 + 已付红利现金，即 6,213,972 = 100,000,000 − 94,075,000 − 25,028 + 314,000，成立；证据 cash_identity_verified 为 true。
- 净值恒等式：期末净值 = 期末现金 + 期末持仓市值 + 期末应收，即 175,206,172 = 6,213,972 + 168,992,200 + 0，成立；证据 net_asset_identity_verified 为 true。
- 配置恒等式：总目标名义 = 已投入 + 配置取整 + 整手取整 + 费用扣手 + 待投 + 过期未投，即 95,000,000 = 94,075,000 + 0 + 925,000 + 0 + 0 + 0，成立；证据 allocation_identity_verified 为 true。
- 业务证据（总收益 0.75206172、最大回撤 −0.384395729479、10 笔订单全部成交、最终权重）与 metrics.json、orders.csv、targets.csv、positions.csv 等产物复算一致。
- 运行时（Python 3.11.15、uv 0.11.23）快照与证据 runtime 一致，候选运行期间运行时未变更。

## 四、审查过程无写入核查

- 候选 A 工作区、venv、输入与产物全部对象的最新修改时间不晚于候选证据生成时刻（审查开始之前），审查期间无任何文件被创建或修改；venv 内既有 `__pycache__` 属安装期产物，时间戳早于审查开始。
- venv 整树与 project 输入、产物目录实为只读：扫描结果可写文件数为 0、可写目录数为 0；审查期间运行以 `PYTHONDONTWRITEBYTECODE=1` 执行，且只读权限本身阻止任何写入。
- `<repository-root>` git 工作树在审查期间保持干净，未新增未跟踪内容。
- 审查仅创建本报告一个文件，未触碰候选 A 证据、产物、配置、冻结输入或正式 sealed/project-wheel 内容。

## 五、安全守卫与证据一致性核查

- 新 venv 整树只读守卫：源码实现拒绝任何带写位（0o222）的对象；实测 venv 树可写对象数为 0，与证据一致。
- 硬链接拒绝：源码实现要求常规文件 nlink 为 1，否则拒绝，防止共享 inode 篡改。
- 外部 symlink 限制：源码实现要求符号链接解析后目标必须位于环境树内或属于明确允许的外部目标（固定基础解释器），否则拒绝。
- 多级 Python 链：venv 解释器符号链接链至固定基础解释器（固定 SHA-256 已记录于证据 runtime），审查中独立验证通过。
- inspect/run/verify/audit 前后守卫：候选运行在阶段前后捕获并验证运行时执行守卫与运行时快照，审计链路在 `finally` 中再次验证守卫；环境树在执行前后以逐对象元数据与内容比对（capture/verify 成对）方式保证未被改动。审查中独立调用环境执行守卫验证通过。
- macOS sandbox 约束：源码使用 `/usr/bin/sandbox-exec` 加网络与写入 deny profile 运行安装、回放与验证子进程，并对校验模式文件逐一路径 deny 写入；实现与证据声明的约束一致（审查环境无法执行嵌套 sandbox-exec 的限制见第一节说明，属审查方环境限制，不属于候选 A 缺陷）。

## 六、时间边界与研究/模拟边界核查

- 输入为公开确定性合成夹具（synthetic public fixture），非真实行情；正式组合区间 2018-01-04 至 2026-07-23，共 2072 个交易日，日历 2074 个日期中边界日仅用于信号或收尾验证。
- 研究边界声明齐全且一致：README、研究范围、已知限制等文档与候选证据 research_boundary 字段均为 research_only、simulation_only，明确不连接券商、不提交订单、不执行自动交易，不证明盈利、Alpha 有效、真实成交可行或可用于实盘；日线数据无法还原开盘盘口与排队顺序，保守拒单不视为真实成交还原；10 个试点标的仅为工程验证，不代表全市场或投资建议。
- 结论不构成对候选 A 未来收益或实盘可成交性的任何证明。

## 七、公开安全核查

- 本报告全文使用仓库相对路径与 `<repository-root>`、`<formal-root>`、`<candidate-a-root>` 等设备无关占位符，不包含本机绝对路径。
- 本报告不含用户目录、设备名、私人邮箱、VPN/代理/DNS 信息、临时工作区绝对路径或任何认证信息。
- 候选产物（run.json 等）经检查不含本机用户名或用户目录路径，公开内容安全。

## 八、分级结论

- P0：0（无阻止性缺陷）
- P1：0（无严重缺陷）
- P2：0（无轻微缺陷；审查环境 sandbox-exec 限制已在第一节如实披露，属于运行环境约束而非候选 A 缺陷，不计入分级）

基于以上核查，候选 A 的代码、证据、输入、产物与全部必查统计量一致，身份闭包完整，安全守卫与证据声明相符，边界声明齐全，公开安全达标。复核结论为 PASS。

---

project = a-share-quant
version = v0.2
gate = E
review_kind = candidate_a
decision = PASS
P0 = 0
P1 = 0
P2 = 0
implementation_commit = ae317a01c5c36a7a59836665917afec4a7377125
candidate_evidence_sha256 = cdfe0602f4f551351adc222f536346bec89f2d7222c8880d85a7d6dcbfabb940
expected_run_id = 8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084
artifact_manifest_sha256 = 2f2e749403bec620687fd6849bd4f92b0731e9b6dc4594ab5fbe180afdabb110
project_wheel_sha256 = 49d574515bdc46b8cc96f0fd9c9f1f2dbf3d9f48790fe01c662e58e3ef43144e
