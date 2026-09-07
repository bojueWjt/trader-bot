# 2026-09-02 ~ 09-04 漏单事故修复计划

状态：Phase 0 止血观察通过；Phase 1 代码已核验未上镜像；Phase 2 已落地；Phase 3 已在 node-a 打补丁并 RESUME · 日期：2026-09-05 · 范围：jp-24 四账户交易系统（watcher / Hermes / control-plane / nautilus-node）

用户裁决（2026-09-05）：Phase 0.1 立即止血；Titan 第二腿走 **β**（不加仓）；**明文授权** FATAL FENCE 后自动 RESUME（Phase 4 再实现，本轮不发 RESUME、不改闸门代码）。

Phase 0.1 执行（03:48–03:50 UTC）：
- 生产 SKILL 已插入铁律 14/15；feeder 已加 `当前时间` / 开仓时效 / β；`trader-v3-hermes-feeder` 停→改→再启。
- `docker restart trader-watcher-1` 后出现 `Connected, listening...`；CLOSE_WAIT `08` 从 **24013 → 0**。
- 首轮 poll 补发 13 行（Titan 4465–4469、舒琴 4138–4140、坚果 6852–6856）已从 sqlite 删除；feeder cursor 仍为 `telegram_messages:1096`。
- 备份：`/srv/trader-staging/phase0-hemo-20260905T034826Z/`。
- 未做 0.2 撤残留保护单；未 RESUME；未清 node-a inbox。
- 10 分钟盯盘（04:09 UTC 结束）：CLOSE_WAIT=0、tail 无 Started reconnecting、feeder 无新 job。容器已 Up 20 分钟，对 91.108.56.195:443 仍 ESTABLISHED。尚未出现 watched 频道新帖，`raw_messages` telegram 最新仍是 9/3 22:29Z（补发已丢，还没有新入库）。

> **2026-09-05 元复核（Grok，file:line + 线上抽查）**：方案主线成立，但不能按原文直接开工。必修项见 §0.5。直播证据（~03:25 UTC）：`trader-watcher-1` Up 2 days，`/proc/net/tcp` 状态 `08`（CLOSE_WAIT）**23192**；`raw_messages source=telegram` 最新 `2026-09-03 22:29:06Z`（已断 ~29h）；四节点 ACTIVE；生产 `/srv/trader-v3/scripts/trade_event_notifier.py` **不含** `node.halted`（34658B），staging 补丁 36998B 仍未安装。

## 0. 结论摘要

这两天的漏单是四个独立故障叠加，其中一个（watcher 断流）截至 9/5 02:50 UTC 仍在持续。

| # | 根因 | 影响窗口 | 丢掉的单 | 状态 |
|---|------|---------|---------|------|
| A | node-b/c 心跳 5s 超时崩溃（exit 75）后重启进 HALTED，无人 RESUME | 9/1 22:41 → 9/2 09:10（10.5h） | c：坚果买金 XAU；b：Titan BTC×2、VIRTUAL×2（VIRTUAL 后来两腿都触发涨 3–4%） | 已恢复；`node.halted` **审计已落库**，Telegram 补丁 `a5a05ab` **未安装**（见 §0.5） |
| B | node-a inbox 里 11 条 8/28–8/31 的 `cancel_order` 记录卡在 `dispatched`，每次重启回放即冻结 BTC/ETH/SOL/MU 新开仓 | 9/1 22:41 起持续 | a：舒琴 BTC 75000、ETH 2302（9/2）；ETH/BTC/SOL 区间单（9/3） | **未修，下次重启复现** |
| C | Titan 分批第二腿 `denied:position_exists`（不加仓设计） | 常态 | b：SAND 0.03666、TAO 208 | 产品决策 |
| D | watcher（GramJS）链路抖动后手写重连与内部重连互踩，泄漏 2.8 万 CLOSE_WAIT 耗尽端口，`EADDRNOTAVAIL` 永久失联 | 9/4 08:23 UTC 起持续 | 9/4 全天所有频道（含坚果闪迪） | **未修，正在丢** |

