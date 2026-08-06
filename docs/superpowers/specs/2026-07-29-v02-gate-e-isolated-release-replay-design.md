# a-share-quant v0.2：Gate E 隔离发布复演设计

> 项目：`a-share-quant`
> 版本：`v0.2`
> Gate：`E`
> 方案：`E1 isolated release replay`
> 状态：公开脱敏实现与发布前代码门正在重建；新的 Candidate A、
> 信任锚和 Candidate B 尚未生成

## 1. 目标

使用 `release/v0.1-research/inputs/` 中经过发布清单和 SHA-256 验真的
10 标的公开合成冻结输入，在两个彼此隔离、禁止出站网络的全新
Python 3.11.15 环境中安装同一份 `a-share-quant 0.2.0` wheel，分别
运行一次共享现金 Buy & Hold 组合。

Gate E 必须证明：

- 正式 wheel 能独立安装，`aquant-portfolio` 不依赖 `PYTHONPATH`；
- 两次运行使用同一份 25 文件输入闭包，但 venv、HOME、缓存、项目根、
  输出根和输入副本互相独立；
- 两次 run ID 相同，run-ID 目录内恰好 13 个文件且原始字节完全一致；
- 外部信任锚可以反向验证候选 A，并驱动环境 B 重新计算和验真；
- 28 个合成 no-bar 缺口、失败状态、实际权重和现金拖累没有被过滤、
  替换或美化。

这只证明可复现的研究与模拟工程，不证明策略有效、可实盘成交或能够盈利。

## 2. 不做什么

- 不调用 AKShare 或任何在线行情接口；
- 不接券商，不发送真实订单；
- 不改 VPN、代理、DNS、防火墙或 macOS 全局网络设置；
- 不使用 `PYTHONPATH`、仓库源码目录或当前 `.venv` 执行正式组合；
- 不修改、移动或重建 `v0.1-research` 标签及其 25 个已声明文件；
- 不取 10 标的共同日期交集，不补 bar，不替换标的，不改参数；
- 不把候选 run ID、一次内存预检或绿色测试称为 Gate E 通过；
- 不在两个环境中分别构建 wheel；两边必须安装同一文件。

## 3. 信任根与冻结输入

正式信任根固定为：

```text
v0.1-research^{commit}
= 6ff6f85849c35e6475cff69f2b3caef5bf5f07f7

release/v0.1-research/release_manifest.json SHA-256
= 9d9ad2ed7c351a9e06d86de6b3edea2221ba6b256de072e3744b478b65ca7422

declared input file count
= 25
```

验收器必须先读取发布清单的 allowlist，逐文件检查：

- 相对路径精确一致；
- SHA-256 精确一致；
- 普通文件、单硬链接、无符号链接；
- 没有缺失或额外文件。

只复制 allowlist 声明的 25 个文件到环境 A/B，禁止递归复制整个
`inputs/` 目录。复制后每个副本重新计算 SHA-256，并证明 A/B 对应文件
不共享 inode。

## 4. 零字节 lock 边界

原始私有审计基线曾记录一个未纳入发布清单的零字节 lock 偏差；该偏差
及其隔离记录只属于私有只读备份，不进入公开历史，也不得作为新 Gate E
证据复用。当前公开合成冻结目录必须从一开始就恰好包含发布清单声明的
25 个文件，不存在需要移动或补写的 lock 文件。

不得通过宽泛忽略 `*.lock` 让其他额外文件绕过文件集合检查。

运行时，A/B 临时副本必须始终保持清单声明的恰好 25 个文件，不得
生成 `data/manifests/manifest.jsonl.lock` 或任何其他 lock、临时文件、
空目录或侧车。Gate E 正式入口使用带预期 SHA-256 的只读 manifest
加载：不创建目录、不创建 lock，并对实际消费的字节即时验真；普通
兼容入口保留原有加锁读取语义。运行前后文件集合、逐文件哈希和目录
集合任一变化均失败。

候选输入按生命周期分开生成：先只创建 A；A 完成复演、独立复核和
信任锚验证后，才允许创建 B。A/B 均只能复制清单声明的 25 个文件，
且最终必须逐文件证明 source、A、B 三方 inode 独立。发布使用
no-replace 原子重命名。任何失败暂存目录或部分发布都保留为审计
证据，不自动递归删除；`copy_partial_publication` 必须同时携带脱敏
`cause_code`。所有复制失败还必须携带仅含 basename 的
`evidence_name` 和白名单 `publication_state`，让控制器精确归属本次
失败证据，而不是猜测目录；只要携带保留或已发布证据，也必须同时
携带非空、脱敏的 `cause_code`。任何此类失败都立即阻断后续复演和
自动重试。

