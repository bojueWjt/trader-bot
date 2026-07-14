# 平仓归属加固方案对抗审查

审查对象：`docs/plans/2026-07-14-close-attribution-hardening.md`

事故背景：`INCIDENT-2026-07-14.md`

审查日期：2026-07-14

审查方式：静态读取证据文件；未执行交易、撤单、服务重启、数据库写入或其他状态变更命令。

## 结论

**当前方案整体须改版后开工。W0、W1 可立即执行；W2-W5 需先修正本文 10 条 P0。**

方案抓住了三个真实问题：频道消息误分类、算法成交状态缺失、管理动作缺少归属闸门。核心实现路径仍缺少可信频道来源、虚拟 lot 数量台账、执行前数量预留、entry 范围管理动作、全写操作幂等、保护单代际与并发协议。

按原文实施会出现两类直接资金风险：

1. 服务端验证一条 `entry_ref`，执行端按 symbol/position side 处理整个聚合仓位，其他频道的同向数量仍会被平掉或改写保护单。
2. operator 路径生成的归属链把所有频道下单统一记录为 `hermes-operator`，`channel=operator` 又由同一个 Hermes 调用方自行声明，闸门同时具备误拒和绕过路径。

发现统计：

| 等级 | 数量 | 结论 |
|---|---:|---|
| P0 | 10 | 上线前必须修正 |
| P1 | 11 | 会导致闸门失效、误拒、状态漂移或灰度失控 |
| P2 | 2 | 工程债与审计可靠性问题 |

在确定性归属闸门上线前，建议把频道来源的 `close/partial/set-sl/set-tps` 临时收紧为人工确认动作。用户口头指令继续走独立 operator 流程，并明确展示受影响的全部归属和数量。

## P0 Findings

### P0-1：operator 落库链保存了固定 operator 来源，真实频道归属没有进入 intent

**证据**

- 方案假定 `trade_intents -> hermes_decisions -> raw_messages.channel_id` 可以提供入场频道：`docs/plans/2026-07-14-close-attribution-hardening.md:10,51-54`
- feeder 通过 prompt 把真实频道交给 Hermes：`.live-mirror/scripts/hermes_signal_feeder.py:86-104`
- open CLI 只发送 `client_ref`、reason、symbol 等字段，没有发送频道或 canonical raw message id：`hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py:109-144`
- operator 端点为每次请求新建 raw message，并把 `channel_id` 固定写成 `hermes-operator`：`.live-mirror/api/read_api.py:1977-1984,1999-2006`
- 新建的 Hermes decision 连接这条 operator raw message：`.live-mirror/api/read_api.py:2017-2029`

**失败场景**

Gauls 的 open intent、舒琴的 open intent、用户口头 open intent 都沿 operator 端点落库。三者的 `raw_messages.channel_id` 都是 `hermes-operator`。W2 按方案查询归属时会得到以下结果之一：

- enforce 模式把真实频道仓判为 operator 专属，Gauls、舒琴等频道的正常管理全部返回 403。
- 实现者额外使用请求体中的 `channel` 覆盖历史链，服务端开始信任 LLM 自报频道，归属闸门失去可信来源。

**修复建议**

open 请求必须携带服务端可验证的来源标识，例如 `source_raw_message_id`。控制面查询 canonical `raw_messages` 后复制不可变的 `source/channel_id/source_message_id` 到专用 attribution 表或 intent 字段。

频道身份由 feeder 或可信 ingress 生成并签名，Hermes 只负责传递。历史 operator intent 进入显式迁移队列，禁止根据 reason 文本自动回填频道。

### P0-2：`channel == "operator"` 是同一 Hermes 凭证可自行声明的全局旁路

**证据**

- 方案允许 `channel == "operator"` 跳过归属拒绝：`docs/plans/2026-07-14-close-attribution-hardening.md:56`
- feeder 只在 prompt 中声明当前频道身份：`docs/plans/2026-07-14-close-attribution-hardening.md:35`
- CLI 的所有写请求使用同一个 `RISK_ADMIN_TOKEN`：`hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py:21-42`
- operator 端点只验证调用者角色为 `risk_admin`：`.live-mirror/api/read_api.py:1801-1803`

**失败场景**

频道消息、prompt 注入、错误推理或重试代码把 payload 中的 `channel` 写成 `operator`。服务端看到的凭证与真实用户口头操作完全相同，随后跳过归属拒绝。Gauls 频道仍可通过 operator 旁路平掉舒琴仓位。

