# 频道种子→量化算法：研究工具链技术选型报告

范围：为 monorepo 内新建的独立量化研究 app（上架前与生产完全分开）选定 M0–M3 工具栈，五个问题：Telegram 全量历史抓取、信号结构化抽取与人工确认、行情数据与研究湖存储、order_plan 级回测引擎、指标特征与研究协议/统计。
分析期：2025-01-01 → 2026-09-07 ｜ 调研截止日：2026-09-07 ｜ 决策人：Balen
方法：深度档三轮（广度收集 → 关键验证 → 对抗校验 + 定量比较），8 个并行调研 agent + 3 项本机实测（Binance 归档 HEAD/解压、NautilusTrader 1.227.0 源码、DuckDB 1.5.5 ASOF）。过程笔记与逐轮发现见 `research/`。
2026-09-08 修订：按 gpt-6-astra 对 Q3–Q5 的独立审稿（`docs/reviews/2026-09-08-gpt6-review-of-toolchain-q3q4q5.md`，5 必修）修正 Q4 双引擎职责、Q3 入湖闸门、Q5 维数上限与 PurgedKFold 验收；按其对 Q1–Q2 的审稿（`…-q1q2.md`，6 必修）把 TDesktop 定义为一次性规范快照、去相关改为待实测的异质校验、抽审改为分层设计并修正总分算术；**本报告各表为唯一权威，`research/round3-findings.md` 中的旧分数（Alpha158 变体 80、purged CV 70）不再引用**。

## 1. 结论先行

| 问题 | 推荐方案 | 总分 | 证据强度 |
|---|---|---|---|
| Q1 Telegram 全量历史 | **Telegram Desktop 官方导出**作**一次性规范快照**（JSON 元数据与媒体物件分开归档，不是自动化采集器）；**Telethon 1.44**（次号、独立 session、锁 Codeberg tag）做按 `peer_id/message_id` 补拉与增量对照；tdl 补媒体 | 80 | 方向性高，可执行性待 PoC |
| Q2 抽取与确认 | **LLM Batch + 直接读原文/OCR 的确定性解析器**作**异质校验路径**（相关性待实测），分歧交第二家 LLM 裁决，一致集**分层抽审**（预设最小样本与 Wilson 区间，10% 只是初始预算）；**Label Studio 1.23.0** 社区版确认；锁箱 = JSONL + sha256 manifest + OpenTimestamps，附重建校验脚本 | 82 | 方向性高，可执行性待 PoC |
| Q3 行情与存储 | **data.binance.vision 全免费**（klines / markPriceKlines / indexPriceKlines / fundingRate 月包自 2020-01；metrics 持仓量日包自 2020-09）+ 入湖完整性闸门；**Parquet 为唯一真身，DuckDB 1.5.5 为可丢弃缓存** | 89 | 实测 |
| Q4 回测引擎 | **共享一个 order_plan → 规范执行事件的领域模型**：永续三要素（mark price 触发、资金费率账务、同 bar 乐观/悲观双界）作独立模块，自研参考模型产出主数字；**NautilusTrader 1.227.0 经适配器消费规范化订单，只审计订单状态机 / filter / OrderList / GTD**。正确性不靠两引擎互证，靠三层验收：规则不变量测试、手工构造最小 episode 的独立期望、Nautilus 差异审计（永续扩展差异单列） | 78 | 高（源码） |
| Q5 特征与统计 | **TA-Lib 0.7.1** + **"Alpha158 变体"**（窗口按墙钟时间、`$vwap` = bar VWAP；**N/10 只是预注册的保守起点**，最终维数由折内嵌套选择与时间外表现决定）；主 CV = 自写 AFML `PurgedKFold(t1)`（先写枚举区间交集的参考实现 + 性质测试 + 按交易员/episode 簇隔离），purgedcv/skfolio 旁证；`arch` 8.0 + 自写事件簇 bootstrap；`imodels` 3.0 规则模型 | 76–85 | 高/中 |

三条贯穿结论：

1. **数据全部免费，不买任何付费源。** 历史 mark price 1 分钟线、资金费率、持仓量都在 Binance 官方归档里；付费源（Tardis $350–700/月、CoinAPI、Kaiko）超单人量级。唯一缺的是清算数据（Coinglass $29/月起），本期不需要。
2. **回测引擎：永续语义参考实现 + Nautilus 兼容性审计，两者不互证。** 本机读 1.227.0 源码证实：模拟交易所不消费 MarkPriceUpdate（`TriggerType::MarkPrice` 落 bid/ask 分支）、回测不结算 funding、bar 驱动时 bid=ask=last。永续合约的三个决定性要素全在 Nautilus 之外，主数字只能来自自研参考模型；Nautilus 只审订单状态机与 filter。两个实现成交序列一致不代表都对，独立真值来自不变量测试与手工构造的最小 episode。
3. **样本管道的最大风险在账号与相关错误，不在工具。** 2025-05 起 Telegram 冻结机制 = 账号连同 session 作废；ICML 2025 在榜单与简历筛选任务上测得"两模型都错时约 60% 给同一答案、越强越相关"，该结论**不能直接外推到交易信号**，但足以说明"两家一致"不是正确性证书，本项目的共错率必须自己按字段测。