## 5. 机器可读正式配置

正式参数必须同时写入规格和
`configs/releases/v0.2_gate_e.json`。JSON 使用 UTF-8、键排序、紧凑
序列化和结尾换行；运行命令只能由该文件生成，不接受额外参数覆盖。

所有具有 Decimal 语义的字段必须使用 JSON 字符串，加载器拒绝 JSON
number，不经过 binary float：

```json
{
  "etf_commission_rate": "0.00025",
  "etf_minimum_commission_yuan": "5.00",
  "gross_target_weight": "0.95",
  "stock_commission_rate": "0.00025",
  "stock_minimum_commission_yuan": "5.00"
}
```

加载后直接用原字符串构造 `Decimal`，再按 canonical decimal 规则复算
费用策略 digest；`5.00` 不得被改写为 `5.0`。

固定内容：

```text
project_name = a-share-quant
project_version = 0.2.0
gate = E
strategy = buy_and_hold
initial_cash_fen = 100000000
gross_target_weight = "0.95"
max_entry_attempts = 5
signal_date = 2018-01-02
end_date = 2026-07-23
post_end_validation_date = 2026-07-24
universe_id = ef1a155c791be3f92c41c465da169c9a8c21cbc6981c01a2351f45d72441d130
calendar_id = 2a00e22557afcb6e320c09650e1fb3a55ab324fac88b006c5c03e6e7532050bc
release_manifest_sha256
= 9d9ad2ed7c351a9e06d86de6b3edea2221ba6b256de072e3744b478b65ca7422
stock_commission_rate = "0.00025"
stock_minimum_commission_yuan = "5.00"
etf_commission_rate = "0.00025"
etf_minimum_commission_yuan = "5.00"
fee_policy_digest = 6935d9e8727417370a69dd97c021514f5517b4f22107fb89b548145195dfa782
```

标的集合按 symbol 排序并精确固定为：

```text
000001
000858
510300
510500
600030
600036
600519
600900
601166
601318
```

法定费率使用成交日期查“小于等于该日期的最新生效项”：

```text
stamp_duty:
  2008-09-19 -> 0.001
  2023-08-28 -> 0.0005

transfer_fee:
  2015-08-01 -> 0.00002
  2022-04-29 -> 0.00001
```

机器配置中的全部法定费率也使用 JSON 字符串，禁止 JSON number。

配置还必须固定 10 组行情 snapshot ID、10 组公司行为 snapshot ID、
25 文件路径与 SHA-256、配置 schema、组合 schema、费用 schema、
Python 版本和 `uv.lock` SHA-256。最终文件不得含未决占位符、空哈希或
运行时默认值。

正式入口固定为：

```text
cd <A 或 B 的独立 project root>
aquant-portfolio run-config --config configs/releases/v0.2_gate_e.json
```

配置内的 manifest、公司行为 manifest 和 output 使用固定安全相对路径；
`project_root` 由受控 cwd 提供，不写入配置或 run ID。A/B 使用不同绝对
cwd，但读取字节相同、相对路径相同的配置和输入。`run-config` 只接受
`--config`，拒绝所有经济参数或输出路径覆盖；旧 `run` 模式保持兼容，
但不得用于 Gate E。

## 6. 日期与未来信息边界

- 2018-01-02 只使用当日及此前数据产生 Buy & Hold 目标；
- 首次成交只能发生在下一官方交易日开盘；
- 正式账本、绩效和审计区间截止 2026-07-23；
- 2026-07-24 必须等于 `next_session(2026-07-23)`，只用于证明日历覆盖
  和最后批次的 T+1 规则可解析；
- 2026-07-24 禁止参与信号、目标权重、指标窗口、成交、现金红利、
  日终现金/持仓/权益或正式绩效。

验收测试必须证明：

- target、attempt、fill、cash、equity、receivable、corporate action
  和 position 的经济 session 均不晚于 2026-07-23；
- `equity.csv` 最后一行是 2026-07-23；
- metrics 仅从该权益序列重算；
- 不因 2026-07-24 行情改变 2026-07-23 以前的任何经济结果。

日期字段按经济事件和计划元数据区分：

