# A/B Handoff 审查 + 集成裁决账本（C 视角，随 B 进度更新）

> A 已完成冻结；B 仍在 foundation 阶段。本账本记录 C 在合并时要核验/裁决的项。
> 原则：A/B 的自报状态**不直接翻 gate**；`release-gate.json` 只在 **C 亲自复核** 后翻（PLAN「不可伪造完成」）。

## 0. Worktree 拓扑（已确认）

```
/Users/pudu/projects/trader-bot                    [work/hermes-data-v3]   ← A，已停手、clean
/Users/pudu/projects/trader-bot-nautilus-dashboard [work/nautilus-dashboard-v3] ← B，进行中
```
二者是**同仓多 worktree**（共享 .git/refs），故 B 的提交在本仓 ref 可见。
**C 合并方式**：另开独立 worktree，避免动 A/B：
```
git worktree add /Users/pudu/projects/trader-bot-integration -b work/integration-acceptance-v3 plan/nautilus-hermes-v3
```

## 1. 窗口 A：已交付（待 C 复核，不预先翻 gate）

- 314 测试通过（92 python @真实 pg16 + 222 bridge）；no-semantic-regex 147 文件/0 违规。
- contracts-v1：`packages/contracts/v1/*.json`（HermesDecisionV1 / ApprovedTradeIntentV1 / ExecutionEventEnvelopeV1 / SystemSnapshotV1）+ `version.json` + `.snapshot.json` 兼容检测。
- 迁移：`db/migrations/000{1,2,3}_*.{up,down}.sql`，`services/control-plane/db/migrate.py up|down`（读 `DATABASE_URL`）。
- API：`docs/handoff/window-a/openapi.json` —— **仅明确 `GET /api/system/snapshot`**。
- A 自报放行条件：G1✅ G2✅ G3✅ G4✅ G5✅ G10✅；**G6⚠️PARTIAL**（网关单账户路由）；G7⚠️partial（ingress/worker/gateway 幂等已证，WS/对账重放属 B/C）；**G8🚧BLOCKED**；G9 非 A 范围。

### A 残余/阻塞（必达 C）
- 🚧 **G8 真实多模态 smoke**：`services/hermes-worker/smoke_replay.py` 需 `HERMES_API_URL/KEY/MODEL` + ≥10 真实图文，未配置即报 BLOCKED（未伪造）。→ C-05 用 HK Hermes + 我手上的 80 图文 corpus 跑通。
- **SystemSnapshotV1.account 是冻结空对象占位**；账户身份不能搭 snapshot，需 contract-change。
- **多账户路由**（G6）网关里很薄。
- **media bytes** 用本地目录抽象，生产需接 S3/MinIO 对象存储。
- compose 层 live 关闭等硬化记在 `baseline-inventory.md`（A 没碰 docker-compose）。

## 2. 窗口 B：进行中（foundation），运行时强依赖

- B-FND in_progress；B-00..B-11 todo（节点 lifecycle / intent client / strategy / risk / projection / 两账户 / dashboard / 容器 / 回归）。
- 运行时门：Nautilus 1.227.0 需 **Linux + Docker + Python 3.12**；pudu-mini 无 Docker、Py3.14。Binance testnet keys 未给前，B-00 连接 smoke 与 B-11 testnet 证据 = needs_review/blocked（不伪造）。
- Dashboard(B-09) 本地 Node24 可建可测，是 B 第一个可验证交付。

## 3. A⇄B 契约裁决项（C 在「锁定 contracts-v1」时定）

| ID | 分歧 | A 现状 | B 假设 | C 裁决方向 |
|---|---|---|---|---|
| SEAM-path | API 前缀 | `/api/system/snapshot` | `/v1/...` | 锁统一前缀；C 在 control-plane 补齐 B 消费的 HTTP 面 |
| SEAM-intent | intent 投递 | outbox `trade_intent.approved`（A 未明确 HTTP 拉取端点） | pull `GET /v1/nodes/{id}/intents?after=` + `POST .../ack`（status∈received/accepted/rejected/executed/expired/duplicate/failed） | 锁 pull 游标端点；C 在 control-plane 实现（owns 全路径），保留 SSE 适配位 |
| B09-CCR-001 | 命令下发端点 | `services/control-plane/commands`（按节点 ack） | `POST /v1/commands {type,args}`→`{command_id,target_nodes,acks,status}` | 锁端点 + ack 词汇 + timeout 表示 |
| B09-CCR-002 | 单仓手动动作 | halt/resume/set_reducing/cancel_all/close_all | 单仓 close/partial 用 close_all+scope；移损用 move_stop_loss | 定 canonical 命令类型/args，**等待全节点 ack** |
| B09-CCR-003 | 日报读端点 | SEAM §4 未锁 | `GET /v1/reports/daily/{date}` 等 5 个 | 锁端点或从 v1 dashboard 移除报表视图 |
| account-id | 账户身份 | SnapshotV1.account 空占位 | 两账户隔离需账户身份 | contract-change：定账户身份载体（不混入 snapshot） |

→ 多为 **C 在 control-plane 侧补/对齐 HTTP seam**（C owns 全路径，实现交 Codex、C 验收）。

## 4. C 需 operator 提供的运行时凭据/主机（有前置周期，建议早备）

1. **真实 Hermes**：`HERMES_API_URL/HERMES_API_KEY/HERMES_MODEL`（HK `hermes-gateway-trader`，`/srv/hermes/hermes-agent`）→ 解 G8/C-05。
2. **Linux+Docker+Py3.12 主机** 跑 Nautilus（与 hk 生产栈隔离端口/项目，或专用 VPS）→ C-04/C-08。
3. **Binance USDT-M Futures testnet API keys** → C-08（14 场景）。
4. **生产对象存储 S3/MinIO** → media bytes。

（凭据按惯例上 balen 的 Mac mini 取，注入 0600 .env。）

## 5. 不变的 corpus 决策（C-03）
- 缺 HYPE 未下单事故；80 条难凑四类各 ≥20 → 建议扩 500 全集。详见 `docs/replay/CORPUS_ASSESSMENT.md`。
