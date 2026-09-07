# backend-api 接缝契约（v1.1，已冻结 frozen）

> 状态：**frozen（2026-08-30，G0 裁决）**——已吸收 codex 落地前 review 的 19 条阻断项（各条标注 review #N）。
> G2 以本文件 schema 造 fixtures 开工；G1 实现必须逐字段一致，偏差走 block 仲裁。
> 完整语义见设计文档 v1.1 对应小节；本文件只写死接缝形状。

## 通用

- 新读端点复用既有 `_envelope()` 信封；mirror 端点的信封 `data_source` 标 `exchange_state_mirror`，信封级 `stale` = 任一账户 stale（review 缺口 #8）。
- 鉴权：**复用既有 operator token 体系（D12），不新增角色**。
- 既有端点字段只增不改不删。

## 1. 既有端点补字段（additive，M1a）

- `GET /v1/positions`、`GET /v1/orders`、`GET /v1/trades` 每行新增 `account_id`（account-a..d）
- `GET /v1/positions` 每行新增 `protection` 对象（结构同 §2）
- `GET /v1/nodes` 每节点新增 `release_id`（64 位完整值）、`halt_reason`、`trading_state`

## 2. GET /v1/mirror/positions（M1b）

```jsonc
// data:
{
  "accounts": [{
    "account_id": "account-a",
    "mirror_age_seconds": 12.4,
    "stale": false,                     // >300s → true
    "positions": [{
      "symbol": "ETHUSDT",
      "position_side": "LONG" | "SHORT" | "BOTH",
      "quantity": "1.25",               // string 十进制，下同
      "entry_price": "...", "mark_price": "...", "unrealized_pnl": "...", "leverage": "...",
      "quantity_step": "0.001",         // 交易所过滤器，供前端百分比→数量预取整（review #12）
      "min_quantity": "0.001",
      "protection": {
        "status": "protected" | "partial" | "unprotected",
        "stop_loss":    [{ "order_id": "...", "trigger_price": "...", "quantity": "...", "is_bot_order": true }],
        "take_profits": [{ "order_id": "...", "trigger_price": "...", "quantity": "...", "is_bot_order": true }]
      }
    }],
    "open_orders_count": 3,
    "algo_orders_count": 2
  }]
}
```

约束：保护单集合 = mirror `open_orders` **∪ `algo_orders`**（币安 SL/TP 条件单在 algo 侧，review #17）；归属按 exit side 拆分（hedge LONG→SELL、SHORT→BUY）；`is_bot_order` 按 `^B[0-9a-f]{32}[0-9]{2}$`；**无 `protection_policy` 字段**（review #18，policy 无稳定 position 归属规则，推迟）。

## 3. GET /v1/reconcile（M1b）

```jsonc
// data:
{
  "ghost_orders":   [{ "account_id": "...", "symbol": "...", "client_order_id": "...", "projection_status": "accepted", "detected_at": "..." }],
  "missing_orders": [{ "account_id": "...", "symbol": "...", "exchange_order_id": "...", "detected_at": "..." }],
  "skipped_accounts": ["account-b"],   // mirror stale 的账户 ghost/missing 都不计算（review #17）
  "last_run": { "run_id": "...", "completed_at": "...", "findings_open": 0 } | null
}
```

交易所侧集合含 `algo_orders`（否则条件保护单全误报幽灵）。

## 4. GET /v1/outcomes?days=7&account_id=…（M1c）

行字段同 v1（symbol/direction/account_id/realized_pnl/r_multiple/holding_seconds/fees/entry_avg/exit_avg/closed_at）。

**KPI 公式冻结**（review 缺口 #6）：
- `win_rate` = 盈利笔数/总笔数，0-1 小数；空窗口 → null
- `profit_factor` = 总盈利/|总亏损|；零亏损 → null（前端显示 ∞）；零盈利 → 0；空窗口 → null
- `avg_r` / `avg_holding_seconds`：分母 = 对应字段非 NULL 行数
- `r_distribution` 6 桶沿用 `report_service.py bucket_r_values` 现状边界，单测锁 -2/0/1/2 边界归属
- `watermark`: `{ materialized_at, stale }`（>36h → stale）

## 5. 写路径契约（既有端点，M0/M3 消费——按代码事实冻结）

**(a) `POST /v1/commands`**（review #1/#2/#3）
- 顶层必填：`type` / `reason` / `confirm: true` / `request_id`（面板前缀 `dashboard-`，app 前缀 `mobile-`）
- 白名单：`HALT / REDUCE / RESUME / CANCEL_ALL / CLOSE_ALL / REFRESH_EVIDENCE`
- account-scoped 四种 = `HALT / REDUCE / RESUME / REFRESH_EVIDENCE`：必带 `scope.account_id` + `target_nodes`，一账户一节点一命令
- RESUME 另需 `scope.symbol`（锚点）+ `scope.release_id`（取自 /v1/nodes 新字段）
- 完成判定：轮询 `GET /v1/commands/{id}` 至终态（pending/partial/failed 语义 + 30s 超时；REFRESH_EVIDENCE 成功后等证据新鲜窗）

