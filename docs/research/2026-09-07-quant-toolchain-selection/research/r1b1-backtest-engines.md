## 预算说明

本轮共用 10 次工具调用（1 次工具加载 + 6 次搜索 + 3 次页面抓取），按硬约束停止搜索。**重要局限**：NautilusTrader 官方 backtesting 概念页抓取只返回了索引页（子页 "Bar execution / Fill models / Accounts and margin" 未抓到），RELEASES.md 抓取只返回了 GitHub 壳页。因此 Nautilus 细节部分 = 搜索摘要（已验证）+ 本人先验（标注 `[先验，本轮未验证]`），第 2 轮需直接抓取子页与 `api_reference/config` 页核实。

---

## 1. 候选表

### 1.1 总览

| 候选 | 最新版本/日期 | 许可证 | 维护状态 | 能否直接消费外部订单计划（非策略回调） | 文档 | 主要疑虑 |
|---|---|---|---|---|---|---|
| **NautilusTrader BacktestEngine** | 1.227.0 / 2026-05（题设采信；Releases 页存在 `[Releases, https://github.com/nautechsystems/nautilus_trader/releases, 2026]`） | LGPL-3.0 `[先验]` | 活跃（28.5k stars，RELEASES.md 8k 行持续更新 `[GitHub 页面元数据, 2026-09]`） | **不能直接**。订单必须经 `Strategy.submit_order`/`OrderList`；但可写一个**无信号逻辑的"计划回放壳策略"**（按时钟定时器/自定义 Data 事件把计划里的绝对价格订单原样提交）`[先验]` | https://nautilustrader.io/docs/latest/concepts/backtesting/ ; https://nautilustrader.io/docs/nightly/api_reference/config/ | bar 级成交假设是"tick 化 OHLC"而非真实路径；资金费率在回测账户里是否结算未见公开证据；MARK_PRICE 触发在 SimulatedExchange 是否生效未验证 |
| **vectorbt (开源)** | 0.2x 系列，2025 年更新缓慢 `[先验]` | Apache-2.0 `[先验]` | 低活跃，作者精力在 PRO | 部分：`from_orders` 可吃外部订单表，但无挂单生命周期（accepted/working/cancel）概念 | https://vectorbt.dev/ | 向量化、无订单状态机；同 bar SL 与入场问题是已知社区争议 `[Discussion #188, https://github.com/polakowo/vectorbt/discussions/188]` |
| **vectorbt PRO** | 持续发布（2025-2026）`[Portfolio 页, https://vectorbt.pro/features/portfolio/]` | 专有/付费（GitHub Sponsor 授权）`[先验]` | 活跃 | 部分：PRO 有限价单类型、`limit_tif`、stop ladder（多档 TP）`[先验]`；仍是"信号/订单表 → 向量模拟"，无 post-only/IOC/mark price | https://vectorbt.pro/features/portfolio/ | 闭源、审计困难；OHLC 内路径靠 `ohlc_stop_choice_nb` 启发式 |
| **backtrader（原版）** | 1.9.78.123（PyPI 停更多年）`[PyPI, https://pypi.org/project/backtrader/]` | GPL-3.0 `[先验]` | **官方不维护**："officially not maintained anymore" `[Backtrader maintenance, https://community.backtrader.com/topic/2466/backtrader-maintenance]`；分叉 backtrader2 / cloudQuant/backtrader / smalinin/backtrader_next 在维护 `[GitHub, 2025-2026]` | 不能直接；需 Strategy.next() 回调下单 | https://www.backtrader.com/docu/ | 无永续/资金费率/mark price 概念；bar 内成交顺序为固定启发式 |
| **hftbacktest** | 2025 持续发布（Rust+Python）`[README, https://github.com/nkaz001/hftbacktest]` | MIT `[先验]` | 活跃 | 部分：可编程回调；订单队列位置建模最真 | https://hftbacktest.readthedocs.io/ | **数据门槛不匹配**："requires input of Tick-by-Tick full order book and trade feed data"，且"free ... not available"；Binance Futures 需自建采集 + 初始快照 `[Data Preparation, https://hftbacktest.readthedocs.io/en/latest/tutorials/Data%20Preparation.html]`。1m K 线无法驱动 |
| **freqtrade backtesting** | 月度版本（2025.x）`[先验]` | GPL-3.0 `[先验]` | 非常活跃 | 不能直接；需 IStrategy 回调。但 futures 模式回测**支持资金费率与 mark 蜡烛下载** `[先验，需核 docs/backtesting "futures"]` | https://www.freqtrade.io/en/stable/backtesting/ | 限价单假设粗（价格落在蜡烛范围内即成交）；多档 TP 需 `adjust_trade_position` 自写；GPL 传染 |
| **Backtesting.py** | 0.6.5 / 2025-07-30；12 月仍有更新 `[Grokipedia 摘要, https://grokipedia.com/page/Backtestingpy]` | AGPL-3.0 `[先验]` | 活跃（轻量） | 不能直接；`Strategy.next()` 下单，但 `buy(limit=, stop=, sl=, tp=)` 一行可表达 | https://kernc.github.io/backtesting.py/ | 单资产、无永续概念、无 post-only/IOC；AGPL |
| **自研事件驱动模拟器** | — | — | — | **天然支持**（输入即订单计划） | 参考实现见 §3 | 需要自己实现并审计全部规则，但样本仅数百个计划、不求性能，成本可控 |

