# 频道执行计划 → 量化算法：双方案合并 review

> 日期：2026-09-07 · 汇总：Claude Code
> 输入：[Claude 方案](2026-09-06-channel-seed-to-quant-claude-proposal.md)、[Codex 方案](2026-09-06-channel-seed-to-quant-codex-proposal.md)（Codex 任务在文件落盘后网络流断开报 failed，文档完整 532 行，其"最没把握的三点"在 §12 已写）
> 核验：Codex 引用的关键 file:line 由 Claude 逐条复核（见 §2）；Claude 的生产库数字来自 2026-09-06 只读查询
> 2026-09-08：按 gpt-6-astra 对本文的对抗 review（docs/reviews/2026-09-08-gpt6-review-of-merged-review.md，6 必修 5 应改）完成正文同步：§2 C3、§5 路线表、§6 授权、§8.1 T1、§9/§9.1 终裁、§10 派发闸门。

## 1. 两份方案的共识（不再争论）

1. **自下而上**。先把"结构 + 执行政策"做成确定性、可回测的机械层，再谈"哪些信号值得做"，最后才是"不靠信号自己触发"。两份都把自主策略定为研究项目，不排产品里程碑。
2. **第一个产品是账本和模拟器，不是新的批准器**。Codex 的原话："最初的成功可以是证明现有 ladder 没有稳定优势，然后不发布"。
3. **三个硬缺口一致**：结局数据几乎没有（生产 `trade_outcomes` 仅 10 行）；发布时间不可信；`hermes_decisions` 是 LLM 解析不是真值。
4. **安全边界一致**：RESUME/开闸只能用户明示；不碰手动单；不引入 watcher→parser→approved 旁路；影子层不写 `trade_intents`。
5. **不建通用量化平台**：不引 freqtrade/Nautilus backtest，不做多 Agent 投票，参数搜索空间预先封顶并全程记账。

## 2. Codex 抓到、Claude 漏掉的（已复核，全部采纳）

| # | 发现 | 复核证据 | 对路线的影响 |
|---|---|---|---|
| C1 | **两套 ladder 实现**。线上 `_execution_order_plan` 调的是 `read_api.py:701 _zone_ladder_order_plan`（输出 `type=zone_ladder, side=buy/sell`，仅在现价未入区时展开，异常回退单档）；`decision_gateway/zone_ladder.py:67 expand_zone_to_plan`（v1.1，含追入/stale/窄区/风险配量）**没有任何线上调用方** | `grep expand_zone_to_plan` 全仓仅定义处与测试；`read_api.py:635` 调的是自己那套 | Claude 方案 §2.2"回测器直接复用线上展开函数"**不成立**。必须先冻结"H-live 实际语义"与"v1.1 目标语义"两个版本，回测分别跑 |
| C2 | **线上 wire 不在契约内**。`approved_trade_intent.v1.json` 的 `order_plan.type` 枚举只有 market/limit/zone，线上却发 `type=zone_ladder`；节点按 `type=zone_ladder` 识别（`intent_execution_strategy.py:2771`） | python 加载契约确认枚举为 `['market','limit','zone']` | 任何"改配置灰度"之前要先做执行一致性（Codex M2），否则回测的是设计稿 |
| C3 | **两条入口时钟不同**。Python collector 用 Telegram `message.date`（`collector.py:64`），但生产 feeder 路径固定 `source_version=v1`、`source_received_at=received_at`（watcher 收到时间，`hermes_signal_feeder.py:707-748`）；operator 通路的 raw 用提交时 now | 已读代码 | `published_at` 无法从库里追回。**M0 仅探查可恢复性，不承诺补齐**：先查 watcher sqlite/备份，再用隔离会话对 20 个已执行 + 10 个失败样本做只读查询，按 `A_content_and_time` / `A_time_only` / `B_watcher_archive` / `C_ingest_only` / `D_unavailable` 分级；只有 `A_content_and_time` 可进入严格 M3-R，其余只进敏感性分析或缺口账本（见 §8） |
| C4 | `scripts/analysis/trade_outcomes.py` 的 `main` 会 **upsert 并删除**不再匹配的结果，不是只读分析；episode 分组键无 side/position_id，多频道混仓、对冲簿会串 | `trade_outcomes.py:442/527` | 研究一律走独立只读导出，不"顺手跑一下"该脚本 |
| C5 | `zone_penetration_stats.py` 的 ATR 回退用全窗口 K 线（含信号后），触价分类不截断 TP1/SL | `:421`、`:119`、`:167` | 现有统计只能做诊断，不能直接当策略输入；回测器要重写这两处 |
| C6 | 网关 `process_one_decision` 拒绝非 Hermes 来源，且库约束 `model_provider='hermes'`（`0001_canonical_schema.up.sql:197`） | 已读 | 量化输出**近期只能作为 Hermes 的建议**，由 Hermes 独立决定采不采纳；Claude 方案 §6.3 步骤 2"仓位乘子直接进 risk_budget"需要另立契约评审，不是配置改动 |
| C7 | bench 的 `strict_ok` 不校验 SL/offset/TP 梯度，允许 1.2% 误差和万倍换算；`run_bench` 不加载生产 SKILL、不传图 | `run_bench.py:185/240` | 30 条 gold 只能当语义烟测；扩容时要按 Codex 三层标注（意图/规范化/可执行性）重建 |
| C8 | 影子层若塞进 `trade_intents` 靠 `status=draft` 挡，网关对批准结果会建 execution job 并写 outbox | `gateway.py:215/286` | 影子层用独立存储、独立游标、**零生产凭据**；Claude 方案用 operator API `dry_run` 做风控预览也不行，影子进程不该持有 operator token |

