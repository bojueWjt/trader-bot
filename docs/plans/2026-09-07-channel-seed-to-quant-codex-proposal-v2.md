# 频道种子到量化：Codex 第二轮修订方案
> 日期：2026-09-07
> 
> 性质：第二轮架构与研究方案，不是实现、部署、恢复交易或开闸授权。
> 
> 读取范围：`docs/plans/2026-09-06-channel-seed-to-quant-codex-proposal.md`、`docs/plans/2026-09-06-channel-seed-to-quant-claude-proposal.md`、`docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md`，以及为复核 file:line 证据读取的仓库代码。
> 
> 写入范围：本轮只新建本文件，不修改前三份文档、不修改代码、不连接生产、不执行交易命令。
> 
> 结论先行：接受合并 review 的安全边界和大部分裁决，但不把 `≥200/60天/30簇` 当作 M3 启动门槛；它应保留为“收益晋级门槛”，M3 可以在较小样本下做严格的工程验证与方向性研究，但必须明确标记为不可证明优势。

## 0. 本轮只增加什么
第一轮已经说明了数据账本、事件归属、双 ladder、回测器、影子权限和四账户边界。
本轮不重复这些设计；引用第一轮和合并 review，集中回答五个未闭合问题。
第一，逐项复核合并 review §4 的十项裁决，说明接受程度和替代方案。
第二，用生产库实测的 306 条带价决策、舒琴 141 条重新审视 M3 门槛。
第三，把 L3a 从“可做的描述统计”收紧为可锁箱、可审计、可处理多重检验的假设循环。
第四，评估用 watcher 的 GramJS 会话按 channel+message id 重拉 Telegram 原消息时间的可行性和替代路径。
第五，给出 M0 第一周四个原子任务，每个任务均采用六段式任务书。
第一轮的基础立场仍然有效：先证明计划与执行可复现，再谈能否赚钱；最初产品是只读账本和无副作用模拟器，不是新的批准器，见 Codex 第一轮 `docs/plans/2026-09-06-channel-seed-to-quant-codex-proposal.md:10-25`。
生产数据规模采用合并 review 记载的只读查询结果，不把这些数量误写成当前代码已经拥有的研究数据集，见 `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:28-37`。

## 1. 修订后的总体判断

### 1.1 生产规模意味着什么
生产库有 836 条原始消息、811 条 Hermes 决策、349 条开仓决策、306 条带入场价的决策，而 `trade_outcomes` 只有 10 行，见 Claude 方案 `docs/plans/2026-09-06-channel-seed-to-quant-claude-proposal.md:11-20`。
舒琴有 141 条频道开仓，约占频道开仓的 63%；开仓还涉及 44 个品种，BTC、ETH、SOL 占七成，见合并 review `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:28-35`。
因此，306 不是 306 个同质、独立、已完成、可追溯的收益样本。
它至少要扣除发布时间不可证、结构字段未核验、无法唯一归属、仍在持仓、只有操作员入口、同日同标的重叠和 K 线缺口的记录。
141 也不是舒琴拥有 141 个独立判断。
一条原始消息可以产生多个决策，一个计划可以产生多个管理事件，跨频道同日同币种计划可能共享市场冲击。
第一轮已经指出 raw、decision、risk approval 和 execution 不是一对一关系，不能宽表 JOIN 后按行数当作信号数，见 `docs/plans/2026-09-06-channel-seed-to-quant-codex-proposal.md:42-59`。

### 1.2 M3 不是只有一个门
M3 应拆成三道不同性质的门。
M3-E 是工程门：模拟器是否按冻结规则运行，能否解释每个 fill、拒绝、右删失和费用。
M3-R 是研究门：样本是否足以输出方向性估计，是否完成时间切分、簇处理、锁箱和成本压力测试。
M3-P 是收益晋级门：是否达到可支持正收益主张的样本、时间跨度、事件簇和置信区间要求。
合并 review 的 `≥200` 个可评估新鲜计划、`≥60` 个自然日、`≥30` 个独立事件簇，保留为 M3-P，而不是 M3-E 或 M3-R 的启动条件，见 `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:497-505`。
低于 M3-P 时，结论只能是“工程通过”“结果方向”“证据不足”或“无优势”，不能写成“策略有效”。

### 1.3 交易员方法的来源改为交易员自述，但不等于真值
用户无法可靠描述交易员方法，方法假设改从频道内交易员自己的行情分析帖和信号理由抽取，这个方向接受，见 `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:32-37`。
但分析帖是交易员的解释性叙事，不是完整的决策日志。
它可以产生候选假设来源，不能直接成为标签、因果证据或盈利保证。
L3a 只在“自述文本 → 形式化规则 → 预注册评估”链条中使用它。
任何看过测试结果后补写的理由，只能登记为事后解释，不能回填成原始假设。

## 2. 对合并 review §4 十项裁决的逐项答复

