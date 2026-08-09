# 真实小额交易验证策略

## 授权范围

| 项目 | 硬限制 |
|---|---:|
| 账户 | `account-a` |
| 最大开仓名义金额 | `12 USDT` |
| 最大累计净亏损 | `< 1.5 USDT`，含手续费与滑点 |
| 最大 round trip | `1` |
| 杠杆调整 | 禁止 |
| 保证金模式调整 | 禁止 |
| 新增挂单数量 | 一个微型 `LIMIT + IOC` 开仓单 |
| 重试 | 仅在交易所明确确认订单不存在时允许一次幂等重试 |

## 交易前置门

以下条件属于硬阻断：

- 当前 release commit、image digest、config hash 与已审查物料一致。
- account-a 节点处于 HALTED，当前 writer ownership、lease 和 fencing identity 一致。
- 目标 symbol 在系统既有允许列表内；交易所 filters 可用时，已知值支持在 12 USDT 内
  构造合法数量。
- 目标 symbol 当前无持仓、无普通挂单、无 algo 挂单。
- 非目标持仓、普通挂单和 algo 挂单已规范化并冻结为签名的
  `portfolio_baseline_sha256`。
- 非目标持仓基线只包含 `symbol`、`position_side`、`position_amt`、`entry_price`、
  `leverage`、`margin_type`、`isolated_margin` 和 `is_auto_add_margin`。
  `mark_price`、`unrealized_pnl`、`notional`、`liquidation_price`、其他行情派生字段和
  未知扩展字段不进入阻断哈希。
- 非目标普通挂单和 algo 挂单继续完整进入阻断哈希。
- durable journal、intent、outbox 和 evidence store 可写且有剩余容量。
- rollout phase 为 `account_a_canary`，受限 canary RESUME gate 已绑定当前 release。
- `process_liveness=false` 或 `loss_monitor_healthy=false` 未出现。
- `RESUME` 后、`OPEN` 前取得同一轮新鲜交易所快照，确认目标仓位和订单归零、非目标
  组合基线未变化。
- before-open 节点状态存在明确值时，`ACTIVE`、`RUNNING` 或 `RESUMED` 允许新增风险；
  `HALTED` 和 `STOPPED` 阻断新增风险。
- 可用 USDT 余额存在明确值时，该值覆盖
  `quantity * limit_price + fee_reserve_usdt`。
- quantity step、min quantity、price tick、min notional 存在明确值时，全部满足。
- 当前控制面路径可提交 canonical intent，并可按实际成交量构造独立 client ID 的
  reduce-only close。

以下条件记录为 `DEGRADED` 并继续交易：

- heartbeat、readiness、projection 或 reconciliation 超过健康阈值。
- 控制面 HTTP timeout、普通 5xx、circuit open 或瞬态 poll/ACK 失败。
- 新鲜交易所 preflight 已确认目标归零和组合基线后，`/v1/nodes` timeout、普通 5xx 或
  node snapshot 缺失。
- heartbeat、execution-event 或 loss-monitor 纯遥测发布失败，包括普通永久 4xx 拒绝；
  本地 durable spool 保留，401/403/409 与 identity/fencing 冲突继续硬阻断。
- Redis、PostgreSQL、控制面或节点资源超过告警阈值，同时 durable 写入仍可完成。
- account-a 存在与本次 ownership、目标仓位和 loss monitor 无关的历史 incident。
- emergency-close 证据来自确定性 `contract_replay`，同时当前 adapter contract、数量
  精度和 reduce-only 请求已经通过受审查回放。
- 普通 queue pressure、历史 enrich 延迟或 exchange mirror 的非目标字段延迟。
- `risk_healthy` 缺失或为 false。
- actor、loss monitor、ownership、fencing、durability、writer、lease、余额或
  exchange filter 遥测缺失；后续一旦取得明确值，任何显式 unhealthy、identity
  mismatch、余额不足、filter 违规或 durable failure 立即转为硬阻断。
- actor/loss monitor 签名时间戳或节点 progress 时间戳陈旧。
- `exchange_authoritative` 标记缺失或为 false，同时 exchange source、freshness、目标归零和
  组合基线证据有效。

## Symbol 选择

本轮目标 symbol 固定为 `SOLUSDT`。执行前仍需重新确认：

1. 交易所 filters 可在额度内满足最小数量和最小名义金额。
2. 当前无 account-a 风险敞口和挂单。
3. 市场数据新鲜，价差和深度符合现有 price guard。
4. 客户端订单 ID、account ID、node ID 全部绑定到 account-a。

