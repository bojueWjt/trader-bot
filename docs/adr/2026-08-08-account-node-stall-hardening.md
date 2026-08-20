# ADR: Account Node Stall Hardening

- 状态：Proposed / review
- 日期：2026-08-08
- 决策范围：Nautilus node 控制面会话、Redis persistence、健康状态、发布与 rollout
- 事故依据：`docs/incidents/2026-08-08-account-node-stall.md`

## Context

当前节点把 heartbeat、operator command poll/ACK、approved intent poll 和部分事件上报
绑定到 Nautilus actor timer callback。同步网络调用可以占住 actor 调度线程。Redis
persistence 使用 runtime instance identity 扩展 key space，message streams 没有 retention。
生产发布依赖 bind-mounted hotpatch，运行字节可以与宿主文件、Git commit 和同组节点分叉。

系统需要一个深模块收敛控制面复杂性：actor 只提交本地工作，网络、重试、队列、时限、
progress clock 和降级策略全部隐藏在模块实现内。调用者只依赖小而稳定的 interface。

控制面角色名称保留现有 `operator-query`，其职责范围是完整操作员 API 平面：
查询接口以及受 `RISK_ADMIN_TOKEN` 和 settings RBAC 保护的订单管理配置写接口。
`node-control` 和 `event-ingest` 不暴露 settings 路由；数据库角色
`trader_v3_operator_query` 持有对应 settings、outbox 和 audit 写权限。

## Decision

引入 `NodeControlPlaneSession` 模块作为 node 与 control plane 的唯一 seam。模块 interface
只暴露：

```text
start()
stop(deadline)
submit_execution_event(event) -> accepted | backpressured
snapshot() -> SessionHealth
```

heartbeat、command、ACK、intent 和 event egress 均属于模块内部实现。Nautilus actor
callback 只写入 coalescing tick token 或 bounded local queue，并在 10 ms 内返回。

```mermaid
flowchart LR
    Actor["Nautilus actor<br/>local tick and delivery only"]
    Session["NodeControlPlaneSession"]
    Heartbeat["Heartbeat lane"]
    Command["Command poll/apply/ACK lanes"]
    Intent["Intent fetch/delivery lanes"]
    Event["Event egress lane"]
    Watchdog["Independent watchdog"]
    Spool["Durable local spool"]
    ControlPlane["Control plane"]
    Redis["Fenced generation Redis namespace"]

    Actor --> Session
    Session --> Heartbeat
    Session --> Command
    Session --> Intent
    Session --> Event
    Watchdog --> Session
    Command --> Spool
    Event --> Spool
    Heartbeat --> ControlPlane
    Command --> ControlPlane
    Intent --> ControlPlane
    Event --> ControlPlane
    Session --> Redis
```

### Lane Isolation

| Lane | 执行资源 | Queue | 满载策略 | 顺序与持久性 |
|---|---|---|---|---|
| heartbeat | 独立单线程 worker | capacity 1，latest-wins | 合并旧 tick，持续发送最新状态 | 不排队历史 heartbeat；记录 last_attempt/last_success |
| command poll | 独立单线程 worker | poll token capacity 1 | 合并重复 poll tick | 命令按 control-plane cursor 串行获取 |
| command apply | node actor 本地 mailbox | capacity 128 | queue 达 80% 进入 degraded；满载立即 HALT | 按 command sequence 串行；command_id 去重 |
| command ACK | 独立 worker + durable outbox | memory capacity 256，disk spool 有字节上限 | 写入 disk spool；spool 达 80% degraded，满载 HALT | 至少一次发送，control plane 按 command_id 幂等 |
| intent fetch | 独立单线程 worker | poll token capacity 1 | delivery queue 达 80% 时暂停拉取 | cursor 只在本地接收并持久化后推进 |
| intent delivery | node actor 本地 mailbox + durable inbox | capacity 256，disk inbox 有字节上限 | 暂停 fetch；过期 intent 按契约 ACK；inbox 满载立即 HALT | `RECEIVED -> DISPATCHED -> EXCHANGE_CONFIRMED/REJECTED`；不确定结果先查交易所 |
| strategy durable I/O | 独立单线程 worker + actor result mailbox | task/result capacity 256 | queue 满、fsync 失败或 deadline 超时立即 sticky HALT | actor callback 只 enqueue；fsync 后 continuation 回 actor；management 以 durable terminal 状态收口 |
| event egress | 独立 worker + durable spool | memory capacity 1024，disk spool 64 MiB 初始上限 | memory 满转 disk；disk 达 80% degraded，满载 HALT | event_id 幂等、批量发送、ACK 后删 spool |

