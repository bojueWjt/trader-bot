# 订单管理系统完善计划（Order Management v1）

> 版本：v1.0  
> 日期：2026-06-21  
> 基线：`work/integration-acceptance-v3`（用户提供头提交 `fe9bfb7`）  
> 目标路径：`docs/order-management/PLAN.md`  
> 任务账本：`docs/order-management/taskList.json`  
> 发布上限：完成全部验收前保持 `testnet_only`，不得切 live。

---

## 0. 当前判断

现有代码已经打通“决策 → 风控批准 → 下单 → 成交事件 → 投影”的执行骨架，但还不构成完整订单管理系统。当前主要缺口包括：

- 订单没有完整、持久化、可恢复的生命周期状态机；
- `move_stop_loss`、`partial_close`、止损/止盈编排尚未完整落地；
- `cancel_all` / `close_all` 是 best-effort 路由，缺少批处理、节流、重试、最终状态核验与终态 ACK；
- 节点拒单和执行失败没有稳定回写到 intent/order 管理状态；
- 缺少主动行情与账户状态 freshness 监控，静默期不能证明仍与交易所同步；
- 风险参数存在，但未形成基于实时 equity、available margin、止损距离的动态仓位计算与资金预留；
- 设置没有统一后端、版本、覆盖层级、审计、回滚和完整 UI；
- 订单中心缺少完整详情、生命周期时间线、保护单状态和可控人工操作。

本计划把这些缺口收敛为一个可验收的订单管理域，范围包括订单、仓位、保护单、价格监控、资金管理、对账、紧急命令和完整设置面板。

---

## 1. 目标

### 1.1 业务目标

1. 每个批准意图都能确定性地进入一个可追踪、可恢复、可幂等的订单生命周期。
2. 开仓后必须拥有符合策略的保护单；保护单缺失或失效时自动进入安全态并告警。
3. 支持市场单、限价单、区间入场、超时撤单、有限次重挂、部分成交策略和滑点保护。
4. 支持止损、分批止盈、移动止损、保本、追踪止损、部分平仓、全平。
5. 支持节点/控制面重启后的恢复、交易所对账、孤儿订单和外部持仓处理。
6. 基于实时账户权益、可用保证金、止损距离和风险上限进行动态仓位计算。
7. 所有订单管理参数均可通过设置面板查看、验证、版本化修改和回滚。
8. 所有高危变更都有 RBAC、原因、二次确认、审计事件和 release gate。

### 1.2 技术目标

- PostgreSQL 是应用查询与审计真相源；交易所是执行现实真相源。
- Nautilus 节点是唯一订单提交、修改、撤销与平仓执行者。
- Control-plane 负责策略无关的订单状态机、风险预算、设置、对账与命令编排。
- Dashboard 只调用 control-plane API，不直连交易所或节点。
- 所有跨服务命令具备 idempotency key、request_id、版本和可重放语义。
- 运行时配置采用“全局默认 → 账户覆盖 → 币种覆盖”的确定性合并规则。

---

## 2. 非目标

- 不在本阶段修改 Hermes 的信号理解或 alpha 策略。
- 不把交易所密钥放入 PostgreSQL 或设置面板。
- 不在未完成 testnet 验收前启用主网。
- 不允许 Dashboard 直接调用 Binance 或 Nautilus 内部对象。
- 不用本地内存、浏览器 localStorage 或 fixture 作为生产设置真相源。
- 不用空 heartbeat 冒充价格、账户或投影 freshness。

---

## 3. 铁律