### 2.1 裁决一：抽象层命名
裁决：统一采用 Claude 的 L0、L1、L2、L3a、L3b 五层。
我的结论：接受命名，但不接受把命名统一误读成能力已经统一。
L0 是给定频道计划的机械复刻。
L1 是执行政策优化。
L2 是“做不做、做多大”的元标签。
L3a 是交易员风格刻画和可解释规则拟合。
L3b 是脱离频道信号的自主 setup。
接受理由：五层把 L3a 的描述性研究与 L3b 的自主触发分开，修复了第一轮只写 L3 的粒度不足。
替代限制：路线表必须同时标 `capability_status=research_only|shadow_only|production_allowed`，不能只写 L0-L3。
例如 L3a 即使通过规则复现，也只能是 `research_only`；L3b 没有独立决策主体、授权和风控契约时必须是 `research_only`，见合并 review `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:380-383`。

### 2.2 裁决二：种子资产形态
裁决：先做版本化 JSONL 导出和 EvidenceManifest，视图待研究稳定后再评审迁移。
我的结论：完全接受，并把“先导出”升级为硬门槛。
第一轮已经要求源数据只读、标签追加版本、不覆盖 `source_received_at`，见 `docs/plans/2026-09-06-channel-seed-to-quant-codex-proposal.md:124-145`。
生产库暂不新增 `signal_seeds` 视图，原因不是反对视图，而是视图会把尚未解决的时间语义、episode 归属和 operator synthetic 混在一个看似稳定的表名里。
导出至少包含原始主键、来源、版本、发布时间证据等级、入库时间、Hermes 时间、风险结果、intent、机器人订单匹配、管理边、K 线 manifest 和缺失原因。
EvidenceManifest 必须保存 SQL 或脚本版本、导出水位、输入哈希、规则版本、K 线文件哈希和排除清单。
替代方案：研究资产稳定后再创建只读物化视图；视图只能是导出层的投影，不能成为新的事实来源。

### 2.3 裁决三：回测器
裁决：采用 Codex 的严格度，加上 Claude 的实盘保真度阈值；只在唯一可归属的纯机器人 episode 上对账。
我的结论：接受，且补充“阈值不可以跨样本稀释”。
两套 ladder 必须分别冻结为 H-live 实际语义和 v1.1 目标语义。
线上 `_execution_order_plan` 调用 `read_api.py:701` 的另一套 `_zone_ladder_order_plan`，纯函数 `expand_zone_to_plan` 当前没有线上调用方，合并 review 已将此列为 C1，见 `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:15-21`。
契约的 `order_plan.type` 枚举仍只有 `market`、`limit`、`zone`，见 `packages/contracts/v1/approved_trade_intent.v1.json:59-76`；线上 `zone_ladder` wire 不能因为节点识别它就被假定为契约已覆盖。
保真度报告分三层。
第一层是唯一纯机器人、唯一 intent、唯一 entry ref、无手动单影响的 episode。
第二层是机器人和手动仓混合但现金流可部分隔离的 episode，只能报告可归属部分。
第三层是无法隔离的 episode，只列为缺口，不能进入策略收益比较。
数量差、价格差和现金流差的容差应按交易所 step/tick 和可解释费用判断，而不是在多个样本平均后掩盖单笔错误。
同一 bar 同时命中 SL 和 TP，要保留保守下界、乐观上界和歧义标记；结论跨过零时禁止晋级。

### 2.4 裁决四：影子层风控预览
裁决：影子零凭据，风控预览改为离线导入纯函数；在线预览另行评审。
我的结论：完全接受，不接受 Claude 原方案中“调用 operator API dry_run”的近期安排。
合并 review 已确认把 shadow 放入 `trade_intents` 再靠 `status=draft` 不安全，因为网关对批准结果可能创建 execution job 并写 outbox，见 `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:22-26`。
shadow 进程不得持有交易所 key、operator token、节点写 token、生产 outbox 写权。
风险预览使用带版本的离线风控函数和导出快照，输出 `risk_preview`，不输出 `approved_at`，不发布任何批准事件。
如果以后确实需要在线校验，应建设独立只读端点，明确拒绝副作用，独立审计并重新评审权限。
替代方案：在 M4 之前只比较“离线风险预览”和生产历史风险结果，不能把离线预览说成线上控制面已经接受。

### 2.5 裁决五：灰度第一步
裁决：先过 M2 执行一致性，再做单账户 canary，并要求用户授权。
我的结论：完全接受，并把“配置改动”从自动步骤改为条件步骤。
由于线上实际展开函数与 v1.1 纯函数不同，直接改 `zone_ladder` 配置可能只是改变 `read_api` 那套语义，不能称为部署 v1.1。
M2 必须产出实际 wire、节点 planner、恢复路径和旧版本回退的逐字段差异。
不满足现有 wire 的功能必须标 `blocked`，不允许通过放宽 schema、silent fallback 或自动单档回退来伪装兼容，见合并 review `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:363-378`。
真实 canary 之前还必须复核停节点前部署门禁、审计、心跳、告警和所有权，不得将研究文档作为部署证据。

