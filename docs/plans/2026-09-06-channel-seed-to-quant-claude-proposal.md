# 频道执行计划 → 可执行量化算法：Claude 独立方案

> 状态：草案（与 Codex 独立方案并列，供合并 review）· 2026-09-06 · 起草：Claude Code
> 2026-09-07 更新：本方案已被 Codex 对抗 review（docs/reviews/2026-09-07-codex-review-of-claude-proposal.md，10 必修）与合并 review §9 终裁定为"路线输入，不是开工依据"。§2.2"回测器直接复用线上展开函数"与 §6.1"影子层调 dry_run"两点已被推翻（线上分档展开是 read_api 另一套实现，纯函数无调用方；影子层须零凭据）。以 [合并 review](2026-09-06-channel-seed-to-quant-merged-review.md) 为准。
> 数据依据：jp-24 生产库 2026-09-06 只读查询；仓库现有网关/统计工具的接口

## 0. 一句话立场

现有系统已经把"频道信号 → 交易"拆成了三层：**触发**（交易员发帖）、**结构**（入场结构/止损/止盈梯度，由 Hermes 从原文解析）、**执行政策**（zone ladder、时效、保护单、仓位管理，网关+节点机械执行）。量化这条路的正确顺序是**自下而上**（L0/L1 的产品价值为待证假设，见 §2 修正）：先把"结构 + 执行政策"做成可回测、可复用的机械层（不动触发），再用信号语料去学"哪些信号值得做、做多大"（元标签），最后才是"不靠信号也能自己触发"的自主策略。倒过来做（先想自主策略）在当前 349 条开仓样本的规模下必然过拟合。

## 1. 种子资产盘点（2026-09-06 生产库实查）

| 资产 | 数量 | 说明 |
|---|---|---|
| Telegram 原始消息 `raw_messages` | 836 | 舒琴 372 / Titan 161 / Gauls 152 / 坚果TV 115 / TraderCash 36；跨 2026-02 → 09 |
| Hermes 决策 `hermes_decisions` | 811 | 其中 `open_position` 349、`move_stop_loss` 89、`replace_take_profits` 46、`partial_close` 17、`close_position` 15、`cancel_order` 151 |
| 带价格的决策 | 306 有入场价 / 314 有止损 / 265 有止盈 | 这是结构化的"执行计划"本体 |
| 意向 `trade_intents` | approved 380 / rejected 166 / expired 52 / cancelled 19 | rejected+expired = 218 条"有计划没执行"，也是回测样本 |
| 实盘结局 `trade_outcomes` | **10** | 物化严重不足（R 均值 0.09）。结局数据基本要靠 K 线重建 |
| gold 标注 | 30 条（`eval/v3_trader_signal_bench/dataset.json`） | Hermes 解析质量的真值，只覆盖解析，不覆盖结局 |

按频道看开仓决策：舒琴 141、Titan 35、Gauls 26、坚果 20、Cash 2、操作员口头 92（`hermes-operator` + `operator`）。**舒琴一家占频道开仓的 63%**，跨频道结论天然被舒琴主导。

三个硬缺口：

1. **结局缺失**：306 条带价计划里只有 10 条有 R。不重建结局，什么都学不到。
2. **信号发布时间不可靠**：09-05 事故复核指出 watcher 不存 `entry.date`（`docs/plans/2026-09-05-missed-signals-remediation-plan.md` §0.5 G 项），`raw_messages.source_received_at` 在补发场景下是入库时间而非发布时间。回测的决策时刻必须以发布时间为准，否则是前视偏差的反面（滞后偏差）。
3. **标签是 LLM 解析不是真值**：`hermes_decisions` 是模型输出，bench 里 `sl_accept` 允许多个答案本身说明解析有歧义空间。把错解当交易员意图会学错东西。

## 2. 抽象层级：L0 → L3

