# 2026-07-24 五日对抗审计结论与修复方案（待 review）

> 审计方式：5 路对抗式子代理（交易活动/数据一致性/部署漂移/代码审查/运维遗留）+ 2 路专项取证（BTC 裸仓、孤儿止损），全程只读（SELECT + 读文件 + 无鉴权健康端点），未动任何线上文件/进程/订单。
> 窗口：2026-07-18 00:00 ~ 2026-07-24 02:30 UTC。时间均 UTC。

## 一、审计结论摘要

**战绩**：已实现盈亏 -202.5 USDT（11 笔平仓），当前浮盈 +56.5U。最大两笔亏损均为系统 bug：07-19 SOL 保护单递归级联（-10.87）、07-20 ETH 止盈误作用于混合来源净仓引发 26 笔级联（-104.66）。

**核心事实（全部有 DB/文件/mountinfo 证据）**：

1. **projection_actor 补丁从未挂载**：补丁 07-19 落盘 container-patches/，但 /proc/<node-pid>/mountinfo 无挂载条目，07-21 容器重建也没带上 → 07-23 node-a 重启回放旧事件，positions_projection 现有 3 个幽灵仓（ETH short 2.265 / ETH long 0.193 / JTO short 850，交易所均无）。reducer 无单调性守卫，旧 PositionChanged 覆盖了新 PositionClosed。
2. **orders_projection reducer 缺陷**：OrderFilled 后 1-2ms 的 OrderUpdated 覆盖终态 → status='updated'、filled_quantity=0，累计 18 张，07-18 后新增 7 张。
3. **孤儿 algo 止损结构性不可撤**：Binance 2025-12 起未触发条件单迁入独立 algo-order 系统（线上 exchange_state_recorder.py:4-5 注释自证并已补读 /fapi/v1/openAlgoOrders，节点对账却没跟上）。容器重建前挂的 algo 单永久掉出 Nautilus 进程缓存；planner 撤单只查 context.existing_orders（container-patches/intent_execution_planner.py:212-218）→ denied:order_not_found；执行层需缓存内 order 对象才能撤，全链路无按 venue_order_id 撤单路径。零自愈：TTL 豁免保护单（monitor ttl_decision :136-151，序号 11/91 判为 protection → skip）、heal 只处理反方向且不撤单（:370-384）、reconcile 因心跳 open_orders=null 每轮 skip、Binance algo 单不随仓位 expire。现存两张：JTO SELL STOP 850@0.5641（order_id 1000002464583088）、ETH SELL STOP 1.309@1802.76（1000002461155352）。当下触发是 -2022 哑弹；未来重开同向仓则真实误平（07-14 同族）。07-19 02:21 的"兜底取消"实为人工在交易所侧直撤（无 PendingCancel 的收养式记账）。
4. **BTC 0.135 多单零保护事故**（07-23）：13:27 撤单只收到 OrderPendingCancel、无 Canceled/无 CancelRejected（僵尸单）；保护 stash 已被第二腿按"一 instrument+side 一 owner"规则弹掉（intent_execution_strategy.py.fixed `_stash_entry_protection` supersede-pop）；13:52:36 僵尸单被系统外手工改价（64764.7→64898.9，系统无 amend 能力）后成交 0.175 → 成交时无人认领保护，intent 里的 SL 62947.3 + 三档 TP 65534.4/66633.3/67432.5 从未提交（非 -2022 拒单，BTCUSDT 窗口内零 OrderRejected）。14:03 用户口头"不调整"固化裸仓。naked 告警未发：sweep_naked 按 symbol 判定，0.004 空头残腿的 SL 使 BTCUSDT 进 sl_symbols，掩盖多头裸仓；另 snapshot_stop_loss_symbols 把 reduce-only TP 也计为 stop-like、position_sides 按 symbol 键控 hedge 下互相覆盖。
5. **trade_outcomes 管道死亡 16 天**：/etc/cron.d/trader-v3-trade-outcomes MAILTO="" 吞错，日志停在 07-07；日报 KPI 假空白；07-21 日报整份缺失无告警。
6. **DB postgres 超管密码明文在 ps aux（ingress cmdline）16 天未轮换**，仍有效。
7. **归属加固停在 shadow**：87 条 shadow 记录 67 条 would_reject（77% 管理动作不带 provenance）；服务端 source_channel 缺省回退 hermes-operator，--channel operator 可旁路；近半成交名义额（$80.1k/$168.5k）是无 intent 的 aos_ 手工单，归属体系失明——07-20 事故"混合来源净仓"的土壤。
8. **feeder**：大脑断供 >6min 丢信号且"已拦截"通知走同一坏链路；TimeoutExpired 不计 attempts 卡 cursor 无限重投。
9. 正面确认：7-14 误平模式 intent 通道内未复发（149 条 intent 溯源链完整、5 笔 close 归属全对）；monitor"只标记不撤单"与 HALTED 放行边界（reduce_only 硬编码 + 仓位前置）经攻击验证是紧的；0008 回填 894:894 账实相符。

## 二、修复方案（待 review 对象）

### 第 0 层：手工动作（用户执行，非代码）
- H1 撤两张孤儿 algo 止损 + 清 RUNE/HYPE 双份止损 + 撤 4 张旧 BTC SELL 挂单（66800/67000/67050/67225）。
- H2 给 BTC 0.135 多单补真实止损（经 Hermes）。
- H3 轮换 DB 密码，连接串移出命令行参数（改环境文件/secret）。

### 第 1 层：本周代码修复（4 个可独立验收的任务包）