1. **先保护、后扩张**：新仓成交后，保护单未安装完成前不得继续增加风险。
2. **交易所现实优先**：订单/持仓冲突时先 HALT/REDUCING，再以交易所现实对账。
3. **失败关闭**：行情过期、账户过期、投影过期、配置无效或对账失败时禁止新增风险。
4. **减仓通道常开**：HALTED 阻止新增风险，但允许 cancel、partial close、close、close_all。
5. **幂等贯穿全链**：decision、intent、execution job、order、command、event 均有稳定幂等键。
6. **保护单优先使用交易所原生单**：STOP_MARKET/TAKE_PROFIT 等原生保护优先，应用触发仅作明确的兼容方案。
7. **设置必须版本化**：运行时不得读取未审计、未版本化的散落环境变量作为业务配置。
8. **危险操作必须留痕**：修改 live 参数、放宽风险、close_all、rollback 都要求权限、reason、request_id 与审计。
9. **不自动猜测**：目标持仓、订单、账户或币种无法唯一解析时进入 `needs_review`。
10. **验收证据可复现**：每个 done 任务必须有命令、输出/报告路径和 commit SHA。

---

## 4. 总体架构

```text
HermesDecision
      │
      ▼
Decision Gateway ── Risk/Money Manager ── Effective Settings
      │                       │
      ▼                       ▼
ApprovedTradeIntent ── Execution Job / Outbox
      │
      ▼
Nautilus Node ── Binance Market Data + User Data + Execution
      │                       │
      ├──── execution events ─┘
      ▼
Control-plane reducers
  ├─ order_projection
  ├─ position_projection
  ├─ account_projection
  ├─ protection_projection
  ├─ price_feed_status
  └─ reconciliation_runs
      │
      ├─ Order Manager / Watchdogs / Command Orchestrator
      ├─ Alerts / Audit
      └─ Dashboard Order Center + Settings Panel
```

### 4.1 组件职责

| 组件 | 职责 |
|---|---|
| Decision Gateway | 验证意图、解析账户/币种、调用资金管理、生成批准意图 |
| Money Manager | 动态仓位、保证金、敞口、日亏损、回撤和资金预留 |
| Order Manager | 订单/保护单状态机、超时、重挂、重试、管理动作和最终状态 |
| Nautilus Node | 唯一交易所执行入口；提交/修改/撤销/平仓并回传事件 |
| Price Monitor | 订阅 mark/last/book ticker；维护 freshness 与触发状态 |
| Reconciler | 启动及周期对账；处理 drift、孤儿订单和外部持仓 |
| Settings Service | 配置 schema、覆盖层级、版本、验证、发布、回滚、审计 |
| Dashboard | 订单中心、完整设置面板、操作确认、版本历史与运行状态 |

---

## 5. 领域模型与状态机

### 5.1 Intent 状态

```text
approved
  → dispatched
  → node_accepted
  → executing
  → completed

任意阶段可进入：
  rejected | denied | failed | expired | cancelled | needs_review
```

Intent 的终态必须由执行结果或显式失败决定，不能只依赖 cursor 已推进。

### 5.2 Order 状态

```text
planned
  → pending_submit
  → submitted
  → accepted
  → working
  → partially_filled
  → filled

旁路终态：
  rejected | denied | pending_cancel → cancelled | expired | failed | lost
```

规则：

- 状态转换由事件 reducer 驱动，非法倒退必须记录 anomaly；
- 每个订单必须关联 `intent_id`、`execution_job_id`、`account_id`、规范化 `instrument_id`；
- parent/child、entry/stop_loss/take_profit/exit 通过 `order_links` 表达；
- `lost` 只表示本地无法解释，必须触发对账，不表示交易所订单不存在。

### 5.3 Position 状态

```text
opening → open → reducing → closed
                ↘ external / reconciliation_required
```

### 5.4 Command 状态

```text
requested → accepted → running → verifying → completed
                                  ↘ partial | failed | timed_out
```

`cancel_all` / `close_all` 只有在交易所最终核验通过后才能 `completed`。

---

## 6. 入场订单管理

### 6.1 支持模式

- `market`
- `limit`
- `zone`：按 side 选择确定性的边界价格
- TIF：GTC / IOC / FOK / GTD
- post-only（交易所与适配器支持时）
- reduce-only 仅用于退出，不允许开仓误用

### 6.2 滑点与价格保护

- market 下单前校验参考 mark/last；
- 超过 `max_slippage_bps` 或价格 freshness 过期时拒绝；
- limit/zone 校验价格精度、最小名义金额和 percent-price filter；
- 所有价格计算使用 Decimal 与交易所 instrument increment。