## 3. Claude 有、Codex 没有的（保留）

| # | 内容 | 出处 |
|---|---|---|
| A1 | **生产库实际规模**：836 条原始消息、811 条决策、349 条开仓、306 条带价、`trade_outcomes` 10 行；舒琴占频道开仓 63%；开仓涉及 44 个品种，BTC/ETH/SOL 占七成 | Claude §1 |
| A2 | **K 线来源已实测**：`data.binance.vision` 本机与 jp-24 均可达，USDT 永续 1m 归档从 2020-01 起，API 可补到 2019-09；新币从上市日起；本期不需要付费数据 | 会话核验 |
| A3 | **L3a 风格刻画的具体做法**：以交易员信号为标签、通用指标为特征、浅模型拟合、两关验证（复现 + 自发信号盈利） | Claude §2.1 |
| A4 | **Alpha-GPT 式假设循环**的落点：人/模型提假设 → 形式化为特征集上的规则 → 小网格扩展 → 回测 → 解释 → 迭代。用户是外行，**假设来源改为交易员自己的行情分析帖**（`hermes_decisions` 里 56 条 `analysis` + 信号正文里的理由），由模型抽"他自述的做单依据"，用户只做勾选 | 会话 |
| A5 | 执行政策参数网格的具体候选（≤5 维、每维 ≤4 档） | Claude §5.2 |
| A6 | 物料清单：数据全部免费；需授权的是生产库导出、watcher sqlite、图片；用户要出的是标注时间和三个决策 | 会话 |

## 4. 分歧与裁决

