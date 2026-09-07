# 对 Claude 方案的对抗式 Review

审查对象：`docs/plans/2026-09-06-channel-seed-to-quant-claude-proposal.md`  
审查日期：2026-09-07  

范围说明：以下只审查除方案已自认被推翻的两点之外的问题。生产库表格中的数量来自 2026-09-06 只读查询，本审查不复核数字本身，只检查这些数字能否支持方案作出的推断。

## 必修

### [必修-01] §1 把状态计数当成“未执行样本”，样本单位已经错了

**证据：** 方案 §1（L15-L20）把 `rejected + expired = 218` 定义为“有计划没执行”，把 `349` 条 `open_position` 定义为开仓样本；仓库中 `hermes_decisions` 是 Hermes 语义判断（`db/migrations/0001_canonical_schema.up.sql:165-203`），`risk_decisions` 是独立的风控批准结果（`:210-225`），`trade_intents` 才是带有效期和执行计划的意向（`:231-262`），`trade_outcomes` 还是按 `(intent_id, account_id)` 唯一的已物化结局（`db/migrations/0006_trade_outcomes.up.sql:1-28`）。

**问题：** `rejected`/`expired` 可能是管理动作、重复/陈旧消息、缺参数、风控拒绝或过期未被执行的意向，状态本身不能证明“原本可交易但没有成交”。同理，349 条决策不是 349 个独立机会，可能包含重复转发、同一信号多账户记录、相关的同日事件和从未进入可执行风险链的记录。用这些计数直接估计参与率、期望 R 或训练标签，会把拒绝机制和重复记录误当成策略结果。

**修法：** 先定义事件级分析母表：一条原始消息/一个稳定 `source_message_id + entry-ref` 对应一个 signal event；分别保留 `decision_status`、`risk_status`、`intent_status`、`execution_status`、`outcome_status`，并标记 `not_actionable`、`duplicate`、`stale`、`system_blocked`、`user/manual`。只有通过预先定义的“当时可执行且未被系统性故障截断”条件的事件进入策略结果估计；其余作为删失/原因分层，不改名为负样本。

### [必修-02] §1/§3.1 的 `published_at` 追回假设与实际 canonical ingress 不符

**证据：** 方案 §1 L26-L28、§3.1 L76-L85 计划优先从 `raw_payload` 取 Telegram 原始 `date`。但 `scripts/hermes_signal_feeder.py:707-746` 的 `canonical_ingress_payload()` 只写入 `source_received_at`，`raw_payload` 目前包含 watcher 行号、标题、sender 等字段，没有 Telegram `message.date`。事故复核也明确指出 watcher 不存 `entry.date`，见 `docs/plans/2026-09-05-missed-signals-remediation-plan.md:44-45`。

**问题：** 对历史补发消息，`source_received_at` 是入库/发现时间，不是发布时间。把它回退为 `published_at` 会制造错误的决策时点；但把无法确认的样本当作可回测样本，同样会产生不可量化的滞后偏差。方案的“真实发布时间 A 级占比 ≥80%”不是现状事实，而是未验证的门槛假设。

**修法：** 在 M0 先做字段存在率审计，按 `telegram_date_present / watcher_received_only / unknown` 分层，未知时间样本禁止进入带时序结论的训练和回测。先修改 watcher 的 canonical schema 保存 Telegram `date`、编辑版本和抓取时间，再从上线后的新数据开始建立 A 级集；历史数据只能作为低等级描述性资料，不能承诺追回到 80%。

### [必修-03] §2.1 L3a 标签把事件压扁成 K 线，正负标签和负样本集合没有定义

**证据：** 方案 §2.1 L48 规定每根 15m/1h K 线标为 `+1/-1/0`，并称舒琴约 141 个正样本对几十万个负样本；§2.1 L62 只说“按事件采样负样本”。仓库的评测集是解析 gold，不是交易时点真值，`eval/v3_trader_signal_bench/dataset.json` 与 `results.json` 的标签包含 `open`、`open_batch`、`skip`、`partial`、`close` 等动作，不能直接充当“价格环境中的开仓标签”。