### 1.2 逐项建模需求支持矩阵

图例：✅支持 / 🟡部分 / ❌不支持 / ❓未知（本轮未查到）

| 需求 | Nautilus | vbt 开源 | vbt PRO | backtrader | hftbacktest | freqtrade | Backtesting.py | 自研 |
|---|---|---|---|---|---|---|---|---|
| 订单状态 accepted/rejected/working/partial/filled/canceled | ✅ 完整 FSM（含 PENDING_*、事件流可审计）`[Orders 概念页, https://nautilustrader.io/docs/latest/concepts/orders/]` | ❌ | 🟡（限价单有 pending/reject 概率）| 🟡（Submitted/Accepted/Partial/Completed/Canceled/Rejected 状态存在）`[先验]` | ✅ | 🟡（订单对象有状态但回测中简化） | 🟡（Order 对象，无 partial） | ✅（自定义） |
| 止损触发基准 mark vs last | ❓ `TriggerType.MARK_PRICE` 枚举存在 `[Orders 页]`；SimulatedExchange 是否按 mark 数据触发**未验证**（先验：需喂 `MarkPriceUpdate` 数据，matching engine 对 MARK_PRICE 的处理是 2025 新增或不完整） | ❌ | ❌ | ❌ | 🟡（可自写） | 🟡（futures 回测有 mark 蜡烛用于清算价，止损触发仍按 last）`[先验]` | ❌ | ✅ |
| tick/step/min notional 过滤 | ✅ Instrument 定义含 price_increment/size_increment/min_notional，提交时校验 `[先验]` | ❌ | 🟡（size_granularity）| ❌ | 🟡 | 🟡（市场 precision 来自 ccxt） | ❌ | ✅ |
| post-only 拒单 | 🟡 Binance 适配器有 "post-only rejection" 单测 `[RELEASES 摘要, 2026]`；SimulatedExchange 会拒绝穿价的 post_only `[先验]`——bar 模式下 bid=ask，需核实 | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ |
| IOC 余量取消 | 🟡 TimeInForce.IOC 存在 `[Orders 页]`；回测撮合行为未验证 | ❌ | 🟡（limit_tif）| ❌ | ✅ | ❌ | ❌ | ✅ |
| reduce_only | ✅ 语义："will only ever reduce an existing position ... never open a new position" `[Orders 页]`；`use_reduce_only` 回测配置 `[先验]` | ❌ | ❌ | ❌ | 🟡 | 🟡 | ❌ | ✅ |
| 同 bar SL/TP 同触的上下界 | 🟡 `bar_adaptive_high_low_ordering`（按开盘更接近高/低决定 H/L 先后）`[Config 页存在该项，https://nautilustrader.io/docs/nightly/api_reference/config/; 语义为先验]`；无"上下界双跑"内建，需两次跑（强制 H 先/L 先） | 🟡 | 🟡 `ohlc_stop_choice_nb` "takes into account the whole bar" `[nb 页, https://vectorbt.dev/api/signals/nb/]` | 🟡 固定启发式 | n/a | 🟡 悲观优先（止损先）`[先验]` | 🟡 SL 先于 TP 检查 `[先验]` | ✅（显式跑乐观/悲观两界） |
| 手续费 / 资金费率分列 | ✅ 手续费 FeeModel（maker/taker）分列于 commissions；❓ 资金费率：`FundingRateUpdate` 有 `interval` 字段（面向 Hyperliquid/Binance live）`[RELEASES 摘要]`，**回测账户结算未见证据** | ❌ | 🟡 手续费；❌ funding | 🟡 手续费 | 🟡 自写 | ✅ futures 回测含 funding fee `[先验]` | 🟡 手续费 | ✅ |
| 多档限价入场 + 多档 TP + 到期撤单 + 移损 | ✅ OrderList/OCO/OUO + `expire_time`(GTD) + modify_order `[先验；support_contingent_orders 配置在 Config 页]` | ❌ | 🟡 stop ladder | 🟡 bracket + valid | ✅ | 🟡 | 🟡 | ✅ |
| 1m K 线可驱动 | ✅ bar_execution | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |

### 1.3 NautilusTrader 重点核查项（半预算）结论

| 核查点 | 结论 | 证据/可信度 |
|---|---|---|
| bar 数据下的成交假设 | bar 被拆成 O→H→L→C 四个"tick"依序进入撮合；限价单在 tick 价格触及/穿越时以限价成交，止损在触及触发价时转市价 | 概念页存在 "Bar execution: Use bars, OHLC sequencing, and bar timing" 章节 `[Backtesting 页索引, 2026]`；具体顺序为 **先验，中等可信** |
| `bar_adaptive_high_low_ordering` | BacktestVenueConfig 选项，开启后按开盘价距高/低哪个更近决定先走 H 还是 L；默认关闭 | Config API 页列有该项 `[https://nautilustrader.io/docs/nightly/api_reference/config/]`；语义先验，中等 |
| FillModel | `prob_fill_on_limit`（默认 1.0，"probability of a limit order filling if the market rests on its price"）、`prob_slippage`（市价单偏一 tick）、新增 `fill_limit_inside_spread`（MatchingCore 支持 at-or-inside-spread 限价成交） | 搜索摘要引自 RELEASES `[https://github.com/nautechsystems/nautilus_trader/blob/develop/RELEASES.md]`，高可信；Issue #2194（2025-01-08 开）指出现有 FillModel "handles only very basic order-fill scenarios"，提议 `process_market_fills/process_limit_fills` 钩子，**状态 Closed，是否实现未知** `[https://github.com/nautechsystems/nautilus_trader/issues/2194]` |
| bar 模式下部分成交 | 默认 L1 且 bar 转 tick，通常整单成交；partial 依赖 volume/FillModel | 先验，低-中 |
| `trigger_type=MARK_PRICE` 回测是否生效 | **未找到公开信息**。搜索词：`nautilus_trader backtest trigger_type MARK_PRICE SimulatedExchange`。只确认枚举存在、文档描述以 LAST_PRICE 举例 `[Orders 页]` | 需第 2 轮读 `matching_engine` 源码 |
| post_only / IOC / reduce_only 在 SimulatedExchange | 文档称 "The behavior in the Nautilus SimulatedExchange is typical of a real venue" `[Orders 页]`；Binance 适配器单测覆盖 post-only 拒单 `[RELEASES 摘要]`；回测撮合具体逻辑先验 | 中 |
| Binance 永续 instrument + funding | CryptoPerpetual instrument 可用 `[先验，高]`；Binance 集成页存在 `[https://nautilustrader.io/docs/latest/integrations/binance/]`；回测中 funding 结算 **未找到公开信息**（搜索词：`nautilus_trader backtest funding rate settlement SimulatedExchange`） | — |
| 外部注入订单 | **未找到"绕过 Strategy 提交订单"的公开 API**（搜索词：`nautilus_trader BacktestEngine submit order without strategy`）。可行路径：壳策略 + 自定义 Data 类型（计划事件）`[先验，高]` | — |