| 议题 | Claude | Codex | 裁决 |
|---|---|---|---|
| 抽象层命名 | L0 复刻 / L1 执行政策 / L2 元标签 / L3a 刻画 / L3b 自主 | L1 复现 / L2 共性因子 / L3 自主 | 统一为 Claude 五层；Codex L1=L0，L2=L1+L2，L3=L3b |
| 种子资产形态 | 生产库物化视图 `signal_seeds` | 版本化 JSONL 导出 + EvidenceManifest，不先动生产表 | **Codex**。先导出，视图等研究稳定后再评审迁移 |
| 回测器 | order_plan 级模拟器，复用纯函数，保守撮合 | 同类模拟器，但要求：双版本语义、同 bar 歧义给上下界、费用/资金费率分列、IOC 允许零成交、右删失 | **Codex 的严格度 + Claude 的保真度阈值**。保真度对账只在"唯一可归属的纯机器人 episode"上做，容差按 Codex（数量 ≤1 步进、价格 ≤1 tick） |
| 影子层风控预览 | 调 operator API `dry_run` | 影子零凭据 | **Codex**。风控预览改为离线导入风控纯函数对导出快照跑；若需在线预览，另起只读端点评审 |
| 灰度第一步 | 改 zone_ladder 配置在单账户生效 | 先过 M2 执行一致性，再单账户 canary，用户授权 | **Codex**。因为 C1/C2，"改配置"实际改的是 read_api 那套，语义未对齐前不动 |
| 量化 → 真实订单的通路 | 仓位乘子进 risk_budget | 量化建议 → Hermes 核验采纳 → 现有风控 → 节点 | **Codex** 为近期路径；乘子/否决权作为后续契约变更单列（需用户明示） |
| 四账户分配 | a/b 对照、c/d 实验，或按信号 hash 随机 | 先全部虚拟配对；用户批准后仅 d 作小风险 canary；不为凑 A/B 增加订单 | **Codex**。真实跨账户对照只作辅助证据 |
| 收益结论门槛 | 306 条样本 walk-forward + bootstrap | ≥200 条可证发布时间的新鲜计划、≥60 自然日、≥30 独立事件簇 | **Codex** 作为收益结论门槛；306 条里满足"可证发布时间"的可能远少于 200，不足则只交工程验证 |
| gold 扩容 | 100 开仓 + 50 管理 | ≥300 条连续消息（含噪音/观察），三层标注，危险样本双人 | **Codex** 范围 + Claude 的 100/50 作为子集下限 |
| L3a 指标刻画 | M2 起做描述统计，舒琴试做触发模型 | 未涉及 | **保留 Claude**，但纳入 Codex 的锁箱/预注册纪律：假设清单先登记再评估，看过结果不得回头改 |

## 5. 统一路线

| 里程碑 | 产出 | 判据 | 路由 | 预估 · capability_status |
|---|---|---|---|---|
| **M0 证据盘点** | 只读导出 JSONL + manifest；字段覆盖矩阵；两条时钟对照；**Telegram 重拉发布时间可行性探查**；当前路由/所有权快照；H-live vs v1.1 执行版本清单 | 抽 20 个已执行计划逐层追到原消息/批准/订单/退出，断链全部有类型；进入 cohort 的样本 100% 有可证发布时间 | Grok 实现（导出脚本、K 线预热）；Claude 判读 | 1 周 |
| **M1 标注与 gold** | ≥300 条连续消息三层标注；episode 关系图；原 30 条 bench 兼容报告；**交易员方法自述抽取**（从分析帖与信号理由抽假设清单，用户勾选） | 危险样本全部人工签字；观察帖误开仓/跨频道误管理/数量级猜测为 0；≥30 条管理事件归属人工核验 | AI 出草稿；用户签字（约 4 小时）；Claude 组织 | 1–2 周 |
| **M2 确定性内核与执行一致性** | `evaluate_plan` / `compile_candidate` 无副作用内核；两套 ladder 差异清单；契约 v1.1 与线上 wire 收编方案；≥60 边界夹具 | 同输入同版本轨迹哈希一致；每个输出过 schema 与节点语义；不满足现网 wire 的功能标 blocked | **Codex** | 2 周 |
| **M3-E 工程门** | 模拟器（自研主 + Nautilus 审计）轨迹重放、撮合一致性、费用桥、拒因、回放哈希 | ≥20 个唯一可归属纯机器人 episode，覆盖 long/short、market、limit/zone、未成交、右删失；**禁止收益优越性主张** | Codex 写模拟器；Claude 判读 | 2 周 · `research_only` |
| **M3-R 探索门** | H-live/H-replay/Q-matched/Q-full 四基线；分层描述；**L3a 风格画像**；舒琴触发模型试跑 | ≥120 个 `A_content_and_time` 且结构完整的计划、45 自然日、20 独立事件簇；报 `N_plan/N_episode/N_cluster`、逐层损耗、宽 CI、证据等级；只能写探索性方向估计 / 无优势 / 不可判断 | Grok 跑网格与出图；Claude 判读 | 1–2 周 · `research_only` |
| **M3-P 晋级门** | 执行政策候选的有限晋级申请 | ≥200 个可评估新鲜计划、60 自然日、30 事件簇、三段 walk-forward、保守成交、成本压力、簇 bootstrap 95% CI、最终 holdout 只跑一次；**不等于生产上线** | Claude 提交，用户裁定 | 按样本到达 · `research_only` |
| **M4 在线只读影子** | 零凭据影子进程、独立存储、配对轨迹面板 | ≥4 周且 ≥50 新鲜候选；误配置也不能产生 outbox/订单；候选 100% 可重放 | Codex 设计、Grok 实现、部署需用户授权 | 4 周墙钟 · `shadow_only` |
| **M5 单账户 canary** | 用户授权记录；d 账户准入核验；逐笔对照 | M0–M4 全过 + 用户明示；≥4 周 30 个完整周期无重复开仓/跨归属/保护缺失 | 用户开闸 | 按步 · 用户授权且门禁通过后方为 `production_allowed` |
| **后续** | L2 乘子/否决权契约、L3b 自主决策主体 | 各需独立契约评审与用户签署授权 | — | 2026 Q4 后 |