**修复建议**

人类 operator 使用独立 endpoint 或独立 token。频道 session 使用受限 token，服务端从 token claims 或签名来源断言中取得 `channel_id`。

`operator` 权限只接受经过用户确认的 request id，响应列出将受影响的全部 entry lots、频道和数量。请求体中的字符串不能提升调用方权限。

### P0-3：单条 `entry_ref` 授权后，`close` 仍会平掉同向聚合仓的全部数量

**证据**

- 方案确认同 symbol 同向仓位在 Binance 层合并：`docs/plans/2026-07-14-close-attribution-hardening.md:13`
- W2 只要求解析并校验一条 entry intent：`docs/plans/2026-07-14-close-attribution-hardening.md:51-55`
- 线上 skill 明确把 close 定义为“整仓平掉某币种，市价 reduce-only”：`.live-mirror/skill/SKILL.md:76-80`
- close CLI 没有 quantity 字段，partial 才有 quantity：`hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py:147-177`
- operator API 只在 `partial_close` 分支读取 quantity：`.live-mirror/api/read_api.py:1911-1923`

**失败场景**

同一账户 BTC LONG 聚合仓包含：

- Gauls：0.040 BTC
- 舒琴：0.085 BTC

Gauls 请求携带合法 Gauls `entry_ref`。归属校验通过。执行端收到 `close_position`，按当前 BTC LONG 总量 0.125 BTC 平仓，舒琴的 0.085 BTC 再次被误平。

**修复建议**

管理动作引入明确范围：

- `close_scope=entry`：按目标 entry lot 的可用剩余数量生成 reduce-only partial close。
- `close_scope=book`：按聚合 book 整仓处理，只允许独立 human operator 权限。

频道 session 的 `close` 命令固定映射为 entry-scope close。服务端把目标 lot 数量写入 intent，执行端只接受确定 quantity。

### P0-4：`set-sl` 和 `set-tps` 仍按聚合 book 工作，频道校验无法保护其他频道的保护单

**证据**

- 方案只要求 close/partial 携带 `entry_ref`，set-sl/set-tps/cancel 只要求 `channel`：`docs/plans/2026-07-14-close-attribution-hardening.md:45,50,57`
- 线上 skill 说明 set-sl 会按“当前仓位数量”重挂：`.live-mirror/skill/SKILL.md:82-87`
- 线上 skill 说明 set-tps 会替换已有仓位的全部止盈档：`.live-mirror/skill/SKILL.md:89-90`
- CLI 的 set-sl/set-tps 请求没有目标 entry 字段：`hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py:219-267`
- API 的保护动作 order plan 只保存 stop/take-profits 和可选 position side：`.live-mirror/api/read_api.py:1931-1940`

**失败场景**

Gauls 与舒琴共同持有 BTC LONG，各自拥有不同止损和止盈计划。Gauls 的更新消息通过频道校验后执行 `set-tps`。节点撤掉 BTC LONG 的全部旧 TP，并按聚合仓总量安装 Gauls 的 TP。舒琴计划被覆盖。

`set-sl` 同样可能把整本 BTC LONG 的止损移动到 Gauls 的价位。

**修复建议**

set-sl/set-tps 强制携带 `target_entry_intent_id`。保护单建立持久关系：

`entry lot -> protection generation -> exchange order ids`

替换动作只撤销目标 lot 当前 generation 的保护单，只按目标 lot remaining quantity 安装新 generation。跨频道 book-level 保护调整归 human operator 权限。

### P0-5：entry 校验缺少 account 与 position side 绑定

**证据**

- 方案校验项包含 action、symbol、channel 和可选数量，没有 account 与 position side：`docs/plans/2026-07-14-close-attribution-hardening.md:52-55`
- operator 请求独立接收 `account_id`：`.live-mirror/api/read_api.py:1811-1813`
- operator 请求独立接收 `position_side`：`.live-mirror/api/read_api.py:1835-1841`
- close/partial 把请求中的 position side直接写入 order plan：`.live-mirror/api/read_api.py:1952-1955`

**失败场景**

- account-b 的合法 Gauls BTC LONG `entry_ref` 被用于 account-a 的 BTC LONG。
- BTC LONG 的合法 `entry_ref` 搭配 `position_side=short`，管理同账户 BTC SHORT book。

频道相同、symbol 相同的情况下，现有 W2 校验会放行。

**修复建议**

