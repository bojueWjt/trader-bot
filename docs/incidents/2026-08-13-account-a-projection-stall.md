# 2026-08-13 Account-A Projection Stall

- 状态：根因链已确认，代码修复已完成，等待发布窗口
- 影响账户：`account-a`
- 故障开始：2026-08-13 18:50:52 UTC
- 恢复观察：2026-08-20 UTC
- 关联 ADR：`docs/adr/2026-08-08-account-node-stall-hardening.md`

## Impact

`account-a` execution projection 从 2026-08-13 起停止 egress。2026-08-20 重启后的
reconciliation replay 将最老事件延迟暴露为 `6 days 13:32:30.290468`。

重启 replay 统计：

| 指标 | 数量 |
|---|---:|
| replayed rows | 27 |
| replayed fills | 17 |
| replayed orders | 3 |
| replayed symbols | 2 |

2026-08-13 起已入账的推断成交盘点：

| 指标 | 数量 |
|---|---:|
| inferred fill rows | 48 |
| distinct inferred orders | 48 |
| symbols | 5 |
| gross quantity | 466.710 |
| gross notional | 170896.9134 |
| foreign-symbol rows | 47 |
| owned-inventory rows | 1 |

foreign symbols 主要为 `GOOGLUSDT`、`MUUSDT`、`SPCXUSDT`、`SNDKUSDT`。
`orders_projection` 当前没有这些 inferred client order 的残留订单行。
`positions_projection` 保留 3 个 quantity 为零、状态 closed 的 `*-EXTERNAL` 历史行。

## Evidence

故障窗口的首个明确 incident：

```text
2026-08-13 18:50:52 UTC
projection failed:
execution projection durable spool made no egress progress
```

同一窗口还观察到：

- control-plane database pool exhausted；
- Redis maxmemory 使用率达到 100%；
- Binance `-1003 Too many requests`；
- `account-a` provider 加载 871 个 instruments；
- reconciliation 约每 5 分钟运行一次。

## Root Cause

1. projection durable spool 的 egress worker 在 2026-08-13 停止推进。
2. 运行时只有 backlog/lag 结果信号，缺少独立 worker progress watchdog，停滞持续到进程
   崩溃和重启。
3. 重启触发 exchange-first reconciliation，旧交易所历史事件重新进入本地 execution
   state 和 projection。
4. 全量 871 instrument provider 与约 5 分钟一次的 reconciliation 扩大 Binance 请求权重，
   在 `-1003` 窗口进一步降低恢复能力。
5. reconciliation 和 projection 缺少 symbol/ownership 边界，手工 symbol 与
   `EXTERNAL` 推断仓位进入投影。

数据库池耗尽、Redis 100% 和 Binance rate limit 是同一窗口的放大因素。可证明的直接故障
是 projection worker egress 无 progress；现有证据无法将该停滞唯一归因到单一外部资源。

## Corrective Actions

- projection egress 每 60 秒接收 progress probe，600 秒没有完成 flush 或空闲 egress cycle
  时创建
  `projection_progress_stall` incident，恢复时关闭 incident。
- live lease owner 改为稳定 `node_id`；同身份陈旧租约允许 fenced takeover，旧版本
  `node_id:hostname:pid:uuid` owner 仅在陈旧时兼容接管，跨身份陈旧租约保持 fail-closed。
- live instrument provider 与 execution-engine reconciliation 使用
  `risk.max_notional_per_order` inventory 白名单。
- continuous reconciliation 的 order/position report 和 startup mass status
  都按 owned `InstrumentId` 逐 symbol 发请求；Binance adapter 不再把单 symbol
  请求扩展回缓存中的其他 active symbol。
- live projection 同时要求 owned instrument、机器人 client order ID，并过滤
  `*-EXTERNAL` position event。
- position ownership 隔离采用 `(account, instrument, position_side)` ledger。推断成交只
  允许更新具备机器人 ownership proof 的仓位；其余差异进入 unattributed 和人工对账。

## Deployment Status

截至 2026-08-20，本文件记录的修复位于本地分支
`codex/ownership-isolation`。jp-24 服务尚未发布新镜像，远端容器尚未因本修复重启。
stable lease owner、600 秒 watchdog、provider/reconciliation 请求白名单和 projection
filter 将在新镜像部署后生效。