次要发现（不丢单但要顺手修）：
- E. SAND 移损被 algo 撤单盲区拒（已知），`replace_take_profits` 因 Hermes 按减仓前数量算 TP 被拒（29416 > 23638）。
- F. account-a BTC 空 0.12 挂两张止损（82912/83000）；account-c 有一张无仓位的 LONG 侧 72700 陈旧止损。
- G. 断流恢复后 watcher 会把每频道最近 5 条重新推送，ingress 只对已入库消息去重，断流期间的消息会被当成新信号交给 Hermes，而 SKILL 无时效规则。

## 0.5 元复核（开工闸门）

核验方式：4 个只读 subagent（watcher / Hermes / freeze+inbox / 告警+HALT）+ jp-24 只读抽查。
总体结论：**主因 A–D、冻结机制、重连泄漏均成立；按原文开工会修错树 / 修了等于没修 / 重启补发旧单。先改下列必修项再派 Codex / 再重启 watcher。**

### ⚠️ 必修项（照原文做会出错）

| 条目 | 方案错在哪 | 证据 | 正确定位 / 做法 |
|---|---|---|---|
| P1 路径 | 本仓库 `services/telegram-watcher/` 是 Python 适配器，没有 `server.js` | 生产 GramJS：`bridge/services/telegram-watcher/server.js`；jp-24：`/srv/trader/services/telegram-watcher/server.js`（`git status M`，**101 行删除**：旧 hermes-cron / freqtrade forward，与本仓库现状对齐） | Codex 写任务必须钉 `bridge/...`（本仓）或 `/srv/trader/...`（jp-24）。派发前 `git diff > /srv/trader-staging/watcher-uncommitted-$(date +%s).diff` |
| P1 重连风暴 | 删 30s 定时器**挡不住** `Started reconnecting` | 定时器最多 ~120 次/小时（`server.js:306-322`）；线上 10 分钟 411 次 GramJS `Started reconnecting`。GramJS `autoReconnect` + `reconnectRetries=Infinity` + `retryDelay=1000`（`telegramBaseClient.js` / `MTProtoSender.js`）。`connectionRetries: 5` **已经设过**（`lib/telegram-proxy.js:16`），那是**首次**连接，不是重连风暴旋钮 | 必须设 `reconnectRetries` 有限次（或关 `autoReconnect` 改由自杀重启）；30s 手写 `connect()` 只许在 `!client.connected && !_reconnecting` 时走。`process.exit(1)` + compose `restart: unless-stopped` 才释放 CLOSE_WAIT。healthcheck **不会**让 `unless-stopped` 重启容器 |
| P0.1 插入点 | SKILL.md **没有**「处理要求」 | 「处理要求」在 `scripts/hermes_signal_feeder.py:172` 的 `PROMPT_TEMPLATE`。SKILL 铁律在 L12–27 | 铁律写进 SKILL.md **并且** feeder prompt 加一条。改 feeder 模板必须 `systemctl restart trader-v3-hermes-feeder`（import 时固化） |
| G 时效闸 | 只改 SKILL / 只「不要推 ingress」挡不住补发 | watcher **不调** ingress；`handleWatchedEntry` → sqlite 盲 INSERT（`trading-api.js:302-324`，不存 `entry.date`）。feeder 按 sqlite 自增 PK 喂 Hermes，ingress `inserted:false` **仍 run_hermes**（`hermes_signal_feeder.py:1761-1801`）。prompt「时间范围」是 ingest `created_at=now`，重启补发会被模型当成「刚刚」 | 重启前停 feeder；重启后删掉/跳过首轮 poll 新插入的 sqlite 行；再启 feeder。代码层：首轮 poll 对 `message.date` >30min 的**不要 INSERT**。SKILL 30 分钟规则仍要，但是第二道闸 |
| C 方案 α | SKILL 改 `add_position` **下不了单** | `v3_trade.py` `cmd_open` 写死 `open_position`；`read_api.py` `_OPERATOR_ACTIONS` 排除 `add_position` → HTTP 400。节点 planner **允许**同向 `add_position`（`intent_execution_planner.py:954-961`） | α = CLI + 控制面白名单 + 测试，不是 SKILL-only。默认走 **β**（第一腿未成交才挂第二腿），除非用户明确要放开加仓 |
| P3 冻结 | 只给 `_freeze_durable_intent_confirmation` 加 action 闸不够；过期开仓跳过危险 | 函数 L6986 不读 `record.action`，把 seq=`99` 的 cancel client_order_id 塞进 `_pending_order_confirmations`，`symbol_open_freezes` 会并上这张表。回放源是 `ApprovedIntentDataClient.replay_pending` → `inbox.pending()`，**不是** `begin_dispatch`。`valid_until` 过期跳过 **open/add dispatched** = 未确认开仓失去保护 | 非开仓：不注册 pending-id **且** 不 `_freeze_symbol_new_opens`。cancel 失败用现有 `rejected`（不要新状态，inbox `_VERSION=1` 未知版本直接 fatal）。过期跳过仅限非开仓。repair `--apply` 只动 `cancel_order`+`dispatched` |
| P3 告警 D | 节点不写 `audit_events`；生产 notifier 还没装 halt 补丁 | 冻结只在内存+日志；`node.symbol_frozen` 全仓库零实现。生产 notifier 34658B 无 `node.halted`，staging `…node-halt-20260902T092308Z.py` 36998B 待裁决 | 先安装 `a5a05ab` 到 `/srv/trader-v3/scripts/trade_event_notifier.py` 并重启 unit。冻结告警要控制面写入 + notifier 白名单； interim 可依赖后续 `intent_ack.rejected` / `symbol_new_open_frozen` |
| P4.1 演练 | `docker stop node-d` **不会**发 `node.halted` | 停容器 = `last_seen_at` 冻结，进程来不及 fail-closed。节点启动默认 HALTED，**重启后**第一条心跳才可能 ACTIVE→HALTED | 用 `docker restart trader-v3-node-d`。先装 notifier。RESUME 仍须用户明示（铁律） |
| P4.2 自 RESUME | 即使「崩溃前 60s ACTIVE 且无事故」也违铁律 | `AGENTS.md`：RESUME 只能用户明示。exit 75 经常**来不及**写 incident | **不做**自动 RESUME。只做「崩溃后 5 分钟仍 HALTED → 二次告警」 |