## 6. 用户需要提供的（更新版）

- **不需要**描述交易员的方法。M1 由模型从频道分析帖抽"他自述的依据"，你在假设清单上勾选即可。
- **标注签字**：M1 约 4 小时，AI 出草稿，你逐条确认危险样本。
- **三个授权（分别、明确、执行前取得）**：① 生产库只读导出到本机（M0-T1）；② 复制经明确授权的 watcher 会话材料到隔离的只读运行目录，**不直接使用生产 watcher 活跃 session**——探查进程仅允许按 channel+message id 读取消息/实体与 `date`，禁止发送、转发、处理 ingress、调用 Hermes/operator API 或任何写路径，遇 FloodWait 立即停批，session 不入 repo/shadow/普通日志，探查进程不得持有生产 operator token（M0-T2）；③ 影子进程部署到 jp-24（M4 时再问）。
- **一个决策**：canary 账户默认 d，M5 前确认。
- 数据不用买。

## 7. 未核验与遗留

- Codex 引用的 `read_api.py:8373/8406`（operator 通路 raw 用 now）与 `intent_execution_strategy.py:10230` 未逐行复核，方向与已核实的 C1/C3 一致。
- `published_at` 能否通过 Telegram 重拉补齐是整条路线的第一道闸，M0 第一天做。
- 两份方案都没有回答"交易员的入场本身有没有正期望"。这是 M3 的输出，不是前提；M3 结论为"无优势"时路线在 M3 结题，不算失败。
- 本文与两份方案均为设计文档，未改任何代码、未跑任何写命令、未触碰生产状态。

## 8. 第二轮收口（Codex v2，2026-09-07）

Codex 第二轮方案：[codex-proposal-v2](2026-09-07-channel-seed-to-quant-codex-proposal-v2.md)（446 行，gpt-5.6-sol 跑出；gpt-6-astra 因网关 ch47 禁用后 ch43 并发不足连续四次"满载"失败）。它逐项答复了 §4 十项裁决：九项接受或附条件接受，一项（收益门槛）保留异议。以下为最终裁决，**覆盖 §4/§5 中冲突之处**（§5 路线表已于 2026-09-08 改写为 M3-E/R/P 三行并加 capability_status）。

