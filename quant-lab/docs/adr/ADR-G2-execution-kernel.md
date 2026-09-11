# ADR-G2：永续执行内核与候选 A/B 验收设计

- 日期：2026-09-11。
- 状态：设计提案；作为 G2 M-06～M-09 的实现依据，公共接缝待 G0 裁定。
- 决策：实现候选 A 自研参考内核，并以候选 B NautilusTrader 1.227.0 + SimulationModule 做同合同 spike；不提前指定最终主内核。
- 适用：research_only、USDT 线性永续、one_way、单 episode 隔离账户、冻结 order_plan。
- 本次仅新增本 ADR；未实现内核、未运行引擎、未修改契约或看板、未访问生产或调用收费模型。
- 本文“必须”是实现验收要求，不表示已有测试通过。

## 1. 依据、优先级与证据边界

1. [根 AGENTS.md](../../../AGENTS.md)：研究零生产凭据，不 import services，不操作生产。
2. [执行契约](../../contracts/execution-interface.md)：ExecutionRequest/ExecutionResult 基础字段。
3. [M-01 签名修订](report-G2-contract-revision-M01.md)：本设计按 R1～R10 修订后的签名；R11 是看板验证修复，与执行语义无关。
4. [契约总纲](../../contracts/README.md)：G0 拥有公共契约；本 ADR 不声称 OR-01 已冻结。
5. [选型报告 Q3/Q4](../../../docs/research/2026-09-07-quant-toolchain-selection/2026-09-07-quant-toolchain-selection-report.md)：Nautilus 源码事实的权威口径。
6. [早期源码核验笔记](../../../docs/research/2026-09-07-quant-toolchain-selection/research/r2-nautilus-source-verify.md)：仅作追溯，错误结论服从选型报告更正。
7. [合并计划 C.1/C.2/D.4](../../../docs/plans/2026-09-11-quant-scale-up-merged-plan.md)：三时钟、行情有效性、共同机会集与估计对象。
8. [G2 目标](../../goals/GOAL-2-market-kernel.md)：最小 episode、不变量、A/B 报告与 G3 对接要求。
9. [G3 接缝](../../contracts/feature-snapshot.md)：EventEvaluator 只消费结果，不重新撮合。

本机只读核对 `.venv-g2/lib/python3.12/site-packages/nautilus_trader/`：
- `nautilus_trader-1.227.0.dist-info/METADATA` 的 Version 为 1.227.0。
- `backtest/modules.pyx:42` 定义 SimulationModule；对应 modules.pxd 声明一致。
- `modules.pyx` 的方法是 `register_venue(self, SimulatedExchange exchange)`、`pre_process(self, Data data)`、`process(self, uint64_t ts_now)`、`log_diagnostics(self, Logger logger)`、`reset(self)`。
- `FXRolloverInterestModule` 示例调用 `self.exchange.adjust_account(Money(-rollover, currency))`。
- 本机 `backtest/engine.pyx:3221` 的签名为 `cpdef void adjust_account(self, Money adjustment)`，返回 void。
- frozen account 会直接返回；账户或该币种余额不存在时记录错误并返回，调用成功返回不等于账务已生效。
- 本机 `engine.pyx:3459` 在 trade tick 撮合前调用 `module.pre_process(tick)`。
- 本机 `engine.pyx:3553` 附近的 `process` 先 `_drain_commands(ts_now)`，再 `module.process(ts_now)`。
- 本机 Cython 运行路径与报告描述的 Rust 路由边界尚未做运行关联验证，登记 U01；不据此推翻报告事实。

## 2. 已核源码事实：is_stop_matched 的语义

- 报告核验的 `crates/backtest/src/engine.rs:1018-1023` 路由绕过 MarkPriceUpdate。
- SimulatedExchange 无 `process_mark_price`；不能假设订阅 MarkPriceUpdate 自动参与撮合。
- 普通止损走 `match_stop_order → is_stop_matched`。
- `matching_core/mod.rs:584-588 is_stop_matched`：买单 ask ≥ trigger；卖单 bid ≤ trigger。
- 多仓 SL 是卖出，空仓 SL 是买入；这是订单方向，不是持仓方向。
- 早期笔记把 `is_touch_triggered` 的反向比较拿来说明普通止损，已被 Q4 更正。
- `get_trailing_activation_price` 只描述追踪止损激活，不能证明普通 mark SL 可用。
- LAST/MID bar 合成 trade，并使 bid = ask = trade；BID/ASK bar 路径不能套用该结论。
- 原生 OHLC 合成价点可跳过未变化点，且时间戳均为 bar.ts_init。
- `PriceType::Mark` bar 会 panic；禁止将 mark bar 伪装成 LAST 污染成交价。
- 没有默认 funding 结算器；FundingRateUpdate 订阅能力不等于钱包入账。
- Instrument 携带 filter 属性不等于每个命令入口都执行全部 filter。
- OrderList 不等于 Binance 原子 OCO；限价触及成交不等于真实盘口排队。

## 3. 估计对象与输入边界

主结果是每个冻结种子的事件级净 R，不是共享资金账户权益曲线。
每个 request 有独立初始钱包、仓位与订单命名空间；禁止跨 episode 自动净额或复用余额。
风险分母 `risk_budget > 0` 在入场前固定；改单、部分成交、移动 SL 均不重置。
`kernel` 参数保持 `simulate(req, *, kernel="A")`；批量一行一个 request，不增加 candidate 参数。
`market_manifest` 必须解析到不可变数据集快照及分区哈希，禁止解析为“最新目录”。
policy_version 必须解析到不可变配置及内容哈希，包括梯档、费用、滑点、容量、账户、时序及价格舍入规则。
配置解析后纳入 request_canonical；同一个版本名解析出不同内容必须拒绝重放。
只支持 multiplier 已知的 USDT 线性合约；反向合约、hedge mode、跨币种抵押不在 v0。
有强平风险但无历史保证金阶梯证据时不得把结果当作可实施收益；`liquidation_unmodeled=true` 永远显式输出。
缺少账户配置属于请求配置错误，抛类型化错误；有效请求的资金不足产生 rejected 事件。

