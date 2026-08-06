# a-share-quant v0.2 交接状态

## 当前结论

- 当前 Gate：v0.2 Gate E
- 本地验收：PASS
- Work Buddy：PASS，P0=0 / P1=0 / P2=0
- Candidate B：独立完成，未触碰 Candidate A
- A/B：run ID 相同，13 份正式文件逐字节一致
- 公开发布状态：待提交、待审计标签、待推送、待 Draft PR、待 GitHub CI

完整交付记录见 `outputs/A股量化项目_v0.2_Gate_E交付与验收.md`。

## 固定信任链

| 对象 | 标识 |
|---|---|
| implementation | `ae317a01c5c36a7a59836665917afec4a7377125` |
| Candidate A review | `bb49a6d1ede126fe1098944d7efd7bbdb6dd386c` |
| trust anchor | `cc0a7c69c6a46cd86d8c42cdfe52efe64a20bfe6` |
| post-approval | `e2eef971a6b42e0a9e2ae172da5ceac646f431ef` |
| run ID | `8db781f55803b4c825b606ec3d7ae5574bbcdf0f9e481d6cec50d72f69c13084` |
| Candidate A evidence SHA-256 | `cdfe0602f4f551351adc222f536346bec89f2d7222c8880d85a7d6dcbfabb940` |
| Candidate B evidence SHA-256 | `5564272fb38333f44f140511e7f8ed28ab715a4756cdb6c3c91920a197071365` |

不要用未来的交付提交替换上述 implementation、trust anchor 或 approval 标识；它们是本次 Gate E 的固定对象。

## 验收摘要

- Gate E 聚焦测试：455 passed
- 全仓测试：1095 passed, 1 skipped
- v0.1 显式冻结重建：1 passed
- pytest node IDs：1096；SHA-256 `b11589b11443b513b77399f1c05f4962b568476aaf4e3f314b592a0a3e005926`
- Ruff、`uv lock --check`、已提交内容空白检查、源码边界与公开敏感信息检查：PASS
- 项目 wheel：正式文件与两次干净重建逐字节一致；SHA-256 `49d574515bdc46b8cc96f0fd9c9f1f2dbf3d9f48790fe01c662e58e3ef43144e`
- 正式产物：13 个文件（12 payload 加 manifest）
- 冻结输入：25 个文件；仓库、A、B 内容一致且物理独立
- 环境闭包：A/B 各 37 个相同包
- 组合证据：10 个标的、2072 sessions、20720 positions、28 no-bar
- 2026-07-24：只用于 provenance 与收尾验证，不进入决策或绩效
- 公开输入：synthetic public fixture；`public_input_deviation_lock_count=0`

## 公开历史说明

当前干净审计历史与 `origin/main` 无共同祖先。`origin/main` 是既有公开遗留历史，可能保留旧身份或旧路径，但不是 Gate E 分支祖先，也不属于本次信任链。

安全同步时采用：

1. 提交本轮最终报告与交接文件；
2. 复核工作树、公开敏感信息和提交身份；
3. 创建新的公开审计标签；
4. 创建与 Gate E 最终树内容完全相同的单父 snapshot bridge；
5. 推送候选分支并创建 Draft PR；
6. 等待 pytest、Ruff、lock、已提交内容空白检查和 wheel 构建等 GitHub CI 全部通过；
7. CI 通过前不合并 `main`，不删除原始私有审计备份。

snapshot bridge 只用于建立安全可审查的公开发布路径，不能改变 Gate E 已审计树或沿用未经验证的新结论。

## 下一步

当前唯一主线是完成上述 GitHub 安全同步。不得在同步过程中顺手引入新策略、Qlib、VectorBT、多 Agent、券商接入、实盘、网页界面或依赖升级。

安全同步完成后，仅提醒用户“只读 GitHub 量化项目借鉴调研”待办；必须等待用户发送正式调研指令，才可搜索或分析外部项目。

## 研究边界

本项目仍是研究与模拟平台：不连接券商、不自动交易、不证明盈利、不构成投资建议。Gate E 证明的是公开合成输入下的可复现和可审计，不是策略有效或真实成交可行。