服务端从目标 entry intent 派生并锁定：

`(account_id, normalized_symbol, position_side)`

管理请求中的 account、symbol、side 只能作为一致性断言，任何不一致直接拒绝。`target_position_id` 也由服务端解析，调用方不能自由覆盖。

### P0-6：归属校验与异步执行之间没有数量预留，SL/TP 可在窗口内消耗目标 lot

**证据**

- 方案只描述请求时计算“该 entry 的未平数量”：`docs/plans/2026-07-14-close-attribution-hardening.md:55`
- operator 请求提交后只写一条 approved trade intent 和 outbox event：`.live-mirror/api/read_api.py:2037-2049`
- 节点通过轮询接口异步拉取 approved intents：`.live-mirror/api/read_api.py:443-501`
- 当前 intent 只保存 `target_position_id`，没有 target entry、reserved quantity 或 attribution version：`.live-mirror/api/read_api.py:2037-2042`

**失败场景**

Gauls lot 剩余 0.040 BTC，服务端通过 `partial_close 0.040` 校验。节点拉取前，Gauls 的止损先成交 0.040 BTC；聚合 BTC LONG 仍包含舒琴 0.085 BTC。延迟到达的 reduce-only sell 继续成交 0.040 BTC，实际扣减舒琴数量。

**修复建议**

审批事务内锁定目标 lot，创建 `reserved_qty`。不变量：

`available_qty = opened_qty - closed_qty - reserved_qty`

执行 intent 保存 `target_entry_intent_id`、`reserved_qty`、`attribution_version`。fill 按实际成交结算 reserved quantity；拒单、过期、撤单释放预留。执行节点在提交交易所前重新验证 reservation 状态和 exchange book 数量。

### P0-7：管理动作缺少强制幂等，uvicorn 重启和网络超时会重复减仓

**证据**

- 方案把 uvicorn 重启后的自动重试描述为“幂等安全”：`docs/plans/2026-07-14-close-attribution-hardening.md:61`
- 服务端只对 `open_position` 强制 client_ref：`.live-mirror/api/read_api.py:1879-1887`
- 所有 CLI 子命令的 `--ref` 默认可空：`hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py:329-334`
- client_ref 为空时，幂等键由随机 intent id 派生：`.live-mirror/api/read_api.py:1972-1982`
- 数据库事务在 HTTP response 前提交：`.live-mirror/api/read_api.py:1986-2054`

**失败场景**

partial close 已提交并 commit，uvicorn 在返回响应前重启。客户端收到连接错误并重试。第二次请求生成新的 intent 和新的 idempotency key，重复减仓。

set-sl、set-tps、cancel 同样会重复提交。entry_ref 只表达目标归属，无法承担一次管理操作的幂等身份。

**修复建议**

所有写动作强制独立 `operation_ref`。幂等材料至少包含：

`operator-v2|actor_scope|account|action|symbol|position_side|target_entry_intent_id|operation_ref`

数据库采用原子 insert-on-conflict 返回已有 intent。客户端对 timeout、connection reset、502、503 使用同一个 operation_ref 重试。

### P0-8：W3 引用的 episode 算法无法产出每条 entry 的实时 remaining quantity

**证据**

- W3 计划基于 P0-4c episode 算法生成多条 attribution 和数量：`docs/plans/2026-07-14-close-attribution-hardening.md:63-67`
- 参考算法按 `(account,instrument)` 分组，没有 position side：`.worktrees/smartness-p0/scripts/analysis/trade_outcomes.py:168-188`
- 算法只根据 BUY/SELL 给 fill 加正负号：`.worktrees/smartness-p0/scripts/analysis/trade_outcomes.py:146-155,190-205`
- episode 只选择第一条同向 tagged fill 作为 primary：`.worktrees/smartness-p0/scripts/analysis/trade_outcomes.py:217-236`
- 其他 intents 只进入 `contributing_intent_ids`：`.worktrees/smartness-p0/scripts/analysis/trade_outcomes.py:260-269`
- 仍未归零的 open episode 被直接丢弃：`.worktrees/smartness-p0/scripts/analysis/trade_outcomes.py:205-206`

**失败场景**

1. Gauls 买 0.040，舒琴买 0.085，随后卖 0.020。算法无法确定这 0.020 应扣哪个 entry，也无法输出两条实时 remaining。
2. hedge mode 下 BUY 1 开 LONG，SELL 1 开 SHORT。按 order side 净额会得到 0，算法生成虚假 closed episode。
3. 多笔 `-e1/-e2` 入场和多档 TP 只形成 contributing intents，所有权会偏向第一笔 fill。

