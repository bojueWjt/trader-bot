# 2026-08-08 Account Node Stall / Redis Growth / Release Drift

- 状态：Historical forensics complete；当前代码 residual stall 机制待 Phase A 重诊
- 取证截止：2026-08-08 06:38 UTC
- 本文“事实”章节描述该取证时刻的生产基线；工作区修复状态单列记录
- 影响账户：`account-a`、`account-b`
- 关联 ADR：`docs/adr/2026-08-08-account-node-stall-hardening.md`
- 时间口径：正文统一使用 UTC；Git 原始提交时间同时保留 commit hash

## 结论

取证结论使用两个时间定义：

- **最早可证明的同类 poller/node stall 迹象**：2026-07-02 14:18:15 UTC。生产
  `operator_commands` 记录 `revive intent poller`；同日 18:39:12 UTC 又记录
  `nodes wedged after control-plane restart; restoring node-a to ACTIVE (prior state)`。
- **首次显式命名 `poller freeze` 的事件**：2026-07-07。对应恢复留痕在
  2026-07-08 06:49:06 UTC 写入 `operator_commands`，reason 为
  `nodes restarted after poller freeze during control-plane deploy 2026-07-07; resume trading per Balen`。

本轮历史生产事故存在三个相互增强的故障域：

1. **历史生产直接执行根因**：旧生产运行字节在 Nautilus actor timer callback 内同步
   执行控制面 HTTP。
   `urlopen(..., timeout=5)` 阻塞 actor 调度线程，intent、heartbeat、command poll/ACK
   和 actor tick 共享失败命运。`account-b` 日志在 2026-08-04 13:47:57 UTC 和
   2026-08-05 10:19:38 UTC 给出了完整阻塞栈。
2. **资源放大根因**：`81f356a` 启用了 cache/message bus 的
   `use_instance_id=True`，message bus 没有 retention。Nautilus 实际使用每次启动变化的
   runtime UUID，产生永久新 namespace 和无界 streams。2026-08-08 Redis 已达
   191,847 keys / 6.08 GiB，`maxmemory=0`、`noeviction`，主机进入 swap 与 I/O 压力区。
3. **发布一致性根因**：生产依赖 bind-mounted hotpatch。宿主文件原子替换后，
   `account-b` 继续绑定 deleted inode，A/B 六个关键文件哈希全部不同。
   `DEPLOYED_COMMIT.txt` 只记录 `6ecad66`，无法表达真实运行字节。

Redis bug 对本次 stall 有直接影响链。同步 actor I/O 是节点停顿的立即触发机制；
Redis namespace/stream 无界增长持续制造内存、swap 和 I/O 压力，提高 HTTP、Redis 和
交易所请求超时概率；发布漂移使 `account-b` 长期保留同步阻塞实现，最终从间歇超时演化为
永久停更。

当前工作树已经将相关外部 I/O offload 到专用 executor/worker。该实现状态与上述历史
生产根因属于不同时间切片。当前 residual stall 机制尚未由 fault-injection 复现，Phase A
将重新检查 executor 饱和、队列背压、shutdown 和多 worker 交互。

## 已证实时间线