### 清理项（不阻塞）

| 条目 | 问题 | 建议 |
|---|---|---|
| P2 验收 ID | 4128/4129/4130、4462/4463、Cash 1990 仓库内零 fixture | 从 `raw_messages` / watcher sqlite 现拉语料，或改用 bench 已有 Titan 4433 / Cash 1982 |
| P2 E | SKILL 铁律 6 已写「按剩余仓位重挂」；CLI 不传 `--qty` 已按当前仓 | SKILL 加**禁止把信号原始数量传给 `--qty`**的例子即可，节点已有 `quantity_exceeds_position` |
| P1 告警 env | compose 把 `WATCHER_ALERT_*` 写死 `""`；notifier 用的是 `TELEGRAM_BOT_TOKEN` + `TELEGRAM_HOME_CHANNEL` | 映射 Hermes bot，不要假设变量同名 |
| P1 `/healthz` | 现网探活是 `/api/status`（`127.0.0.1:9090`） | 可新增 `/healthz`；它只是可观测，不能替代 `exit(1)` |
| `uncaughtException` 吞退出 | `server.js:33-37` 注释还写 PM2 | 自杀路径必须显式 `process.exit(1)`，别指望未捕获异常 |
| P3 5 秒回放 | fetch 间隔默认 1s；`replay_pending` 可能跑两次 | 测试按 `replay_pending` 写，不要绑 5s |

### 可开工项（核验通过，按修正后的做法）

