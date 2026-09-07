# 频道种子→量化算法 工具链技术选型 调研笔记

调研日期：2026-09-07 ｜ 截止日：2026-09-07 ｜ 时间范围：2025-01 → 2026-09-07
调研目标：为 M0–M3（样本资产化 → 标注 → 回测 → 研究协议）选定工具栈，重点裁决"回测引擎自研 vs 复用 NautilusTrader 回测"与"Telegram 频道全量历史抓取方案"。
决策人：Balen ｜ 用途：决定开工技术栈，避免重复造轮子
输出：本目录 `.md` + `.html` + `research/round{1,2,3}-*.md`

## 关键问题
1. Telegram 频道全量历史抓取：Telethon / GramJS / Telegram Desktop 导出 / tdl 等 CLI，各自在限流、媒体下载、编辑历史、reply 链、只读账号隔离、封号风险上的实测口径？
2. 信号结构化标注流水线：LLM 结构化抽取（模型/批量接口/成本）+ 人工标注工具（Label Studio / Argilla / doccano / 自建）+ 截图 OCR，哪套能承接"三层标注 + 锁箱"要求？
3. 行情数据获取与存储：Binance Vision/API 之外要不要付费源；研究湖用 Parquet+DuckDB / ClickHouse / TimescaleDB / ArcticDB？
4. order_plan 级回测引擎：自研 OHLC 反事实模拟器 vs NautilusTrader BacktestEngine（生产节点同框架）vs vectorbt / backtrader / hftbacktest / freqtrade，谁能满足订单状态机、post-only/IOC、止损触发基准、同 bar 歧义上下界？
5. 指标特征与研究协议：指标库（pandas-ta / TA-Lib / vectorbt）、预注册与锁箱（DVC / MLflow / git+hash）、统计（block bootstrap、purged walk-forward、deflated Sharpe）的可用实现？

## 已知约束（仓库事实，直接采信）
- 生产执行节点 = NautilusTrader **1.227.0**（`infra/docker/nautilus/uv.lock` 锁定，PyPI 2026-05-18 发布；jp-24 node-a 容器实跑同版本），控制面 Postgres + Redis，Hermes 决策器 Python 3.14 venv
- 生产 watcher = GramJS `telegram@^2.26.22`（bridge/services/telegram-watcher/package.json）；仓库另有 Python 采集器 services/telegram-watcher/telegram_watcher/collector.py
- 已有 Binance Vision 1m 下载器（scripts/analysis/zone_penetration_stats.py:316 load_klines，本地 zip 缓存）
- 样本：生产库 836 条原始消息（2026-02 起，舒琴 2026-06 起）；Telegram 频道历史远早于此，全量抓取可能把样本量放大数倍
- 回测语义要求（review 终裁）：订单状态机 accepted/rejected/working/partial/filled/canceled、mark/last 触发基准、tick/step/min-notional、post-only 拒单、IOC 余量取消、同 bar 多触发上下界、费用与资金费率分列
- **用户裁定（2026-09-07）：在本 monorepo 内起一套全新 app 承载研究/回测/标注，上架交易策略之前与现有生产系统完全分开**：不接控制面、不写生产库、只通过只读导出与 Telegram 全量抓取取样本；Python 版本、依赖、存储自选。**选型权重据此调整**：技术选型预设的"生态与集成 15%"改释为"上架时对接生产（NautilusTrader 节点 / order_plan 契约）的迁移成本"，其余维度不变（能力匹配 30 / 生产成熟度 25 / TCO 20 / 对接迁移 15 / 退出成本 10）。
- 用户追加（2026-09-07）：开源因子集（Alpha101/191、Qlib Alpha158/360、tsfresh 等）纳入特征候选池评估，已派专项快查。

## 发现
（每轮追加）

## 来源列表
| 来源 | URL | 发布日期 | 可信度 |
|------|-----|---------|--------|

### 阶段摘要（第 1 轮 · 视角 C 特征/协议/统计，2026-09-07，详见 research/r1-features-protocol.md）

