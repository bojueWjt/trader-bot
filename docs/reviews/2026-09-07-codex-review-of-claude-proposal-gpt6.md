# GPT-6 独立对抗 Review：频道执行计划 → 量化算法

审阅对象：`docs/plans/2026-09-06-channel-seed-to-quant-claude-proposal.md`  
审阅日期：2026-09-07  
审阅立场：独立审稿人；不把方案中的生产查询、门槛数字或“已实现”表述当作事实，除非能由仓库代码、schema、脚本或可复现数据链证明。

## 结论摘要

方案可以保留为研究路线图，但不能作为 M0 开工依据。核心原因不是“还缺一个回测器”，而是方案尚未冻结三条事实边界：

1. 什么是可观测的信号事件、什么是 Hermes 解析、什么是风险/执行状态；
2. 什么是“交易员本来会做”的反事实，以及在缺少盘口、发布时间和完整订单生命周期时能否识别它；
3. 哪些研究输出仅供描述，哪些输出会改变仓位、否决开仓或形成自主触发权。

方案 §0 把 L0/L1 说成“确定能做、且立即有产品价值”，但 §1 自己已经列出发布时间不可靠、解析非真值、结局物化严重不足三项前提缺口。这是内部不一致，不是单纯的工期问题。

## 必修

### 必修-01：样本单位仍未被方案定义为可去重、可审计的事件

**证据：** 方案 §1（L12-L23）直接把 `open_position` 349、`rejected + expired` 218 当作样本；§3.1 计划生成一行 `signal_seeds`。仓库 schema 将 `hermes_decisions`、`risk_decisions`、`trade_intents` 分成不同事实层，见 `db/migrations/0001_canonical_schema.up.sql:165-262`。

**裁定：确认前审必修-01。** 同一个频道消息可产生多条解析决策、多个账户意向、管理动作和重复重放；状态不是机会，也不是标签。方案没有规定去重键、事件版本、重放关系、人工消息与频道消息的排除规则，因此其“按频道优化”可能只是按入库路径计数。

**具体修法：** M0 先产出不可变 `PlanEvent` 清单：`source_message_id + entry_ref + parser_revision` 为候选身份，另存 `decision_id`、`intent_id`、账户、side、symbol、动作和状态。重复、编辑、重放、手工 operator 事件分层，不删除原事实；只有通过规则分类且可回溯的事件进入估计。

### 必修-02：发布时间恢复假设与 canonical ingress 不符

**证据：** 方案 §1（L25-L28）、§3.1（L76-L85）假设优先从 `raw_payload` 取 Telegram `date`。但 `scripts/hermes_signal_feeder.py:707-746` 的 `canonical_ingress_payload()` 写入的是 `source_received_at` 等 ingress 字段；事故计划 `docs/plans/2026-09-05-missed-signals-remediation-plan.md:44-45` 明确记录 watcher 不保存 `entry.date`。

**裁定：确认前审必修-02。** 历史补发时 received time 不能代表 published time；方案 §9 的“A 级 ≥80%”仍是未验证门槛，不是当前资产事实。没有发布时间就不能做严格的信号时点指标、ATR、延迟和可成交性估计。

**具体修法：** 历史事件分 `telegram_published_at_verified / inferred / unavailable`，不可验证样本不得混入主结果；先为 watcher→feeder→canonical schema 增加并贯穿 `telegram_published_at`、消息 edit/version 和时区语义，再重新统计覆盖率。任何回退到 `source_received_at` 必须在报告中标记为滞后偏差样本。

### 必修-03：L3a 的 K 线标签和负样本 universe 仍不可用

**证据：** 方案 §2.1（L45-L62）把每根 15m/1h K 线压成 `+1/-1/0`，并将几十万个未出手时段视为负样本；`eval/v3_trader_signal_bench/dataset.json` 是 Hermes 动作解析 gold，不是市场机会真值。

**裁定：确认前审必修-03。** 一根 K 线可以含多个 symbol、方向、编辑和管理动作；把事件压成 bar 会丢失顺序与 at-risk 时间。全市场负样本会学习频道覆盖率，事件采样又改变先验，二者都不能直接解释 production precision。