### 2.6 裁决六：量化到真实订单的通路
裁决：近期走“量化建议 → Hermes 核验采纳 → 现有风控 → 节点”；乘子或否决权另立契约变更。
我的结论：完全接受。
当前数据库约束 `model_provider='hermes'`，见 `db/migrations/0001_canonical_schema.up.sql:189-200`；网关还会拒绝非 Hermes 来源，见合并 review C6 的复核结论 `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:22-26`。
量化结果不得伪造为 Hermes decision，不得伪造 `source=operator`，不得借管理员 token 充当适配层。
近期量化只能输出建议、差异、理由和风险预览；Hermes 仍独立查看原文、时效、归属和上下文后接受或拒绝。
任何仓位乘子、算法否决或策略主体接管都需要新的来源模型、审计字段、风控契约、用户授权和回滚方案。

### 2.7 裁决七：四账户分配
裁决：先全部虚拟配对；用户批准后只把 d 作为小风险 canary；不为凑 A/B 增加订单。
我的结论：接受，但不接受“d 默认可用”的隐含假设。
合并 review 已要求先导出并审核当前路由、未平计划和账户归属，见 `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:385-398`。
d 只有在无混合仓、无未归属保护单、风险上限和最小数量均可满足时才是候选账户。
如果 d 不空闲，不得借用 a、b、c 以凑实验臂。
同一信号的虚拟 H/Q 配对足以支持第一阶段比较；真实跨账户对照只作辅助证据，不能成为开额理由。
任何真实订单仍受 AGENTS.md 的 RESUME/开闸铁律、机器人订单所有权正则和用户手动单隔离约束，见 `AGENTS.md:7-9`。

### 2.8 裁决八：收益结论门槛
裁决：`≥200` 个可评估新鲜计划、`≥60` 个自然日、`≥30` 个独立事件簇；不足只交工程验证。
我的结论：接受为 M3-P 晋级门槛，不接受为整个 M3 的启动或研究门槛。
理由见本方案第 3 节：以 306 条带价决策为分母，200 条要求保留约 65%；而发布时间、唯一归属、成熟标签、可交易 K 线和去重都会继续减小分母。
如果 M3 必须等到 200 条，项目可能在第一轮就无法验证模拟器和 L3a 方法，而这两者本身不需要把 200 条写成收益证据。
替代分层：
M3-E：至少 20 个唯一可归属纯机器人 episode 用于轨迹、撮合、费用和回放对账；不输出收益优势。
M3-R：至少 120 个具备可证发布时间和完整结构字段的计划、45 个自然日、20 个独立事件簇；可输出方向性估计、置信区间和缺口，但结论统一为探索性。
M3-P：恢复原门槛 `200/60/30`，再加保守成交、成本压力、簇 bootstrap 95% CI 和样本外尾部约束；只有这一层允许讨论“候选政策可能优于当前政策”。
M3-R 如果达不到 120，也可以做工程验证，但不能做“交易员方法有效/无效”的统计判断。

### 2.9 裁决九：gold 扩容
裁决：采用 Codex 的 ≥300 条连续消息范围，并吸收 Claude 的 ≥100 条开仓、≥50 条管理子集。
我的结论：接受，且将其与收益样本严格分离。
连续消息标注的目的，是估计噪音、观察、复盘、失败、未批准和误读分母，不是制造 300 个交易样本。
三层标注仍然是消息意图、规范化字段、指定账户和规则版本下的可执行性，见第一轮 `docs/plans/2026-09-06-channel-seed-to-quant-codex-proposal.md:147-159`。
危险样本需人工签字；缺依据的数值必须是 `unknown`，不能把系统默认或 Hermes 解析当交易员真值。
Cash 没有可执行新开仓时保持零样本，不能为了平衡频道而合成订单。
bench 原 30 条保留为兼容回归；由于 strict_ok 未覆盖完整 SL、offset、TP 梯度且允许宽松误差，它不能被解释成安全或收益认证，见合并 review C7 `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:24-26`。

### 2.10 裁决十：L3a 风格刻画
裁决：保留 L3a，并纳入 Codex 的锁箱和预注册纪律；假设来源改为交易员分析帖和信号理由。
我的结论：接受研究方向，但把“复现 ≥60%”和“自发信号 CI 下界 >0”降为候选规则的预注册判据，不是自动立项或上线判据。
Claude 的两关设计有价值：先问规则能否找回交易员信号，再问规则自己发出的全部信号是否有净收益，见 `docs/plans/2026-09-06-channel-seed-to-quant-claude-proposal.md:42-62`。
但 141 条舒琴开仓不足以支持复杂指标组合、多个时间框架和多个阈值同时搜索。
L3a 第一产出应是“哪些自述可被价格指标部分表达、哪些无法表达”，而不是替代交易员。
L3b 必须另过完整市场机会集、无信号时段、成本压力和独立决策主体门禁。

## 3. M3 门槛的重估与诚实降级方案

### 3.1 为什么 200 条不适合作为启动门槛
合并 review 的 306 条带价决策是最好的当前上界，不是可直接使用的有效样本数，见 `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:28-35`。
只要发布时间追回率低于约 65%，在其他字段全部完美的理想情况下，200 条就已经不可达。
现实中还要扣除没有可靠原图、结构字段冲突、消息为更新而非开仓、同一 episode 多条 intent、管理无法归属、K 线缺失和混合手动仓。
舒琴的 141 条足以支撑“先对舒琴做单人画像”，但不能支撑“跨频道共性策略”。
其他频道开仓数量更小，跨频道统计又会被舒琴的 63% 权重主导。
30 个独立事件簇也不等于 30 个独立交易日；同一天同一宏观行情下的多条计划必须按事件簇 purge。
因此，200/60/30 作为严肃收益主张的保守门槛合理，作为第一阶段是否能运行回测器的门槛不合理。