三时钟分工：
- 决策输入只用闭合且已可用记录；默认 strict_lt，等号需序列证据或明确 H0 假设。
- `last_closed_bar(..., latency=0)` 与 `mark_bar_at(marks, at, ...)` 遵循 M-01 的 H0 等号语义并保留依据。
- event_time 为 bar 右端点，不能把原始 close_time 的减 1ms 值当标准右端点。
- 决策快照不能读取包含 t_dec 的未闭合 bar 的 H/L/C。
- 执行标签可回放 t_start 之后的历史路径；合成路径是结果模型假设，不是当时可见特征。
- 若 t_start 落在分钟内部且没有逐笔证据，v0 从下一完整 bar 的 O 开始，不使用该分钟此前极值。
- mark/last 异步时分别推进自身时钟；只使用已推进到的 mark，不拿未来 mark 值回填 last。
- 超过 120 秒的 mark 不前填；缺口、重复主键和未知规则区间不得填零通过。

## 4. 候选 A 对象模型

`ExecutionContext` 持有 request、展开政策、历史规则视图、行情游标、逻辑时钟和确定性 ID 分配器。
`OrderLedger` 以 order_id 保存不可变订单版本与当前状态，终态订单保留供审计。
`WorkingQueue` 维护买卖限价簿及 pending market 队列；`ConditionalBook` 单独保存 mark SL。
`BracketGroup` 连接 entry 梯、一个 SL、多档 TP 和政策平仓腿。
`PositionLedger` 保存 signed_qty、entry_cost、累计入场量、已平量和已实现损益。
`AccountLedger` 保存 cash、reserved_margin、reserved_entry_fees、position_margin、累计费用和有符号 funding 现金流。
`FundingSchedule` 保存 instrument、结算时间、周期、费率和已入账键。
`EventJournal` 只追加领域事件；`ResultReducer` 从账本和事件产生合同结果。
`MarketCursor` 分开保存 last、mark 的价点、时间、来源 bar 和 coverage。
这些对象是职责划分，可先置于 kernel_a.py；避免把每个数据类拆成微型文件。

### 4.1 订单状态与挂单队列

合法主线：submitted → accepted → working → partial_fill* → filled。
拒绝路径：submitted → rejected；存活订单可 cancelled 或 expired。
partial_fill 后仍是存活订单，累计量等于目标量时只发 filled，不能重复发同笔 partial_fill。
每个订单只有一个终态；duplicate cancel/amend 只返回幂等结果，不再追加终态。
order_id 从 episode 身份、leg、梯档索引和版本序号派生，不使用 UUID 或墙钟。
同侧限价队列按价格优先、accepted_seq 优先、order_id 兜底；买价高先，卖价低先。
入场与出场冲突先执行保护性出场，entry 在退出 latch 激活后停止。
容量是模型假设：每个 last 价点有共享可成交预算，数量按 step 向下取整。
默认研究政策可设 `floor_step(participation × bar_volume / 4)`，四价点分别消耗，不跨点借用剩余预算。
重复价点仍保留独立容量槽；禁止按挂单数重复复制同一份流动性。
每笔 fill 为订单剩余量、价点剩余容量、账户可承担量、reduce-only 可平量的最小值。
未知成交量是缺证据；零成交量是零容量，不能当无限流动性。
盘口真实 queue-ahead、冲击传播和撮合延迟不由 OHLC 推断，报告披露近似。

### 4.2 入场梯、SL、多档 TP

entry fractions 必须为正且总和等于 1；TP fractions 为正且总和不大于 1。
limit 要求 price_lo = price_hi；区间使用 ladder；market_ref 用 t_start 后首个可用 last。
梯档数 N 由 policy 固定；N=1 使用区间中点，否则含端点等距展开。
risk_budget 模式的 entry_ref 采用展开梯价按 entry fraction 加权值；market_ref 用决策时已可用参考 last。
`qty = floor_step(risk_budget / (abs(entry_ref-stop) × multiplier))`；multiplier=1 时与 M-01 原式一致，非 1 接缝见 C02。
总量分配到梯档后向下量化，余数按固定档序逐 step 分配；分配后总量必须等于有效计划量。
每次 entry fill 增加实际持仓并激活或增量调整出场腿；未成交前不能有可成交 TP/SL。
TP i 的目标累计量为 `floor_step(cumulative_entry_qty × fraction_i)`，扣除该腿已平量。
只有 fractions 总和为 1 时，舍入余量分给最后一档；否则保留未分配仓位。
SL 覆盖当前全部可平仓位，不把各 TP 预留量相加后从 SL 再扣一次。
首次 TP fill 或 SL trigger 设置 exit latch，取消所有未成交 entry，防止退出期间重新加仓。
每笔 TP fill 后缩减 SL 数量；SL trigger 后取消 TP，释放 reduce-only market SL。
只触及 TP 而无容量时不设置退出 latch；SL trigger 即设置，不等待成交。
零仓位后撤销剩余兄弟腿，closed 每个 bracket 只发一次。
OrderList 仅表达组合关系，本模型的顺序原子性不宣称为交易所原子 OCO。

### 4.3 撤改单、TIF 与账户约束