| 时间 UTC | 事件 | 证据 |
|---|---|---|
| 2026-06-19 21:08:21 | 引入 Redis cache/message bus 持久化；cache 与 bus 均设置 `use_instance_id=True`，bus 未配置 `autotrim_mins` | commit `81f356a9ff1c01aec570a9fb814fde1f72951dcf`；`services/nautilus-node/persistence/nautilus_config.py:31-49,106-137`；`services/nautilus-node/persistence/RECOVERY.md:25-42` |
| 2026-06-20 14:09:34 | `CommandPollerActor` 接入 2 秒 timer；timer callback 直接调用同步 `poll_commands` 和 `ack_command` | commit `58e80cba2d56c3e2a1eee2f18d9387bdb79c3c46`；`services/nautilus-node/app/nautilus_actors.py` |
| 2026-06-21 02:10:26 | heartbeat 被并入 `CommandPollerActor.poll_once`，heartbeat、command poll、ACK 形成同一串行故障域 | commit `6096c00d62d9533b754799a4731ad9fabf9cfb70` |
| 2026-07-02 14:18:15 | 最早可证明的同类 poller/node stall 迹象；执行 RESUME，reason=`revive intent poller` | 生产 PostgreSQL `operator_commands.created_at/reason`，2026-08-08 只读取证 |
| 2026-07-02 18:39:12 | 双节点在 control-plane restart 后 wedged | 生产 `operator_commands`，reason=`nodes wedged after control-plane restart; restoring node-a to ACTIVE (prior state)` |
| 2026-07-07 | 首次显式命名 `poller freeze`；control-plane deploy 期间冻结，节点通过 restart 恢复 | 2026-07-08 06:49:06 的 `operator_commands`：`nodes restarted after poller freeze during control-plane deploy 2026-07-07` |
| 2026-07-08 17:34 | host reboot 再次冻结 node actors | 2026-07-09 07:09:35 的 `operator_commands`：`host reboot froze node actors` |
| 2026-07-10 | order amnesia / silent HALT 事故确认 random runtime UUID namespace，Redis 已积累 11.7 万孤儿 key；该项留作 P2 | `docs/incidents/2026-07-10-node-order-amnesia.md:14-31,81-84`；修复 commit `6f9564d61016b3ccb48388273f2414997ffb2a56` |
| 2026-07-24 02:51 起 | 双节点僵死约 7 小时，心跳表残留 `readiness=true`，监控未识别冻结时间 | commit `a5998bb723af3169fa5680538fc74f18bdb7461c`；`scripts/hk-root-window-20260724.sh:39-44` |
| 2026-07-24 | Redis 3.7 GiB、主机无 swap 的资源事故被写入部署内存闸门 | commit `002c706d818fca4fa80947f01e0f55d365be8653`；`scripts/hk-root-window-20260724.sh:35-48` |
| 2026-07-24 | hotpatch 未挂载、运行版本无法由 commit 表达的问题被固化为部署基线 | commit `6252484`；`docs/runbooks/2026-07-24-hk-deployment-baseline.md:3-6,33-63,185-214` |
| 2026-08-03 08:26-08:44 | `account-a` intent 消费游标因 schema 毒丸停滞 18 分钟；证明消费进度缺少独立 watchdog | `docs/plans/2026-08-03-five-day-work-review.md:20-25` |
| 2026-08-03 | Redis 5.48 GiB、88 个僵尸 namespace、free 155 MiB、swap 5.7/8 GiB | `docs/plans/2026-08-03-five-day-work-review.md:39-42` |
| 2026-08-04 13:47:57 | `account-b` 的 `IntentPublisherActor` timer callback 阻塞于 `urlopen`，抛 `TimeoutError` | 生产 `trader-v3-node-b` Docker log；栈指向 `/app/app/nautilus_actors.py:184`、`approved_intent_client.py:124`、`http_client.py:215` |
| 2026-08-05 10:19:38 | `account-b` 同一同步 callback 超时再次发生 | 同上 |
| 2026-08-06 04:49 起 | `account-a` 周期性出现 heartbeat HTTP timeout | 生产 `trader-v3-node-a` Docker log |
| 2026-08-06 12:40:27 | `account-b` 最后一笔数据库 heartbeat；表中仍残留 `ACTIVE/readiness=true` | 生产 `node_heartbeats` |
| 2026-08-06 12:40:39 | `account-a` heartbeat、command poll、intent poll 在同一秒全部 timeout，并进入 sticky HALTED | 生产 node-a log，halt reason=`control_plane failed: control-plane heartbeat stale` |
| 2026-08-08 06:38 | `account-a` heartbeat 已恢复且保持 HALTED；`account-b` heartbeat 仍冻结在 8 月 6 日 | 生产 `node_heartbeats` |

## 事实

### 历史生产 Actor 同步阻塞

- `HttpControlPlaneClient._request_json` 使用同步
  `urllib.request.urlopen(request, timeout=self._timeout_seconds)`：
  `packages/execution-domain/execution_domain/http_client.py:191-229`。
- `ApprovedIntentDataClient.poll_once` 同步调用 `fetch_intents`：
  `services/nautilus-node/data_client/approved_intent_client.py:123-132`。
- `IntentPublisherActor._on_poll_timer` 在 actor timer callback 内直接执行
  `poll_once`；`CommandPollerActor` 同样在 timer callback 内串行执行 heartbeat、
  command poll 和 ACK。引入 lineage 为 `58e80cb`、`6096c00`。
- 生产 `account-b` 的 Python traceback 完整穿过上述调用链，终点为 socket
  `recv_into` 超时。

### Redis 无界增长

- `81f356a` 同时在 cache 与 message bus 打开 `use_instance_id=True`。
- 2026-07-10 事故文档已证明配置里的 `INSTANCE-ACCOUNT-A` 没有进入实际 key；
  runtime UUID 每次启动变化，并记录 11.7 万孤儿 key。
