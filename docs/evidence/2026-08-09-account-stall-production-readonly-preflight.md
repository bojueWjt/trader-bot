# Account Stall Production Read-only Preflight

- 采集日期：2026-08-09
- 采集窗口：12:09-12:12 UTC
- 目标：HK `/srv/trader-v3`
- 操作范围：SSH、Docker、Redis、PostgreSQL 和 HTTP 只读查询
- 真实交易：0

## 结论

当前生产仍运行 2026-08-03 的 `6ecad66` hotpatch lineage。该版本保留 actor callback
同步控制面 HTTP 路径，普通 heartbeat timeout 会把 `account-a` sticky HALT。

Redis 历史 generation 未收敛，主机已进入重度 swap。当前状态支持以下因果链：

```text
109 个历史 Redis generation
-> Redis 6.21 GiB / host 7.8 GiB
-> host swap 7.2 GiB
-> 控制面和 actor HTTP 响应长尾
-> heartbeat / command / intent timeout
-> 旧 lifecycle 将 soft timeout 升级为 sticky HALT
```

## 节点与时间线

| 项目 | 生产事实 |
|---|---|
| account-a container | `trader-v3-node-a`，启动于 2026-08-06 04:01:49 UTC |
| 首个本容器 heartbeat timeout | 2026-08-06 04:49:38 UTC |
| 首个本容器 sticky HALT | 2026-08-06 12:40:39 UTC |
| 当前 account-a | process live，readiness true，trading state HALTED |
| account-b container | 2026-08-08 07:26:57 UTC 以 exit 137 退出 |
| account-b DB heartbeat | 冻结于 2026-08-06 12:40:27 UTC，payload 仍为 ACTIVE |

account-b 证明状态值必须与 freshness 一起判定。冻结的 `ACTIVE/readiness=true` 不能代表
当前运行健康。

## Redis 与主机

| 项目 | 值 |
|---|---:|
| Host RAM | 7.8 GiB |
| Host available | 288 MiB |
| Host swap used | 7.2 GiB / 8.0 GiB |
| Redis used memory | 6.21 GiB |
| Redis maxmemory | 0，无上限 |
| Redis policy | `noeviction` |
| Redis keys | 191,854 |
| account-a generations | 67 |
| account-b generations | 42 |
| account-a keys | 117,912 |
| account-b keys | 73,943 |
| AOF | disabled |
| RDB last save | success，最近一次耗时 95 秒 |

Redis 没有容器 memory limit，节点和 PostgreSQL 也没有独立 cgroup memory limit。

## 数据真相

- `exchange_state_mirror` 在采集时保持新鲜。
- account-a 交易所镜像包含 3 个持仓、20 个普通 open order、17 个 algo order。
- account-b 交易所镜像包含 0 个持仓、1 个普通 open order、0 个 algo order。
- `SOLUSDT` 在两账户交易所镜像中均无持仓、普通订单或 algo order。
- `positions_projection` 仍保留 account-a 的 `SOLUSDT 1.10 long`，更新时间为
  2026-07-24 18:48:55 UTC；该记录与新鲜交易所镜像冲突，属于陈旧投影。
- 生产数据库只应用到 `0009`；本轮 `0010` 尚未部署。

真实小额交易必须使用新鲜 exchange mirror 冻结目标与非目标组合基线，同时把陈旧
PostgreSQL projection 作为 reconciliation incident 记录。

## 放行顺序

1. 部署 offloaded `NodeControlPlaneSession` 和 soft/hard failure classification。
2. 修复过期 `RESUME` 和 startup replay capacity+1。
3. 在统一 operation lock 下备份 Redis，保留当前 generation，清理 retired generation。
4. 配置 Redis maxmemory 和容器 memory limit，等待 swap 与 HTTP latency 收敛。
5. 恢复 account-b 到 HALTED，执行 exchange-first reconciliation。
6. account-a 连续 30 个 2 秒新鲜样本通过后执行受限 `12 USDT SOLUSDT` round trip。

## 可复现查询

```bash
ssh root@100.104.27.123 'docker ps -a --format "{{.Names}}|{{.Status}}"'
ssh root@100.104.27.123 'free -h'
ssh root@100.104.27.123 \
  'docker exec trader-v3-redis redis-cli INFO memory'
ssh root@100.104.27.123 \
  'docker exec trader-v3-postgres psql -U postgres -d trader -c \
  "select node_id,account_id,status,last_seen_at from node_heartbeats order by account_id"'
```