选择过程记录交易所 filters、价格、数量和预估费用。策略不包含市场方向判断，持仓时间保持最短。
2026-08-09 20:39 UTC 启动的 account-a 活跃 Redis generation 中，`SOLUSDT` instrument
记录 `min_notional=5 USDT`、`price_increment=0.01`、`size_increment=0.01` 和
`min_quantity=0.01`。签名 permit 绑定该新鲜运行时证据。

## 执行顺序

1. **执行契约验证**：验证同形状 `LIMIT + IOC` 开仓、订单查询、撤单和独立 client ID
   的 exact reduce-only close 请求。新鲜 testnet execution 可直接进入证据；
   `contract_replay` 以签名降级告警进入证据。
2. **冻结基线**：确认 `SOLUSDT` 无持仓和订单，签名冻结全部非 `SOLUSDT` 的
   `portfolio_baseline_sha256`。
3. **审计放行**：确认节点仍为 HALTED，创建单次 canary permit，通过 operator command
   将 account-a 单节点切换到 ACTIVE。
4. **开仓路径验证**：提交一个显式 quantity 和 limit price 的 `LIMIT + IOC` 开仓单；
   本轮 quantity 固定 `0.07`，并满足
   `quantity * limit_price <= permit.max_notional_usdt <= 12 USDT`。
5. **成交确认**：交易所历史按 account、client order ID、symbol、filled quantity 和
   observed_at 给出因果绑定的成交证据。节点事件与 PostgreSQL projection 作为 enrichment
   记录延迟和差异。IOC 未成交时禁止追加风险；只有交易所明确确认订单不存在时才允许使用
   相同 deterministic client order ID 进行幂等恢复。
6. **平仓路径验证**：按实际 filled quantity 提交 reduce-only MARKET 平仓。
7. **恢复安全态**：立即通过 operator command 将 account-a 切回 HALTED。
8. **目标归零确认**：以 CLOSE 派发后的新鲜交易所镜像确认 `SOLUSDT` 仓位为零、普通挂单
   为零、algo 挂单为零。节点缓存与 PostgreSQL 投影差异进入 enrichment 告警。
9. **组合保护确认**：重新计算非 `SOLUSDT` 的 `portfolio_baseline_sha256`，要求与交易前
   签名基线完全一致。
10. **财务证明**：open/close 订单、唯一 trade ID 成交集合、成交数量守恒、逐 fill
    commission、开仓方向、signed realized PnL 与成交价全部完整后，才认证手续费和净损益。
11. **事故关闭**：记录费用、滑点、时间线和 release metadata，关闭验证 incident。

## 自动停止条件

任一条件触发后立即停止新增风险并执行精确 reduce-only 平仓：

- account、node、release、writer、lease 或 fencing identity 不一致。
- 节点或 loss-monitor 发布返回 401、403、409，或明确 identity/fencing conflict。
- durable journal、intent、outbox 或 evidence 写入失败或容量耗尽。
- 节点明确报告 `process_liveness=false` 或 `loss_monitor_healthy=false`。
- OPEN 请求结果存在歧义，且交易所查询无法确认唯一订单状态。
- 订单数量、方向、position side、reduce-only 或 client order ID 与计划不一致。
- 累计净亏损达到 permit 阈值；permit 阈值严格低于 `1.5 USDT`。
- 出现额外目标仓位、额外目标挂单、重复 intent、重复 order 或跨账户数据。
- 非目标结构 `portfolio_baseline_sha256` 发生变化。
- exact reduce-only close 无法按实际成交数量提交或确认。
- 最终 HALT 或 HALT 后交易所归零快照无法证明。
- 最终手续费、滑点和净损益证明缺失，无法认证累计净亏损低于 permit 阈值。

heartbeat、readiness、projection、reconciliation、HTTP、circuit、资源压力和历史
incident 的软异常持续写入审计轨迹。执行器使用有界重试推进 RESUME、OBSERVE、查询和
证据采集。金融 enrichment 缺失发生在精确平仓和 HALT 之后，只阻断最终 PASS 认证。
交易保护动作保持最高优先级。

## 证据要求

- Exchange：目标订单、成交、目标持仓、普通挂单和 algo 挂单快照，以及非目标组合基线。
- Node：actor tick、heartbeat、lifecycle、reconciliation 和 execution event 日志。
- PostgreSQL：intent、command、ack、execution event、order projection、position projection
  和 incident；projection 用于 enrichment，不阻塞交易所已证明的精确平仓。
- Release：git SHA、image digest、config hash、dependency lock hash。
- Financial：实际成交额、手续费、滑点和净损益。

所有证据脱敏，禁止输出 API key、secret、token、完整连接串或签名请求。