## 2. 方法与局限

评分模型：技术选型预设一。`总分 = 能力匹配 30% + 生产成熟度 25% + 总拥有成本 20% + 上架对接迁移成本 15% + 退出成本 10%`。第四维按用户裁定（研究 app 上架前与生产完全分开）由"生态与集成"改释为"策略上架时与生产 NautilusTrader 节点 / order_plan 契约对接的迁移成本"。置信度反映证据质量，不反映吸引力。

局限：
- OpenAI 价格页抓取 403，中档单价沿用第三方聚合页（中可信）。
- FloodWait 2025 年后无公开实测值；tdl JSON 字段、TDesktop 导出 `edited`/`reply_to_message_id` 未实跑核（各 30 分钟可闭环）。
- Label Studio `<Repeater>` 表达可变止盈档未实跑。
- 截图价格标签 OCR 无任何公开基准，只能自建小样。
- 已闭合正事件数 N、事件簇数、特征有效秩尚未从全量历史数出，维数起点（N/10）待 M0 后预注册。
- Q1/Q2 的"高"证据强度已改为"方向性高、可执行性待 PoC"：TDesktop 导出规模/媒体体积、Telethon 对齐、Label Studio Repeater 均未实跑。
- 总分由脚本按权重复算（2026-09-08）：TDesktop 75.0、Telethon 79.75→80、Label Studio 82.75→82.8；组合方案（Q4）无单项推导公式，78 为组合评分。

## 3. 逐项证据卡

### Q1 Telegram 全量历史抓取

| 候选 | 能力 | 成熟 | TCO | 对接 | 退出 | 总分 | 置信 |
|---|---|---|---|---|---|---|---|
| **TDesktop 官方导出（一次性规范快照）** | 70 | 90 | 60 | 70 | 90 | 75.0 | 高 |
| **Telethon 1.44（次号补拉/增量）** | 90 | 65 | 85 | 70 | 90 | 80 | 高 |
| Kurigram / pyrofork | 85 | 60 | 80 | 70 | 85 | 75.5 | 中 |
| TDLib 绑定 | 85 | 70 | 45 | 70 | 70 | 69 | 中 |
| tdl（Go CLI） | 60 | 70 | 70 | 70 | 80 | 67.5 | 中 |
| GramJS / teleproto | 85 | 40 | 60 | 80 | 80 | 67.5 | 高 |