**具体修法：** 正样本保持事件粒度；负样本先定义为交易员可见、品种可交易、数据完整且未发信号的 opportunity set，并记录采样概率做 IPW。报告按频道、symbol、日期和 episode 阻塞切分，同时报告 coverage、precision、calibration，不只报告 recall。

### 必修-04：L3b 两关阈值没有可审计的统计含义

**证据：** 方案 §2.1（L53-L60）规定留出月份召回 ≥60%、自发信号 ≤3 倍，以及 OOS 期望 R 的 95% CI 下界 >0。

**裁定：确认前审必修-04。** 没有基线、损失函数、最小 coverage、独立 holdout、聚类单位和选择校正，60%/3 倍/CI>0 只是拍脑袋门槛。尤其“规则搜索后再报 OOS CI”不能把候选选择当作未发生。

**具体修法：** M0 预注册基线、主指标、最小 coverage、资金/回撤限制和“不可判定”出口；外层时间 walk-forward，内层调参，最终 holdout 永不触碰；按日或 episode block bootstrap，并在候选全量结果中报告选择过程。

### 必修-05：episode 归属不能以 nullable `target_position_id` 为主链

**证据：** 方案 §3.1（L74-L80）优先使用 `target_position_id`，缺失时用 symbol/side/时间窗兜底；字段在 `db/migrations/0001_canonical_schema.up.sql:177,242` 可为空。风险层更新动作还要求目标仓位唯一，见 `services/control-plane/risk/governor.py:109-122`。现有结局脚本以机器人 fill stream 和净仓回到 flat 切 episode，见 `scripts/analysis/trade_outcomes.py:230-348`。

**裁定：确认前审必修-05。** 时间窗兜底在连续开仓、部分平仓、hedge mode、同品种多频道并发时会制造隐蔽错配。

**具体修法：** 主事实改为 `(account, instrument, position_side)` 的 fill/position ledger，加 entry/management intent 链；`target_position_id` 只做一致性校验 hint。多候选、跨 episode 或未闭仓统一 `unresolved`，不得强行归因。

### 必修-06：OHLC 模型不能声称复现 Binance 执行

**证据：** 方案 §4.1（L102-L112）以延迟后首根 K 线 open、`low < price` 和同柱 SL 优先撮合。线上展开读取 Binance mark price，见 `services/control-plane/api/read_api.py:701-798`；普通 order plan 的 market/limit/TIF 语义在 `services/control-plane/api/read_api.py:605-698`，节点还区分 order type、TIF、post-only 和触发保护单。

**裁定：确认前审必修-06。** 1m OHLC 没有 bid/ask、队列、成交量、提交确认延迟、过滤器拒单、IOC residual 或同柱路径，不能把价格触碰当成交。SL 优先只是悲观/保守情景，不是真实顺序。

**具体修法：** 命名为“OHLC 反事实模型”，显式记录 accepted/rejected/working/partial/filled/canceled、mark/last 基准、延迟、spread proxy、tick/step/min-notional、post-only 和 IOC；同柱触发给上下界。M1 先完成交易所语义合同和单测，再做政策搜索。

### 必修-07：合并 review 对生产事件量的反驳不成立为“保真度底子够”

**证据：** 方案 §4.2（L114-L124）只对“实盘成交的信号”要求中位价差 <0.1%、成交一致率 ≥90%；合并 review §9（`docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md:116-125`）称生产有 411 fills、118 intents、657 reject+cancel。`trade_outcomes` 仍由 `db/migrations/0006_trade_outcomes.up.sql:1-28` 的物化结局表约束，而不是自动证明每个订单生命周期完整。

**裁定：部分确认前审必修-07，但反驳其关键前提。** 411 个 `OrderFilled` 足以说明可做事件校准，不足以证明回测器可靠：它们可能是多个 fill/账户/管理订单，不能替代完整的 signal→intent→order→position episode。118 intents 与 657 reject/cancel 也不是同一分母；若不证明 join 完整性、时间窗、机器人 clientOrderId 和未观测状态，数字不能构成样本量或保真度证据。合并 review 只修正“trade_outcomes=10 是全部执行证据”的说法，没有修正“成交子集选择偏差”和状态分母不一致。

**具体修法：** 先生成订单生命周期审计表，按机器人 `clientOrderId`、intent、账户、symbol、position side 和事件时间串联；量化每种状态的观测覆盖率、重复率、孤儿事件和 join failure。保真度分别报告 fill probability calibration、价格误差分位数、未成交/拒单分类、按 episode/day block CI；在覆盖率未达预设值时只能出描述报告。