### 6.3 未成交与重挂

- `unfilled_timeout_seconds`
- `reprice_interval_seconds`
- `max_reprices`
- `max_submit_retries`
- 指数退避 + jitter
- 重挂前必须确认旧单已撤或已终态
- 禁止在网络不确定时无条件提交替代订单

### 6.4 部分成交策略

可配置：

- `keep_remainder`
- `cancel_remainder`
- `convert_remainder_to_market`
- `abort_and_reduce_filled`

并配置：

- `minimum_fill_ratio`
- `partial_fill_timeout_seconds`
- `market_conversion_max_slippage_bps`

---

## 7. 保护单与退出管理

### 7.1 保护安装

- 开仓成交后立即创建保护计划；
- 默认要求 stop loss；
- 保护安装超时进入 `protection_missing`，节点切 REDUCING/HALTED 并告警；
- 每次持仓数量变化后重新核对保护数量；
- 不允许 TP/SL 总退出数量超过实际仓位。

### 7.2 止损

- 原生 `STOP_MARKET` 为默认；
- trigger basis：mark / last；
- 支持更新、撤换和恢复；
- 同一 position 每个 lifecycle role 只能有一个有效 stop loss。

### 7.3 分批止盈

每级包含：

- trigger/limit price 或 R 倍数；
- quantity 或 position fraction；
- TIF；
- 执行后是否移动剩余仓位止损。

所有级别数量总和必须 `<= 100%`。

### 7.4 保本与移动止损

- `breakeven_enabled`
- `breakeven_trigger_r`
- `breakeven_offset_bps`
- `move_stop_loss`
- `move_stop_to_entry`

必须生成 stop order 计划，不得错误翻译为普通 market 单。

### 7.5 追踪止损

- 交易所原生 trailing stop 优先；
- 应用侧 trailing 仅在行情 freshness 与节点健康满足时启用；
- 配置 activation R、callback rate、minimum step 和 update rate limit。

### 7.6 部分/全部平仓

- partial close 需显式 fraction/quantity；
- full close 使用实际交易所仓位数量；
- 所有退出均 reduce-only；
- 平仓完成后撤销残留保护单并进行最终对账。

---

## 8. 价格监控

### 8.1 数据源

- Binance mark price；
- last trade；
- best bid/ask 或 book ticker；
- user data/account update；
- Nautilus cache 仅作为节点内实时视图，control-plane 使用事件投影。

### 8.2 Freshness

分别维护：

- `market_data_last_seen_at`
- `account_data_last_seen_at`
- `execution_event_last_seen_at`
- `projection_applied_at`
- `reconciliation_verified_at`

不得用空 payload heartbeat 更新这些字段。

### 8.3 Fail-safe

满足任一条件时禁止新增风险：

- market data stale；
- account state stale；
- projection lag 超阈值；
- reconciliation failed；
- node heartbeat stale；
- price deviation 超阈值。

减仓和撤单仍可执行。

### 8.4 保护 watchdog

持续比较：

- 实际持仓数量；
- 预期 stop/TP；
- 交易所工作订单；
- 本地保护投影。

差异触发自动修复、REDUCING 或 HALT，策略由设置决定。

---

## 9. 资金管理

### 9.1 仓位计算

默认 `fixed_risk`：

```text
risk_amount = min(
  equity × risk_per_trade_pct,
  remaining_daily_risk_budget,
  remaining_total_risk_budget
)

stop_distance_pct = abs(entry - stop) / entry
raw_notional = risk_amount / stop_distance_pct
allowed_notional = min(
  raw_notional,
  max_notional_per_order,
  remaining_instrument_exposure,
  remaining_correlated_exposure,
  available_margin_adjusted_notional
)
quantity = floor_to_increment(allowed_notional / entry)
```

如果没有可验证止损距离，新增风险必须 `needs_review` 或拒绝。

### 9.2 限额

- risk per trade；
- max total open risk；
- max notional per order；
- max instrument exposure；
- max correlated exposure；
- max leverage；
- max open positions；
- minimum free margin；
- reserve balance；
- daily loss limit；
- max drawdown；
- loss cooldown；
- per-account 和 per-instrument override。