**问题：** 同一根 K 线可有多个信号、不同品种、相反方向或重复编辑消息；压成一个三分类标签会丢失事件顺序和动作数量。更严重的是，“几十万个负样本”没有 at-risk universe：是该交易员关注过的品种、全部 Binance 合约，还是每个时刻可交易的品种？若从全市场全时段抽负样本，模型只学到频道覆盖率/品种先验，而不是交易员的出手条件。按事件采样后又改变生产类别先验，PR 曲线不会自动恢复真实 precision。

**修法：** 以信号事件为正样本单位，保留发布时间、symbol、side、entry type、重复/编辑关系；为每个事件定义有限的候选集和观察窗，负样本必须是“交易员当时实际可见且满足风险/数据可用条件但未发信号”的机会。预先固定采样率，用 inverse-probability weighting 或先验校正恢复生产概率，并报告按日期/品种/频道阻塞切分的 precision、recall、校准曲线和事件数。

### [必修-04] §2.1 的“两关”不是有统计意义的立项判据

**证据：** 方案 §2.1 L53-L60 以“留出月份召回 ≥60%、自发信号数 ≤3 倍”和“规则自发信号 OOS 期望 R 的 95% CI 下界 >0”作为 L3b 门槛。

**问题：** 60% 和 3 倍没有给出业务损失、基线或置信区间；在 141 个正样本且按月份/品种聚集时，几个事件就能大幅改变结果。只报告 recall 会允许 precision 很低的规则通过，3 倍上限也不是 false discovery 或资金约束。第二关未说明 CI 的重采样单位、是否包含策略选择、手续费/滑点、部分成交、未闭仓删失和信号间相关性。普通 iid bootstrap 会把同日和同品种相关事件当成独立观测，低估不确定性。

**修法：** 在开工前注册基线规则、主指标和停止规则；按日或 episode 做 block bootstrap，并按 symbol/频道做阻塞 OOS。第一关同时给出 precision、coverage、事件召回和置信区间；第二关使用完全未触碰的最终 holdout，纳入费用、延迟、部分成交和删失，报告 block CI、最大回撤/风险暴露及模型搜索后的选择校正。若样本不足，应输出“不可判定”，不能把阈值通过解释为可自主开仓。

### [必修-05] §3.1 的 episode 归属方法不是仓库当前的事实来源

**证据：** 方案 §3.1 L74-L80 以 `target_position_id` 优先，缺失时用同 symbol/side、时间窗和 `entry-ref` 归属管理事件。可是 `target_position_id` 在 Hermes 决策和 trade intent schema 中都是 nullable（`db/migrations/0001_canonical_schema.up.sql:177,242`），风险层对更新动作要求目标必须存在且唯一（`services/control-plane/risk/governor.py:109-122`）。当前结局构建器实际从机器人订单的 `OrderFilled` 与 `PositionClosed` 事件按 `(account, normalized instrument)` 追踪净数量并在回到 flat 时切 episode（`scripts/analysis/trade_outcomes.py:230-284`），再把第一个带 intent 的 entry fill 作为主归属，未标记 close 事件只按五分钟窗口附着（`:290-348`）。

**问题：** 方案把一个可为空的提示字段提升成主链接，并把“同 symbol/side + 时间窗”当成可接受兜底，会在同品种连续开仓、部分平仓、同一账户多频道、对冲模式多空并存时错配管理动作。错配的 episode 会同时污染训练标签、R、管理策略回放和频道归因；而 `trade_outcomes` 只有 10 行（方案 §1 L19），没有足够结果去人工发现这种错配。

**修法：** 以机器人拥有的 fill stream、账户/品种/position-side 账本和明确的 entry/management intent 链为主事实；`target_position_id` 只作为经过账户、symbol、side、时间一致性验证的 hint。对多个候选或净仓未闭、跨 episode close 的记录返回 `ambiguous/unresolved`，不强行归属；先用人工 gold 核验归属精度，再允许进入政策拟合。特别为 hedge mode 保留 position side，不能只按 `(account, symbol)` 聚合。

### [必修-06] §4.1 的 OHLC 撮合规则不能声称复现 Binance USDT-M 执行