| 层 | 名称 | 输入 | 输出 | 触发权归谁 |
|---|---|---|---|---|
| L0 | 机械复刻 | 频道信号的结构化参数 + 发布时间 + 1m K 线 | 每条信号在**给定执行政策**下的模拟结局（fill/R/MAE/MFE/参与率） | 交易员 |
| L1 | 执行政策优化 | 同上 + 政策参数空间 | 每频道（或全局）的最优执行政策：ladder 权重/追入窗口/TTL/TP 分配/移损规则 | 交易员 |
| L2 | 元标签（做不做、做多大） | 信号特征（频道、品种、区间高度/ATR、距现价溢价、时段、趋势一致性、频道近期表现、同日信号密度） | 每条信号的期望 R 与建议仓位乘子（含 0 = 跳过） | 交易员触发，算法调 size / 否决 |
| L3a | 风格刻画 | 信号时刻的指标环境 | 每个交易员的可读规则画像（入场环境、SL/TP 相对结构） | 交易员 |
| L3b | 自主 setup | 行情本身 | 不依赖信号的开仓触发（用频道信号作为正样本校验规则是否在同一时刻发同样的单） | 算法 |

L0/L1 是**工程上确定能做**的，且不新增任何开仓权；但"改善执行质量"是**待证假设**（2026-09-07 review 修正）：若主要损失来自 watcher 断流、HALT、拒单等基础设施原因，优化 ladder 只是在错误入口上精细化。证伪条件：shadow 阶段按 `algorithm_skip / risk_reject / stale / halted / node_unavailable / exchange_reject` 拆的漏斗里，执行政策可改善的部分必须占可观测损失的多数。L2 是中期目标，前提是样本量。L3 拆成两半：**L3a 风格刻画**（用通用指标描述交易员出手时的市场环境，纯描述统计）从 M2 起就做；**L3b 自主触发**（规则自己开仓）要过 §2.1 的两关才排产品里程碑。

### 2.1 用通用指标拟合交易员的市场判断（L3a → L3b）

用户设想：交易员各有一套方法，能否用几个普遍指标拼出他们的做单思路。量化里对应三个成熟做法：行为克隆（把人的动作当监督标签学策略）、规则归纳（用可解释浅模型把"何时出手"写成 if-then）、元标签（不学何时出手，只学出手时哪些情况值得跟，即 L2）。Alpha-GPT（arXiv 2308.00016）那类 LLM 因子挖掘解决的是横截面选股问题，评估依赖成千上万样本点；它的"人提假设 → LLM 转公式 → 搜索扩候选 → 人审"分工可借来做假设生成，评估方式必须换成下面这套。

**做法**（每个交易员单独做，先只做舒琴）：

1. 标签：对该交易员交易过的每个品种，按 15m/1h 切 K 线；该根 K 线内他发出多单信号标 +1、空单 −1、其余 0。舒琴约 141 个正样本对几十万个负样本。
2. 特征（刻意少，四类）：趋势（EMA 斜率、ADX、HH/HL 结构）、位置（距前高前低、斐波那契回撤位、VWAP、布林带位置）、动量（RSI、MACD）、波动（ATR 相对值、区间高度/ATR）；再加时段、资金费率、持仓量变化。全部在信号时刻之前可算。
3. 模型：逻辑回归或深度 ≤ 3 的决策树 / 规则列表，输出必须能读成人话（如"1h 上升趋势 + 回踩 EMA20 附近 + RSI < 45 → 开多"）。
4. 止损/止盈逻辑单独回归：SL 距离 = k × ATR 或前低下方 x%，TP 梯度 = 前高 / R 倍数。这部分比入场触发稳得多，优先做。

**两关验证**（都过才算 L3b 可立项）：

| 关 | 问题 | 判据 |
|---|---|---|
| 复现 | 规则在留出月份能否找回交易员的信号 | 召回 ≥ 60% 且规则自发信号数 ≤ 交易员信号数的 3 倍 |
| 盈利 | 规则**自己发出的**全部信号（含交易员没发的）放进 §4 回测器按选定执行政策跑 | OOS 期望 R 的 95% CI 下界 > 0 |

第二关才是要害：抄一个交易员的入场只有在他的入场本身有正期望时才值得，而这一点要等 M1 结局重建之后才知道。

