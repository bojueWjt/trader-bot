# 从频道执行计划到量化策略：Codex 独立方案

> 方案基准日：2026-09-06；文件名按任务指定保留。
> 类型：架构与研究路线提案，不是实现、部署或交易授权。
> 证据基线：本地 HEAD `dfc8d07e525cb65a384ec9c6492c63825194b6e8` 加阅读时工作树。
> 工作树已有未提交代码及未跟踪 eval、事故文档；下文引用工作树，不冒充已发布版本。
> 本次只读仓库，没有查询生产数据库、连接账户、调用模型 bench 或执行任何交易命令。
> 所有新增类型、研究资产和门槛均为建议；未标为现有的对象，不代表仓库已有实现。

## 1. 独立判断：先证明能复现，再讨论能赚钱

我建议先做“频道计划的确定性执行研究”，而不是直接做“模仿交易员的自主交易机器人”。
频道消息提供的是经过交易员筛选的机会及操作指令，并不完整暴露他为什么没有在其他时刻交易。
因此，复刻执行计划可以检验入场和管理规则；仅靠这些计划，不能识别完整的自主择时策略。
第一项产品应是可审计的计划账本、逐事件模拟器和只读 shadow，而不是新的生产批准器。
首个候选限定为“已明确获准交易的结构化 zone 计划”；优先用舒琴模板做工程种子，不因频道名预设收益优势。
Titan/Gauls 的更新链用于检验管理语义；坚果TV 用于暴露上下文依赖；TraderCash 先作为不交易负例。
最初的成功可以是证明现有 ladder 没有稳定优势，然后不发布；不能把“必须做出盈利算法”设为验收。

已有先例支持研究方法，不支持照搬结论：49 条带价记录最终只有 17 条新鲜 zone 样本。
原文承认 BTC 集中、同日相关和 corpus 回放污染；55/30/15 是参与优先取舍，不是收益最优解。
证据：`docs/plans/zone-ladder-order-plan-v1.md:22`、`docs/plans/zone-ladder-order-plan-v1.md:103`。

本方案只要求三个模块边界：证据资产、无副作用策略内核、独立授权与执行适配。
不要建设通用量化平台、策略市场或复杂多 Agent 投票；这些都不能补回缺失的时间与归属证据。

## 2. 不可改变的安全边界

RESUME、开闸及扩大交易范围必须由用户明确授权；研究结论、恢复健康和 shadow 达标都不是授权。
任何自动化只管理能证明机器人所有权且属于目标计划的订单，不撤、不改、不针对用户手动单报警骚扰。
机器人订单格式为 `^B[0-9a-f]{32}[0-9]{2}$`；格式匹配只是入口条件，还要匹配账户、计划和在场状态。
依据：`AGENTS.md:7`、`AGENTS.md:8`；部署门禁先于停节点，完成报告必须复核审计与心跳，见 `AGENTS.md:9`。
禁止 watcher → parser/importer → approved；数据解析、语义认可、风险批准必须保持不同责任主体。
被禁 importer 的语料可以复用，`--approve-parsed`、`--refresh-window` 回放机制不能复用。
依据：`docs/replay/CORPUS_ASSESSMENT.md:31`。
当前 Hermes 是唯一决策者、节点是订单执行出口，见 `hermes-profile/skills/trading/v3-trader/SKILL.md:10`。
近期上线仍由 Hermes 独立判断是否采纳算法建议，算法不持有 operator 写凭据，也不能自动确认自己的候选。
完全自主策略需要另行修改并批准决策者契约；本方案不以“量化”名义默认获得例外。
事故文档顶部记载曾讨论特定自动 RESUME 授权，正文又有否决意见；本任务不继承这段历史转述。
冲突依据：`docs/plans/2026-09-05-missed-signals-remediation-plan.md:5`、`docs/plans/2026-09-05-missed-signals-remediation-plan.md:209`。

## 3. 仓库现状：有数据骨架，没有可直接信任的研究闭环

### 3.1 数据链成立，但不是“一条 raw 对应一笔完整交易”

基础链是 raw_messages → hermes_decisions → risk_decisions → trade_intents → execution_events → trade_outcomes。
raw 与 decision、decision 与账户风险批准均可能一对多；开仓、移损、减仓、平仓又是不同 intent。
必须保留关系边和基数，不能一次宽表 JOIN 后直接 count 行数当信号数或成交数。
定义依据：`db/migrations/0001_canonical_schema.up.sql:78`、`db/migrations/0001_canonical_schema.up.sql:165`。
批准链约束见 `db/migrations/0001_canonical_schema.up.sql:210`、`db/migrations/0001_canonical_schema.up.sql:231`。
事件与结果见 `db/migrations/0001_canonical_schema.up.sql:286`、`db/migrations/0006_trade_outcomes.up.sql:1`。

还存在 Hermes operator 通路：`operator_order()` 自行生成 source=operator 的 raw 和配套决策链。
其 raw 的 source_received_at 使用提交时 now，而不是原 Telegram 发布时间。
因此，从某个已成交 intent 沿 FK 找到 raw，可能只得到一条操作摘要，而非交易员原文。
必须再用 authorization.source_message_id、频道及规范 ref 建立原消息关系，无法证明时标记断链。
证据：`services/control-plane/api/read_api.py:8373`、`services/control-plane/api/read_api.py:8406`。
model_provider=hermes 也不自动等于“保存了实际 LLM 推理”：这里 model_version 来自 created_by_service。
研究必须另存实际模型、prompt、上下文和工具查询快照；缺失的历史只标“不可精确复现”。

### 3.2 时间字段名不能作为时间语义的保证

raw 的 source_received_at 是 insert-only，但仅凭该约束不能证明它等于 Telegram message.date。
证据：`db/migrations/0001_canonical_schema.up.sql:94`。
Python collector 会读取 date、edit_date、reply_to；feeder 的 canonical payload 却固定 source_version=v1、reply_to=None。
feeder 从 received_at 填 source_received_at，故不同路径的“source”时钟必须逐条辨认。
证据：`services/telegram-watcher/telegram_watcher/collector.py:64`、`scripts/hermes_signal_feeder.py:707`。
漏单事故明确记载补发旧消息被 ingest=now 伪装成新信号的风险，不能靠回测时重置时间解决。
证据：`docs/plans/2026-09-05-missed-signals-remediation-plan.md:45`。