**修复建议**

W3 改为事件化 lot ledger，至少保存：

`entry_intent_id, channel_id, account_id, symbol, position_side, opened_qty, closed_qty, reserved_qty, remaining_qty, attribution_status`

fill 消费使用唯一 event/trade id。系统管理 fill 按 reservation 精确结算；entry 自带保护单 fill 回到原 lot；外部变化进入 `unattributed_qty`。核心守恒式：

`sum(lot.remaining_qty) + unattributed_qty = exchange book quantity`

### P0-9：W2 上线到 W5 上线之间，新的 close 仍会持续制造孤儿保护单

**证据**

- 事故已经证明平仓后残留保护单可形成裸空、超量和幽灵单：`INCIDENT-2026-07-14.md:32-43`
- 当前 skill 只要求 partial 后由 Hermes 手工执行 set-sl/set-tps：`.live-mirror/skill/SKILL.md:19-20`
- close 命令只描述整仓平仓，没有清理保护单步骤：`.live-mirror/skill/SKILL.md:76-90`
- 方案把保护单 reaper 放在 W5，并安排 W2 先上线、W5 一周内后续上线：`docs/plans/2026-07-14-close-attribution-hardening.md:75-79,85-87`

**失败场景**

W0 清理了事故当下的危险单。W2 上线后，任意一次正常 close 又留下旧 SL/TP。W5 到达前，这些订单触发后仍可创建反向仓位或超量退出。

**修复建议**

保护单收尾进入 close/partial 的同步生命周期：

- close fill 完成后撤销该目标范围的全部 residual protections。
- partial fill 完成后按 lot remaining 生成新 protection generation。
- 交易所 reconciliation 通过后，管理 intent 才进入 completed。

W5 reaper承担异常修复，主流程承担正常收尾。

### P0-10：W5 复用当前 monitor 会漏读 algo orders，并在保护单替换窗口误判

**证据**

- W5 计划在仓位减少后自动撤销超量/无主系统单：`docs/plans/2026-07-14-close-attribution-hardening.md:75-79`
- 当前 monitor 的 exchange truth 只读取 `payload.open_orders`：`.live-mirror/scripts/order_lifecycle_monitor.py:325-359`
- 当前查询工具明确把 `open_orders` 与 `algo_orders` 分开展示：`hermes-profile/skills/trading/v3-trader/scripts/v3_query.py:184-215`
- 当前 monitor 在 fresh mirror 中找不到订单时会把投影直接终态化：`.live-mirror/scripts/order_lifecycle_monitor.py:523-531`
- naked 检测把 position side 按 symbol 收进单值字典，同 symbol 多空会互相覆盖：`.live-mirror/scripts/order_lifecycle_monitor.py:734-749`
- monitor 默认 15 秒循环，mirror 最长允许 300 秒：`.live-mirror/scripts/order_lifecycle_monitor.py:46-58,817-857`

**失败场景**

1. SL/TP 位于 `algo_orders`。reaper 只看 `open_orders`，把真实在场保护单识别为无主或已消失。
2. 节点正在替换保护单，短时间内出现旧单、新单并存，或旧单已撤、新单待接受。reaper依据延迟镜像撤掉新 generation，节点随后完成旧单清理，仓位进入裸仓。
3. hedge mode 同 symbol LONG/SHORT 被压成一个 side，reaper按错误 book 计算超量。

**修复建议**

W5 使用独立保护单账本和 `protection_generation`。自动撤单前满足全部条件：

- 新鲜 exchange snapshot 同时覆盖 open orders 与 algo orders。
- `(account,symbol,position_side)` advisory lock 已持有。
- 订单与 entry lot、generation、lifecycle role 精确关联。
- 候选连续两轮稳定，且 generation 已明确 superseded。
- enforce 前经过 observe 模式、人工抽样和故障注入。

## P1 Findings

### P1-1：`client_ref` 命名空间缺少 channel、action 和 symbol

**证据**

- feeder 和 skill 使用 `tg-<消息ID>`：`.live-mirror/scripts/hermes_signal_feeder.py:98-104`、`.live-mirror/skill/SKILL.md:16`
- operator 幂等键真实公式为 `sha256("operator|account_id|client_ref")`：`.live-mirror/api/read_api.py:1972-1982`