cancel 在下一确定性命令处理点生效；在此前已完成的 fill 不回滚。
amend qty 不得小于 cumulative_filled；价格变化或增量改量失去队列优先级，减量保留。
校验失败的 amend 保留旧订单、旧预留，输出带 reason 的审计拒绝；不把旧订单终结为 rejected。
成功 amend 先校验新预留，再原子替换；记录 amended 和 old/new 版本关系。
当前 ExecutionRequest 没有管理命令流；v0 公共 simulate 只处理政策生成的撤改，G1 历史编辑接入见 C01。
GTD 有效区间 `[accepted_at, expire_at)`；等于 expire_at 不得 fill。
entry TTL 从 t_start 起算，约束全部未成交 entry，包括 GTC；GTD deadline 取两者更早。
IOC 在提交后首个合法撮合机会只尝试一次，立即撤销剩余，不能等下一 bar。
没有合法价格的 IOC 取消并记录 NO_EXECUTABLE_PRICE；行情整体缺失另按 coverage 删失。
post_only 穿越当前 last 代理盘口时拒绝，不能把拒绝单改记 maker fill。
reduce-only 只减现有反向仓位；逐 fill 截断，不得穿零反向开仓；零仓位剩余撤销。
初始钱包、杠杆 L、最大名义额、最大挂单数与手续费预留由冻结 policy 给出。
入场预留为 `qty × reservation_price × multiplier / L + fee_buffer`；撮合前按实际价重新校验。
钱包不足不自动加杠杆，整单拒绝或部分可承担成交必须由 policy 固定；v0 整笔候选 fill 不足则拒绝余量。
reduce-only 出场不要求新开仓保证金；费用仍入账，负钱包保留，不捏造强平。
mark 更新计算 equity = cash + unrealized；触及政策保证金可用性限制时停止新入场，保留出场。
这些账户约束只是隔离 episode 的可承担性模型，不能合成共享账户资金曲线。

### 4.4 精度与 filter

规则按 `[effective_from,effective_to)` as-of 连接；每次 submit/amend/fill 均检查有效历史版本。
至少检查 PRICE_FILTER 的 tick/min/max、LOT_SIZE 的 step/min/max、MARKET_LOT_SIZE 和最小名义额。
若历史规则只提供 M-01 的基础列，则不宣称覆盖完整交易所 filter，见 C04。
Decimal(str(silver_float)) 后按整数 tick/step 运算；不得用 float epsilon 接受非法价格。
用户明确价格不合 tick 拒绝；梯档生成价买向下、卖向上量化，stop 原价不偷偷调整。
mark 量化采用政策固定 ROUND_HALF_EVEN；行情不合法值隔离，不能作为正常舍入处理。
费用和 funding 钱包金额统一到冻结 settlement_quantum，ROUND_HALF_EVEN；DF 输出 Decimal(38,12)。
超过 12 位的小数或 38 位容量要显式拒绝或在政策声明量化；禁止悄悄截断。
market 滑点买加、卖减；stress = 1 tick + 0.02% 参考价，朝不利方向量化。
限价成交不能劣于限价；stress 穿越边际不足时不成交，不把限价变成无界市价。
slippage 是已包含在实际成交价中的诊断成本，不能从净 PnL 再扣一次。

## 5. 路径情景：primary / adverse / favorable

本设计将路径定义为确定性压力情景，不把事后表现命名为全局上下界。
方向从冻结计划 side 取值；运行中不得因仓位或收益改变路径。
每根 last bar 独立生成 O、第一极值、第二极值、C 四个离散价点。
primary：若 `abs(O-L) < abs(H-O)`，取 O→L→H→C；否则 O→H→L→C，等距 H 先。
adverse：long 取 O→L→H→C；short 取 O→H→L→C。
favorable：long 取 O→H→L→C；short 取 O→L→H→C。
mark bar 使用相同情景规则，但 primary 用 mark 自己的 O/H/L/C 判距离，不能复用 last 的极值次序。
四点合成时间为 open_time + 0、20、40、60 秒减 1 微秒（1m bar）；一般 interval 使用 0、1/3、2/3、interval−1us。
原始 close_time 保持右端点；合成 ts 是执行模型时间，bar_open_time 保留原归属。
价点间是离散跳变，不插值制造无限成交机会；不声称覆盖连续路径中的所有阈值先后。
last 与 mark 合成点按时间合并；同时间先缓存该 mark，再处理该 last；异步 bar 不强制对齐。
真实有序逐笔输入尊重源序列；三情景在无 bar 歧义的逐笔夹具上应相同。
报告给出三档逐 episode 数值与选定情景区间 `[min(R_s),max(R_s)]`，任一删失则不得隐去该档凑区间。
跳空、未成交入场、多次穿越、管理指令及追踪止损可使真实路径超出此区间。
三档与 base/stress 做笛卡尔积；primary 提前注册，不按 A/B 收益优劣更换。

### 5.1 触发与撮合判定顺序

内部使用 `(economic_ts, phase, source_seq, order_priority)`，输出事件序号不是撮合调度器。
P0：验证当前规则及行情 coverage；保存结算前仓位 q(t−)。
P1：处理该时刻 funding，以 q(t−) 结算；不得先处理同刻 entry 或 close。
P2：执行到期与已排定 cancel/amend，保证 GTD 等号不成交。
P3：摄入该时刻 mark 和 last 快照，校验各自新鲜度；未出现的新流不补未来数据。
P4：对已有持仓判定 mark SL：long mark≤stop，short mark≥stop；触发一次后进入退出 latch。
P5：未被 SL 抢占的 TP 按 last 判定：long last≥TP，short last≤TP；只在仍有可平量时发 tp_triggered。
P6：先撮合触发后的 SL market，再存活 TP，再 entry；每笔之后更新仓位、费用、预留和兄弟腿。
P7：新 entry fill 激活保护腿后重新检查当前 mark；若已越 SL，立即进入退出，最多一次无环检查。
P8：IOC 撤余量，零仓位撤兄弟腿，计算 mark 暴露与结果快照，执行事件后不变量。
SL 在 mark-only 时间点触发只释放订单，必须等当前或后续合法 last 价点才能成交。
SL 的 fill 事件 trigger_basis=last，只有 stop_triggered 可用 mark，符合 M-01 §3.4。
同一微步 SL 与 TP 同时满足时 SL 优先；同 bar 不同微步则按路径先后，不事后回溯覆盖较早 fill。
GTD 到点且 funding/SL 同刻：先结算旧仓位，再过期入口，再处理 SL；过期序列化位置不改变其经济效果。
限价 touch 采用 inclusive 判定；跳空给价格改善：买 `min(limit,last)`，卖 `max(limit,last)`。
A 的保守穿越变体应另立 policy，不能与 B 默认 touch 混成相同合同结果。
M-01 同 ts 的全局 kind 排序缺乏完整生命周期优先级，且不能承载多轮因果，修订 C03 专门处理。

