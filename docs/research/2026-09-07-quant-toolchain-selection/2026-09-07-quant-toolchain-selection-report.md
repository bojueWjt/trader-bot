# 频道种子→量化算法：研究工具链技术选型报告

范围：为 monorepo 内新建的独立量化研究 app（上架前与生产完全分开）选定 M0–M3 工具栈，五个问题：Telegram 全量历史抓取、信号结构化抽取与人工确认、行情数据与研究湖存储、order_plan 级回测引擎、指标特征与研究协议/统计。
分析期：2025-01-01 → 2026-09-07 ｜ 调研截止日：2026-09-07 ｜ 决策人：Balen
方法：深度档三轮（广度收集 → 关键验证 → 对抗校验 + 定量比较），8 个并行调研 agent + 3 项本机实测（Binance 归档 HEAD/解压、NautilusTrader 1.227.0 源码、DuckDB 1.5.5 ASOF）。过程笔记与逐轮发现见 `research/`。
2026-09-08 修订：按 gpt-6-astra 对 Q3–Q5 的独立审稿（`docs/reviews/2026-09-08-gpt6-review-of-toolchain-q3q4q5.md`，5 必修）修正 Q4 双引擎职责、Q3 入湖闸门、Q5 维数上限与 PurgedKFold 验收；按其对 Q1–Q2 的审稿（`…-q1q2.md`，6 必修）把 TDesktop 定义为一次性规范快照、去相关改为待实测的异质校验、抽审改为分层设计并修正总分算术；**本报告各表为唯一权威，`research/round3-findings.md` 中的旧分数不再引用**。
2026-09-10 修订（不沿用任何既有 review 结论，按 gpt-6-astra 从零独立审稿 `docs/reviews/2026-09-10-gpt6-fresh-review-of-toolchain-selection.md`，12 必修）：① 修正本报告自己的源码核验错误——普通止损用 `is_stop_matched`（买 ask≥触发价、卖 bid≤触发价，`matching_core/mod.rs:584-588`），`is_touch_triggered`（:594）是触价单、方向相反；`get_trailing_activation_price`（`engine.rs:3523`）只管追踪止损激活，MarkPrice 落 bid/ask 的结论仅对该函数成立；"bid=ask=last"只对 LAST/MID 类型 bar 成立（`engine.rs:1482-1492` 分 LAST/MID、BID、ASK 路径，`PriceType::Mark` 直接 panic；`book.rs:1116-1131` 把 trade.price 同时写入 bid/ask）；bar 拆成**至多**四个合成价点（未变化的价点跳过），时间戳全为 bar.ts_init；"funding 零命中"只对 Rust 撮合/结算路径成立，Python `backtest/data_client.pyx:311` 支持订阅 FundingRateUpdate，只是没有默认结算器；MarkPriceUpdate 在 `backtest/engine.rs:1018-1023` 被路由绕过交易所。② 撤回"主数字只能来自自研参考模型"：`backtest/modules.pyx:42` 的 `SimulationModule` + `:200` `exchange.adjust_account` 提供"单 Nautilus 内核 + 外部 mark 条件控制器 + 同时序 funding 模块"的第三条路，须与自研参考实现在同一最小 episode 集上比较后再定。③ "乐观/悲观双界"改称"选定路径情景区间"（O→H→L→C 与 O→L→H→C 不构成一般收益上下界，跳空与追踪止损可给出更差结果）。④ 全部评分由脚本按公布权重重算（见 §2）。

## 1. 结论先行

