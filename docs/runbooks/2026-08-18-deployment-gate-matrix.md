# JP24 两相部署门禁总表

## 时间预算

- 部署全链最坏耗时 `W = 46,800s = 13h`。
- 自动刷新阈值 `2W = 93,600s = 26h`。
- 有效窗口 `< 2W` 的证据并入 `preflight` 自动刷新；刷新失败或证据过期写告警，除五道硬闸外不阻断部署。
- `preflight` 只生成镜像、证据和恢复材料，A-D 容器保持运行。
- `execute` 重跑前置校验，只有硬闸通过且目标镜像可 inspect，才进入 HALT/fence/停机窗口。

## 五道硬闸

| 硬闸 | 证据来源 | 有效窗口 | 刷新方式 | 最坏耗时 |
|---|---|---:|---|---:|
| RESUME 仅限用户审计命令 | 单账号 canary executor、签名 release/safety/emergency/permit 文档、用户确认命令 | 单次 canary | 第 4 步由人工触发单账号命令；部署脚本不 RESUME | 13h |
| 账实对账健康 | lifecycle monitor、`orders_projection`、交易所 open regular/algo、`node_heartbeats` reconciliation payload | 15s 到单次采样 | preflight/execute 查询最新 HALTED ready 与 reconciliation；monitor 可随时复跑 | 13h |
| canary 亏损上限 | live-trade report、permit `max_cumulative_net_loss_usdt`、成交/手续费/PnL 计算 | 单次 canary | canary executor 在 RESUME 后、OPEN 前和观测中实时刷新 | 13h |
| Redis 单写者围栏 | operation lock、maintenance fence row、owner token、A-D HALTED heartbeat、active Redis fencing epoch | fence lease 120s，heartbeat 15s | execute 在停机前 acquire/verify；停机后用 frozen receipt 复验 | 13h |
| 镜像哈希 = manifest 哈希 | `release-manifest.json`、bundle manifest、immutable attestation、Docker image digest/labels | 与镜像 digest 同寿命 | preflight 构建或复用镜像并 `docker image inspect`；digest 必须匹配 manifest | 13h |

## 降级为告警

| Gate | 证据来源 | 有效窗口 | 刷新方式 | 处置 | 最坏耗时 |
|---|---|---:|---|---|---:|
| Redis capacity age | `capacity-evidence.json`、`capacity-refresh.json`、Docker/Redis/磁盘实时采样 | 24h | `preflight` 第一步运行 `refresh_redis_capacity_evidence.py refresh/verify` | 告警 | 13h |
| 磁盘储备 | `statvfs` 对 staging、`/srv/trader-v3`、Docker root 的可用字节 | 实时 | 每次 preflight 自动采样 | 告警 | 13h |
| two-run/reviewer trust ritual | `reviewer-trust-proof.json`、签名 reviewer、attestation hash、review subject hash | 与 attestation 同寿命 | 保留 proof 和 hash 记录；preflight 自动读取 | 告警 | 13h |
| canary permit 签发仪式 | signed permit JSON、DB `live_canary_permits` 记录 | 10m | 单账号 canary 命令自动生成并插入 | 告警记录；实际 RESUME 仍受用户审计命令硬闸约束 | 13h |
| 20/20 fleet freshness | `node_heartbeats` fresh rows、writer identity、HALTED status | 5s | rollout register 自动尝试锁 fresh heartbeats | 告警写入 `registration_warnings` | 13h |
| Account-B evidence refresh | `refresh-account-b-evidence` hook、evidence/signature | 1h | account-B preflight 自动刷新，timeout 900s | 告警 | 13h |
| Account-B exchange snapshot age | signed `exchange_snapshot` captured_at | 1h | 同一 hook 自动刷新 | 告警 | 13h |
| Account-B PostgreSQL snapshot age | signed `postgres_snapshot` captured_at | 1h | 同一 hook 自动刷新 | 告警 | 13h |
| Account-B fault report age | signed `fault_report` completed_at | 1h | 同一 hook 自动刷新 | 告警 | 13h |
| Account-B soak clock | evidence `soak_seconds` | 30m | 同一 hook 自动刷新 | 告警 | 13h |
| Account-B top-level evidence age | evidence `issued_at` | 1h | 同一 hook 原子替换 evidence/signature | 告警 | 13h |
| Testnet emergency close age | signed live-trade report `verified_at` | 24h | 同一 hook 自动刷新 | 告警 | 13h |
| Rollout phase advancement | closure report/signature/public key、maintenance fence | 单次确认 | 独立 `jp24_confirm_rollout_advance_20260818.py` 命令 | 告警/人工确认路径；部署脚本不推进下一账号 | 13h |
| Soak clock | account canary evidence、live-trade report | 30m | preflight hook 或单账号 canary 重新采集 | 告警 | 13h |

## 验收注入

硬闸注入使用：

```bash
DEPLOY_ALLOW_GATE_FAILURE_INJECTION=1 \
DEPLOY_INJECT_GATE_FAILURE=image \
bash hk-deploy-20260803.sh preflight
```

降级项注入使用：

```bash
DEPLOY_ALLOW_GATE_FAILURE_INJECTION=1 \
DEPLOY_INJECT_DEGRADED_GATE=capacity-age \
bash hk-deploy-20260803.sh preflight
```

验收条件：硬闸失败时部署拒绝且 `DOWNTIME_WINDOW_ENTERED=0`；降级项失败时部署继续并产生 `gate warning`；两类注入均要求 A-D heartbeat sequence 持续增加，Docker `stop/rm/run/create` 计数为零。