**失败场景**

两个频道都出现消息 5019，并在同一账户开仓。第二笔 `tg-5019` replay 第一笔 intent。相同 ref 用于不同 symbol 或不同 action 时也会 replay 已有 intent。

**修复建议**

entry ref 使用复合身份：

`telegram:<channel_id>:<source_message_id>:<leg>`

幂等键包含 actor scope、account、action、symbol、position side 和复合 ref。历史 `tg-<id>` 标记为 legacy ambiguous。

### P1-2：`tg-` 直查 raw_messages 只能提供候选消息，无法证明该消息创建了目标 intent

**证据**

- 方案把 `tg-` 前缀直查 raw_messages 作为 entry intent 解析路径：`docs/plans/2026-07-14-close-attribution-hardening.md:51`
- operator intent 连接的是新造的 `hermes-operator` raw message：`.live-mirror/api/read_api.py:1999-2029`
- v3_query 的审计链沿 `hermes_decisions.raw_message_id` 查询，因此看到的也是 operator raw message：`hermes-profile/skills/trading/v3-trader/scripts/v3_query.py:235-265`

**失败场景**

系统按 `tg-5019` 找到某条 Telegram raw message，再按 ref hash 找到某条 operator open intent。两者之间缺少数据库外键，重复 message id、历史手工 ref、拼写错误都可能形成错误拼接。

**修复建议**

open intent 直接保存 `source_raw_message_id` 或 attribution 外键。直查文本 ref只用于迁移候选，迁移结果需要人工确认或可复现审计证据。

### P1-3：cancel 所有权证明只验证 UUID 对应某条 intent

**证据**

- cancel 只检查 client order id 格式并验证内嵌 UUID 存在于 `trade_intents`：`.live-mirror/api/read_api.py:1845-1873`
- 方案只提出“补频道比对”：`docs/plans/2026-07-14-close-attribution-hardening.md:57`

**失败场景**

调用方提交真实系统订单号，同时传入错误 account 或 symbol。当前所有权证明没有验证该 client order id 与请求 account、symbol、position side、orders projection 和 exchange mirror 的一致性。

**修复建议**

cancel 校验完整元组：

`(client_order_id, intent_id, account_id, symbol, position_side, channel, live_exchange_presence)`

目标订单的频道从其 entry/protection relation 取得。

### P1-4：外部/手动减仓无法恢复真实频道意图

**证据**

- W4 计划通过仓位减少和缺失系统 close intent定位归属频道：`docs/plans/2026-07-14-close-attribution-hardening.md:69-73`
- positions 查询只提供交易所聚合 book 数量：`hermes-profile/skills/trading/v3-trader/scripts/v3_query.py:142-178`
- episode 参考算法没有外部成交到 entry lot 的映射：`.worktrees/smartness-p0/scripts/analysis/trade_outcomes.py:168-205`

**失败场景**

用户在 Binance App 手工卖出 BTC LONG 0.040。聚合仓包含 Gauls 0.040 和舒琴 0.085。系统只能观察总量从 0.125 降到 0.085，无法证明用户关闭了哪个频道。

**修复建议**

外部变化进入 `unattributed_qty` 或 `attribution_status=needs_reconciliation`。系统采用明确会计政策时必须标记 `estimated`。存在 unattributed quantity 的 book 限制频道自动管理，转 human operator 对账。

### P1-5：现有 fill watcher 会漏掉管理 intent 的保护单，并吞掉同订单后续部分成交

**证据**

- `is_protection_order` 只把序号 `>=11` 视为保护单：`.live-mirror/scripts/order_lifecycle_monitor.py:104-128`
- monitor 自身说明 move-stop management intent 的保护单可能使用序号 01，并在价格解析中单独处理：`.live-mirror/scripts/order_lifecycle_monitor.py:134-145,566-586`
- fill watcher 按 client order id 去重：`.live-mirror/scripts/order_lifecycle_monitor.py:687-712`
- 保护动作在当前 API 中会生成独立 management intent：`.live-mirror/api/read_api.py:1931-1940`

**失败场景**

- move-stop 或 replace-tps 生成的管理订单使用另一套序号，watcher不会识别为保护单。
- 同一个保护单分两次成交，第一次写入 dedup key 后，第二次成交不再回写 channel_ctx，remaining quantity 错误。

**修复建议**

W4 使用 `execution_events.event_id` 或 trade id 持久 cursor。订单 lifecycle role 从 intent action 和 protection relation读取。累计成交字段按前值计算 delta。

