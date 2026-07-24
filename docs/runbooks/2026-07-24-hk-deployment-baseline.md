# hk trader-v3 最小部署基线（2026-07-24）

- 采集时刻：2026-07-24 07:51–08:01 UTC（ssh balen@149.104.30.223 只读采集；当时 hk 15 min load ≈ 100，Tailscale 100.104.27.123 不可达，公网 IP 可用但 banner 慢）
- 采集账号：balen（无 docker / journald 权限，权限盲区见 §6）
- 背景事故：补丁 `container-patches/projection_actor.py` 落盘但从未 bind-mount 进容器，07-23 node-a 重启造成投影幻影仓，靠 `/proc/PID/mountinfo` 才发现。本基线把"改的文件真的在跑"固化为机械化验证门（§5）。
- 已知漂移：线上是独立演化的 hotpatch 分叉，**不等于任何 git 分支**。`DEPLOYED_COMMIT.txt` = `6bf052a2702a8cfe834ec9cba48f8eabf27eafbd`（2026-06-20 写入，早已失真，仅作历史参考）。对照基线只能以本文件的 sha256 清单为准。

## 1. 运行时进程（真相源之一：ps）

两个 nautilus node 容器进程（宿主机视角，均 2026-07-21 03:14 容器重建后启动）：

| 角色 | PID | 启动时间 | cmdline | cfg 挂载源 |
|---|---|---|---|---|
| node-a | 4114531 | 2026-07-21 03:14:41 | `python -m app.run_node --config /cfg.json` | `/srv/trader-v3/node-a.hk.json` |
| node-b | 4114923 | 2026-07-21 03:14:52 | `python -m app.run_node --config /cfg.json` | `/srv/trader-v3/node-b.hk.json` |

其余 trader 相关常驻进程（DB 密码已打码，systemd 管理状态见 §4）：

| PID | 启动 | 摘要 |
|---|---|---|
| 2065 | Jul 08 | `.venv-cp/bin/python -m ingress.http --host 127.0.0.1 --port 8087 --database-url=postgresql://postgres:***@127.0.0.1:5432/trader` |
| 2067 | Jul 08 | `.venv-cp/bin/python services/hermes-worker/market_snapshot_recorder.py --interval 20 --max-age-seconds 900` |
| 2068 | Jul 08 | `.venv-cp/bin/python scripts/signal_tee.py run` |
| 1059748 | Jul 13 | `.venv-report/bin/uvicorn report_service:app --host 127.0.0.1 --port 8090` |
| 1179437 | Jul 09 | `.venv-cp/bin/python services/control-plane/tools/exchange_state_recorder.py --interval 45` |
| 3274461 | Jul 14 14:36 | `/usr/bin/python3 scripts/hermes_signal_feeder.py` |
| 3692076 | Jul 19 02:08 | `/usr/bin/python3 scripts/order_lifecycle_monitor.py` |
| 4114033 | Jul 21 03:14 | `.venv-cp/bin/uvicorn read_api:app --host 127.0.0.1 --port 8080`（控制面） |
| 679 / 3812326 | Jul 08 / Jul 12 | hermes gateway（profile luna / trader） |

注意：ingress 进程 cmdline 中 postgres 超级用户密码明文可见（07-24 审计未修项 #1），本文档一律打码。

## 2. 挂载真相（唯一真相源：/proc/PID/mountinfo）

node-a (4114531) 与 node-b (4114923) 的 `/app`、`/cfg.json` 挂载条目**完全一致**（cfg 除外），共 10 个代码挂载 + 1 个 cfg：