## 6. Funding 账务时点

结算记录键为 `(instrument_id, calc_time, source_version)`；同快照重复键拒收，已应用键不重复入账。
正费率 long 支付、short 收取：`cash_delta = -signed_qty_before × multiplier × settlement_mark × rate`。
ExecutionResult.funding 采用有符号现金流，收入为正；净值为 `gross_pnl - fees + funding`。
funding 不是 commission，不计 slippage；与交易费分别累计。
在 calc_time 读取结算前仓位，结算同刻刚开的仓位不缴本次费，同刻平仓旧仓位仍缴费。
费率正负都按原样使用；不得 abs，也不得先按持有小时数摊薄。
8 小时例：00:00、08:00、16:00 UTC；这只是有证据的 schedule 实例，不是全品种常量。
可变周期例：08:00 后改为 4 小时，则有 12:00、16:00；按历史结算表校验，不能自己推成 16:00 才结。
未来已结算费率只可用于对应时刻账务，不可提前送进特征或 sizing。
结算 mark 取 calc_time 之前最后已闭合 1m mark close；H0=0 且允许等号时可取恰在 calc_time 闭合者。
该值是结算 mark 近似；禁止取 calc_time 所在未闭合分钟的 close，另跑 latency/前一根敏感性。
实际 calc_time 是否严格等于交易所入账时刻属 U03，真实数据适用性不得从合成测试推出。
无 mark 或超 120 秒时 MARK_STALE；缺结算行/周期证据时 FUNDING_SCHEDULE_GAP，不能费用补零。
funding 定时器须独立于成交量运行；无市场成交的一整段也必须在结算点处理。
canonical funding 事件用 leg=funding、trigger_basis=funding、qty=结算前有符号数量、price=settlement_mark。
当前事件 schema 缺 rate/cash_delta，见 C05；裁定前 funding 总额与内部审计明细一致，不把资金费伪装为 fee。

## 7. 不变量与断言位置

| ID | 不变量 | 断言位置 |
|---|---|---|
| I01 | t_dec≤t_start≤horizon_end，预算正，UTC，有限价格/数量 | contract 输入验证 |
| I02 | 历史规则、market 身份唯一，版本区间不重叠 | manifest 解析与每次规则切换 |
| I03 | order_qty = cum_fill + leaves + cancelled_or_expired_qty | 每次订单转移后 |
| I04 | 0≤cum_fill≤order_qty；只有一个订单终态 | 每笔 fill/cancel/expiry 后 |
| I05 | signed_position = Σsigned fills；entry_cost 与已实现成本守恒 | PositionLedger 更新后 |
| I06 | reduce-only 后绝对仓位不增，符号不翻转 | fill 前裁量及 fill 后 |
| I07 | 无 entry fill 时 TP/SL 不可平仓，qty=0 | trigger 前及 bracket 更新后 |
| I08 | GTD 到点无 fill，IOC 一次机会后 leaves=0 | 撮合入口及 P8 |
| I09 | 同价点总成交≤共享容量，成交价满足限价 | 撮合循环内 |
| I10 | cash_end = cash_start + realized_gross − fees + funding | 每笔账务及结果归约 |
| I11 | funding 使用 q(t−)，每个结算键至多一次 | P1 前后 |
| I12 | 预留不重复释放、不为负；新入场不超可承担额度 | submit/amend/fill/cancel |
| I13 | trigger_basis=mark ⇒ kind=stop_triggered；tp_triggered ⇒ last | 事件封装 |
| I14 | event seq 从 0 连续、订单 ID 稳定、无因果回环 | journal/导出 |
| I15 | SL 与 TP 共享可平量，零仓位后不能重入 | bracket 每次转移 |
| I16 | censor 非空 ⇒ net_pnl/net_R 为 null；完整未成交 ⇒ 二者 0 | ResultReducer |
| I17 | closed ⇒ 仓位零且兄弟腿终态；未闭仓不伪造 exit_avg | finalize |
| I18 | 风险分母固定，净 PnL 中滑点不重复扣除 | reducer 与 G3 契约测试 |
| I19 | 同输入/版本/seed 的 canonical trace 字节与 hash 相同 | replay 测试 |
| I20 | 当前 feature/决策不受未来 bars 或费率修改影响 | as-of 性质测试 |

运行时 invariant 失败抛 ExecutionInvariantError，停止该批研究，不伪装成市场删失。
普通数据缺口保留结果行、已有事件和 coverage，按约定原因删失；二者不可混为一类。

## 8. 最小 episode 独立期望

夹具放 `quant-lab/tests/market/fixtures/episodes/*.json`，不放 src。
每例含 request、resolved policy、历史规则、last/mark 点或 bar、funding 表、manifest 摘要。
每例另含手写事件表、账户 T 账、期望标量、coverage、删失原因与逐条推导说明。
独立期望不调用 kernel_a、nautilus_adapter、其撮合 helper 或 ResultReducer；A 输出不是 gold。
可用标准库 Decimal/Fraction 计算器复算手写算式，计算器不得读取引擎中间状态。
同一编写者先冻结纸面事件表再运行 A/B；后续独立复核比较公式和输入，不能复制实际输出覆盖期望。

默认夹具：long 1 单位、multiplier=1、tick=1、step=1、entry=100、SL=95、TP=105、risk_budget=5。
钱包=1000，L=1，容量充足；测试专用 policy `fixture-zero-v1` 费用/滑点为零，非 base 生产假设。
所有未特别说明者需在完整观察窗闭仓；点流显式有序，事件时间从 T 起递增。
报价/mark 初始 100，规则有效且 funding schedule 完整（无结算点也须有覆盖证据）。
每行期望必须扩展成 JSON 全字段；下表是最小规格，不表示 JSON 已存在。