**已知难点**：正样本少且集中（舒琴以外单独拟合无统计效力）；主观交易员用的信息不全在价格里（消息面、大周期观点），指标只能抓到风格里"可用价格描述"的部分，剩下的表现为同形态时有时出手时不出手；类别极度不平衡，需按事件采样负样本并用 precision-recall 而非准确率。这些决定了 L3a 现阶段的产出是"风格画像"而非"替代交易员"。

### 2.2 为什么 L0/L1 是主战场

- zone ladder 方案（`docs/plans/zone-ladder-order-plan-v1.md` §7）已经用 17 条 zone 信号做过一次"刺入测算 → 三档权重"的抽象，这就是 L1 的雏形，但样本太小、只覆盖 zone、且没有闭环回测器。
- 网关的 `expand_zone_to_plan`（`services/control-plane/decision_gateway/zone_ladder.py:67`）是纯函数、输出绝对价格计划、节点零语义。**回测器可以直接调用它**，回测与实盘共用同一份展开逻辑，这是消除回测-实盘偏差最便宜的方式。
- 交易员的"管理策略"（移损到成本、TP1 减半、区间失效撤单）在 `hermes_decisions` 里是 `position_update` 事件流（89+46+17+15 条），可以按 episode 归到原开仓信号，形成"交易员管理规则"的样本。这部分现在完全靠 Hermes 逐条解释，机械化之后是 L1 的一部分。

## 3. 数据资产化（M0）

### 3.1 `signal_seeds` 物化视图

以 `hermes_decisions`（`action='open_position'`）为主键，左连：

- `raw_messages`：频道、原文、`raw_payload`（图片路径、Telegram 原始 date 若有）、`source_received_at`
- `risk_decisions` / `trade_intents`：是否批准、拒因、`order_plan`（含 07-02 后的 wire 字段和 v1.1 ladder 计划）、`valid_until`
- `execution_events`：实际 fill 价格/数量/时间（按 `client_order_id ~ '^B[0-9a-f]{32}[0-9]{2}$'` 反解 intent + seq 段：01–09 入场、11 止损、21+ 止盈）
- `trade_outcomes`：实盘 R（有则用）
- **episode 链**：同 symbol/side 且晚于本开仓、早于对应平仓的 `position_update` 决策（`move_stop_loss`/`replace_take_profits`/`partial_close`/`close_position`），用 `target_position_id` 优先、缺失时按时间窗+`entry-ref` 兜底归属。这是交易员"仓位管理"行为的原始数据。

新增字段（回测必需）：

- `published_at`：Telegram 消息真实发布时间。来源优先级：`raw_payload` 里的原始 date → watcher sqlite 的行（若存了）→ `source_received_at`。**标注来源等级**，回测报告按等级分层。
- `hermes_latency_s`：`hermes_decisions.created_at − published_at`，实盘决策延迟的经验分布，回测时按此分布加延迟，而不是假设 0 延迟。
- `parse_verified`：是否经人工核对（见 3.2）。

### 3.2 gold 扩容

`eval/v3_trader_signal_bench` 现有 30 条解析 gold。扩到 **≥100 条开仓 + ≥50 条仓位管理**，覆盖五个频道、四种入场类型（market/limit/zone/分批）、模糊措辞（附近/略破）。这不是为了评 Hermes，而是为了让 `signal_seeds` 里至少三分之一样本的结构参数是真值。工作量是人工的，无法绕开；AI 可出草稿，签字必须是人（沿用 `docs/replay/CORPUS_ASSESSMENT.md` §5 立场）。

### 3.3 K 线

`scripts/analysis/zone_penetration_stats.py:316 load_klines` 已能从 binance vision 拉 1m 并本地缓存。需要：覆盖 `signal_seeds` 里全部 symbol（含 MUUSDT 一类美股代币）、日期范围从最早信号前 30 天到今天；离线批量预热缓存；对 geo-block 失败要有明确的缺失清单而不是静默跳过（`trade_outcomes.py:382` 现在是跳过并记原因，回测器要拒绝无 K 线的样本进入统计）。