### 9.3 资金预留

在批准 intent 时建立 reservation：

- 防止并发意图重复占用同一风险预算；
- reservation 有 TTL；
- 下单失败、拒绝、过期、取消后释放；
- 成交后转为 live exposure；
- 重启后由对账修复。

---

## 10. 完整设置面板

路由：`/settings/orders`

### 10.1 页面框架

- 左侧设置分类；
- 顶部 scope selector：全局 / 账户 / 币种；
- 当前生效版本、运行环境、节点状态和数据 freshness；
- “继承值 / 覆盖值 / 最终生效值”三态展示；
- 未保存变更提示；
- 服务器端验证；
- 保存前 diff、影响分析、reason 和确认；
- 版本历史与一键回滚；
- hot-reload / restart-required 标签；
- live 环境放宽风险时强制高危确认。

### 10.2 Tab：General

- order manager enable；
- execution mode：shadow/testnet/live；
- allowed instruments；
- default account；
- max concurrent execution jobs；
- intent TTL；
- idempotency retention；
- default command timeout。

### 10.3 Tab：Entry Orders

- default order type；
- TIF；
- post-only；
- max slippage；
- limit offset；
- unfilled timeout；
- reprice interval；
- max reprices；
- submit retries/backoff；
- partial fill policy；
- minimum fill ratio；
- market conversion guard。

### 10.4 Tab：Protection & Exits

- require stop loss；
- stop order type；
- trigger basis；
- protection install timeout；
- take-profit ladder editor；
- breakeven；
- trailing stop；
- partial close defaults；
- replace/cancel protection behavior；
- protection repair policy。

### 10.5 Tab：Money & Risk

- sizing mode；
- fixed notional；
- equity percentage；
- risk per trade；
- max order notional；
- leverage；
- max positions；
- instrument/correlated/total exposure；
- minimum free margin；
- reserve balance；
- daily loss limit；
- drawdown；
- cooldown；
- risk reservation TTL。

### 10.6 Tab：Price Monitor

- mark/last/book sources；
- stale thresholds；
- evaluation interval；
- allowed price deviation；
- disconnect action；
- account refresh interval；
- protection watchdog interval；
- recovery grace period。

### 10.7 Tab：Reconciliation & Recovery

- startup reconciliation required；
- periodic interval；
- orphan order policy；
- external position policy；
- drift policy；
- auto-adopt restrictions；
- projection lag threshold；
- retry/backoff；
- restart recovery mode。

### 10.8 Tab：Emergency Commands

- cancel batch size；
- close batch size；
- inter-order delay；
- max retries；
- verify timeout；
- close-all final state；
- allow close while HALTED；
- command retention。

### 10.9 Tab：Notifications

- order accepted/rejected/filled；
- partial fill；
- protection missing；
- stale market/account；
- reconciliation drift；
- daily loss/drawdown；
- cancel_all/close_all result；
- channels and severity routing。

### 10.10 Tab：Advanced

- outbox batch size；
- spool size/age；
- reducer replay window；
- event retention；
- rate limits；
- diagnostics bundle；
- import/export（不含 secrets）。

### 10.11 UI 安全与可用性

- `viewer` 只读；
- `operator` 可执行低风险操作；
- `risk_admin` 可修改风险设置；
- live 放宽限制需 `operator_signoff`；
- 所有 PATCH 带 `expected_version` 防止覆盖；
- 表单支持键盘、错误摘要、ARIA、移动端；
- 数值同时显示单位、范围和默认值；
- secret 字段永不显示或从 API 返回。

---

## 11. 设置合并与发布语义

### 11.1 层级

```text
system defaults
  < global persisted settings
  < account override
  < instrument override
```

每个字段返回：

- `value`
- `source_scope`
- `inherited`
- `validation`
- `apply_mode`

### 11.2 发布流程