每条 lane 使用独立 timeout、retry budget、circuit state、metrics 和 progress timestamp。
lane 之间不共享 thread pool，不允许 heartbeat 被 intent long poll、ACK retry 或 event backlog
占用。

初始容量是明确的运维参数，必须通过压力测试校准。容量修改进入 release manifest 和 config
hash，禁止运行时静默漂移。

### Actor Durable Continuation Barrier

- `_on_intent_msg()`、`on_data()`、order/protection callback 和 management callback 只更新
  actor 内存或 enqueue bounded task。HTTP、文件锁、fsync、Redis 和 exchange refresh
  全部运行在独立 lane。
- 开仓与加仓采用 `RECEIVED -> durable PREPARE -> actor submit`。PREPARE 同时持久化
  intent dispatch、single-use canary claim 和 protection snapshot。
- PREPARE 完成回到 actor 后再次读取实时 `trading_state`。状态已进入 HALTED/REDUCING
  时拒绝 risk-increasing plan；reduce-only/cancel 路径继续可用。
- strategy 设置 stopping latch 后取消 timers、停止 worker 并丢弃 mailbox continuation。
  stopped worker 禁止被 callback 自动重启。
- durable lane 进入 sticky HALT 后，晚到 continuation 禁止触发 submit、retry、fallback
  或 management side effect；PREPARE 的 actor preimage 会恢复。
- management 在首个 replacement/cancel side effect 前持久化 `DISPATCHED` operation marker；
  terminal cancel 和最终 protection snapshot fsync 完成后持久化
  `EXCHANGE_CONFIRMED`。重启看到 `DISPATCHED` 且无法证明 terminal result 时保持
  fail-closed，禁止重复执行。

### Timeout And Retry

- 每次网络调用必须有 connect/read/total deadline。
- 每条 lane 同时设置 hard deadline。hard deadline 到期后将
  `process_liveness=false`，触发单次 fatal process termination，由 orchestrator
  重启到 HALTED。
- retry 使用 bounded exponential backoff 与 jitter。
- heartbeat 始终优先发送最新状态，不回放历史 heartbeat。
- command ACK 和 execution event 使用 durable outbox；重试跨进程重启保留。
- command journal 恢复出的 pending ACK 在 session 启动后直接进入 ACK lane，
  发送成功后按 `command_id` durable 删除。
- intent fetch timeout 只影响 intent lane。intent queue backpressure 主动停止新 fetch。
- 连续 timeout 打开该 lane circuit，其他 lane 继续运行并报告精确 degraded reason。

### Watchdog And Health

增加独立于 Nautilus actor event loop 的 watchdog。watchdog 读取单调时钟上的 progress
timestamps，不执行控制面网络调用。

`SessionHealth` 至少包含：

- `process_liveness`：进程与 watchdog tick；
- `actor_tick_age_ms`：Nautilus actor 最近一次轻量 tick；
- 每条 lane 的 `last_attempt_at`、`last_success_at`、`in_flight_age_ms`、
  `queue_depth/capacity`、`circuit_state`；
- Redis ping/write、reconciliation、projection 和 exchange mirror freshness；
- release identity 与 peer consistency。

默认判定：

- actor tick age > 15 秒：readiness=false，创建 incident，进入 HALTED；
- heartbeat success age > 15 秒：control-plane readiness=false，进入 HALTED；
- actor tick age > 60 秒：watchdog 触发进程退出，由 orchestrator 重启到 HALTED；
- 任一 lane in-flight age 超过该 lane hard deadline：立即标记 process dead，
  触发进程退出；阻塞线程的存活状态不再代表进程健康；