### 3.3 ladder 不是一套已经贯通的实现

纯函数 expand_zone_to_plan(ZoneSignal, MarketContext, RiskContext) 返回 plan_version=1.1、mode、tranches、invalidation。
它实现 0.35% 窗口、窄区间、stale、按风险配量和 48h 上限，见 `services/control-plane/decision_gateway/zone_ladder.py:67`。
read_api 的 _execution_order_plan() 则调用另一套 _zone_ladder_order_plan()，返回 type=zone_ladder、side=buy/sell。
该实现只在现价还未进入区间时展开；输入不适用或异常回退 single_plan，并非纯函数的完整分支。
证据：`services/control-plane/api/read_api.py:633`、`services/control-plane/api/read_api.py:701`。
纯函数使用 risk_budget_usd，API 版本用 max_notional/near_edge 反推风险；同权重不代表同预算。
证据：`services/control-plane/decision_gateway/zone_ladder.py:235`、`services/control-plane/api/read_api.py:747`。
纯函数 RiskContext 有 max_leverage，但 _size_once() 并不读取它；杠杆约束需在上层及节点实际核验。
现有 schema 的 order_plan.type 枚举没有 zone_ladder，却另有 mode=zone_ladder；不能假设两种 wire 等价。
证据：`packages/contracts/v1/approved_trade_intent.v1.json:70`、`packages/contracts/v1/approved_trade_intent.v1.json:130`。
节点持久化恢复路径按 type=zone_ladder 识别分档，见 `services/nautilus-node/strategy/intent_execution_strategy.py:10230`。
判断：先定义“现有真实执行基线”与“目标 v1.1 基线”两套可复现版本，不能把目标模拟收益称为实盘复现。

### 3.4 bench 是语义烟测，不是安全或收益认证

dataset 有 30 条，逐条计数为舒琴13、Titan6、Gauls3、坚果7、Cash1；覆盖严重不均衡。
入口：`eval/v3_trader_signal_bench/dataset.json:3`；构造器说明是无实盘持仓的孤立消息判断，见 `eval/v3_trader_signal_bench/build_dataset.py:19`。
run_bench 使用内置 SYSTEM，只传正文和时间，不加载完整生产 SKILL，也不传图片或历史仓位。
证据：`eval/v3_trader_signal_bench/run_bench.py:19`、`eval/v3_trader_signal_bench/run_bench.py:263`。
strict_ok 不要求 sl_ok、offset_ok，zone 只检查 entry_type；没有校验 TP 梯度与逐档绝对数量。
数字比较允许约 1.2% 误差并容忍万倍换算；这不适合作为可直接下单的价格精度验收。
证据：`eval/v3_trader_signal_bench/run_bench.py:185`、`eval/v3_trader_signal_bench/run_bench.py:240`。
S28 gold 接受冲突数量级的 SL，不能覆盖当前“图文冲突须像素核验”的安全纪律。
证据：`eval/v3_trader_signal_bench/dataset.json:708`、`hermes-profile/skills/trading/v3-trader/SKILL.md:20`。
analyze.py 还对特定条目放宽答案；保留作诊断，但不能用 relaxed 分数证明上线合格。
证据：`eval/v3_trader_signal_bench/analyze.py:13`。

## 4. 路径一：种子数据资产化

### 4.1 现有表、必取字段和用途

| 资产 | 必取字段 | 用途与不能证明的事 |
|---|---|---|
| raw_messages | id/source/channel_id/source_message_id/source_version/content_hash/message_text/raw_payload/source_received_at/ingested_at | 原始证据；时间语义、真实来源需核对 |
| media_assets | asset_id/raw_message_id/sha256/object_key/mime/download_status/downloaded_at | 原图可取与完整性；不能凭路径存在就算读图成功 |
| message_processing_runs | processing_run_id/raw_message_id/status/model_version/prompt_version/context_version/started_at/finished_at/error | 重试、失败、延迟；失败也是样本 |
| context_snapshots | context_snapshot_id/raw_message_id/context_version/snapshot/created_at | 决策当时可见仓位、市场及消息上下文 |
| hermes_decisions | decision_id/raw_message_id/processing_run_id/context_snapshot_id/message_type/action/ambiguous/ambiguity_reasons | Hermes 如何解释，不等于人工真值 |
| hermes_decisions | instrument_symbol/side/entry_type/entry_price/entry_price_min/entry_price_max/stop_loss/take_profits/leverage/valid_until/evidence | 交易语义、价格来源和有效期 |
| risk_decisions | risk_decision_id/hermes_decision_id/account_id/status/risk_budget/checks/reason/decided_by/decided_at | 批准、拒绝、待复核的原因，不只取 approved |
| trade_intents | intent_id/account_id/instrument_id/action/status/order_plan/risk_budget/target_position_id/valid_until/idempotency_key | 提交与最终计划，保留编译前后差异 |
| execution_commands/outbox_events | intent_id/status/payload/created_at/dispatched_at；outbox 的 aggregate_id/event_type/status | 入队、投递、重试；两表字段不要混用 |
| execution_events | event_id/account_id/intent_id/client_order_id/venue_order_id/trade_id/event_type/ts_event/ts_ingest/payload | 实际执行序列与交易所成交去重依据 |
| trade_outcomes | intent_id/account_id/entry_avg_price/exit_avg_price/filled_quantity/realized_pnl/fees/initial_risk/r_multiple/mae/mfe/details | 已平仓机器人 episode 的物化结果，不是所有信号标签 |
| audit_events/node_heartbeats | event_type/intent_id/payload/created_at；node_id/status/last_seen_at | HALT、拒因、不可用窗口；心跳现值不足以重建历史 |

表与字段定义：`db/migrations/0001_canonical_schema.up.sql:117`、`db/migrations/0001_canonical_schema.up.sql:135`。
上下文及执行投递：`db/migrations/0001_canonical_schema.up.sql:154`、`db/migrations/0001_canonical_schema.up.sql:269`。
审计及 outbox：`db/migrations/0001_canonical_schema.up.sql:399`、`db/migrations/0001_canonical_schema.up.sql:413`、`db/migrations/0001_canonical_schema.up.sql:432`。
结果定义：`db/migrations/0006_trade_outcomes.up.sql:1`。

### 4.2 建“计划事件账本”，先做不可变导出，不先迁移生产表