| 问题 | 推荐方案 | 总分 | 证据强度 |
|---|---|---|---|
| Q1 Telegram 全量历史 | **Telegram Desktop 官方导出**作**一次性规范快照**（JSON 元数据与媒体物件分开归档，不是自动化采集器）；**Telethon 1.44**（次号、独立 session、锁 Codeberg tag）做按 `peer_id/message_id` 补拉与增量对照；tdl 补媒体 | 80 | 方向性高，可执行性待 PoC |
| Q2 抽取与确认 | **LLM Batch + 直接读原文/OCR 的确定性解析器**作**异质校验路径**（相关性待实测），分歧交第二家 LLM 裁决，一致集**分层抽审**（预设最小样本与 Wilson 区间，10% 只是初始预算）；**Label Studio 1.23.0** 社区版确认；锁箱 = JSONL + sha256 manifest + OpenTimestamps，附重建校验脚本 | 82 | 方向性高，可执行性待 PoC |
| Q3 行情与存储 | **data.binance.vision 全免费**（klines / markPriceKlines / indexPriceKlines / fundingRate 月包自 2020-01；metrics 持仓量日包自 2020-09）+ 入湖完整性闸门；**Parquet 为唯一真身，DuckDB 1.5.5 为可丢弃缓存** | 89 | 实测 |
| Q4 回测引擎 | **共享一个 order_plan → 规范执行事件的领域模型**（只共享 schema、单位、事件封装，不共享撮合判定 helper）；永续三要素（mark price 触发、资金费率账务、同 bar 路径情景区间）作独立模块。**主执行内核未定型，两条候选路线用同一最小 episode 集比较后再选**：A) 自研永续参考实现出主数字 + Nautilus 1.227.0 适配器审计 FSM/filter；B) 单 Nautilus 内核 + `SimulationModule` 外部 mark 条件控制器 + 同时序 funding 模块（`modules.pyx:42/:200`）。三层验收：规则不变量测试、手工最小 episode 独立期望、差异审计 | 78.5（组合评分，见 §2） | 源码事实高；架构选择待比较 |
| Q5 特征与统计 | **TA-Lib 0.7.1** + **"Alpha158 变体"**（新实现，不靠名称背书；窗口按墙钟、`$vwap` = bar VWAP）；**先冻结 estimand 与分母**（所有候选计划 / 入选 / 未成交 / 盈亏 / 删失分别计数），首轮只做预先指定的少量可解释特征 + 固定规则基线，N/10 仅作预注册起点；**未来表现主评估 = expanding/rolling walk-forward（只训练测试起点前可用且标签已完成的样本）**，自写 `PurgedKFold(t1)`（参考实现 + 性质测试 + episode 隔离）只回答稳定性问题；`arch` 8.0 + 共同日历块簇 bootstrap；`imodels` 3.0 规则模型；LLM 假设尝试记账并预留一次最终时序测试 | 76–85 | 高/中 |

三条贯穿结论：

1. **数据全部免费，不买任何付费源。** 历史 mark price 1 分钟线、资金费率、持仓量都在 Binance 官方归档里；付费源（Tardis $350–700/月、CoinAPI、Kaiko）超单人量级。唯一缺的是清算数据（Coinglass $29/月起），本期不需要。
2. **回测引擎：Nautilus 默认实现有三处永续缺口，但缺口不推出唯一架构。** 本机读 1.227.0 源码证实：内置模拟交易所不消费 MarkPriceUpdate（回测引擎在路由层绕过）、没有默认永续 funding 结算器、LAST/MID 类型 bar 驱动时合成成交价同时写入 bid/ask。补缺口有两条候选路线（自研参考实现 vs Nautilus `SimulationModule` 扩展），须在同一最小 episode 集上比较代码量、可验证性、维护面后再定；无论哪条，独立真值来自不变量测试与手工最小 episode，不靠两引擎互证。
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
- 总分由脚本按权重复算（2026-09-10，`S=(30×能力+25×成熟+20×TCO+15×对接+10×退出)/100`，保留两位）；2026-09-08 手工复算有错（Label Studio 应为 81.75 而非 82.75），已全部以脚本值替换。Q4 组合方案的五维分为组合估计，另附实施成本说明。
- 评分权重未反映真正的硬约束（永续语义、时点正确性、账号可见性）；重算后各问题 TOP1 方向不变，但 Q4 纯 Nautilus（72.5）与纯自研（69.25）顺序翻转、组合仅领先纯 Nautilus 6 分，不能沿用"组合大幅领先"的印象。

## 3. 逐项证据卡

### Q1 Telegram 全量历史抓取

| 候选 | 能力 | 成熟 | TCO | 对接 | 退出 | 总分 | 置信 |
|---|---|---|---|---|---|---|---|
| **TDesktop 官方导出（一次性规范快照）** | 70 | 90 | 60 | 70 | 90 | 75.0 | 高 |
| **Telethon 1.44（次号补拉/增量）** | 90 | 65 | 85 | 70 | 90 | 79.75 | 高 |
| Kurigram / pyrofork | 85 | 60 | 80 | 70 | 85 | 75.5 | 中 |
| TDLib 绑定 | 85 | 70 | 45 | 70 | 70 | 69.5 | 中 |
| tdl（Go CLI） | 60 | 70 | 70 | 70 | 80 | 68.0 | 中 |
| GramJS / teleproto | 85 | 40 | 60 | 80 | 80 | 67.5 | 高 |

