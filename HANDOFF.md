# a-share-quant v0.2 交接状态

## 当前状态

- v0.2 Gate E 已通过；本仓库的公开历史来自完成隔离核验的 clean-room 迁移。
- `main` 是正式发布分支。任何未来变更都应先通过 Pull Request、CI 和发布边界检查。
- F1C 只引入 README 与本文件的公开发布文档更新；不改变生产代码、测试、工作流、依赖、锁文件或固定审计对象。
- F1C 发布状态：该提交是 v0.2 的 docs-only 公开发布就绪记录；公开可见性、规则与安全设置必须以 GitHub 的已验证状态为准。
- 仓库公开状态、保护规则和 GitHub 安全功能应以 GitHub 当前设置为准，并在每次治理变更后重新核验。

## 固定审计关系

| 对象 | 标识 |
|---|---|
| trust anchor | `cc0a7c69c6a46cd86d8c42cdfe52efe64a20bfe6` |
| post-approval | `e2eef971a6b42e0a9e2ae172da5ceac646f431ef` |
| audit commit | `577c157235cac50e0ab721a7c845b0f0836aa15b` |
| audit tag | `v0.2-gate-e-public-audit` |
| public-release baseline before F1C docs | `378c10c07d5cd741a710b8133b4063e36604322c` |

审计标签必须继续指向 audit commit。`main` 可以在该基线之上承载经过批准的文档或工程变更，但不得重建、移动或替代 trust anchor、post-approval、audit commit 或 audit tag。

## 已完成的验证范围

- Gate E 的隔离重放与审计链已通过；该结论只覆盖公开合成输入下的可复现和可审计性。
- clean-room 迁移、正式名称切换和独立 Git 对象回读已完成；旧污染历史不属于正式仓库可达历史。
- F1A CI 基线记录为：1095 passed、1 skipped、0 failed；Ruff、`uv lock --check`、已提交内容空白检查和 wheel 构建均通过。
- 当前仓库仍应按 README 中的研究边界理解：不连接券商、不自动交易、不承诺收益，也不构成投资建议。

## 尚未完成或需要单独授权的事项

- 只读 GitHub 量化项目借鉴调研：待用户明确授权后再开始。
- 后续工程化收缩：待独立任务书和授权。
- LICENSE 决策、券商接入、实盘执行、策略扩展和依赖升级：均不在当前发布阶段范围内。

不得把后续工作与本次 docs-only 发布混合，也不得以本仓库的测试或回测记录表述已完成实盘验证。