以下是拟新增研究对象名称，不是现有数据库表：SeedMessage、PlanEvent、PlanEpisode、EvidenceManifest、ShadowCandidate。
第一版以研究目录中的版本化 JSONL 与 manifest 交付；后续需要数据库再单独评审迁移及权限。
SeedMessage 主键沿用 source/channel/message/version；额外记录原始发布日期、首次看到、首次入库、图片可用时间。
PlanEpisode 使用独立 plan_id；记录 source_root、频道、标的、方向、开仓事件、管理事件和终止原因。
PlanEvent 记录 event_kind、known_at、effective_at、父计划、原始字段、规范化字段、证据片段及版本。
同一条消息允许多个 PlanEvent，例如“新开多，同时旧空移保本”；不能像 S03 只评分一个主动作。
S03 依据：`eval/v3_trader_signal_bench/dataset.json:99`。
事件种类建议包含 create/amend/cancel/partial/move_stop/replace_tps/close/observe/expire/needs_review。
这些是研究事件枚举，不直接充当 approved_trade_action_v1，也不代表新增生产动作已获准。
计划归属边记录 entry_ref、entry_intent_id、management_intent_id、关联方法、可信等级和复核人。
原始 ref、变更操作 ref 与实验 candidate_id 分开；重试不换 ref，实验版本不能制造新的真实开仓身份。
不同频道相似价区只进入同一统计相关簇，不自动合并为同一个交易计划。
这尊重独立来源不得仅因同币种同价区被跳过的规则，见 `hermes-profile/skills/trading/v3-trader/SKILL.md:18`。
无法关联的管理消息保留为 orphan，不推断成开仓、不猜目标仓位。

EvidenceManifest 至少存抽取 SQL/脚本版本、数据截止、水位、原图哈希、K线哈希、规则版本、路由版本。
额外保存 source_clock_kind、ingress_path、origin_kind=live/replay/backfill/operator_synthetic，以及缺失原因。
历史回填永不改成实时消息；同源 replay、编辑版和重试版必须可识别、可分组。
源数据保留只读，标签修正追加版本；不覆盖原消息，也不重置 source_received_at。
生产导出需另行获准，仅用最小只读权限，排除 token、账户密钥和无关个人聊天内容。

### 4.3 标注分三层，不能让 Hermes 自己给自己打分

层 A：消息意图真值——新计划、管理、复盘、观察、噪音、歧义，以及多动作切分。
层 B：规范化真值——品种、方向、区间、精确/模糊措辞、SL/TP、百分比语义、时间和目标计划。
层 C：可执行性真值——在指定历史账户快照及版本规则下，能否执行、拒绝原因、应产生哪些保护动作。
每个数值标记 explicit_text/explicit_image/user_policy/derived/unknown；不能把系统默认说成交易员偏好。
每个管理动作都标数量基准：初始仓、当前剩余仓、利润比例或不确定。
历史“已止盈20%”与即时“现在减仓20%”分开；文字时态、所指仓位和上下文缺一就留歧义。
图片 OCR 只能出草稿，数量级冲突须人工对原图多个标签复核；后来的行情不是校正证据。
高风险样本双人独立标注并仲裁；预算不足时由一位人工分两轮盲审，明确不等价于双人独立。
没有人审签名就标 provisional；既有 GOLDS 硬编码不是人工签字证据。
人工复核原则已有要求，见 `docs/replay/CORPUS_ASSESSMENT.md:53`。
保留 label_policy_version：按当时规则判定的历史标签，与按当前 SKILL 判定的反事实标签不能混用。

### 4.4 种子抽样与缺口清单

先导出指定连续时间窗全部关注频道消息，包括失败、无图、未批准、未成交和断流补发，不按盈亏挑选。
30 条 bench 保留原版本作回归；另建覆盖当前规则的后继版本，不能悄悄改原 gold 提高分数。
80 条 corpus 文档只证明当时存在这批素材；本次没有验证其原图目前可读、HYPE 事故已补齐。
证据：`docs/replay/CORPUS_ASSESSMENT.md:10`、`docs/replay/CORPUS_ASSESSMENT.md:27`。
最低先收 300 条连续真实消息做标注试点，按频道、消息类型、是否有图和链路状态报告覆盖。
其中至少 100 条新计划及管理事件、50 条观察/复盘/噪音；类别可重叠，但统计必须说明。
样本不够就扩大时间窗；不能复制模板、重复转发或拿合成样本凑真实数量。
优先补发布时间、编辑历史、reply_to、原图、频道路由历史、工具快照和未成交理由七项缺口。
删帖前内容、断流期间未采到的帖子不能凭空恢复；用不可观测区间标记，而不是假设那里没有信号。

## 5. 路径二：交易员行为特征抽取

### 5.1 先抽结构，再估分布，最后才提出策略假设

| 维度 | 记录或计算 | 需要防止的误解 |
|---|---|---|
| 入场结构 | market/limit/zone、区间宽度/价格、宽度/过去ATR、相对现价距离、腿数、条件触发 | 给了两个价格不一定允许成交后加仓 |
| 止损逻辑 | 明示价、突破/跌破百分比、结构失效位、触价/收盘确认、止损距离/过去ATR | “失效位”不自动等于可执行 SL |
| 止盈梯度 | TP数量、各档距离/初始风险、分配比例、保本时机、未给比例状态 | 缺省均分属于系统规则，不是频道统计事实 |
| 仓位管理 | 当前余量减仓比例、移损方向、是否减仓后重整保护、追加条件 | 不把“利润10%”与“减仓10%”无条件等同 |
| 时效 | 发帖到接收/决策/下单、等待入场时长、更新间隔、失效原因 | 30分钟开仓准入不是48小时挂单存活期 |
| 上下文 | 原图依赖、跨帖引用、同币多空、多频道混仓、源内重复 | 当前仓位不是历史仓位 |
| 市场背景 | 截止决策时已闭合K线的波动、趋势、成交量、时段、可交易品种版本 | 未来极值、未来资金费率不能当输入 |