关键证据：
- Telethon 2026 发 1.43.0/1.43.1/1.43.2/1.44.0（最新 2026-06-15），作者明文"v1 大体处于维护模式"；GitHub 仓库 2026-02-21 归档迁 Codeberg。[PyPI release history, https://pypi.org/project/Telethon/#history]（高）
- layer 断崖先例：Telegram 抬高最低 layer 时旧客户端直接 `RPCError 406 UPDATE_APP_TO_LOGIN`，作者靠"赶工发布"救回。[Issue #3226, https://github.com/LonamiWebs/Telethon/issues/3226]、[Changelog, https://docs.telethon.dev/en/stable/misc/changelog.html]（高）
- 2025-05 冻结机制：冻结账号不能发/收消息、不能重登录或加设备。[tginfo.me/frozen-account-en, https://tginfo.me/frozen-account-en/]（高）
- 会话隔离硬约束：同 session 两处用 → `database is locked`；同 auth key 两处登录 → `AuthKeyDuplicatedError` 踢线。[Telethon FAQ, https://docs.telethon.dev/en/stable/quick-references/faq.html]（高）
- 生产依赖 GramJS `telegram@2.26` 2026-07-14 归档，README 推荐 teleproto。[gram-js/gramjs, https://github.com/gram-js/gramjs]（高）
- 编辑历史不可回溯：`getHistory` 与 TDesktop 导出只给最终版 + `edit_date`。（高）

风险与反驳：只读频道历史属低风险类，2025–2026 无"纯只读拉历史被封"公开案例（搜索词 `telegram userbot ban scraping channel history telethon 2025`）；风险集中在新号/虚拟号（Issue #3955/#4150）。审稿人指出全量抓取中一个未知 constructor 会抛 `TypeNotFoundError` 断游标，需外套跳过并记录。**gpt-6 审稿补（2026-09-08）**：① 五频道数万条含图消息的导出规模未证明可接受——按 5 万条 × 40% 含图 × 0.5–2 MB 估 10–40 GB 媒体，需分离"JSON 元数据快照"与"媒体物件归档"，按频道逐个导出、预留 2 倍空间、记录消息数/媒体数/总字节/最大单文件/墙钟/失败点，成功判据是"可重跑、可续传、目录清单与 JSON 一一对应"；② 对齐键必须是 `peer_id/message_id`（另存 `grouped_id`、`date`、`edit_date`、`reply_to_message_id`、`media_relpath`），一致率定义为 `|A∩B|/|A∪B|` 且差集逐条分类（仅 TDesktop / 仅 Telethon / 字段不一致 / 媒体缺失 / 相册拆分 / 已删除），5 万条的 1% 是 500 条信号，不能容许未解释缺失；③ 两客户端都只给最终编辑版，**编辑过且无原始版本证据的信号隔离或做敏感性分析**，从现在起追加存储编辑/删除事件与内容哈希；④ "次号可用"是门槛不是默认前提，没有次号则 Telethon 环节全部由 TDesktop 承担。**2026-09-10 从零审稿补**：⑤ 最终编辑版不能回填成原始可交易信号——保留 `message_date` / `edit_date` / `snapshot_at` / 首次实收时间 / 内容哈希建立版本来源；未知原始内容的消息可用于结构研究，**收益研究只允许从可证明的可用版本时间开始**，或作为不确定样本单列；删帖造成的幸存偏差不能靠"现存历史一致"消除，推论总体限定为可观察消息并另建前瞻样本；⑥ 双客户端一致不证明全量——权限不足、管理员删帖、加入前不可见会让两端共同缺失而 Jaccard 仍为 1；每频道冻结观测窗与高水位 message_id，登记账号权限、最早可见日期、过滤设置、导出起止；统一 peer 类型与 ID 编码；媒体另设内容 hash / 类型 / 尺寸 / 下载状态，相册多图归组；不可恢复图片、空文、服务消息保留在清单中，说明消息→候选→计划每步损耗；断点恢复、重拉最近窗口、编辑/删除增量、file reference 过期重取列为 PoC 项；⑦ 冻结不等于立即使账号和 session 全部作废，按官方说明分阶段处理。

下一步：先单频道——TDesktop 导出 JSON + 媒体（分开归档）并记录规模与耗时；Telethon 次号对同一频道拉一次带日志（`wait_time=1`、`flood_sleep_threshold=120`）记 FloodWait 基线；出对齐报告（主键 `peer_id/message_id`、Jaccard 一致率、差集分类）。通过后再扩到五频道。止损：账号出现冻结提示即停，回落 TDesktop；任一不可恢复区间不进规范样本。

### Q2 LLM 结构化抽取 + 人工确认

| 候选（确认工具） | 能力 | 成熟 | TCO | 对接 | 退出 | 总分 | 置信 |
|---|---|---|---|---|---|---|---|
| **Label Studio 1.23.0 社区版** | 85 | 85 | 80 | 70 | 85 | 81.75 | 高 |
| Prodigy | 85 | 85 | 55 | 70 | 60 | 74.25 | 中 |
| 自建 Streamlit 审核页（备胎） | 75 | 60 | 65 | 70 | 90 | 70.0 | 中 |
| doccano | 40 | 50 | 80 | 70 | 80 | 59.0 | 中 |
| Argilla 2.x | 80 | 30 | 45 | 70 | 70 | 58.0 | 高 |

关键证据：
- Label Studio 1.23.0（2026-03-13），季度发版，Apache-2.0。[PyPI, https://pypi.org/project/label-studio/]（高）；predictions 预填 + "Show predictions to annotators"。[Import pre-annotated data, https://labelstud.io/guide/predictions]（高）；导出含 `completed_by`/`created_at`/`lead_time`。[JSON format, https://labelstud.io/blog/understanding-the-label-studio-json-format/]（高）
- Argilla `develop` 最近提交 2025-08-05，最后功能 PR 2025-03-10，近 90 天零 commit。[commits, https://github.com/argilla-io/argilla/commits/develop]（高）→ 出局
- 价格：Claude Sonnet 5 $2/$10，Batch 半价。[claude.com/pricing]（高）；Gemini 3.x Flash $0.75/$3.75（促销至 2026-12-31 后翻倍），Batch 半价，图片按 token 计入。[ai.google.dev/gemini-api/docs/pricing]（高）。千条 $0.1–5，5000 条双模型 <$30。
- 相关错误：350 个模型、榜单与简历筛选等数据集上，两模型都错时约 60% 给出同一错答案，越强越相关。[Correlated Errors in LLMs, ICML 2025, https://arxiv.org/pdf/2506.07962]（高，**但任务域不同，不可直接外推到交易信号**）
- OpenTimestamps 客户端 0.7.2（2024-12-31），停更但协议稳定。[PyPI, https://pypi.org/project/opentimestamps-client/]（高）

风险与反驳：截图价格标签读数（OCR + 空间关联）无任何公开基准，已有评测只说明 VLM 看形态不可靠。[gist 40 条信号审计, https://gist.github.com/roman-rr/c1cd675f7c35b68ae5ac281c30080166]（中）。约束库（Instructor/BAML/LangExtract/Outlines）在厂商原生结构化输出下非必需。**gpt-6 审稿补（2026-09-08）**：① "去相关"是过度表述——确定性解析器只对它能覆盖的语法提供不同错误机制，处理不了"关注区域""分批止盈看情况""图文冲突"这类语义缺省；改为"异质校验路径，相关性待实测"，解析器必须直接读原始文本/OCR 中间产物而不是 LLM 草稿，LLM 输出保留原文 span/图像坐标，按字段（文本/价格/方向/多档止盈）测两路径的错误共现率、漏报率、拒答率；② 一致集抽审改为**分层随机抽样**（层：频道、文本/图片、信号模板、模型版本、字段类型、置信度），预设绝对最小样本与停止规则，报每层错误率与 Wilson 区间——零错误时单侧 95% 上界为 `1−0.05^(1/n)`，n=10 约 25.9%、n=100 约 2.95%、n=299 才低于 1%；低置信、价格字段、新模板做 100% 复核；③ 先建**不展示预测**的人工金标集再做模型评测，防预填锚定；标签分 `human_verified` / `auto_accepted_under_audit` / `ambiguous`，未抽记录不得冒称人工真值；两路都空或共享 OCR 不算强一致，图文冲突必须弃权不补默认价。**2026-09-10 从零审稿补**：④ 共错率不显著高于独立假设不等于绝对错误率够低，百级样本功效不足；分歧交第二家 LLM 不能变成人工真值；预注册**整单**关键字段错误率、数字级容差、漏检信号率、拒答率，价格/方向/数量/品种映射以人工金标验收，困难样本人工最终确认或拒收；独立二项 0/100 错的双侧 95% Wilson 上界约 3.70%，0/50 约 7.13%，要低于 1% 约需 381 个独立样本，图或消息簇相关时还要调整；30% 图片纠错率只是人工工作量止损线，另定准入错误率目标与收益对标签错误的敏感性；⑤ 哈希锁箱不是盲评锁箱——OpenTimestamps 只证存在时间，不证内容正确或研究者没看过测试集；抽取金标、开发标签、最终检验集按时间与 episode 先分配，最终集只限评估入口；确认界面除隐藏预测还要隐藏后续走势、战报、未来回复；预注册主指标、排除规则、成本、路径情景、尝试预算、停止条件，每次开箱登记请求与不可变结果；锁定 dataset / prompt / 模型版本 / schema / 人工修订 / 代码环境哈希。

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
| **Parquet 真身 + DuckDB 1.5.5 缓存** | 90 | 85 | 95 | 80 | 95 | 88.75 | 高 |
| ClickHouse 26.7 / chDB | 90 | 90 | 55 | 70 | 70 | 78.0 | 中 |
| ArcticDB 3.0（BSL 1.1） | 80 | 75 | 80 | 70 | 60 | 75.25 | 中 |
| QuestDB 9.4 | 85 | 80 | 55 | 70 | 70 | 74.0 | 中 |
| TimescaleDB 2.26 | 65 | 85 | 50 | 75 | 75 | 69.5 | 中 |

关键证据：DuckDB ASOF 本机实测（1.5.5）：`>=` 与 `>` 均支持，`>` 严格取更早 bar，右表同时间戳多行时结果稳定但顺序未文档化 → 右表先去重。[DuckDB AsOf Join, https://duckdb.org/docs/current/guides/sql_features/asof_join]（高）。

风险与反驳（对抗轮，全部采纳为闸门）：月包曾静默缺整整两天。[binance-public-data #297, https://github.com/binance/binance-public-data/issues/297]（高）；SPOT 2025-01-01 起时间戳微秒、期货仍毫秒。[README, https://github.com/binance/binance-public-data/blob/master/README.md]（高）；旧 CSV 无表头新有表头混用；DuckDB 磁盘不足可损坏库文件 [#9667]、v1.4 曾破坏兼容。→ 入湖闸门：逐文件 `.CHECKSUM` 校验、按月 bar 数核对、缺口日包回填、时间戳统一毫秒、表头检测；**再加（gpt-6 审稿补）**：每个 partition 一份不可变 manifest（URL、HEAD 元数据、checksum 及其来源与下载时间、下载/解析器版本、ZIP 内文件名与行数、主键时间范围、重复数、缺口、schema hash、源文件 hash、状态）；临时目录 + 原子 rename 入湖，同一 `(dataset, symbol, period, source_hash)` 幂等；异常只进 quarantine 不静默补零；区分"缺失"与"零成交量"；永续 symbol 上线/下线/合约迁移/交易暂停与时区边界入 manifest；funding 的 `calc_time` 与实际结算时点、可变 `funding_interval_hours` 做**结算账务单测**（不只是 ASOF 连接单测）；metrics 字符串时间统一到毫秒的舍入规则写死。**Parquet 为唯一真身，查询走 `read_parquet` 视图，.duckdb 可随时删除重建，单写者，版本进 lockfile。** **point-in-time 闸门（2026-09-10 补）**：每行区分 `event_time` / `available_at` / `ingested_at`；特征与回测一律 ASOF LEFT JOIN 保留未匹配样本并限制最大年龄，mark/last 缺口不得无限前填；funding 的已结算值与当时预测费率分列，不得把最终费率提前当特征；bar 收盘时间等号是否可用取决于接收延迟与消息排序，不能只靠严格大于号；补闸门：UTC/时间单位、主键市场身份、OHLC 合法性、价格精度、成交量单位、排序、历史规则有效期；质量状态逐列传给特征和回测，quarantine 有解释不等于准许当正常数据；单文件原子 rename 不等于数据集快照，数据集级快照用 manifest 版本号（或 DuckLake）表达。

下一步：44 品种 × 4 类型 × 2025-01 起预热并出覆盖矩阵；mark/last/funding 三表 ASOF 对齐单测。

### Q4 order_plan 级回测引擎

| 候选 | 能力 | 成熟 | TCO | 对接 | 退出 | 总分 | 置信 |
|---|---|---|---|---|---|---|---|
| **共享领域模型 + 候选 A/B 二选一（自研参考实现 / Nautilus SimulationModule 扩展）** | 80 | 85 | 65 | 95 | 60 | 78.5（组合估计） | 源码高，架构待比较 |
| 纯自研事件驱动模拟器 | 85 | 50 | 70 | 55 | 90 | 69.25 | 中 |
| 纯 Nautilus 壳策略（无永续扩展） | 60 | 85 | 65 | 95 | 60 | 72.5 | 高 |
| hftbacktest | 90 | 80 | 20 | 50 | 60 | 64.5 | 高 |
| freqtrade backtesting | 55 | 85 | 50 | 40 | 60 | 59.75 | 中 |
| Backtesting.py 0.6.5 | 45 | 70 | 70 | 40 | 70 | 58.0 | 中 |
| vectorbt PRO | 60 | 75 | 45 | 40 | 40 | 55.75 | 中 |
| backtrader 原版 | 50 | 30 | 60 | 40 | 60 | 46.5 | 高 |

Nautilus 1.227.0 源码核验（PyPI sdist，sha256 与生产 uv.lock 一致，全部一手）：

| 审稿要求 | 源码事实 | 裁定 |
|---|---|---|
| 订单状态 accepted/rejected/working/partial/filled/canceled | 完整 FSM（`crates/execution/src/matching_engine/engine.rs`） | 支持 |
| 止损触发基准 mark vs last | `crates/backtest/src/engine.rs:1018-1023` 在路由层把 MarkPriceUpdate 绕过交易所；`exchange.rs` 无 `process_mark_price`；普通止损 `matching_core/mod.rs:557-565 match_stop_order → :584-588 is_stop_matched`（买 ask≥触发价、卖 bid≤触发价，只看 bid/ask）；`engine.rs:3523 get_trailing_activation_price` 仅追踪止损激活，MarkPrice 落默认分支取 bid/ask；`PriceType::Mark` 的 bar 在 `engine.rs:1492` panic | **内置不支持**；候选 A 自研层实现，候选 B 用 `SimulationModule` 外部 mark 条件控制器（顺序待验） |
| 资金费率 | Rust 撮合/结算路径无 funding 入账；Python `backtest/data_client.pyx:311` 可订阅 FundingRateUpdate；`modules.pyx:92 FXRolloverInterestModule` 示范 `exchange.adjust_account` 定时入账钩子 | **无默认结算器**；候选 B 可仿 FX rollover 模块做同时序 funding 入账，候选 A 在账务层实现；两者都要按结算前有符号仓位 × 结算 mark 入账并披露 mark 近似 |
| bar 驱动语义 | `engine.rs:1482-1492` 按 bar 价格类型分路：LAST/MID → 合成 TradeTick（至多四个价点 O/H/L/C，未变化者跳过，时间戳均为 bar.ts_init，`bar_adaptive_high_low_ordering` 可 L 先），`book.rs:1116-1131` 把成交价同时写入 bid/ask；BID/ASK 类型 bar → 合成 QuoteTick，bid/ask 分离 | tick 化 OHLC；"bid=ask=last"仅 LAST/MID 路径 |
| post-only 拒单 | `engine.rs:2985`：限价穿越 bid/ask 即拒（"would have been a TAKER"） | 支持，但 bar 下退化为"穿越 last" |
| IOC 余量取消 | `engine.rs:3038/4346` | 支持 |
| tick/step/min notional | Instrument 携带属性；撮合层命中主要是 precision / price increment | 部分：须逐条验证 PRICE_FILTER / LOT_SIZE / MARKET_LOT_SIZE / 最小名义额 / reduce-only / 订单组合在所选下单通道的实际执行 |
| 同 bar 双触 | 只有自适应排序一种 | 跑 O→H→L→C 与 O→L→H→C 两条**选定路径情景**，报告为情景区间而非上下界（跳空、追踪止损、多次触发可超出该区间） |
| 限价成交条件 | `is_limit_matched` 触及即成交 | 偏乐观，用 `FillModel.prob_fill_on_limit<1` 或自研保守界 |
| 外部订单计划 | Rust 层 `exchange.send(TradingCommand)` 存在；Python 走无信号逻辑壳策略 | 可承接 |

风险与反驳：官方 Issue #2194（2025-01）承认 FillModel "only very basic"，#1476 报过 EOD bar 权益曲线算错（高）。第一轮对抗审稿主张主次对调，已采纳；gpt-6 审稿进一步指出"两引擎互证"不成立并给出第三条路，已采纳：

- **架构**：共享一个项目内的 `order_plan → canonical execution events` 领域模型（只共享事件 schema、价格/数量单位、时间边界、订单优先级、资金费率账务事件的封装，**不共享撮合判定 helper**，否则制造共同错误）；mark 触发、funding 账务、路径情景作独立模块。**主执行内核两条候选**：A) 自研永续参考实现出主数字（须承担挂单队列、部分成交、撤改单、组合订单、账户约束的全部实现）+ Nautilus 适配器审计；B) 单 Nautilus 内核 + `SimulationModule`（`modules.pyx:42`）外部 mark 条件控制器 + 仿 `FXRolloverInterestModule` 的同时序 funding 模块（`:200 adjust_account`），须验证 mark→订单释放、资金入账、风控刷新的事件顺序。**用同一最小 episode 集比较代码量、可验证性、性能、维护面后定型**，不预设优胜。"参考模型"与"生成测试期望的 helper"必须分离。
- **三层验收**：① 规则不变量测试（撮合前后数量守恒、手续费/资金费率账务守恒、reduce-only 不增仓、GTD/IOC 终态唯一、订单与成交可重放）；② 独立事件模型：手工构造最小 episode 的期望结果，覆盖 mark 与 last 分离、资金费率结算边界、同 bar 双触发、跳空、部分成交；③ Nautilus 对拍只对订单状态、filter、OrderList/GTD、价格精度、可归属事件做差异审计，永续扩展差异单列，不以"序列一致"作总体正确性证明。每个 episode 保存输入 manifest、两引擎版本、随机种子、事件 diff 与解释码。
- **注意**：`PriceType::Mark` 的 bar 在 1.227.0 直接 panic，把 markPriceKlines 伪装成 LAST 喂入会污染成交价，不是可用修法；**estimand 先冻结**："事件级收益估计"与"账户资金曲线"两种口径分开，指定手续费、滑点、杠杆、仓位模式、资金不足处理；funding 用结算前有符号仓位 × 结算 mark 入账（归档只有费率与结算时间，结算 mark 由 1m markPriceKlines 近似并做敏感性），同时间 funding/平仓/GTD 顺序显式；强平证据不足时限制研究问题，不默认"永不强平"；多计划共享账户时逐计划收益相加不等于资金曲线。
- **Nautilus 审计范围**：Instrument 带 min_notional 不等于所选下单通道执行 Binance 全部 filter；须逐条列 PRICE_FILTER / LOT_SIZE / MARKET_LOT_SIZE / 最小名义额 / reduce-only / 订单组合的实际执行层，每条造合法与非法各一例验证拒绝事件；OrderList 不等同 Binance 原子 OCO；实盘对账拆成确定性状态对账与执行误差分布，"差异有解释码"之外设不可解释误差率上限。
- 批量按"单 engine 一次长跑、壳策略按时间表调度全部计划、按 order tag 归因"，避免每计划冷启动。LGPL-3.0 对内部不分发使用零义务，不列风险。本机 Python 3.14 + uv 可零编译安装 1.227.0（PyPI 最新 1.231.0）。

下一步：先冻结事件 schema 与不变量测试集；手工构造 ≥10 个最小 episode 作独立期望；再取 20 个唯一可归属纯机器人 episode（M3-E）两引擎回放并与 `execution_events` 对账（数量 ≤1 步进、价格 ≤1 tick），差异按解释码归类。止损：不变量测试或最小 episode 期望不过即停止政策研究，先修参考模型。

### Q5 指标特征、研究协议与统计

| 子项 | 推荐 | 总分 | 置信 | 关键证据 |
|---|---|---|---|---|
| 指标库 | TA-Lib 0.7.1 官方 wheel（Py3.9–3.14） | 84.5 | 高 | [PyPI, https://pypi.org/project/TA-Lib/]；pandas-ta 原版 2025-07 起付费订阅、PyPI 历史被清 → 排除；pandas-ta-classic 0.6.52（MIT）备选 |
| 特征字典 | "Alpha158 变体"：以 Qlib Alpha158 表达式为起点在 polars 复现，**是新的特征实现，不靠名称背书**，逐算子契约与前视测试独立验收 | 76 | 中 | 158 条全单标的时序、只用正向 `Ref`（源码 `qlib/contrib/data/loader.py`）；`polars_ta` 现成约 70%，BETA/RSQR/RESI/CNTP/CNTN/SUMP/SUMN/WVMA 自写；窗口按墙钟时间重定、`$vwap` = bar VWAP、信号按 `close_time <= signal_ts` 对齐 |
| 开源因子集 | Alpha101 只借 18 条纯时序；Alpha191/AlphaGen 不用；加密专属 3–5 维自建 | — | 中高 | 详见 research/r1-factor-sets-quickcheck.md；加密因子库不存在 |
| 分母与维数 | **先定义 estimand 与分母**：所有候选计划 / 入选 / 未成交 / 到期 / 盈亏 / 删帖 / 删失分别计数，报告样本数、结果事件数、独立簇数；"正事件"若指盈利交易则只数胜者会遗漏负类，若指有效信号则不等于 logistic 的 outcome events；N/10 只是预注册起点，从 158 筛到 10 的搜索自由度不等于 10；首轮只做预先指定的少量可解释特征 + 固定规则基线，折内样本预算决定是否训练；预设统一观察期或显式处理右删失，禁止只按最后闭合者分析固定结束日附近信号 | — | 中 | Peduzzi 1996 / van Smeden 2016 是特定模型下的经验启发式；Alpha158 滚动特征高度相关、同事件簇重叠，**有效样本远小于正事件数**。须同时报告每折有效事件数、事件簇数、重叠率、特征相关矩阵/有效秩、缺失率、标签基准率；用折内嵌套选择/稳定性选择/正则化；最终维数由时间外表现、CI 与敏感性分析决定；**禁止用全样本 N 先定维数再做 CV** |
| as-of 对齐 | polars `join_asof` / DuckDB ASOF；等号语义单测；自写截断重算 + NaN 注入两类前视测试 | 85 | 高 | 无成熟 PIT 校验包 |
| 预注册/锁箱 | git + JSONL + sha256 manifest + OpenTimestamps 0.7.2 | 82 | 高 | DVC/MLflow 只记账；W&B 云依赖；Guild AI 停更 |
| 主 CV | **未来交易表现的主评估 = expanding / rolling walk-forward**（只训练测试起点前可用且标签已完成的样本；一般 purged K-fold 允许测试块之后的样本进入训练，去重叠不等于只用部署时已发生的数据）；自写 AFML `PurgedKFold(t1)` 只回答另一种稳定性问题，不得直接叫上线后预测表现；embargo 放测试块前还是后由信息区间决定；overlap 谓词闭区间为 `max(start) ≤ min(end)`、半开改 `<`；按 episode/复制信号隔离防重复，留交易员/频道验证新来源泛化，两者不合并；五频道整频道隔离只剩极少组、全时域重叠可能连成一大簇，须报告可用训练集而非强行折分；插补/归一化/筛特征/超参只在内层训练完成，空折、单类折拒绝评估。**PurgedKFold 验收**：先写枚举所有区间交集的 reference implementation，再对随机区间、相同时间戳、不同资产、变长标签、空折、边界点做性质测试；明确 `[t0,t1]` 闭开语义、embargo 按时间还是按样本、缺失 t1 处理；每个 split 输出 train/test 索引、purge 原因、embargo 时间范围；**加 cluster-level split**（按交易员/信号源/仓位 episode）防跨折泄漏；purgedcv 0.1.6 / skfolio 旁证；`TimeSeriesSplit(gap=)` 只作定长标签基线；N<300 不用 CPCV | 74 | 中 | purgedcv 事件级 purge 语义正确但无第三方对拍；skfolio 按行数 purge |
| bootstrap 与推断协议 | arch 8.0 + 自写簇抽样；**先明确估计对象**（固定策略的事件平均收益 vs 资金曲线统计）；同日跨币、跨频道信号共享市场冲击，只按 episode 抽样低估不确定性，只按频道又只有五簇 → 跨资产用共同日历块保留块内关联，报告簇数、块长、块长敏感性；比较两策略用配对重采样；若要估整个选模流程的误差，每次重采样重跑选择，不只抽最终赢家；LLM 每个假设/窗口/成本/筛选尝试记账，预留一次最终时序测试，外层结果反馈给 LLM 后不再是外层；不把不等间隔交易收益按固定 bar 频率年化 Sharpe | 75 | 高 | [arch docs, https://bashtage.github.io/arch/bootstrap/timeseries-bootstraps.html]、[DSR, https://ssrn.com/abstract=2460551] |
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
| 5 | 冻结 order_plan → 规范执行事件 schema 与 estimand（事件级收益 vs 资金曲线）；不变量测试集；≥10 个手工最小 episode 期望（含跳空例、同分钟两价格流异步例、mark/last 分离、funding 结算边界）；**候选 A（自研参考实现 v0）与候选 B（Nautilus SimulationModule 扩展）各跑同一集合**，记录代码量、可验证性、性能、维护面；Binance filter 逐条合法/非法例；20 个纯机器人 episode 对账 | 不变量与最小 episode 全过；对账拆确定性状态对账（数量 ≤1 步进、价格 ≤1 tick）与执行误差分布，不可解释误差率 ≤ 预设上限；A/B 比较表出具后定型 | 两候选都不过 → 先修，不进 M3-R | 8 天 |
| 6 | 先冻结 estimand 与分母表（候选/入选/未成交/到期/盈亏/删帖/删失）；Alpha158 变体复现（逐算子契约）+ 8 算子自写 + 两类前视测试；预先指定 ≤10 个可解释特征 + 固定规则基线；数出各分母、事件簇数、重叠率、特征有效秩 | 截断重算一致；分母表与簇数报告齐；是否训练由折内样本预算决定 | 分母表出不来 → 不进建模 | 3 天 |
| 7 | walk-forward 主评估框架（expanding/rolling，只用测试起点前可用样本）+ `PurgedKFold(t1)` 参考实现（枚举区间交集）与性质测试 + episode/频道两种隔离 + 共同日历块簇 bootstrap + 尝试记账与最终时序测试预留 | 性质测试全过；每 split 输出 train/test 索引、purge 原因、embargo 范围；可用训练集规模报告 | 可用训练集不足 → 只出描述 | 2 天 |

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