关键证据：
- Telethon 2026 发 1.43.0/1.43.1/1.43.2/1.44.0（最新 2026-06-15），作者明文"v1 大体处于维护模式"；GitHub 仓库 2026-02-21 归档迁 Codeberg。[PyPI release history, https://pypi.org/project/Telethon/#history]（高）
- layer 断崖先例：Telegram 抬高最低 layer 时旧客户端直接 `RPCError 406 UPDATE_APP_TO_LOGIN`，作者靠"赶工发布"救回。[Issue #3226, https://github.com/LonamiWebs/Telethon/issues/3226]、[Changelog, https://docs.telethon.dev/en/stable/misc/changelog.html]（高）
- 2025-05 冻结机制：冻结账号不能发/收消息、不能重登录或加设备。[tginfo.me/frozen-account-en, https://tginfo.me/frozen-account-en/]（高）
- 会话隔离硬约束：同 session 两处用 → `database is locked`；同 auth key 两处登录 → `AuthKeyDuplicatedError` 踢线。[Telethon FAQ, https://docs.telethon.dev/en/stable/quick-references/faq.html]（高）
- 生产依赖 GramJS `telegram@2.26` 2026-07-14 归档，README 推荐 teleproto。[gram-js/gramjs, https://github.com/gram-js/gramjs]（高）
- 编辑历史不可回溯：`getHistory` 与 TDesktop 导出只给最终版 + `edit_date`。（高）

风险与反驳：只读频道历史属低风险类，2025–2026 无"纯只读拉历史被封"公开案例（搜索词 `telegram userbot ban scraping channel history telethon 2025`）；风险集中在新号/虚拟号（Issue #3955/#4150）。审稿人指出全量抓取中一个未知 constructor 会抛 `TypeNotFoundError` 断游标，需外套跳过并记录。**gpt-6 审稿补（2026-09-08）**：① 五频道数万条含图消息的导出规模未证明可接受——按 5 万条 × 40% 含图 × 0.5–2 MB 估 10–40 GB 媒体，需分离"JSON 元数据快照"与"媒体物件归档"，按频道逐个导出、预留 2 倍空间、记录消息数/媒体数/总字节/最大单文件/墙钟/失败点，成功判据是"可重跑、可续传、目录清单与 JSON 一一对应"；② 对齐键必须是 `peer_id/message_id`（另存 `grouped_id`、`date`、`edit_date`、`reply_to_message_id`、`media_relpath`），一致率定义为 `|A∩B|/|A∪B|` 且差集逐条分类（仅 TDesktop / 仅 Telethon / 字段不一致 / 媒体缺失 / 相册拆分 / 已删除），5 万条的 1% 是 500 条信号，不能容许未解释缺失；③ 两客户端都只给最终编辑版，**编辑过且无原始版本证据的信号隔离或做敏感性分析**，从现在起追加存储编辑/删除事件与内容哈希；④ "次号可用"是门槛不是默认前提，没有次号则 Telethon 环节全部由 TDesktop 承担。

下一步：先单频道——TDesktop 导出 JSON + 媒体（分开归档）并记录规模与耗时；Telethon 次号对同一频道拉一次带日志（`wait_time=1`、`flood_sleep_threshold=120`）记 FloodWait 基线；出对齐报告（主键 `peer_id/message_id`、Jaccard 一致率、差集分类）。通过后再扩到五频道。止损：账号出现冻结提示即停，回落 TDesktop；任一不可恢复区间不进规范样本。

### Q2 LLM 结构化抽取 + 人工确认

| 候选（确认工具） | 能力 | 成熟 | TCO | 对接 | 退出 | 总分 | 置信 |
|---|---|---|---|---|---|---|---|
| **Label Studio 1.23.0 社区版** | 85 | 85 | 80 | 70 | 85 | 82.8 | 高 |
| Prodigy | 85 | 85 | 55 | 70 | 60 | 73 | 中 |
| 自建 Streamlit 审核页（备胎） | 75 | 60 | 65 | 70 | 90 | 71.5 | 中 |
| doccano | 40 | 50 | 80 | 70 | 80 | 59 | 中 |
| Argilla 2.x | 80 | 30 | 45 | 70 | 70 | 58 | 高 |

关键证据：
- Label Studio 1.23.0（2026-03-13），季度发版，Apache-2.0。[PyPI, https://pypi.org/project/label-studio/]（高）；predictions 预填 + "Show predictions to annotators"。[Import pre-annotated data, https://labelstud.io/guide/predictions]（高）；导出含 `completed_by`/`created_at`/`lead_time`。[JSON format, https://labelstud.io/blog/understanding-the-label-studio-json-format/]（高）
- Argilla `develop` 最近提交 2025-08-05，最后功能 PR 2025-03-10，近 90 天零 commit。[commits, https://github.com/argilla-io/argilla/commits/develop]（高）→ 出局
- 价格：Claude Sonnet 5 $2/$10，Batch 半价。[claude.com/pricing]（高）；Gemini 3.x Flash $0.75/$3.75（促销至 2026-12-31 后翻倍），Batch 半价，图片按 token 计入。[ai.google.dev/gemini-api/docs/pricing]（高）。千条 $0.1–5，5000 条双模型 <$30。
- 相关错误：350 个模型、榜单与简历筛选等数据集上，两模型都错时约 60% 给出同一错答案，越强越相关。[Correlated Errors in LLMs, ICML 2025, https://arxiv.org/pdf/2506.07962]（高，**但任务域不同，不可直接外推到交易信号**）
- OpenTimestamps 客户端 0.7.2（2024-12-31），停更但协议稳定。[PyPI, https://pypi.org/project/opentimestamps-client/]（高）

风险与反驳：截图价格标签读数（OCR + 空间关联）无任何公开基准，已有评测只说明 VLM 看形态不可靠。[gist 40 条信号审计, https://gist.github.com/roman-rr/c1cd675f7c35b68ae5ac281c30080166]（中）。约束库（Instructor/BAML/LangExtract/Outlines）在厂商原生结构化输出下非必需。**gpt-6 审稿补（2026-09-08）**：① "去相关"是过度表述——确定性解析器只对它能覆盖的语法提供不同错误机制，处理不了"关注区域""分批止盈看情况""图文冲突"这类语义缺省；改为"异质校验路径，相关性待实测"，解析器必须直接读原始文本/OCR 中间产物而不是 LLM 草稿，LLM 输出保留原文 span/图像坐标，按字段（文本/价格/方向/多档止盈）测两路径的错误共现率、漏报率、拒答率；② 一致集抽审改为**分层随机抽样**（层：频道、文本/图片、信号模板、模型版本、字段类型、置信度），预设绝对最小样本与停止规则，报每层错误率与 Wilson 区间——零错误时单侧 95% 上界为 `1−0.05^(1/n)`，n=10 约 25.9%、n=100 约 2.95%、n=299 才低于 1%；低置信、价格字段、新模板做 100% 复核；③ 先建**不展示预测**的人工金标集再做模型评测，防预填锚定；标签分 `human_verified` / `auto_accepted_under_audit` / `ambiguous`，未抽记录不得冒称人工真值；两路都空或共享 OCR 不算强一致，图文冲突必须弃权不补默认价。

下一步：先建不展示预测的分层人工金标集（含 50–100 张带真值截图），schema 每个价格字段带 `source: text|image` + `confidence` + 原文 span；解析器与 LLM 各自从原文并行抽取，报字段级共错率；分层抽审按最小样本设计；Label Studio 跑 3 档/5 档止盈样例验证 `<Repeater>`、导出 diff、备份恢复后再导入全量预测；锁箱附重建校验脚本（从 JSONL、manifest、签名、可选 `.ots` 重建，失败即报错）。止损：图片字段纠错率 > 30% 则全部转人工录入；任一字段共错率显著高于独立假设则该字段 100% 人工。

### Q3 行情数据源与研究湖存储

数据源（本机 S3 listing + HEAD + 解压实测）：

| 类型 | 存在 | 日/月包 | 最早（BTCUSDT） | 示例 |
|---|---|---|---|---|
| klines 1m | 是 | 日/月 | 2020-01 | 已有下载器 |
| **markPriceKlines 1m** | 是 | 日/月 | 2020-01 | `monthly/markPriceKlines/BTCUSDT/1m/BTCUSDT-1m-2025-06.zip`（1.0 MB） |
| indexPriceKlines / premiumIndexKlines | 是 | 日/月 | 2020-01 | 同骨架 |
| **fundingRate** | 是 | 月 | 2020-01 | 列 `calc_time,funding_interval_hours,last_funding_rate` |
| **metrics**（持仓量等，5 分钟） | 是 | 仅日包 | 2020-09 | 列含 `sum_open_interest`、多空比 |
| aggTrades / trades / bookTicker | 是 | 日/月 | 2019-12 / 2023-05 | — |
| liquidationSnapshot | 否 | — | — | 归档已无 |

REST 限制：`openInterestHist` 仅最近 1 个月；`fundingRate` 可分页回溯。付费源：Tardis Academic $350/月（季付起）、Solo $700/月；CoinAPI 无真免费层；CCData 2026-05-21 起免费层下线；Kaiko 并购 Amberdata 后仅询价；Coinglass Hobbyist $29/月（清算）。**本期不买。**

| 存储候选 | 能力 | 成熟 | TCO | 对接 | 退出 | 总分 | 置信 |
|---|---|---|---|---|---|---|---|
| **Parquet 真身 + DuckDB 1.5.5 缓存** | 90 | 85 | 95 | 80 | 95 | 89.3 | 高 |
| ClickHouse 26.7 / chDB | 90 | 90 | 55 | 70 | 70 | 76.3 | 中 |
| ArcticDB 3.0（BSL 1.1） | 80 | 75 | 80 | 70 | 60 | 74.5 | 中 |
| QuestDB 9.4 | 85 | 80 | 55 | 70 | 70 | 73 | 中 |
| TimescaleDB 2.26 | 65 | 85 | 50 | 75 | 75 | 68.8 | 中 |

关键证据：DuckDB ASOF 本机实测（1.5.5）：`>=` 与 `>` 均支持，`>` 严格取更早 bar，右表同时间戳多行时结果稳定但顺序未文档化 → 右表先去重。[DuckDB AsOf Join, https://duckdb.org/docs/current/guides/sql_features/asof_join]（高）。

风险与反驳（对抗轮，全部采纳为闸门）：月包曾静默缺整整两天。[binance-public-data #297, https://github.com/binance/binance-public-data/issues/297]（高）；SPOT 2025-01-01 起时间戳微秒、期货仍毫秒。[README, https://github.com/binance/binance-public-data/blob/master/README.md]（高）；旧 CSV 无表头新有表头混用；DuckDB 磁盘不足可损坏库文件 [#9667]、v1.4 曾破坏兼容。→ 入湖闸门：逐文件 `.CHECKSUM` 校验、按月 bar 数核对、缺口日包回填、时间戳统一毫秒、表头检测；**再加（gpt-6 审稿补）**：每个 partition 一份不可变 manifest（URL、HEAD 元数据、checksum 及其来源与下载时间、下载/解析器版本、ZIP 内文件名与行数、主键时间范围、重复数、缺口、schema hash、源文件 hash、状态）；临时目录 + 原子 rename 入湖，同一 `(dataset, symbol, period, source_hash)` 幂等；异常只进 quarantine 不静默补零；区分"缺失"与"零成交量"；永续 symbol 上线/下线/合约迁移/交易暂停与时区边界入 manifest；funding 的 `calc_time` 与实际结算时点、可变 `funding_interval_hours` 做**结算账务单测**（不只是 ASOF 连接单测）；metrics 字符串时间统一到毫秒的舍入规则写死。**Parquet 为唯一真身，查询走 `read_parquet` 视图，.duckdb 可随时删除重建，单写者，版本进 lockfile。**

下一步：44 品种 × 4 类型 × 2025-01 起预热并出覆盖矩阵；mark/last/funding 三表 ASOF 对齐单测。

### Q4 order_plan 级回测引擎

| 候选 | 能力 | 成熟 | TCO | 对接 | 退出 | 总分 | 置信 |
|---|---|---|---|---|---|---|---|
| **共享领域模型：自研永续参考实现（主数字）+ Nautilus 1.227.0 适配器审计** | 80 | 85 | 65 | 95 | 60 | 77.8 | 高（源码） |
| 纯自研事件驱动模拟器 | 85 | 50 | 70 | 55 | 90 | 70.5 | 中 |
| 纯 Nautilus 壳策略 | 60 | 85 | 65 | 95 | 60 | 62 | 高 |
| hftbacktest | 90 | 80 | 20 | 50 | 60 | 60.5 | 高 |
| freqtrade backtesting | 55 | 85 | 50 | 40 | 60 | 58.8 | 中 |
| Backtesting.py 0.6.5 | 45 | 70 | 70 | 40 | 70 | 56.8 | 中 |
| vectorbt PRO | 60 | 75 | 45 | 40 | 40 | 55.3 | 中 |
| backtrader 原版 | 50 | 30 | 60 | 40 | 60 | 47.5 | 高 |

Nautilus 1.227.0 源码核验（PyPI sdist，sha256 与生产 uv.lock 一致，全部一手）：

| 审稿要求 | 源码事实 | 裁定 |
|---|---|---|
| 订单状态 accepted/rejected/working/partial/filled/canceled | 完整 FSM（`crates/execution/src/matching_engine/engine.rs`） | 支持 |
| 止损触发基准 mark vs last | `crates/backtest/src/exchange.rs` 无 `process_mark_price`；`engine.rs:3531-3543` MarkPrice 落默认分支按 bid/ask；`matching_core/mod.rs:594 is_touch_triggered` 用 bid/ask | **不支持**，自研层补 |
| 资金费率 | backtest 与 matching 目录 `funding` 零命中 | **不支持**，自研层后算 |
| bar 驱动语义 | `engine.rs:1412/1496`：O→H→L→C 四个 TradeTick，`bar_adaptive_high_low_ordering` 可 L 先；每 tick 后 bid=ask=last | tick 化 OHLC |
| post-only 拒单 | `engine.rs:2985`：限价穿越 bid/ask 即拒（"would have been a TAKER"） | 支持，但 bar 下退化为"穿越 last" |
| IOC 余量取消 | `engine.rs:3038/4346` | 支持 |
| tick/step/min notional | Instrument 校验 | 支持 |
| 同 bar 双触上下界 | 只有自适应排序一种 | 自研层跑乐观/悲观双界 |
| 限价成交条件 | `is_limit_matched` 触及即成交 | 偏乐观，用 `FillModel.prob_fill_on_limit<1` 或自研保守界 |
| 外部订单计划 | Rust 层 `exchange.send(TradingCommand)` 存在；Python 走无信号逻辑壳策略 | 可承接 |

风险与反驳：官方 Issue #2194（2025-01）承认 FillModel "only very basic"，#1476 报过 EOD bar 权益曲线算错（高）。第一轮对抗审稿主张主次对调，已采纳；gpt-6 审稿进一步指出"两引擎互证"不成立并给出第三条路，已采纳：

- **架构**：共享一个项目内的 `order_plan → canonical execution events` 领域模型（先冻结事件 schema、价格/数量单位、时间边界、订单优先级、资金费率账务事件）；mark 触发、funding 账务、同 bar 双界作独立模块；自研不是"另一个完整撮合器"，而是缺失语义的参考模型与账务层；Nautilus 通过适配器消费规范化订单与市场事件并输出其 FSM 事件。"参考模型"与"生成测试期望的 helper"必须分离。
- **三层验收**：① 规则不变量测试（撮合前后数量守恒、手续费/资金费率账务守恒、reduce-only 不增仓、GTD/IOC 终态唯一、订单与成交可重放）；② 独立事件模型：手工构造最小 episode 的期望结果，覆盖 mark 与 last 分离、资金费率结算边界、同 bar 双触发、跳空、部分成交；③ Nautilus 对拍只对订单状态、filter、OrderList/GTD、价格精度、可归属事件做差异审计，永续扩展差异单列，不以"序列一致"作总体正确性证明。每个 episode 保存输入 manifest、两引擎版本、随机种子、事件 diff 与解释码。
- **注意**：用 markPriceKlines 作驱动 bar 只是触价型触发的近似，不是 mark 触发的等价实现；funding 后算会改变保证金与强平边界，账务层要显式建模而非事后加减。
- 批量按"单 engine 一次长跑、壳策略按时间表调度全部计划、按 order tag 归因"，避免每计划冷启动。LGPL-3.0 对内部不分发使用零义务，不列风险。本机 Python 3.14 + uv 可零编译安装 1.227.0（PyPI 最新 1.231.0）。

下一步：先冻结事件 schema 与不变量测试集；手工构造 ≥10 个最小 episode 作独立期望；再取 20 个唯一可归属纯机器人 episode（M3-E）两引擎回放并与 `execution_events` 对账（数量 ≤1 步进、价格 ≤1 tick），差异按解释码归类。止损：不变量测试或最小 episode 期望不过即停止政策研究，先修参考模型。

### Q5 指标特征、研究协议与统计

| 子项 | 推荐 | 总分 | 置信 | 关键证据 |
|---|---|---|---|---|
| 指标库 | TA-Lib 0.7.1 官方 wheel（Py3.9–3.14） | 84.5 | 高 | [PyPI, https://pypi.org/project/TA-Lib/]；pandas-ta 原版 2025-07 起付费订阅、PyPI 历史被清 → 排除；pandas-ta-classic 0.6.52（MIT）备选 |
| 特征字典 | "Alpha158 变体"：Qlib Alpha158 表达式在 polars 复现，不引框架 | 76 | 中高 | 158 条全单标的时序、只用正向 `Ref`（源码 `qlib/contrib/data/loader.py`）；`polars_ta` 现成约 70%，BETA/RSQR/RESI/CNTP/CNTN/SUMP/SUMN/WVMA 自写；**窗口按墙钟时间重定（原为交易日）、`$vwap` = bar VWAP（与 Qlib 日 VWAP 非同量）、信号按 `close_time <= signal_ts` 对齐** |
| 开源因子集 | Alpha101 只借 18 条纯时序；Alpha191/AlphaGen 不用；加密专属 3–5 维自建 | — | 中高 | 详见 research/r1-factor-sets-quickcheck.md；加密因子库不存在 |
| 维数起点 | N/10（EPV≥10）只是**预注册的保守起点**，不是上限定律 | — | 中 | Peduzzi 1996 / van Smeden 2016 是特定模型下的经验启发式；Alpha158 滚动特征高度相关、同事件簇重叠，**有效样本远小于正事件数**。须同时报告每折有效事件数、事件簇数、重叠率、特征相关矩阵/有效秩、缺失率、标签基准率；用折内嵌套选择/稳定性选择/正则化；最终维数由时间外表现、CI 与敏感性分析决定；**禁止用全样本 N 先定维数再做 CV** |
| as-of 对齐 | polars `join_asof` / DuckDB ASOF；等号语义单测；自写截断重算 + NaN 注入两类前视测试 | 85 | 高 | 无成熟 PIT 校验包 |
| 预注册/锁箱 | git + JSONL + sha256 manifest + OpenTimestamps 0.7.2 | 82 | 高 | DVC/MLflow 只记账；W&B 云依赖；Guild AI 停更 |
| 主 CV | 自写 AFML `PurgedKFold(t1)` + embargo 走前，**验收**：先写枚举所有区间交集的 reference implementation，再对随机区间、相同时间戳、不同资产、变长标签、空折、边界点做性质测试；明确 `[t0,t1]` 闭开语义、embargo 按时间还是按样本、缺失 t1 处理；每个 split 输出 train/test 索引、purge 原因、embargo 时间范围；**加 cluster-level split**（按交易员/信号源/仓位 episode）防跨折泄漏；purgedcv 0.1.6 / skfolio 旁证；`TimeSeriesSplit(gap=)` 只作定长标签基线；N<300 不用 CPCV | 74 | 中 | purgedcv 事件级 purge 语义正确但无第三方对拍；skfolio 按行数 purge |
| bootstrap | arch 8.0 + 自写事件簇 block（arch 无簇索引） | 75 | 高 | [arch docs, https://bashtage.github.io/arch/bootstrap/timeseries-bootstraps.html] |
| 规则模型 | imodels 3.0.0（2024-08）+ sklearn 浅树/LR | 74 | 中 | 25 个月无 release |
| LLM 假设循环 | 借 RD-Agent hypothesis/experiment/feedback trace 与 AlphaGen `Expression` 表示；评估器换事件级 | — | 中 | Alpha-GPT 无官方代码 |

## 4. 被排除的候选及原因

| 候选 | 原因 |
|---|---|
| Argilla | 2025-08 起无功能提交、近 90 天零 commit；部署重 |
| doccano | 任务类型不匹配多字段表单 |
| pandas-ta 原版 | 2025-07 许可/维护变更、PyPI 历史被清 |
| tulipy / finta | 7 年未更 / 已归档 |
| mlfinlab | 商业许可 |
| timeseriescv / Guild AI / Sacred | 2018 / 2023 / 低活跃 |
| hftbacktest | 需逐笔 L2，免费归档不存在 |
| backtrader 原版 | 官方停维、GPL |
| vectorbt PRO | 闭源付费、无订单状态机 |
| Tardis / CoinAPI / Kaiko / CCData | 超单人量级或无免费层 |
| ClickHouse / QuestDB / TimescaleDB | 个位数 GB 单人场景多余的常驻服务 |
| ArcticDB | BSL 1.1 许可边界 + 无 SQL ASOF + 专有格式 |
| Alpha191 / AlphaGen / Alpha360 | 横截面为主 / 不可解释 / 360 维滞后原值 |

## 5. 可执行的下一步（PoC 清单）

| # | PoC | 成功判据 | 止损判据 | 工时 |
|---|---|---|---|---|
| 1a | 单频道：TDesktop JSON + 媒体分开归档，记录消息数/媒体数/总字节/最大单文件/墙钟/失败点；Telethon 次号同频道对照拉取带日志 | 可重跑、可续传、目录清单与 JSON 一一对应；对齐报告（`peer_id/message_id`、Jaccard、差集六类归因）无未解释缺失；FloodWait 基线记录 | 冻结提示 / FloodWait > 1h / 不可恢复区间 → 停 | 1.5 天 |
| 1b | 五频道扩展 | 规模、耗时、字节、缺失分类全部有记录；任一不可恢复区间不进规范样本 | 同上 | 2 天 |
| 2 | Binance Vision 预热：44 品种 × klines/markPriceKlines/fundingRate/metrics × 2025-01 起，含 per-partition manifest、原子 rename 幂等、quarantine、funding 结算账务单测 | 覆盖矩阵通过标准：每 partition 行数 = 期望 bar 数或缺口有归因；重复主键 0；schema hash 一致；quarantine 清单为空或逐条有解释 | 任一 partition 无法归因 → 该 symbol 排除出 M3-R | 1.5 天 |
| 3 | 不展示预测的分层人工金标集（含 50–100 张截图）+ 解析器/LLM 从原文并行抽取 + 字段级共错率 + 分层抽审设计 | 图片字段纠错率 ≤ 30%；各字段共错率有 Wilson 区间报告 | > 30% 转人工录入；共错率显著高于独立假设的字段 100% 人工 | 2.5 天 + 人工 3 小时 |
| 4 | Label Studio 3/5 档止盈样例（Repeater）+ 导出 diff + 备份恢复；锁箱重建校验脚本 | 表单可表达、导出含 predictions 与 annotations；校验脚本能从 JSONL/manifest/签名/`.ots` 重建且失败即报错 | 表达不了 → Streamlit 备胎 | 1 天 |
| 5 | 冻结 order_plan → 规范执行事件 schema；不变量测试集；≥10 个手工最小 episode 期望；自研永续参考实现 v0（mark 触发 / funding 账务 / 双界）；Nautilus 适配器单 engine 壳策略；20 个纯机器人 episode 对账 | 不变量与最小 episode 全过；对账数量 ≤1 步进、价格 ≤1 tick，差异按解释码 100% 归类 | 不变量/最小 episode 不过 → 先修参考模型，不进 M3-R | 6 天 |
| 6 | Alpha158 变体在 polars 复现（逐算子契约）+ 8 算子自写 + 两类前视测试 + 数出已闭合正事件 N、事件簇数、重叠率、特征有效秩 | 截断重算一致；N/簇数/有效秩报告齐；维数起点 N/10 预注册，最终维数留给折内嵌套选择 | — | 2.5 天 |
| 7 | `PurgedKFold(t1)` 参考实现（枚举区间交集）+ 优化实现性质测试 + cluster-level split + purgedcv 对拍 + 事件簇 bootstrap | 性质测试全过；对拍数值一致；每 split 输出 purge 原因与 embargo 范围 | — | 1.5 天 |

## 6. 待确认问题

- OpenAI 中档模型官方单价（页面 403，需人工打开）。
- tdl JSON 字段、TDesktop 导出 `edited`/`reply_to_message_id`（各实跑一次）。
- Label Studio `<Repeater>` 实跑；imodels `get_rules()` 输出格式。
- teleproto npm 维护数据（`npm view teleproto`）作生产 GramJS 债务证据。
- 是否有可用的有历史次号；没有则 Telethon 补拉环节全部改由 TDesktop 承担。

## 7. 来源表（精选，完整清单见 research/ 各轮文件）

| 来源 | URL | 日期 | 可信度 |
|---|---|---|---|
| Telethon PyPI release history | https://pypi.org/project/Telethon/#history | 2026-09-07 抓取 | 高 |
| Telethon Issue #3226 UPDATE_APP_TO_LOGIN | https://github.com/LonamiWebs/Telethon/issues/3226 | 2021 | 高 |
| Telegram 冻结机制说明 | https://tginfo.me/frozen-account-en/ | 2025 | 高 |
| gram-js/gramjs 归档 | https://github.com/gram-js/gramjs | 2026-07-14 | 高 |
| Correlated Errors in LLMs (ICML 2025) | https://arxiv.org/pdf/2506.07962 | 2025-06 | 高 |
| Label Studio PyPI | https://pypi.org/project/label-studio/ | 2026-03-13 | 高 |
| Argilla commits | https://github.com/argilla-io/argilla/commits/develop | 2026-09-07 抓取 | 高 |
| Claude pricing | https://claude.com/pricing | 2026-09-07 抓取 | 高 |
| Gemini pricing | https://ai.google.dev/gemini-api/docs/pricing | 2026-09-07 抓取 | 高 |
| Binance Vision 归档（本机实测） | https://data.binance.vision/ | 2026-09-07 | 实测 |
| binance-public-data #297 缺两天 | https://github.com/binance/binance-public-data/issues/297 | 2022 | 高 |
| Binance openInterestHist 30 天限制 | https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics | 2026 | 高 |
| Tardis 价目 | https://tardis.dev/ | 2026-09-07 抓取 | 中 |
| DuckDB 1.5.5 / ASOF | https://duckdb.org/docs/current/guides/sql_features/asof_join | 2026 | 高 + 实测 |
| duckdb #9667 | https://github.com/duckdb/duckdb/issues/9667 | 2023 | 高 |
| NautilusTrader 1.227.0 sdist | https://files.pythonhosted.org/packages/…/nautilus_trader-1.227.0.tar.gz | 2026-05-18 | 一手源码 |
| nautilus_trader #2194 / #1476 | https://github.com/nautechsystems/nautilus_trader/issues/2194 | 2025-01 / 2024 | 高 |
| TA-Lib PyPI | https://pypi.org/project/TA-Lib/ | 2026-07 | 高 |
| Qlib Alpha158 loader / ops | https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py | 2026 | 高 |
| Qlib #1514 label 定义 | https://github.com/microsoft/qlib/issues/1514 | — | 高 |
| purgedcv | https://github.com/eslazarev/purged-cross-validation | 2026-09-04 | 高 |
| skfolio CombinatorialPurgedCV | https://skfolio.org/generated/skfolio.model_selection.CombinatorialPurgedCV.html | 2026 | 高 |
| arch 8.0 bootstrap | https://bashtage.github.io/arch/bootstrap/timeseries-bootstraps.html | 2025-10 | 高 |
| imodels releases | https://github.com/csinva/imodels/releases | 2024-08-03 | 高 |
| opentimestamps-client | https://pypi.org/project/opentimestamps-client/ | 2024-12-31 | 高 |
| Peduzzi 1996 EPV | https://pubmed.ncbi.nlm.nih.gov/8970487/ | 1996 | 高 |
