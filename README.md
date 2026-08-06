# A 股量化研究与模拟平台

这是一个面向 A 股普通主板股票与境内股票型宽基 ETF 的量化**研究与模拟**项目。它的重点是可复现的数据、规则、回测与审计流程，而不是预测涨跌或自动交易。

## 公开基线状态

- `v0.1-research`：研究级单标的基础能力与离线验证代码。
- `v0.2 Gate E`：干净公开基线已完成 Candidate A/B 隔离重放、信任锚、后置审批和独立只读复核；本地验收为 **PASS（P0=0 / P1=0 / P2=0）**。
- GitHub 发布仍待审计标签、候选分支推送、Draft PR 和 CI；在这些步骤完成前，不表述为已经公开发布或已经合并 `main`。

本仓库只记录可公开的代码、配置、文档和验证方法。候选运行目录、缓存、虚拟环境、用户目录和未经公开审计的本地证据均不属于发布内容。

完整身份、哈希、A/B 比较、会计闭包和研究边界见 [v0.2 Gate E 交付与验收](outputs/A股量化项目_v0.2_Gate_E交付与验收.md)。

## 快速开始

需要 Python 3.11 与 [uv](https://docs.astral.sh/uv/)。在仓库根目录执行：

```bash
uv sync --frozen
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv build
```

常用命令行入口如下：

```bash
uv run --no-sync aquant-data --help
uv run --no-sync aquant-backtest --help
uv run --no-sync aquant-report --help
uv run --no-sync aquant-experiment --help
uv run --no-sync aquant-release --help
uv run --no-sync aquant-portfolio --help
uv run --no-sync aquant-gate-e --help
```

仓库已包含确定性生成的合成 v0.1 发布夹具，可执行：

```bash
./scripts/verify_v01.sh
```

该命令只用于冻结输入的离线重建验证；不下载或更新行情，也不以本机已有的 `data/`、`outputs/`、缓存或用户级配置作为输入。

## Gate E 如何验证

Gate E 的公开通过标准是：从固定实现与冻结输入独立生成 Candidate A；完成独立代码/证据复核、信任锚和后置审批；再在完全独立的 Candidate B 环境重放。两者必须得到相同 run ID 和相同的正式审计文件字节内容，并验证 Candidate B 从未触碰 Candidate A 工作区。

最终通过记录已列出实现提交、输入闭包、环境约束、产物哈希、会计恒等式与审查结论。Gate E 的本地复现门已经完成；远端 Draft PR 与 GitHub CI 属于后续安全同步步骤。

## 研究边界

- 不连接券商、不提交订单、不执行自动交易。
- 不证明策略盈利、Alpha 有效、真实成交可行，或可用于实盘。
- 日线 OHLC 无法验证开盘时的盘口、排队顺序或成交量；保守拒单不是对真实成交的还原。
- 10 个试点标的仅用于工程验证，不代表全市场、历史成分股或投资建议。
- 公开冻结输入是确定性合成夹具，不是真实行情；2074 个日历日期中，
  2072 个进入正式组合区间，边界日仅用于信号或收尾验证。
- 未支持的证券、历史阶段和公司行为必须拒绝运行，而不是套用近似规则继续计算。

详情见 [研究范围](docs/scope.md)、[支持矩阵](docs/support_matrix.md)、[A 股执行规则](docs/a_share_execution_rules.md)、[数据质量验收](docs/data_quality_acceptance.md) 与 [已知限制](docs/known_limitations.md)。