**证据：** 方案 §4.1 L104-L112 把 `p0` 设为延迟后第一根 K 线 open，规定 `low < price` 成交、追入档按下一根 open 加滑点、同一根 K 线 SL 优先。实际线上 zone 展开会读取 Binance mark price 并在 mark 已穿透/进入条件下拒绝或切换路径（`services/control-plane/api/read_api.py:701-798`，尤其 `:739-745`）；普通路径对 market 使用 IOC、limit/zone 默认 GTC（`:605-617`、`:676-698`）。节点 planner 还区分 `MARKET`、`LIMIT`、TIF 和 `post_only`（`services/nautilus-node/strategy/intent_execution_planner.py:202-232,443-487`），止损是带触发价的 STOP order（`:527-571`），止盈是 reduce-only 的触发订单（`:574-640`）。

**问题：** 1m OHLC 没有 bid/ask、队列位置、成交量分配、部分成交、提交/确认延迟或订单被拒事件。`low < price` 只能是人为保守假设，不是 Binance limit order 的成交语义；`low <= price` 的边界也不能解决队列问题。SL/TP 同柱“SL 优先”是区间内路径的不确定性上界选择，不是真实触发顺序。追入“下一根 open”忽略异步提交、mark price 与 last price 的差异、spread、tick/step/min-notional、marketable limit 的价格上限以及 IOC 未成交余量取消。方案还没有要求模拟 post-only 拒单或交易所 filter 拒单，因此回测会系统性高估可执行性。

**修法：** 把回测器命名为“OHLC 反事实模型”，不要称为实盘复现，直到用真实订单状态校准。模拟器至少要输入/记录：订单 accepted/rejected/working/partial/filled/canceled、触发价格基准（mark/last）、提交延迟、spread proxy、tick size、quantity step、min notional、post-only reject、IOC residual cancel 和保护单触发。对同柱多触发给出乐观/悲观上下界或使用更细粒度逐笔/盘口数据；实盘 adapter/testnet 的语义测试必须先于政策优化。

### [必修-07] §4.2 保真度阈值不可验证，也不足以证明策略回测可靠

**证据：** 方案 §4.2 L114-L124 只要求成交价中位偏差 `<0.1%`、成交/不成交一致率 `≥90%`，触发不一致则人工归为“回测器缺陷”或“实盘异常”。

**问题：** 方案 §1 L19 已说明只有 10 个 `trade_outcomes`，而 §4.2 只取“实盘成交的信号”，这会丢掉被拒、撤销、挂而未成和系统故障的订单，形成条件选择偏差。中位数会掩盖尾部价格误差和系统性漏成交；90% 在小样本上没有稳定置信区间，且对“全部未成交”这种退化预测也可能没有意义。人工分类不是可重复验收规则，容易把模拟器的结构性错误贴成实盘异常。

**修法：** 预注册按 order/tranche、symbol、side、order type 和状态分层的指标；对所有已接受/拒绝/挂存/成交事件做校准，不只看 materialized outcomes。报告 fill probability calibration、precision/recall、绝对误差的均值/中位数/95%分位数、partial-fill quantity error、覆盖率和按日 block CI。不可观察的队列位置必须显式标为不可识别并进入区间结果；异常分类用固定规则和证据字段，不能靠逐条主观裁定过门。

### [必修-08] §5.2 网格搜索后再报普通 OOS CI，存在严重多重检验和选择偏差

**证据：** 方案 §5.2 L144-L154 枚举 ladder 权重、追入窗口、TTL、管理规则、TP 分配，声称全网格“几百个组合”，在 306 条带价计划上做 walk-forward + bootstrap，并以 OOS 期望 R CI 下界高于当前政策为判据。

**问题：** “参数少”不等于搜索自由度少；管理规则、TP 分配、TTL 还改变成交和结局定义。按月滚动调参会反复使用同一历史观测，选出赢家之后的 bootstrap CI 不是该选择过程下的置信区间。没有嵌套验证、最终 holdout、multiplicity 控制或完整候选结果表，最终“CI 下界 > baseline”很可能只是数据窥探。306 条还不是 306 个独立 episode，且实际可用于拟合的 verified/closed 子集更少。

**修法：** 先冻结当前政策和唯一主指标；外层按时间做 walk-forward，内层只调参，另留完全未触碰的最终时间 holdout。按 episode/day block bootstrap，使用 pre-registered candidate family 的 FWER/FDR 或把所有候选结果和选择过程完整报告。若无法保证独立 holdout 或有效样本量，M2 只能做描述性报告，不得据此更改线上政策。

