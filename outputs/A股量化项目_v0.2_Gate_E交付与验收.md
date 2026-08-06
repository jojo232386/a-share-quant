# A 股量化项目 v0.2 Gate E 交付与验收

```text
project = a-share-quant
version = v0.2
gate = E
research_only = true
simulation_only = true
profit_claim = false
live_trading = false
```

## 一、验收结论

v0.2 Gate E 公开重建基线完成了 Candidate A、独立复核、信任锚、后置审批和 Candidate B 隔离重放。Candidate A 与 Candidate B 的 run ID 相同，13 份正式文件逐字节一致，Candidate B 未触碰 Candidate A 工作区。Codex 自审与 Work Buddy 最终独立只读复核均为 **PASS**，最终分级为：

- P0：0
- P1：0
- P2：0

Work Buddy 披露的 P3 仅为其审查执行环境不能运行嵌套 `sandbox-exec`；Codex 控制器在正常环境完成对应测试。该限制不影响 Candidate A/B、正式产物或信任链，也不改变 Gate E 结论。

## 二、固定身份与证据

| 对象 | 固定标识 |
|---|---|
| implementation commit | `ae317a01c5c36a7a59836665917afec4a7377125` |
| Candidate A review commit | `bb49a6d1ede126fe1098944d7efd7bbdb6dd386c` |
| trust anchor commit | `cc0a7c69c6a46cd86d8c42cdfe52efe64a20bfe6` |
| post-approval commit | `e2eef971a6b42e0a9e2ae172da5ceac646f431ef` |
| expected run ID | `8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084` |
| Candidate A evidence SHA-256 | `cdfe0602f4f551351adc222f536346bec89f2d7222c8880d85a7d6dcbfabb940` |
| Candidate B evidence SHA-256 | `5564272fb38333f44f140511e7f8ed28ab715a4756cdb6c3c91920a197071365` |
| Candidate A 复核报告 SHA-256 | `962c54a5265a709f370f840d757766a421b556a30754634c4e9ebc03790331be` |
| trust manifest SHA-256 | `fa770a6e65fc456c028c2f1bdd5b180b1b30556d4a6a3119ee2170b6fceb8d0f` |
| trust anchor 复核报告 SHA-256 | `b34da03a1b4bc970a17712157861d1ce33e5d1e6d3e5c05eacbb441f5257eb61` |
| release config SHA-256 | `1794ed454604d77dacdd9bb87b778721afd12cdbe7354f90a6b5d38dadd49935` |
| project wheel SHA-256 | `49d574515bdc46b8cc96f0fd9c9f1f2dbf3d9f48790fe01c662e58e3ef43144e` |
| wheelhouse manifest SHA-256 | `3ffd3ffd12b5136193aba33350e77e8598126c7092d2a57c9a47ca71a4f63bc0` |
| install lock SHA-256 | `11a643bb9c4d9dee1934b732c6649f99133ab6f459c789995e89ee4a36bbf29b` |
| `uv.lock` SHA-256 | `c8dfc359f40afde9849f7704dafe5449efe47bdef55fd7e29da4ef35214ae712` |
| artifact manifest SHA-256 | `2f2e749403bec620687fd6849bd4f92b0731e9b6dc4594ab5fbe180afdabb110` |
| pytest node ID 清单 | 1096 行；SHA-256 `b11589b11443b513b77399f1c05f4962b568476aaf4e3f314b592a0a3e005926` |
| Work Buddy 最终复核报告 SHA-256 | `0a87551842ed602b87adb079c42dcbd5a9508333500e06d9e3fe50ca6f2a47c2` |

`release/v0.2-gate-e/trust_manifest.json` 对配置、25 份冻结输入、13 份正式文件、项目 wheel、wheelhouse、Python、uv、`uv.lock`、v0.1 冻结基线和研究边界逐项绑定。Candidate A/B 的正式运行证据位于仓库外的隔离审计根；本仓库只记录公开安全的内容哈希、验证方式和结论。

## 三、隔离发布复演

Candidate B 通过 Candidate A 已安装的正式 CLI，在独立工作根、独立虚拟环境、独立项目副本和独立输出目录中运行。它使用已批准的信任锚、后置审批、同一正式 wheel、sealed wheelhouse、固定配置和冻结输入，不通过源码目录注入导入路径，也不复用 Candidate A 的缓存或可变中间产物。

复演取得以下闭包：

- 两个候选各安装 37 个名称与版本完全相同的包；
- 两个候选各生成 13 份正式文件，即 12 份 payload 加 `artifact_manifest.json`；
- 两边文件名集合完全相同，无额外未声明输出；
- 13 份正式文件逐文件大小、SHA-256 和原始字节完全一致；
- 仓库输入、Candidate A 输入和 Candidate B 输入三方各有 25 份文件，内容哈希逐项一致，设备号与 inode 组合彼此独立；
- Candidate B 未在 Candidate A 工作区创建、修改或清理文件、目录、缓存、锁文件或临时文件；
- 项目正式 wheel、两次干净重建 wheel 三方逐字节一致，SHA-256 均为 `49d574515bdc46b8cc96f0fd9c9f1f2dbf3d9f48790fe01c662e58e3ef43144e`。

结论：`a_b_byte_identical = true`，`candidate_a_zero_touch = true`。