- **指标库**：TA-Lib 0.7.1（2026-07，官方 wheel 覆盖 Py3.9–3.14，安装痛点已消失）为首选；**pandas-ta 原版排除**（2025-07 起许可/维护变动、PyPI 历史被清、原仓库移除），需其 API 用 `pandas-ta-classic` 0.6.52（MIT，2026-06）；`polars_ta` 0.5.17 为 Polars 原生备选；tulipy 7 年未更、finta 已归档。多周期闭合 bar 对齐无库内置，用 Polars `group_by_dynamic` / pandas `resample(closed='right')` 自做。【可信度高：PyPI/GitHub】
- **非价格特征是数据问题**：Binance `openInterestHist` API 仅回溯 30 天，历史 OI 要走 data.binance.vision `metrics` 归档或第三方；资金费率 as-of 键取结算时间。【中】
- **as-of / 前视**：polars `join_asof` / DuckDB ASOF / pandas `merge_asof` 三者等价可互校，核心坑是等号语义（`allow_exact_matches=False` 或 bar_close+ε）；**无成熟 point-in-time 校验包**（搜过 point-in-time feature leakage tools python / lookahead bias checker），需自写"截断重算一致性 + NaN 注入传播"两类测试（思路来自 freqtrade lookahead-analysis、leak-detect）。Feast 0.66 有 PIT join 但对单人过重。
- **预注册/锁箱**：git + JSONL + sha256 manifest 为底，**外部时间锚定用 OpenTimestamps（比特币锚定，免费）或 RFC3161 TSA** 才能证明"看结果前已登记"；DVC 3.67 / MLflow 3.16 只解决记账；W&B 云依赖、Guild AI 2023 后停更、lakeFS 过重、Sacred 低活跃。无现成"ML 预注册工具"。
- **统计**：purged/CPCV 活跃实现只有 `purgedcv` 0.1.6（**2026-09-04 发布，单作者，含 DSR/PSR/PBO**）与 `skfolio` 1.0.3 的 CombinatorialPurgedCV（按样本数 purge，非标签区间）；mlfinlab 非开源商业许可排除；timeseriescv 2018 后停更但可对拍。block bootstrap 用 `arch` 8.0 或 `tsbootstrap` 0.7.2；簇 bootstrap 与 max-T 簇置换需自写；多重比较 arch 的 SPA/StepM/MCS 可用。
- **规则模型**：`imodels` 3.0.0（2026-08，RuleFit/SkopeRules/BRL/FIGS 统一 `get_rules()`）+ sklearn 浅树/LR；EBM（interpret 0.7.8）作边际解释，非 if-then。
- **LLM 假设挖掘**：Alpha-GPT 无官方代码；可借数据结构的是 **RD-Agent(Q)**（hypothesis/experiment/feedback trace）与 **AlphaGen** 的 `Expression` 表达式树；全部框架评估横截面 IC，本项目评估器必须换成事件级 PR/校准/bootstrap；Profit Mirage（2510.07920）警示 LLM 抽取要记录知识截止。

### 阶段摘要（专项快查 · 开源因子集，2026-09-07，详见 research/r1-factor-sets-quickcheck.md）