### [必修-09] §6.3 的灰度步骤不都是“加法”，且第 2 步越过风险授权边界

**证据：** 方案 §6.3 L172-L180 宣称每步可秒回滚，提出把 L2 乘子写进 `risk_decisions.risk_budget`，再以 `multiplier=0` 否决开仓；第 1 步还称“只改配置，不改开仓权”。实际风险决策会受 live open rollout gate 影响，可能从 approved 转为 `needs_review`/canary-only（`services/control-plane/decision_gateway/gateway.py:306-354`）。operator action 白名单只有 `open_position`、管理动作和 cancel，没有 `add_position`（`services/control-plane/api/read_api.py:5744-5751`）；canary 还要求 armed permit、单账户单 symbol、唯一新鲜 ACTIVE 节点、release、flat 校验、事故检查及单次 open（`read_api.py:7351-7475`），canary open 强制 limit+IOC（`:7663-7687`）。

**问题：** 改 `risk_decisions.risk_budget` 不是普通可逆配置，而是改变风控批准的审计事实和执行权限，需要 schema/写入方/角色/幂等/回放契约。乘子为 0 也不是中性操作：它会改变意向是否进入执行、有效期和拒绝可观测性。四账户 treatment/control 不是天然 A/B：账户余额、持仓、HALT、节点延迟、路由和共享市场冲击不同；同一信号复制到多个账户还会改变总订单流。已提交的挂单、部分成交、保护单和过期 intent 也不能“秒回滚”。

**修法：** 先做 control-plane 拥有的离线 shadow policy flag，不写 `risk_decisions`、不带执行凭据；随后由用户明确授权一个账户/一个节点/一个 symbol 的 canary permit，复用现有 permit、release、ACTIVE、flat、incident 和 notional 门禁。任何开闸、重启、停机和恢复都遵循 `AGENTS.md`：RESUME 只能由用户明确触发，HALT 可自动，部署门禁必须在停节点前完成，绝不能自动恢复或触碰手动订单。回滚定义为停止新意向和撤机器人自有挂单，并单独处理在途成交与保护单，而不是声称瞬时撤销整个政策。

### [必修-10] §7 的 CUSUM 在滚动 30 笔和多频道场景下会误报，并暗含未经授权的自动风控

**证据：** 方案 §7 L181-L186 规定每频道滚动 30 笔期望 R 做 CUSUM，跌破阈值后告警并“建议降乘子”。

**问题：** 没有给出 in-control 均值、方差、reference value、decision interval、warm-up、reset/cooldown 和缺失结局处理。30 笔中可能混有同日相关信号、部分成交、尚未平仓和系统拒单，均值/方差不稳定；频道越多，重复告警概率越高。若阈值从同一历史数据调出，误报率没有可解释含义。更关键的是“降乘子”改变 `risk_budget` 和开仓资格，即使不是自动停频道，仍可能成为未经用户授权的自动风险策略。

**修法：** 第一阶段只做 alert-only：按已闭合、已归属且质量合格的 episode 计算，预先固定基线和阈值，用按日/episode block resampling 校准误报率，设置最小有效样本量、缺失/删失规则、频道级 cooldown 和多告警控制。任何乘子变更必须进入显式人工授权的 operator/config 流程；自动动作仅保留符合铁律的安全 HALT。

## 应改

### [应改-01] §3.1 的“左连一行 signal_seeds”没有解决一对多关系和事实层次

**证据：** 方案 §3.1 L72-L80 以 `hermes_decisions` 为主键左连 raw message、风险、intent、execution 和 outcome；schema 中一个 Hermes decision 关联风险决策，intent 还按账户保存（`db/migrations/0001_canonical_schema.up.sql:210-262`），execution event 另有事件流（`:286-381`）。

**问题：** 直接做宽表会把多个账户、多个 intent、多个 fill、多个保护单笛卡尔相乘，导致计数和 R 被重复展开；把“决策、计划、订单、成交、episode”放在同一行也会让后续脚本误以为它们是同一个粒度。