### 必修-08：网格搜索后的 OOS CI 仍有多重检验

**证据：** 方案 §5.2（L144-L154）搜索 ladder、TTL、TP 分配、移损规则后，§9 用“回测优于基线”作为里程碑；方案没有列出候选全量结果或选择流程。

**裁定：确认前审必修-08。** “留出月份”若被反复查看、按频道挑优或按结果改参数，就不再是 holdout；研究者自由选择频道/政策/样本过滤也会产生隐性多重检验。

**具体修法：** 预注册搜索空间和主结果；保留每个候选及失败原因；采用 nested walk-forward 和一次性最终 holdout，必要时用 block permutation/选择校正。样本不足时只发布描述性结论。

### 必修-09：灰度不是“每步都是加法”，且风险预算写入越权

**证据：** 方案 §6.3（L172-L180）把仓位乘子送入 `risk_decisions.risk_budget`，并称每步可秒回滚；AGENTS.md 铁律要求 RESUME/开闸只能由用户明确指令，机器人订单与手工订单隔离，部署/门禁失败不得留停机态。

**裁定：确认前审必修-09。** 调大乘子是增加风险，不是加法；停新意向不能撤销已在途订单或处理保护单。`risk_budget` 还是授权链的一部分，不能由 shadow/模型隐式改写。M4 即使有用户开闸，也不能把回滚定义成单一配置反转。

**具体修法：** 将 shadow、建议、人工批准、执行四态分开；乘子仅通过明确的 operator command、request_id、reason、scope、release_id 和审计链生效。回滚定义为停新意向、只撤机器人自有挂单、核对在途/保护单/仓位，并逐账户确认；绝不触碰 `aos_`、`stToAg_` 等手工订单。

### 必修-10：CUSUM 参数缺失且“降乘子”仍是隐性自动风控

**证据：** 方案 §7（L181-L185）规定每频道滚动 30 笔期望 R 做 CUSUM，跌破阈值告警并建议降乘子；没有定义基线均值、漂移、方差、warm-up、cooldown、跨频道错误率或多重告警控制。

**裁定：确认前审必修-10。** 30 笔不是稳定的有效样本量，频道间共享市场 regime 会造成相关误报；“建议降乘子”如果被自动接线就是未授权自动风控。AGENTS.md 允许安全方向 HALT 自动化，不允许 AI 自行 RESUME，也不等于允许模型自动改仓位。

**具体修法：** 第一阶段只做 alert-only；预注册基线、阈值、warm-up/cooldown、缺失和删失处理，并按 episode/day block 重采样估误报。任何乘子或状态变更都走人工 operator command；真正的安全 HALT 另走现有审计和节点身份链。

## 应改

### 应改-01：`signal_seeds` 宽表会混淆一对多事实

**证据：** 方案 §3.1（L72-L86）要求一行包含 signal、decision、intent、episode、OHLC 和结果。一个 signal 可有多个 Hermes decision、账户 intent、fills、TP/SL 订单。

**修法：** 采用不可变事件表 + 关联表/派生宽表；宽表只作为查询产物，不能作为事实源。每个派生字段保留来源 id、as-of 时间和构建版本。

### 应改-02：30 条解析 gold 不支持“至少三分之一样本结构真值”

**证据：** 方案 §3.2（L88-L90）要求扩到 100 个开仓/50 个管理，并暗示足以让至少三分之一结构参数是真值；bench 目录的 `dataset.json` 与 `results.json` 只验证解析动作。

**修法：** gold 按频道、动作、歧义类型和时间分层抽样；报告字段级 agreement、置信区间和 unresolved 比例，不把 gold 数量等同于生产覆盖率。

### 应改-03：K 线覆盖承诺没有合约生命周期与版本化数据合同

**证据：** 方案 §3.3（L92-L100）承诺 44 个品种 1m 覆盖；没有规定 Binance 合约上线/下线、symbol rename、mark/index source、缺 bar、时区、API 修订和修复数据版本。

**修法：** 每个回测样本带 instrument metadata、listing/delisting 边界、数据源、缺口、下载时间和 checksum；ATR/指标只读决策时刻前闭合 bar，缺口返回不可判定而非插值成交。