### 3.2 新的 M3 分层

| 层级 | 最低数据条件 | 允许输出 | 明确禁止 |
|---|---|---|---|
| M3-E 工程 | 20 个唯一可归属纯机器人 episode；至少覆盖 long、short、market、limit/zone、未成交和右删失 | 轨迹重放、撮合一致性、费用桥、拒因和回放哈希 | 任何收益优越性主张 |
| M3-R 探索 | 120 个可证发布时间且结构字段完整的计划；45 个自然日；20 个独立事件簇 | 分层描述、方向性估计、宽 CI、无优势或不可判断 | “统计证明盈利”“跨频道共性已成立” |
| M3-P 晋级 | 200 个可评估新鲜计划；60 个自然日；30 个独立事件簇；三段 walk-forward | 候选政策收益比较和有限晋级申请 | 把点估计、已平仓子集或单频道结果冒充全局优势 |
三层都要求不把 `trade_outcomes` 10 行当作完整结局；结局主要由只读 K 线重建，实盘 outcome 只作为可归属校准，见 `docs/plans/2026-09-06-channel-seed-to-quant-claude-proposal.md:17-19`。
三层都要求未成交为计划级 0 或明确的删失定义进入分母，不能只统计成交单。
三层都要求 H-live、H-replay、Q-matched、Q-full 分开，避免把链路修复收益算成 alpha，见第一轮 `docs/plans/2026-09-06-channel-seed-to-quant-codex-proposal.md:286-296`。

### 3.3 M3-R 的报告措辞
当样本达到 M3-R 但未达到 M3-P，报告标题必须写“探索性回放与执行政策估计”。
报告可以说“在当前样本、当前假设、当前成本模型下，Q-matched 的点估计高于 H-live”。
报告必须同时说“该差异未达到预设收益晋级门槛，不能排除样本选择、时间追回、episode 归属、相关簇和成本模型造成的偏差”。
若区间跨零，结论写“不可判断”。
若区间整体低于零，结论写“本样本下没有支持该政策的证据”，不能写“交易员方法无效”。
若区间整体高于零但只有一个频道或一个市场状态，结论写“频道/状态专属候选”，不能升格为共性因子。

### 3.4 样本不足时仍然值得交付什么
第一，验证两套 ladder 的实际差异，避免继续回测不存在的生产语义。
第二，验证订单级模拟器的撮合、同 bar 上下界、费用、资金费率和右删失。
第三，建立每条计划的时间、结构、归属和 K 线覆盖矩阵。
第四，输出舒琴的指标分布和 SL/TP 相对结构描述，不声称可自主交易。
第五，验证“交易员自己写出的理由”能否被少量通用指标表达。
第六，发现哪些收益差异来自 HALT、断流、拒单、延迟或手动仓，而非策略。

## 4. L3a 锁箱、预注册与假设循环

### 4.1 研究对象和边界
L3a 只研究交易员在信号时刻的可观测市场判断。
输入包括交易员自述分析帖、信号正文理由、消息发布时间证据、在当时可见的闭合 K 线、品种可交易状态和已知上下文。
输入不包括未来最高价、最终 TP、最终持仓时长、后续编辑版本、未来资金费率、未来订单结果或事后交易员解释。
原始消息的 `source_received_at` 是 insert-only，但 feeder 路径把 received_at 写入它，不能把字段名当作 Telegram 发布时间，见 `services/telegram-watcher/telegram_watcher/collector.py:64-88` 和 `scripts/hermes_signal_feeder.py:707-745`。
因此每个假设必须同时保存 `published_at_evidence_level` 和 `available_at`。

### 4.2 假设登记格式
假设登记采用追加式 JSONL，不在已有记录上覆盖修改。
建议每行包含以下字段：

```json
{
  "hypothesis_id": "l3a-shuqin-0001",
  "trader": "shuqin",
  "source_refs": [
    {"channel_id": "-100...", "source_message_id": "123", "source_version": "v1"}
  ],
  "source_kind": "analysis_post|signal_reason",
  "source_text_hash": "sha256:...",
  "self_claim_quote_ref": "span:abc123",
  "self_claim_summary": "回踩高周期支撑后观察反弹",
  "formal_rule_version": "rule-0.1",
  "feature_set_id": "small-four-family-v1",
  "timeframes": ["15m", "1h"],
  "direction": "long|short|both",
  "eligible_symbols_policy": "historical-tradable-universe-v1",
  "as_of_policy": "closed-bars-only-v1",
  "primary_endpoint": "signal_recall|net_r_per_opportunity",
  "secondary_endpoints": ["precision", "coverage", "mae", "mfe"],
  "exclusions": ["missing_published_at", "ambiguous_direction"],
  "candidate_budget": 3,
  "split_plan": "train_2026q1_valid_2026q2_test_2026q3",
  "cluster_key": "market_event_day_symbol",
  "multiple_testing_family": "shuqin-entry-v1",
  "registered_at": "2026-09-07T00:00:00Z",
  "lockbox_hash": "sha256:...",
  "status": "registered"
}
```
其中 `self_claim_quote_ref` 只指向原文片段哈希或不可变导出中的行号，不在登记文件直接复制大量原文。
`formal_rule_version` 必须说明指标定义、窗口、滞后、阈值、方向、触发时点和缺失处理。
`primary_endpoint` 只能在看到结果前选定；结果出来后新增指标只能作为探索性附加输出。
`candidate_budget` 是该假设允许的小网格变体数，超预算必须新建假设族并承担新的多重检验代价。