**修法：** 拆成不可变的 event 表/视图：`signal_event`、`decision_attempt`、`intent`、`order_leg`、`execution_event`、`episode`、`outcome`，每层只用稳定 ID 关联；研究查询明确声明粒度和去重键，禁止用宽表直接聚合收益。

### [应改-02] §3.2 把解析 gold 当作“至少三分之一样本的结构真值”，推断过强

**证据：** 方案 §3.2 L88-L90 要求 gold 扩到 100 条开仓+50 条管理，并称这样可使至少三分之一 `signal_seeds` 的结构参数是真值；现有 bench 明确是 Hermes 解析质量评测，且样本只有 30 条，方案 §1 L20 也承认不覆盖结局。

**问题：** gold 数量不等于覆盖率，除非按频道、动作、entry type、模糊表达和时间分布做概率抽样并和全体样本建立映射。人工确认结构也不等于确认交易员真实意图，更不等于确认实际执行与结局。

**修法：** 预先设计分层抽样和标注协议，报告每字段的 inter-rater agreement、缺失率和抽样权重；把 `parse_verified` 作为质量分层，不把 verified 直接称为交易真值。结局仍必须由执行事件和可追溯的市场数据独立重建。

### [应改-03] §3.3 的 K 线覆盖承诺没有处理合约生命周期和数据可比性

**证据：** 方案 §3.3 L92-L96 要求从最早信号前 30 天到今天覆盖全部 symbol，并引用 `zone_penetration_stats.py:316-343` 的 Binance Vision 1m loader；该脚本对不支持/缺失档案会抛出 `UnsupportedSymbol`/`MissingKlines`（`:25-35`），现有 zone 查询还按 `raw_messages.source_received_at` 排序而非真实发布时间（`:290-313`）。

**问题：** “下载到今天”不等于可用于当时回测：上市前无合约、下架/更名、合约规则变化、时区/时间边界、缺口和压缩档案失败都会改变可用样本。对 MUUSDT 一类标的还要验证当时确实是 Binance USDT-M 合约，不是只按字符串拼接。

**修法：** 建立 symbol metadata/contract availability 快照，按信号时刻验证 listing、tick/step/min-notional 和市场类型；K 线缺口做连续性报告，任何缺口穿过决策/触发/退出窗口的样本标为不可判定，不得静默删除后再统计。

### [应改-04] §4.1 的“复用 trade_outcomes 字段”掩盖了模拟与实盘的语义差异

**证据：** 方案 §4.1 L111-L112 要求模拟器复用 `trade_outcomes.py:71` 的字段口径；数据库结局表的 `realized_pnl`、`fees`、`first_fill_at`、`closed_at` 等字段定义见 `db/migrations/0006_trade_outcomes.up.sql:1-24`，而 `trade_outcomes.py:351-370` 只在有真实首填和闭合时间时加载行情重建。

**问题：** 模拟交易没有真实 realized PnL、交易所 fee、订单状态和闭仓事件，直接复用列名会让下游把反事实数值当成实盘观测。尤其部分成交、未闭仓、系统拒单和手动干预需要 censoring/status，而不是空值。

**修法：** 增加 `observation_type = simulated|observed|hybrid`、`fill_model_version`、`fee_model_version`、`censoring_reason`、`data_quality` 等字段；实盘结局与反事实回测分表或至少强制类型隔离，禁止同一 `r_multiple` 聚合而不分层。

### [应改-05] §4.1 没有把保护单数量和 reduce-only 约束建模

**证据：** 方案 §4.1 L111 允许 `channel_replay` 回放移损/减仓；节点 planner 的止损和 TP 都按当前 position 生成，TP 总量还校验不得超过当前仓位（`services/nautilus-node/strategy/intent_execution_planner.py:527-640`）。现有 skill 明确 partial 后要按剩余仓位重挂保护单（`hermes-profile/skills/trading/v3-trader/SKILL.md:22-25`）。

**问题：** 如果回测把原始信号数量或减仓前数量带入 TP/SL，模拟可以“卖出不存在的仓位”；如果把保护单当成瞬时、完整成交，又会高估管理规则。方案没有规定部分成交后保护单数量、撤旧单竞态、保护单未被接受时的风险。