- **Qlib Alpha158 是唯一"全部单标的时序、close 归一、无横截面、无前视"的成熟集合**（源码核对 `qlib/contrib/data/loader.py`：KBAR 9 + PRICE 4 + 29 滚动算子 × 5 窗口；其中 `Rank` 是 ts_rank 非横截面）。用其**表达式定义**在 pandas/polars 复现，不引入 Qlib 框架。Alpha360 是 360 维滞后原值，不可解释，排除。【高】
- **Alpha101 仅 18 条纯时序**（#6/7/9/12/21/23/24/26/35/41/43/46/49/51/53/54/84/101），其余外层套 `rank`，26 条需行业中性化，在 44 品种加密宇宙无意义；Alpha191 约 63% 用横截面 rank，排除；AlphaGen 生成因子不可解释且目标是横截面 IC，不作起点。【中高，作者逐条核对】
- **加密专用因子库不存在**（搜过 cryptofactors / crypto-factor / crypto factor library github 2025 三组词），学术因子（Liu-Tsyvinski-Wu 三因子、趋势因子、资金费率/OI/carry、Quarter-Hour Effect 2607.09426）均无可复用代码 → 资金费率、OI 变化、清算、basis 3–5 维自建。
- **样本量硬约束**：EPV≥10 规则（Peduzzi 1996 / van Smeden 2016）→ 100 条正样本 ≤10 维，300 条 ≤30 维；直接喂 158 维 EPV<2 必过拟合。裁剪流程：每算子留 2 窗口 → |ρ|>0.8 层次聚类每簇留一 → 单变量检验 + BH-FDR 或 clustered MDA → 按 EPV 封顶。
- **前视风险点**：tsfresh Issue #807（全表 impute 跨窗口泄漏）、pandas-ta 的 dpo/ichimoku 为 centered 计算（官方文档标注 Potential Data Leaks）、Qlib 的负向 Ref 仅在 label；Quarter-Hour Effect 表明 15m 整点是波动脉冲，bar 边界归属要谨慎。
- **与手工四类指标的映射**：Alpha158 覆盖趋势(MA/BETA/RSQR/RESI)、位置(RSV/QTLU/QTLD/RANK/MAX/MIN)、动量(ROC/SUMP/SUMN)、波动(STD/KLEN)，并多出"量价关系"(CORR/CORD/VSUMD/WVMA)与 KBAR 当根形态；可替代大部分手工指标。

### 阶段摘要（第 1 轮 · 视角 A1 Telegram 抓取，2026-09-07，详见 research/r1a1-telegram-harvest.md）

- **主抓取器候选 = Telethon 1.44**（2026-06 文档）：字段最全（date/edit_date/reply_to/媒体）、`get_messages(ids=)` 精确重取、分页与 FloodWait 处理成熟。**GitHub 仓库 2026-02-21 归档、迁至 Codeberg，仍在发版**，PyPI 正常；选 v1 API。备选 Kurigram / pyrofork（Pyrogram 原版已停维）。【高】
- **生产依赖 GramJS `telegram@2.26` 已于 2026-07-14 归档**，npm 页标注不再维护，活跃分叉为 teleproto（单人维护）——独立于本课题的技术债，需另立任务。【高】
- **编辑历史不可回溯**：`getHistory` 与 TDesktop 导出都只给最终版 + `edit_date`；编辑序列只能靠实时监听 `UpdateEditMessage`。研究样本要标"仅最终版"。【高】
- **会话隔离是硬约束**：同一 session 两处用 → `database is locked`；同一 auth key 两处登录 → `AuthKeyDuplicatedError` 把生产 watcher 踢下线。各库 session 格式互不通用，不存在复用生产 session 的路径。【高】
- **封号风险在账号属性而非读取行为**：只读频道历史属低风险（商业博客+Telethon issues，中）；2025-2026 无"纯只读拉历史被封"公开案例（搜过两组词）；风险集中在新号/虚拟号（Issue #3955/#4150）。推荐：主账号新开独立 session 保守限速（≥1s/页），或实体 SIM 独立号 ≥1 周 warm-up。
- **限流**：`messages.getHistory` 单页 100 条；2025 后 FloodWait 典型秒数无官方口径（低：社区经验 0–30s）。tdl 默认只导含媒体消息（`--all` 才全量），适合补媒体不适合主抓取；TDesktop 官方导出可作一次性完整性对照。
- 未核实：Kurigram/tdl/pytdbot 精确版本；tdl JSON 是否含 edit_date/reply_to；2025-05 账号 freeze 机制细节。

### 阶段摘要（第 1 轮 · 视角 B2 行情数据与存储，2026-09-07，详见 research/r1b2-market-data-storage.md；Binance 归档为本机 S3 listing + HEAD + 解压实测）

