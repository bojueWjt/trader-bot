# C-00 合并报告

> 分支 `work/integration-acceptance-v3`（worktree `/Users/pudu/projects/trader-bot-integration`，base `plan/nautilus-hermes-v3`=237cabc）。
> 合并提交 `2ce3625`。**git 冲突 0 个**；所有 A/B 不一致是**语义层**，列在下方由 C 裁决/修复。**release gate 保持 `blocked`。**

## 1. 合并机制

- A（`work/hermes-data-v3`，+20）→ fast-forward。
- B（`work/nautilus-dashboard-v3`，+14）→ 三方合并，**0 冲突**。
- 0 冲突原因：A/B 严格遵守 path ownership；**B 没改 A 的契约，而是把 contracts-v1 复制成 fixtures**（`tests/nautilus/_fixtures/contracts-v1/*.schema.json`）。git 不冲突 ≠ 语义一致（见 §3）。
- 工作树合并前均 clean、已停手；A/B 各自 worktree 未受影响。

## 2. 合并后构成（均在位）

数据面（A）：`services/{ingress,hermes-worker,control-plane,telegram-watcher}`、`packages/contracts`、`db/migrations`、`bridge/apps/api`。
执行面（B）：`services/nautilus-node`、`packages/nautilus-adapter`、`packages/execution-domain`、`infra/compose`、Dashboard 在 **`bridge/apps/dashboard`**（不是 `apps/dashboard`）。
测试面：A python 22 + B nautilus 15 + dashboard 7（运行需真实 pg / Linux+Docker+Py3.12，见 §6）。

## 3. 契约锁定前必须裁决的 A/B 语义分歧（C「锁定 contracts-v1」）

| # | 分歧 | A（冻结） | B（fixture/实现） | C 裁决 |
|---|---|---|---|---|
| 1 | **`ApprovedTradeIntentV1.order_plan`** | `{additionalProperties:false, properties:{}}` = **闭合空对象，装不下任何下单细节** | 开放对象（按 action 由 strategy 校验） | **P0：改 A 契约**为开放对象（或按 action 规范化）。否则 B 收不到订单计划，执行链根本跑不通 |
| 2 | `idempotency_key` pattern | `^[0-9a-f]{64}$` + min/maxLength 64（严格小写） | `^[A-Fa-f0-9]{64}$`（允许大写、无长度） | 锁 A 的严格小写；B 对齐（B 现在更宽松，会放过 A 会拒的 key）|
| 3 | `intent_id`/`instrument_id` minLength | 无 | `minLength:1` | 采纳 B 的收紧，写回 A 契约 |
| 4 | `$id` 命名 | URL | `contracts-v1/XxxV1` | 统一为 A 的；cosmetic |

## 4. 服务端 seam 分歧（详见 docs/acceptance/HANDOFF_REVIEW.md）

- API 前缀：A `/api/...` vs B 假设 `/v1/...`。
- B 需要、A **未暴露**的端点：`POST /v1/nodes/{id}/intents`(+ack)、`POST /v1/commands`、`GET /v1/reports/daily/...`、节点 execution-event/heartbeat 摄入端点（`ProjectionWriter` 是 stub）。→ **C 在 control-plane 补齐**（owns 全路径）。
- served `/api/system/snapshot` 缺 §2.2 data-quality envelope（合规 builder 未接）。

## 5. 继承的缺陷（gate 保持 blocked 直到关闭）

- **窗口 A 11 个 P0**（`docs/acceptance/WINDOW_A_REVIEW.md`）：风控 fail-OPEN、服务端跑 legacy 不安全栈、test-token 兜底含 risk_admin、ingress 零鉴权、量化风控闸门是死的、决策/快照新鲜度不入闸、无代码层挡非法动作、hermes_decisions 无 DB 幂等、served 快照缺 envelope。**未修**（A 仅加了一个 replay 提交）。
- **窗口 B 残留**：Binance testnet keys 未提供 → B-00 testnet 下单冒烟 + testnet 验收 = needs_review；11 个 running-node/真 Redis/testnet 集成测试 deferred 给 C；projection 事件订阅 + intent timer TODO 待真节点。

## 6. 运行时依赖（operator 提供，C-01+ 阻塞项）

1. 真实 Hermes `HERMES_API_URL/KEY/MODEL`（HK gateway）。
2. Linux+Docker+Py3.12 主机跑 Nautilus（与 hk 生产隔离）。
3. Binance USDT-M Futures **testnet keys**。
4. 生产对象存储 S3/MinIO（media bytes）。
5. 本地真实 PostgreSQL 16（跑 A 的 92 个 pg 测试）。

## 7. 下一步

C-00 收尾（本提交：落 C 验收脚手架 + 本报告）→ 修 order_plan/idempotency_key、锁 contracts-v1 → C-01 补 control-plane 缺失端点 + 修 A 的 P0 + 统一 wiring（全 HALTED/testnet）→ 跑合并测试面 → C-02..C-12。实现交 Codex，C 验收。
