# 成熟 GitHub 量化框架对比与本项目取舍

日期：2026-07-25

## 对比目的

这次对比只回答当前项目的四个工程问题：

1. 原始成交价、指标价和估值价应如何分工；
2. 分红、送转、拆股等公司行为应如何进入回测；
3. 涨跌停应使用什么参考价；
4. Buy & Hold 和 SMA 应按固定股数还是目标仓位下单。

不以 Star 数量代替代码审查，也不因为框架成熟就立即迁移。对比使用以下仓库在
2026-07-25 取得的代码快照：

| 框架 | 检查提交 | 与本项目最相关的能力 |
|---|---|---|
| Microsoft Qlib | `79633dd9506ea689e5400dea0197717b5b3d74b7` | 复权因子、交易限制、停牌、交易单位、权重转股数 |
| RQAlpha | `3503ab57932540cd36bf8375134e52c6923bf0d2` | A 股分红/拆股、T+1、昨收/涨跌停字段、目标仓位 |
| QuantConnect LEAN | `cd52034ddf55c0c9aa57264d2a148e563924100f` | 原价/复权模式、Dividend/Split 事件、目标持仓 |

## 关键对比

| 主题 | Qlib | RQAlpha | LEAN | 当前项目（修正前） | 本项目决定 |
|---|---|---|---|---|---|
| 价格流 | `$factor` 与价格配合，交易单位也随因子换算 | 行情包含 `prev_close`、`limit_up`、`limit_down`，估值和成交分阶段 | 明确区分 `Raw`、`Adjusted`、`SplitAdjusted`、`TotalReturn` | 同一份不复权 `close` 同时用于指标、估值和成交 | 保留原始 OHLC 成交/估值，新增只供指标使用的连续价格和只供规则使用的参考价 |
| 公司行为 | 主要依靠价格和 factor 约定 | 持仓模型在除息日登记应收股息、付款日入现金，并在拆股日调整数量和成本 | Dividend/Split 是独立辅助事件；Raw 模式下现金分红和拆股直接作用于组合 | 没有公司行为事件 | 新增经哈希验证的公司行为快照；先支持现金分红，遇到送股、转增、配股等未支持事件直接拒绝正式回测 |
| 涨跌停 | 支持显式 `limit_buy`/`limit_sell` 或变化阈值 | 数据包直接保存每日 `prev_close`、`limit_up`、`limit_down` | 由市场数据和证券模型提供 | 用上一根原始收盘价推算，除权日会错 | 每日生成独立 `reference_price`；除权日按公司行为公式计算，非除权日才等于上一交易日原始收盘 |
| 停牌 | 当日 `close` 缺失即不可交易 | 数据源和价格板共同判断 | 交易所时间和订阅数据共同判断 | 已用官方交易日历和缺失 bar 保守拒单 | 保留现有做法 |
| 仓位 | 可将目标权重按成交价转成数量，再按交易单位取整 | `order_target_percent` 使用账户总资产减当前市值；组合调仓再处理最小数量、步长、停牌和涨跌停 | `SetHoldings` / `CalculateOrderQuantity` 以组合价值百分比计算订单 | 名为 Buy & Hold，实际固定买 100 股；不同股价导致暴露差异巨大 | 正式基准改为目标仓位，按实际开盘价、费用和 100 股整手计算可买数量；固定 100 股只保留为工程测试概念，不再称为 Buy & Hold |
| 多资产 | 原生支持 codes/组合 | 原生账户与目标组合 | 原生 Portfolio | runner 每次只加一个 data feed | 先把 10 个标的作为“10 次独立单标的回测”；共享现金组合另立任务，不能混称组合回测 |
| 审计与复现 | 完整研究平台，侧重工作流 | 完整回测框架，数据包独立 | 完整交易引擎 | 已有 manifest、SHA-256、实现指纹、原子导出 | 保留当前窄而强的审计链，不为修三个问题整体迁移框架 |

## 代码证据

### Qlib

