# 第 3 轮 · 定量比较（R3-2，主会话，2026-09-07）

评分模型：技术选型预设一，按用户裁定改释第四维。
`总分 = 能力匹配 30% + 生产成熟度 25% + TCO 20% + 上架对接迁移成本 15% + 退出成本 10%`。
每维 0–100；置信度（高/中/低）反映证据质量，不反映吸引力。"上架对接"指策略上架时与生产 NautilusTrader 节点 / order_plan 契约对接的迁移成本（研究期新 app 与生产完全分开）。

## Q1 Telegram 全量历史抓取

| 候选 | 能力 30 | 成熟 25 | TCO 20 | 对接 15 | 退出 10 | 总分 | 置信 | 依据 |
|---|---|---|---|---|---|---|---|---|
| **Telethon 1.44（独立 session）** | 90 | 75 | 85 | 70 | 90 | **83.0** | 高 | 字段最全（date/edit_date/reply_to/媒体）、按 id 精确重取、FloodWait 自动处理（PyPI/docs 一手）；v1 维护模式、仓库迁 Codeberg（-25 成熟）；Python 与研究栈同语言（TCO 高）；对接：生产 watcher 是 GramJS，抓取器不复用生产会话，对接本就为零（70 = 中性）；退出：MTProto 数据可原样转存，换库无锁定 |
| Kurigram / pyrofork | 85 | 60 | 80 | 70 | 85 | 75.5 | 中 | 承袭 Pyrogram API；个人维护、双域名并存（成熟 -）；session 格式与 Telethon 不通用但都是 SQLite |
| tdl（Go CLI） | 60 | 70 | 70 | 70 | 80 | 67.5 | 中 | 默认只导含媒体消息、JSON 疑似无 edit_date/reply_to（`--raw` 才有，未实跑核）；namespace 天然隔离；适合补媒体 |
| Telegram Desktop 官方导出 | 50 | 90 | 60 | 70 | 90 | 68.0 | 中 | 无 id 精确重取、无增量、24h 限流；但官方口径，作 ground truth 对照价值高 |
| TDLib 绑定 | 85 | 70 | 45 | 70 | 70 | 69.0 | 中 | 最接近官方客户端语义；部署最重（tdjson 编译/预编译），对 5 个频道过重 |
| GramJS / teleproto | 85 | 40 | 60 | 80 | 80 | 67.5 | 高 | GramJS 2026-07-14 归档；teleproto 单人维护；Node 与研究栈异语言 |

矛盾证据：Telethon 2026 发版 4 次但 06-15 后近 3 个月无新版（迁移以来最长间隔）；FloodWait 2025 后无公开实测值。
裁决：**Telethon 主抓取器 + TDesktop 导出一次性对照 + tdl 补媒体**。
PoC：单频道全量拉取带日志（`wait_time=1`、`flood_sleep_threshold=120`），记 FloodWait 基线；与 TDesktop 导出比对消息数与 id 集合。止损：若主号触发冻结告警或 FloodWait > 1h，切 tdl/TDesktop 通道并暂停。

## Q2 LLM 结构化抽取 + 人工确认

| 候选（确认工具） | 能力 30 | 成熟 25 | TCO 20 | 对接 15 | 退出 10 | 总分 | 置信 | 依据 |
|---|---|---|---|---|---|---|---|---|
| **Label Studio 1.23.0 社区版** | 85 | 85 | 80 | 70 | 85 | **81.8** | 高 | predictions 预填 + 人改、`completed_by`/时间戳/lead_time 导出、`<Repeater>` 表达可变止盈档（未实跑）；季度发版；pip 单进程 SQLite；导出 JSON 可直接进 sha256 锁箱 |
| 自建 Streamlit 审核页 | 75 | 60 | 65 | 70 | 90 | 71.5 | 中 | 完全可控、与锁箱耦合最紧；无标注流管理，1–2 天开发；成熟度取决于自己 |
| Prodigy | 85 | 85 | 55 | 70 | 60 | 73.0 | 中 | `*.correct` 范式贴合；$490 席位、闭源、Python 版本绑定（退出 -） |
| Argilla 2.x | 80 | 30 | 45 | 70 | 70 | 58.0 | 高 | suggestions 机制贴合；**2025-08 起无功能提交、近 90 天零 commit**；ES+PG+Redis 部署重 |
| doccano | 40 | 50 | 80 | 70 | 80 | 59.0 | 中 | 任务类型（NER/分类）不匹配多字段表单 |