### 4.3 假设来源的抽取流程
第一步只抽交易员明确写出的判断依据，不从盈利结果反推原因。
第二步把同义表达归并，例如“回踩支撑”“踩到支撑”“支撑附近接多”可以进入同一语义族，但要保留原始引用。
第三步由模型提出候选指标表达，由人工只确认“是否忠实表达原话”，不确认“是否赚钱”。
第四步把候选指标翻译成确定性规则，冻结计算版本。
第五步生成锁箱 manifest，包含原始语料哈希、样本水位、K 线哈希、代码版本、特征版本和规则版本。
第六步锁箱后才可运行回测。
用户的职责是勾选“这条规则是否忠实于交易员自述”和“危险歧义是否可接受”，不是替模型发明交易规则。
如果没有交易员明确自述，模型可以登记“待验证解释”，但不能把它标成 `self_claim`。

### 4.4 评估顺序
第一阶段是语义忠实度，不看收益。
检查规则是否与原文的方向、时间框架、位置概念和条件关系一致。
第二阶段是信号时刻描述统计。
只在交易员已发出信号的样本上比较指标分布、方向、位置和 SL/TP 相对结构。
第三阶段是时间外复现。
训练期拟合阈值，验证期固定规则，测试期只运行一次；交易员信号标签是 +1、-1、0，但负样本要按预注册的事件和时间抽样。
第四阶段是自发机会集回放。
规则必须在完整历史可交易市场时刻运行，包括交易员未发帖、没有频道消息和没有真实成交的时段。
第五阶段才比较净 R、参与率、尾部损失、回撤、MAE/MFE 和成本压力。
第六阶段输出“可表达部分”和“不可表达部分”，不把不能解释的部分自动归因于噪音。
两关判据沿用 Claude 方案的思想，但改写为研究判据：复现指标和自发机会集净收益都必须在预注册的时间外集合上评估，见 `docs/plans/2026-09-06-channel-seed-to-quant-claude-proposal.md:53-62`。

### 4.5 多重检验和小样本纪律
每个交易员先只允许一个主假设族。
每个假设族最多 12 个基础假设，每个基础假设最多 3 个预注册变体；超出后不删除旧结果，而是登记新研究族。
不同交易员、不同方向、不同目标变量分别定义检验家族。
描述统计不做显著性宣称，但仍要报告全部候选、全部排除和缺失原因。
若需要给出探索性 p 值，采用按市场事件簇重采样的 block bootstrap 或置换检验，而不是把每根 K 线当独立样本。
若要做正式的多重比较，优先采用以检验家族为单位的 max-T 或置换校正；Benjamini-Hochberg 只能用于探索性发现率控制，不能单独作为上线证据。
主要收益判据不依赖单个 p 值，而依赖预注册指标、簇级置信区间、成本压力、三段 walk-forward 和独立测试期。
测试期一旦读取结果，不能再改规则后继续使用同一测试期作为确认集。
新规则必须创建新的 `hypothesis_id`；旧锁箱保留，若用于新规则比较，只能降级为探索性验证集。
任何“看完结果才想到的新特征”必须标记 `post_hoc=true`，不进入原假设的确认结论。

### 4.6 L3a 的停止条件
如果发布时间可证样本不足，停止 L3a 的收益评估，只做语义和指标覆盖。
如果舒琴的 141 条经过清洗后不足 80 条，停止触发模型，只做分布和规则忠实度。
如果自发机会集与频道时刻的结果差异主要由交易成本、延迟或市场状态造成，结论应是“价格指标不足以替代完整信息集”。
如果复现过关但自发净收益不过关，不立项 L3b。
如果自发净收益点估计过关但簇 CI、保守成交或成本压力不过关，不立项 L3b。
如果两个都过，但只在舒琴和 BTC/ETH/SOL 上过，只能标记为交易员/品种专属候选。

## 5. Telegram GramJS 重拉发布时间评估