- target signal、attempt intent/execution、fill execution、cash event、
  position session、equity session、receivable `registered_date`、
  corporate action `ex_date` 和非空 `paid_date` 均不得晚于 end date；
- `lot.available_date` 以及未付 receivable 的
  `source_payable_date`、`actual_cash_date` 可以晚于 end date，它们只
  表示已发生交易或已登记应收的计划元数据；
- 允许上述计划日期等于 2026-07-24，但不得因此生成 2026-07-24 的现金
  事件、付款状态、持仓快照、权益行或绩效观察。

## 7. wheel、版本与 wheelhouse

正式 Python distribution 版本提升为 `0.2.0`；同时更新 `uv.lock`。
这不改动冻结 `v0.1-research` 标签，只消除“工程 v0.2、wheel 0.1.0”
的身份歧义。

在候选实现 commit 上只构建一次 wheel：

```text
a_share_quant-0.2.0-py3-none-any.whl
```

记录文件 SHA-256，并验证 wheel 内：

- 包含 `aquant/portfolio_cli.py`；
- `entry_points.txt` 声明
  `aquant-portfolio = aquant.portfolio_cli:main`；
- metadata 版本为 `0.2.0`；
- 依赖与锁文件一致。

正式安装不读取用户 uv 缓存。实施阶段先按 `uv.lock` 制作一个受控、
只读 wheelhouse，生成每个依赖文件的路径、版本、大小和 SHA-256 清单。
同时保留 `uv export` 产生的原始依赖锁及其 SHA-256。若上游仅发布源码
包（当前已确认 `jsonpath==0.82.2` 属于此情况），原始锁中的源码包哈希
不得冒充本机构建 wheel 的哈希；准备阶段必须另行生成当前
Python 3.11/macOS ARM64 专用的 canonical 安装锁，逐项绑定已验真 wheel
的 distribution、version 和 SHA-256。正式断网安装只读取这份安装锁，
仍强制 `--require-hashes --only-binary :all:`。原始依赖锁、安装锁和
wheelhouse manifest 三者的 SHA-256 全部进入最终证据。

wheelhouse 可以在 Gate E 核心复演前准备；一旦上述三份证据固定，环境
创建、安装、运行和验真全部在操作系统级禁止网络条件下完成。

封存信任根不得位于 macOS File Provider、云盘或会自动重写权限位的同步
目录。实施中已确认用户 `Documents` 带
`com.apple.file-provider-domain-id`，且只读目录会异步恢复为可写；
因此该位置只保留准备材料，不得作为正式 trust root。正式 wheelhouse
使用本机非同步专用目录，并在复制、安装前、安装后和延迟复查时同时校验
文件集合、内容哈希与权限位。

环境 A/B 共享只读 wheelhouse 和同一项目 wheel，但分别使用独立空
`UV_CACHE_DIR`。若 wheelhouse 缺 Python 3.11.15 所需的任一锁定依赖，
安装必须失败，禁止临时联网补包。

## 8. 双环境隔离

A/B 必须分别拥有且其根目录、可变文件和输入文件不共享 inode：

- venv；
- HOME；
- XDG cache；
- uv cache；
- 25 文件输入副本；
- project root；
- output root。

两个环境使用同一个已安装的 uv 版本和同一只读 Python 3.11.15 基础
解释器，但创建两个独立 venv。A/B 的解释器 symlink 允许共同指向这个
明确声明、哈希相同的只读基础解释器；除此之外不得共享 venv 文件。设置：

```text
PYTHONPATH = unset
PYTHONNOUSERSITE = 1
PYTHONDONTWRITEBYTECODE = 1
PIP_CONFIG_FILE = /dev/null
UV_OFFLINE = 1
UV_PYTHON_DOWNLOADS = never
UV_CACHE_DIR = distinct A/B path
XDG_CACHE_HOME = distinct A/B path
HOME = distinct A/B path
LC_ALL = C
LANG = C
TZ = Asia/Shanghai
PYTHONHASHSEED = distinct fixed values for A/B
```

通过 macOS `sandbox-exec` 为环境创建、安装、CLI 运行和验真子进程添加
`deny network*`，并在 Python 内继续使用现有 `offline_network_guard`
作为第二层。该局部进程沙箱不得修改用户 VPN 或系统网络设置。

正式运行前必须证明：