**F1 节点撤单路径修复**（根因 #3）
- 节点启动/周期对账补读 /fapi/v1/openAlgoOrders，algo 单纳入缓存或至少纳入可撤集合。
- planner _plan_cancel_order_intent 在缓存 miss 时回查 exchange_state_mirror（或控制面注入 mirror 视图），mirror 中存在则放行撤单。
- 执行层支持按 venue_order_id / algoId 撤单兜底。
- 验收：重启节点后对重启前挂出的 algo 止损下 cancel intent 能成功撤到交易所侧。

**F2 投影正确性**（根因 #1 #2）
- 部署（挂载）projection_actor 补丁；reducer 加事件时间单调性守卫（旧 ts_event 不得覆盖新终态）。
- 修 OrderFilled 被 OrderUpdated 覆盖：终态（filled/canceled/expired）不可被非终态事件降级，filled_quantity 只增不减。
- 从 execution_events 重建 positions_projection，清 3 个幽灵仓。
- 验收：重放 07-23 10:49 的重放窗口事件序列，投影不再复活已平仓位；18 张 status='updated' 已成交单修正。

**F3 撤单确认追踪 + naked 判定修正**（根因 #4）
- strategy/monitor 任一层：OrderPendingCancel 后 N 分钟未见 OrderCanceled/CancelRejected → TG 告警 + 重试撤单。
- sweep_naked / snapshot_stop_loss_symbols 改按 (symbol, position_side) 粒度；reduce-only TP 不计为止损；position_sides 改复合键。
- 验收：构造 hedge 双向持仓 + 单边裸仓用例，naked 告警触发；构造 PendingCancel 悬置用例，告警触发。

**F4 outcomes 管道复活 + freshness 告警**（根因 #5）
- 修 cron（去 MAILTO="" 吞错，错误输出落地可读日志），回补 07-07 以来 outcomes。
- monitor 新增 sweep：trade_outcomes.max(computed_at) 落后 PositionClosed 超 24h → 告警；日报文件当日缺失 → 告警。
- 验收：回补后 11 笔平仓 outcomes 齐全；人为停 cron 24h 告警触发。

### 第 2 层：结构性（下迭代，本次只 review 方向）
- S1 保护单挂载时机重设计：保护参数随 intent 落 DB，成交事件到达时无论 stash owner 在否都兜底挂出；撤单中/已放弃 intent 的成交同样触发。
- S2 归属 shadow→硬拒前置：管理动作强制 provenance（先解决 77% 缺失）、堵 --channel operator 旁路，再切硬拒。
- S3 手工单（aos_）实时 TG 播报 + 写 channel_ctx。
- S4 仓库与线上 hotpatch 分叉对齐（先收编 feeder 两条纪律回仓库）。

### 明确不急
heartbeat halt_reason、HALT 审计记录、heal SQL 参数化、feeder 超时重投、3ec33d0 权限矩阵对齐（并入 S4）。

## 三、部署约束
- 线上真身是 hotpatch 分叉（deployed strategy 2230 行 vs 仓库 651 行），F1/F2 的节点侧改动需以 container-patches + 容器重建方式部署（需 root 窗口），并必须验证挂载（mountinfo）——projection_actor 就是部署了没挂载的前车之鉴。
- balen 无 docker/journal 权限；DB 直连方式见 memory。

## 四、v2 修订（2026-07-24 Codex 对抗 review 后，关键主张已本地核实）

review 证实的三个任务书缺陷：
1. F2 写入点错误：真正覆盖终态的是控制面 repository 的无条件 upsert（repository.py upsert_position_projection 为 last-write-wins，无 ts_event 守卫）；orders 侧仓库已有成熟 order_reducer（stale_backfill + legal_transition + GREATEST(filled_quantity)），修复应收敛到它而非在 actor.py 上叠守卫。F2 拆为 F2a（reducer/写路径原子守卫）+ F2b（shadow 表重建、(ts_event,event_id) 排序、mirror 对账、事务切换）。
2. F4 freshness 指标失效：computed_at 仅 insert 时 DEFAULT now()，upsert 更新不刷新 → max(computed_at) 永久误报/漏报。改用独立 job-run watermark；episode 算法在 hedge/混合来源下的 KPI 正确性问题另立任务。F4 按 review 降到最后一批。
3. F3 naked 判定粒度不足：主键应为 (account, symbol, position_side)，且校验 STOP 类型、方向相反、有效触发价、覆盖数量 ≥ 仓位数量；180s 成交豁免须收窄到具体 symbol+side。pending-cancel 追踪与 naked 拆为独立故障域，追踪键 (account, client_order_id) 持久化，超时后先查 mirror 真相再告警。

优先级调整（采纳）：feeder TimeoutExpired 修复从"不急"提升到本周；S1（保护单持久化）提升到本迭代、在 F3 pending-cancel 接自动重试前完成；S4 最小部署基线（线上文件 hash/mount/image 清单 + 部署后 hash+mountinfo 验证门）提到 F1/F2 部署之前。F1 需实现独立 exchange cancel adapter（orderId 与 algoId 分路由 + 撤后回读确认），验收补五类用例（普通单/algo 单/撤前成交/已撤幂等/错误账户拒绝）。

H1~H3 顺序修订：预检（读 fresh 交易所仓位+双类挂单）→ H2 先给 BTC 补真实 STOP 保护并从 openAlgoOrders 确认 → H1 逐张撤（每张：撤单请求→交易所缺席确认→仓位复核）→ H3 凭据原子轮换（注意 trade_outcomes CLI 强制 --db-url 会继续暴露密码，需与 F4 协同改运行方式）。

开工批次：第一批 = F2a+F2b（worktree）与 F3 修订版+feeder timeout（主树）并行；第二批 = S4 最小基线 → F1 adapter；第三批 = S1 → F3 自动重试衔接；F4/S2/S3 按 review 顺序后置。