| episode | 输入及边界 | 独立期望与推导 |
|---|---|---|
| E01 跳空 SL | T 入场100；T+1 mark94、last90 | stop_triggered(mark)→filled(last90)→closed；gross/net=-10，R=-2，entry/exit=100/90 |
| E02 双流异步 | 入场后 T+20 mark94，无新last；T+21 last98 | T+20只触发，T+21平98；net=-2，R=-0.4；禁止回用旧last100平仓 |
| E03 last 跌但 mark 未触 | 入场后 last94、mark100；再 last105、mark100 | 无 SL 事件，TP105平；net=5，R=1 |
| E04 同 bar 双触 | 已入场；两流 O100/H110/L90/C100 | long primary/favorable先H，TP按价点110成交，net10/R2；adverse先L，SL90成交，net-10/R-2；不是限价105的插值成交 |
| E05 funding 与平仓同刻 | 08:00旧仓1，settlement_mark100，rate0.001；同刻TP last105 | funding=-0.1先入账，再平；gross5，net4.9，R0.98 |
| E06 funding 与开仓同刻 | 08:00前仓位0；08:00入场100，之后TP105 | 本次funding=0，net5；不得使用开仓后qty收0.1 |
| E07 可变周期/空仓收付 | short1@100，08:00 rate0.001；12:00 rate-0.002；mark均100；之后100平仓 | 两次现金流+0.1、-0.2；funding/net=-0.1；第二次不能漏掉或乘4/8 |
| E08 部分成交 IOC | entry qty3，IOC，last100容量1，mark100，后TP105容量足 | entry fill1后cancel2；filled_qty1，fill_status=partial；TP1平，net5；同点不可再次消耗容量 |
| E09 未成交到期 | buy limit90，last100；GTD恰T+60，T+60 last90 | expired且无fill；filled_qty0、net0、R0、entry_avg=null |
| E10 reduce-only 竞合 | 已多仓1；TP qty2与SL同时满足，mark94/last105 | SL优先，只卖1@105；净仓0，TP取消，无反向仓；net5 |
| E11 区间入场梯 | qty2，ladder[98,100]两档各1；last100后98，再TP105 | 买100、98各1，均价99；平2@105，gross12；预算固定10时R1.2 |
| E12 多档止盈 | qty4@100；TP105 fraction0.5，TP110 fraction0.25；剩余1在mark94/last94止损 | 平2@105、1@110、1@94；gross14；exit_avg103.5；SL数量4→2→1 |
| E13 改单队列/撤单 | 两个buy90同价；首单增量改量后last90容量1；取消其余；已成交腿95平 | 后单获得唯一fill，首单失去优先级；各终态一次，gross5；另测非法减量保留旧单 |
| E14 filter/资金不足 | 分别tick1价100.5、step1量0.5、min_notional101下量1价100、钱包99而保证金100 | 每个子例submitted→rejected，无fill，拒因分别PRICE/LOT/NOTIONAL/MARGIN；合法边界对照必须接受 |
| E15 右删失/mark缺口 | 入场后窗口结束仍有仓；另一子例mark年龄121s且仍有仓 | 前者LABEL_RIGHT_CENSORED，后者MARK_STALE；net_pnl/net_R=null；保留entry事件和费用 |
| E16 funding缺口 | 持仓跨08:00但schedule要求的结算行缺失 | FUNDING_SCHEDULE_GAP，funding_ok=false；net=null，不能补零得完整R |
| E17 费用与滑点 | buy market参考100→101；sell market参考110→109；均taker0.0005，mark不触SL | gross8，fees0.0505+0.0545=0.105，slippage2，net7.895；不能再减2 |
| E18 入场即越SL | last100可入场，mark94；单点容量2；entry1后保护重检 | 入场1@100，SL立即触发并同点剩余容量平1@100，net0；不得等待下一bar才激活保护 |

E04 额外镜像 short 验证方向；E05/E06 增加 funding/expiry/close 三者等时组合。
E07 用跨日、跨月记录验证周期切换和 UTC；重复结算记录拒收，重复回调不重复入账。
E08 增加 GTC 两价点各fill1、余量到期；确保 partial_fill 与 filled 的增量量不混用累计量。
E14 每项分别做合法/非法边界对，不接受“任意拒绝就算通过”；post-only touch 拒绝另作子例。
E15 增加 BAR_GAP/RULE_HISTORY_MISSING/SYMBOL_TIME_INVALID，各自保留机会与正确 mask。
每例检查全 canonical_events、钱包和标量；gold hash 从独立序列化金标计算，不从实际结果录制。

## 9. 候选 B：SimulationModule 扩展

### 9.1 接入对象与 mark 控制器

Nautilus 版本固定 1.227.0，正常下单走无信号策略壳 → ExecEngine → BacktestExecClient。
SimulationModule 子类持有 mark 游标、funding schedule、条件 SL 表、去重键和诊断账本。
只向交易所撮合通道喂 last 交易数据；mark 由模块持有的独立只读流输入。
不能依赖 MarkPriceUpdate 自动进入 module.pre_process；入口覆盖须由 U01/U02 的 spike 证明。
优先将显式合成价点喂为 TradeTick，关闭原生 bar 展开，避免所有 O/H/L/C 同 ts 和自适应顺序覆盖政策。
SimulationModule.pre_process 可在 last tick 前读取截至该逻辑时点已到达的 mark，并向策略壳排队释放 SL。
mark-only 时点必须有独立调度脉冲；若公开 API 不支持在不制造 last 成交的条件下推进，则 B 本阶段不合格。
控制器自行判 long mark≤SL / short mark≥SL，不调用 A 的触发 helper。
触发后生成 reduce-only market order，订单成交仍由 Nautilus 实际产生，不能把 A 的 fill 注入 B。
同时间 mark→释放订单→last撮合的顺序必须从真实事件日志证实，不根据方法名称推定。
本机 process 先 drain commands 再运行 SimulationModule.process；在 process 内提交的命令可能要下一轮才排出。
因此只实现一个 process 回调并不足够；驱动器须用公开支持的推进/下单路径完成阶段屏障，见 U02。
不得调用私有 `_drain_commands` 或修改 vendor 包来假装薄适配器。
若只能延后一价点执行，必须输出差异码 B_COMMAND_LATENCY 并判合同不通过，不调整 gold 配合。

### 9.2 Funding 与账户刷新