| 议题 | v2 立场 | 最终裁决 |
|---|---|---|
| M3 门槛 | 拆三道门：M3-E 工程（≥20 唯一可归属纯机器人 episode）/ M3-R 探索（≥120 可证发布时间计划、45 天、20 簇，只出方向性估计）/ M3-P 晋级（200/60/30 + 保守成交 + 成本压力 + 簇 bootstrap CI） | **采纳**。§5 M3 行改为三层；306 条扣除不可证时间/归属/删失后大概率只到 M3-R，报告措辞按 v2 §3.3 |
| GramJS 重拉发布时间 | 可行但先做 20+10 样本只读探查；不得共用生产 watcher 活动 session；方法白名单（只读消息/实体解析）；FloodWait 立即停批；结果分 A_recovered / A_current_not_original / B_watcher_archive / C_ingest_only；A 级 <80% 不硬凑 M3-P | **采纳**，替换 §2 C3 与 §6 中"重拉即可补齐"的表述。用户授权项相应改为"复制会话材料到隔离只读目录"而非直接用生产会话 |
| 交易员分析帖 | 是带原文哈希的假设来源，不是方法真值；用户只勾"规则是否忠实于原话"，不勾"是否赚钱"；看过结果后补的理由标 post_hoc | **采纳**，§3 A4 与 §6 第一条按此收紧 |
| L3a 两关 | 降为候选规则的预注册判据；每交易员一个主假设族、≤12 基础假设 × ≤3 变体；簇级 block bootstrap / 置换检验；舒琴清洗后 <80 条则停触发模型只做分布 | **采纳**，Claude 方案 §2.1 判据按此解释 |
| trade_outcomes 10 行 | 不得支撑任何收益结论；结局由 K 线重建，实盘 outcome 只作可归属校准 | 与 §1 一致，**采纳** |
| 五层命名 | 接受，但路线表加 `capability_status=research_only\|shadow_only\|production_allowed` 列；L3a/L3b 本期一律 research_only | **采纳** |

### 8.1 M0 第一周任务书（可直接派发）

v2 §6 给出四个六段式原子任务，互斥输出，全部只读：

| 任务 | 内容 | 路由 |
|---|---|---|
| M0-T1 生产证据只读导出 | JSONL + manifest + 字段覆盖矩阵 + 信号/decision/intent/order/position/episode 断链分类；禁止运行 `trade_outcomes.py` 的 main；**新增订单生命周期审计表**：按机器人 clientOrderId、intent、账户、symbol、position side、事件时间串联，报告各状态覆盖率、重复事件、孤儿事件、join failure、未观测状态与右删失；覆盖率未达预注册阈值时 M3-E 只允许工程描述，不得出保真度或收益结论；手动单（clientOrderId 不匹配 `^B[0-9a-f]{32}[0-9]{2}$`）不读不动 | Grok（需用户授权只读导出到本机） |
| M0-T2 双时钟与 GramJS 可行性探查 | 20 已执行 + 10 失败样本；先查 watcher sqlite/备份有无 date，再隔离会话只读查询 | Codex 写探查脚本，Grok 跑；**需用户授权会话材料复制** |
| M0-T3 执行版本/契约/所有权快照 | H-live vs v1.1 逐字段差异、wire 枚举、节点识别、≥60 边界夹具候选、`blocked` 列 | Codex |
| M0-T4 K 线覆盖与前视风险清单 | 44 品种 UM 1m 覆盖/缺口/上市起点；ATR 只用信号前闭合 bar；同 bar 歧义上下界配置 | Grok |

派发顺序：T1 与 T4 无依赖可并行；T2 依赖 T1 的样本清单；T3 独立。四份任务书原文见 v2 §6，派发时直接引用，不再改写。

### 8.2 gpt-6 版 v2 增补（2026-09-07，[codex-proposal-v2-gpt6](2026-09-07-channel-seed-to-quant-codex-proposal-v2-gpt6.md)，483 行）