- **Phase 0 止血（运维）**：先 SKILL 铁律 + feeder prompt + **停 feeder → 重启 watcher → 丢掉首轮 poll 的 sqlite 新行 → 启 feeder**。再盯 10 分钟。0.2 撤残留保护单仍要先 `v3_query.py positions`。
- **Phase 1**：`bridge/services/telegram-watcher/`（jp-24 `/srv/trader/services/telegram-watcher/`）+ `bridge/docker-compose.yml`。核心是掐断 GramJS 无限重连 + 自杀释放 FD + 首轮 poll 不 INSERT 过期消息。
- **Phase 2**：`SKILL.md` + `scripts/hermes_signal_feeder.py` + `tests/test_hermes_signal_feeder.py`。β + Cash 观察帖 + TP 数量例子 + prompt 当前时间。α 单列，需产品拍板。
- **Phase 3**：`intent_execution_strategy.py` `_freeze_durable_intent_confirmation`（L6986）+ `intent_execution_inbox.py` cancel 终态 `rejected` + `replay_pending` 跳过非开仓过期 + `inbox_repair.py`（cancel-only）+ `tests/execution/open/test_intent_execution_strategy_shell.py` 旁新用例。
- **Phase 4**：先装 notifier 补丁；`docker restart` d 账户做一次真告警；二次告警 5 分钟，不自动 RESUME。

### 阻塞项（等人）

| 条目 | 缺什么 | 找谁 |
|---|---|---|
| C Titan 第二腿 | α（放开加仓，改 CLI+API）还是 β（维持不加仓，SKILL 跳过） | 用户 |
| P4.2 | 已建议否决自动 RESUME | 用户（默否） |
| P0.2 撤单 | 撤前对仓：a BTC 82912 vs 83000；c 无仓 LONG 72700 | 用户确认后运维 |
| P4.1 / 冻结 Telegram | 安装 `a5a05ab` 到生产 notifier | 用户确认后运维（与 0 无关，watcher 止血不依赖它） |

## 0.6 验收复核（Claude Code，2026-09-05 05:50 UTC）

核验：`.venv-arch` 下 5 个测试文件 236 passed；watcher `node --test` 61 passed；GramJS 2.26.22 源码核对 `reconnectRetries/isReconnecting/_reconnecting/_reconnect/userDisconnected` 均存在；jp-24 直播抽查。

**上线前必改（阻塞）**
- P1-a：GramJS 用尽 `reconnectRetries` 后只置 `sender.userDisconnected=true`，`_userConnected` 仍为 true → `client.connected` 仍 true、`isReconnecting` false → `isTelegramConnected()` 返回 true，120s 自杀不会触发，只剩 20 分钟 stale 探测兜底。`isTelegramConnected()` 需增加 `sender.userDisconnected === true` 视为断连。
- P1-b：`bridge/docker-compose.yml` 仍写死 `WATCHER_ALERT_BOT_TOKEN/CHAT_ID: ""`，告警代码是死的；改为从 env 文件注入 notifier 同一 bot。
- P2-a：生产 SKILL 比仓库多 5.2/5.3 与报告段三行，仓库先吸收再同步，否则下次覆盖会删掉生产规则；且生产 5.3「后续档仍须逐档提交」与新 15 条 β「第一腿已成交则跳过」**互相矛盾**，5.3 必须删或改写。

**建议改（不阻塞）**
- P3-a：`_management_cancel_failure_reason` 不识别线上最常见的 `CancelStateError(... terminal status NEW)`（algo 撤单盲区），此类记录仍停 `dispatched`（已不会冻结，24h 后回放跳过，repair 工具可清），语义上正确，记录会积累。
- P3-b：策略里用 `logging.getLogger(__name__).warning`，nautilus-node 未配置 stdlib logging，靠 lastResort 打到 stderr 无时间戳；改 `self.log.warning`。
- P3-c：冻结/解冻 audit 事件未实现；过渡期靠 notifier 已转发的 `intent_ack.rejected detail=symbol_new_open_frozen`。
- 文档：Phase 3 改动项 2 写「exchange_confirmed 或新状态」，实现用的是 `rejected`（正确），更新原文。