---

## 2. 同 bar 歧义与 "low<=price 即成交" 假设

| 议题 | 发现 | 来源/可信度 |
|---|---|---|
| 同 bar 同时触及 TP 与 SL 的路径依赖 | "If the profit target and stop loss are both hit inside the range of the bar, the path-dependency is important... some backtesting platforms make the blanket assumption that the profit target was hit first, resulting in overly optimistic backtests" | `[Algorithmic Futures Substack: Look Inside the Bar, https://algorithmicfutures.substack.com/p/backtesting-look-inside-the-bar-backtesting]`，中 |
| 入场当 bar 即触止损 | vectorbt 社区：新仓开立同一 bar 内 low 可能已低于止损，属已知难点 | `[vectorbt Discussion #188, https://github.com/polakowo/vectorbt/discussions/188]`，中 |
| 用次 bar open 而非本 bar close | vectorbt 在某些情况下用下一时段 open 计算 TP/SL | `[Issue #780, https://github.com/polakowo/vectorbt/issues/780]`，中 |
| 保守做法（社区共识） | (a) 悲观优先：同 bar 双触判 SL 先（freqtrade / Backtesting.py 的做法 `[先验]`）；(b) 自适应排序：Nautilus `bar_adaptive_high_low_ordering`、TradeStation "look inside bar"；(c) 报告上下界：乐观（TP 先）与悲观（SL 先）各跑一次；(d) 限价成交条件用**穿越**而非**触及**（`low < price` 而非 `<=`），或叠加 `prob_fill_on_limit<1` 模拟排队 | 综合，中 |

---

## 3. 同类"信号回测"开源项目（2025-2026）

| 项目 | 挂单/止损建模方式 | 来源 |
|---|---|---|
| 965311532/signals-backtesting | 从 Telegram 频道解析信号，用 MetaTrader5 库回测（挂单/SL/TP 交给 MT5 语义） | `[https://github.com/965311532/signals-backtesting]`，中 |
| amirphl/Telegram-Trading-Bot | 实盘执行而非回测：LLM 解析信号 → 杠杆、SL、多 TP、价格偏离检查、保护单对账 | `[https://github.com/amirphl/Telegram-Trading-Bot]`，中 |
| TelegramFXBacktest / TSCopier Backtester（商业） | AI 抽取 order type/entry/SL/TP，输出权益曲线与交易日志；建模细节闭源 | `[https://telegramfxbacktest.com/]`, `[https://telegram-signals-copier.com/backtester]`，低 |
| 博客：Backtesting Crypto Trading Signals | 自写简单模拟器思路 | `[https://blog.timonrieger.de/backtesting-crypto-trading-signals]`，低 |

**未找到**：2025 年后专门做"加密永续订单计划级 + mark price 触发 + post-only/IOC"的开源模拟器。搜索词：`telegram crypto signal backtest github 2025 limit orders stop loss take profit simulator`。

---

## 4. 对本项目的初步含义（≤5 条）