每个特征保留 available_at 与计算版本；找不到当时可见的输入，就不能放入自主策略特征集。
频道“信心度中”“仓位10%”“10倍”先作为声明保存，不直接映射成资金风险或机器学习权重。
自动定量是系统风险配置决定，见 `hermes-profile/skills/trading/v3-trader/SKILL.md:15`。
百分比管理与 TP 剩余量基准已有明确语义，见 `hermes-profile/skills/trading/v3-trader/SKILL.md:23`。
模糊词 0.3% 与 entry-offset 0.1% 是用户策略约定，不是从频道样本学出的最优参数。
依据：`hermes-profile/skills/trading/v3-trader/SKILL.md:27`。

### 5.2 频道画像只能陈述已观察样式

舒琴：S01/S02/S12 提供区间、方向、SL 和多 TP，适合先做结构化编译与风险归一实验。
证据：`eval/v3_trader_signal_bench/dataset.json:33`、`eval/v3_trader_signal_bench/dataset.json:296`。
Titan：样本有多腿计划及更新，S16 涉及多个标的和减仓；应优先建立计划上下文，不先拟合入场价格。
证据：`eval/v3_trader_signal_bench/dataset.json:399`；第二腿 β 纪律见 `hermes-profile/skills/trading/v3-trader/SKILL.md:34`。
Gauls：S26 平仓、S27 移损、S28 入场量级冲突，是“管理归属+多模态核验”的回归种子。
证据：`eval/v3_trader_signal_bench/dataset.json:670`、`eval/v3_trader_signal_bench/dataset.json:687`、`eval/v3_trader_signal_bench/dataset.json:708`。
坚果TV：S03 同帖包含新旧多空计划，S09 是口语市价意图，S10 是价格提醒；先学区分，不学“看到数字就下单”。
证据：`eval/v3_trader_signal_bench/dataset.json:99`、`eval/v3_trader_signal_bench/dataset.json:231`。
TraderCash：现有 bench 仅 S30，且是更新帖；不足以推断其可执行新开仓风格。
Cash“关注区域+失效位+潜在反弹”必须零交易；有价格也不能拼成订单。
证据：`eval/v3_trader_signal_bench/dataset.json:765`、`hermes-profile/skills/trading/v3-trader/SKILL.md:30`。
第一版频道差异仅体现在语义模板、上下文要求、数据质量和响应延迟，不做“谁更强”的收益排行。

## 6. 路径三：算法抽象层级与最小内核

### 6.1 L1：复现频道的已授权计划，不重建其全部交易思想

输入是人工复核或 Hermes 确认的 PlanEvent、历史市场快照、目标计划状态及版本化风险配置。
输出是 candidate/hold/needs_review，以及编译所需的绝对参数；输出不是 approved intent。
首个策略族只有结构化 zone；冻结当前默认参数，分别复现 API 实际分支与纯函数目标分支。
管理状态机至少区分待入场、部分成交、持仓、部分退出、已平、失效和归属待核验。
“第二腿 open”与“同一个已批准 ladder 内多个 tranche”是不同概念，不能借后者绕过 β。
过期管理消息仍需核验当前状态；它不是永久授权，已平仓或归属不明时只能 no-op/needs_review。
边界依据：`hermes-profile/skills/trading/v3-trader/SKILL.md:31`、`hermes-profile/skills/trading/v3-trader/SKILL.md:33`。
该层通过的是行为复现，不要求盈利；即使不赚钱也能减少语义漂移和执行不可解释性。

### 6.2 L2：跨频道共性因子，但仍以信号为机会入口

只提出三组预注册假设：风险归一后的区间深度、距离/波动归一的时效、管理更新的增量价值。
例一：同等初始风险与成本下，深浅档对每个种子期望收益、未成交率和尾部损失的影响。
例二：决策延迟占预期计划寿命的比例，是否比固定延迟更能解释失效；不因此放宽30分钟铁律。
例三：原始固定 SL/TP 对比“按到达时间执行后续管理消息”，管理究竟增加还是损害净收益。
消融顺序固定：不含频道ID → 加频道偏置 → 留一频道验证；只在见过的频道有效就不叫共性因子。
参与度、收益和风险是三个指标；不得以成交率提高替代收益或尾部改善。
不同源同时看多同一币仅是相关暴露，不把投票数直接当独立证据数量。
参数候选总量预先封顶，完整记录试验，包括被淘汰者；不先跑上千组合再挑最好的一条。

### 6.3 L3：脱离信号独立运行，是另一个识别问题

先把频道提出的“支撑反弹/阻力回落”当假设来源，定义仅由历史行情形成的候选区域与触发器。
例如研究版本可用过去已闭合窗口的高低点构造区域；若用摆动点，必须等右侧确认完成后才可见。
策略的开仓、失效、SL、TP、定量和退出都必须由行情与自身状态决定，不再等待频道更新。
训练和评估机会集必须覆盖所有合格市场时刻，包括频道没发帖的时段及没有交易的区域。
同样需要当时可交易的品种集合，不能只留下今天仍有数据、后来表现好的标的。
切断 Telegram 输入后仍能逐事件重现相同输出，才算技术上脱离信号；不是删掉特征中的 channel_id 就算。
再比较“信号筛选的机会”与“自主触发的全部机会”；二者绩效不能直接外推。
本期 L3 只交付可证伪假设与离线研究契约；没有新鲜跨时段证据就明确终止，不为路线完整强行上线。

### 6.4 建议内核接口，不新增复杂框架

拟新增研究接口：evaluate_plan(PlanEvent, PlanState, AsOfMarket, PolicyVersion) → CandidateDecision。
拟新增编译接口：compile_candidate(CandidateDecision, RiskSnapshot, InstrumentRules) → ExecutionPlan 或明确拒因。
内核必须无网络、无系统当前时间、无数据库写入；时间与数据由调用者显式注入。
研究者可以替换数据和执行模拟器，不能替换生产授权检查；职责不能藏在同一个 evaluate 里。
优先复用纯函数算术，但先验证全部语义，不把 import 成功当成与现网一致。
每个输出记录 input_hash、policy_version、parameter_hash、compiler_version、decision_time、reason_codes。
预算例：long 区间[100,102]、SL=98、风险10，三档价格为102/101/100.3。
风险权重55/30/15对应基础数量为5.5/4、3/3、1.5/2.3；这不是名义金额权重。
名义金额是 quantity×price，随后统一封顶并按步进向下取整；手续费、跳空和滑点需另留预算。
原方案把 risk/distance 称为“每档名义”，量纲实际是数量；实现 raw_qty 的算式才是参考。
证据：`docs/plans/zone-ladder-order-plan-v1.md:60`、`services/control-plane/decision_gateway/zone_ladder.py:248`。

