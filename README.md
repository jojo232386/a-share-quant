# A 股量化研究与模拟平台

这是一个面向 A 股普通主板股票和境内股票型宽基 ETF 的量化研究与回测项目。项目关注可复现的数据处理、交易规则建模、回测和审计流程；它不是预测涨跌或自动下单系统。

## 发布状态与审计基线

`main` 承载 v0.2 的公开发布文档。仓库是否已公开以 GitHub 显示的 repository visibility 为准；本次文档更新仅说明发布准备状态，不代表实盘能力或任何收益结论。

固定审计标签为 `v0.2-gate-e-public-audit`，其目标提交为 `577c157235cac50e0ab721a7c845b0f0836aa15b`。公开审计材料位于仓库内的 [v0.2 Gate E 交付与验收](outputs/A股量化项目_v0.2_Gate_E交付与验收.md)。

## 能力边界

- 支持受明确范围约束的 A 股研究、数据质量检查和离线回测验证。
- 使用公开的确定性合成夹具验证发布链路；它们不是真实行情，也不构成实时数据服务。
- 不连接券商、不提交订单、不执行自动交易。
- 不证明策略盈利、Alpha 有效、真实成交可行或适用于全部市场环境。
- 对未支持的证券、历史时期或公司行为，项目应拒绝近似运行，而不是把结果包装为有效结论。

本项目仅供研究和工程验证使用，不构成投资建议，也不承诺收益。它当前不等于可直接连接券商的实盘系统；使用者仍须自行承担数据、模型、执行和市场风险。

## 安装与最小运行

需要 Python 3.11 与 `uv`。在仓库根目录执行：

```bash
uv sync --frozen
uv run --no-sync aquant-backtest --help
```

常用命令行入口还包括：

```bash
uv run --no-sync aquant-data --help
uv run --no-sync aquant-report --help
uv run --no-sync aquant-experiment --help
uv run --no-sync aquant-release --help
uv run --no-sync aquant-portfolio --help
uv run --no-sync aquant-gate-e --help
```

仓库包含确定性生成的 v0.1 合成发布夹具。`./scripts/verify_v01.sh` 只用于冻结输入的离线重建验证；它不下载或更新行情，也不以用户级配置、缓存或本机已有结果作为输入。

## 测试与构建

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv lock --check
uv build
```

这些检查验证代码、锁文件和构建的当前状态；它们不构成投资表现、可交易性或未来结果的保证。

## 进一步阅读

- [研究范围](docs/scope.md)
- [支持矩阵](docs/support_matrix.md)
- [A 股执行规则](docs/a_share_execution_rules.md)
- [数据质量验收](docs/data_quality_acceptance.md)
- [Research Loop v1](docs/research_loop_v1.md)
- [已知限制](docs/known_limitations.md)
- [风险治理与 Blocker/Deferred 登记册](docs/engineering/risk_governance.md)