1. **NautilusTrader 回测不能"直接"承接外部订单计划，但可以低成本承接**：没有公开的绕过 Strategy 的提交接口（证据强度：中——搜索未命中 + 先验）；写一个无信号逻辑的"计划回放壳策略"（自定义 Data 事件 + 定时器 + `OrderList` 提交绝对价格单）即可驱动，且与生产 1.227.0 同一套订单 FSM/instrument 校验，审计一致性最高。
2. **Nautilus 的三个待证风险点决定它能否单独胜任**：① `MARK_PRICE` 触发在 SimulatedExchange 是否生效（未找到证据）；② 回测账户是否结算 funding（未找到证据，倾向"不结算"→需外挂按 8h 资金费率从 mark 1m 归档后算）；③ bar→tick 拆分下 post-only/IOC 的语义（bid=ask 时 post-only 几乎必拒或必过）。第 2 轮应直接读 `backtest/matching_engine.pyx` 与 `concepts/backtesting` 子页。
3. **同 bar 歧义没有任何候选内建"上下界"输出**：建议无论选谁，都以 `adaptive` + 强制 "H 先" + 强制 "L 先" 三次运行给出区间；Nautilus 可通过 `bar_adaptive_high_low_ordering` 覆盖前者，后两者需自定义 bar→tick 拆分或在自研层实现。
4. **hftbacktest 直接淘汰**（需逐笔 L2，免费归档不存在）；backtrader 原版停维护、GPL；freqtrade 唯一内建 futures funding 回测但限价语义粗且 GPL；vectorbt PRO 闭源不利审计；Backtesting.py 单资产/AGPL——它们都只能做交叉验证，不能做主引擎。
5. **推荐第 2 轮验证路径**："Nautilus 壳策略主引擎 + 数百行自研参考模拟器做差分验证"。自研模拟器承担 mark-price 触发、funding 分列、上下界三跑等 Nautilus 未证实项；两者对同一计划的成交事件序列 diff 即审计产物。

**Sources**
- [Releases · nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader/releases)
- [RELEASES.md](https://github.com/nautechsystems/nautilus_trader/blob/develop/RELEASES.md)
- [Issue #2194 Enhanced order-fill simulation](https://github.com/nautechsystems/nautilus_trader/issues/2194)
- [Issue #2549 execution based on trades](https://github.com/nautechsystems/nautilus_trader/issues/2549)
- [Backtesting concepts](https://nautilustrader.io/docs/latest/concepts/backtesting/)
- [Config API](https://nautilustrader.io/docs/nightly/api_reference/config/)
- [Orders concepts](https://nautilustrader.io/docs/latest/concepts/orders/)
- [Binance integration](https://nautilustrader.io/docs/latest/integrations/binance/)
- [vectorbt Discussion #188](https://github.com/polakowo/vectorbt/discussions/188) · [Issue #780](https://github.com/polakowo/vectorbt/issues/780) · [signals/nb](https://vectorbt.dev/api/signals/nb/) · [vectorbt PRO Portfolio](https://vectorbt.pro/features/portfolio/)
- [Look inside the bar](https://algorithmicfutures.substack.com/p/backtesting-look-inside-the-bar-backtesting)
- [Backtrader maintenance](https://community.backtrader.com/topic/2466/backtrader-maintenance) · [backtrader PyPI](https://pypi.org/project/backtrader/) · [cloudQuant/backtrader](https://github.com/cloudQuant/backtrader) · [backtrader_next](https://github.com/smalinin/backtrader_next)
- [hftbacktest README](https://github.com/nkaz001/hftbacktest) · [Data Preparation](https://hftbacktest.readthedocs.io/en/latest/tutorials/Data%20Preparation.html)
- [Freqtrade backtesting](https://www.freqtrade.io/en/stable/backtesting/)
- [Backtesting.py](https://kernc.github.io/backtesting.py/) · [Grokipedia Backtesting.py](https://grokipedia.com/page/Backtestingpy)
- [signals-backtesting](https://github.com/965311532/signals-backtesting) · [Telegram-Trading-Bot](https://github.com/amirphl/Telegram-Trading-Bot) · [TelegramFXBacktest](https://telegramfxbacktest.com/)