| 挂载源（宿主机） | 容器内目标 | 两节点均挂载 |
|---|---|---|
| `/srv/trader-v3/intent_execution_strategy.py.fixed` | `/app/strategy/intent_execution_strategy.py` | ✅ |
| `/srv/trader-v3/container-patches/intent_execution_planner.py` | `/app/strategy/intent_execution_planner.py` | ✅ |
| `/srv/trader-v3/container-patches/event_mapper.py` | `/app/projection/event_mapper.py` | ✅ |
| `/srv/trader-v3/container-patches/contracts.py` | `/app/execution_domain/contracts.py` | ✅ |
| `/srv/trader-v3/node_config.py.fixed` | `/app/config/node_config.py` | ✅ |
| `/srv/trader-v3/nautilus_actors.py.fixed` | `/app/app/nautilus_actors.py` | ✅ |
| `/srv/trader-v3/binance_adapter_config.py.fixed` | `/app/runtime/binance_adapter_config.py` | ✅ |
| `/srv/trader-v3/run_node.py.fixed` | `/app/app/run_node.py` | ✅ |
| `/srv/trader-v3/lifecycle.py.fixed` | `/app/runtime/lifecycle.py` | ✅ |
| `/srv/trader-v3/node.py.fixed` | `/app/app/node.py` | ✅ |
| `/srv/trader-v3/node-a.hk.json` / `node-b.hk.json` | `/cfg.json` | 各自 |

### ⚠️ 存在但未挂载（重点）

以下文件在磁盘上、看起来像"已部署的补丁"，但**不在任何 node 进程的 mountinfo 中，容器没在跑它们**：

| 文件 | mtime | 结论 |
|---|---|---|
| `/srv/trader-v3/container-patches/projection_actor.py` | 07-19 02:08 | **未挂载**（已知事故源：旧事件回放免疫补丁从未生效，07-23 重启已实际造成幻影仓） |
| `/srv/trader-v3/container-patches/binance_execution.py` | 07-10 17:33 | **未挂载**（本次基线新发现。69KB 执行客户端补丁；mtime 早于 07-21 容器重建——即使当年曾 docker cp 进容器，重建后也已丢失。是否需要重新部署待产品判断） |
| `/srv/trader-v3/intent_execution_planner.py.hedgefixed` | 07-02 13:21 | 未挂载（已被 container-patches/intent_execution_planner.py 取代，属历史残留） |

`gen_recreate.py`（生成容器重建脚本）只显式追加 planner + contracts 两个挂载，其余靠"继承旧容器 Mounts"传递——**任何新补丁如果没进过一次 docker inspect Mounts，重建永远不会带上它**。这是 projection_actor.py 类事故的结构性根因。

局限性说明：balen 无 docker 权限，无法排除"文件以 docker cp / 镜像重建方式进入容器"的路径；但 07-21 03:14 容器重建后 mountinfo 即全量真相，重建前的 docker cp 均已失效。

## 3. 文件指纹基线（sha256 / size / mtime / 挂载状态）

全部可读，无权限拒绝（recreate-*.sh 除外，见 §6）。

### 3.1 顶层 *.py.fixed（bind-mount 补丁，旧机制）

| sha256 | size | mtime (UTC+0 本地时) | 文件 | 挂载 |
|---|---|---|---|---|
| `2d4df4be776c84a56fc5c3e7d18ed071adb7d1b7e010e4c6a4aede2d6b83d769` | 2940 | 2026-06-21T10:18:21 | binance_adapter_config.py.fixed | ✅ 双节点 |
| `7eb91d678e0fcbe234346517f61fbf8bd17c26e049937309ef4db7485427c94c` | 93342 | 2026-07-21T03:14:30 | intent_execution_strategy.py.fixed | ✅ 双节点 |
| `e39975a7affdb614f679f55f71dd149004e2245b7d11e6ceed51b44d235ae0f4` | 6684 | 2026-07-10T17:46:14 | lifecycle.py.fixed | ✅ 双节点 |
| `7cba61844b13d299a1f6289a594da3d5d091b39b9e81c15813c6e72b558a4f72` | 17064 | 2026-07-07T07:51:03 | nautilus_actors.py.fixed | ✅ 双节点 |
| `a17933859008805be6081f507070df9013d22fb2802477b4ca7601f2a97fcfd3` | 20521 | 2026-07-03T08:02:51 | node.py.fixed | ✅ 双节点 |
| `a5ecbd255e2b6602f60383e5b791527aee85db021b340a34c44432a561ce33d3` | 11037 | 2026-06-21T10:07:14 | node_config.py.fixed | ✅ 双节点 |
| `00e4052cbcd34d12dda88712ce1dd74aa1bc40078479c2f9963bf40dc1affc64` | 2498 | 2026-06-20T13:05:54 | run_node.py.fixed | ✅ 双节点 |
| `5bfa022a28f687cd26cb094ced447e01a5b98dc15eb625ac5f449c015e0b7145` | 22777 | 2026-07-02T13:21:54 | intent_execution_planner.py.hedgefixed | ❌ 历史残留 |

