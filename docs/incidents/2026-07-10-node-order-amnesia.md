# 2026-07-10 节点挂单失忆 / TTL 撤单风暴 / Hermes DM 卡死

## 症状

- Hermes 每 2 小时向用户轰炸 ⚠️「超龄挂单撤销没成功：撤单请求已存在但状态是
  rejected」（ETH/MU/SOL 多笔，持续两天）。
- 用户口头指令「ETH多单止损调整为1725」被拒（`position_not_unique`）。
- 「裸仓检测」给早已平掉的 TAO 空仓补止损，被拒（`position_required`）。
- 用户给 Hermes 发消息无回应/乱回复（15:49/16:22 UTC 两条消息被吞）。
- 日报数据严重失真（挂单 25 笔 vs 交易所真实 8 笔、ETH 数量 4.167 vs 2.053）。

## 根因链

### 1. 节点重启即失忆（撤单 `order_not_found` 的病根）

- nautilus Binance 适配器 `_parse_order_status_reports` 把
  `order.time < start_ms` 的单全部丢弃，而启动对账带
  `reconciliation_lookback_mins=60` → **挂龄超 1 小时的在场挂单在每次节点重启
  后被静默丢出对账**。日志铁证：每次启动 `Received 0 OrderStatusReports`
  （仓位报告正常，连接无恙）。
- 第二层：`open_only=False` 路径丢弃 openOrders 快照、只用 per-symbol
  allOrders，而 futures allOrders 不返回创建超 ~7 天的单（即使仍在场）——
  account-b 的 6 月老单因此永远失踪。
- 第三层：Redis cache 持久化失效——cache key 的 instance 段是每次启动随机的
  UUID（配置的 `INSTANCE-ACCOUNT-A` 没进 key），每次启动
  `Cached 0 orders from database`，Redis 积累 11.7 万孤儿 key（未修，
  对账修复后影响消除）。
- 结果：cancel planner 只查节点缓存 → 重启前的单一律
  `denied:order_not_found`，而单子在币安上活着。
- 叠加幂等回放：operator 端点同 client_ref 重试直接返回旧 rejected 意向
  （「已存在同一撤单请求但状态是 rejected」），换 ref 也无用 → 死循环。

### 2. 投影层与交易所脱节（TTL 风暴与假警报的燃料)

节点僵死窗口丢失终态事件 + 失忆单永无后续事件 → orders_projection 有 17 笔
幽灵「在场」单、positions_projection 有已平仓位（TAO）和幽灵仓（ETH-BOTH
0.02）、数量漂移。order_lifecycle_monitor 的 TTL 扫描只读投影 → 给不存在的
单反复开撤单任务（续期耗尽后每 2h 重唤醒）。

### 3. hedge 双向持仓无法定位（口头调止损失败）

ETH 多空同在 account-a，planner `_select_target_position` 无 side 线索 →
`position_not_unique`。operator API / v3_trade skill 均无 side 传递路径。

### 4. 投影 lag 守卫静默永久停机（修复过程中发现）

event_mapper `_timestamp_to_datetime` 把缺失/为 0 的 ts_event 变成 1970 →
projection actor 算出 56 年「滞后」→ `mark_projection_failed` → `_halt()`
**不打任何日志**地把节点置 HALTED，且永不自愈。修第一笔撤单后 2 分钟内
node-a 就这样静默死亡（靠 :8081/ready 的 halt_reason 才定位）。

### 5. Hermes DM 管道卡死（乱回复的真相）

gateway 收到用户消息只 flush 不处理（无 inbound/response 日志），同时 TTL
cron 刷屏在时间上像「乱回复」。重启 hermes-gateway-trader 恢复。当天上游
LLM（api.balenw.cloud gpt-5.5）间歇超时是诱因之一，精确卡点未定位
（证据存 hk `/root/incident-20260710-dm-freeze/`）。

## 修复（全部已部署 hk 并验证）

| # | 修复 | 位置 |
|---|------|------|
| 1 | 开放单不受对账 lookback 过滤 + openOrders 快照合并兜底 | `container-patches/binance_execution.py`（挂载进两节点） |
| 2 | TTL 扫描按账户对照 exchange_state_mirror，幽灵单直接终态化投影（`lifecycle_heal` 标记），不再唤醒 Hermes | `scripts/order_lifecycle_monitor.py` |
| 3 | 一次性投影修复：16+5 笔幽灵单终态化、TAO/ETH-BOTH 幽灵仓关闭、数量按 mirror 对齐（备份表 `heal_bak_20260710_*`） | hk 数据库 |
| 4 | operator API + planner 支持 `position_side`（move_stop_loss/replace_take_profits/close/partial），止损方向校验按 side 收窄 | `read_api.py`、`intent_execution_planner.py`、hermes profile `v3_trade.py --side` + SKILL.md |
| 5 | ts_event 缺失/1970 → 用 ingest 时间兜底；`_halt()` 必须打日志 | `event_mapper.py`、`runtime/lifecycle.py` |
| 6 | Hermes 模型切换 gpt-5.5 → gpt-5.6-sol（reasoning medium），brain probe 同步 | hermes profile config.yaml、monitor `BRAIN_PROBES` |

## 验证

- 节点重启后对账：node-a `Received 6 OrderStatusReports`、node-b `2`（此前恒 0）。
- 卡了 100-137 小时的 3 笔 ETH 老单（SELL 1.141@1840、SELL 0.799@1846.30、
  BUY 0.775@1710）经 operator 撤单全部成功，币安实盘确认消失。
- 双节点 RESUME 后 12 小时保持 ACTIVE、零 HALT。
- 幽灵单自愈在真实运行中生效（18:00 自动 healed 2 笔）；投影与交易所全量对齐。
- `--side long` dry_run 通过方向校验；planner manage 测试套件全绿
  （open 套件 1 个失败为存量 hedge 分歧，与本次无关）。
- Hermes DM 恢复（07-11 05:25 用户指令正常处理），新模型 gpt-5.6-sol 生效。

## 遗留 / 未修

- Redis cache instance_id 不稳定 + 11.7 万垃圾 key（P2，对账修复后无行为影响）。
- 投影 lag>30s 仍会（现在有日志地）HALT 且不自愈——策略待议。
- DM 卡死的精确 await 点未定位；gateway 无卡死自愈/告警。
- Hermes DM 场景缺「拉频道最近 N 条消息」接口（07-11 用户查舒琴消息时暴露）。
- PENDLE `protection_revisions_exhausted`：重启后保护单重放撞 revision 上限
  （保护单在场，无实际风险，属噪音）。
- account-b 两笔 6 月 aos_ 外部老单（BTC 65245.5、SPCX 193）仍在场——用户
  自管，未动。