抽取模型（不打分，结论性）：Batch 均半价；Sonnet 5 Batch $1/$5、Gemini 3.x Flash Batch $0.375/$1.875（促销至 2026-12-31）；OpenAI 页 403 未核。千条 $0.1–5，**双厂商交叉**（取分歧作人工重点）成本 <$30/5000 条。约束库（Instructor/BAML/LangExtract/Outlines）非必需。
矛盾证据：截图价格标签 OCR **无任何公开基准**；已有评测只说明 VLM 看形态不可靠。
裁决：**Label Studio + 双厂商 Batch；Streamlit 备胎**。
PoC：50–100 张带真值截图小基准，schema 每个价格字段带 `source: text|image` + `confidence`，以人工纠错率为验收；Label Studio 跑一个 3 档/5 档止盈样例验证 Repeater 与导出 diff。止损：纠错率 > 30% 则图片字段全部转人工录入。

## Q3 行情数据源与研究湖存储

数据源（结论性）：data.binance.vision 免费覆盖 klines / **markPriceKlines** / indexPriceKlines / premiumIndexKlines / fundingRate（月包自 2020-01）、metrics 持仓量（日包 5m 自 2020-09）、aggTrades/bookTicker；清算无归档（需 Coinglass $29/月起）。付费源（Tardis $350–700/月、CoinAPI、Kaiko）全部超单人量级，**不买**。

| 存储候选 | 能力 30 | 成熟 25 | TCO 20 | 对接 15 | 退出 10 | 总分 | 置信 | 依据 |
|---|---|---|---|---|---|---|---|---|
| **Parquet + DuckDB 1.5.5** | 90 | 85 | 95 | 80 | 95 | **89.3** | 高 | 原生 ASOF JOIN（本机实测 `>`/`>=` 均可，严格前视用 `>`，右表先去重）；MIT；零服务进程；分区 Parquet 即不可变快照；Parquet 通用格式退出成本最低 |
| ArcticDB 3.0 | 80 | 75 | 80 | 70 | 60 | 74.5 | 中 | 内建版本化/time-travel；BSL 1.1 许可边界待确认；无 SQL ASOF；专有存储格式（退出 -） |
| ClickHouse 26.7 / chDB | 90 | 90 | 55 | 70 | 70 | 76.3 | 中 | 原生 ASOF；个位数 GB 用不满；常驻服务运维（chDB 可降） |
| QuestDB 9.4 | 85 | 80 | 55 | 70 | 70 | 73.0 | 中 | 原生 ASOF/`SAMPLE BY`；JVM 常驻 |
| TimescaleDB 2.26 | 65 | 85 | 50 | 75 | 75 | 68.8 | 中 | 无原生 ASOF（LATERAL 模拟）；需独立 PG 实例；TSL 许可分层 |

裁决：**Parquet 分区（type/symbol/year-month）+ DuckDB**，DuckLake 作表级版本备选。
PoC：44 品种 × 4 数据类型 × 2025-01 起预热缓存并出覆盖矩阵；ASOF 对齐 mark/last/funding 三表单测。止损：无。

## Q4 order_plan 级回测引擎

| 候选 | 能力 30 | 成熟 25 | TCO 20 | 对接 15 | 退出 10 | 总分 | 置信 | 依据 |
|---|---|---|---|---|---|---|---|---|
| **NautilusTrader 1.227.0 壳策略 + 自研差分层** | 80 | 85 | 65 | 95 | 60 | **77.8** | 高（源码） | 一手确认：完整订单 FSM、tick/step/min-notional、IOC 余量取消、reduce-only、OrderList/GTD、`bar_adaptive_high_low_ordering`；**不足**：MARK_PRICE 触发不生效（撮合不消费 MarkPriceUpdate，落 bid/ask 分支）、funding 不结算、bar 驱动 bid=ask=last 使 post-only 语义退化、限价触及即成交偏乐观 → 自研层补三项（能力 80）；与生产同版本同 Rust 撮合路径（对接 95）；LGPL-3.0 + 学习曲线（TCO 65）；退出：策略代码绑定 Nautilus API（60） |
| 纯自研事件驱动模拟器 | 85 | 50 | 70 | 55 | 90 | 70.5 | 中 | 输入即计划、三项缺口天然可做、全规则自审计；成熟度靠自己（无外部验证）；上架时与生产订单语义无共享（对接 55） |
| freqtrade backtesting | 55 | 85 | 50 | 40 | 60 | 58.8 | 中 | 唯一内建 futures funding；限价语义粗；策略回调驱动、GPL |
| vectorbt PRO | 60 | 75 | 45 | 40 | 40 | 55.3 | 中 | 有 stop ladder / limit_tif；闭源付费、无状态机、无 post-only/mark |
| Backtesting.py 0.6.5 | 45 | 70 | 70 | 40 | 70 | 56.8 | 中 | 单资产、AGPL、无永续概念 |
| backtrader（原版） | 50 | 30 | 60 | 40 | 60 | 47.5 | 高 | 官方停维、GPL |
| hftbacktest | 90 | 80 | 20 | 50 | 60 | 60.5 | 高 | 需逐笔 L2，免费归档不存在 → 淘汰 |