**M0 判据**：每条开仓决策能回答四个问题（何时发布、什么计划、实盘做了什么、结局是什么），缺失率按字段列表；`published_at` 等级 A（真实发布时间）占比 ≥ 80%，否则先修 watcher 存 date 再往下走。

## 4. 回测引擎：order_plan 级模拟器（M1）

**不引入** freqtrade backtesting 或 Nautilus backtest。两者的语义单位是"策略在每根 K 线上给信号"，而我们的单位是"一份已展开的绝对价格 order_plan 在 K 线上如何被成交/止损/失效"。专用模拟器 `scripts/analysis/plan_replay.py` 的规模是几百行，且能直接复用现有纯函数。

### 4.1 输入/输出

- 输入：`signal_seeds` 一行 + 执行政策配置 + 1m K 线切片（`published_at + latency` 起，到 `expires_at` 或平仓）
- 展开：调 `expand_zone_to_plan` / 单档 limit / market 路径，`MarketContext` 的 p0 取延迟后第一根 K 线的 open（严禁用信号时刻之后的价格）
- 撮合规则（保守口径，写死并写进测试）：
  - 挂单：long 需 `low < price`（不是 `<=`）才算成交，成交价 = 挂单价；short 镜像
  - 追入档：下一根 K 线 open + 滑点（滑点参数化，默认 0.05%，封顶 `limit_cap`）
  - 同一根 K 线同时触及 SL 与 TP：**SL 优先**（悲观）
  - 失效规则三条（TP1 先到撤挂单、15m 收盘击穿、TTL）按 zone ladder §3.2 实现
  - 管理规则可插拔：`none` / `breakeven_after_tp1` / `trail_by_atr(k)` / `channel_replay`（按 episode 链回放交易员实际的移损/减仓时点）
- 输出：每档 fill 记录、R、MAE/MFE、参与率、持仓时长、失效原因；复用 `trade_outcomes.py:71 build_trade_outcome` 的字段口径，让模拟结局与实盘结局同构。

### 4.2 保真度校准（回测器自己先过关）

对已实盘成交的信号（`execution_events` 有 fill 的），拿模拟器在**相同 order_plan** 下跑一遍，比较：

| 指标 | 阈值 |
|---|---|
| 模拟 fill 价 vs 实际 fill 价 中位偏差 | < 0.1% |
| 模拟是否成交 vs 实际是否成交 一致率 | ≥ 90% |
| 模拟 SL/TP 触发 vs 实际 | 不一致的逐条人工看，归类为"回测器缺陷"或"实盘异常"（HALT/拒单/手动干预） |

不过关不得进入 M2。这一步的副产品是一份"实盘执行异常清单"，本身就是运维价值。

### 4.3 前视/滞后偏差防护清单

- 决策时刻 = `published_at + latency`，latency 从 3.1 的经验分布抽样，不用 0
- 展开用的市场快照只能用决策时刻之前的 K 线
- 参数搜索（M2）严格 walk-forward：按月切，参数只在前序窗口拟合
- 频道近期表现类特征（L2）只用截至信号时刻已平仓的样本
- 信号被交易员编辑/删除的情况：`raw_messages.source_version` 保留多版本，回测只用第一版

## 5. 研究与政策优化（M2）

### 5.0 L3a 风格刻画（与 5.1 并行）

按 §2.1 步骤 1–2 给 `signal_seeds` 每条开仓补"信号时刻指标向量"（存进视图，便于 L2 复用），出每频道的指标分布图与 SL/TP 相对结构回归。这是描述统计，不做触发模型；触发模型（步骤 3）在 M2 只对舒琴试做一次并报告两关结果，不进产品。

### 5.1 先描述后优化

按频道 × 入场类型 × 品种大类，出：参与率、命中率、期望 R、MAE/MFE 分布、区间高度/ATR、发布到首次触及的时间分布。这份报告本身就回答"每个频道的交易员是什么风格"，是后续所有政策设计的依据（Titan 分批第二腿要不要加仓这类产品问题，应该由这份数据回答，而不是靠拍板）。