**部署边界**
- 生产 `/srv/trader/services/telegram-watcher/server.js` 与仓库 HEAD 字节一致，Phase 1 diff 可干净应用；需 `docker compose build watcher` 重建镜像；新代码首轮 poll 自带 30 分钟过期过滤，部署重启不必再手删 sqlite。
- 工作树同时含未提交的 `-2013` 修复（`owned_order_recovery.py` +157，测试绿）与其它无关改动（read_api/report/conftest）；节点镜像构建前先把 Phase 3 与 -2013 分别提交，无关改动不入本次发布。

**线上状态（05:45 UTC）**：watcher Up since 03:48，CLOSE_WAIT=0，无重连风暴，`/api/status connected=true`，实时路径在收非关注频道消息并正确 FILTERED OUT；关注频道自 03:50 起无新帖，端到端尚未被真实消息验证；首轮补发 13 行已清（sqlite max=1096 = feeder cursor）；四节点 ACTIVE；生产 notifier 仍无 `node.halted`；0.2 残留保护单未撤；node-a 冻结待 Phase 3 上线。

## 1. 目标与总验收

目标：恢复信号接收；消除"重启即冻结"和"重连即死"两个自伤机制；让"节点崩溃/断流/冻结"三类静默故障 15 分钟内到人。

总验收（全部满足才算关闭事故）：
1. `raw_messages` 中 `source='telegram'` 的最新 `ingested_at` 持续跟随频道发帖（每日抽查）。
2. node-a 对 BTC/ETH/SOL 新开仓 intent 返回 `intent_ack.accepted`（用一条 valid_until 极短的测试 intent 或等待真实信号验证）。
3. 人工 `docker restart trader-v3-node-a` 后，日志中不再出现 `symbol_new_open_frozen` 的启动序列。
4. 人工模拟 watcher 断连（`docker network disconnect` 30 秒）后 3 分钟内自动恢复，`CLOSE_WAIT` 数量不增长。
5. 三类告警各触发一次并到达 Telegram：node.halted、watcher 断流、symbol 冻结。

## 2. 阶段划分

### Phase 0 — 止血（今天，运维手工，≈30 分钟）

**0.1 先加时效规则，再停 feeder，再重启 watcher**（顺序不能反；且不能只改 SKILL）

元复核后的正确顺序：
1. SKILL 铁律（`hermes-profile/skills/trading/v3-trader/SKILL.md`，插在铁律列表末、编号 14）**以及** feeder `PROMPT_TEMPLATE`「处理要求」前各加一条：
   > 开仓类信号若 Telegram 消息发布时间距当前超过 30 分钟，只汇报不执行（回复标注 ⏰过期）；仓位管理类（移损/止盈/平仓）不受此限但需先核对当前仓位。
2. 同步 SKILL 到 jp-24 `/srv/hermes/profiles/trader/skills/trading/v3-trader/SKILL.md`（新 cron job 会重读，不必重启 gateway）。
3. 同步 feeder 到 `/srv/trader-v3/scripts/hermes_signal_feeder.py` 后 **必须** `systemctl restart trader-v3-hermes-feeder`（模板在 import 时固化）。
4. **停 feeder** → 记下 watcher sqlite `telegram_messages` 当前 `max(id)` → `docker restart trader-watcher-1` → 等到 `Connected, listening...` → **删除/跳过**首轮 poll 新插入的行（`id > 旧 max`；sqlite 不存 `message.date`，prompt「时间范围」是 ingest=now，SKILL 30 分钟挡不住补发）→ 再启 feeder。
5. 之后 10 分钟盯 `journalctl -u trader-v3-hermes-feeder -f`；若仍有误开仓，立即 `v3_trade.py` 撤单/平仓。

```bash
ssh -p 53222 root@100.89.58.40 'docker restart trader-watcher-1 && sleep 10 && docker logs --tail 8 trader-watcher-1'
```

验收：watcher 日志出现 `Connected, listening...` 且 `[raw-update]` 持续；容器内 `/proc/net/tcp` 状态 `08`（CLOSE_WAIT）在个位数（9/5 03:25 UTC 实测 **23192**）。