- 2026-08-08 生产 Redis：
  - Redis 7.4.9；
  - 191,847 keys；
  - `used_memory_human=6.08G`；
  - `maxmemory=0`；
  - `maxmemory-policy=noeviction`；
  - account-a 117,904 keys / 67 个 runtime UUID namespace；
  - account-b 73,943 keys / 42 个 runtime UUID namespace。
- 2026-08-08 06:38 UTC 的生产运行字节仍使用两个
  `use_instance_id=True`，并且没有 `autotrim_mins`。
- `infra/compose/multi-account.sandbox.yml:4-8` 只启用 AOF，没有 Redis 内存上限和
  retention 参数。

### 当前工作区修复状态

- live cache 与 message bus 已改为 fenced generation 模式。稳定
  `trader-{trader_id}` 承担 lease resource；每次成功 acquisition 原子固化独立 UUID4，
  `TradingNodeConfig`、cache 与 message bus 使用同一 generation。fencing token 负责
  顺序 owner fencing，UUID4 负责跨 Redis 回滚周期的 generation 隔离。
- lease freshness 与 takeover cutoff 统一使用 Lua 内 Redis `TIME`，客户端时钟偏差不会
  提前抢占或写入未来 freshness score。
- testnet 保持 `use_instance_id=False`；live 设置 `use_instance_id=True`。
- message bus 已配置默认 1440 分钟 `autotrim_mins`。
- live 节点会在 Nautilus `TradingNodeConfig` 与 `TradingNode(...)` 构造前获取 fenced
  namespace lease。
  Nautilus 1.227.0 在构造期间创建 Redis message bus/cache 并执行 `load_cache()`，当前顺序
  保证 replacement 先取得新 generation。暂停超过 freshness 的旧 owner 恢复后只能写旧
  generation，无法污染 replacement keyspace。
- fenced namespace lease、原子 janitor 和 Redis 容量计划已实现，生产接线与 canary
  验证属于发布门禁。janitor apply 强制 live lease metadata 包含完整 UUID4 generation
  证据；legacy metadata 仅进入 dry-run 审计。
- intent、protection 和 management actor callback 已改为 bounded durable-I/O lane。
  actor 只 enqueue；worker 完成 inbox/protection fsync 后由 actor mailbox continuation
  执行 exchange side effect。
- PREPARE continuation 会再次检查实时 trading state。operator HALT 发生在 fsync 与
  submit 之间时，risk-increasing order 保持零提交；strategy stopping 或 durable sticky
  HALT 后的晚到 continuation 会被丢弃，stopped worker 无法由 timer 复活。
- management intent 在 replacement/cancel 前持久化 `DISPATCHED` operation marker，
  terminal cancel 与最终 protection snapshot fsync 后持久化终态。清空进程内去重集合后
  重放保持零 replacement、零 cancel。
- Redis rebaseline、control-plane isolation 与 systemd resource drop-in 已统一使用
  `/var/lock/trader-v3-account-stall-operation.lock`，并增加 bounded memory、zero swap、
  CPU/tasks/file descriptor 预算。当前仅完成离线测试，尚未安装到生产。

### 生产风险配置迁移事实

- `/srv/trader-v3/node-a.hk.json` 与 `/srv/trader-v3/node-b.hk.json` 的 `risk` 当前为空。
- 两个旧容器仅设置 `NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON`，BNB/BTC/ETH/SOL 四个
  instrument cap 均为 `100 USDT`。
- submit/modify rate 环境变量缺失，应用有效默认值为 `50/00:00:01` 和
  `1/00:00:01`。
- 工作区 `trader-v3-live-risk-policy/v2` 会捕获上述有效配置，原子迁移到 A/B JSON，
  限制 migration ceiling 为 `100 USDT`，并在失败时回滚两份配置。
- Nautilus 1.227.0 `RiskEngine` 对 margin account 在
  `max_notional_per_order` 检查前返回。Binance futures canary 的 `12 USDT` 上限由
  release-bound 单次 permit、data client/strategy 双重实际名义金额校验、确定性 client
  order ID 和 durable single-use store 执行。

### 发布漂移

- 2026-07-24 基线已记录线上 hotpatch 分叉、漏挂载补丁和失真的
  `DEPLOYED_COMMIT.txt`。
- 2026-08-08 两节点镜像 digest 相同：
  `sha256:19f6f9b78c9df4c94cc1e2bc502b5429729bf3de6e9390105abbf210ac732f83`。