矛盾证据：Issue #2194（2025-01）称 FillModel "only very basic"，已 Closed 但是否实现未知；社区无"Nautilus bar 回测 vs 实盘差距"的具体案例（对抗轮在查）。
裁决：**Nautilus 主引擎 + 自研差分层**；两者对同一计划的成交事件序列 diff 即审计产物；自研层承担 mark 触发（用 markPriceKlines 作驱动 bar 再跑一遍即可近似）、funding 后算、乐观/悲观双界。
PoC：取 20 个唯一可归属纯机器人 episode（M3-E），Nautilus 与自研分别回放，与 `execution_events` 对账（数量 ≤1 步进、价格 ≤1 tick）。止损：若 Nautilus 壳策略对 OrderList/GTD/修改单的回放与实盘事件差异无法解释且自研层可解释，主引擎降为交叉验证。

## Q5 指标特征、研究协议与统计

| 子项 | 领先候选 | 总分（能力/成熟/TCO/对接/退出） | 置信 | 备注与矛盾证据 |
|---|---|---|---|---|
| 指标库 | **TA-Lib 0.7.1 官方 wheel** | 84.5（85/95/85/70/85） | 高 | pandas-ta 原版排除（2025-07 许可变更、PyPI 历史被清）；pandas-ta-classic 0.6.52 备选；`polars_ta` 覆盖 Alpha158 约 70%，8 个算子自写 |
| 特征字典 | **Qlib Alpha158 表达式复现（不引框架）** | 80.0（90/85/70/70/80） | 中高 | 158 全单标的时序、无前视（源码核）；EPV≥10 裁到 ≤30 维；加密专属 3–5 维自建；对抗轮在查非日频坑 |
| as-of 对齐 | polars `join_asof` / DuckDB ASOF | 85.0 | 高（实测） | 等号语义是唯一坑；无成熟 PIT 校验包，自写截断重算 + NaN 注入两类测试 |
| 预注册/锁箱 | **git + JSONL + sha256 + OpenTimestamps 0.7.2** | 82.0（85/75/95/70/90） | 高 | OTS 客户端 2024-12 起停更但协议稳定，锁版本存 `.ots`；DVC/MLflow 只记账不锁箱 |
| purged CV | **自写 AFML PurgedKFold + purgedcv 0.1.6 对拍** | 70.0（85/45/75/70/90） | 中 | purgedcv 事件级 purge 语义正确但 31 stars 无第三方对拍；skfolio CPCV 按行数 purge，仅定长标签备选 |
| bootstrap | arch 8.0 + 自写事件簇 block | 75.0 | 高 | arch 无簇索引参数；tsbootstrap 未核 |
| 规则模型 | imodels 3.0.0 + sklearn | 74.0（85/60/85/70/85） | 中 | 25 个月无 release；`get_rules()` 输出未核 |

裁决：全部采纳"成熟库 + 少量自写 + 对拍"路线；自写总量估计：Alpha158 缺 8 算子、PurgedKFold、簇 bootstrap、两类前视测试，合计数百行。

## 总排序（每问题 TOP1）

1. Q3 存储 Parquet+DuckDB 89.3（证据最硬，实测）
2. Q1 Telethon 83.0
3. Q2 Label Studio 81.8
4. Q4 Nautilus+差分层 77.8（能力缺口已量化，靠自研层补）
5. Q5 各子项 70–85，最弱是 purged CV 生态（70.0）

## R3-1 对抗校验裁决（详见 r3-adversarial.md）