### 5.1 可行性结论
结论：技术上可行，证据上必须先做小样本探查，权限上只能做只读 API 调用，不能在当前方案中假定 watcher 已具备可复用 GramJS 会话。
合并 review 将“按 channel+message id 重拉原消息拿 date”列为 M0 第一项探查，见 `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:15-26` 和 `:74-79`。
Telegram 消息如果仍存在、频道实体可解析、会话账号仍有读取权限，按频道和消息 ID 查询通常可以取得服务端消息对象的 `date`。
当前仓库读取到的 Python collector 直接从 incoming message 的 `date` 生成 `source_received_at`，见 `services/telegram-watcher/telegram_watcher/collector.py:64-89`。
生产 feeder 则固定 `source_version=v1`，把 watcher row 的 `received_at` 写入 `source_received_at`，且 raw payload 没有原始 Telegram date，见 `scripts/hermes_signal_feeder.py:707-745`。
这说明“重新拉取”可以补一条研究证据，但不能修正生产 raw 的历史时钟。

### 5.2 只读、会话和限流风险
GramJS 客户端库本身不是安全边界；调用方可以拥有发送能力。
因此“只读 Telegram API”必须由代码审查、允许的方法集合、网络策略和审计共同保证，而不能只靠任务名称。
重拉程序只允许使用按 ID 读取消息和实体解析的方法，禁止 send、forward、edit、delete、mark-read 等副作用调用。
不得直接让研究程序与 watcher 生产进程共用一个正在使用的 session 文件并发写入。
会话文件可能有锁、更新、认证状态刷新或连接竞争；即使 GramJS 允许多个连接，也不能把未验证行为带入生产 watcher。
优先方案是由用户授权后复制会话材料到隔离的只读运行目录，运行程序使用独立进程、独立日志和短时生命周期。
如果会话材料不能安全复制，则使用专门的只读研究账号；但频道访问权限、隐私政策和 Telegram 登录验证必须由用户明确批准。
批量调用会遇到 FloodWait、网络抖动、实体解析失败、频道权限变化和已删除消息。
任务必须按小批量、固定间隔、指数退避执行，遇到服务端限流立即停止该批次，记录 `retry_after`，不绕过限制。
不能因为一部分消息成功就把整个时间字段列标成完整。
编辑消息、删除消息和频道迁移会造成“当前可见对象”不等于当时看到的第一版。
重拉结果必须保存消息 ID、返回 date、edit_date、当前文本哈希、当前媒体哈希、查询时间、会话身份标识的不可逆摘要和失败原因。
不保存 session 字符串、API hash、手机号、访问 token 或无关聊天内容到研究 manifest。

### 5.3 逐步探查协议
第一批只选 20 个已执行计划，覆盖五个频道、不同月份、至少一个 operator synthetic 对照和一个已知补发风险样本。
第二批只选 10 个可读但不进入研究 cohort 的样本，用于测量删除、权限和限流失败。
每个样本先用数据库的 `channel_id + source_message_id` 建立候选键，再调用读取接口。
成功时比较 Telegram `date`、数据库 `source_received_at`、Hermes `created_at` 和 execution 时间。
如果 Telegram `date` 与原始 collector 语义一致，记录为 `A_recovered_from_telegram`。
如果只能取得当前编辑版本，记录为 `A_current_message_not_original_version`，不能伪装成第一版证据。
如果查不到消息，记录为 `unrecoverable_deleted_or_inaccessible`，不回退成 A 级。
如果只取到 watcher 本地 SQLite 的原始 date，记录为 `B_watcher_archive`，并保存 SQLite 文件哈希和行主键。
如果只能使用 `source_received_at`，记录为 `C_ingest_time_only`，该样本不得进入无前视收益 cohort。
探查的成功率按频道、月份、消息类型和失败原因分层，不只报总成功数。

### 5.4 替代方案和优先级
首选是 Telegram 重拉，但只作为历史证据恢复，不改变生产库。
第二选择是查找 watcher SQLite 或备份中是否已经存了 `telegram_messages` 的原始 date；现有 feeder 明确依赖 watcher row 和 message id，但这不等于生产行已保存 date，必须实测。
第三选择是从历史导出、频道管理员归档或用户提供的原始消息包补证据，并将其标成外部归档来源。
第四选择是从 2026-09-07 之后修复新入口，确保未来 payload 同时保存 `telegram_published_at` 和 `source_received_at`；这是未来数据质量修复，不补历史。
第五选择是只用事件相对顺序而不用绝对发布时间；这只能支持有限的顺序性测试，不能替代决策时间回测。
如果 A 级恢复率低于 80%，不应硬凑 M3-P；先报告缺失分层，再决定是否继续收集新鲜数据。

### 5.5 对生产安全的明确限制
本轮不执行 Telegram 登录、重拉、数据库导出或生产命令。
即使未来获得用户授权，重拉程序也不得调用 watcher 的消息处理、ingress、Hermes、operator API 或任何写路径。
它不得重新投递旧消息，不得刷新 `source_received_at`，不得触发 Hermes 重新判断，不得产生 intent。
如果会话占用、网络失败或 API 限流无法排除，替代方案是将历史样本排除出收益 cohort，而不是放宽时间门槛。

## 6. M0 第一周可直接派发的任务书
以下任务书只描述未来执行内容，本轮不派发、不执行。
每个任务独占输出范围；生产访问必须只读；任何需要凭据、部署、写库或 Telegram 会话的动作都要在执行前单独取得用户授权。