### 3.2 container-patches/（bind-mount 补丁，新机制）

| sha256 | size | mtime | 文件 | 挂载 |
|---|---|---|---|---|
| `40c863754be94a88eda62801da4a82c5bc964b9b3b45f53f277b66237560059b` | 69786 | 2026-07-10T17:33:07 | binance_execution.py | ❌ **未挂载** |
| `e2a4a6883fd9e1a01e2d8f6101ff638c6ba40ec8251bb4fecf643e6747af3c15` | 4079 | 2026-07-06T19:17:23 | contracts.py | ✅ 双节点 |
| `6720ef2d079ebbc0362121a35b5d27fd6b174edbc8241349828f52e44acd8d49` | 8524 | 2026-07-10T17:46:14 | event_mapper.py | ✅ 双节点 |
| `1decd27541b4588d24a50d2293e277216e6ea7d9db6049e9d3033a985fa1d2d5` | 25967 | 2026-07-19T02:08:17 | intent_execution_planner.py | ✅ 双节点 |
| `23c3c08ecf6f587eca27bd22caa8aa3a227ac5a8cf5c04b5f12bb8d961d6a485` | 5584 | 2026-07-19T02:08:17 | projection_actor.py | ❌ **未挂载** |

### 3.3 宿主机直跑脚本（不涉及挂载，靠 systemd 拉起，见 §4）

| sha256 | size | mtime | 文件 | 运行中 |
|---|---|---|---|---|
| `e8c7e566b0c89fcff942bfa1221264e457fdbb97ca69f5d5ed4848c5732243d6` | 23273 | 2026-07-14T14:35:31 | scripts/hermes_signal_feeder.py | ✅ pid 3274461（07-14 起，磁盘=运行版本） |
| `19f5fd20a3505ca5b14ecbd24d962a6a207bbb8edf8f695d593d8d0492428f6a` | 39684 | 2026-07-19T02:08:17 | scripts/order_lifecycle_monitor.py | ✅ pid 3692076（07-19 起） |
| `a560c98449fa52bfb8ffe1d9ce2cb70a0078cf6c0444ebc891f9a73de0f1b2a0` | 4041 | 2026-06-21T11:15:58 | scripts/signal_tee.py | ✅ pid 2068 |
| `05cb80568c755dc5fb475f4de0170d23d954b142898dbeff76e90a9791595ec3` | 6886 | 2026-07-09T09:10:31 | services/control-plane/tools/exchange_state_recorder.py | ✅ pid 1179437 |
| `2c1a7c010c0f6fa92916c58bd0d0855e7a2d2ab75f3225b9e874900131104d17` | 101190 | 2026-07-21T03:12:50 | services/control-plane/api/read_api.py | ✅ pid 4114033（07-21 起） |

注意：宿主机直跑脚本"磁盘文件 = 运行代码"只在**进程启动时间晚于文件 mtime**时成立。上表均满足。若改了文件没重启服务，验证门查不出（哈希会匹配但进程跑的还是旧代码）——部署后必须核对进程启动时间 > 文件 mtime。