SimulationModule.process 接收纳秒 ts_now，转换到合同 UTC 微秒时严格检查单位。
按结算表驱动，而不是 FX 示例的美东17:00或周三/周五三倍规则。
在每个结算时点快照 q(t−) 和近似 settlement_mark，计算有符号 Money adjustment。
调用 `exchange.adjust_account(Money(cash_delta, USDT))` 后核对 cache 余额与账户事件增量。
初始化必须确认账户存在、USDT余额存在、is_frozen_account=false，否则 B 初始化失败。
同刻新订单不得抢在 funding 之前消费旧余额；调整后风控/可用保证金视图必须同步刷新再处理入场。
如果仅在回测结束把 funding 减进结果，账户在过程中未变化，则不符合候选 B。
余额检查通过后标记结算键 applied；异常中断后的重放从空引擎开始，不带半完成模块状态。
Funding 算式由 B 独立实现；可以共享 Money 单位和事件 schema，不能共享 A 的账务计算函数。
Money 币种精度与 Decimal(38,12) 的差异须实测 U04，不用浮点容差掩盖账务不守恒。

### 9.3 适配器审计范围

| 范围 | 实际执行层需记录 | 必测 |
|---|---|---|
| 订单状态 | strategy/ExecEngine/exchange/事件归约分别留原始事件 | accepted/working/partial/filled/cancel/reject映射及幂等 |
| PRICE_FILTER | Instrument属性、命令验证或撮合层 | tick/min/max合法非法各例 |
| LOT_SIZE/MARKET_LOT_SIZE | limit与market所用验证路径分列 | step/min/max，不把两种filter混为一个 |
| 最小名义额 | submit与fill哪个阶段、使用何参考价 | 刚好边界与低一单位，包括reduce-only豁免 |
| reduce-only | 原生截断、拒绝、撤余量及适配层策略 | E10并发兄弟腿，不允许反向仓 |
| OrderList | 组合提交与后续子腿激活层 | 部分入场、TP缩SL、SL取消TP、拒一腿后的其余状态 |
| GTD/IOC | 引擎时钟、过期定时器、命令队列顺序 | 等号不成交、无tick也到期、IOC余量取消 |
| 价格精度 | Price/Quantity/Money构造与fill层 | 一tick、半tick、长Decimal、金额舍入 |
| 账户 | funding余额事件与风控缓存 | 资金不足前后、结算同刻开仓 |

原生未执行而 adapter 补做的 filter 必须标 ADAPTER_FILTER，不标 NATIVE_PASS。
公共请求验证可共享 schema；A/B filter 语义判断独立实现，防止共同 helper 隐藏错误。
每条记录源位置、配置、输入通道、观察到的事件和判定；报告源码事实不能代替运行测试。
不能覆盖的差异：真实盘口队列、交易所原子 OCO、真实结算 mark、强平/ADL、网络撤改单竞态、跨账户组合保证金。
容量/部分成交分配若无法用公开 FillModel 配置复现 A 的政策，标 B_LIQUIDITY_MODEL 并视作不同 policy，不能宣布对拍全过。

## 10. A/B 比较方法与报告模板

共同输入只共享 schema、单位、冻结政策文本、原始行情与事件封装，不共享撮合/触发/funding计算 helper。
先各自对独立期望和不变量验收，再比较 A/B；两者一致但都不符合 gold 仍为失败。
比较 canonical 业务字段时剔除 kernel/version 身份字段；保留数量、价格、因果顺序、费用、资金费和原因。
确定性夹具必须精确相等，经过明确量化后无浮点容差；真实离线校准的tick/step容差不套给金标。
P1 所有不变量和最小夹具必须通过；无法解释差异率必须为 0，已解释的合同违例也不能放行。
原生审计未覆盖项输出 not_run；缺合法离线快照时不运行实盘校准，不触碰生产获取资料。

| 比较维度 | 候选 A | 候选 B | 证据/选择依据 |
|---|---|---|---|
| 一次性代码量 | 待测：业务/测试/适配净行数 | 待测：壳/模块/测试净行数 | 同一计数工具与commit，排除生成文件和vendor |
| 可验证性 | 待测：gold通过数/总数，不变量覆盖 | 待测：同项及公开调度屏障证据 | 失败硬门先于评分 |
| 性能 | 待测：冷启动、热吞吐、p50/p95、RSS | 待测：相同量及模块调度开销 | 同机同manifest同episode规模 |
| 维护面 | 自研队列/账务/组合订单责任 | 上游升级/事件适配/扩展钩子责任 | 按模块列源码依赖与版本迁移测试 |
| 永续语义 | mark/funding/path每项状态 | mark/funding/path每项状态 | 明确native/adapter/not_supported |
| 逐episode差异 | hash、结果、事件位置 | hash、结果、事件位置 | 下表解释码与gold判定 |
| 最终决策 | 待P2 | 待P2 | 不用主观分数抵消正确性失败 |

| episode / scenario / cost | A版本/hash | B版本/hash | gold判定 | 首个事件diff | 数量/价格/费用/funding/净R差值 | 解释码 | 严重度/修复/复验 |
|---|---|---|---|---|---|---|---|
| E01 / primary / fixture-zero | 待运行 | 待运行 | not_run | — | — | NOT_RUN | 阻止定型 |

解释码固定枚举：
- MATCH：业务序列和标量精确一致且都通过 gold。
- B_MARK_ROUTE：mark 没有进入外部控制器或被错当 last。
- B_COMMAND_LATENCY：SL释放、撤单或账户调整比约定晚一处理点。
- FUNDING_ORDER / FUNDING_SIGN / FUNDING_ROUNDING：结算前仓位、符号或币种舍入差异。
- PATH_ORDER / SAME_TS_PRIORITY：合成路径、同刻因果排序差异。
- B_LIQUIDITY_MODEL / LIMIT_TOUCH / GAP_PRICE：容量、触及判定或跳空价差异。
- FILTER_MISSING / ADAPTER_FILTER / PRICE_PRECISION：过滤执行层或精度差异。
- ORDER_FSM / ORDERLIST_NONATOMIC / GTD_BOUNDARY / REDUCE_ONLY：订单生命周期差异。
- COVERAGE / CONTRACT_SERIALIZATION / UNEXPLAINED：数据用途、封装或未知差异；UNEXPLAINED 必须阻断。