## 7. 路径四：回测、逐信号复现与评估

### 7.1 先把两个现有统计工具降到正确职责

zone_penetration_stats 的 load_zone_signals 取带区间的 decision，并未筛选 live/new_signal/去重计划。
必须由新的只读导出层建立干净 cohort，不能把原 SQL 输出直接当交易机会总集。
证据：`scripts/analysis/zone_penetration_stats.py:281`。
classify_retrace 从首次触区一直看窗口尾极值；fill_at_depth 在整个窗口找触价，不截断 TP1/SL/失效。
同一分钟同时触区与 TP 时，当前分类也不能提供盘中真实先后；“触价率”不是可执行成交率。
证据：`scripts/analysis/zone_penetration_stats.py:119`、`scripts/analysis/zone_penetration_stats.py:167`。
aggregate 在无历史 ATR 输入时回退到 all_klines，可能包含信号后行情；此口径只能诊断，不能进策略输入。
证据：`scripts/analysis/zone_penetration_stats.py:421`。
默认研究窗口24小时，纯函数 TTL 上限48小时；复现必须显式对齐，不能用不同窗口宣称效果改善。
证据：`scripts/analysis/zone_penetration_stats.py:19`、`services/control-plane/decision_gateway/zone_ladder.py:383`。

trade_outcomes 按账户+标的净数量归零切 episode，再归因给第一笔带标签的入场 intent。
多频道共同持仓、同标的对冲簿、跨零成交都必须专项核验；当前分组键并没有 side/position_id。
证据：`scripts/analysis/trade_outcomes.py:230`、`scripts/analysis/trade_outcomes.py:290`。
它只从 OrderFilled/PositionClosed 拉取，不能未经确认就认定部分成交事件已完整计入。
证据：`scripts/analysis/trade_outcomes.py:138`。
PositionClosed 在末次成交附近五分钟匹配；无明确归属的事件可能不适合频道级 PnL，需隔离核算。
证据：`scripts/analysis/trade_outcomes.py:287`、`scripts/analysis/trade_outcomes.py:316`。
fees 单列，r_multiple 用 realized_pnl/initial_risk；没有统一证明已扣全手续费与资金费率。
证据：`scripts/analysis/trade_outcomes.py:711`、`scripts/analysis/trade_outcomes.py:726`。
仅已平仓进入结果；没成交、拒绝、尚未平仓不能从评估分母消失，末端持仓要标记浮盈亏与右删失。
不要在本任务跑该脚本“顺便统计”：main 会 upsert 并删除不再匹配的结果，不是只读分析命令。
证据：`scripts/analysis/trade_outcomes.py:527`、`scripts/analysis/trade_outcomes.py:442`。

### 7.2 四组基线，分清算法收益与链路损失

H-live：历史 Hermes 真正提交、获批和执行的结果，含 HALT、冻结、断流、超时及费用缺失标记。
H-replay：冻结历史可得上下文与规则重跑 Hermes；模型不可复得时只能称近似重放，不替代 H-live。
Q-matched：与 Hermes 使用相同机会、同一可见时间、风险预算及模拟器，只改变机械执行策略。
Q-full：对完整消息机会集或 L3 完整市场机会集运行，记录所有跳过、等待、拒绝和未成交。
额外设 no-trade 与冻结的简单执行规则作对照，避免只证明“比另一个复杂规则好”。
收益分解分别展示信号机会、语义选择、风险拒绝、执行损耗、可用性损失，不把修复断流收益算作 alpha。
事故已有四类独立漏单原因，依据 `docs/plans/2026-09-05-missed-signals-remediation-plan.md:21`。
H-live 与 Q-matched 不能用最终成交均价初始化策略；策略初值必须来自当时可见行情。
账户原有持仓仅用于入口资源约束；策略运行后管理各自虚拟仓，不借用未来真实仓位修正虚拟状态。

### 7.3 逐信号、逐管理事件的可重放流程

1. 按原始证据重建计划版本树及首次可见时刻；来源发布时间不明的样本不得进入可交易 cohort。
2. 定义 decision_time=max(消息可见、必要图片可用、上下文可用)+该决策器测得的处理延迟。
3. 只加载 decision_time 前可得的历史数据；按发布至决策的30分钟准入及 signal valid_until 检查。
4. 编译并验证 order_plan，保存全部输入、风险封顶、取整和拒绝原因，不在模拟中偷偷修正参数。
5. 订单进入有延迟的执行队列；模拟挂单、IOC未成交、部分成交、保护单生效、撤单和重试。
6. 后续管理消息在其 known_at 才触发；按本计划当前剩余数量减仓，并重新计算保护单数量。
7. 到终止或观察窗尾输出交易明细、现金流、风险占用、未成交原因、右删失状态及轨迹哈希。
8. 对纯机器人且能唯一归属的真实 episode，与 trade_outcomes 对账；混合 episode 单列不强拆频道收益。

语料回放与统计模拟是两类验证：前者必须走 ingress→真 Hermes→Gateway→隔离 Nautilus→投影。
后者可直接调用无副作用内核，但只能写研究结果，不能写正式批准链；不能把后者冒充端到端验收。
既有回放约束见 `docs/replay/CORPUS_ASSESSMENT.md:43`。

### 7.4 K线与执行模型的最低可信程度