1. 客户端获取当前版本；
2. 本地编辑；
3. `POST /validate`；
4. 展示 effective diff 与影响；
5. 提交 reason + expected_version；
6. 服务端事务写 settings_version + audit_event；
7. 发布 `settings.changed` outbox；
8. 节点 ACK 应用版本；
9. Dashboard 显示 desired/effective version；
10. 应用失败自动保留旧版本并告警。

---

## 12. API 草案

### 12.1 Settings

- `GET /v1/order-management/settings`
- `GET /v1/order-management/settings/effective`
- `POST /v1/order-management/settings/validate`
- `PATCH /v1/order-management/settings`
- `GET /v1/order-management/settings/versions`
- `GET /v1/order-management/settings/versions/{version}`
- `POST /v1/order-management/settings/rollback`
- `GET /v1/order-management/settings/runtime-status`
- `POST /v1/order-management/settings/export`
- `POST /v1/order-management/settings/import/validate`

### 12.2 Orders

- `GET /v1/orders`
- `GET /v1/orders/{order_id}`
- `GET /v1/orders/{order_id}/timeline`
- `POST /v1/orders/{order_id}/cancel`
- `POST /v1/orders/{order_id}/replace`
- `POST /v1/positions/{position_id}/partial-close`
- `POST /v1/positions/{position_id}/close`
- `POST /v1/positions/{position_id}/move-stop`
- `POST /v1/commands/cancel-all`
- `POST /v1/commands/close-all`

### 12.3 Monitoring

- `GET /v1/order-management/health`
- `GET /v1/order-management/price-status`
- `GET /v1/order-management/protection-status`
- `GET /v1/order-management/reconciliation`
- `POST /v1/order-management/reconciliation/run`
- `GET /v1/order-management/risk-explain`

所有写请求必须携带：

- `request_id`
- `reason`
- `expected_version`（设置变更）
- `confirm`（危险操作）
- idempotency key

---

## 13. 数据模型

新增或完善：

- `order_management_settings`
- `order_management_setting_versions`
- `order_management_setting_overrides`
- `execution_jobs`
- `orders_projection`
- `order_events`
- `order_links`
- `protective_orders_projection`
- `positions_projection`（规范化 position identity）
- `account_projection`
- `risk_reservations`
- `price_feed_status`
- `reconciliation_runs`
- `reconciliation_findings`
- `node_command_runs`
- `audit_events`

关键约束：

- `(account_id, idempotency_key)` 唯一；
- event_id 唯一；
- 同 position + lifecycle role 的 active protection 唯一；
- settings scope + version 唯一；
- 所有金额与数量使用 NUMERIC/Decimal；
- 时间统一 UTC。

---

## 14. 可观测性与告警

指标：

- intent 到 submit/accept/fill 延迟；
- order reject/deny/retry 比率；
- partial fill 数量与持续时间；
- protection install latency；
- positions without protection；
- price/account freshness；
- projection lag；
- reconciliation drift；
- cancel_all/close_all 完成时间；
- risk reservation usage；
- settings desired/effective version mismatch。

告警等级：

- Critical：无保护仓位、close_all 未完成、交易所/投影严重漂移、live 数据 stale；
- High：订单丢失、账户状态 stale、日亏损/回撤闸；
- Medium：重挂耗尽、部分成交超时、节点设置版本落后；
- Info：普通成交、设置发布、对账成功。

---

## 15. 测试与验收

### 15.1 单元与契约

- 状态机所有合法/非法转换；
- Decimal/precision/min-notional；
- settings 合并与验证；
- position sizing；
- idempotency；
- reducer replay；
- RBAC 与审计。

### 15.2 模拟交易所

- market/limit/zone；
- 部分成交；
- 拒单；
- 撤单竞态；
- 网络超时但交易所已接受；
- stop/TP；
- move stop；
- restart/recovery；
- duplicate events；
- out-of-order events。

### 15.3 Binance testnet 固定矩阵