- `aquant-portfolio --help` 可直接执行；
- `aquant.__file__` 位于各自 venv 的 site-packages；
- `sys.path` 不包含仓库、用户 site-packages 或另一个环境；
- 35 个受控研究闭包发行包（34 个封存依赖加项目包）以及 2 个固定基础
  引导包 `pip`、`setuptools`，组成完整的 37 项安装 inventory；A/B 的 37
  项名称和版本必须完全一致；
- `uv pip check` 通过；
- 运行进程无法读取仓库源码路径，只能读取自己的临时根、venv 和允许的
  基础解释器/系统库。

## 9. 运行与审计文件集合

每次只通过正式 `aquant-portfolio run-config` 入口运行，输入参数完全
来自冻结 JSON；旧 `run` 不得用于 Gate E。每个 run-ID 目录必须恰好包含：

```text
artifact_manifest.json
availability.csv
cash.csv
corporate_actions.csv
equity.csv
fills.csv
lots.csv
metrics.json
orders.csv
positions.csv
receivables.csv
run.json
targets.csv
```

验收必须同时检查：

- 文件名集合与源码 `PORTFOLIO_ARTIFACT_FILES` 完全一致；
- 没有第 14 个文件、链接、子目录或未声明输出；
- `artifact_file_count` 精确为 13；
- `payload_file_count` 精确为 12，不含 `artifact_manifest.json`；
- CLI 和反向验真结果不得再使用含义不明的单一 `file_count`；若为兼容
  保留，必须明确定义为 13 并同时输出上述两个具名字段；
- 两边 run ID 相同；
- 13 个文件逐个 SHA-256 相同；
- 13 个文件原始字节直接相同，不做路径、时间或文本替换；
- 两边 `artifact_manifest.json` 原始字节相同，并能独立重建
  result digest 和 run ID。

输出根可存在源码当前原子发布机制声明的精确侧车
`.<run_id>.lock`。它必须位于 run-ID 目录外、为零字节普通单硬链接文件，
并在外部环境报告中声明；除此之外输出根不得出现任何文件或目录。

正式 13 文件禁止墙钟时间、绝对路径、HOME、缓存、venv、主机名、用户名、
PID、耗时或随机 UUID。上述原始环境证据只写入 bundle 外的独立报告；
规范化副本按字段 allowlist 生成，也不得参与 run ID 或13文件比较。

## 10. 外部信任锚

信任锚文件固定为：

`release/v0.2-gate-e/trust_manifest.json`

它必须位于两个运行包之外，至少绑定：

- 候选实现 Git commit；该 commit 不含 trust manifest；
- 项目 wheel 文件名、大小和 SHA-256；
- `uv.lock` SHA-256；
- Python 3.11.15 与 uv 的设备无关内容快照，包括版本、大小和 SHA-256；
- 仅在控制器内存中保留的 runtime inode、ctime、mtime 执行护栏，并在
  Python 创建 venv、版本探测、每个 uv 安装子命令和整段候选审计前后验证；
- uv 使用复制模式安装；安装结束后拒绝普通文件硬链接和未获批准的外部
  symlink，仅允许 venv 的 Python 链接最终指向已守卫的固定基础解释器；
- 安装后的完整 venv 移除写位并绑定每个文件、目录、symlink、内容哈希及
  inode/ctime/mtime 元数据。导入检查、CLI 运行、反向验真均在整棵 venv
  的系统沙箱只读约束下执行，并在子进程前后复核同一内存护栏；
- wheelhouse 每个文件的相对文件名、版本、大小和 SHA-256；
- v0.1 tag commit 和冻结发布清单 SHA-256；
- 完整 Gate E JSON 配置及其 SHA-256；
- expected run ID；
- 13 个文件的文件名、大小和 SHA-256；
- 预期行数、标的数、session 数和 no-bar 数；
- 研究/模拟边界。
- 固定相对路径
  `outputs/Work_Buddy候选A复核_v0.2_Gate_E.md`
  的完整字节 SHA-256、大小和全部解析绑定；
- 候选复核必须绑定实现 commit、Candidate A evidence SHA-256、expected
  run ID、`artifact_manifest.json` SHA-256 和项目 wheel SHA-256。

禁止一个未审脚本生成信任锚后立即自我通过。流程固定为：

1. 环境 A 生成候选包；
2. Codex 独立反向验真候选 A；
3. Work Buddy 使用 A 股量化与通用量化标尺复核候选 A；
4. 将获批候选身份固定到 Git 中的 trust manifest 并创建新 commit；
5. 该 trust manifest 内记录步骤 1 使用的
   `implementation_commit`，不得试图记录包含自身的 commit；
