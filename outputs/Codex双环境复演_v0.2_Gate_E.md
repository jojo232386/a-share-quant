# Codex 双环境复演报告（v0.2 Gate E）

- 项目：a-share-quant
- 版本：v0.2
- Gate：E
- 复演类型：Candidate A / Candidate B 隔离发布复演
- 复演日期：2026-08-06
- 结论：PASS

## 一、对象与身份绑定

本次复演绑定同一套公开重建实现、已批准的 Candidate A 及其信任链：

| 对象 | 标识 |
|---|---|
| implementation commit | `ae317a01c5c36a7a59836665917afec4a7377125` |
| Candidate A review commit | `bb49a6d1ede126fe1098944d7efd7bbdb6dd386c` |
| trust anchor commit | `cc0a7c69c6a46cd86d8c42cdfe52efe64a20bfe6` |
| post-approval commit | `e2eef971a6b42e0a9e2ae172da5ceac646f431ef` |
| expected run ID | `8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084` |
| Candidate A evidence SHA-256 | `cdfe0602f4f551351adc222f536346bec89f2d7222c8880d85a7d6dcbfabb940` |
| Candidate B evidence SHA-256 | `5564272fb38333f44f140511e7f8ed28ab715a4756cdb6c3c91920a197071365` |
| project wheel SHA-256 | `49d574515bdc46b8cc96f0fd9c9f1f2dbf3d9f48790fe01c662e58e3ef43144e` |

Candidate A 证据位于 `<candidate-a-root>/candidate-a-evidence.json`，Candidate B 证据位于 `<candidate-b-root>/candidate-b-evidence.json`。这些正式运行证据保存在仓库外的隔离根中；本报告只记录公开安全的内容哈希和复核结论。

## 二、隔离复演方法

Candidate B 由 Candidate A 已安装的正式 CLI 触发，在独立工作根、独立虚拟环境、独立项目副本和独立输出目录中完成。复演使用同一份已验真的项目 wheel、sealed wheelhouse、固定配置、冻结输入和信任锚，不通过源码目录注入导入路径，也不复用 Candidate A 的可变缓存、中间产物或工作目录。

复演控制器在运行前后执行以下闭环检查：

- 验证 trust anchor 与 post-approval 的提交关系和内容绑定；
- 验证正式 wheel、安装锁、wheelhouse、配置和 25 个冻结输入；
- 验证 Candidate B 的 37 个已安装包与 Candidate A 完全一致；
- 验证两次运行的 run ID、语义证据和正式产物；
- 验证 Candidate A 工作区在 Candidate B 全流程中未被写入或清理；
- 验证 Candidate B 未通过硬链接复用 Candidate A 或仓库冻结输入。

## 三、双环境一致性结果

### 3.1 身份与环境

- Candidate A 与 Candidate B 的 run ID 均为 `8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084`。
- 两个环境各安装 37 个包；名称与版本集合完全一致。
- 两个环境均绑定 implementation commit `ae317a01c5c36a7a59836665917afec4a7377125`。
- Candidate B 通过已批准 trust anchor 和 post-approval 进入重放，未创建新的信任锚。

### 3.2 正式产物逐字节比较

两个环境各生成固定的 13 个正式文件，其中 12 个 payload 文件加 `artifact_manifest.json`。复核结果为：

- 文件名集合完全一致；
- 没有额外未声明输出；
- 13 个文件逐文件大小一致；
- 13 个文件逐文件 SHA-256 一致；
- 13 个文件逐字节一致；
- `artifact_manifest.json` 自身也在比较范围内。

因此，本次复演不是仅比较汇总指标或 run ID，而是验证了完整正式输出集合的字节级确定性。

### 3.3 输入独立性与 Candidate A 零触碰

- 仓库冻结输入、Candidate A 输入副本和 Candidate B 输入副本共三方的 25 个文件内容 SHA-256 逐项相同。
- 同一逻辑输入在三方的设备号与 inode 组合彼此独立，不存在硬链接共享。
- Candidate B 运行前后对 Candidate A 工作区的文件、目录、修改时间、状态变化和内容快照复核通过。
- Candidate B 未在 Candidate A 工作区创建临时文件、缓存、锁文件或目录，也未对其执行清理操作。