### P1-6：channel_ctx 的无锁 append 与整文件压缩会丢失机器事实

**证据**

- feeder 直接以 append 模式写文件，没有文件锁：`.live-mirror/scripts/hermes_signal_feeder.py:413-428`
- 压缩器先读整文件，再以 `w` 覆盖：`.live-mirror/scripts/hermes_signal_feeder.py:460-488`
- 压缩器只保留 `- ` 开头的记录：`.live-mirror/scripts/hermes_signal_feeder.py:479-488`
- W4 计划新增 `## 系统持仓事实` 小节：`docs/plans/2026-07-14-close-attribution-hardening.md:71-72`

**失败场景**

W4 正在刷新机器事实，feeder 同时 append session 结果，压缩器又基于旧内容覆盖文件。任一写入会丢失。`## 系统持仓事实` 标题和非 `- ` 行也会在压缩时消失。

**修复建议**

机器事实存入 PostgreSQL 或独立结构化文件。prompt 构建时读取并渲染。保留 markdown 时统一使用 `flock + temp file + os.replace`，压缩器只处理人工历史区。

### P1-7：channel_ctx 文件身份和末尾截断会让系统事实退出 prompt

**证据**

- 文件名 digest 使用 channel id，slug 同时依赖 channel name：`.live-mirror/scripts/hermes_signal_feeder.py:261-266`
- prompt 只读取文件末尾 1500 字符：`.live-mirror/scripts/hermes_signal_feeder.py:53-55,269-275`

**失败场景**

- 频道改名或 W4 只持有 channel id，W4 与 feeder 写入两个不同文件。
- 系统事实区位于文件前部，人工 session 记录增长后，系统事实落在末尾 1500 字符之外。

**修复建议**

文件主键只使用不可变 channel id。系统事实以独立 prompt block 完整注入，并附 DB event watermark 和生成时间。

### P1-8：feeder cursor 提供至少一次投递，重启安全依赖下游幂等

**证据**

- Hermes 执行和 channel_ctx append 发生在 cursor 保存前：`.live-mirror/scripts/hermes_signal_feeder.py:436-457,528-542`
- cursor 为空时 feeder直接初始化到最新消息，历史不会重放：`.live-mirror/scripts/hermes_signal_feeder.py:515-519`
- attempts 和 retry_after 只保存在进程内存：`.live-mirror/scripts/hermes_signal_feeder.py:504-506`

**失败场景**

- 进程在订单提交或 Telegram 回复后、cursor 保存前退出，重启后重复处理同一批。
- cursor 文件丢失或部署路径变化，feeder跳到最新消息，停机窗口消息被跳过。
- 重启清空失败计数，poison batch重新获得三次尝试。

**修复建议**

建立持久 delivery ledger，按 `batch_hash/signal_ids` 记录 `job_created, intents_committed, response_delivered, ctx_committed, cursor_committed`。管理动作的 operation_ref 从 batch 和具体动作确定性生成。

### P1-9：monitor 缺少单实例锁，observe 模式也缺少持久候选状态

**证据**

- feeder 启动时使用 flock：`.live-mirror/scripts/hermes_signal_feeder.py:497-502`
- lifecycle monitor 直接进入循环，没有实例锁：`.live-mirror/scripts/order_lifecycle_monitor.py:817-857`
- `--dry-run` 是显式参数，默认进入 active 运行：`.live-mirror/scripts/order_lifecycle_monitor.py:817-827`
- dry-run 跳过状态保存：`.live-mirror/scripts/order_lifecycle_monitor.py:850-854`

**失败场景**

服务重启重叠产生两个 monitor。两个实例同时生成 reaper 候选和撤单请求。observe 模式每 15 秒重复输出同一候选，三天统计被重复样本污染。

**修复建议**

W5 使用 `REAPER_MODE=off|observe|enforce`，默认 off。monitor 通过数据库 advisory lock 或 lease 保证单实例。observe 也持久化候选、去重、人工判定和最终结果。

### P1-10：当前 TTL 流程按“唤醒 Hermes 成功”消耗续期，缺少实际订单结果

**证据**

- TTL 挂龄来自 `orders_projection.ts_event`：`.live-mirror/scripts/order_lifecycle_monitor.py:506-512`
- Hermes job 创建成功后立即记录 woken 和 renewal：`.live-mirror/scripts/order_lifecycle_monitor.py:539-561`
- W5 只写“修复 48h TTL 清理器”，没有定义确定性终态：`docs/plans/2026-07-14-close-attribution-hardening.md:78`