- **历史 mark price 1m K 线免费可得**：`data.binance.vision/data/futures/um/{monthly,daily}/markPriceKlines/<SYMBOL>/1m/` 自 2020-01 起，月包约 1MB/symbol；同骨架另有 `indexPriceKlines`、`premiumIndexKlines`。现有下载器只需把 `klines` 换成类型名。【实测】
- **资金费率**：`monthly/fundingRate` 自 2020-01，列 `calc_time,funding_interval_hours,last_funding_rate`（含 4h/8h 变更）；daily 目录为空但月包足够。**持仓量**：只有 `daily/metrics`（5 分钟粒度、2020-09 起、无月包），REST `openInterestHist` 仅回溯 1 个月。**清算**：归档中已无 `liquidationSnapshot`，要清算只能付费（Coinglass Hobbyist $29/月起）。【实测 + 官方文档】
- **付费源均超单人量级**：Tardis Academic $350/月（季付起）、Solo $700/月；CoinAPI 无真免费层；CCData 2026-05-21 起免费层下线；Kaiko 并购 Amberdata 后仅询价。本期不买。
- **存储首选 Parquet + DuckDB 1.5.5**（MIT、零运维、原生 `ASOF JOIN` 解决 mark/last/funding 对齐、分区 Parquet 即不可变快照，DuckLake 可加表级版本）。ArcticDB 3.0 有内建 time-travel 但 BSL 1.1 许可 + 无 SQL ASOF；ClickHouse 26.7 / QuestDB 9.4 / Timescale 2.26 在个位数 GB 单人场景都是多余的常驻服务。
- 未核实：Kaiko/CoinDesk Data 2026 价目；ArcticDB 精确最新 tag；`daily/fundingRate` 目录为空原因。

### 快查 · NautilusTrader 本机可装性（2026-09-07，本机实测 + PyPI JSON）

- 本机 Python 3.14.2（Homebrew）+ uv 0.11.26；PyPI `nautilus_trader` 最新 **1.231.0（2026-08-02）**，`requires_python >=3.12,<3.15`；生产锁定的 1.227.0 有 macOS arm64 cp312/313/314 wheel，**本机可直接 `uv pip install nautilus_trader==1.227.0`**，无需编译。【实测】
- 含义：若回测引擎选 Nautilus，可与生产同版本 1.227.0 做回放（4 个小版本落后于最新，升级另议）；TCO 维度的"安装/环境"成本为零。

### 阶段摘要（第 1 轮 · 视角 A2 LLM 抽取与人工确认工具，2026-09-07，详见 research/r1a2-extract-labeling.md；价格为第三方聚合页转述，官方页待第 2 轮核）

- **成本不是约束**：三家 Batch 均 50% 折扣、24h 窗口。按"1000 条、40% 带图、图 1500 token"估算：小模型 $0.1–1/千条，中档（Sonnet 5、GPT-5.6 Terra、Gemini Flash）$1.5–3/千条，旗舰 $5/千条；5000 条用两个厂商各跑一遍 <$30。→ 应双模型交叉、取分歧做人工重点，而非省钱选小模型。【中】
- **图上价格标签抽取无现成证据**：已有评测（gist 40 条信号审计、arXiv 2604.12659）都是"VLM 看形态不可靠"，而本项目要的是读图上标注的数字（OCR + 空间关联），**未找到**针对该任务的公开评测（搜过 vision LLM OCR price labels chart benchmark）。需自建 50 张截图小基准；schema 每个价格字段加 `source: text|image` + `confidence`。
- **约束库价值有限**：厂商原生结构化输出已覆盖；Instructor 最薄、Outlines 面向本地模型、BAML 引入 DSL、LangExtract 偏文档抽取绑 Gemini。版本号本轮低可信，第 2 轮逐个核 PyPI。
- **人工确认工具首选 Label Studio 社区版**（Apache-2.0；`predictions` 预填 + "Show predictions to annotators" 人只改错；导出含 `completed_by`/`created_at`/`lead_time`/history；pip 单进程 SQLite）。Argilla 的 `suggestions` 机制更贴合但要 ES+PG+Redis 三四个容器、HF 收购后节奏不明；doccano 任务类型不匹配排除；Prodigy 约 $490 付费备选；自建 Streamlit 审核页作备胎（1–2 天，与 JSONL+sha256 锁箱耦合最紧）。
- 疑虑：Label Studio 的标签配置表达"可变档数止盈 + 管理动作"可能笨拙；三家图片计费口径不同（Anthropic (w×h)/750、OpenAI tile、Gemini 258/tile），需实测一张典型截图的 token 数。