### 应改-04：复用 `trade_outcomes` 字段会掩盖模拟/实盘语义差异

**证据：** 方案 §4.1（L102-L112）称输出复用 `trade_outcomes` 字段；`db/migrations/0006_trade_outcomes.up.sql:1-28` 的表以真实 `intent_id/account_id` 唯一结局为中心。

**修法：** 新建反事实结果 schema，明确 `simulation_run_id`、模型版本、假设、状态、删失和上下界；只有通过 adapter 的字段映射才能与真实 outcome 对比，禁止伪装成 production outcome。

### 应改-05：保护单和 reduce-only 约束没有成为模拟器账本

**证据：** 方案 §4.1 只列入场/SL/TP 价格与 R；§4.1/§5.2 未规定保护单数量、reduce-only、部分成交、撤单竞态。线上 `read_api.py:8042-8148` 与 operator order 路径会处理目标仓位及保护语义。

**修法：** 以订单级保护单 ledger 模拟每个 TP/SL 的数量、剩余量、触发后撤销竞态和仓位上限；保护单不得在没有对应自有仓位时扩仓。

### 应改-06：四账户 A/B 设计不是天然可比

**证据：** 方案 §6.3（L177-L180）称同一信号在 control/treatment 两组执行即天然 A/B，并只用 R 比较。

**修法：** 先证明账户余额、杠杆、手续费、延迟、symbol 可用性和仓位隔离相同；以信号事件为随机化单位，处理同一账户跨组污染和 treatment 改变后续市场暴露。否则只做 shadow paired replay，不宣称因果 A/B。

### 应改-07：累计 50 条新鲜信号没有有效样本量和停止规则

**证据：** 方案 §7（L181-L185）以累计 50 条触发重跑 M2，但没有按频道/策略/状态拆分，也没有最小 fill、episode、覆盖率或缺失阈值。

**修法：** 把触发器改为证据闸门：有效 episode 数、状态覆盖率、发布时间完整率、每频道最小样本、数据新鲜度和停止/回退规则共同满足才重跑。

### 应改-08：里程碑工期排除了真正的门禁与人工依赖

**证据：** 方案 §9（L196-L207）给 M0 一周、M3 四周墙钟，并把 gold 人工另计；没有把 watcher schema、历史 provenance、只读生产导出、部署审批、备份恢复和验收算入关键路径。

**修法：** 用证据闸门替代墙钟承诺：provenance、episode/gold、交易所语义合同、模拟器单测、生产事件校准、研究协议、零凭据 shadow、最后才是 canary。未满足即停止，不以日期推动上线。

## 建议

### 建议-01：把“立即有产品价值”改成待证假设

**证据：** 方案 §0（L9）、§2（L39-L40）称 L0/L1 确定有立即价值；但 §8（L188-L194）承认 09-05 的 HALT、inbox、第二腿和 watcher 断流可能主导偏差。

**修法：** 先列可证伪收益假设：在相同事件、相同约束和相同成本下，政策改善 fill/R/回撤中的哪一项；若系统拒绝占比超过阈值，收益结论暂停。

### 建议-02：特征必须带 as-of、版本和缺失语义

**证据：** 方案 §2.1（L45-L52）列频道近期表现、同日密度、ATR、趋势一致性，但未规定窗口截止、是否包含当前事件、指标版本和缺 bar 行为。

**修法：** 每个 feature 存 `as_of`、lookback、source revision、timezone 和 missing reason；近期表现只能用当时已结束的 episode，不能读未来结局回填。

### 建议-03：shadow 对比必须区分“算法不做”和“系统没执行”

**证据：** 方案 §6.2（L168-L170）只要求比较实际计划、shadow 计划、实际/模拟结局；§8（L188-L194）又承认基础设施异常。

**修法：** 漏斗分为 `not_selected / parser_unresolved / risk_rejected / intent_expired / order_rejected / no_fill / partial / filled / position_unresolved`，并绑定证据事件。否则 shadow 会把系统故障归因给策略。

### 建议-04：影子层必须零凭据，不能调用 operator API