- A/B 的 `nautilus_actors.py`、`node.py`、`lifecycle.py`、`control_plane.py`、
  `http_client.py`、`approved_intent_client.py` 容器内 sha256 全部不同。
- `account-b` mountinfo 显示五个关键 bind source 带 `//deleted`：
  `nautilus_actors.py`、`control_plane.py`、`node.py`、`lifecycle.py`、
  `http_client.py`。这证明容器继续持有原子替换前的 inode。
- `scripts/hk-deploy-20260803.sh:170-207` 已采用 inode-preserving 写入并做双节点
  容器内 hash 校验；线上后续手工热补丁绕过了完整 release identity。

## 推断

以下结论由多项事实共同支持，保留“推断”标签：

1. **Redis 是 stall 的强促成因素。** 6.08 GiB Redis 数据集、接近耗尽的 RAM 和大量
   swap 会显著增加调度、网络和磁盘延迟；这些延迟落入同步 actor callback 后，会冻结
   控制面关键路径。当前证据支持强因果链，缺少逐秒 host profiler 将某一次 timeout
   唯一归因到 Redis。
2. **`account-b` 的永久冻结由旧运行字节放大。** deleted inode 与 A/B hash 差异证明
   B 没有运行 A 的后续异步/容错 hotpatch；B 的 8 月 4/5 traceback 也显示同步旧路径。
3. **`readiness=true` 是冻结快照。** heartbeat 停更后 DB payload 不会自动变成 false，
   因此健康判定必须同时检查值、年龄和独立进程 tick。
4. **7 月 10 日将 Redis 问题降为 P2 延长了风险窗口。** 当时对账修复降低了订单失忆的
   行为影响，namespace 和 stream 的容量增长继续存在，并在 7 月 24 日、8 月 3 日和
   8 月 8 日形成递增资源证据。

## Redis Bug 影响链

```text
81f356a: use_instance_id=True + bus 无 retention
  -> 每次重启生成新的 runtime UUID namespace
  -> cache keys 和 instrument/message streams 永久留存
  -> 7/10: 11.7 万孤儿 key
  -> 7/24: Redis 3.7 GiB，8 GiB 主机资源耗尽
  -> 8/03: Redis 5.48 GiB，swap 5.7/8 GiB
  -> 8/08: Redis 6.08 GiB，191,847 keys
  -> 内存回收、swap、AOF 与 I/O 延迟持续升高
  -> 同步 actor HTTP/Redis/交易所调用更易超过 timeout
  -> actor tick、intent、heartbeat、command/ACK 一起停顿
  -> heartbeat 数据冻结且残留 readiness=true
  -> watchdog 延迟发现；节点保持旧 ACTIVE 快照或进入 sticky HALTED
```

## 根因分类

| 分类 | 结论 | 置信度 |
|---|---|---|
| 历史直接根因 | 旧生产运行字节的外部同步 I/O 运行在 Nautilus actor callback，多个控制面职责共享一个调度故障域 | 已证实 |
| 历史直接根因 | Redis namespace 与 streams 没有稳定 identity 和 retention | 已证实 |
| 当前残余机制 | 当前工作树已 offload 外部 I/O；executor/queue/shutdown 是否仍可造成 stall | 待 Phase A 复现 |
| 促成因素 | Redis 无内存容量边界，主机长期在低可用内存和高 swap 下运行 | 已证实 |
| 促成因素 | heartbeat、command、ACK、intent 缺少独立 lane、bounded queue 和独立 progress clock | 已证实 |
| 促成因素 | 健康检查读取冻结 payload，缺 heartbeat age 与 actor tick age 的组合语义 | 已证实 |
| 促成因素 | bind-mounted hotpatch 与原子替换造成 deleted-inode release drift | 已证实 |
| 促成因素 | 7/10 已识别 Redis orphan keys，容量治理保持未完成 | 已证实 |
| 影响判断 | Redis 压力提高 timeout 概率，并把同步回调缺陷从间歇故障放大为长时 stall | 强推断 |

## 当前安全语义

- `account-a` 在控制面恢复后仍保持 HALTED，这符合 fail-closed 和显式 RESUME 语义。
- `account-b` 的 `ACTIVE/readiness=true` 是 2026-08-06 12:40:27 UTC 的冻结快照，
  不能作为可交易证据。
- readiness 恢复只代表依赖重新健康；交易状态必须保持 sticky HALTED，直到完成版本、
  对账、Redis、lane liveness 和 operator audit gates。