- queue 达 80%：degraded + alert；queue 满或 durable spool 写失败：HALTED；
- frozen DB heartbeat payload 无论内容为何，超过 freshness threshold 都判 unavailable。

阈值必须配置化并写入 release manifest。生产初始值采用上述值，chaos test 验证后调整。

### Readiness And HALTED Semantics

状态拆成四个正交维度：

1. `process_liveness`
2. `dependency_readiness`
3. `reconciliation_health`
4. `trading_state`

语义：

- dependency failure、queue overflow、watchdog stall、release drift、Redis write failure 会将
  `trading_state` 转为 sticky `HALTED`。
- 依赖恢复只更新 readiness；trading state 继续 HALTED。
- `RESUME` 需要显式 operator command、有效审计身份、fresh heartbeat、健康 reconciliation、
  无 P0/P1 incident、release gate PASS。
- HALTED 允许 cancel、reduce、close 等风险降低动作；open/add 始终拒绝。
- watchdog 自动重启进程时，节点启动默认 HALTED，并执行 exchange-first reconciliation。
- readiness endpoint 同时返回状态值与年龄，禁止只返回布尔值。

### Redis Namespace And Retention

生产 Redis persistence 同时使用两个 identity：

```text
lease_namespace       = trader-{trader_id}
persistence_instance  = acquisition_uuid4
cache_namespace       = trader-{trader_id}:{persistence_instance}
message_bus_namespace = trader-{trader_id}:{persistence_instance}:{streams_prefix}
```

决策：

- `lease_namespace` 是执行账户的稳定互斥资源。进程 owner、release ID 与单调
  `fencing_token` 共同决定当前 owner。
- 当前 writer identity 由
  `{redis_fencing_epoch, runtime_generation, lease_fencing_token}` 构成。
  heartbeat、intent fetch/ACK、command poll/ACK 和 execution event 全部携带该 identity；
  control plane 对每次 node 请求统一校验，旧 writer 收到 409 并触发本地 fatal fence。
- 每次 acquisition attempt 由进程生成独立 UUID4 candidate；Lua 仅在成功获取 lease 时
  原子固化该 candidate，同一 attempt 的重试复用 candidate。`fencing_token` 承担当前
  Redis 历史内的顺序 fencing，UUID4 承担 counter 丢失、旧备份恢复和 Redis 替换后的
  generation 隔离。
- acquire、refresh、stale takeover 和 registry score 全部使用 Lua 内的 Redis `TIME`。
  客户端 wall clock 不参与 owner freshness 判定，时钟偏差无法触发提前抢占或未来 score。
- live cache 与 message bus 设置 `use_instance_id=True`，并将该 UUID4 同时传给
  `TradingNodeConfig.instance_id`。testnet 保持无 generation 的稳定本地配置。
- live 节点在构造 `TradingNodeConfig` 与 Nautilus `TradingNode` 前获取 namespace
  fencing lease。
  Nautilus 1.227.0 会在 `TradingNode(...)` 构造期间创建 Redis message bus/cache 并执行
  `load_cache()`；lease 获取失败会在任何 Redis cache 读写发生前终止启动，后续 assembly
  失败会释放 startup lease。
- 旧 owner 与 replacement owner 使用不同 generation。旧进程在暂停超过 lease freshness
  后恢复，只能继续写旧 generation，无法污染 replacement 的 cache 或 stream。
- lease metadata 的 `namespace` 字段记录逻辑 `lease_namespace`，并同时记录精确
  `persistence_instance_id` 与 `persistence_namespace`。refresh、remove、janitor 和发布
  证据必须验证三者一致。janitor apply 拒绝缺少任一 generation 字段的 legacy metadata；
  legacy metadata 只允许 dry-run 审计。
- lifecycle 使用单调时钟维护本地 lease freshness，初始阈值 120 秒，早于 Redis
  takeover freshness 300 秒。strategy 每次处理 intent 都读取 lifecycle trading state；
  stale process 恢复后会先 sticky HALT，再进入任何新增风险提交。
- active runtime 写入带 freshness score 的
  `trader-bot:redis-namespaces:active` registry；metadata hash 使用同 key 的
  `:leases` 后缀。