6. 将步骤 4 的 commit 作为外部 `trust_anchor_commit` 写入验收调用、
   原始日志和最终报告，不写回 trust manifest，避免 Git 自引用；
7. Work Buddy 复核
   `trust_anchor_commit:release/v0.2-gate-e/trust_manifest.json`
   和同一 commit 中的候选复核精确 Git blob，确认
   `P0=0 / P1=0 / P2=0`；
8. 将后置复核固定为
   `outputs/Work_Buddy信任锚复核_v0.2_Gate_E.md`，单独创建
   `approval_commit`，不 amend `trust_anchor_commit`；
9. B 入口只接受两个不同的 40 位小写十六进制 Git
   object ID，并用 `git cat-file -t` 确认两者精确为
   `commit`（annotated tag object SHA 不可替代）；所有 Git
   子进程强制 `GIT_NO_REPLACE_OBJECTS=1`，且发现任何
   `refs/replace` 或 `info/grafts` 都在创建 B 目录前拒绝；再验证
   `trust_anchor_commit` 是 `approval_commit` 的祖先、
   `approval_commit` 是已解析 HEAD commit 的祖先，并从两个
   commit 精确提取 trust、候选复核和后置复核；
10. 确认 approval commit 中的 trust/候选复核与 anchor 字节未变，
    并且当前 HEAD 中 trust、候选复核、后置复核三个 Git
    blob 仍与 anchor/approval 精确字节一致，然后才检查工作树
    的三个固定路径；后置复核的 commit、路径、SHA-256、
    implementation commit 和 expected run ID 全部交叉一致，
    再重新验证 A。`approval_commit` 可以就是当前 HEAD，
    因为 Git 祖先判定包含自身；但三个 HEAD blob 仍必须未变；
11. 环境 B 从空环境重算，并从同一
   `trust_anchor_commit:path` 读取 trust manifest；
12. 用 `--expected-run-id` 和信任锚验证 B；
13. 先要求 A/B 完整 37 项安装 inventory 的名称和版本逐项一致，再原始
    字节比较 A/B。

用户已授权 Work Buddy 承担后续独立询问与审查，因此步骤 3 的明确 PASS
可作为候选固定门；任何 P0/P1 或未关闭 P2 均停止流程。

Git 在这里只证明指定字节属于指定 commit，不能证明文件真的由
Work Buddy 本人或某个特定模型产生。这一身份局限必须在最终报告保留，
不得把 Git 字节归属误写成审查者身份证明。

## 11. no-bar、失败和现金拖累

公开合成冻结数据的已知 no-bar 事实：

```text
000001、000858、600030、600036、600519、600900、601166、601318: 各 3 个
510300、510500: 各 2 个
全部缺口均为单日隔离缺口，最长连续 1 个 session
合计: 28
```

验收器必须按原始交易日历与单标行情逐日重算这些缺口，不能只相信输出。
`availability.csv` 必须保留全部 28 个源缺口及 carried-session 链。

禁止：

- 取共同日期交集；
- 补造 OHLC；
- 将无 bar 改成正常可成交；
- 替换失败标的；
- 调整信号、日期、重试次数或权重；
- 把未成交预算重分配给其他标的；
- 丢弃 rejected、pending 或 expired 状态。

业务失败可以成为真实结果；文件损坏、对账错误、身份不一致、输入闭包错误
或验真失败才阻断发布。

最终报告必须使用整数分分别给出现金账本、目标分配和权益三条守恒式。
现金账本：

```text
ending_cash_fen
= initial_cash_fen
- invested_notional_fen
- paid_fees_fen
+ dividend_cash_paid_fen
```

目标名义金额分配：

```text
gross_target_notional_fen
= invested_notional_fen
+ allocation_rounding_fen
+ ordinary_lot_rounding_fen
+ fee_lot_reduction_fen
+ pending_uninvested_fen
+ expired_uninvested_fen
```

最终权益：

```text
ending_equity_fen
= ending_cash_fen
+ ending_position_market_value_fen
+ ending_receivable_fen
```

具体分项必须从 targets、orders、fills、cash、红利现金事件、receivables
和最终账本重建；没有某类失败、红利或期末未付应收时显式写 0。报告使用
“最终现金及其来源分解”，不得把全部最终现金直接命名为收益拖累。最终
现金权重和每个标的实际权重必须从最终权益、最终现金和分标的最终市值
重新计算，不能只抄 `metrics.json`。