ch47 仍禁用时在 ch43 上与 review 任务并发跑通。与 gpt-5.6-sol 版结论一致，六条增补全部采纳：

| 增补 | 内容 | 落点 |
|---|---|---|
| 三个分母 | `N_plan`（306 上界）/ `N_episode`（唯一可归属纯机器人 episode）/ `N_cluster`（事件簇 purge 后）互不替代；`411/118` 不能算成交率，`411/306` 不能算计划收益率 | M3 各门同时报三个分母与逐层损耗表 |
| 保真度 F1–F4 | F1 计划语义（原文→字段→规范化计划）/ F2 执行语义（计划→ladder→节点 wire）/ F3 订单轨迹（回放 vs 411 fills + 118 intent 双向对账）/ F4 结局重建（K 线触价 vs 10 行 outcome 方向性校准）；先锁 F1/F2 再校 F3 最后 F4；校准参数只在校准集拟合 | 替代 Claude 方案 §4.2 的三阈值 |
| A 级再拆 | `A_content_and_time`（channel+message id+文本/媒体指纹+date 全对上）与 `A_time_only`；只拿到 date 不算原始计划 | M0-T2 输出字段 |
| 拒绝/撤单分层 | 657 条 reject+cancel 按 plan→intent→order 漏斗位置分层（halt/品种限制/确认卡死/过期/手动仓隔离），不做拒因排行榜 | M0-T1 断链分类 |
| M0-blocked 路由 | T2 时间探查或 T3 wire 兼容失败时，路线明确停在"工程交付 + 缺口账本"，不得以 Hermes 建议或配置灰度绕开 | §5 路线表加失败出口 |
| M4 负向测试 | 主动制造重复 raw、过期建议、未知 symbol、错误账户、网络重试、错误配置，证明零路径能写 intent/outbox/订单/节点 | M4 判据 |

其 §9 对 Claude 方案的保留意见与 5.6 版相同（分析帖非真值、GramJS 非补齐方案、200/60/30 非唯一入口、影子零 token、L3a 两关非上线判据），均已在 §8 采纳。

### 8.3 尚未闭合

- Codex 对 Claude 方案的对抗 review 尚在跑（见 §9，待补）。
- 网关：ch47 禁用未恢复，gpt-6-astra 在 ch43 上并发不足，且 ch43 对 codex 类模型不支持 chat/completions（Hermes 主脑接口）。Hermes 视觉模型已于 2026-09-06 17:01 UTC 切为 gpt-5.6-sol（备份 `config.yaml.bak-vision-gpt56sol-20260906T170114Z`），无需重启，待下一条带图信号验证。

## 9. Codex 对 Claude 方案的对抗 review：终裁（2026-09-07）

来源：[codex-review-of-claude-proposal](../reviews/2026-09-07-codex-review-of-claude-proposal.md)（203 行，gpt-5.6-sol）。10 必修 / 8 应改 / 4 建议。Claude 逐条判断，代码引用抽核两处均成立（`read_api.py:739` 区间展开读 Binance mark price；`governor.py:109-122` 更新动作必须唯一匹配一个仓位）。