- message bus 开启 `autotrim_mins`，初始 retention 为 24 小时；同时设置每 stream
  max entries/bytes guard。
- Postgres execution events 和 durable local spool 承担审计与恢复，Redis streams 只承担
  有界实时传输。新 generation 从 exchange-first reconciliation 重建状态，旧 generation
  不承担恢复真相源职责。
- Redis 配置显式 `maxmemory`，HK 8 GiB 主机目标为 512 MiB，保留
  `noeviction` 以维持 fail-closed；container memory limit 为 640 MiB。
- A-D 节点各使用 448 MiB、1 CPU 的有限配额。Redis capacity plan 为四节点
  和其他常驻服务保留 2304 MiB，并为 OS 保留至少 3 GiB。
- bootstrap 串行启动节点；每个节点达到 ready+HALTED 后，宿主机
  `MemAvailable` 必须仍不少于 3 GiB。
- migration 后的自动恢复复用相同的 `/version`、cgroup peak、OOM/restart 和
  3 GiB `MemAvailable` 门禁；启动资源证据仅记录通过项，并在成功或失败收尾时
  以 `0400` 权限纳入备份校验和。
- 告警阈值采用 60%/75%/85%；写失败或 85% 持续超窗触发 HALTED 和容量 incident。
- janitor 默认 dry-run。apply safety manifest 对每个执行账户同时携带稳定
  `lease_namespace` 和精确 `persistence_namespace`；前者验证当前 lease owner，后者保护
  活跃 generation。执行前创建 RDB/AOF 备份；分批 `UNLINK`；每批前重新读取 lease
  metadata；每批后验证 key count、memory 和节点 reconciliation。
- legacy random UUID 与已退休 fenced generation 只有在 replacement 完成 exchange-first
  reconciliation、满足 idle threshold 且 safety manifest 仍指向其他活跃 generation 后
  才可删除。

### Immutable Release Identity

生产节点使用不可变 image digest，应用代码、依赖 lock 和 migration manifest 全部进入镜像。
生产禁止 bind-mounted source hotpatch。配置和 secret 可以外置，配置必须生成 hash。

每个节点暴露 `/version`：

```text
git_sha
image_digest
build_id
dependency_lock_sha256
config_sha256
schema_epoch
started_at
```

release identity 同时写入 heartbeat。A/B gate 要求除 account-specific config hash 外的
identity 完全一致。`DEPLOYED_COMMIT.txt` 只保留为人类备注，不参与真实性判定。

emergency rollback 需要临时 bind mount 时：

- 只允许 inode-preserving 写入；
- 部署后立即 restart/recreate 全部相关容器；
- 检查宿主 hash、容器内 hash、mount source/destination、`//deleted`、进程启动时间；
- 节点全程保持 HALTED；
- hardening 正常 rollout 仅接受 `immutable_image` 与 control-plane isolation `require`。

### Live Risk Migration And Canary Authority

生产节点 JSON 的 `risk` 当前为空，旧容器通过
`NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON` 为 BNB/BTC/ETH/SOL 四个 instrument 配置
`100 USDT` 上限。submit/modify rate 环境变量缺失时，应用有效默认值分别为
`50/00:00:01` 和 `1/00:00:01`。

发布使用 `trader-v3-live-risk-policy/v2`：

- 同时读取 `/srv/trader-v3/node-a.hk.json`、`/srv/trader-v3/node-b.hk.json` 与两节点
  容器环境，要求所有可信来源一致。
- 将既有四个 `100 USDT` cap 和有效默认 rate 原子写入 A/B JSON，发布失败时从私有备份
  回滚两份配置。
- migration ceiling 固定为 `100 USDT`，禁止借发布扩大现有风险额度。
- recreate 计划移除旧风险环境变量，运行时只读取 release-bound JSON。

Nautilus 1.227.0 `RiskEngine` 对 margin account 的 order submission 会在
`max_notional_per_order` 检查前返回。Binance futures canary 的权威额度由控制面单次 permit、
data client permit 校验、strategy 实际名义金额校验、deterministic client order ID 和 durable
single-use store 共同执行。Nautilus risk config 保留为现有策略迁移与额外防线，不承担
canary `12 USDT` 硬上限的授权职责。