复用 Binance 1m 缓存读取，但当前 Kline 只有时间和OHLC，没有盘口、排队量、成交量或资金费率。
证据：`scripts/analysis/zone_penetration_stats.py:15`、`scripts/analysis/zone_penetration_stats.py:676`。
每批数据校验缺口、重复、时间单位、品种映射、来源版本与哈希；XAU 的脚本拒绝不代表现实永远不可交易。
它只证明当前工具硬编码不支持该归一化符号，见 `scripts/analysis/zone_penetration_stats.py:22`。
股票/商品类合约另设数据覆盖矩阵；缺 UM 历史不能拿现货或股票日线悄悄替代。
分钟中途发来的消息，不能使用该分钟完整 high/low 判入场；缺细粒度数据时从下一完整分钟开始并声明近似。
已闭合小时/15m 数据才可用于特征与失效确认；不能把未收盘聚合K线当已知最终形态。
同 bar 入场、TP、SL、击穿并发时分别给保守下界与乐观上界；结论跨零就不通过收益门槛。
要消除歧义需逐笔成交或更细行情；tick 也不自动解决真实订单排队，仍需成交模型校准。
post-only 可能被拒，触价不保证成交；IOC 必须允许零成交和部分成交，止损跳空不保证按 SL 成交。
执行模型需区分 last/mark 的触发依据、tick/step/min-notional、账户模式、费率与杠杆约束。
无盘口时不宣称 maker 排队优势；采用保守穿价与容量假设，并对成本、延迟加倍做敏感性测试。
净收益按可核验价格现金流、手续费、资金费率分别核算；交易所 PnL 字段口径不清时避免重复扣费。
费用缺失不是0；研究输出净收益区间与缺失标志，成本足以改变符号时禁止上线。
MAE/MFE 按有效持仓窗口截断，并注明进出场分钟边界的不确定性；不能把出场后极值算进持仓风险。

### 7.5 防前视、选择偏差与参数过拟合

所有消息编辑、图片补齐、路线变更、管理更新都按首次可见时间推进，不按最终版本回填过去。
未来最高刺入、是否最终到 TP1、最终持仓时长只能作标签，不能作候选生成或样本筛选特征。
先按时间冻结训练/验证/测试，至少三段 walk-forward；每段参数只能使用此前完成且标签成熟的计划。
同一计划的开仓与管理不可跨训练和测试；同源转发、编辑、回放归为同组。
重叠持仓与行情事件簇做 purge，隔离期按预定义最大标签窗口设置；跨边界未平仓只做删失处理。
按日/行情事件簇做 block bootstrap，报告簇数和有效样本量；同日10条BTC计划不是10次独立试验。
L2 增加留一频道验证；L3 增加无信号时段和未见市场状态验证，不能只在频道精选时刻测试。
只在完整记录的预注册候选集合中比较；一旦看过锁箱结果再改参数，旧锁箱只能降为验证集。
收益指标同时报告每个合格种子的净R（未成交为0）、每个成交计划净R、风险占用及资金时间效率。
风险分母固定为计划初始承诺风险，并另报实际成交风险；不能靠只成交深档缩小分母制造高R。
同时报告最大回撤、尾部损失、保护缺失时长、重复开仓、非法管理、拒绝率、触发到成交延迟。
对多个 TP 与多腿，聚合计划级结果；订单、成交回报、tranche 都不得冒充独立样本。
先用 venue trade_id+账户等稳定键识别重复成交；无稳定键时隔离歧义，不能凭同价同秒盲删真成交。
原 SKILL 已警告回报重复，见 `hermes-profile/skills/trading/v3-trader/SKILL.md:235`。

## 8. 路径五：shadow、接入契约与四账户灰度

### 8.1 shadow 必须在权限上不可能变成交易

ShadowCandidate 使用独立研究存储、独立 consumer 游标、虚拟账户账本和与生产不同的幂等命名空间。
只读取批准过的导出/镜像快照；无交易所 key、operator token、节点写 token、生产 outbox 写权。
禁止把 shadow 塞入 trade_intents 再靠 status=draft 或某个过滤条件避免消费。
因为 gateway 对批准结果会创建 execution job 并写 outbox；安全性不能依赖一个易漏的 shadow 布尔值。
证据：`services/control-plane/decision_gateway/gateway.py:215`、`services/control-plane/decision_gateway/gateway.py:286`。
shadow 输出 candidate_id、source_root、policy_version、as_of、proposed_plan、risk_preview、virtual_fills、diff_reason。
risk_preview 只叫“假设检查结果”，没有 approved_at，也不得发布 trade_intent.approved 事件。
同一可见消息流并行喂 Hermes 和 quant；双方推理彼此不可见，完成后再比较，避免答案互相污染。
若给 Hermes 提供候选作为辅助，必须单独标为 assisted 实验臂，不能与独立 Hermes 基线混合。
shadow 重放允许研究时钟，但禁止改写生产 TTL；重启从研究游标恢复，绝不补发真实订单。

### 8.2 上线接入前先锁定“哪一种 plan”

现有 process_one_decision() 明确拒绝非 Hermes 来源；数据库也把 model_provider 限定为 hermes。
证据：`services/control-plane/decision_gateway/gateway.py:97`、`db/migrations/0001_canonical_schema.up.sql:197`。
近期可用路径：quant 输出建议 → Hermes 核验原文/时效/归属并独立接受或拒绝 → 现有控制面风险检查 → 节点。
不得把 quant JSON 写成 hermes_decisions、伪造 source=operator 或拿管理员 token 充当“兼容适配”。
要让确定性编译器影响真实订单，先单独评审接线；现有 operator API 不能被假设为任意 order_plan 的透传接口。
确认 compiler、schema、_execution_order_plan、节点 planner/恢复器共同接受同一版本，不能只改 schema 放行。
迁移期保留旧计划消费路径；新的不识别字段或模式 fail closed，不 silently fallback 成单档/市价。
尤其 _execution_order_plan 当前 B 格式直通条件是 type+quantity+buy/sell，不是 plan_version 守卫。
证据：`services/control-plane/api/read_api.py:557`；历史方案也指出该风险，见 `docs/plans/zone-ladder-order-plan-v1.md:95`。
编译验收至少覆盖常规、追入、区内stale、穿区拒绝、窄区、short镜像、零数量合并、步进、TP1/击穿/TTL。
每例同时校验语义输入、wire、节点预期订单与恢复轨迹，而不是仅判断返回 dict 非空。
已有单测可作入口，但不能代表本次已执行：`tests/control-plane/decision_gateway/test_zone_ladder.py:1`。
三档生命周期打点是原设计要求；没有逐档真实事件证据前不把它视为已部署能力。
证据：`docs/plans/zone-ladder-order-plan-v1.md:97`。

远期 L3 若申请自主生产：必须新增真实的策略主体、用户签署的策略授权和独立风险批准契约。
授权要限定版本、账户、品种、动作、预算、有效期和撤销机制；策略本身不能签授权或执行 RESUME。
届时需评审 decision 来源模型与 FK 演进、风控入口和审计消费者；不放宽现有 Hermes 来源约束充数。
这一变更未获明确批准之前，L3 只能离线/shadow；“能上线”的含义是明确门禁，不是现在就能写生产表。