**失败场景**

Hermes job创建成功，cancel 请求失败或根本没有执行，monitor仍把续期次数加一。部分成交更新 ts_event 后，挂龄可能重新计算。过期单继续留在交易所。

**修复建议**

TTL 基于 entry intent 的 `created_at/valid_until`。到期后生成确定性 cancel intent。续期必须显式写入新的 valid_until 和审计事件。状态只根据 exchange order disappearance 与 cancel terminal event推进。

### P1-11：灰度、回滚和仓库镜像缺少可执行闭环

**证据**

- W2 只定义一个 `OPERATOR_ATTRIBUTION_ENFORCE` 开关：`docs/plans/2026-07-14-close-attribution-hardening.md:58`
- warn 日志位置、指标和误拒率仍是开放问题：`docs/plans/2026-07-14-close-attribution-hardening.md:105`
- 回滚只描述恢复 live `.bak` 和重启：`docs/plans/2026-07-14-close-attribution-hardening.md:61`
- 仓库版 `services/control-plane/api/read_api.py` 只有 snapshot API，线上镜像有 2113 行 operator 热补丁：`services/control-plane/api/read_api.py:1-66`、`.live-mirror/api/read_api.py:1-2113`
- 仓库版 ProjectionWriter 仍是占位接口：`services/control-plane/db/repository.py:82-98`

**失败场景**

- live API、CLI、SKILL、feeder、W4 cursor、channel_ctx 格式、W5 reaper 分别处于不同版本。
- 恢复 read_api `.bak` 后，新 CLI 仍发送新字段，新 feeder仍要求新流程，W4/W5继续运行。
- warn-only 只有文本日志，无法统计候选总数、未知归属率、潜在误拒率和旁路使用率。
- 仓库镜像缺少 operator 路径，测试通过的代码与部署代码形成两套实现。

**修复建议**

先把 live operator 路径收敛到仓库可测试版本。每个工作块拥有独立开关和回滚步骤：

- provenance capture
- attribution shadow
- attribution enforce
- entry-scoped close
- context writer
- reaper observe/enforce

shadow 指标至少包含：管理请求数、解析成功率、legacy/ambiguous 数量、跨频道候选数、operator bypass 数量、quantity mismatch、reservation conflict、最终执行差异。

## P2 Findings

### P2-1：数量使用 float，归属守恒和交易所步长会出现边界误差

**证据**

- `_op_num` 把所有数值转换为 float：`.live-mirror/api/read_api.py:1778-1789`
- partial quantity 以 float 进入 order plan，再转字符串：`.live-mirror/api/read_api.py:1922-1923,1952-1953`

**失败场景**

多次 partial、部分成交和 reservation 累加后产生微小误差。`remaining_qty` 可能显示负小数、超出交易所 step，或在守恒校验中产生假 mismatch。

**修复建议**

API、lot ledger、reservation、事件 reducer 全链路使用 Decimal 字符串。按 instrument quantity increment 向下量化后再比较和预留。

### P2-2：intent 前缀查询会在多匹配时静默取第一条

**证据**

- v3_query 接受最短四字符 intent 前缀：`hermes-profile/skills/trading/v3-trader/scripts/v3_query.py:235-243`
- 多条匹配时直接选择 `intents[0]`：`hermes-profile/skills/trading/v3-trader/scripts/v3_query.py:244-247`

**失败场景**

归属调查使用短前缀，查询返回另一条 intent，人工核对得到错误频道和执行链。

**修复建议**

查询结果超过一条时拒绝并列出候选前缀。生产操作要求至少 8 位且唯一。

## 对 8 个开放问题的判定