## 四、组合证据、权重与现金

两个候选的业务语义完全一致：

- 10 个标的：8 只主板股票和 2 只境内股票型宽基 ETF；
- 2072 个正式 sessions；
- 20720 行 positions；
- 20692 个 available 记录和 28 个 `no_bar_unavailable` 记录；
- 10 个目标和 10 笔订单均保留在正式证据中，没有过滤失败类别、缺行情或现金拖累；
- 初始资金为 100,000,000 分，总目标权重为 0.95，对应目标名义 95,000,000 分；
- 实际投入 94,075,000 分，整手取整残差 925,000 分；
- 期末 10 个标的权重与现金权重合计严格为 1，并与证据中的 `final_symbol_weights` 和 `final_cash_weight` 逐位一致。

三项会计恒等式均以“分”为整数单位独立复算通过：

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

`cash_identity_verified`、`net_asset_identity_verified` 和 `allocation_identity_verified` 均为 true。

期末现金权重为 `0.03546662728297037389755881431`；10 个标的期末权重如下，
与现金权重合计严格为 1：

| 标的 | 期末权重 |
|---|---:|
| 000001 | 0.1438705024615228737489909887 |
| 000858 | 0.06799418002237957690211963537 |
| 510300 | 0.09417305230548613321681384603 |
| 510500 | 0.1111262221972408597569268279 |
| 600030 | 0.107073853539817079046735865 |
| 600036 | 0.1039175720362179935076716361 |
| 600519 | 0.07837908815221418113055971567 |
| 600900 | 0.1165027451201890307836872322 |
| 601166 | 0.07268237102971463813500816626 |
| 601318 | 0.06881378585224725987392727238 |

## 五、数据与时间边界

本次公开输入是确定性合成夹具（synthetic public fixture），不是交易所真实历史行情。公开源、Candidate A 和 Candidate B 始终各含精确 25 份输入，不存在需要迁移或豁免的额外 lock：

```text
public_input_deviation_lock_count = 0
```

正式信号日为 2018-01-02，正式绩效截止日为 2026-07-23。冻结输入覆盖到 2026-07-24，但 2026-07-24 只用于 provenance 与收尾完整性验证，未进入信号、目标权重、指标窗口、订单、成交、现金、持仓、应收、权益或正式绩效文件。

28 个 no-bar 记录按标的保留并进入审计，没有通过共同日期交集或结果筛选被抹除。该证据只说明缺行情处理和组合流水线可审计，不证明日线 OHLC 能还原盘口排队、成交量约束或真实可成交性。

## 六、发布门结果

| 验证门 | 结果 |
|---|---:|
| Gate E 聚焦测试 | 455 passed |
| 全仓测试 | 1095 passed, 1 skipped |
| v0.1 显式冻结重建 | 1 passed |
| Ruff | PASS |
| `uv lock --check` | PASS |
| 已提交内容空白检查 | PASS |
| 源码边界与公开敏感信息检查 | PASS |
| wheel 确定性重建 | 正式 wheel 与两次重建逐字节一致 |
| Work Buddy 最终独立只读复核 | PASS；P0=0 / P1=0 / P2=0 |

唯一跳过项是默认模式下的 v0.1 发布集成测试；在显式打开冻结重建门后，该测试单独取得 1 passed。

事实源：

- `outputs/Codex双环境复演_v0.2_Gate_E.md`
- `outputs/Codex自审_v0.2_Gate_E.md`
- `outputs/Work_Buddy代码与审计包复核_v0.2_Gate_E.md`
- `outputs/pytest_nodeids_v0.2_Gate_E.txt`

## 七、研究与发布边界

Gate E 的 **PASS** 只证明固定实现、固定合成输入和隔离环境下的工程可复现性与审计闭包。它不证明：

- 策略存在稳定 Alpha、未来收益或可持续盈利能力；
- 合成夹具能代表真实 A 股市场；
- 日线价格可以证明真实盘口成交、容量或冲击成本；
- 系统已连接券商、能自动下单或适合投入真实资金；
- 10 个试点标的代表 A 股全市场或构成投资建议。

系统边界保持 `research_only=true`、`simulation_only=true`、`profit_claim=false`、`live_trading=false`。

## 八、公开历史与后续发布

当前干净审计历史与 `origin/main` 没有共同祖先。`origin/main` 是既有公开遗留历史，可能仍保留旧身份或旧路径，但它不是本 Gate E 公开重建分支的祖先，也未进入本次信任链或正式产物。

该差异不是 Gate E 的实现或复现缺陷。公开同步阶段应先创建审计标签，再以树内容完全相同的单父 snapshot bridge 建立可审查的 Draft Pull Request；在 CI 通过前不得合并 `main`。桥接只解决 GitHub 审查与发布路径，不得改写或静默复用本报告固定的 Gate E 身份。

## 九、最终判断

v0.2 Gate E 的本地公开重建验收结论为 **PASS**。Candidate B 独立完成，A/B 正式产物逐字节一致，Candidate A 零触碰，业务与会计证据闭合，公开输入和研究边界已如实披露。

Git 提交、审计标签、远端推送、Draft PR 和 GitHub CI 属于下一步“安全同步”发布流程；在这些步骤完成前，不得表述为已经公开发布或已经合并 `main`。