### 8.3 四账户分工：先虚拟分臂，再考虑真实路由

| 阶段 | account-a | account-b | account-c | account-d |
|---|---|---|---|---|
| 数据与shadow | 保持当前真实职责，虚拟H/Q配对 | 同左 | 同左 | 同左；不假设空闲 |
| 用户批准首轮canary后 | 现有Hermes真实参考 | 现有Hermes真实参考 | 现有Hermes真实参考 | 条件合格时唯一小风险候选账户 |
| 第二轮条件满足后 | 保留Hermes参考 | 不变 | 用户单独批准后可选第二候选账户 | 固定首个候选版本，不同时试多个参数 |

这是建议分配，不是断言当前频道映射；第一阶段必须导出并审核当前路由与未平计划归属。
若 d 不空闲、混有无法隔离持仓或交易所最小数量不满足风险上限，就不挪仓、不强行借用其他账户。
首轮 a/b/c 不为了凑A/B对照增加订单；同一信号在 shadow 配对即可，真实跨账户结果只作辅助证据。
候选消息按事前固定且用户批准的路由进入 d；原通路对应消息不得同时重复新增风险。
路由改绑只影响新计划，存量管理必须回到原入场账户；按 intent/entry_ref 归属，不按频道今天绑在哪。
依据：`hermes-profile/skills/trading/v3-trader/SKILL.md:32`、`hermes-profile/skills/trading/v3-trader/SKILL.md:182`。
canary 风险预算取用户批准的绝对预算和现有配置上限中更小者，并保留合约最小量检查；不建议照搬约2%默认。
全舰队总承诺风险不得因为多开实验账户而增加；预算预留覆盖所有未成交档及已成交仓，不只看当前持仓。
原 SKILL 的≤12USDT permit 示例属于审核发布 canary，不等于所有策略都能用同样尺寸或直接继承授权。
证据：`hermes-profile/skills/trading/v3-trader/SKILL.md:115`。

### 8.4 防双脑写入与安全退回

同一账户+计划只允许一个真实写入责任者；shadow 永不竞争，Hermes 的管理与批准的机械保护职责明确分开。
管理动作必须带稳定操作 ref 和 entry_ref；多源混仓、用户手动增减影响净仓时停止自动归因并转复核。
只允许按已证明机器人计划数量管理，不能用整账户净仓作为策略可操作数量。
出现归属错配、重复新开仓、裸仓或契约不识别，立即停止候选新增风险，必要时走既有 HALT 安全路径。
已成交部分继续由获准的既有保护路径管理；不能为了回滚删除状态或撤掉保护单，更不能清手动单。
回退不自动向 Hermes 补发被候选处理过的旧信号；先解决所有权与在途幂等，再等待新的合法机会。
任何再次 RESUME 都待用户明示，附本次审计、release 和新鲜心跳；不能拿上一次恢复记录作证据。
所有演练、部署和路由修改都是后续单独授权事项，本次不执行。

## 9. 路径六：再校准与退化检测

每个交易日先出数据质量/链路健康摘要，再出语义偏差，再出执行偏差，最后才看收益偏差。
断流、模型失败、风险拒绝、订单拒绝和无行情机会必须独立计数；“0成交”不是一种统一的故障。
频道无新帖但链路探测正常时不报警；心跳老化、已知新帖未入库、投递积压或探测失败才按证据报警。
巡检需检查 Telegram 告警实际可达，不把库里存在事件当作用户已收到，见 `AGENTS.md:22`。

| 维度 | 监测对象 | 建议动作 |
|---|---|---|
| 数据 | 发布时间缺失、图不可用、重复率、K线缺口、路由版本缺失 | 隔离批次，禁止用它重新拟合 |
| 语义 | 模板未知率、needs_review率、zone丢失、SL/TP冲突、管理归属失败 | 停止该模板新增风险，人工复核样本 |
| 执行 | 候选与wire差异、逐档成交偏差、滑点、撤单终态、保护延迟 | 停候选升级，查契约与节点，不先调参数 |
| 链路 | source→ingest→decision→risk→dispatch→fill延迟、HALT/冻结窗口 | 独立运维处理，保留算法版本 |
| 收益 | 滚动净R、回撤、尾部、按市场状态的偏差 | 按预注册风险界限停新增风险并复审 |

现有“≥50条新鲜zone再校准”保留为触发研究的门槛，不是自动改权重的开关。
证据：`docs/plans/zone-ladder-order-plan-v1.md:101`。
建议同时要求覆盖至少4周并有足够独立事件簇；未满就继续记录，不按日历硬调参。
训练窗只用截至校准时已成熟标签；近窗尚未退出的计划不能选择性剔除亏损或提前标胜。
每轮提交旧/新参数差异、候选总数、三段前推结果、成本压力和保守成交下界。
校准版本从一个明确未来事件边界生效；存量计划固定原入场与退出策略版本，不半途改变风险承诺。
近期模型/提示词变化单独做语义回归，不与 ladder 权重调整捆绑发布，避免无法归因。
安全异常零容忍；普通收益恶化用预注册风险预算及区间判断，不把“连亏三单”当统一统计结论。
监测可以自动安全收缩和发聚合告警，不自动扩权、改风控、迁移账户或 RESUME。

## 10. 最大的三个风险，以及明确砍掉的东西

### 风险一：把被筛选、被修订的叙事误当完整可学习策略

频道只展示部分机会，历史还存在断流、补发、图片缺失和未签字 gold；这会同时污染输入与真值。
后验战绩、未来编辑、Hermes 自己的标签与已成交样本叠加，会让任何算法看起来优于现实。
最先的防线是全消息分母、双时钟、版本树、未交易负例、盲审和完整市场机会集。
砍掉：端到端模仿学习、用频道战绩训练收益预测、仅在发帖时刻验证的“自主策略”。
L3 推迟到独立机会集和锁箱证据成立；否则以“不可识别”结题，接受没有策略可发布。

### 风险二：回测的是设计稿，实盘执行的是另一套语义

两套 ladder、静默单档回退、宽松 bench、净仓 episode 归因，可能令收益和风险数字都无法对齐。
最危险的不是某个参数略差，而是研究测了TP1撤单/追入，线上根本没执行同一条规则。
防线是冻结 H-live 与目标版本、逐字段wire契约、节点回放轨迹、费用/归因核对及盘中路径上下界。
砍掉：大规模参数搜索、深度盘口仿真平台、漂亮频道收益榜；先交付可解释的少数确定性夹具。
推迟：只有逐笔/盘口数据才支撑的maker排队优势、低周期反转确认和精细追单优化。