基准计划：固定 18 例及全部子例，另用合成 100/1000/10000 request 测扩量，冷启动与热运行各分列。
每规模重复至少5次，固定硬件、并发度、版本、seed；报告中位数、p95与峰值RSS，不在本 ADR 伪造结果。
B 长跑优化只能在episode账户隔离成立后启用；order tag只能归因，不能隔离共享资金和净仓。
优化前逐请求启动成本照实报告；优化后必须与逐请求结果逐行一致并检测跨episode污染。
性能预算由G2/G0另冻结，未批准时仅报告测量，不挪用G3的20分钟AST预算。

## 11. trace_hash 与重放一致性

遵循 M-01：`sha256(canonical_json(execution_contract_version, kernel, kernel_version, request_canonical, canonical_events))`。
五项封装为固定命名对象；UTF-8、键按Unicode码点排序、无空格分隔、不转义非ASCII。
Decimal 使用非指数十进制字符串，去多余尾零，负零变0；时间统一 `YYYY-MM-DDTHH:MM:SS.ffffffZ`。
JSON null 保留用于合同缺值，不能按一般“偏好false”规则改成 false；禁止 NaN/Infinity。
数组顺序保留；canonical_events 按因果导出顺序，seq 从0连续；不能按JSON字段排序重新排列事件。
request_canonical 包含实际 t_start/horizon_end、完整计划、解析政策哈希及内容、数据集快照哈希、seed。
从解析内容剔除 ingested_at、下载墙钟、运行耗时、机器路径、随机UUID；保留决定可见性的available_at假设与源版本。
kernel_version 是代码/配置构建身份，B 含 Nautilus 包版本与制品摘要；不同内核 hash 本来就不同。
trace_hash 不承诺 A.hash=B.hash；A/B比业务事件与标量，另记录不含内核身份的diff摘要供定位。
M-01 哈希不直接包含派生标量，所以另断言 ResultReducer 可从事件和冻结输入复算所有金额；不得只验hash。
重放验收覆盖：同进程两次、清空对象后两次、不同进程、请求批次顺序打乱、单条与批量。
seed 派生每episode局部随机流，不依赖全局随机状态或批次位置；v0确定性容量不需要抽随机数。
改 ingested_at 不改 hash；改 policy 内容、seed、有效行情值、路径、事件或内核版本必须反映到 hash。
对没有实际影响结果的行情更改，manifest 哈希仍变化，因而 request_canonical 与 trace_hash 应变化。

## 12. G3 EventEvaluator 的 ExecutionResult 列

simulate_batch 输出列严格按 M-01 §3.3，候选 take/skip 由 G3 应用，不在 G2 重复执行一份 candidate 模拟。
配对键为 `(episode_id,graph_version,policy_version,cost_scenario,path_scenario,kernel)`。
同键多行若 decision_snapshot_hash/manifest/合同不一致则拒绝连接，不能静默取最后一行。

| 列组 | 列与定义 |
|---|---|
| 输入身份 | episode_id, graph_version, decision_snapshot_hash, t_dec, policy_version, cost_scenario, path_scenario, market_manifest, execution_contract_version, seed |
| 内核身份 | kernel, kernel_version, trace_hash |
| 成交 | fill_status；filled_qty为累计entry数量，不含平仓量；fill_status按有效展开计划量判none/partial/filled |
| 均价/时刻 | entry_avg_price, exit_avg_price按实际fill量加权；position_open_at为首次entry fill；position_close_at仅完整归零时有值 |
| 金额 | fees正支出；funding有符号现金流；slippage正不利执行差额；gross_pnl为已实现毛损益，完整闭仓后与全交易差额一致 |
| 标签 | net_pnl=gross_pnl−fees+funding；net_R=net_pnl/risk_budget；删失优先返回null |
| 暴露指标 | mae_R,mfe_R：每个mark微步计算累计realized_gross+当前unrealized的轨迹，分别取min(0,min轨迹)/预算与max(0,max轨迹)/预算，费用不混入；接缝见C06 |
| 质量 | censor_reason；coverage_mask={mark_ok,funding_ok,rules_ok,bars_ok,liquidation_unmodeled} |
| 明细 | canonical_events: List(Struct)，保留M-01 seq/ts/kind/order_id/leg/trigger_basis/price/qty/fee/reason/bar_open_time/path_step |

所有金额列 Decimal(38,12)，时间 Datetime(us,UTC)，缺值是 null；G3 如需浮点自行 cast。
未成交且证据完整：gross/net/R=0，均价与仓位时刻null；mae/mfe=0。
已成交未闭合：保留已实现gross、fees、funding和暴露指标，net_pnl/net_R=null，不按horizon市价强平。
max_holding_s 在 M-01 被纳入观察终点；v0视为删失边界，不擅自生成强制平仓成交。
若未来政策要求超时平仓，需显式增加可执行close政策并确保观察窗允许成交，见C06。
coverage字段在未使用但已证明完整的输入段可以为true；缺少所需证据必须false，不默认全true。
多个缺证据同刻：按 SYMBOL_TIME_INVALID→RULE_HISTORY_MISSING→BAR_GAP→MARK_STALE→FUNDING_SCHEDULE_GAP 选主因，其余写事件/诊断；首次阻断后不继续撮合。
G3先冻结共同机会集和证据排除，完整未成交记0；删失/证据缺失不补0、不候选各删各的。
候选AST NaN遵循G3 fallback=skip(0)，不能改变baseline或共同分母。
G3统计未闭合率需区分已成交未闭仓与从未开仓，不能仅看position_close_at为null。
G3冒烟断言同一take策略差额0、全skip差额为负baseline、亏损样本NaN不缩分母、删失不补0。

## 13. 模块划分与测试矩阵