### 5.2 政策参数空间（刻意小）

| 参数 | 候选 |
|---|---|
| ladder 权重 | 55/30/15、40/35/25、70/30/0 |
| 追入窗口 | 0.2% / 0.35% / 0.5% |
| TTL | 12h / 24h / 48h / 信号 valid_until |
| 管理规则 | none / breakeven_after_tp1 / channel_replay |
| TP 分配 | 均分 / 前置 50-30-20 |

总共 ≤ 5 个维度、每维 ≤ 4 档，全网格也就几百个组合，在 306 条样本上做 walk-forward + bootstrap 置信区间。**判据是 out-of-sample 期望 R 的 95% CI 下界高于当前政策**，不是点估计。达不到就维持现状，报告照样有价值。

### 5.3 L2 元标签（仅当 M2 样本足够）

用可解释模型（分组统计或 logistic，不上树模型），特征见 §2 表。输出是仓位乘子 ∈ {0, 0.5, 1, 1.5}。启动条件：out-of-sample 信号 ≥ 300 条。以当前 5 频道每月约 60 条开仓的速率，是 2026 年底以后的事。写在这里是为了让 M0 的数据资产化从现在就把特征字段存下来。

## 6. 影子运行与灰度（M3/M4）

### 6.1 影子层不进 `trade_intents`

新进程 `strategy_shadow`（部署在 jp-24，systemd，与 feeder 平级）消费新写入的 `hermes_decisions`，对每条产出：shadow order_plan（M2 选定政策）+ 预测 R + 特征向量，写入**新表** `strategy_shadow_decisions`（外键到 `hermes_decisions.decision_id`）。~~同时调 `POST /v1/operator/orders` 的 `dry_run=True` 让网关校验该计划~~ **（已推翻，2026-09-07：影子进程不得持有 operator token，风控预览改为离线导入风控纯函数对导出快照跑；见合并 review C8/§4）**

这条路径**不写 approved、不产 intent、不碰节点**，与 AGENTS.md 铁律和 `CORPUS_ASSESSMENT.md` §3 禁止的 importer 旁路完全无关。

### 6.2 对比面板

逐信号列：Hermes 实际计划 vs shadow 计划、实际结局 vs 模拟结局、预测 R vs 实现 R。运行 4 周后出一份"shadow 保真度 + 政策差异"报告。判据：预测 fill 与实际 fill 一致率 ≥ 85%，且政策差异带来的 R 改善方向与 M2 回测一致。

### 6.3 灰度顺序（每步都是加法、都可秒回滚）

1. **执行政策参数**：把 M2 选定的 ladder/TTL/管理规则参数作为 `zone_ladder` 配置在**一个账户**上生效。这是改配置，不改开仓权，Hermes 仍是唯一决策者。
2. **仓位乘子**：L2 输出作为 `risk_decisions.risk_budget` 的乘子进入网关。算法只能把仓位调小或调大到上限，不能新增开仓。
3. **算法否决**（乘子 = 0）：算法可以让 Hermes 的开仓不执行。这一步改变了"谁说了算"，**需要用户明示授权**，且默认关闭。
4. L3 自主开仓：作为新的 `raw_messages.source='strategy'` 走完整 Hermes/risk 链，与铁律相容，但不在本期。

四账户分配：a/b 维持 Hermes 基线，c/d 作 treatment；或按信号 hash 随机分到 treatment/control（同一信号在两组都有账户执行，天然 A/B）。跨账户比较一律用 R，不用 USDT。

## 7. 再校准与退化检测

- **政策复审触发**：累计 ≥ 50 条新鲜信号（沿用 zone ladder §6），重跑 M2 网格
- **频道退化**：每频道滚动 30 笔期望 R 做 CUSUM，跌破阈值发 Telegram 告警（走现有 notifier），建议动作是"降乘子"而不是自动停频道（停频道属用户决策）
- **回测器保真度漂移**：shadow 预测 fill 与实际 fill 一致率滚动监控，跌破 80% 说明市场微观结构或节点行为变了，先修回测器再谈政策
- **样本污染**：每月抽 20 条新决策做人工解析核对，解析错误率 > 10% 时暂停用新样本做参数拟合