**证据：** 方案 §6.1（L160-L166）仍写“调 `POST /v1/operator/orders` 的 `dry_run=True`”；虽然文件顶部声明该点已被推翻，但正文仍是可执行的错误指令。`read_api.py:7524` 表明 dry-run 仍位于 risk_admin operator 路径。

**修法：** 影子层只消费脱敏 canonical event，调用离线、无凭据的纯校验库或冻结的 semantics adapter；不得持有 `RISK_ADMIN_TOKEN`，不得访问 operator endpoint。该项虽为已知推翻点，仍必须在 M0 开工前从正文删除，防止误派发。

## 前一份 review 十条必修逐条裁定

| 项目 | GPT-6 裁定 | 依据与差异 |
|---|---|---|
| 必修-01 | 确认 | 状态计数不是事件；本 review 进一步要求 parser revision、重放和手工路径分层。 |
| 必修-02 | 确认 | canonical ingress 没有 Telegram published date；A 级覆盖率仍未测得。 |
| 必修-03 | 确认 | K 线压缩与负样本 universe 均未定义；增加 IPW 和机会集要求。 |
| 必修-04 | 确认 | 两关阈值没有预注册统计语义；增加候选选择过程审计。 |
| 必修-05 | 确认 | nullable target id 不能作事实主链；强调 position side 和 unresolved。 |
| 必修-06 | 确认 | OHLC 不是 Binance 执行仿真；要求状态机和语义合同。 |
| 必修-07 | 部分确认 | 前审指出成交子集选择偏差成立；合并 review 的 411/118/657 只能证明存在事件，不能证明分母、join 完整性或保真度。 |
| 必修-08 | 确认 | 网格搜索后普通 OOS CI 有选择偏差；要求 nested walk-forward 与最终 holdout。 |
| 必修-09 | 确认 | 灰度不是全加法，risk budget 改写需授权；补充在途和手工订单隔离。 |
| 必修-10 | 确认 | CUSUM 参数、多频道误报和自动降乘子均未闭合；只允许 alert-only 起步。 |

## 新增发现

相对前一份 review，本次新增或加重了四点：

1. **正文自相矛盾是操作风险。** 文件顶注已承认 §2.2 和 §6.1 被推翻，但 §6.1 仍给出 operator dry-run 指令；文档若被按段落派发，会重新引入零凭据禁忌。
2. **订单量数字没有共同分母。** 411 fills、118 intents、657 reject/cancel 横跨不同实体和状态，不能直接拼成“校准底子”；必须先证明 clientOrderId→intent→episode 的完整 join。
3. **事实宽表会把一对多折叠成伪确定性。** 这不仅是数据库建模问题，还会把多个账户、多个保护单和重复 fill 误当成一条训练样本。
4. **L0/L1 的“立即价值”未被证伪条件约束。** 如果主要损失来自 watcher 断流、HALT 或拒单，优化 ladder 可能只是在错误入口上精细化，不能承诺收益改善。

## M0 开工裁定

**裁定：不能直接作为 M0 开工依据。** 可以把它作为路线输入，但开工必须先满足以下只读证据闸门：

- **Provenance：** 事件身份、发布时间分级、消息版本、手工/频道来源、parser revision 可追溯；不可验证样本隔离。
- **Episode/gold：** fill ledger、position side、intent 链和 unresolved 规则通过人工抽样；gold 报告字段级 agreement，不以数量代替覆盖率。
- **Execution contract：** 冻结 mark/last、order type、TIF、post-only、filters、延迟、partial、保护单和同柱上下界语义；模拟结果与真实 outcome 分离存储。
- **Event calibration：** 证明机器人 clientOrderId 的 lifecycle join，覆盖 accepted/rejected/working/partial/filled/canceled，并给出状态分母、孤儿率、缺失率和 block CI。
- **Research protocol：** 预注册机会集、特征 as-of、搜索空间、主指标、最终 holdout、停止规则和“不可判定”出口。
- **Shadow boundary：** 只读、脱敏、零凭据；不得调用 operator API，不得写 risk budget，不得触发 RESUME 或任何开闸动作。

满足以上条件后，M0 只能先做数据资产化和离线校验；M1 回测器仍应先作为 OHLC 反事实模型验收，M2 才能研究政策，M3 之后才讨论用户明确授权下的灰度。任何门禁失败都应中止该阶段并保持生产版本运行，符合 AGENTS.md 的停机与授权铁律。