**修法：** 每个 episode 维护 position quantity ledger；每次 partial fill 后按剩余数量重算 reduce-only 保护单，记录撤旧/重挂的延迟和失败状态。模拟结果必须区分“保护已接受”“保护未接受”“仓位仍开放”，并在没有足够事件数据时保守截断。

### [应改-06] §6.3 的四账户 A/B 设计没有控制干扰和可比性

**证据：** 方案 §6.3 L179 说 a/b 做基线、c/d 做 treatment，或按 signal hash 随机分配，并称同一信号在两组都有账户执行、天然 A/B；仓库交易是四账户独立风险状态，但节点、市场和频道输入并非实验隔离。`AGENTS.md` 还明确用户会在同账户手动交易，机器人订单必须按 `^B[0-9a-f]{32}[0-9]{2}$` 隔离。

**问题：** 账户不是同质实验单元；余额、持仓、risk state、节点延迟、符号可用性和已有机器人订单都会改变成交。把同一信号复制到多账户会增加总下单量，导致执行价格和 fill probability 互相影响；手动仓位还使按账户 R 的比较产生混杂。

**修法：** 把实验单位定义为 signal event，先做不下单 shadow counterfactual；若进入 canary，只允许单账户单节点单 symbol，按 order intent 做配对比较，记录当时账户/节点/仓位/门禁状态。不要把四账户分配称为天然 A/B；至少做 cluster-robust 或 block 分析，并把手动干扰样本排除/单独分层。

### [应改-07] §7 的“累计 50 条新鲜信号后重跑”没有最小有效样本和停止规则

**证据：** 方案 §7 L183、§8 风险 L190 采用 50 条触发重跑；旧 zone 方案也只是把 50 条作为重新评审触发（`docs/plans/zone-ladder-order-plan-v1.md:97-101`），其历史参数警告明确为 n=17、BTC 占大头、同日相关（`:103-110`）。

**问题：** 50 条“信号”不等于 50 条独立、已闭合、可归属 episode；如果大部分未成交、同日相关或来自单一频道，重跑频率只会增加噪声和研究者自由度。

**修法：** 触发条件拆成新鲜 signal 数、可执行 order 数、已闭合 episode 数和有效日期数；只有达到预设 effective sample size、频道/品种覆盖和数据质量门槛才允许再校准，否则只出监控报告不改参数。

### [应改-08] §9 工期把外部依赖、人工标注和生产门禁排除在估时之外

**证据：** 方案 §9 L196-L207 把 M0 估为 1 周但写“gold 人工另计”，M1 1–2 周，M2 1 周，M3 4 周墙钟，M4 每步 2 周；旧 zone 计划明确节点执行器和网关接线曾因跨窗口依赖与风险规模决策阻塞（`docs/plans/zone-ladder-order-plan-v1.md:112-124`）。

**问题：** M0 同时包含历史时间溯源、episode 链、全 symbol K 线覆盖、缺失策略和 150 条人工标注；把标注“另计”使 M0 判据依赖一个未排期前置项。M1 还要确定交易所触发语义、部分成交、延迟、过滤器和保真校准。M2 的嵌套时序验证和统计审查不可能在一周内以可信结论完成。M3 需要新迁移、零凭据 shadow、部署、面板和连续四周观测；M4 两周内不可能得到“不劣于 control”的性能结论，除非事先给出功效和最小样本，而方案没有。

**修法：** 把里程碑改成证据闸门而非墙钟承诺：M0a 数据 provenance/coverage，M0b episode/gold，M1a 交易所语义合同，M1b 模拟器单测，M1c 实盘事件校准，M2 研究协议，M3 shadow 观测期，M4 用户授权 canary。每个闸门声明依赖、回滚和“样本不足时不可判定”，不要用日历时间代替统计证据。

## 建议

### [建议-01] §0/§2 的“L0/L1 立即有产品价值”应改成待证假设

**证据：** 方案 §0 L9-L10、§2 L40 断言 L0/L1“确定能做、且立即有产品价值”；但旧 zone 方案只有 17 条有效 zone 信号，且明确参数只是起点（`docs/plans/zone-ladder-order-plan-v1.md:103-110`），当前真实 `trade_outcomes` 只有 10 条（方案 §1 L19）。