1. market open → SL + TP；
2. limit timeout → cancel；
3. reprice 后成交；
4. partial fill keep remainder；
5. partial fill cancel remainder；
6. stop loss 成交；
7. 分批 TP；
8. move stop；
9. breakeven；
10. trailing stop；
11. partial close；
12. full close；
13. cancel_all；
14. close_all 多仓位批处理；
15. node restart；
16. control-plane restart；
17. WS reconnect；
18. reconciliation drift；
19. stale market/account fail-closed；
20. duplicate intent/event 无重复订单；
21. 两账户无串单；
22. 设置发布、节点 ACK 与 rollback。

### 15.4 Dashboard E2E

- 所有设置 tab；
- scope 与继承；
- 校验错误；
- diff/impact；
- 权限；
- 保存冲突；
- 版本历史/回滚；
- dangerous confirm；
- desired/effective mismatch；
- order timeline 与人工操作。

---

## 16. 里程碑

| 里程碑 | 内容 | 优先级 |
|---|---|---|
| OM0 | 契约、状态机、规范化 ID 与 DB 基础 | P0 |
| OM1 | 设置服务、版本、覆盖、审计和运行时应用 | P0 |
| OM2 | 投影、行情 freshness、保护 watchdog 和对账 | P0 |
| OM3 | 动态资金管理与风险预留 | P0 |
| OM4 | 入场订单完整生命周期 | P0 |
| OM5 | 保护单、止盈止损与退出管理 | P0 |
| OM6 | 紧急命令、恢复与最终核验 | P0 |
| OM7 | 完整设置面板与订单中心 | P1 |
| OM8 | 可观测性、安全、全量验收和放量 | P0/P1 |

具体任务、依赖、owned paths 与验收标准见 `taskList.json`。

---

## 17. 发布阶段

### Phase 1：Shadow

- 只生成计划，不提交订单；
- 对比旧执行路径；
- 设置面板可读、不可 live 写。

### Phase 2：Testnet

- 全部固定矩阵通过；
- chaos 与 restart 通过；
- 所有 settings 版本可回滚；
- `release-gate` 仍不超过 `testnet_only`。

### Phase 3：Live Readonly

- 读取主网订单/持仓/账户；
- 不提交新风险；
- 对账与 freshness 连续稳定。

### Phase 4：Live Small

- operator signoff；
- 小金额、低杠杆、限币种；
- 初始 HALTED，人工 ACTIVE；
- 可在一个命令周期内 HALT；
- close_all 必须最终核验为 flat。

---

## 18. 完成定义

订单管理只有同时满足以下条件才可称为完成：

- 全部 P0 任务 done；
- testnet 22 项矩阵全过；
- 无保护仓位指标为 0；
- duplicate order 为 0；
- 两账户串单为 0；
- 设置 desired/effective 版本一致；
- cancel_all/close_all 有终态 ACK 和交易所核验；
- stale 数据时新增风险为 0；
- restart 后无重复订单，状态可恢复；
- Dashboard 不使用 fixture/fake fallback；
- release gate evidence 填写完整；
- live 仍需 operator signoff。

---

## 19. 建议 owned paths

```text
packages/contracts/v1/**
packages/execution-domain/**
services/control-plane/order_management/**
services/control-plane/settings/**
services/control-plane/risk/**
services/control-plane/db/migrations/**
services/nautilus-node/strategy/**
services/nautilus-node/commands/**
services/nautilus-node/projection/**
services/nautilus-node/runtime/**
bridge/apps/dashboard/src/pages/settings/**
bridge/apps/dashboard/src/components/settings/**
bridge/apps/dashboard/src/pages/orders/**
bridge/apps/dashboard/src/utils/api.ts
tests/order_management/**
tests/nautilus/**
docs/order-management/**
docs/runbooks/**
release-gate.json
```

---

## 20. 开始顺序

第一波并行：

- OM0 契约/状态机；
- OM1 设置 schema/数据库；
- OM2 投影规范化与 freshness 契约；
- OM7 设置面板信息架构与 mock contract（只做 UI contract，不接假生产数据）。

第二波：

- OM3 资金管理；
- OM4 入场生命周期；
- OM5 保护/退出；
- OM6 命令编排。

第三波：

- 接真实 API；
- 模拟交易所；
- testnet；
- chaos；
- release gate。
