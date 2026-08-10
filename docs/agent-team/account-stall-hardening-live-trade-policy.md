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
- durable journal、intent、outbox 和 evidence store 可写且有剩余容量。
- rollout phase 为 `account_a_canary`，受限 canary RESUME gate 已绑定当前 release。
- `process_liveness=false` 或 `loss_monitor_healthy=false` 未出现。
- `OPEN` 前取得 freshness 窗口内的交易所快照，确认目标仓位和订单归零。快照早于
  `RESUME` ACK 时记录 causality degradation，保留 freshness 和目标归零硬校验。
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
- 新鲜交易所 preflight 已确认目标归零后，`/v1/nodes` timeout、普通 5xx 或
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
- `exchange_authoritative` 标记缺失或为 false，同时 exchange source、freshness 和目标归零
  证据有效。
- 非目标持仓、普通挂单或 algo 挂单相对签名审计快照发生变化。执行器记录签名哈希、
  实时哈希和发生阶段，目标交易继续执行。
- `orders_projection` 缺行或延迟，同时 durable `OrderFilled` 事件已经提供完整
  client order ID、trade ID、成交数量、成交价和 commission。
- CLOSE ACK 或 mirror advancement 超时，同时同一 deterministic CLOSE 已发出，后续新鲜
  交易所快照已证明目标仓位归零。

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
2. **记录审计快照**：确认 `SOLUSDT` 无持仓和订单，签名记录全部非 `SOLUSDT` 的
   `portfolio_baseline_sha256`。该快照仅承担同账户并发活动审计职责。
3. **审计放行**：确认节点仍为 HALTED，创建单次 canary permit，通过 operator command
   将 account-a 单节点切换到 ACTIVE。
4. **开仓路径验证**：提交一个显式 quantity 和 limit price 的 `LIMIT + IOC` 开仓单；
   本轮 quantity 固定 `0.07`，并满足
   `quantity * limit_price <= permit.max_notional_usdt <= 12 USDT`。
5. **成交确认**：交易所历史或 durable `OrderFilled` 集合按 account、client order ID、
   symbol、trade ID、filled quantity 和 observed_at 给出成交证据。`orders_projection`
   作为可重建 enrichment，缺行或延迟不能覆盖 durable fill。IOC 未成交时禁止追加风险；
   只有交易所明确确认订单不存在时才允许使用相同 deterministic client order ID 进行
   幂等恢复。
6. **关闭新增风险窗口**：OPEN 进入终态、返回异常或结果不明确后，立即通过 operator
   command 将 account-a 切回 HALTED。首次 HALT 全部重试失败时仍推进撤单和平仓。
7. **平仓路径验证**：撤销残余 OPEN，查询当前目标仓位，按本轮实际 filled quantity
   和授权上限提交 capped exact reduce-only MARKET 平仓。本轮 close quantity 固定为
   当前目标仓位、本轮实际成交量和授权上限的最小值。ACK 丢失后的 reconciliation
   先查询目标仓位；仓位已归零时停止 CLOSE 重放，仓位仍存在时只重放同一 close identity
   和首次 quantity。残余仓位进入 BLOCKED 证据。
8. **确认最终安全态**：平仓流程结束后再次幂等 HALT，确保 durable ledger 最终记录
   HALTED。
9. **目标归零确认**：以最终 HALT 后的新鲜交易所镜像确认 `SOLUSDT` 仓位为零、普通挂单
   为零、algo 挂单为零。节点缓存与 PostgreSQL 投影差异进入 enrichment 告警。
   post-HALT 目标归零证明失败时，permit ledger 保持 recoverable，等待同一授权恢复流程。
10. **组合活动审计**：重新计算非 `SOLUSDT` 的 `portfolio_baseline_sha256`，记录与交易前
    签名快照的差异。
11. **财务证明**：open/close 唯一 trade ID 成交集合、成交数量守恒、逐 fill
    commission、开仓方向和成交价完整后认证手续费和净损益。交易所未提供 realized PnL
    时，按签名方向和两腿加权成交价推导 gross PnL。
12. **事故关闭**：记录费用、滑点、时间线和 release metadata，关闭验证 incident。

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
- exact reduce-only close 无法按实际成交数量提交，或最终仍存在目标风险敞口。
- 最终 HALT 或 HALT 后交易所归零快照无法证明。

heartbeat、readiness、projection、reconciliation、HTTP、circuit、资源压力和历史
incident 的软异常持续写入审计轨迹。执行器使用有界重试推进 RESUME、OBSERVE、查询和
证据采集。金融 enrichment 缺失发生在精确平仓和 HALT 之后，将结果降为 DEGRADED 并
提交可重复 enrichment 的 evidence。交易保护动作保持最高优先级。

已消费 permit 的只读终结使用 `--recover-evidence-only`。原签名文档即使已经过期，
仍须通过签名链和 ledger identity 校验，并携带 reviewer 独立签名的
`--evidence-recovery-gate`。recovery gate 绑定当前 executor/adapter hash、固定 ledger、
固定 evidence path 和 `final-snapshot`/`publish-evidence` 两项权限；`refresh_after`
超期只记录 warning。该模式只接受以下 ledger disposition：无 prepared evidence 的
`HALTED`、`HALTED + pending_action=PUBLISH_EVIDENCE` 且包含 `pending_evidence` 的
prepared disposition、`EVIDENCE_ENRICHMENT_PENDING` 或 `EVIDENCE_COMMITTED`。
`EVIDENCE_PREPARED` 只作为 history event，用于记录 prepared disposition 的形成。
最终快照发现残余目标风险时 ledger 进入
`RISK_RECOVERY_REQUIRED`，后续调用在 adapter 启动前停止。已提交 DEGRADED evidence
允许同 identity 的只读原子 enrichment。

## 证据要求

- Exchange：目标订单、成交、目标持仓、普通挂单和 algo 挂单快照，以及非目标组合基线。
- Node：actor tick、heartbeat、lifecycle、reconciliation 和 execution event 日志。
- PostgreSQL：intent、command、ack、execution event、order projection、position projection
  和 incident；projection 用于 enrichment，不阻塞交易所已证明的精确平仓。
- Release：git SHA、image digest、config hash、dependency lock hash。
- Financial：实际成交额、手续费、滑点和净损益。

所有证据脱敏，禁止输出 API key、secret、token、完整连接串或签名请求。
