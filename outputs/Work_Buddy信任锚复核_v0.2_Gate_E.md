# Work Buddy 信任锚复核报告（v0.2 Gate E）

- 复核对象：公开 v0.2 Gate E Candidate A 信任锚（trust anchor）
- 复核类型：最终只读复核（trust_anchor）
- 复核日期：2026-08-06
- 复核方：Work Buddy（独立复核）
- 结论：PASS（P0/P1/P2 均为零）

## 一、复核范围与方法

对公开重建仓库（以下简称 `<repository-root>`，即本仓库根目录）的信任锚提交、信任清单（trust manifest）与候选 A 复核报告进行只读复核。未修改 Candidate A、候选 A 复核报告、信任清单、代码、测试、配置、发布输入、wheelhouse、Git 历史或任何其他文件；未执行 commit/push，未运行 Candidate B；唯一写入为本报告文件。

复核方式为项目自带的信任验证链路加独立复算：先阅读源码确认信任解析与验证逻辑，再调用已安装库内的核心验证函数，从固定候选 A 证据与已批准候选 A 复核报告确定性重建信任清单并逐字节比对，再对已发布信任清单做全量只读验证，并逐项核对信任绑定闭包。

方法说明（环境约束披露）：Work Buddy 的审查执行环境拒绝了嵌套 `sandbox-exec`，报错 `sandbox_apply: Operation not permitted`。Codex 控制器在其正常 shell 中验证了同一合法 `sandbox-exec` profile（`(version 1)(allow default)`）可以成功执行（退出码 0），因此这是审查方运行环境的限制，不是主机级 macOS 限制，也不是候选 A 或信任链的缺陷。本报告全部哈希、重建与验证均为不依赖 sandbox 的独立复算结果。

## 二、HEAD 与工作树核查

- 仓库 HEAD 精确等于信任锚提交 `cc0a7c69c6a46cd86d8c42cdfe52efe64a20bfe6`，分支为 `<repository-root>` 的公开重建分支，工作树在报告写入前保持干净。
- 信任锚提交相对其父提交仅新增一个文件（`release/v0.2-gate-e/trust_manifest.json`，1 行插入），是最小化、干净的锚定提交。
- git 对象库完整：无 replace refs（0 条）、无 grafts、非 shallow 克隆，`git fsck --no-dangling` 通过，无对象损坏或篡改痕迹。

## 三、信任锚提交中的固定 blob 核查

- 信任锚提交树中包含两个固定路径：
  - `release/v0.2-gate-e/trust_manifest.json`：内容 SHA-256 为 `fa770a6e65fc456c028c2f1bdd5b180b1b30556d4a6a3119ee2170b6fceb8d0f`，与任务给定值一致。
  - `outputs/Work_Buddy候选A复核_v0.2_Gate_E.md`：内容 SHA-256 为 `962c54a5265a709f370f840d757766a421b556a30754634c4e9ebc03790331be`，与任务给定值及当前工作树完全一致。
- 信任锚树中的两个 blob 内容与工作树文件逐字节一致，无漂移。

## 四、信任清单 canonical 与确定性重建核查

- 信任清单为单行 canonical JSON（排序键、紧凑分隔、无重复键），`schema_version` 为 1.0。
- 独立调用 `gate_e_trust_bytes`，从固定候选 A 证据（SHA-256 `cdfe0602f4f551351adc222f536346bec89f2d7222c8880d85a7d6dcbfabb940`）与已批准候选 A 复核报告（`verify_approved_review` 解析通过）确定性重建信任字节：重建结果与已发布 `trust_manifest.json` **逐字节一致**（17883 字节），SHA-256 均为 `fa770a6e65fc456c028c2f1bdd5b180b1b30556d4a6a3119ee2170b6fceb8d0f`。
- 已发布信任清单通过 `verify_gate_e_trust` 全量只读验证：`trust_sha256` 为 `fa770a6e…`，`implementation_commit` 为 `ae317a01…`，`expected_run_id` 为 `8db781f5…`，正式文件数 13、payload 文件数 12、文件绑定 13 项，全部一致。

## 五、信任绑定闭包核查

信任清单逐项绑定以下内容，均与候选证据、正式产物及固定输入一致：

- 实现提交：`ae317a01c5c36a7a59836665917afec4a7377125`。
- 预期 run ID：`8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084`（同时等于产物实际 run ID）。
- 候选 A 复核报告：路径 `outputs/Work_Buddy候选A复核_v0.2_Gate_E.md`，SHA-256 `962c54a5…`，13 个绑定全部一致（decision=PASS，P0/P1/P2 均为 0）。
- 产物清单：13 个正式文件，expected_counts 完整（sessions 2072、symbols 10、targets 10、no_bar_total 28、各文件行数）。
- 运行配置：config 快照（filename/payload/sha256/size）与固定配置一致。
- 冻结 v0.1 输入：v01 tag_commit `6ff6f85849c35e6475cff69f2b3caef5bf5f07f7`，release_manifest SHA-256 与固定发布清单一致。
- 项目 wheel：SHA-256 `49d574515bdc46b8cc96f0fd9c9f1f2dbf3d9f48790fe01c662e58e3ef43144e`。
- wheelhouse：34 个 wheel 条目，manifest SHA-256 `3ffd3ffd…`。
- Python：SHA-256 `7e470bc0…`；uv：SHA-256 `fc8f6670…`；uv.lock：SHA-256 `c8dfc359…`，均与候选证据 runtime 一致。
- 研究/模拟边界：`live_trading=false`、`profit_claim=false`、`research_only=true`、`simulation_only=true`。

## 六、审查过程无写入核查

- 除本报告文件外，候选 A、候选 A 复核报告、信任清单、代码、测试、配置、发布输入、wheelhouse 与仓库均未被审查修改。
- 审查仅创建 `outputs/Work_Buddy信任锚复核_v0.2_Gate_E.md` 一个文件；信任锚提交、Git 历史与工作树内容在审查期间保持原状。

## 七、公开安全核查

- 本报告全文使用仓库相对路径与占位符，不包含本机绝对路径。
- 本报告不含用户目录、设备名、私人邮箱、VPN/代理/DNS 信息、临时工作区绝对路径或任何认证信息。
- 信任清单与候选证据均未向报告引入任何个人或私有信息。

## 八、分级结论

- P0：0（无阻止性缺陷）
- P1：0（无严重缺陷）
- P2：0（无轻微缺陷；审查环境无法执行嵌套 sandbox-exec 的限制已在第一节如实披露，属审查方环境限制，不属于信任链缺陷，不计入分级）

基于以上核查，信任锚提交身份正确，信任清单 canonical 且可由固定证据与已批准复核确定性重建（逐字节一致），全量验证通过，绑定闭包完整，无审查写入，公开安全达标。复核结论为 PASS。

---

project = a-share-quant
version = v0.2
gate = E
review_kind = trust_anchor
decision = PASS
P0 = 0
P1 = 0
P2 = 0
trust_anchor_commit = cc0a7c69c6a46cd86d8c42cdfe52efe64a20bfe6
trust_sha256 = fa770a6e65fc456c028c2f1bdd5b180b1b30556d4a6a3119ee2170b6fceb8d0f
candidate_review_sha256 = 962c54a5265a709f370f840d757766a421b556a30754634c4e9ebc03790331be
implementation_commit = ae317a01c5c36a7a59836665917afec4a7377125
expected_run_id = 8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084
trust_path = release/v0.2-gate-e/trust_manifest.json
candidate_review_path = outputs/Work_Buddy候选A复核_v0.2_Gate_E.md