**0.2 清理残留保护单（F）**

- account-a BTCUSDT SHORT 两张止损保留 83000（系统最新裸仓修复挂的），撤 82912。
- account-c BTCUSDT LONG 侧 72700 STOP_MARKET 撤掉。
- 用 `/root/sndk_algo_cleanup.py` 同款路径（list → 带特征核对 cancel → 回读），撤前先 `v3_query.py positions` 对仓位。

**0.3 暂不处理 B 的线上状态**：清 inbox 文件需要停 node-a，走一次 HALT→RESUME，与 Phase 3 的代码上线合并做，避免两次舰队停摆。期间 account-a 的 BTC/ETH/SOL 开仓仍会被拒，如有强信号由人工下单。

### Phase 1 — Watcher 加固（Codex；本仓库 `bridge/services/telegram-watcher/`，jp-24 `/srv/trader/services/telegram-watcher/server.js`）

注意：jp-24 上该文件有未提交改动（`git status` 显示 M），派发前先 `git diff > /srv/trader-staging/watcher-uncommitted-$(date +%s).diff` 备份并确认内容。

改动项：
1. **删掉手写 30 秒重连定时器**，或改为：仅当 `client.connected === false` 且 GramJS 内部 `_reconnecting` 未置位且距上次尝试 ≥ 60 秒时才调 `connect()`；两条重连链绝不并发。
2. **断连自杀**：维护 `lastUpdateAt`（每条 raw-update 刷新）；若 `!client.connected` 持续 120 秒，或 `lastUpdateAt` 超过 20 分钟且 `getMessages` 探测也失败，`process.exit(1)`，交给 compose `restart: unless-stopped` 拉起（进程重启天然释放泄漏 socket）。
3. **重连退避**：GramJS `connectionRetries` 有限次 + `retryDelay` 指数退避上限 30 秒，避免每秒多次建连触发 DC 侧拒绝。
4. **healthcheck**：`docker-compose.yml` 的 watcher 加 `healthcheck`（本地 HTTP `/healthz` 返回 `lastUpdateAt` 与 `connected`，超过 15 分钟无更新返回 503），`interval: 60s, retries: 3`。
5. **告警**：配置 `WATCHER_ALERT_BOT_TOKEN/CHAT_ID`（复用 trade-event-notifier 的 bot），断连 ≥ 2 分钟、自杀前、重连成功各发一条。
6. **启动补发保护**（G 的第二道闸）：启动时的首轮 `pollGroups` 只把 `message.date` 在 30 分钟内的消息交给 `handleWatchedEntry`，更早的只入本地库不推 ingress。

验收：
- 单测/脚本：模拟 `client.connected=false` 120 秒 → 进程退出码 1。
- 线上演练：`docker network disconnect trader_default trader-watcher-1`，60 秒后 `connect` 回去；观察 3 分钟内恢复收更新，`CLOSE_WAIT` 不超过 5。
- `docker inspect --format '{{.State.Health.Status}}' trader-watcher-1` 为 healthy。

验证命令：
```bash
docker logs --since 10m trader-watcher-1 | grep -cE "Started reconnecting"   # 演练期间应 < 20
docker exec trader-watcher-1 sh -c 'awk "NR>1&&\$4==\"08\"" /proc/net/tcp | wc -l'
```

### Phase 2 — Hermes 规则修正（Codex，本仓库 `hermes-profile/skills/trading/v3-trader/SKILL.md` + `scripts/hermes_signal_feeder.py`）

1. 时效规则正式化（Phase 0.1 的临时版本转正），feeder 在 prompt 的"时间范围"后追加 `当前时间`，让模型能算差值。
2. **TP 数量基准**（E）：`replace_take_profits` 在 partial_close 之后必须按交易所镜像的当前仓位数量重算，禁止沿用信号原始数量；SKILL 加规则并给出示例。
3. **Titan 分批第二腿**（C，需用户拍板）：二选一
   - 方案 α：SKILL 规定同一批次的第二腿改用 `add_position` 动作（需确认控制面/节点对 add_position 的风控是否放开）；
   - 方案 β：维持不加仓，SKILL 明确"第二腿仅在第一腿未成交时挂"，回复里说明跳过原因，不再制造无意义的 rejected。