| # | 对抗方"应改" | 裁决 | 理由与修订 |
|---|---|---|---|
| 1 | Telegram 主源改 TDesktop 官方导出，Telethon 降为增量/对照，用次号，固定 Codeberg tag，外套 `TypeNotFoundError` 跳过 | **接受** | 一次性全量样本用官方客户端导出风险最低（无第三方 API、无 layer 断崖 #3226）；Telethon 只做按 id 补拉、edit_date 与增量对照。"主号 + 新 API_ID = 冻结典型模式"为审稿人推断无一手来源，但零成本规避：优先次号，否则只用 TDesktop。Q1 排序改为 TDesktop 导出 → Telethon 补拉；TDesktop 能力分 50→70（作规范来源），Telethon 成熟分 75→65（layer 断崖先例） |
| 2 | 交叉改为 LLM + 确定性语法解析器去相关组合；一致集人工抽审 10% 作交付指标；双 LLM 只裁分歧 | **接受** | ICML 2025《Correlated Errors in LLMs》：两模型同错率约 60%、越强越相关。确定性解析器仅用于研究标注（生产路径禁止 parser→approved 的约束不受影响） |
| 3 | Binance Vision 入湖完整性闸门：`.CHECKSUM` 校验、按月 bar 数核对、缺口日包回填、时间戳统一毫秒（SPOT 2025-01 起微秒）、逐文件表头检测 | **接受** | Issue #297 静默缺两天；README 时间戳单位变更；均为一手 |
| 4 | 回测主次对调：自研差分模拟器为主引擎（含 mark 触发/funding/双界，必须有单测与对拍），Nautilus 只作交叉审计；单 engine 一次长跑按 order tag 归因；LGPL 删除 | **接受，双引擎均为必做** | 主数字来源必须包含永续三要素，Nautilus 一手源码已证明缺失，故主数字来自自研层；Nautilus 的价值是订单状态机、filter、OrderList/GTD 一致性审计（自研最易错处）。Q4 表改名"自研主引擎 + Nautilus 审计引擎"，总分不变 77.8（组合能力不变，只是职责换位）；纯 Nautilus 单独作主引擎降为 62；LGPL 从风险表删除；批量效率按"单 engine 长跑 + 壳策略调度"实现 |
| 5 | 写死 Parquet 为唯一真身，.duckdb 为可丢弃缓存；查询走 `read_parquet` 视图；重建脚本入库；单写者；DuckDB 版本进 lockfile | **接受** | #9667 磁盘不足损坏、v1.4 兼容破坏先例；Parquet 真身后这些风险归零。Q3 分数不变（方案本意如此，补充写死） |
| 6 | 信号→bar 按 `close_time <= signal_ts` 对齐；改称"Alpha158 变体"；滚动窗按墙钟时间；`$vwap` 明确为 bar VWAP；先数已闭合正事件 N，维数 ≤ N/10；主 CV = 自写 `PurgedKFold(t1)` + embargo 走前，purgedcv/skfolio 旁证，N<300 不用 CPCV；补 `TimeSeriesSplit(gap=)` 零依赖基线与 AFML 序贯 bootstrap/uniqueness 加权 | **接受** | Q5 特征字典改名"Alpha158 变体"分数 80→76（vwap 口径差、窗口语义需重定义）；purged CV 行改为"自写主实现 + 两库旁证"分数 70→74（口径明确后风险降低） |

未被推翻的结论：Label Studio、DuckDB+Parquet、Binance Vision 免费源、TA-Lib、锁箱方案、Nautilus 源码事实。

## 修订后 TOP1（每问题）

| 问题 | 修订后方案 | 总分 |
|---|---|---|
| Q1 | TDesktop 官方导出（规范全量）+ Telethon 1.44 次号补拉/增量对照 + tdl 补媒体 | 80 |
| Q2 | LLM（Batch）+ 确定性解析器去相关抽取，一致集 10% 抽审；Label Studio 1.23 确认；JSONL+sha256+OTS 锁箱 | 82 |
| Q3 | Binance Vision（带完整性闸门）→ Parquet 真身 + DuckDB 缓存 | 89 |
| Q4 | 自研差分模拟器（主数字）+ Nautilus 1.227 单 engine 审计回放 | 78 |
| Q5 | TA-Lib 0.7 + "Alpha158 变体"（窗按墙钟、维 ≤ N/10）+ 自写 PurgedKFold(t1) 主 CV + arch/自写簇 bootstrap + imodels | 76–85 |