| 文件 | 职责 | 禁止职责 |
|---|---|---|
| src/quant_lab/market/contract.py | M-01模型、单位、版本解析契约、请求构造和纯schema验证 | 产生金标或决定撮合 |
| src/quant_lab/market/kernel_a.py | A的队列、组合订单、撮合、账务、路径与不变量 | import B的撮合逻辑 |
| src/quant_lab/market/nautilus_adapter.py | 策略壳、SimulationModule、独立路径/账务、原生事件归因 | 调A求fill、写vendor补丁 |
| src/quant_lab/market/execution.py | simulate分发、batch列、规范序列化/哈希、结果边界验证 | 重做A/B触发或包含G3选股逻辑 |
| tests/market/ | 独立夹具、性质测试、A/B spike与G3合同测试 | 放src内、引用生产服务 |

`build_request(episode_row, events, *, policy_version, risk_budget)` 属主建议 G2 contract.py；仅消费G1已冻结规范事件。
该函数不能重新解释原文或把t_dec之后的编辑合并进初始order_plan，待C01/G0定稿。

| 测试文件（实现规划） | 矩阵 |
|---|---|
| test_contract.py | M-01全部列/枚举/可空性、Decimal溢出、配对键重复 |
| test_kernel_episodes.py | E01～E18×适用方向×3路径×成本；逐事件与手算账本 |
| test_order_invariants.py | 随机submit/amend/cancel/fill，数量/终态/reduce-only/队列容量 |
| test_funding.py | 正负费率、零仓、8h→4h、同刻开平仓、重复/缺失、币种舍入 |
| test_path_scenarios.py | 等距primary、双流不同极值次序、异步、跳空、重复价点 |
| test_asof.py | 等号、晚到、未来修改、跨周期未闭合、120/121秒 |
| test_nautilus_adapter.py | SimulationModule回调、命令屏障、余额刷新、完整filter审计表 |
| test_kernel_ab.py | 首个diff、解释码、独立gold、未解释率0、not_run不得算pass |
| test_replay.py | 跨进程、批次重排、seed/manifest变化、非决定性字段排除 |
| test_execution_g3.py | 一request一行、null/0区别、共同分母与take/skip冒烟 |

本 ADR 不运行这些测试；G2 实现后按 M-06→M-07→M-08→M-09 提交证据。
M-06先冻结独立期望；M-07全部gold和不变量通过；M-08完成B阶段屏障与差异报告；M-09通过G3列契约。
任何不变量失败先修内核；B不合格可保留A参考结果及失败报告，但不得声称两候选完成定型。

## 14. 未决单列：契约修订建议（新增6条）

以下 C01～C06 是本 ADR 新增建议，不重复计数 M-01 已提交的 R1～R11；本次不修改公共契约。

| ID | 建议及临时设计 | 裁定属主/阻断范围 |
|---|---|---|
| C01 | 确认build_request属主与签名；增加未来管理命令流的available_at/seq/id/版本语义；当前只允许冻结计划和政策内部撤改 | G0+G1+G2；阻断历史编辑回放，不阻断静态夹具 |
| C02 | policy补账户余额/杠杆/容量/梯档/entry_ref；risk sizing公式纳入multiplier；冻结费用、funding正负号与slippage不重复扣 | G0+G2；未裁定时非multiplier1与非夹具政策不得出正式标签 |
| C03 | 将M-01同ts按kind全局排序改为因果phase/source_seq排序，增加可承载amend拒绝及多轮fill因果的字段；kind优先级仅同因果层使用 | G0+G2+G3；阻断最终canonical serializer冻结，禁止用排序倒置生命周期 |
| C04 | 扩历史rules涵盖price/qty上下限、MARKET_LOT_SIZE、min-notional适用性、保证金参数及有效期；未知项明确unsupported | G0+G2；阻断完整交易所filter合规声明 |
| C05 | canonical funding增cash_delta、rate、settlement_id（或等价typed payload），补amended old/new字段；当前price/qty/fee不足以自包含复算 | G0+G2+G3；阻断仅靠canonical_events复算完整账务的声明 |
| C06 | 冻结mae/mfe轨迹定义、未成交零值、gross部分实现含义、max_holding与horizon边界、路径合成时点、GTD等号规则 | G0+G2+G3；阻断跨政策指标比较，临时按本文合成夹具设计 |

## 15. 未决单列：待核事项（8条）

每项只计一次；报告的既定源码事实不重新降级为猜测。

| ID | 待核内容 | 验证出口 |
|---|---|---|
| U01 | 本机Cython SimulatedExchange与报告Rust路由的实际连接、SimulationModule登记及mark-only调度可达性 | 最小运行spike记录对象类型、包摘要、回调序列；有冲突报G0，未验证不称可用 |
| U02 | 同刻funding→余额刷新→mark触发→命令释放→last撮合可否由公开API实现 | E02/E05/E06/E10日志验证；不能实现则B失败，不修改vendor |
| U03 | 真实funding calc_time与入账时刻、周期切换和近似结算mark偏差 | 获授权公开归档证据与敏感性报告；当前只支持合成证明 |
| U04 | Nautilus Money/账户币种精度能否满足冻结钱包量化与Decimal(38,12)合同 | 最小正负微额funding/fee账务例，必须显式量化一致 |
| U05 | 全部filter、OrderList部分拒绝、GTD、reduce-only在选定命令通道的真实执行层 | §9.3每项合法/非法对照；原生与adapter分别记录 |
| U06 | 公开FillModel能否复现确定性共享容量、部分成交及重复价点预算 | E08及多订单竞争测试；不符标B_LIQUIDITY_MODEL且不得同policy通过 |
| U07 | M-01遗留instrument_id命名、H0默认latency、费率放policy还是契约的最终裁定 | G0 OR-01；本文临时采用全市场ID、H0=0、policy存费率，不冒充已批准 |
| U08 | A/B批量性能、账户隔离可行性与维护成本 | 同机5次基准、跨episode污染测试、代码量报告；P2再选主内核 |

待核项允许通过合成夹具继续实现；对应能力的正式声明必须等证据闭合。
本文件交付验收只验证文档存在、行数与关键词；不等同 M-06～M-09 工程验收。
