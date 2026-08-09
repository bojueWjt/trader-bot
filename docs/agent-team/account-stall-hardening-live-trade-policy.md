# 真实小额交易验证策略

## 授权范围

| 项目 | 硬限制 |
|---|---:|
| 账户 | `account-a` |
| 最大开仓名义金额 | `12 USDT` |
| 最大累计净亏损 | `1.5 USDT`，含手续费与滑点 |
| 最大 round trip | `1` |
| 杠杆调整 | 禁止 |
| 保证金模式调整 | 禁止 |
| 新增挂单数量 | 一个微型 `LIMIT + IOC` 开仓单 |
| 重试 | 仅在交易所明确确认订单不存在时允许一次幂等重试 |

## 交易前置门

以下条件属于硬阻断：

- 当前 release commit、image digest、config hash 与已审查物料一致。
- account-a 节点处于 HALTED，当前 writer ownership、lease 和 fencing identity 一致。
- 目标 symbol 在系统既有允许列表内，交易所 filters 支持在 12 USDT 内构造合法数量。
- 目标 symbol 当前无持仓、无普通挂单、无 algo 挂单。
- 非目标持仓、普通挂单和 algo 挂单已规范化并冻结为签名的
  `portfolio_baseline_sha256`。
- durable journal、intent、outbox 和 evidence store 可写且有剩余容量。
- rollout phase 为 `account_a_canary`，受限 canary RESUME gate 已绑定当前 release。
- actor progress 与独立 mark-to-market loss monitor 正常推进。
- 当前控制面路径可提交 canonical intent，并可按实际成交量构造独立 client ID 的
  reduce-only close。

以下条件记录为 `DEGRADED` 并继续交易：

- heartbeat、readiness、projection 或 reconciliation 超过健康阈值。
- 控制面 HTTP timeout、5xx、circuit open 或瞬态 poll/ACK 失败。
- Redis、PostgreSQL、控制面或节点资源超过告警阈值，同时 durable 写入仍可完成。
- account-a 存在与本次 ownership、目标仓位和 loss monitor 无关的历史 incident。
- testnet emergency-close 证据陈旧，同时当前 adapter contract、数量精度和
  reduce-only 请求可在本地确定性验证。
- 普通 queue pressure、历史 enrich 延迟或 exchange mirror 的非目标字段延迟。

## Symbol 选择

本轮目标 symbol 固定为 `SOLUSDT`。执行前仍需重新确认：

1. 交易所 filters 可在额度内满足最小数量和最小名义金额。
2. 当前无 account-a 风险敞口和挂单。
3. 市场数据新鲜，价差和深度符合现有 price guard。
4. 客户端订单 ID、account ID、node ID 全部绑定到 account-a。

选择过程记录交易所 filters、价格、数量和预估费用。策略不包含市场方向判断，持仓时间保持最短。

## 执行顺序

1. **执行契约验证**：验证同形状 `LIMIT + IOC` 开仓、订单查询、撤单和独立 client ID
   的 exact reduce-only close 请求；testnet 结果进入证据并允许陈旧状态降级继续。
2. **冻结基线**：确认 `SOLUSDT` 无持仓和订单，签名冻结全部非 `SOLUSDT` 的
   `portfolio_baseline_sha256`。
3. **审计放行**：确认节点仍为 HALTED，创建单次 canary permit，通过 operator command
   将 account-a 单节点切换到 ACTIVE。
4. **开仓路径验证**：提交一个显式 quantity 和 limit price 的 `LIMIT + IOC` 开仓单；
   `quantity * limit_price <= permit.max_notional_usdt <= 12 USDT`。
5. **四层确认**：确认交易所 order/fill、节点事件、PostgreSQL execution event 和
   projection 一致。IOC 未成交时禁止追加风险；只有交易所明确确认订单不存在时才允许使用
   相同 deterministic client order ID 进行幂等恢复。
6. **平仓路径验证**：按实际 filled quantity 提交 reduce-only MARKET 平仓。
7. **恢复安全态**：立即通过 operator command 将 account-a 切回 HALTED。
8. **目标归零确认**：确认 `SOLUSDT` 仓位为零、普通挂单为零、algo 挂单为零，节点缓存与
   PostgreSQL 投影一致。
9. **组合保护确认**：重新计算非 `SOLUSDT` 的 `portfolio_baseline_sha256`，要求与交易前
   签名基线完全一致。
10. **事故关闭**：记录费用、滑点、时间线和 release metadata，关闭验证 incident。

## 自动停止条件

任一条件触发后立即停止新增风险并执行精确 reduce-only 平仓：

- account、node、release、writer、lease 或 fencing identity 不一致。
- durable journal、intent、outbox 或 evidence 写入失败或容量耗尽。
- actor progress 或持仓期间的 loss monitor 停止推进。
- OPEN 请求结果存在歧义，且交易所查询无法确认唯一订单状态。
- 订单数量、方向、position side、reduce-only 或 client order ID 与计划不一致。
- 累计净亏损达到 1.5 USDT。
- 出现额外目标仓位、额外目标挂单、重复 intent、重复 order 或跨账户数据。
- 非目标 `portfolio_baseline_sha256` 发生变化。
- exact reduce-only close 无法按实际成交数量提交或确认。
- 最终 HALT 或 HALT 后交易所归零快照无法证明。

heartbeat、readiness、projection、reconciliation、HTTP、circuit、资源压力和历史
incident 的软异常持续写入审计轨迹。执行器使用有界重试推进 RESUME、OBSERVE、查询和
证据采集，交易保护动作保持最高优先级。

## 证据要求

- Exchange：目标订单、成交、目标持仓、普通挂单和 algo 挂单快照，以及非目标组合基线。
- Node：actor tick、heartbeat、lifecycle、reconciliation 和 execution event 日志。
- PostgreSQL：intent、command、ack、execution event、order projection、position projection 和 incident。
- Release：git SHA、image digest、config hash、dependency lock hash。
- Financial：实际成交额、手续费、滑点和净损益。

所有证据脱敏，禁止输出 API key、secret、token、完整连接串或签名请求。