4. Cash 频道「关注区域 + 失效位 + 潜在反弹区域」格式的判定规则（历史摇摆项）一并补上：明确按观察帖处理，不下单。

验收：用 9/2–9/3 的真实消息（4128/4129/4130、4462/4463、Cash 1990）做离线回放，Hermes 结论符合规则；`tests/` 下若有 feeder 单测需通过。

### Phase 3 — 节点"卡死记录冻结 symbol"修复（Codex，本仓库 `services/nautilus-node/`）

根因位置：
- `strategy/intent_execution_strategy.py` `_freeze_durable_intent_confirmation`（约 L6986）对任何 action 的 `dispatched` 记录都冻结 symbol；
- `runtime/intent_execution_inbox.py` 里 `cancel_order` 记录在撤单失败/订单不存在时没有被置为终态，永远停在 `dispatched`；
- 启动后 5 秒内逐条回放（9/1 22:41:02–22:41:12 日志），回放源待定位（`begin_dispatch` 返回 `RECOVERY_REQUIRED` 的调用链，怀疑在 durable-io 恢复或 intent 流首轮拉取）。

改动项：
1. **冻结只对开仓类动作生效**：`_freeze_durable_intent_confirmation` 在 `record.action not in {"open_position","add_position"}` 时不冻结，只记录 denial。
2. **cancel/管理类记录终态化**：撤单目标不存在、`CancelStateError`（含线上常见 `terminal status NEW`）、交易所返回订单已终态时，把 inbox 记录标记 **`rejected`**（`order_cancel_not_found` / `order_already_filled` / `order_already_terminal`），不再回放。不要新增 inbox 状态，也不要把失败 cancel 标成 `exchange_confirmed`。
3. **过期记录不参与恢复**：`dispatched` 记录若 `intent_payload.valid_until` 已过期超过 24h，启动恢复时跳过并打一条 WARN，不冻结。
4. **冻结可观测**：symbol 冻结/解冻写一条 `audit_events`（event_type `node.symbol_frozen` / `node.symbol_unfrozen`），并加进 trade-event-notifier 白名单（这是唯一活着的告警通道）。
5. **一次性清理脚本** `services/nautilus-node/tools/inbox_repair.py`：列出 `dispatched` 且 action 非开仓、valid_until 过期的记录，`--apply` 时改为终态并备份原文件（`intent_execution_inbox.json.bak-<ts>`）。

回归测试（`tests/execution/open/test_intent_execution_strategy_shell.py` 已有 `symbol_new_open_frozen` 用例，在其旁新增）：
- 卡死的 `cancel_order` 记录回放不冻结 symbol；
- 卡死的 `open_position` 记录（未过期）仍冻结（保持原保护语义）；
- 过期 24h 的 `open_position` 记录跳过；
- 冻结/解冻各产生一条 audit 事件。

上线步骤（与 0.3 合并，一次舰队停摆）：
1. 部署新镜像到 node-a（先只 a）；
2. 停 node-a，跑 `inbox_repair.py --apply`，核对备份；
3. 启 node-a，确认日志无 `symbol_new_open_frozen` 启动序列；
4. `python3 /srv/trader-staging/resume_race.py account-a`；
5. 观察一轮真实信号后再滚动到 b/c/d。

验收：总验收第 2、3 条。

### Phase 4 — 崩溃后 HALTED 静默窗（Codex 调研 + 用户决策）

现状：9/2 已部署 halt **审计**（`audit_events` 有 `node.resumed` 实证）；Telegram 补丁 `a5a05ab` **未安装**（生产 34658B 无 `node.halted`，staging 36998B 待装）。告警链尚未被真实崩溃验证过。