### 阶段摘要（第 1 轮 · 视角 B1 回测引擎，2026-09-07，详见 research/r1b1-backtest-engines.md；Nautilus 细节部分为搜索摘要 + 先验，官方子页未抓到，第 2 轮读源码核）

- **NautilusTrader 不能"直接"承接外部订单计划**（无绕过 Strategy 的公开提交接口，证据中），但可写"计划回放壳策略"（自定义 Data 事件 + 定时器 + `OrderList` 提交绝对价格单）低成本承接，且与生产 1.227.0 共用订单 FSM / instrument 校验。
- **三个待证风险点**：① `TriggerType.MARK_PRICE` 在 SimulatedExchange 是否生效（未找到证据）；② 回测账户是否结算 funding（未找到证据，倾向不结算）；③ bar→tick 拆分后 post-only / IOC 语义（bid=ask 时 post-only 几乎必拒或必过）。已确认：完整订单状态机；`bar_adaptive_high_low_ordering` 配置项；FillModel `prob_fill_on_limit`/`prob_slippage`/`fill_limit_inside_spread`；Issue #2194（2025-01）称 FillModel "only very basic"，已 Closed 但是否实现未知。
- **其余候选只能做交叉验证**：hftbacktest 需逐笔 L2（免费归档不存在）淘汰；backtrader 原版官方停维、GPL；freqtrade 唯一内建 futures funding 回测但限价语义粗、GPL；vectorbt PRO 闭源；Backtesting.py 单资产 AGPL；vectorbt 开源低活跃。
- **同 bar 歧义无人内建上下界**：社区共识 = 悲观优先 / 自适应排序 / 乐观悲观各跑一次 / 限价用穿越而非触及（`low < price`）或叠加 `prob_fill_on_limit<1`。
- **同类信号回测开源项目**（2025-2026）无一做到永续 + mark 触发 + post-only/IOC 级别；多数把订单语义交给 MT5 或闭源。
- **初步推荐**："Nautilus 壳策略主引擎 + 数百行自研参考模拟器做差分验证"，自研层承担 mark 触发、funding 分列、上下界三跑等 Nautilus 未证实项，两者成交事件序列 diff 即审计产物。

## 第 1 轮总摘要（2026-09-07）

| 问题 | 第 1 轮领先候选 | 第 2 轮要验证的决定性证据 |
|---|---|---|
| Q1 Telegram 抓取 | Telethon 1.44（Codeberg）独立 session；TDesktop 导出作对照；GramJS 已归档（生产债） | Telethon 迁移后发版节奏；2025+ FloodWait 实测；tdl JSON 字段；freeze 机制 |
| Q2 抽取与标注 | 双厂商 Batch 交叉 + Label Studio 社区版；自建 Streamlit 备胎 | 官方价格页；Label Studio 动态字段表单可行性；截图价格 OCR 小基准设计 |
| Q3 行情与存储 | Binance Vision 全免费（含 markPriceKlines/fundingRate/metrics）+ Parquet/DuckDB | 已实测，第 2 轮只补 DuckDB ASOF 平局语义 |
| Q4 回测引擎 | Nautilus 壳策略 + 自研差分模拟器 | **读 1.227.0 源码**：MARK_PRICE 触发、funding 结算、post_only/IOC、bar 拆分顺序、非 Strategy 提交 |
| Q5 特征与协议 | TA-Lib 0.7 + Alpha158 表达式复现（裁到 ≤30 维）；git+JSONL+sha256+OpenTimestamps；purgedcv/skfolio 对拍自写；arch bootstrap；imodels | purgedcv purge 定义正确性；OpenTimestamps 客户端状态；polars_ta 能否直接算 Alpha158 算子 |