| # | 发现 | 成立？ | 处置 |
|---|---|---|---|
| 必修-01 | 状态计数（349 开仓、218 rejected+expired）不是样本单位 | 成立 | 采纳。样本单位 = 信号事件（`source_message_id + entry-ref`），各层状态分列，`not_actionable/duplicate/stale/system_blocked/manual` 分类后才进入估计。与 Codex v1 PlanEvent、v2 M0-T1 一致 |
| 必修-02 | `raw_payload` 里没有 Telegram date，"A 级 ≥80%"是未验证假设 | 成立 | 已由 §2 C3 与 §8 GramJS 探查覆盖；补一条：**先修 watcher/feeder 让新数据存 `telegram_published_at`**，历史只能分级不能承诺 |
| 必修-03 | L3a 标签把事件压成 K 线，负样本没有 at-risk universe | 成立 | 采纳。正样本 = 信号事件；负样本 = 该交易员当时关注品种集合内、数据可用且未发信号的机会；固定采样率 + IPW 校正；报告 precision/recall/校准曲线按日期/品种阻塞 |
| 必修-04 | 两关判据（60%/3 倍/CI>0）无统计意义 | 成立 | 采纳。v2 §4 已降为预注册判据；再加 precision/coverage、按日/episode block bootstrap、最终 holdout、"不可判定"出口 |
| 必修-05 | episode 归属不能以 nullable 的 `target_position_id` 为主链 | 成立 | 采纳。主事实 = 机器人 fill stream + 账户/品种/**position side** 账本 + entry/management intent 链；`target_position_id` 只作校验过的 hint；歧义返回 unresolved |
| 必修-06 | OHLC 撮合不能声称复现 Binance 执行（mark/last、IOC 余量、post-only 拒单、filter） | 成立 | 采纳。模拟器定名"OHLC 反事实模型"；记录订单状态机、触发基准、tick/step/min-notional、post-only reject、IOC residual；同柱多触发给上下界 |
| 必修-07 | 保真度阈值不可验证（"只有 10 条结局"） | **前提有误，结论部分成立** | 生产 `execution_events` 机器人订单：411 OrderFilled / 118 intent / 657 reject+cancel，说明校准**事件存在**，但不构成校准样本（见 §9.1 收回）：须先做订单生命周期 join 审计，再对 accepted/rejected/working/partial/filled/canceled 全状态做校准，报告 fill probability calibration、误差分位数、block CI |
| 必修-08 | 网格搜索后普通 OOS CI 有多重检验 | 成立 | 采纳。外层时间 walk-forward + 内层调参 + 完全未触碰最终 holdout；候选结果表全量公开；达不到就只出描述报告 |
| 必修-09 | 灰度不全是加法，写 `risk_budget` 越过授权边界 | 成立 | §4 已裁决走 Codex 路径；补充：第 1 步"改配置"因 C1 也不成立；回滚定义 = 停新意向 + 撤自有挂单 + 单独处理在途 |
| 必修-10 | CUSUM 无参数定义、多频道误报、"降乘子"是隐性自动风控 | 成立 | 采纳。第一阶段 alert-only，预设基线/阈值/warm-up/cooldown，误报率按 block 重采样校准；乘子变更只能走人工授权 |
| 应改-01~08 | 宽表粒度、gold 覆盖率推断、合约生命周期、模拟/实盘字段隔离、保护单账本、四账户非 A/B、50 条触发无有效样本量、工期排除人工与门禁 | 全部成立 | 全部采纳。里程碑改为证据闸门（M0a provenance / M0b episode+gold / M1a 交易所语义合同 / M1b 模拟器单测 / M1c 实盘事件校准 / M2 研究协议 / M3 shadow / M4 canary），墙钟估时保留为非约束参考 |
| 建议-01~04 | L0/L1 价值是待证假设；特征 as_of；shadow 漏斗按原因拆；上线前冻结 execution semantics contract | 成立 | 采纳。execution semantics contract 并入 M0-T3 产出 |

**终裁**：接受 review 的裁定。Claude 方案作为路线输入保留，**不作为开工依据**；M0-T1~T4 四份任务书（全部只读、零生产改动）**只覆盖 M0 阶段允许的 provenance、时间证据、执行契约候选、K 线/前视风险**；episode+gold、research protocol、shadow boundary 仍分别由 M1/M2/M4 闸门验收。**M0 派发不等于六项前提全部满足，且不得据此启动 M1 研究、M3 政策选择、M4 shadow 或 M5 canary。** M1 之前必须冻结 execution semantics contract（M0-T3 产出）。

### 9.1 gpt-6 版对抗 review 对照（[codex-review-of-claude-proposal-gpt6](../reviews/2026-09-07-codex-review-of-claude-proposal-gpt6.md)，211 行）

独立审查后对前一份十条必修：九条确认，必修-07 部分确认。**它反驳了本文 §9 表中对必修-07 的反驳，反驳成立**：411 fills / 118 intent / 657 reject+cancel 横跨不同实体与状态，只证明"事件存在"，不证明 `clientOrderId→intent→episode` 的 join 完整性、状态分母与孤儿率；"校准底子够"是过度声明，收回。处置：M0-T1 增加**订单生命周期审计表**（已写入 §8.1 T1 任务书，2026-09-08）（按机器人 clientOrderId/intent/账户/symbol/position side/事件时间串联，报告各状态覆盖率、重复率、孤儿事件、join failure），覆盖率未达预设值前保真度只出描述报告。

新增四点及处置：

| 新增发现 | 处置 |
|---|---|
| Claude 方案 §6.1 正文仍含 operator `dry_run` 指令，按段落派发会重引零凭据禁忌 | **已改**：原文划掉并标注推翻 |
| 订单量数字无共同分母 | 同上，并入 M0-T1 生命周期审计 |
| 宽表折叠一对多为伪确定性 | 与应改-01 一致，已采纳事件分层 |
| L0/L1"立即有产品价值"无证伪条件 | **已改**：Claude 方案 §0/§2 改为待证假设，证伪条件 = shadow 漏斗中执行政策可改善部分须占可观测损失多数 |

两份 review 的 M0 裁定一致：方案为路线输入，开工须先过六项只读证据闸门（provenance / episode+gold / execution contract / event calibration / research protocol / shadow boundary）。**分阶段对照**：provenance → M0-T1/T2；event calibration（部分）→ M0-T1 生命周期审计；execution contract（候选冻结）→ M0-T3；K 线/前视 → M0-T4；**episode+gold → M1；research protocol → M2；shadow boundary → M4**。M0-T1~T4 不覆盖后三项，M0 完成只允许进入对应的 M1/M2 闸门。

## 10. 最终状态与下一步

**文档**（全部未提交，在工作树）：

| 文件 | 角色 |
|---|---|
| `docs/plans/2026-09-06-channel-seed-to-quant-claude-proposal.md` | Claude 独立方案（已标注被推翻处） |
| `docs/plans/2026-09-06-channel-seed-to-quant-codex-proposal.md` | Codex 第一轮 |
| `docs/plans/2026-09-07-channel-seed-to-quant-codex-proposal-v2.md` | Codex 第二轮，含 M0-T1~T4 任务书 |
| `docs/reviews/2026-09-07-codex-review-of-claude-proposal.md` | Codex 对抗 review（gpt-5.6-sol） |
| `docs/reviews/2026-09-07-codex-review-of-claude-proposal-gpt6.md` | Codex 对抗 review（gpt-6-astra，对照裁定） |
| `docs/plans/2026-09-07-channel-seed-to-quant-codex-proposal-v2-gpt6.md` | Codex 第二轮（gpt-6-astra 版） |
| 本文 | 合并 review + 终裁 |

**派发闸门（按敏感动作分别授权，不是一次性批准整组）**：T3（Codex）/T4（Grok）可先做本地静态与离线工作；**T1 只有在用户明确授权生产库只读导出后派发**；**T2 只有在用户另行明确授权复制会话材料后派发**，复制失败、FloodWait、权限不足或内容同一性不足时立即停止，不改用生产 session、不绕过分级。本次派发仅授权只读证据盘点；不表示六项前提已通过，不得把任务输出解释为研究、shadow 或交易授权。任何部署、重启、RESUME、风险预算/乘子变更均不属于 M0 授权；所有失败只停在证据缺口，不触发任何生产写路径。

**用户待决**：① 三项授权（只读导出 / 会话材料复制 / 后续影子部署）；② ch47 是否恢复启用；③ M1 标注约 4 小时的时间。