1. **先安装 notifier 补丁**（生产文件无 `node.halted`）：把 staging `/srv/trader-staging/trade_event_notifier.node-halt-20260902T092308Z.py` 装到 `/srv/trader-v3/scripts/trade_event_notifier.py` 并 `systemctl restart trader-v3-trade-event-notifier`。
2. **验证告警真发**：对空账户 d 用 `docker restart trader-v3-node-d`（**不要** `docker stop`：停容器不会发 `node.halted`）。确认 Telegram 收到 `node.halted`；随后 **用户明示** 再 RESUME，确认 `node.resumed`。
3. **崩溃自动恢复：默认不做**（违 RESUME 铁律）。只做「崩溃后 5 分钟仍 HALTED → 二次告警」。
4. **心跳超时根因**：9/1 22:39 的 5 秒心跳超时与 9/2 22:39 记忆中的同类事件成对出现，怀疑 PG 锁等待，开 `log_lock_waits=on` 与 `deadlock_timeout=1s` 收集一周证据。5s 是 HTTP total deadline（`http_client.py`），不是 `heartbeat_timeout_seconds`（15s，只 HALT 不自杀）。

### Phase 5 — 复盘补齐（Claude Code）

- `docs/plans/` 本文档随进度更新状态列；
- `jp24-trader-ops-entrypoints` 记忆补一条"漏单排查顺序"（已写入 `jp24-missed-signals-2026-09-04`）；
- 事故关闭条件：总验收 5 条全绿 + 连续 3 天无 `intent_ack.rejected detail in (halted, symbol_new_open_frozen)`。

## 3. 风险与回滚

| 改动 | 风险 | 回滚 |
|------|------|------|
| watcher 重启 | 补发旧信号（G） | Phase 0.1 先加时效规则；出错立即撤单 |
| watcher 自杀逻辑 | 误判导致频繁重启，丢几秒消息 | 阈值保守（120s/20min）；`restart: unless-stopped` 兜底；git revert |
| 节点冻结逻辑放宽 | 真实未确认的开仓单失去保护 | 只放宽非开仓动作与过期记录，开仓语义不变；回归测试覆盖 |
| inbox 一次性清理 | 改错记录 | 脚本只动 `dispatched` + 非开仓 + 过期三重条件；改前备份；HALT 窗内操作 |
| 舰队停摆 | 与 8/25、8/30 同类 | 合并为一次，走 resume_race.py 标准流程 |

## 4. Codex 派发模板（按 Phase 逐个派）

```
/codex:rescue --background \
目标：<Phase N 的改动项 1..k>
范围：<允许改的文件/目录，见各 Phase>
约束：不改 order_plan/intent schema；不改 RESUME 闸门语义；保留现有日志格式；不引入新依赖
验收：<该 Phase 的验收条目>
验证命令：<该 Phase 的验证命令 + 相关 pytest 路径>
输出要求：列出 changed files、verification、remaining risks。
```

派发顺序：Phase 3（最长，先起）→ Phase 1 → Phase 2；Phase 0 与 Phase 4.1 由运维手工执行，不派 Codex。

## 5. 证据索引

- 拒因总表：`select created_at, payload->>'detail' from audit_events where event_type='intent_ack.rejected' and created_at >= '2026-09-02'`
- 冻结序列：`docker logs -t --since 2026-09-01T22:40:00Z trader-v3-node-a | grep symbol_new_open_frozen`
- inbox 卡死记录：node-a 容器 `/state/spool/account-a/nautilus-node-account-a/intent_execution_inbox.json`，`state=dispatched` 共 11 条，全部 `cancel_order`
- 崩溃：`docker logs -t trader-v3-node-b | grep "FATAL RUNTIME FENCE"`（9/1 22:39:15）
- watcher：`docker logs -t trader-watcher-1 | grep -c EADDRNOTAVAIL`；`Started reconnecting` 每小时 3k–16k 次；容器内 CLOSE_WAIT 21,547 个全部指向 91.108.56.195:443
- 通路健康：用 watcher 镜像另起容器 `node /tmp/tgtest.js wss` 1.6 秒连通（9/5 02:51）