permit downlink 包含绝对 `expires_at`，control plane、data client 和 strategy 均使用
可信 UTC 时钟校验。canary 持仓建立后，独立风险 worker 持续读取新鲜 mark price，
按实际 filled quantity、fees 和 realized/unrealized PnL 计算累计净亏损。达到
`1.5 USDT` 或 mark stale 时立即 HALT，并按交易所确认数量执行精确 reduce-only close。
该监控不依赖后续 fill callback。

### Rollout Gates

1. **Build gate**：单元、集成、Redis 重启恢复、queue overflow、timeout、stale heartbeat、
   sticky HALTED 和 release identity 测试 PASS。
2. **Fault gate**：分别注入 control-plane latency、intent timeout、ACK failure、Redis
   latency/write failure、actor freeze、process restart；验证 lane 隔离和 watchdog。
3. **Capacity gate**：连续 replacement 产生互相隔离的 generation；janitor 使 retired
   generation 数量与 key count 收敛；24 小时 streams 收敛；Redis 在目标上限内。
4. **Pre-deploy gate**：整个 Redis、Postgres migration、control-plane isolation、
   rollout 和 permit 流程持有同一个非阻塞 operation lock；备份 Redis/Postgres/manifest；
   `pg_dump` 使用 deadline、SHA256 和 restore/list validation；审计 HALT 两节点。无法 ACK
   的冻结节点执行 fail-closed stop。
5. **Canary gate**：先发布 `account-a`，启动保持 HALTED；验证 `/version`、无 deleted inode、
   heartbeat/tick、exchange-first reconciliation、orders/positions 对齐。
6. **Soak gate**：HALTED canary 观察至少 30 分钟，并执行 control-plane timeout 与 Redis
   短时故障验证。
7. **Emergency-close gate**：同 release、同 order shape 的 emergency reduce-only close
   路径完成测试网验证，证据绑定 release ID、symbol、position side 和实际 filled quantity。
8. **Account-a canary authority gate**：rollout phase 为 `account_a_canary` 时，
   仅 `account-a`、单个 fresh writer、有效 single-use permit、测试网 emergency-close
   证据和完整健康矩阵可以获得受限 RESUME。常规 RESUME 继续要求 `fleet_complete`。
9. **Live gate**：由审计执行器机械化
   `permit -> restricted RESUME -> intent -> exact close -> HALT -> evidence`，并按
   `docs/agent-team/account-stall-hardening-live-trade-policy.md` 完成单次小额 round trip，
   开仓使用显式 quantity/price 的 `LIMIT + IOC`，精确 reduce-only 平仓并确认目标 symbol
   flat、目标普通/algo orders 为零、非目标 `portfolio_baseline_sha256` 不变。执行器在机器
   层硬绑定 `account-a`、`SOLUSDT`、`12 USDT` 和累计净亏损 `< 1.5 USDT`。
10. **Fleet gate**：canary PASS 后发布 `account-b`，成功后 rollout phase 进入
    `fleet_complete`；再次检查 A/B release identity、
   account-a 目标 symbol 安全状态、签名的非目标组合基线和健康矩阵。
11. **Cleanup gate**：fenced generation 运行验证完成后，执行 legacy 与 retired
    generation janitor。
12. **Close gate**：两节点回到期望 trading state，证据包包含 version、metrics、commands、
    reconciliation、trade、目标 symbol flat/zero-orders、非目标组合基线和 rollback 验证。

任一 gate 失败都会保持 HALTED，并执行上一不可变 image digest 的 rollback。

## Consequences

- actor event loop 不再承载外部网络等待，单个 endpoint timeout 只降级对应 lane。
- heartbeat、command 和 intent 拥有独立可观测进度，冻结 payload 无法伪装健康。
- bounded queues 将无限等待转换为可检测的 backpressure 和明确 HALTED。
- fenced generation 消除 stale owner 的共享写入窗口；retention、janitor 和容量门让
  generation 生命周期与 Redis 容量保持有界。