### M0-T1：生产证据只读导出
**目标**
建立可重放的最小研究导出，回答每条候选开仓计划的来源、结构、批准、intent、机器人订单和当前结局状态。
**范围**
只读查询 `raw_messages`、`media_assets`、`message_processing_runs`、`context_snapshots`、`hermes_decisions`、`risk_decisions`、`trade_intents`、`execution_events`、`audit_events` 和 `trade_outcomes`。
按稳定主键导出 JSONL，并生成字段覆盖矩阵、排除清单和 EvidenceManifest。
包含 operator synthetic、replay、backfill、失败、未批准、未成交和仍持仓记录，但分别标记来源类型。
**约束**
禁止运行 `scripts/analysis/trade_outcomes.py` 的生产 main，因为它会 upsert 并删除 stale outcome，见 `scripts/analysis/trade_outcomes.py:442-475` 和 `:527-557`。
禁止新增表、视图、迁移、生产写入和修改原始时间字段。
机器人订单只按 `^B[0-9a-f]{32}[0-9]{2}$` 识别；`aos_`、`stToAg_` 和其他手动单必须排除且不报警。
**验收**
至少随机抽取 20 个已执行计划，逐层追到 raw、Hermes、risk、intent、client order、venue order/trade 和退出或右删失状态。
每个断链必须归类为 `missing_source`、`ambiguous_ownership`、`mixed_manual_position`、`missing_fill`、`unclosed`、`missing_media`、`missing_time` 或明确的新类型。
同一计划多 intent、多 tranche、多管理动作不得被计为多个开仓样本。
导出 manifest 的输入水位、查询版本、字段列表和文件哈希完整。
**验证**
用只读数据库账号执行固定 SQL。
对 JSONL 做主键唯一性、外键关系、机器人订单正则、时间单调性和 manifest 哈希检查。
随机人工复核 20 条，检查数据库数值与导出逐字段一致。
验证命令和账号信息写入任务日志，不写入凭据。
**输出**
`seed-export.jsonl`、`seed-export.manifest.json`、`field-coverage.csv`、`broken-links.jsonl` 和 `m0-t1-verification.txt`。
输出中明确可进入 M3-E、M3-R、M3-P 的数量，不足项不填默认值。

### M0-T2：发布时间双时钟与 GramJS 可行性探查
**目标**
确定历史消息能否按 channel+message id 恢复 Telegram 服务端 `date`，并量化 A/B/C 级时间证据覆盖率。
**范围**
从 T1 取 20 个已执行计划和 10 个失败样本，覆盖频道、月份、补发风险、编辑消息和不同消息类型。
先检查原始 JSON、watcher SQLite 及其备份是否已有 date，再对经用户授权的隔离会话执行只读查询。
比较 `telegram_date`、`source_received_at`、`hermes_decision_at`、`intent_created_at` 和第一笔 execution event。
**约束**
本任务不得连接生产 watcher 的活动 session 文件，不得调用发送、转发、编辑、删除、标记已读、ingress、Hermes 或 operator API。
会话、API hash、手机号和 token 不进入文件、日志或 manifest。
遇到 FloodWait、权限失败、删除消息或实体解析失败立即记录并停止该批次，不重试绕过限流。
恢复到的当前编辑版不能标成原始第一版。
**验收**
每个样本都有 `time_evidence_level`、查询来源、查询时间、消息版本、内容哈希和失败原因。
输出 A 级 Telegram 恢复率、B 级 watcher archive 率、C 级 ingest-only 率，均按频道和月份分层。
能够明确回答“是否足以让至少 120 个样本进入 M3-R”和“是否有希望达到 200 个 M3-P 样本”。
任何无法证明的时间不进入可交易 cohort。
**验证**
对成功样本做 channel id、message id、date 类型、时区和内容哈希一致性检查。
对失败样本人工抽查服务端错误和本地缺失原因。
复核运行日志无写 API 方法、无 session 明文、无重复投递。
**输出**
`time-recovery-sample.jsonl`、`time-coverage-report.md`、`telegram-readonly-audit.json` 和 `m0-t2-verification.txt`。
若未获会话授权，输出“未执行 API 探查”和本地可得替代证据，不得把未执行写成失败。

### M0-T3：执行版本、契约和所有权快照
**目标**
冻结 H-live 实际执行语义、v1.1 目标语义、wire 契约状态、节点识别路径和账户/计划所有权现状。
**范围**
读取 `read_api.py` 的实际展开分支、`decision_gateway/zone_ladder.py` 的纯函数、approved intent JSON schema、节点 planner/恢复逻辑、频道路由、intent、execution event 和机器人订单归属。
列出 market、limit、zone、zone_ladder、单档回退、追入、stale、TP1 撤挂和 TTL 的字段差异。
**约束**
只读代码和只读数据库。
禁止修改 schema、网关、节点、路由、配置、生产订单或账户状态。
不得因节点代码能识别 `zone_ladder` 就把它标成契约已批准。
不得使用手动订单推导机器人计划，不得跨账户合并同币种仓位。
**验收**
每个 plan 字段都有来源、生产实际状态、目标状态、契约枚举和节点预期。
至少列出 60 个边界夹具候选，其中包括 short 镜像、区内、穿区、窄区、零数量、步进、TP1、击穿、TTL、重启恢复和 β 第二腿。
输出单独的 `blocked` 列，任何 wire 不一致不得 silent fallback。
账户路由和未平计划均带快照时间、来源和所有权置信度。
**验证**
用静态搜索确认 `expand_zone_to_plan` 的调用者和线上 `_execution_order_plan` 的实际调用链。
用 schema loader 验证 `order_plan.type` 枚举。
随机抽取机器人 clientOrderId，反解 intent 并核对账户、entry ref、symbol 和管理边。
**输出**
`execution-version-matrix.md`、`wire-diff.json`、`ownership-route-snapshot.json`、`boundary-fixture-index.json` 和 `m0-t3-verification.txt`。