### 2026-08-08 21:00 UTC 只读安全复核

- account-a 容器已运行 2 天，数据库 heartbeat 新鲜且状态为 HALTED。
- account-b 容器于 2026-08-08 07:26:57 UTC 以 exit 137 退出。2026-08-10
  复核确认 `OOMKilled=false`，dockerd 记录
  `hasBeenManuallyStopped=true` 与 `restart canceled`，说明该退出来自手工
  stop/kill 路径，`unless-stopped` 因此没有拉起容器。数据库 heartbeat 仍冻结在
  2026-08-06 12:40:27 UTC 的 ACTIVE。
- 2026-08-10 已显式将 account-b 的 BTCUSDT/ETHUSDT/SOLUSDT risk state
  置为 HALTED，撤销一笔 2026-06-30 创建的 SPCXUSDT opening order，并用新鲜
  exchange read 证明仓位、普通订单和 algo 订单全部归零。
- `/v1/nodes` 现保留 reported state，同时输出
  `operational_state=OFFLINE`、`heartbeat_stale=true`、
  `effective_readiness=false` 和 `admission_eligible=false`；decision gateway
  freshness 已按 account 作用域计算。
- Redis 为 191,849 keys / 6.14 GiB，`maxmemory=0`、`noeviction`，
  fencing epoch marker 仍缺失。
- 主机可用内存 384 MiB，swap 使用 7,430/8,191 MiB。
- PostgreSQL 最新 migration 仍为 `0009_trade_outcome_job_runs`。
- Production Gate 继续 FAIL，节点和真实交易保持冻结。

### 2026-08-08 离线修复验证

- execution/nautilus 全套：`491 passed, 21 skipped, 22 subtests passed`。
- actor durable-I/O 聚焦状态机：`116 passed, 4 subtests passed`。
- Redis rebaseline、control-plane isolation、systemd resource budget：`32 passed`。
- `py_compile`、Ruff `F/E9`、`bash -n`、`git diff --check` 通过。
- production deploy、Redis/PostgreSQL mutation、节点 restart 和真实交易均未执行；
  `account-a` 继续 HALTED，发布结论继续 NO-GO。

### 2026-08-09 元复核与证据边界

- 历史生产 traceback 继续证明旧运行字节在 actor callback 内执行同步 HTTP，该结论只
  描述 2026-08-08 取证覆盖的生产版本。
- 当前工作树的 heartbeat、command、ACK、terminal 与 exchange adapter 外部 I/O 已进入
  专用 executor/worker。当前 residual stall 机制尚未复现。
- 重新复盘新增 `GAP-0`，Phase A 将对 executor 饱和、queue 背压、blocked I/O 与
  shutdown 交互建立 red-capable fault-injection loop。
- 2026-08-09 使用固定 uv 依赖收集 538 个 execution/Nautilus 测试，结果为
  `523 passed, 4 failed, 11 skipped, 22 subtests passed`。四个失败均位于
  `tests/nautilus/runtime/test_node_config_risk.py`，对应 live runtime resource fail-open。
- 2026-08-08 的 `491 passed, 21 skipped` 缺少 collect-only inventory 和依赖快照；
  2026-08-09 的 538 项 inventory 已记录 SHA-256。两次结果对应不同日期和选择范围，
  不能计算回归差值。
- 可复现命令、依赖版本、suite path list 与 inventory hash 见
  `docs/evidence/2026-08-09-account-stall-test-baseline.md`。
- 当前只授权 Phase A。生产继续 `HALTED / NO-GO`。

## 后续验证证据要求

1. actor callback 在故障注入下保持 tick age 小于 2 秒，超过 15 秒触发 HALT，
   超过 60 秒触发进程重启。
2. heartbeat、command poll、ACK、intent poll 分别有独立 progress timestamp、queue
   depth、timeout 和 error counter。
3. Redis replacement generation 保持互相隔离；retired generation 经 janitor 后数量与
   key count 收敛；streams 按 retention 收敛。
4. A/B `/version` 的 git SHA、image digest、lock/config hash 一致；生产 mountinfo
   不存在 `//deleted`。
5. 节点从 dependency failure 恢复后保持 HALTED；只有审计 RESUME 可以转 ACTIVE。
6. 同 release、同 order shape 的 emergency reduce-only close 路径先完成测试网验证。
7. canary 与真实小额 round trip 全程保留 command、intent、`LIMIT + IOC` order、fill、
   reduce-only close、目标 symbol flat/zero-orders 和非目标组合签名基线证据。