## 8. 三大风险与我会砍掉的东西

1. **样本量与集中度**。349 条开仓、舒琴占 63%、跨 6 个月、同日多信号高度相关。任何"优化"都是过拟合高发区。对策：参数维度 ≤ 5、每维 ≤ 4 档；只认 bootstrap CI；报告必须分频道；跨频道"共性"结论在 M2 一律标注为假设。
2. **标签污染**。`hermes_decisions` 是 LLM 解析，bench 已证明有歧义；episode 归属靠 `target_position_id` 不完整。对策：gold 扩容 + `parse_verified` 分层报告；M2 的核心结论必须在 verified 子集上复现。
3. **回测-实盘偏差来自基础设施而非策略**。09-05 事故的四个根因（HALT 静默窗、inbox 冻结、第二腿拒绝、watcher 断流）没有一个是策略问题；任何回测都假设"信号能被执行"。对策：M1 保真度校准把"实盘异常"单独归类；shadow 阶段同时统计"算法想做但系统没执行"的比例，这个数字若大于政策改善带来的收益，优先级应该回到运维。

砍掉/推迟：L3b 自主触发（要过 §2.1 两关才立项；L3a 风格刻画保留）；ML 模型（用可解释统计）；新回测框架（不引 freqtrade/Nautilus backtest）；节点侧任何改动（所有政策变化都在网关展开函数的参数层）；自动停频道/自动 RESUME（违反铁律）。

## 9. 里程碑

| 里程碑 | 产出 | 判据 | 预估 |
|---|---|---|---|
| M0 数据资产化 | `signal_seeds` 视图 + episode 链 + K 线缓存预热 + gold 扩到 100/50 + 数据质量报告 | `published_at` A 级 ≥ 80%；每条开仓可回答四问 | 1 周（gold 人工另计） |
| M1 回测器 | `scripts/analysis/plan_replay.py` + 测试（撮合规则、失效规则、short 镜像、延迟采样）+ 保真度报告 | §4.2 三阈值全过 | 1–2 周 |
| M2 研究报告 | 分频道描述统计 + 政策网格 walk-forward + 建议参数 | OOS 期望 R CI 下界 > 现状，或明确结论"维持现状" | 1 周 |
| M3 影子运行 | `strategy_shadow` 进程 + `strategy_shadow_decisions` 表 + 对比面板 | 4 周后保真度 ≥ 85%，方向与 M2 一致 | 4 周墙钟 |
| M4 灰度 | 单账户执行政策参数 → 仓位乘子 | 用户明示开闸；每步 2 周观察，R 不劣于 control | 按步 |
| M5 L2/L3b 立项 | 条件触发 | L2：OOS 信号 ≥ 300；L3b：§2.1 两关通过 | 2026 Q4 之后 |

派发建议：M0 视图与 K 线预热是边界清晰的日常实现（Grok）；M1 回测器涉及撮合语义与现有纯函数复用（Codex）；M2 报告与图表（Grok 跑脚本，Claude 判读）；M3 shadow 进程涉及生产部署与新表迁移（Codex 设计，Grok 实现）。

## 10. 我最没把握的三点

1. `published_at` 能否从 `raw_payload` 里追回：若 watcher 从来没存过 Telegram date，历史样本的 A 级占比可能远低于 80%，M0 要先做这一项探查。
2. 撮合规则的保守口径（`low < price` 才成交、SL 优先）可能低估参与率，导致 M1 保真度校准通不过；届时需要用实盘 fill 数据反过来定撮合规则，而不是拍脑袋。
3. 舒琴以外的频道样本（Titan 35、Gauls 26、坚果 20）单独做政策优化几乎没有统计效力，"分频道政策"在本期可能只对舒琴成立，其余频道只能沿用全局政策。