## 12. 环境证据

外部环境报告保留原始值并另附规范化摘要，至少记录：

- OS 版本与 build、架构、CPU、内存、逻辑核、可用磁盘；
- Python 和基础解释器 SHA-256；
- uv 版本；
- wheel、wheelhouse、锁文件、配置和输入哈希；
- 已安装包清单；
- locale、TZ 和受控环境变量；
- 实际命令、退出码、阶段耗时；
- A/B project root、venv、HOME、缓存和输出根的隔离证明；
- 网络拒绝探针；
- 所有13文件和信任锚的哈希。

这些机器信息不得进入正式 run ID 或13文件。

## 13. 测试和失败处理

测试先行覆盖：

1. 冻结目录额外 lock 使严格验真失败；
2. 偏差记录和精确隔离后，25 文件逐项通过；
3. JSON 配置未知字段、缺字段、错误 symbol/ID/日期/费率、Decimal
   number 或绝对路径均失败；`run-config` 的额外覆盖参数也失败；
4. 2026-07-24 不进入任何经济行或绩效；
5. 空独立缓存且 wheelhouse 缺包时离线安装失败；
6. wheel 不是 0.2.0、入口缺失或 SHA 不符时失败；
7. A/B 环境路径或 inode 复用，或完整安装包名称与版本清单不一致时失败；
8. 出站网络探针必须失败；
9. run-ID 目录缺文件、多文件、链接、字节变化或
   `artifact_file_count/payload_file_count` 错误时失败；
10. 输出根出现未声明侧车时失败；
11. 错误 expected run ID 或信任锚任一哈希错误时失败；
12. 28 个 no-bar 被共同日期交集抹除时失败；
13. A/B 原始字节、run ID 或文件集合不同即失败；
14. Gate E 指定测试文件显式运行并全绿；
15. 全仓测试无失败，只允许既有具名 skip
    `tests/integration/test_v01_release.py::
    test_rebuilds_complete_v01_release_from_frozen_inputs`；
16. 保存 `pytest --collect-only` 的规范化 node ID 清单及 SHA-256，
    证明新增 Gate E 测试没有漏收集；
17. Ruff、`uv lock --check`、wheel 构建和 v0.1 冻结边界通过。
18. 旧七行 PASS、错候选复核、篡改复核绑定、单 commit
    自批、缺失/错绑后置复核和当前文件缺失均在创建 B 前失败。

长流程每个阶段输出固定、脱敏的进度事件；错误只返回稳定错误码，不泄露
HOME、用户名、临时目录或原始参数。

## 14. Gate E 通过条件

必须全部满足：

1. 空 lock 偏差完整记录，冻结目录恢复为严格 25 文件；
2. 机器配置完整、不可被 CLI 参数覆盖；
3. `a-share-quant 0.2.0` wheel 构建、入口和 SHA 验真通过；
4. 受控 wheelhouse 完整，两个空缓存环境离线安装通过；
5. A/B venv、HOME、缓存、输入、项目和输出隔离，完整 37 项安装
   inventory 的名称和版本一致；
6. 操作系统级与 Python 级双层断网；
7. 2026-07-24 不参与决策、账本或绩效；
8. 一个共享现金账户、一条组合权益序列、10 个分标的持仓序列；
9. 28 个 no-bar 及所有业务失败证据原样保留；
10. A/B run ID 相同；
11. 两个 run-ID 目录各自恰好 13 文件且原始字节完全一致；
12. 外部信任锚反验 A/B 全部通过；
13. 实际权重和现金拖累可从原始账本精确重建；
14. Gate E 指定测试文件显式运行并全绿；
15. 全仓测试无失败，只允许既有具名 v0.1 完整复演测试跳过；
16. `pytest --collect-only` 规范化 node ID 清单和 SHA-256 已保存，实际
    收集数必须等于清单行数且包含全部 Gate E 测试；
17. Work Buddy 设计、代码和审计包复核
    `P0=0 / P1=0 / P2=0`；
18. `trust_anchor_commit` 同时包含候选复核和 trust，独立
    `approval_commit` 包含后置复核，两级复核绑定闭包全部通过；
19. 最终报告完整披露环境、命令、输入、哈希、失败、研究边界和
    Git 不能证明 Work Buddy 真实身份的局限。

任一条件不满足时，保留候选、日志和失败证据，Gate E 状态为 BLOCKED；
不得换标的、调参或修改输出后重试。