### 阶段摘要（第 2 轮 · R2-1 Nautilus 源码核验，详见 research/r2-nautilus-source-verify.md，一手源码）
- **MARK_PRICE 触发在回测不生效**：SimulatedExchange 无 `process_mark_price`；触发价选择中 MarkPrice 落默认分支按 bid/ask；止损判定 `is_touch_triggered` 用 bid/ask。
- **funding 不结算**：backtest 与 matching 目录零命中。
- **bar 驱动 = O→H→L→C 四个 TradeTick，bid=ask=last**；post-only 仅在穿越 last 时拒；IOC 余量取消已建模；限价"触及即成交"偏乐观，需 `prob_fill_on_limit<1` 或自研保守界。
- Rust 层 `exchange.send(TradingCommand)` 存在；Python 走壳策略即可承接外部计划。
- **裁定：Nautilus 主引擎 + 自研差分层（mark 触发 / funding / 双界）**，第 1 轮推荐成立，证据升为高。

### 阶段摘要（第 2 轮 · R2-2 Telegram 验证，详见 research/r2-telegram-verify.md）
- Telethon 2026 年发 4 版（最新 1.44.0，06-15），作者明文"v1 大体维护模式"；锁 1.44.0，只为 layer 更新升级。【高】
- **2025-05 账号冻结机制**：冻结 = 不能发/收消息、不能重登录或加设备 → session 作废；无证据表明第三方客户端本身触发冻结，风险仍在新号/虚拟号。抓取账号用有历史的主号或养过的号，冻结作一级告警。【高】
- FloodWait 2025 后无实测数据，须自测（`wait_time=1`、`flood_sleep_threshold=120` 记日志作项目基线）。
- tdl 默认 JSON 疑似不含 edit_date/reply_to（`--raw` 才有）、tdesktop 导出 `edited`/`reply_to_message_id` 未经官方页核；各实跑一次闭环。
- GramJS 归档一手确认（2026-07-14，README 推荐 teleproto）；teleproto 单人维护数据待 `npm view` 补。

### 阶段摘要（第 2 轮 · R2-3 抽取/标注验证，详见 research/r2-labeling-verify.md）
- 官方价：Sonnet 5 $2/$10，Batch 半价；Gemini 3.x Flash $0.75/$3.75（促销至 2026-12-31，之后翻倍）；OpenAI 页 403 未核。【高/待补】
- **Label Studio 1.23.0（2026-03-13）活跃**；可变止盈档位用 `<Repeater>`；predictions 与 annotations 同在导出 JSON，diff 自算。**Argilla 出局**：13 个月无功能提交。【高】
- 截图价格 OCR 无任何专门基准（通用 OCR 榜与 CharXiv 不覆盖 K 线价格读数）→ 自建 50–100 张带真值小样，纠错率作验收指标。
- opentimestamps-client 0.7.2（2024-12-31）可用但停更，锁版本并归档 `.ots` 原文件。

### 阶段摘要（第 2 轮 · R2-4 统计/协议验证，详见 research/r2-stats-verify.md）
- **purgedcv**：purge 按事件级 `[prediction_time, evaluation_time]` 区间重叠（AFML t1 语义），embargo 三单位互斥；**无第三方对拍测试**，31 stars 单作者 → 当"可替换实现"，M3 必自写 10 行 AFML PurgedKFold 对拍。【高，README】
- **skfolio CPCV** 的 `purged_size/embargo_size` 是行数，不支持事件级不等长标签 → 降级为定长标签备选。【高，官方 API】
- **polars_ta 覆盖 Alpha158 约 70%**：ROC/MA/STD/MAX/MIN/QTL/ts_rank/IMAX/IMIN/CORR/RSV 现成；BETA/RSQR/RESI/CNTP/CNTN/SUMP/SUMN/WVMA 8 个需自写（1–2 行表达式或协方差组合）。【中】
- **arch 8.0 block bootstrap 不支持按簇/组索引**（仅 `block_size`）；tsbootstrap 未核；事件簇 bootstrap 大概率自写（按 event id 抽块再 index 回行）。【高】
- imodels 最新 release v3.0.0 为 **2024-08-03**（25 个月无新版），`get_rules()` 输出未核。
- **DuckDB 1.5.5 ASOF 本机实测**（`uv run --with duckdb`）：`>=` 与 `>` 均支持；`>` 严格取更早 bar；右表同一时间戳多行时 5 次运行结果一致但顺序未文档化 → 右表先按 (key, ts) 去重再 ASOF，严格前视用 `>`。【实测】