| # | 开放问题 | 判定 | 审查结论 |
|---|---|---|---|
| 1 | entry_ref 强制是否卡死正常流 | **成立** | legacy、外部仓、多 entry 和聚合保护动作都会被卡住。更深问题是单 ref 无法授权整本仓位。历史仓进入迁移/人工队列；新仓使用复合 entry identity 和 lot ledger。 |
| 2 | idempotency_key 与 `tg-` 直查是否可行 | **成立** | operator 公式为 `sha256(operator\|account\|client_ref)`，已知 account 时可以计算候选 hash；hash 无法反向恢复 ref。`tg-` raw message 与 operator intent 缺少外键，消息 id 也缺少 channel 命名空间。 |
| 3 | operator bypass 是否成为逃逸口 | **成立，P0** | 同一个 Hermes 使用 risk_admin token并自行提交 `channel`。`operator` 字符串直接提升到旁路权限。需要独立 human operator 身份。 |
| 4 | uvicorn 重启对在途 intent/feeder 的影响 | **成立，P0** | commit 后 response 前重启会触发重复管理动作。所有写操作强制 operation_ref 后，才能把该窗口降为可控重放。需做 pre-commit/post-commit kill 实验。 |
| 5 | feeder 重启 cursor/幂等安全性 | **成立** | cursor 文件写入本身原子，端到端处理具备至少一次语义。订单、回复、ctx 与 cursor 之间存在多个退出窗口。需做执行后、ctx 后、cursor 前的 SIGKILL 实验。 |
| 6 | W5 自动撤单误杀和节点竞态 | **成立，P0** | 当前 monitor 漏读 algo orders、允许 300 秒镜像、缺少 protection generation、book lock 和 side-safe 聚合。episode 分摊策略还需历史回放和并发实验。 |
| 7 | W4 与 feeder 写 channel_ctx 的竞态 | **成立** | append 无锁，压缩整文件覆盖，机器小节会被压缩器删除，文件名和末尾截断还会隐藏事实。机器事实应进入 DB 并在 prompt 时渲染。 |
| 8 | 灰度默认值与观测指标 | **成立** | 单一 enforce 开关无法覆盖来源采集、shadow、entry-close、W4、W5。默认值、日志结构、误拒率口径、旁路率、回滚阈值和版本健康检查都缺失。 |

## 必须先改的方案骨架

### 1. 可信来源

open intent 保存 canonical `source_raw_message_id`。服务端从 canonical raw message取得 channel。频道 session 与 human operator 使用不同身份。

### 2. 虚拟 lot 台账

每条 entry intent 形成独立 lot。系统持续维护 opened、closed、reserved、remaining 和 attribution status。交易所聚合 book 与 lot 总量保持守恒。

### 3. entry 范围管理动作

频道 close 固定为 entry-scope quantity close。set-sl/set-tps 只管理目标 lot 的 protection generation。book-scope 动作只允许 human operator。

### 4. 执行预留

管理请求审批时锁 lot 并预留数量。执行 fill 结算预留，失败和过期释放预留。执行前重验 attribution version 和 exchange book。

### 5. 全写操作幂等

open、close、partial、set-sl、set-tps、cancel 都强制 operation_ref。幂等键包含 actor、account、action、symbol、side、target entry 和 operation ref。

### 6. 保护单生命周期

正常 close/partial 自己完成保护单撤销或缩量。W5 只处理异常漂移。保护单具有 lifecycle role、target lot、generation 和 exchange order关系。

### 7. 状态与上下文

W4 从 DB event cursor 消费，机器事实存 DB。channel_ctx 只保存 session 定性摘要。prompt 分别注入实时系统事实和人工上下文。

### 8. 灰度与回滚

部署顺序：

1. W0 清理当前危险单。
2. W1 上线，并临时把频道管理动作改为人工确认。
3. 收敛 live operator 代码到仓库，补齐自动测试和历史回放。
4. 上线 provenance capture，保持 shadow。
5. 上线 lot ledger、reservation 和 entry-scoped management，保持 shadow。
6. 对历史仓完成迁移或标记 ambiguous。
7. enforce 小流量放行，监控误拒、未知归属、reservation conflict 和执行差异。
8. W4 使用 DB 状态源。
9. W5 依次进入 off、observe、enforce。

每一步拥有独立回滚开关。W5 回滚的第一动作固定为 `REAPER_MODE=off`。

## 开工判定

**W0、W1 可以立即开工。**

**W2-W5 按当前方案不可开工。** 开工前需要完成以下设计决策并写入修订版方案：

1. canonical 来源如何绑定 open intent。
2. human operator 与频道 session 如何分权。
3. lot ledger、外部变化和 unattributed quantity 的会计政策。
4. entry-scope close/set-sl/set-tps 的数量与保护单语义。
5. reservation、执行重验和失败释放协议。
6. 全管理动作幂等协议。
7. close/partial 的同步保护单收尾。
8. W5 generation、锁、observe 指标与回滚协议。

这些决策落地后，方案具备进入实现和历史事故回放测试的条件。