- [`qlib/backtest/exchange.py`](https://github.com/microsoft/qlib/blob/79633dd9506ea689e5400dea0197717b5b3d74b7/qlib/backtest/exchange.py)
  明确说明 `$close` 用于日末总价值、缺失表示停牌，`$factor` 用于交易单位换算；
  同一 Exchange 还负责涨跌停、成交价、费用和目标权重转数量。
- Qlib 的做法说明“复权价格”和“真实股数/交易单位”不能只取其一。缺 factor 时它甚至会警告
  调整价模式不支持交易单位，这正是当前项目必须显式拆分价格流的原因。

### RQAlpha

- [`position_model.py`](https://github.com/ricequant/rqalpha/blob/3503ab57932540cd36bf8375134e52c6923bf0d2/rqalpha/mod/rqalpha_mod_sys_accounts/position_model.py)
  在交易前处理股息登记、拆股和股息付款；应收股息也计入 position equity。
- [`bundle/utils.py`](https://github.com/ricequant/rqalpha/blob/3503ab57932540cd36bf8375134e52c6923bf0d2/rqalpha/data/bundle/utils.py)
  将 `prev_close`、`limit_up`、`limit_down` 作为股票和基金日线的正式字段，而不是临时从上一根
  `close` 猜测。
- [`api_stock.py`](https://github.com/ricequant/rqalpha/blob/3503ab57932540cd36bf8375134e52c6923bf0d2/rqalpha/mod/rqalpha_mod_sys_accounts/api/api_stock.py)
  的 `order_target_percent` 使用账户总价值和当前市值计算差额。
- [`order_target_portfolio.py`](https://github.com/ricequant/rqalpha/blob/3503ab57932540cd36bf8375134e52c6923bf0d2/rqalpha/mod/rqalpha_mod_sys_accounts/api/order_target_portfolio.py)
  在权重落到股数后继续执行整手、停牌、涨跌停和可卖数量检查。

### LEAN

- [`DataNormalizationMode`](https://github.com/QuantConnect/Lean/blob/cd52034ddf55c0c9aa57264d2a148e563924100f/Common/Global.cs)
  明确说明 Raw 模式下股息进入现金、拆股直接调整持仓数量；Adjusted、SplitAdjusted 和
  TotalReturn 是不同语义，不能混为一列价格。
- [`Dividend.cs`](https://github.com/QuantConnect/Lean/blob/cd52034ddf55c0c9aa57264d2a148e563924100f/Common/Data/Market/Dividend.cs)
  与 [`Split.cs`](https://github.com/QuantConnect/Lean/blob/cd52034ddf55c0c9aa57264d2a148e563924100f/Common/Data/Market/Split.cs)
  把公司行为建成独立事件，并携带 reference price。
- [`QCAlgorithm.Trading.cs`](https://github.com/QuantConnect/Lean/blob/cd52034ddf55c0c9aa57264d2a148e563924100f/Algorithm/QCAlgorithm.Trading.cs)
  的 `SetHoldings` 以组合价值百分比计算订单数量。

## 不照搬的部分

1. 现在不迁移到 Qlib、RQAlpha 或 LEAN。迁移会同时改变数据、策略、经纪商和报告口径，无法判断
   结果变化来自修错还是换引擎。
2. 现在不实现完整 A 股公司行为全集。当前 10 标的从 2018 年以来只有现金分红；v0.1 对送股、
   转增、拆股、配股、合并和证券变更全部保守拒绝。
3. 现在不做共享现金的 10 股票组合。先验证每只股票在同一规则下的独立结果，再单独设计组合层。
4. 现在不接 Qlib 或多 Agent。10 标的工程门通过后先完成风险指标与报告，再决定是否进入
   Qlib 因子研究。

## 结论

当前项目的 manifest、SHA-256 验真、实现指纹和原子导出值得保留；需要补的是成熟框架共有的
基础语义，而不是换一个更大的框架。实施顺序固定为：

1. 公司行为快照与价格三分流；
2. 除权参考价和股息会计；
3. 目标仓位基准；
4. 固定四标的回归（已完成）；
5. 内容寻址 universe 与 10 标的独立回测试点（已完成）；
6. 第 4 周风险指标和报告（已完成）；
7. 第 5 周受限策略实验与 10 日操作流程回放（已完成）；
8. 第 6 周冻结输入、离线重建和可复现发布（进行中）。