### 风险三：增加一个“大脑”却共享同一个故障域与写权限

Hermes 和 quant 共用 watcher、数据库、路由和节点，不构成独立容灾；双脑可能把重复单与误管理放大。
四账户也不是四个独立研究样本，更不保证与用户手动仓隔离。
防线是无写权shadow、单计划单写入者、入场归属管理、真实心跳/告警门禁、独立用户开闸。
砍掉：自动RESUME、自动跨频道投票批准、无SL固定小额策略、自动追补所有漏单、成交后第二腿加仓。
无SL小额虽在 SKILL 有现存通道，本研究可以更保守地排除；不修改原通道，也不假装策略覆盖它。

## 11. 分阶段里程碑与可验证判据

以下数量是启动验收的最低工程门槛，不是“统计显著”的替代；不足时延长观察，不降低标准凑上线。
所有后续新文件、代码、数据库、凭据、部署动作均需另开任务授权；本次产出仅是本文。

### M0：证据盘点与基线冻结

产出：只读数据manifest、字段覆盖矩阵、两条入口时钟对照、真实路由/所有权快照、执行版本清单。
判据：随机抽查至少20个已执行计划，逐层追到原消息、批准、订单和退出；失败者全部有明确断链类型。
拟进入研究cohort的样本100%有可证发布时间、来源类型、版本和资产哈希；其他样本留在缺口账本。
区分“存在代码”“本地有测试”“线上已加载”三种状态；没有生产证据就不勾选最后一项。
线上canary前另需真实审计/心跳/告警到达证据，不能以事故文档状态行代替。
失败出口：只交付盘点，不启动策略收益评比。

### M1：计划事件标签与当前规则gold

产出：≥300条连续消息的版本化标注试点、计划关系图、争议仲裁记录、原30条bench兼容报告。
判据：危险样本全部人工签字；观察/复盘误开仓、跨频道误管理、数量级猜测、超时开仓均为0。
每个交易数值可追到正文、原图或明确用户规则；缺少依据的字段必须unknown而非填默认伪真值。
至少30条管理事件完成到入场计划的人工核验；多动作、编辑、纯图和重复样本均有覆盖。
不要求Cash凑新开仓样本；如果没有可执行指令，就维持该频道0交易样本的事实。
失败出口：继续补标，不将 provisional gold 用于模型或收益排名。

### M2：确定性内核与执行一致性

产出：L1规格、计划状态机、编译器契约草案、API实际/目标v1.1差异清单、≥60个边界夹具。
判据：相同输入与版本重复运行轨迹哈希一致；每个合法输出均通过schema和节点语义验证。
逐档价格数量、剩余量保护、TP1/击穿/TTL、重启恢复、β第二腿与非法所有权拒绝都有明确断言。
不能满足现有wire的功能标blocked；不以宽松schema或silent fallback伪装通过。
失败出口：保留可用纯函数的研究用途，禁止canary接线。

### M3：无前视配对回测与参数冻结

产出：H-live/H-replay/Q-matched/Q-full分层报告、每计划轨迹、费用桥接、三段walk-forward和完整实验账本。
判据：至少200个可评估新鲜计划、覆盖≥60个自然日及≥30个独立事件簇；不足仅交工程验证，不批准收益结论。
真实成交对账：唯一可归属样本数量差≤一个交易所步进，价格差≤一个tick；现金流差在可解释费用/舍入内。
晋级候选需保守成交与成本压力下净期望仍为正，配对每种子净R差的95%簇置信区间下界>0。
另需样本外回撤/尾部损失不超事前用户认可上限；区间太宽即证据不足，不用点估计替代。
L2必须通过留一频道检验，否则只能标频道专属；L3还必须评估无信号机会，不能搭便车晋级。
失败出口：冻结默认、报告无优势或不可判断，不继续无预算地搜参数。

### M4：在线只读shadow

产出：至少4周且≥50个新鲜候选的配对轨迹、延迟/拒因/可用性漏斗、隔离与恢复演练记录。
判据：shadow没有生产写凭据；模拟误配置也不能产生真实outbox、订单或管理命令。
候选100%可重放；过期、重复、图片缺失、归属不明均有拒因；按剩余量管理的轨迹全部守恒。
已知频道发帖到处理链路可核对，冻结/断流/心跳失活能被区别观测；不以市场静默判失败。
所有未解释计划差异清零，并在这批新鲜证据上不突破M3预注册风险门槛，才申请用户canary授权。
失败出口：只停shadow或候选升级，不影响现有账户和手动订单。

### M5：单账户小风险canary与有限推广

产出：用户授权记录、路由/风险预算/版本冻结、d账户准入核验、逐笔实盘对照与安全退回记录。
判据：M0至M4通过且用户明确批准；部署所有门禁在停节点前完成，开闸不自动执行。
首轮至少4周、30个完整计划周期，无重复开仓、跨归属操作、保护缺失或未解释现金流差异。
实际滑点、成交率、延迟与M3预注册预测区间一致；超出时先查模型/执行，不立即调参扩大窗口。
30个周期只验工程稳定，不足以重新证明收益；推广还必须保持样本外优势及用户风险上限。
扩到第二账户须重新核验聚合风险、路由和用户授权；自主L3仍需要第8.2节的新决策主体审批。
失败出口：停止候选新增风险、保留归属明确的保护管理，人工核验后决定下一步；永不自动RESUME。

## 12. 需要明确接受的不确定性

第一，我不能从仓库证明生产消息的真实发布时间、图片与管理链能补齐到足以支持无前视回放。
第二，我不能证明现有两套ladder及节点行为在生产采用哪一版本，也不能保证历史PnL可细分到频道计划。
第三，我不能保证交易员公开计划存在可迁移、扣成本后仍成立的共性因子，更不能保证能脱离信号自主择时。
这三项分别由M0/M1、M2/M3、M3/M4提供证据；任何一项不成立，都应缩小范围而不是降低验收门槛。
最终推荐：先立项M0至M2，冻结研究预算；是否进入收益研究和真实canary，由可复核产出逐阶段决定。
