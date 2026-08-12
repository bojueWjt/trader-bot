# 真实小额交易验证策略

## 授权范围

| 项目 | 硬限制 |
|---|---:|
| 账户 | `account-a`、`account-b`、`account-c`、`account-d` |
| 执行顺序 | `account-a` → `account-b` → `account-c` → `account-d` |
| 每账号最大开仓名义金额 | `12 USDT` |
| 每账号最大累计净亏损 | `1.5 USDT`，含手续费与滑点 |
| 每账号最大 round trip | `1` |
| 杠杆调整 | 禁止 |
| 保证金模式调整 | 禁止 |
| 新增挂单数量 | 一个微型 `LIMIT + IOC` 开仓单 |
| 重试 | 仅在交易所明确确认订单不存在时允许一次幂等重试 |

## 交易前置门

以下条件必须同时满足：

- 当前 release commit、image digest、config hash 与已审查物料一致。
- 当前账号节点 HALTED，actor tick age 小于 2 秒，heartbeat age 小于 5 秒。
- readiness 完整，reconciliation 为 HEALTHY，projection lag 小于 5 秒。
- 目标 symbol 在系统既有允许列表内。
- 交易所当前 filters 允许在 12 USDT 内构造合法最小数量。
- 当前账号 USDT-M 可用余额足以完成本次开仓、手续费和紧急平仓。
- 目标 symbol 当前无持仓、无普通挂单、无 algo 挂单。
- 非目标持仓、普通挂单和 algo 挂单已规范化并冻结为签名的
  `portfolio_baseline_sha256`。
- 当前账号没有开放 P0/P1 incident。
- Redis、PostgreSQL、控制面和节点资源均在验收阈值内。
- emergency reduce-only close 路径已在 testnet 对相同 order shape 验证。
- rollout phase 与当前账号一致：`account_a_canary`、`account_b_rollout`、
  `account_c_rollout` 或 `account_d_rollout`。
- 当前账号的受限 canary RESUME gate 已绑定当前 release；其余账号保持 HALTED。
- 前一账号已完成签名闭环、目标仓位归零并回到 HALTED。
- 独立 mark-to-market loss monitor 正常运行，mark freshness、position quantity、
  fees 和 cumulative PnL 可观测。

## Symbol 选择

本轮目标 symbol 固定为 `SOLUSDT`。执行前仍需重新确认：

1. 交易所 filters 可在额度内满足最小数量和最小名义金额。
2. 当前账号无 `SOLUSDT` 风险敞口和挂单。
3. 市场数据新鲜，价差和深度符合现有 price guard。
4. 客户端订单 ID、account ID、node ID 和 rollout phase 全部绑定到当前账号。

选择过程记录交易所 filters、价格、数量和预估费用。策略不包含市场方向判断，持仓时间保持最短。

## 执行顺序

1. **测试网闭环**：验证同形状 `LIMIT + IOC` 开仓、订单查询、撤单和 emergency
   reduce-only close 路径。
2. **冻结基线**：确认 `SOLUSDT` 无持仓和订单，签名冻结全部非 `SOLUSDT` 的
   `portfolio_baseline_sha256`。
3. **审计放行**：确认当前节点仍为 HALTED，创建单次 canary permit，通过 operator
   command 将当前账号单节点切换到 ACTIVE。
4. **开仓路径验证**：提交一个显式 quantity 和 limit price 的 `LIMIT + IOC` 开仓单；
   `quantity * limit_price <= permit.max_notional_usdt <= 12 USDT`。
5. **四层确认**：确认交易所 order/fill、节点事件、PostgreSQL execution event 和
   projection 一致。IOC 未成交时禁止追加风险；只有交易所明确确认订单不存在时才允许使用
   相同 deterministic client order ID 进行幂等恢复。
6. **平仓路径验证**：按实际 filled quantity 提交 reduce-only MARKET 平仓。
7. **恢复安全态**：立即通过 operator command 将当前账号切回 HALTED。
8. **目标归零确认**：确认 `SOLUSDT` 仓位为零、普通挂单为零、algo 挂单为零，节点缓存与
   PostgreSQL 投影一致。
9. **组合保护确认**：重新计算非 `SOLUSDT` 的 `portfolio_baseline_sha256`，要求与交易前
   签名基线完全一致。
10. **事故关闭**：记录费用、滑点、时间线和 release metadata，关闭验证 incident。
11. **签名推进**：签署当前账号闭环证据，推进 rollout phase，再开始下一账号。

## 自动停止条件

任一条件触发后立即停止新增风险并执行精确 reduce-only 平仓：

- 任一身份字段不一致。
- A-D 任一 image digest 或 config hash 发生变化。
- actor tick、heartbeat、projection 或 reconciliation 变 stale。
- 请求结果存在超时歧义，且交易所查询无法确认订单状态。
- 订单数量、方向、position side、reduce-only 或 client order ID 与计划不一致。
- 累计净亏损达到 1.5 USDT。
- mark price 缺失、陈旧，或持仓期间 loss monitor 停止推进。
- 出现额外仓位、额外挂单、重复 intent、重复 order 或跨账户数据。
- 非目标 `portfolio_baseline_sha256` 发生变化。
- 前一账号闭环证据缺失、签名失效或未恢复 HALTED。

## 证据要求

- Exchange：目标订单、成交、目标持仓、普通挂单和 algo 挂单快照，以及非目标组合基线。
- Node：actor tick、heartbeat、lifecycle、reconciliation 和 execution event 日志。
- PostgreSQL：intent、command、ack、execution event、order projection、position projection 和 incident。
- Release：git SHA、image digest、config hash、dependency lock hash。
- Financial：实际成交额、手续费、滑点和净损益。

所有证据脱敏，禁止输出 API key、secret、token、完整连接串或签名请求。