**问题：** “能实现”不等于“改善执行质量”。在真实发布时间、订单拒绝、部分成交和系统故障未分离前，L1 可能只是把历史执行选择偏差编码成政策。

**修法：** 将产品价值改为待验证假设，先以 shadow 的 fill/拒单/成本分解证明改善方向；没有统计或执行证据时只交付数据资产和描述性报告。

### [建议-02] §2.1 的通用指标集合仍有隐含前视和版本漂移风险

**证据：** 方案 §2.1 L49 要求 EMA、ADX、VWAP、布林带、资金费率、持仓量变化等“全部在信号时刻之前可算”，但 §3.1 L84-L85 又承认发布时间可能只有 received time，且 `zone_penetration_stats.py:290-313` 现有查询以 received time 排序。

**问题：** 若信号时间不可靠，指标窗口边界和“之前”无法证明；资金费率、OI 和合约元数据还需要明确可获得时间、更新延迟和修订规则。

**修法：** 每个特征保存 `as_of`、数据源、到达延迟和版本；发布时间未知的样本禁止进入触发模型；用冻结的在线特征生成器或逐时点重放验证没有读取未来修订值。

### [建议-03] §8 的“shadow 统计算法想做但系统没执行”需要原因分解

**证据：** 方案 §8 L188-L194 计划将“算法想做但系统没执行”作为优先级判断；现有事故复核显示 HALT、inbox 冻结、第二腿 `position_exists`、watcher 断流是不同根因（`docs/plans/2026-09-05-missed-signals-remediation-plan.md:17-31`）。

**问题：** 只统计一个总比例会把算法拒绝、风险拒绝、过期、节点不活跃、交易所 reject、网络丢失和策略取消混在一起，无法判断策略价值还是基础设施可用性造成的损失。

**修法：** 为每条 shadow decision 建立状态机和拒绝原因枚举，至少拆成 `algorithm_skip`、`risk_reject`、`stale`、`halted`、`node_unavailable`、`exchange_reject`、`partial/filled`、`manual_interference`，按原因和时间窗口报告漏斗。

### [建议-04] §10 的不确定性清单不足以覆盖上线前必须冻结的契约

**证据：** 方案 §10 L209-L213 只把发布时间追回、保守撮合口径和小频道样本列为“最没把握的三点”，但 §4.1 L106-L112、§6.3 L174-L180 仍把触发基准、订单过滤器、权限和回滚当作已定设计。

**问题：** 真正会导致回测/实盘脱节的契约还包括 mark/last 触发源、post-only/IOC 拒单、min notional、保护单竞态、episode identity、手动订单隔离和 RESUME 授权。把这些留在实现阶段会使 M1/M3 的验收标准随代码变动。

**修法：** 在 M0/M1 之前建立一份不可变 execution semantics contract，逐项绑定线上代码、事件字段、testnet/生产证据和模拟器版本；契约未签字时禁止进入 M2 参数搜索和 M4 灰度。

## 最终裁定

**该方案不能直接作为 M0 开工依据。**

可以作为研究议题和拆分方向的输入，但只有满足以下前提后，才可启动受限的 M0 数据盘点工作：

1. 先冻结事件粒度和状态分类，明确哪些是可执行机会、删失样本、系统故障和重复消息。
2. 先验证并补齐 Telegram 原始发布时间；未知时间样本不得进入时序回测和触发模型。
3. 先定义基于 fill stream/position ledger 的 episode identity，并对歧义样本隔离。
4. M0 只做只读数据 provenance、覆盖率、质量报告和人工标注协议，不改线上执行、风险预算或开仓权。
5. 在 M1 前冻结 Binance USDT-M execution semantics contract；在统计方案中预注册 block/nested/holdout 设计和“不可判定”规则。
6. 任何 shadow 必须零执行凭据；任何灰度、开闸、恢复或风险乘子变更必须经过用户明确授权并遵守 `AGENTS.md`。

最站不住的一个断言是 **§4.2 的“模拟成交/不成交一致率 ≥90%”作为保真度门槛**：在只有 10 条物化结局、且只观察实盘成交子集的条件下，它既不可稳定估计，也不能识别未成交、拒单、队列和触发基准造成的系统性偏差。即使该数字偶然达标，也不足以支持 M2 政策选择或 M4 灰度。