其余 scripts/*.py（未常驻，工具类）：

| sha256 | size | mtime | 文件 |
|---|---|---|---|
| `19367f5794ee057df14077372e0844c0eed4f3a3afc74186ee75dce3c1a9958d` | 5856 | 2026-06-21T02:43:00 | scripts/c08_scenarios.py |
| `7ea80c7a4855ce7a900e5ae5b0ad15c865bb7a57a40e3cc01667974508252150` | 5273 | 2026-06-20T08:39:22 | scripts/check_no_semantic_regex.py |
| `0e3259f0122de8fa002e0e99ef40b94a226a5f71985819af8527a32d93cb4ebd` | 566 | 2026-06-20T12:55:20 | scripts/clean_slate.py |
| `b50dd253c174048dc320368db03be432aa56c0bc26938522791e405cd4840e8d` | 1639 | 2026-06-20T12:49:14 | scripts/diag_hermes.py |
| `8f39a3c70a9be84a86371872c6017c50d8ac4752193774d47989601d90998ebc` | 9464 | 2026-06-21T03:01:55 | scripts/governor_demo.py |
| `7e3a355608f5ca1755623a32e6de015e3d849dfe3d8730d0947748ed12fe9b83` | 3118 | 2026-06-20T12:56:41 | scripts/ingest_corpus.py |
| `2020b048b15e9d45a00406f4af281830ceea224febed7095f3216bb0fba8ccb8` | 1428 | 2026-06-21T11:22:44 | scripts/live_gateway.py |
| `a01591082cb77ccd5bfb583ef31eeb518cc69659602c9919ee28d975e82a0edd` | 2706 | 2026-07-02T03:16:17 | scripts/live_worker.py |
| `dfad1188a748a2ee65acc78bd3c56f3fc12d1ffd55184d2da65fa2cc8c037119` | 9679 | 2026-06-20T10:43:48 | scripts/migrate_legacy_sqlite.py |
| `b566b0fb26370f1a372cff11dfb20b3055bfc8ff64aab5c780f51f121a2d8710` | 8178 | 2026-06-20T08:39:22 | scripts/replay_real_corpus.py |
| `90c5c4e4250d2cb5fe1f6e46aebf5d945c73947c13dd802dc8e21c307f05f57a` | 4073 | 2026-07-07T07:15:15 | scripts/report_data.py |
| `ac0e10ba9d5aa6c97834abb3d96e4d188e7c79865497144b2f9a8c7d8bb625a8` | 153 | 2026-07-06T17:58:57 | scripts/report_data_weekly.py |
| `04442d1c4a7f80241c22428fbdce4cdd0345ba08a4e02bb868b468a0b5004b54` | 1302 | 2026-06-20T14:14:11 | scripts/repost_events.py |
| `d3202c9ab5697d47307f67c9ce40d75e009192661f957957771aeae51b691f21` | 788 | 2026-06-20T12:46:04 | scripts/reset_replay.py |
| `46b14cd970e4ed6de7d9385b69b9d8b57e80d9979da6eaf6ac63fe453e6a345a` | 2775 | 2026-06-20T13:58:37 | scripts/seed_node_intent.py |

### 3.4 db/migrations/（全量）

| sha256 | size | mtime | 文件 |
|---|---|---|---|
| `24b456943a57c1919fc220ed3da5000a505a91b40cf2363131afc0f48c93458b` | 21333 | 2026-06-20T08:39:22 | 0001_canonical_schema.up.sql |
| `8038d305ef8de3facf323172ea5fedc752831becd514a8fddd106782009be254` | 4246 | 2026-06-20T08:39:22 | 0001_canonical_schema.down.sql |
| `e1deca2c85fc17c14aae6569e3a8a1c9d8527a633af2f4adeafbf87a7110c615` | 1118 | 2026-06-20T08:39:22 | 0002_processing_run_lease_and_statuses.up.sql |
| `2381901637f655dd44778b3a2def9e4de10a7fc2f1ebf0ae07604b0ff5faa1ce` | 667 | 2026-06-20T08:39:22 | 0002_processing_run_lease_and_statuses.down.sql |
| `d4f83f6a56a987807a0951b2c3e0c3fda446515d80f41fb421a95baaaaff51c3` | 1569 | 2026-06-20T08:39:22 | 0003_operator_commands.up.sql |
| `48070eb42d1d6b393a67b382781c373b973d95f9f28a232abc177e8cbc9a2138` | 80 | 2026-06-20T08:39:22 | 0003_operator_commands.down.sql |
| `25a5cb747b52c96fbf0f4560637d54396d155ae861fe534fb3b5e5a11c05e76d` | 647 | 2026-06-20T09:57:05 | 0004_hermes_decisions_unique_raw_message.up.sql |
| `40895585e24985c25d63e2df70239369e656279564b9e72b7d38e24fcd3b21e7` | 246 | 2026-06-20T09:57:12 | 0004_hermes_decisions_unique_raw_message.down.sql |
| `9817517b26ee1d49cacc098d4e1657c3d97f358e93239f0386ddfdfba4bb7258` | 874 | 2026-07-07T18:58:50 | 0006_trade_outcomes.up.sql |
| `787e0600e38d8c560d7e367789e80405adb9f0a2fc267da2d2b39472b92416e0` | 96 | 2026-07-07T18:58:50 | 0006_trade_outcomes.down.sql |
| `4591ac5e5598fe250b504f75bb8640815c53108b6d5cd1cdc184e274e4b95532` | 519 | 2026-07-19T02:08:17 | 0008_orders_projection_protection_fields.up.sql |
| `ec217466e56d4caa7d4d39fe63ce98fefcd911873668ecf96acdb779074ff484` | 132 | 2026-07-19T02:08:17 | 0008_orders_projection_protection_fields.down.sql |
| `80b3ce3d14390f3f59060d71c0080b5d184d01beb201560428ce811dce28d0dd` | 2983 | 2026-06-20T08:39:22 | README.md |

缺号说明：0005、0007 不存在于线上目录（编号跳号，非丢失——线上从未有过这两个文件）。

### 3.5 node 配置

| sha256 | mtime | 文件 |
|---|---|---|
| `04bfaf1115379ecb9f1962ecd911bf96c934b11228774ad7cfc36c8326642d3a` | 2026-06-21T10:09:25 | node-a.hk.json |
| `7947c5b3323eeac88f9f368b3f5cf36fed5f73012081e9e367c89cea8bdc0310` | 2026-06-21T10:09:53 | node-b.hk.json |

## 4. 调度面

### cron

`/etc/cron.d/` 中 trader 相关仅一个文件：

- `trader-v3-trade-outcomes`（644, root, 2026-07-07）：每日 00:30 UTC，flock 单实例，`.venv-cp/bin/python scripts/analysis/trade_outcomes.py`，输出至 `/var/log/trader-v3/`。**已知问题：`MAILTO=""` 吞错，该管道自 07-07 起实际死亡无告警**（07-24 审计未修项 #4）。
- `/etc/crontab` 不存在；root 用户 crontab 不可读（见 §6）。

### systemd（unit 文件均可读，状态可查）

| unit | active | enabled | ExecStart 摘要 |
|---|---|---|---|
| trader-v3-ingress | active | enabled | `.venv-cp python -m ingress.http :8087 --database-url=${DATABASE_URL}` |
| trader-v3-controlplane | active | enabled | `.venv-cp uvicorn read_api:app :8080` |
| trader-v3-exchange-state | active | enabled | `exchange_state_recorder.py --interval 45` |
| trader-v3-hermes-feeder | active | enabled | `/usr/bin/python3 scripts/hermes_signal_feeder.py` |
| trader-v3-lifecycle-monitor | active | enabled | `/usr/bin/python3 scripts/order_lifecycle_monitor.py` |
| trader-v3-market-snapshot | active | enabled | `market_snapshot_recorder.py --interval 20` |
| trader-v3-report | active | enabled | `.venv-report uvicorn report_service:app` |
| trader-v3-signal-tee | active | enabled | `scripts/signal_tee.py run` |
| hermes-gateway-luna / -trader | active | enabled | hermes gateway |
| trader-v3-binance-jp-route | inactive | static (+timer active) | `/root/binance_jp_route.sh`（脚本本体不可读） |
| trader-v3-node-jp-route | inactive | disabled | `/root/wire_nodes_jp_apply.sh`（不可读） |
| trader-v3-gateway | inactive | disabled | live_gateway.py（未启用） |
| trader-v3-hermes-worker | inactive | disabled | live_worker.py（未启用） |

node 容器本身由 docker 管理（`docker-abc78a7b….scope`），不是 systemd unit；重建靠 root-only 的 `recreate-trader-v3-node-{a,b}.sh`。

## 5. 部署验证门：scripts/verify_hk_deployment.sh

仓库内 `scripts/verify_hk_deployment.sh`（只读、幂等、不需要 root，balen 账号即可跑）。

### 标准流程（每次部署后必跑）

1. 部署者在本地对**每个改动文件**算 sha256，写成清单（`sha256sum` 输出格式，路径换成 hk 上的目标绝对路径）：

   ```
   # manifest.txt
   23c3c08e…b625a8 /srv/trader-v3/container-patches/projection_actor.py
   7eb91d67…c94c   /srv/trader-v3/intent_execution_strategy.py.fixed
   ```

2. 跑验证门：

   ```bash
   scripts/verify_hk_deployment.sh manifest.txt
   # hk 高负载 banner 超时时，可复用 ControlMaster 或走跳板：
   # HK_SSH_OPTS="-J balen@home-mini" scripts/verify_hk_deployment.sh manifest.txt
   ```

3. 判定：
   - 退出码 0 = 清单内文件哈希全部匹配，且 container-patches/ 下的文件全部出现在**两个** node 进程的 mountinfo 中 → 部署通过。
   - 退出码 1 = 打印每条差异（缺文件 / 哈希不符 / 未挂载）→ 部署未生效，禁止收工。
   - 退出码 2 = 用法或 ssh 连接错误。

4. 补充人工核对（脚本覆盖不到的）：宿主机直跑脚本改动后，确认对应 systemd 服务已重启（进程启动时间 > 文件 mtime，`ps -o lstart -p <pid>`）；容器内非挂载路径的改动（docker cp / 镜像）本脚本无法验证，一律**禁止**用这两种方式部署。

### 实测记录（2026-07-24）

- `bash -n` 语法通过。
- 正向：planner（已挂载）+ strategy + monitor 三条 → PASS，退出码 0，并逐节点打印挂载目标。
- 负向：projection_actor.py（哈希正确但未挂载）+ 故意错哈希 → 两类 FAIL 均被拦截，退出码 1。

## 6. 权限盲区（balen 账号读不到的）

| 项 | 影响 |
|---|---|
| `/srv/trader-v3/.env.v3`（600 root） | 无法核对 DATABASE_URL / token 配置漂移 |
| `/srv/trader-v3/recreate-trader-v3-node-{a,b}.sh`（700 root） | 无法直接审计重建脚本挂载清单，只能靠 mountinfo 反推 + 可读的 gen_recreate.py |
| `/srv/trader-v3/secrets/`（700 root） | 不可见 |
| `/root/binance_jp_route.sh`、`/root/wire_nodes_jp_apply.sh` | jp 路由脚本内容不可审计 |
| docker CLI / docker inspect | 无法确认容器镜像 tag、无法排除 docker cp 路径（mountinfo 弥补挂载部分） |
| journald | 服务日志不可读，服务级错误不可审计（既有已知问题） |
| root crontab（/var/spool/cron/crontabs） | 除 /etc/cron.d 外可能存在的 root 私有 cron 不可见 |

## 7. 本基线的失效条件

以下任一发生后，本文件的指纹表作废，需重新采集：容器重建（recreate 脚本执行）、container-patches/ 或 *.py.fixed 任何写入、systemd unit 变更、DEPLOYED_COMMIT.txt 更新。验证门脚本本身不依赖本文件，长期有效。