### M0-T4：K 线覆盖和前视风险清单
**目标**
确认所有候选 symbol 的 Binance UM 1m 数据覆盖、缺口、上市起点、时间单位和信号前 ATR 可计算性。
**范围**
使用已知可达的 `data.binance.vision` 作为优先来源，按 T1 的候选 symbol 和发布时间范围生成缓存 manifest。
覆盖信号前 lookback、信号到失效/终止窗口、分钟边界、交易品种映射和缺失清单。
比较现有 `zone_penetration_stats.py` 的诊断口径与研究需要的截断口径。
**约束**
不把现有统计脚本直接当收益引擎；其 ATR 回退和窗口触价问题已在合并 review C5 记录，见 `docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:22-25`。
禁止用现货、股票日线或未来 K 线替代缺失的永续数据。
分钟内发帖不能使用该分钟尚未闭合的完整 high/low 判定入场。
缓存失败必须显式记录，不得静默跳过。
**验收**
每个候选样本都有 `kline_coverage_level`、文件哈希、缺口、重复、时间单位、symbol 映射和可用起点。
至少验证 20 个已执行计划在 decision_time 前有足够 lookback，且没有使用信号后的 K 线计算 ATR 或特征。
缺口样本自动进入 `excluded_from_m3_r`，不删除原始记录。
输出保守、乐观和同 bar 歧义的研究配置。
**验证**
检查 K 线时间严格递增、无重复、无未来行、OHLC 合法、品种映射一致。
用固定样例复核 ATR 输入只包含 signal_time 之前的闭合 bar。
对同 bar 同时触发 SL/TP 的样本验证上下界均被保存。
**输出**
`kline-coverage.jsonl`、`kline-cache.manifest.json`、`lookahead-risk-register.md` 和 `m0-t4-verification.txt`。

## 7. 路线调整后的 M0-M5
M0 只做证据盘点、时间探查、执行版本冻结、路由快照和 K 线覆盖，不做收益排名。
M1 做 300 条连续消息三层标注，至少 100 条开仓和 50 条管理，并登记交易员自述假设；原 30 条 bench 只作兼容回归。
M2 做无副作用内核、两套 ladder 差异、wire/节点一致性和至少 60 个边界夹具。
M3 分为 M3-E、M3-R、M3-P；不足时交工程或探索性报告，不降低 M3-P。
M4 做零凭据在线 shadow，独立存储、独立游标、虚拟账本和 4 周新鲜候选；不得写 intent、outbox 或节点。
M5 只有在 M0-M4、用户明示授权、d 账户所有权和部署门禁均通过后，才讨论单账户 canary。
RESUME、开闸、扩大账户或品种范围始终只能由用户明确指令触发。
任何 HALT 安全方向可按既有铁律处理，但研究结果、shadow 达标或策略收益不能自动恢复交易。
部署门禁必须在停节点之前完成；门禁失败保持旧版本，不得把舰队留在停机态，见 `AGENTS.md:9`。

## 8. 最终接受和保留异议
我接受合并 review 对双 ladder、wire 枚举、双时钟、危险 outcome 脚本、前视统计、Hermes 来源、bench 弱校验、shadow 零凭据的全部修正。
我接受五层命名、只读 JSONL、严格回测器、离线风控预览、先一致性再 canary、Hermes 建议通路、虚拟配对、gold 扩容和 L3a 锁箱。
我不接受把 `≥200/60/30` 放在 M3 的唯一入口位置；替代是 M3-E/M3-R/M3-P 三层门，200/60/30 只保留为收益晋级门。
我不接受把 GramJS 重拉描述成“可直接补齐发布时间”；替代是 20+10 小样本、隔离会话、只读方法白名单、FloodWait 立即停批和 A/B/C 分级。
我不接受把交易员分析帖当作真实方法标签；替代是把它作为带原文哈希的假设来源，并经过形式化、锁箱、时间外评估和多重检验。
我不接受把 L3a 两关通过自动解释为 L3b 可上线；替代是将其作为候选规则证据，另行通过完整市场机会集、独立决策主体和用户授权。
我不接受用 10 行 `trade_outcomes` 支撑策略收益；替代是 K 线重建、纯机器人 episode 对账、混合 episode 隔离和右删失。
在这些限制下，第二轮方案的成功不是产出一个看起来盈利的算法，而是把“可复现”“可解释”“可归属”“可验证”和“可授权”分别证明，证明不了的部分明确留在缺口账本。