结论：Candidate B 独立完成，Candidate A 工作区零触碰验证通过。

## 四、语义与会计核验

Candidate A 与 Candidate B 的 counts、accounting、business evidence 和 research boundary 逐项一致：

- 标的数：10；
- sessions：2072；
- positions 行数：20720；
- 冻结输入文件数：25；
- 正式文件数：13；
- no-bar：28，其中 available 为 20692、no_bar_unavailable 为 28；
- 10 个目标与 10 笔订单均保留在正式证据中，未过滤失败或现金拖累证据。

会计金额单位均为分，以下恒等式在两个候选中均通过：

```text
期末现金
= 初始现金 - 已投入名义 - 已付费用 + 已付红利现金
= 100,000,000 - 94,075,000 - 25,028 + 314,000
= 6,213,972
```

```text
期末净值
= 期末现金 + 期末持仓市值 + 期末应收
= 6,213,972 + 168,992,200 + 0
= 175,206,172
```

```text
总目标名义
= 已投入 + 配置取整 + 整手取整 + 费用扣手 + 待投 + 过期未投
= 94,075,000 + 0 + 925,000 + 0 + 0 + 0
= 95,000,000
```

证据中 `cash_identity_verified`、`net_asset_identity_verified` 和 `allocation_identity_verified` 均为 true。

正式信号日为 2018-01-02，正式绩效截止日为 2026-07-23。冻结输入覆盖到
2026-07-24，但该日只承担 provenance 与收尾完整性验证；它未进入信号、
目标权重、指标窗口、订单、成交、现金、持仓、权益或正式绩效文件。公开源、
Candidate A 与 Candidate B 从开始到结束均为精确 25 份输入，公开链没有需要
迁移或豁免的额外 lock：`public_input_deviation_lock_count = 0`。

## 五、发布门验证

在同一 implementation commit 上取得以下新鲜验证结果：

| 检查 | 结果 |
|---|---|
| Gate E 聚焦测试 | 455 passed |
| 全仓测试 | 1095 passed, 1 skipped |
| v0.1 显式重建 | 1 passed |
| Ruff | PASS |
| `uv lock --check` | PASS |
| 已提交内容空白检查 | PASS |
| 源码边界与公开敏感信息检查 | PASS |
| wheel 确定性重建 | 两次重建及正式 wheel 的 SHA-256 均为 `49d574515bdc46b8cc96f0fd9c9f1f2dbf3d9f48790fe01c662e58e3ef43144e` |

## 六、研究边界

本次使用的是公开确定性合成夹具（synthetic public fixture），不是交易所真实历史行情。复演只证明：在冻结输入、固定实现和隔离环境下，研究与模拟流水线能够确定性重建并通过审计。

本报告不证明：

- 策略存在可持续 Alpha 或未来盈利能力；
- 合成数据结果可以代表真实 A 股市场；
- 日线 OHLC 可以还原盘口排队、成交量约束或真实可成交性；
- 系统已连接券商、能够自动交易或适合投入真实资金；
- 10 个试点标的代表全市场或构成投资建议。

## 七、结论

Candidate B 已在独立环境完成受信重放。A/B run ID 相同，37 个包一致，25 个输入内容一致且物理独立，13 个正式文件逐字节一致，Candidate A 工作区零触碰，会计与业务证据一致。Codex 双环境复演结论为 **PASS**。

---

project = a-share-quant
version = v0.2
gate = E
review_kind = dual_environment_replay
decision = PASS
implementation_commit = ae317a01c5c36a7a59836665917afec4a7377125
candidate_a_evidence_sha256 = cdfe0602f4f551351adc222f536346bec89f2d7222c8880d85a7d6dcbfabb940
candidate_b_evidence_sha256 = 5564272fb38333f44f140511e7f8ed28ab715a4756cdb6c3c91920a197071365
expected_run_id = 8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084
artifact_files_byte_identical = true
candidate_a_zero_touch = true