**(b) `POST /v1/operator/orders`**（review #5/#6/#11/#12/#13）
- 通用字段：`action / symbol / account_id / reason / client_ref`；管理动作目标解析：`parent_intent_id` 或 `entry_ref` 或 `position_side`（hedge 必带）
- 字段名：cancel → 机器人格式 `client_order_id`；move SL → `stop_loss`；partial_close → **绝对 `quantity`**（百分比换算在客户端，用 §2 的 quantity_step/min_quantity 预取整）
- 二段式确认：`dry_run: true` 预检（不落 intent）→ 用户确认 → 正式提交；重试复用同 `client_ref`
- 响应：`intent_id / status / order_plan / warnings / attribution`，幂等重放时附 **`replay: true`**（字段名以 read_api 实现为准，2026-08-31 G0 核实补录；客户端识别重放应以此字段+本地持久 intent_id 双轨判定）；（**没有** command_id/acks/execution job id）；终态经 operator status 轮询，终态集合以 `packages/contracts/v1/order_state.v1.json` 的 intent 状态机为唯一真源（含 denied/rejected/expired/partial 等，禁止客户端自行发明归类）
- 审计落点：`trade_intents` + `audit_events`（不是 operator_commands）

## 6. M1e 溯源与节点读模型（新增）

- 溯源端点（扩展 `GET /v1/operator/orders/{intent_id}` 或新 `/v1/intents/{id}/trace`）：`data` 六层 = `raw_message`（含 media 引用）/ `hermes_decision` / `risk_decision` / `intent` / `execution_events[]` / `node_acks[]`，任一层缺失置 null（不报错）
- `GET /v1/incidents?status=open`：`production_incidents` 只读列表（id/node/summary/opened_at）；resolve 不在本契约（需节点身份）
- `GET /v1/commands/{id}`：确认响应含终态字段（若已存在则此条为契约核对）

## 7. G2 消费约定

- fixtures 覆盖形态：hedge 双向、SL 在 algo_orders、stale 账户、空窗口 KPI、dry_run 响应含 would_reject。
- 数据龄徽章阈值（展示层）：绿 <60s，黄 60–299s，红 ≥300s 或 stale。战报发布闸门的 mirror 阈值维持 180s，两者语义不同（review #16）。
- Reports 页按 bridge 真实形状解析：markdown=JSON 对象、versions=裸数组、daily.open_positions=汇总对象（review #9）；数据全零标注"数据源未接线，待 M2g"（review #10）。
- 面板双上游（review 缺口 #9）：`/v1/*` → 控制面、`/api/*` → bridge；本地开发与 jp-24 部署都需保证这两条分流，联调验收含真实路由测试。

## 附录: 测试入口（B-01 2026-08-30 实跑固化）

- 控制面：`.venv-arch/bin/python -m pytest tests/control-plane -q`
  - 2026-08-30 两次一致：collected=450 passed=450 failed=0 error=0 skip=0（另 1 warning、1 subtests passed）
- 战报：`.venv-arch/bin/python -m pytest services/report/tests -q`
  - 2026-08-30 两次一致（G1 workspace cwd）：collected=39 passed=39 failed=0 error=0 skip=0
  - 隔离 worktree `auto/task-B-01`：collected=35 passed=35 failed=0 error=0 skip=0
  - G0 已在 `.venv-arch` 安装 `python-multipart`；`create_app()` 无条件注册 `POST /reports/assets`。collect 0 error。锁点：`tests/control-plane/test_b01_pytest_entry_collect.py`（实跑 `.venv-arch` `--collect-only`）。
- 契约登记（review 缺口 #7）：新 schema 须同步 `tests/contracts/test_contracts_v1.py` 的 SCHEMAS、version manifest、snapshot、examples 索引
- 面板：`npm --prefix bridge/apps/dashboard test` / `run test:e2e`（vitest 只收集 `*.test.tsx` 命名；axe 依赖待加）
- app（alert-personal 根）：`npm run test:android` / `typecheck:android` / `lint:android` / `build:android`

## 8. 账户权益历史（additive，2026-09-05，G0 裁决）

- 采样：`exchange_state_recorder` 每次成功快照后，按 **30 分钟桶**（`bucket_at = date_trunc 到 30min`）向非记账表 `account_equity_samples(account_id, bucket_at timestamptz, equity numeric, available numeric, margin numeric, sampled_at timestamptz, PRIMARY KEY(account_id, bucket_at))` upsert，**桶内保留最新一次读数**。无历史回填来源，序列自部署时刻起累积。
- 读取：**不新增端点**。`GET /v1/accounts?history_hours=<1..168>`（缺省不传则响应与现状完全一致）时，信封 `data` 新增：
  - `equity_history_total: [{ "t": ISO8601 UTC, "equity": "string-decimal", "available": "string-decimal", "accounts_sampled": int }, …]`：按 30 分钟桶对**全部账户求和**，按 `t` 升序，最多 336 点。`accounts_sampled` 为该桶内有样本的账户数；消费端**仅在 `accounts_sampled == accounts_expected` 时画点**，否则留空（避免缺账户造成的假跌）。
  - `equity_history_meta: { "bucket_seconds": 1800, "accounts_expected": 4, "since": ISO8601, "first_sample_at": ISO8601 | null }`。
  - 不返回逐账户序列（用户裁决：只要总额）。
- 移动端走既有 `/m/v1/accounts` 路径（query 不影响 Caddy 路径匹配），面板走 `/v1/accounts`。
- 记账红线：新表不属于记账表；除 `account_equity_samples` 外零写入新增。