- immutable release identity 消除“同镜像标签、不同运行字节”的状态。
- 模块增加 worker、spool、watchdog 和 metrics 实现成本；复杂性集中在一个可测试 seam，
  节点调用侧 interface 保持小。

## Rejected Alternatives

### 继续提高 HTTP timeout

更长 timeout 会延长 actor callback 占用时间，无法隔离 heartbeat、command 和 intent。

### 共享线程池处理全部控制面任务

共享 pool 在 intent long poll、event backlog 或 ACK storm 下仍会耗尽，heartbeat 缺少资源保留。

### 只清理 Redis

清理能恢复容量，runtime UUID namespace 与无 retention 会再次增长；同步 actor I/O 仍会在下次
网络抖动中冻结。

### readiness 恢复后自动 ACTIVE

自动 ACTIVE 会绕过事故确认、exchange reconciliation 和 release identity gate。sticky HALTED
提供可审计恢复点。

### 继续以 bind-mounted hotpatch 作为常态发布

bind mount 的 inode 与容器生命周期允许宿主文件、运行字节和 A/B 节点分叉，无法形成可靠
release identity。

## Implementation Order

1. `NodeControlPlaneSession` lane 隔离与 bounded queues。
2. watchdog、health schema、sticky HALTED tests。
3. fenced Redis generation、retention、capacity limit、dry-run janitor。
4. immutable image 与 `/version`，A/B release gate。
5. Redis/Nautilus integration 与 fault matrix。
6. HALTED canary、真实小额交易、fleet rollout、legacy namespace cleanup。

## 2026-08-20 Decision Amendment

### Stable Node Lease Takeover

- live lease `owner` 使用稳定 `node_id`，`release_id` 继续标识具体发布。
- 同一 `node_id` 的旧进程租约超过 300 秒 freshness window 后，新 release 可以接管；
  Redis Lua 原子递增 fencing token，并为新 persistence generation 分配独立 UUID。
- 不同 `node_id` 的陈旧租约返回 `STALE_FOREIGN`。跨身份 takeover 保持 fail-closed。
- 新鲜租约拒绝 takeover。120 秒本地 lease freshness 继续早于 300 秒 Redis takeover
  window，将旧进程先置为 sticky HALTED。
- 升级兼容窗口严格接受旧版本
  `node_id:hostname:pid:uuid` owner 格式，且只在记录陈旧时接管；新记录统一写回稳定
  `node_id`。其他 owner 格式继续按 foreign identity 处理。

### Projection Progress Alert

- execution projection 每 60 秒向 worker queue 投递 progress probe。
- egress 连续 600 秒没有完成 flush 或空闲 egress cycle 时，记录
  `projection_progress_stall` incident，并在 readiness provider 暴露 stalled 状态。
- egress 恢复 progress 后关闭同 reason incident。启动回滚和正常停止负责终止 watchdog。

### Temporary Ownership Isolation

- live Binance instrument provider 和 Nautilus
  `reconciliation_instrument_ids` 共同使用 release-bound
  `risk.max_notional_per_order` inventory。
- live execution projection 只接收 inventory 内的 instrument；order event 同时要求机器人
  client order ID，`*-EXTERNAL` position event 被丢弃。
- continuous reconciliation 的 order/position report 和 startup mass status
  逐 owned `InstrumentId` 发请求；Binance adapter 的 targeted order/fill query
  只访问请求 symbol，不并入缓存中的其他 active symbol。
- 临时策略把 inventory symbol 视为机器人独占 book。发布 gate 要求操作员停止在这些
  symbol 上进行手工交易，直至 position-side ownership ledger 上线。
- 最终所有权策略以 `(account_id, instrument_id, position_side)` 为仓位 book 主键，
  以机器人 client order ID、intent lot ledger 和 venue position evidence 证明 ownership。
  推断成交只允许更新具备机器人 ownership proof 的仓位；无 proof 的差异进入
  `unattributed_qty` 和人工对账流程。
- 该临时策略收窄权重和污染范围；同一 inventory symbol 内的手工活动仍属于发布阻断项。
