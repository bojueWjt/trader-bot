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
- 读取：**不新增端点**。`GET /v1/accounts?history_hours=<1..8760>`（2026-09-17 扩至 365 天；缺省不传则响应与现状完全一致）时，信封 `data` 新增：
  - `equity_history_total: [{ "t": ISO8601 UTC, "equity": "string-decimal", "available": "string-decimal", "accounts_sampled": int }, …]`：先按原始 30 分钟桶对**全部账户求和**；7 天以内保留原始点，7–30 天每 6 小时取最后一个真实快照，30–365 天每天取最后一个真实快照。按真实 `t` 升序；7 天最多 336 点，一年最多 366 点（首尾可能跨 UTC 日）。不跨时间累加权益、不拼接不同时间的账户余额、不补造历史。`accounts_sampled` 为选中原始桶内有样本的账户数；消费端**仅在 `accounts_sampled == accounts_expected` 时画点**，否则留空（避免缺账户造成的假跌）。
  - `equity_history_meta: { "bucket_seconds": 1800 | 21600 | 86400, "accounts_expected": 4, "since": ISO8601, "first_sample_at": ISO8601 | null }`。`bucket_seconds` 表示本次查询的展示取样间隔；底层采样仍为 30 分钟。
  - 不返回逐账户序列（用户裁决：只要总额）。
- 移动端走既有 `/m/v1/accounts` 路径（query 不影响 Caddy 路径匹配），面板走 `/v1/accounts`。
- 记账红线：新表不属于记账表；除 `account_equity_samples` 外零写入新增。

## 9. watcher 网关与配置快照（2026-09-26 冻结）

> 状态：**frozen（WGW-1.0，2026-09-26）**。设计真相：`docs/plans/2026-09-11-watcher-to-app-migration.md` v0.6 §2.1–§2.3、§3、§4.2/§4.3（含附录 E 吸收的 v0.3 review P1-12..P1-19、P2-03）。
> **路由字段以 `contracts/watcher-gateway-routes.yaml` 为准**（method、outer/inner path、identity、roles、query、body allow/deny/required、response omit/mask/headers、phase、budget、write 元数据）；本节规定语义（鉴权顺序、错误码、事务顺序、状态机、媒体规则）。两者冲突时路由字段听 YAML、语义听本节；任一与计划冲突，停工并由 Planner 召回 Architect。
> 本节只新增 `/v1/watcher/*` 与 watcher 内部契约；§1–§8 既有端点的字段、状态码、错误体一律不变（见 §9.13）。

### 9.1 现状事实（worktree `auto/wac-001`，基线 `integ/watcher-app-crew`；app 基线 `0f7d26d`）

| 事实 | 证据 |
|---|---|
| 控制面 reader 环境变量表：`SYSTEM_OBSERVER_TOKEN / VIEWER_TOKEN / RISK_ADMIN_TOKEN / REVIEWER_TOKEN` → 四角色 | `services/control-plane/api/read_api.py:104-109`（`READER_TOKEN_ENV`）；实际解析用同内容的 `security/principal.py:28-33`（`READER_ROLE_BY_ENV`） |
| `require_reader` = `resolve_principal` + 拒绝 signal 身份，只返回四角色之一 | `read_api.py:348-352`；`read_api.py:337-345`（异常映射）；会话 token 角色也限四角色 `security/session_auth.py:13`、`:64-71` |
| 缺 `Bearer` → 401；未知 token → 403 `forbidden`；token 目录重复/未配置 → 503；signal token → 403 `account-scoped reader required`；会话 token 验签失败 → 401 | `principal.py:111-115`、`:148`、`:95-97`/`:110`/`:146-147`、`read_api.py:350-351`、`principal.py:132-136` |
| token 规范化 = 去掉 `"Bearer "` 前缀后 `strip()` | `principal.py:113` |
| 锁定既有 401/403 的测试 | `tests/control-plane/api/test_snapshot_and_api.py:210-211` |
| 写权限判定 `kind=operator ∧ role=risk_admin ∧ scope=global` | `principal.py:190-196`；`read_api.py:355-368` |
| operator-query 进程由 route name 白名单拼装新 FastAPI 实例，只复制 `APIRoute`，异常处理器需重装 | `services/control-plane/api/app_roles.py:145-178`；`read_api.py:10585-10603` |
| 交易写 `operator_order` 是同步 `def` | `read_api.py:9080-9095` |
| 开仓读 watcher SQLite：路由/执行账号/父账号/启用/addon；品种风险→账号 `default_risk_ratio`→env 默认，范围 `(0, 0.1]` | `read_api.py:6311-6420`（`_load_channel_risk_route`）、`:6486`（addon）、`:8492-8519`（`_symbol_risk_ratio`）；DB 路径 `:6214-6244` |
| watcher 中间件注册顺序：`express.json` → `express.static(public)` → `/media` static（无鉴权）→ … → 交易 API | `bridge/services/telegram-watcher/server.js:68`、`:69`、`:70`、`:863` |
| watcher 现有站点路由 | `server.js:573`（`/healthz`）、`:583`（status）、`:594`（config）、`:607-794`（login/qr）、`:804`（groups）、`:813`（dialogs，limit 100）、`:835`（ring messages，默认 100）、`:842`/`:854`（disconnect/reconnect） |
| 交易路由 | `lib/trading-api.js:344`/`:382`/`:496`/`:654`（accounts GET/POST/PUT/DELETE）、`:709`/`:735`/`:772`（channels）、`:790`/`:803`/`:826`（risks）、`:844`/`:868`（orders）、`:885`（briefings）、`:903`（messages，`LIMIT 500`）、`:927`/`:931`/`:952`/`:972`/`:988`（price monitor/alerts） |
| 账号 GET 为 `SELECT account.*` 后掩码 `api_key/api_secret`；PUT 非空即换密钥；POST 必填 key/secret | `trading-api.js:348-374`、`:1002-1008`、`:581-590`、`:406-408` |
| 账号表列与约束 | `trading-api.js:55-74`（`account_type ∈ main/subaccount`、`is_enabled`、`default_risk_ratio`、`risk_capital_addon ≥ 0`）；执行账号唯一索引 `:199-200`；ID 正则 `:12-13` |
| 现有错误体 `{error: "..."}`，异常一律 500 | `trading-api.js:1205-1208`；站点读 `payload.error`：`public/index.html:910-929` |
| 连接只设 WAL/FK，无 `busy_timeout` | `trading-api.js:45-50` |
| 敏感键正则与掩码 | `lib/safe-log.js:1`、`:9-20`、`:76-95` |
| 媒体文件名 = `Date.now()-<message.id>.<ext>`，ext 取 MIME 子类型 | `server.js:249-251`；`lib/telegram-utils.js:31-41` |
| price monitor 另用 `TRADING_DB_PATH`（默认本机路径） | `price-monitor.js:13`、`:26-30` |
| 容器监听 `0.0.0.0:9100`，宿主映射 `127.0.0.1:9090`；健康检查无凭据调 `/healthz` | `bridge/docker-compose.yml:157`、`:178`、`:160-176`；`server.js:866-867` |
| app 请求 = `${baseUrl}${path}`，path 常量以 `/v1/` 起；头 `Accept`、`Authorization: Bearer`、写时 `X-Request-Id=client_ref`；12s 超时在拿到头后清除；非 JSON → `invalid_json`；仅 401 置 `writeDisabled`；其余非 2xx → `request_failed` | app `apps/attention-android/src/services/tradingApi.ts:370`、`:657-668`、`:759-768`、`:773`、`:793`、`:797-807`、`:810-817`、`:819-825` |
| app 存储键只有 `trading.config.v1`、`trading.write-ops.v1` | app `src/services/tradingStorage.ts:5-8`、`:28-31` |

### 9.2 拓扑与三种服务端身份

```
app ──baseUrl(/m)+/v1/watcher/*──▶ Caddy(strip /m 一次) ──▶ operator-query 网关(async) ──Bearer gateway──▶ watcher 127.0.0.1:9090
浏览器 ──basicauth──▶ Caddy(清除外来 X-Watcher-* → 注入 X-Watcher-Proxy-Auth) ──▶ watcher
operator-query 快照 reader ──Bearer snapshot──▶ watcher GET /api/trading/config-snapshot（内网，不经网关）
```

- **凭据与环境变量**（名字即契约，值由 O-0 发行侧生成，两两互异，app 与用户不接触）：

  | 身份 | watcher 侧（校验用，当前+上一值） | 持有方 | 出示方式 |
  |---|---|---|---|
  | `gateway` | `WATCHER_GATEWAY_TOKEN`、`WATCHER_GATEWAY_TOKEN_PREVIOUS` | operator-query：`WATCHER_GATEWAY_TOKEN` | `Authorization: Bearer <token>` |
  | `snapshot` | `WATCHER_SNAPSHOT_TOKEN`、`WATCHER_SNAPSHOT_TOKEN_PREVIOUS` | operator-query：`WATCHER_SNAPSHOT_TOKEN` | `Authorization: Bearer <token>` |
  | `browser` | `WATCHER_BROWSER_PROXY_TOKEN`、`WATCHER_BROWSER_PROXY_TOKEN_PREVIOUS` | Caddy | `X-Watcher-Proxy-Auth: <token>` |

- **watcher 启动**：三个当前值任一缺失、长度 < 32、或六个已配置值中任意两个相等 → 进程非零退出。watcher 环境里不放任何控制面 reader token。
- **operator-query 启动**：缺 `WATCHER_GATEWAY_TOKEN` → 网关路由照常注册，但一律 503 `gateway_disabled` 并打告警日志；**不得**让交易端点启动失败。快照开关（§9.11）打开且缺 `WATCHER_SNAPSHOT_TOKEN` → 按计划 §2.1 启动失败（上线前配置校验锁定，开关默认关）。
- **轮换**：`*_TOKEN_PREVIOUS` 双值。顺序：watcher 先接受新旧两值 → 持有方切新值 → 确认请求与审计正常 → 清空 `*_PREVIOUS`。不是时钟过期。
- **watcher 身份判定**（每个请求，在 `server.js:68` 的 `express.json` 与全部 static/handler 之前；比较用恒定时间）：
  1. 用 `req.rawHeaders` 计数（Node 会合并/丢弃重复头，不能用 `req.headers`）：`Authorization` 或 `X-Watcher-Proxy-Auth` 出现多于一条 → 401。
  2. `Authorization` 以 `Bearer ` 开头：规范化（去前缀 + trim）后命中 gateway 当前/上一值 → `gateway`；命中 snapshot → `snapshot`；否则 401。此时若同时带 `X-Watcher-Proxy-Auth` → 401（身份歧义）。
  3. 否则若带 `X-Watcher-Proxy-Auth` 且命中 browser 当前/上一值 → `browser`（`Authorization: Basic …` 是 Caddy basicauth 残留，忽略不参与判定；Caddy 宜在注入前删除它）。
  4. 其余 → 401 `unauthenticated`。
  5. 路径规范化：`gateway`、`snapshot` 身份用原始请求路径（`req.originalUrl` 的 path 部分，未解码）匹配，含 `%`、`//`、点段、尾斜杠或路径参数不合 `gateway_pattern` → 404 `route_not_found`（与网关同规则）；`browser` 身份沿用 Express 单次解码（站点用 `encodeURIComponent`），参数按 `browser_pattern` 校验。
  6. 身份确定后查该身份的生成路由表：路径与方法都在 → 放行；路径在本身份表内但方法不在 → 405（`Allow` 头列本身份的方法）；路径只属于其他身份 → 403 `identity_forbidden`；任何身份都没有的路径 → 404 `route_not_found`。鉴权先于 404，未认证请求探测不到路由存在性。
  7. actor 头检查见 §9.8；之后才进入 body 解析与 handler。
- `/healthz` 也受身份校验：它只有 `browser` 行（YAML `br.healthz.get`），容器健康检查（`docker-compose.yml:160-176`）需改为携带 `X-Watcher-Proxy-Auth: $WATCHER_BROWSER_PROXY_TOKEN`（取自容器自身环境）。它在 `never_allowed` 中，永不经网关（见 §9.15 待裁决 A-1）。

### 9.3 网关鉴权与角色 scope

- 网关路由鉴权沿用 `resolve_principal` 的判定（不修改 `require_reader` 及既有端点），但按下表映射为结构码（网关实现可直接调用 `resolve_principal` 并按异常类型映射）：

  | 情形（证据 §9.1） | HTTP | `code` |
  |---|---|---|
  | 缺 `Authorization` / 非 `Bearer ` / 空 token / 会话 token 验签失败（`AuthRequired`） | 401 | `unauthenticated` |
  | 未知 token（`PermissionDenied("forbidden")`）——**保持现有 403** | 403 | `invalid_token` |
  | signal token（账户级，非 reader） | 403 | `insufficient_scope` |
  | 读行：四角色任一 | — | 放行 |
  | 写行（POST/PUT/DELETE）：角色 ≠ `risk_admin` | 403 | `insufficient_scope` |
  | token 目录不可用（`TokenCatalogError`） | 503 | `auth_unavailable` |

- scope 表即 YAML 每行的 `roles`：读行恰为 `[system_observer, viewer, risk_admin, reviewer]`，写行恰为 `[risk_admin]`，且仅限 YAML 已列写行。不新增角色、不新增 token。
- **契约声明**：持有同一 `risk_admin` token（静态或会话）的所有调用方一并获得 watcher 写权限（包括停采集、改风控）。以后要区分自然人，用服务端 principal 映射，不给 app 新密钥。
- 同一个 `viewer` 写被 403 `insufficient_scope` 之后，读 status 仍须成功（无任何"禁写传染"）。

### 9.4 路径、方法与请求规范化（网关）

- **路径**：app 请求 `baseUrl + outer_path`，`baseUrl` 已以 `/m` 结尾，app 常量只写 `/v1/watcher/...`。Caddy 外部路径 = `/m` + outer_path，逐路径追加（`{param}` 段在 Caddy 中写 `*`），禁止 `/m/v1/*` 通配。
- **匹配基于 `scope["raw_path"]`**（未解码），规则：raw path 含 `%`、`//`、`.`/`..` 段、尾斜杠、大小写不符，或路径参数不匹配 YAML `path_params.*.gateway_pattern` → 404 `route_not_found`。**禁止尾斜杠 307 重定向**（operator-query 的角色 app 是新建的 FastAPI 实例，`redirect_slashes` 默认开启，C-1 必须显式处理并有测试）。
- **未注册路径 404、已注册路径未列方法 405**（`Allow` 头列 YAML 中该路径的方法），与 FastAPI 默认一致；这两类判定先于鉴权。`OPTIONS` 一律 405（无 CORS）。`HEAD` 只在 YAML 显式列出的行允许（仅媒体）；其余路径的 HEAD → 405。
- `/v1/watcher/` 前缀下的 404/405 响应体使用 §9.5 统一形状；该前缀以外的 404/405/422 响应体保持 FastAPI 现状。实现提示：异常处理器必须同时安装到 `create_app()` 生成的角色 app 上（`app_roles.py:145-178` 不复制处理器）。
- **路由名**：`watcher_gateway__<YAML id 中的 . 换成 _>`；必须出现在 operator-query 角色 app，不出现在 node-control / event-ingest。
- **阶段门**：生成时带 `phase_max`（P0..P3），只注册 `phase ≤ phase_max` 的行；未注册行按 404 处理。`phase_max` 写入生成物并参与 §9.14 diff。
- **query**：只接受该行 `query` 列出的键，定义见 YAML `query_params`；未列键、同键重复、值不合规、`before_created_at` 与 `before_id` 不成对 → 400 `invalid_query`。媒体行不接受任何 query（token 禁止入 query）。
- **body**：
  - GET/HEAD 行带 body → 400 `invalid_body`。写行必须 `Content-Type: application/json` 且为 JSON object，否则 400 `invalid_body`；大于 64 KiB → 413 `payload_too_large`。
  - 判定顺序：键命中 `secret_fields` 或 `secret_key_pattern` → 400 `secret_field_rejected`（`details.fields` 只列键名，不回显值）；键不在 `allow`（含 `deny` 中非秘密键）→ 400 `invalid_body`（`details.unknown_fields`）；缺 `required` → 400 `invalid_body`（`details.missing_fields`）；类型/格式不符 YAML `body_fields`（gateway 行用 `gateway_enum`/`gateway_pattern`；数字必须是 JSON number、布尔必须是 JSON boolean）→ 400 `invalid_body`（`details.field`、`details.rule`）。
  - 通过后网关只转发 allow 内字段（重新序列化的 JSON）。未知字段拒绝而不是静默丢弃。
- **请求头清洗**：丢弃来访的 `Authorization`、`Cookie`、全部 `X-Watcher-*`（含重复）、hop-by-hop 头与 `X-Request-Id`；转发给 watcher 的头只有：`Authorization: Bearer <gateway>`、`X-Watcher-Actor`、`X-Watcher-Token-Fingerprint`、`Accept: application/json`、写行的 `Content-Type: application/json`、媒体行的 `Range`/`If-Range`。
- **不包 `_envelope()`**：网关响应 body 即 watcher JSON 经 §9.6 过滤后的结果，不做 Postgres 查询（`_envelope` 需要 Postgres，会破坏 §2.1 隔离）。
- **分页**（`trading/messages`、`trading/briefings`，两身份相同）：排序 `created_at DESC, id DESC`；带游标时只返回 `(created_at, id) < (before_created_at, before_id)` 的行；`hours` 缺省 24、上限 168；`limit` 缺省 500、上限 500；响应仍是数组（形状不变），客户端取最后一行的 `(created_at, id)` 作为下一页游标。`created_at` 为 SQLite `datetime('now')` 的 UTC 文本（`YYYY-MM-DD HH:MM:SS`）。
- **status 响应**：P0 为现有字段 `configured, loggedIn, connected, watchGroups`（`server.js:583-591`）；P1 按计划 §5.1 追加 `observed_at, connection, listener, last_telegram_activity_at, last_message_ingested_at`，只增不改。

### 9.5 错误体与结构码

所有 `/v1/watcher/*` 错误响应与 watcher 新增/改造的错误响应统一为：

```jsonc
{
  "code": "revision_conflict",          // 必有；客户端只按 code 分流
  "message": "config revision changed", // 必有；人读文本，非契约
  "details": { ... },                   // 可选；绝不含 token、密钥、会话、密码
  "request_id": "9f0c…"                 // 网关生成（uuid4 hex）；网关发出或转发的每个错误体都带；watcher 直出可省略
}
```

watcher 对 `browser` 身份的错误体额外保留 `error`（= `message`），兼容站点 `public/index.html:925-927`；网关转发时删除 `error`。

| HTTP | `code` | 产生层 | 含义 / details | app 处理（计划 §2.1） |
|---|---|---|---|---|
| 400 | `invalid_query` | 网关、watcher | query 未列/重复/不合规 | 显示错误 |
| 400 | `invalid_body` | 网关、watcher | 非 JSON/未知字段/缺字段/类型不符 | 显示错误 |
| 400 | `secret_field_rejected` | 网关、watcher（gateway 身份） | 提交秘密字段；`details.fields` 仅键名 | 显示错误（属 bug） |
| 400 | `masked_value_rejected` | watcher（browser 身份） | 回写掩码占位值作为密钥 | — |
| 400 | `validation_failed` | watcher | 业务校验失败；`details.field`、`details.rule` | 显示错误 |
| 400 | `invalid_actor_headers` | watcher | actor/指纹头缺、多或不该出现（§9.8） | 视为 `watcher_unavailable`（网关会映射为 503） |
| 401 | `unauthenticated` | 网关、watcher | 缺/坏凭据 | 交易服务连接失效入口 |
| 403 | `invalid_token` | 网关 | 未知控制面 token（保持现有 403） | 交易服务连接失效入口 |
| 403 | `insufficient_scope` | 网关 | 角色不足或 signal token | "当前凭据无此权限"，不引导换密钥 |
| 403 | `identity_forbidden` | watcher | 身份不匹配该路由（交叉调用） | 经网关时映射为 503 |
| 404 | `route_not_found` | 网关、watcher | 路径未注册 / 路径参数不合规 | 显示错误 |
| 404 | `not_found` | watcher | 资源不存在（账号/路由/品种/提醒/媒体） | 缺图不阻塞文本 |
| 405 | `method_not_allowed` | 网关、watcher | 已注册路径未列方法；带 `Allow` | 显示错误 |
| 409 | `revision_conflict` | watcher | 条件写版本不符（§9.12） | 展示最新差异，重新确认 |
| 409 | `idempotency_conflict` | watcher | 同键异摘要（§9.12） | 显示错误，不自动重试 |
| 409 | `constraint_conflict` | watcher | 既有业务冲突（有子账号、被路由引用、有活动单、执行账号已存在、主子环境不一致）；保留原计数字段 | 显示错误 |
| 413 | `payload_too_large` | 网关 | body > 64 KiB | 显示错误 |
| 416 | `range_not_satisfiable` | 网关、watcher | 多段/畸形/越界 Range | 缺图不阻塞文本 |
| 500 | `snapshot_invalid` / `snapshot_unreadable` | watcher（snapshot 身份） | §9.10 | app 不可达 |
| 500 | `internal_error` | watcher | 未分类异常（替换现有 `Internal server error`） | 经网关时映射为 503 |
| 503 | `watcher_unavailable` | 网关 | 上游 401/403/5xx、3xx、超时、连接失败、非 JSON、不合契约的错误体；`details.reason` | 仅在信号页/交易配置页提示"采集服务不可达" |
| 503 | `gateway_busy` | 网关 | 准入信号量已满（§9.7） | 同 `watcher_unavailable`；写请求用同一 `client_ref` 重试 |
| 503 | `gateway_disabled` | 网关 | operator-query 未配置 `WATCHER_GATEWAY_TOKEN` | 同 `watcher_unavailable` |
| 503 | `auth_unavailable` | 网关 | 控制面 token 目录不可用 | 同 `watcher_unavailable` |
| 503 | `db_busy` | watcher | SQLite 锁等待超过 `busy_timeout`；事务已回滚 | 用**同一** `client_ref` 重试 |
| 503 | `media_too_large` | 网关、watcher | 整文件 > 20 MB，发头前拒绝 | 缺图不阻塞文本 |

### 9.6 上游响应映射（网关）

- 原样透传状态码（body 经下述过滤）的只有：`200`、`206`（媒体）、`400`（`code ≠ invalid_actor_headers`）、`404`（`code ≠ route_not_found`）、`409`、`416`，以及 `503` 且 `code ∈ {db_busy, media_too_large}`。
- 其余一律 503 `watcher_unavailable`：上游 401、403、405、413、400 `invalid_actor_headers` 与 404 `route_not_found`（网关自身缺陷或契约漂移）、其他 5xx、3xx（`follow_redirects=False`）、连接/读/总超时、JSON 行返回非 JSON 或超 8 MiB、错误体缺 `code`。`details.reason` ∈ `upstream_status`、`timeout`、`connect_error`、`redirect`、`malformed_response`、`response_too_large`，可带 `upstream_status`，**不回显上游 body**。上游 401 绝不透传。
- 错误体：从上游取 `code`/`message`/`details`，丢弃其他键（含 `error`），加网关 `request_id`。
- 成功体过滤：按行 `response.omit` 删除键；再对整个 JSON 递归删除命中 `secret_key_pattern` 的键（删除而非掩码）；`details.current` 同样过滤。
- 响应头：只回 YAML `response_headers.<行的 headers>` 中的头，并删除 `Connection` 声明的逐跳头。JSON 行由网关设置 `Cache-Control: no-store`。配置读行（accounts/channels/risks GET）回传 watcher 的 `X-Config-Revision`。

### 9.7 async、准入与超时（P1-12）

- 网关路由全部 `async def`，使用异步 HTTP 客户端与异步流；禁止同步 `def` 转发、禁止同步 handler 等待 future、禁止在线程池内获取准入。
- 准入：每个 worker 在事件循环上非阻塞 try-acquire 对应预算的信号量，取不到立即 503 `gateway_busy`；在 `finally` 中关闭上游并释放槽（含客户端取消）。准入发生在 §9.3/§9.4 校验之后、发起上游请求之前。
- 三份预算互不挤占，各自独立的 HTTP 客户端与连接池（数值以 YAML `budgets` 为准）：

  | 预算 | 每 worker 槽 | 配置项 | 超时 connect/read/write/pool/total（秒） |
  |---|---|---|---|
  | `config`（全部 JSON 行） | 4 | `WATCHER_GATEWAY_CONFIG_SLOTS` | 1 / 6 / 2 / 0.5 / 8 |
  | `media` | 4 | `WATCHER_GATEWAY_MEDIA_SLOTS` | 1 / 10（单块）/ 10 / 0.5 / 60 |
  | `snapshot`（快照 reader，单飞） | 1 | — | 1 / 2 / 1 / 0.5 / 2 |

  网关每 worker 合计 8 槽（config + media），舰队在途上限 = 8 × operator-query worker 数；该数值写入配置与测试。`config.total = 8s` 大于 watcher `busy_timeout = 5s`，保证 `db_busy` 能回到客户端而不是先超时。
- 影响范围（P1-19）：隔离只保证不依赖快照的端点（查询、平仓、止盈止损、dry_run 平仓）不受 watcher 故障影响；快照过期后开仓返回 `snapshot_unavailable` 是预期行为，不为此放宽 `max_age`。

### 9.8 actor 与指纹头（P1-14）

- 网关在 §9.3 鉴权成功后，删除来访的全部 `X-Watcher-*`，再注入恰好一条：
  - `X-Watcher-Actor: app:<role>`，`role` 为四角色之一（会话 token 同样只写角色，不写 subject）。
  - `X-Watcher-Token-Fingerprint: <hex12>` = `sha256(规范化 token 的 UTF-8).hexdigest()[:12]`，规范化与 `principal.py:113` 相同（去 `"Bearer "` 前缀后 `strip()`）。
  - 任一应注入的值算不出来 → 不转发，503 `watcher_unavailable`（`details.reason=actor_injection_failed`）。
- watcher（`req.rawHeaders` 计数）：
  - `gateway` 身份：两个头必须各恰好一条且匹配 YAML `actor_headers.*_pattern`，否则 400 `invalid_actor_headers`；不合并、不取第一条、不默认。写行另校验 actor 为 `app:risk_admin`，否则 403 `identity_forbidden`（纵深防御）。
  - `snapshot` / `browser` 身份：出现任一 `X-Watcher-Actor` 或 `X-Watcher-Token-Fingerprint` → 400 `invalid_actor_headers`。`browser` 写入的 actor 固定为 `browser`。
- 指纹只是审计标签，不是授权依据，也不是唯一身份；`role` 不是自然人。双层白名单挡不住网关进程本身失陷（计划 §8 风险表）。

### 9.9 媒体（P1-16）

适用 `gw.media.get` / `gw.media.head`（网关）与 `br.media.*`（watcher 对 browser 同规则）。

- **文件名**：gateway 只接受 `^[0-9]{10,16}-[0-9]{1,20}\.(jpg|jpeg|png|gif|webp)$`；Content-Type 由扩展名决定（jpg/jpeg→`image/jpeg`、png、gif、webp）。browser 用 YAML `browser_pattern`。
- **watcher 侧**：鉴权在 static 之前；`realpath` 必须落在媒体根（`WATCHER_MEDIA_DIR`）内，禁止符号链接逃逸，否则 404 `not_found`；缺文件 404。
- **Range**（网关先校验，watcher 同规则）：只支持单段 `bytes`，语法严格为 `bytes=<start>-<end>`、`bytes=<start>-`、`bytes=-<suffix>`（十进制，无空白，`start ≤ end`，`suffix > 0`）。多段（含逗号）、其他单位、畸形 → 416 `range_not_satisfiable`，**不回退为整文件 200**；`start ≥ size` → watcher 416 并带 `Content-Range: bytes */<size>`。`bytes=0-0` → 206，`Content-Range: bytes 0-0/<真实大小>`，`Content-Length: 1`。`If-Range` 转发，由 watcher 按 ETag/Last-Modified 判定 206 或 200。
- **HEAD**：忽略 Range（网关不转发 Range/If-Range），返回 200、整文件元数据，`Content-Length` = 真实全文件大小，无 body。
- **大小**：以整文件大小判定（watcher 用 `stat`；网关用上游 200 的 `Content-Length` 或 206 的 `Content-Range` total，缺失视为不合规）。> 20 MB（20 971 520 字节）→ 发响应头之前 503 `media_too_large`。响应头发出后出现超限、超时、断开：只能截流、关闭上游、记一条 `truncated` 日志（`request_id`、`filename`、`bytes_sent`、`reason`），不改状态码。
- **流控**：块 ≤ 64 KiB，异步迭代、有背压，总时限 60s；客户端取消后在 `finally` 关闭上游并释放槽。
- **头**：请求只转 `Range`、`If-Range`；响应只回 `Content-Range`、`Accept-Ranges`、`Content-Length`、`Content-Type`、`ETag`、`Last-Modified`、`Cache-Control`，并删除 `Connection` 声明的逐跳头。`Cache-Control: private, no-store`（网关强制覆盖）。`no-store` 与 app 本地清理都不能撤回已落盘字节（计划 §8 已接受）。
- **上游**：host 固定为配置值 `WATCHER_GATEWAY_URL`（缺省 `http://127.0.0.1:9090`），不跟随重定向。
- app 媒体请求独立携带同一 bearer，不走 JSON `send()`。

### 9.10 config-snapshot（snapshot 身份，计划 §2.2）

`GET /api/trading/config-snapshot`，只接受 `snapshot` 身份，无 query、无 body。同一 SQLite 读事务内读取三表与 `config_revision`。

```jsonc
{
  "schema_version": "watcher-config-snapshot.v1",
  "revision": 42,                                   // config_revision 当前值（整数）
  "content_sha256": "<64 位小写 hex>",
  "generated_at": "2026-09-26T08:00:00.000Z",       // 仅表示生成时间，UTC，毫秒，Z 结尾
  "accounts": [{
    "account_id": "…",                              // account_configs.account_id
    "kind": "main" | "sub",                         // account_type：main→main，subaccount→sub
    "parent_account_id": "…" | null,                // main 或空串 → null
    "execution_account_id": "…",
    "enabled": true,                                // is_enabled = 1
    "risk_capital_addon": "0",                      // 十进制字符串
    "default_risk": "0.01" | null                   // default_risk_ratio，十进制字符串；NULL → null
  }],
  "channels": [{ "channel_id": "…", "target_account_id": "…" }],
  "risks":    [{ "symbol": "BTCUSDT", "risk_ratio": "0.02" }]
}
```

- **白名单构造**：只输出上列字段（YAML `snap.config_snapshot.get.response.fields`）。`api_key`、`api_secret`、`is_testnet`、`risk_capital_multiplier`、`channel_name`、任何会话/token 字段都不出现。禁用账号也输出（`enabled:false`）。
- **排序**：accounts 按 `account_id`、channels 按 `channel_id`、risks 按 `symbol`，均 `COLLATE BINARY` 升序。
- **数值**：`risk_capital_addon`、`default_risk`、`risk_ratio` 输出为十进制字符串（JS `String(Number(x))`），与 §2 "string 十进制"惯例一致，并保证跨语言摘要可复现。
- **`content_sha256`** = SHA-256（小写 hex）of 规范化 JSON `{"accounts":…, "channels":…, "risks":…, "schema_version":…}`：键按字典序、分隔符 `,`/`:` 无空白、UTF-8、非 ASCII 不转义（Python 等价：`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`）。不含 `revision` 与 `generated_at`。控制面收到后必须重算并比对。
- **服务端校验**（任一失败 → 500 `snapshot_invalid`，不发布快照；`details.violations: [{rule, table, key}]`，`key` 只含非秘密主键）：`duplicate_execution_account`、`dangling_route`（路由目标不在 accounts）、`invalid_parent`（sub 缺父、父不存在、父非 main、自引用；main 带父）、`invalid_number`（负数或非有限数）、`missing_field`（必需列缺失或 NULL，`default_risk_ratio` 除外）、`invalid_enum`（`account_type`/`is_enabled` 越界）、`invalid_string`（空主键、控制字符、孤立代理项）。
- **空与不可读**：三表存在但无行 → 正常返回空数组；表不存在、SQLite 读错误 → 500 `snapshot_unreadable`；锁超时 → 503 `db_busy`。
- 其他身份调用 → 403 `identity_forbidden`；无身份 → 401。
- **版本关系**：同 `revision` 不同 `content_sha256` 说明存在绕过 revision 的写入者，控制面判为 `invalid`（见 §9.11）。

### 9.11 控制面快照缓存状态机（C-0，计划 §2.2）

- 开关 `WATCHER_CONFIG_SNAPSHOT_ENABLED`，缺省 `0`（沿用旧 SQLite reader，`read_api.py:6214-6244` 的路径解析与旧 reader 保留到影子对照通过）。其他配置：`WATCHER_SNAPSHOT_URL`（缺省 `http://127.0.0.1:9090/api/trading/config-snapshot`）、`WATCHER_SNAPSHOT_TOKEN`。
- 参数：`refresh_interval = 30s`、`max_age = 60s`、单次请求总时限 2s、单飞（每 worker 同时最多一个在途刷新）。失败退避 1s、2s、4s、8s，此后每 15s，无抖动；成功后回到 30s 周期。刷新由后台任务驱动，不依赖请求触发。
- 状态（每 worker 独立）：
  - `cold`：从未成功验证过（启动预热失败时 worker 仍就绪，但处于 `cold`）。
  - `fresh`：距最近一次成功验证 `age ≤ max_age`。刷新失败不改变状态，只让 age 增长；**不设宽限态**。
  - `expired`：`age > max_age`。
  - 最近一次失败原因另记 `last_refresh_error ∈ {null, timeout, unavailable, unauthorized, invalid, schema_mismatch}`；对外的 `snapshot_state` 在非 fresh 时优先报告 `unauthorized`（上游 401）或 `invalid`（校验失败），否则报 `cold`/`expired`。
- 成功验证 = HTTP 200 ∧ `schema_version == "watcher-config-snapshot.v1"`（否则 `schema_mismatch`）∧ 重算 `content_sha256` 相等 ∧ 字段白名单与类型合规 ∧ 非"同 revision 异摘要"（否则 `invalid`）。无效快照永不替换已缓存的有效快照。`revision` 小于缓存值时接受并记 `snapshot_revision_regressed` 告警（真库回滚场景）。
- 使用：`open_position`（含 dry_run）在状态非 `fresh` 时一律拒绝，HTTP 503、body 保持既有形状 `{"detail": "snapshot_unavailable"}`。非开仓路径不引入快照依赖（回归测试锁定）。展示类读取若使用快照，须带 `stale: true`。
- 单次订单从同一不可变快照对象取路由、addon、risk，不跨版本；dry_run 与正式提交的 `checks` 追加一项（只增）：`{"name": "config_snapshot", "passed": true, "revision": 42, "content_sha256": "…", "age_ms": 1234, "snapshot_state": "fresh"}`。
- 恢复：watcher 恢复后 30s 内回到 `fresh`（退避上限 15s + 请求 2s 保证）。

### 9.12 watcher 写路径契约（计划 §2.3）

**新表**（watcher 自己的 SQLite，按计划执行；不涉及控制面迁移与记账表）：

```sql
CREATE TABLE IF NOT EXISTS config_revision (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  revision INTEGER NOT NULL CHECK (revision >= 0)
);                                   -- 缺行时插入 (1, 0)
CREATE TABLE IF NOT EXISTS config_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL UNIQUE,   -- actor || '|' || operation || '|' || client_ref
  actor TEXT NOT NULL,                    -- 'app:<role>' | 'browser'
  source TEXT NOT NULL,                   -- 'gateway' | 'browser'
  token_fingerprint TEXT,                 -- gateway 身份的 12 位指纹；browser 为 NULL
  operation TEXT NOT NULL,                -- YAML write.operation
  request_sha256 TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  response_json TEXT NOT NULL,            -- 不含任何秘密
  revision_before INTEGER NOT NULL,
  revision_after INTEGER NOT NULL,
  created_at TEXT NOT NULL                -- UTC ISO-8601
);
```

- 所有连接显式 `PRAGMA busy_timeout = 5000`；事务短，不含网络 await。price monitor 的 DB 路径须先统一到 canonical env（计划 §7.1），P3 写行才能与 `config_audit` 同库同事务。
- **写请求公共字段**：所有写行（两种身份）必带 `client_ref`（`^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$`，调用方生成，建议 UUID）。YAML `write.conditional: true` 的行（账号 POST/PUT/DELETE、路由 POST/DELETE、风险 POST/DELETE）必带 `expected_revision`（整数，全局 `config_revision`）；缺失 → 400 `invalid_body`，**不存在缺版本的覆盖通道**（浏览器同样发送）。DELETE 的参数同样放在 JSON body。
- **`request_sha256`** = SHA-256 of 规范化 JSON `{"method", "path"(解码后的 inner path), "body"(去掉 client_ref)}`（规则同 §9.10）。含密钥的 browser 请求只存摘要，明文不落库。
- **transactional 行的顺序**（全部在一个 `BEGIN IMMEDIATE` 事务内；拿锁超时 → 回滚、503 `db_busy`）：
  1. 按 `idempotency_key` 查审计：命中且摘要相同 → 返回存储的 `status_code` 与 `response_json`，其中 `replay` 置为 `true`（**先于版本比较**，已成功的重放不被误判为冲突）；命中且摘要不同 → 409 `idempotency_conflict`。
  2. 读 `config_revision`；conditional 行 `expected_revision ≠ revision` → 409 `revision_conflict`。
  3. 业务校验（现有规则 + 计划 §6.1：风险比例与 `default_risk_ratio` ∈ `(0, 0.1]`，`addon ≥ 0` 有限数，主子账号规则；gateway 身份额外拒绝秘密字段；browser 身份拒绝掩码值）→ 400/404/409 `constraint_conflict`。
  4. 业务写；`bumps_revision` 行 `revision + 1`。
  5. 插入 `config_audit`（`status_code = 200`，`response_json` 为成功响应且 `replay:false`）。
  6. `COMMIT`；事务外只发响应。
  - 被拒的请求（第 1 步重放除外的任何 4xx/503）不写审计、不占幂等键；崩溃在 COMMIT 前 = 无变更无审计；COMMIT 后响应丢失 = 同 `client_ref` 重试拿到原响应。
- **reentrant 行**（`groups.save`、`telegram.disconnect`、`telegram.reconnect`，副作用在 JSON 文件或 Telegram 连接，不在 SQLite 事务内）：先按幂等键查审计（命中同摘要 → 重放；异摘要 → 409）；执行副作用；再单独事务插入审计（`revision_before = revision_after`）；插入撞 UNIQUE 时按第 1 步规则返回。不承诺跨存储恰好一次，成功以回读 `/status` 为准。disconnect/reconnect 须在 5s 内响应（结果以回读为准）。
- **成功响应**（所有写行、所有身份）：保留现有字段（如 `ok`、`account_id`、`channel_id`、`target_account_id`、`symbol`、`risk_ratio`、`id`、`watchGroups`），并**总是**追加 `revision`（写后的全局 revision；不改 revision 的行返回当前值）与 `replay`（首次 `false`，重放 `true`）。不含秘密字段。
- **409 `revision_conflict`**：

  ```jsonc
  { "code": "revision_conflict", "message": "config revision changed",
    "details": { "expected_revision": 12, "current_revision": 14,
                 "resource": "account" | "channel" | "risk", "key": "<主键>",
                 "current": { /* 该键在对应 GET 列表中的行（gateway 删秘密列、browser 掩码）；不存在则 null */ } } }
  ```

  UI 展示最新差异后重新确认；重新确认是新的用户决定，使用**新的** `client_ref`（原请求已确定未生效）。
- **409 `idempotency_conflict`**：`details: {"client_ref", "operation"}`，不回显原请求或原响应。
- **503 `db_busy`**：`details: {"busy_timeout_ms": 5000, "retryable": true}`，事务已回滚，用**同一** `client_ref` 重试。
- **客户端重试规则**（A-0）：只有确定性 4xx 之后才允许换新 `client_ref`；超时、`status 0`、`watcher_unavailable`、`gateway_busy`、`db_busy` 一律结果未知或未执行，必须用同一 `client_ref` 回读或重放。
- **配置读的 revision**：accounts/channels/risks 的 GET 在同一读事务里读 `config_revision`，以响应头 `X-Config-Revision: <int>` 返回（body 仍为数组，站点兼容），网关透传该头。
- **browser 密钥**：`br.account.put` 的 `api_key`/`api_secret` 为空 → 保留原值（现状 `trading-api.js:581-590`）；值等于当前掩码或匹配 `^\*{4}$`、`^.{4}\.\.\..{4}$` → 400 `masked_value_rejected`。gateway 身份对任何秘密字段 → 400 `secret_field_rejected`，DB 不变（watcher 第二层，不依赖网关）。
- **保留期**：`config_audit` 至少保留 30 天；幂等保证只在保留期内成立（过期后重放按新请求处理，conditional 行会因版本不符得到 409）。
- **写入者**：watcher 是三张配置表唯一写入者（D1）；`db_manager.py` 写命令停用或改走本服务。任何绕过者会造成"同 revision 异摘要"，被 §9.11 判为 `invalid`。

### 9.13 兼容性声明

- 既有 `/v1/*`、`/api/system/snapshot` 等端点的字段、状态码、错误体（`{"detail": …}`）一律不变；错 token 仍 403。`/v1/watcher/*` 是新增前缀，统一错误体只作用于此前缀。
- `/v1/operator/orders` 只增：新 `checks` 项 `config_snapshot`；快照不可用时 503 `{"detail":"snapshot_unavailable"}`（仅开关打开后、仅开仓路径）。
- watcher 站点：GET 响应形状不变（数组）；新增 `X-Config-Revision` 头；错误体新增 `code/message/details` 并保留 `error`；写请求新增必填 `client_ref` 与 `expected_revision`（站点 JS 随 W-0 同步改）；风险比例校验从"非负"收紧为 `(0, 0.1]`（计划 §6.1）。
- app 不新增存储键与设置项；watcher 服务层独立实例，其写禁用不传给交易 `TradingApi`。

### 9.14 路由真源校验脚本规格（C-1 实现，本节只定规格）

**入口**：`scripts/check_watcher_gateway_routes.py`（Python 3.12，`.venv-arch`，依赖 PyYAML），三种模式：`--self-check`、`--generate --phase-max P<n>`、`--check`（缺省：self-check + 生成物 diff + 运行时 diff）。所有测试夹具 token 由脚本随机生成，不读取任何真实环境值。任何检查比较了 0 行即判失败。输出最后一行 `ROUTES_DIFF_EMPTY rows=<n> yaml_sha256=<hex> phase_max=<Pn>`，退出码 0；否则逐条列出差异，退出码 1。

**9.14.1 真源自洽检查（S-01..S-19）**

| 编号 | 检查 |
|---|---|
| S-01 | `yaml.safe_load` 成功；顶层键齐全；`schema_version == watcher-gateway-routes.v1`；`frozen_at` 为 ISO 日期 |
| S-02 | 每行键集合恰为 `id, phase, identity, method, outer_path, inner_path, roles, query, body, response, budget, write`；`body` 含 `allow/deny/required`（可选 `field_overrides`）；`response` 含 `omit/mask/headers`（snapshot 行另含 `fields`） |
| S-03 | `id` 唯一，形如 `^(gw\|snap\|br)\.[a-z0-9_]+\.(get\|head\|post\|put\|delete)$`，前缀与 identity 对应、后缀与 method 对应 |
| S-04 | identity ∈ {gateway, snapshot, browser}，三者各至少一行 |
| S-05 | phase ∈ {P0, P1, P2, P3}；snapshot 与 browser 行为 P0 |
| S-06 | method ∈ {GET, HEAD, POST, PUT, DELETE}；`(identity, method, inner_path)` 唯一；gateway `(method, outer_path)` 唯一 |
| S-07 | gateway 行：outer_path 以 `paths.app_outer_prefix + "/"` 开头；设 `rest = outer_path[len(prefix):]`，若 `rest` 以 `/media/` 开头则 `inner_path == rest`，否则 `inner_path == "/api" + rest`；两侧路径参数名与顺序一致 |
| S-08 | 非 gateway 行：`outer_path`、`roles` 为 null；browser 行 `budget` 为 null；snapshot 行 `budget == snapshot` |
| S-09 | gateway GET/HEAD 行 `roles` 与 `roles.all_readers` 集合相等；POST/PUT/DELETE 行 `roles == [risk_admin]` |
| S-10 | gateway 行：`secret_fields ⊆ body.deny`；`body.allow ∩ secret_fields = ∅`；`body.allow` 与 `query` 的每个名字都不匹配 `secret_key_pattern`；所有行 `allow ∩ deny = ∅`、`required ⊆ allow` |
| S-11 | `body.allow`（经 `field_overrides`）全部定义在 `body_fields`；gateway 行不得引用 `secret: true` 或 `browser_only: true` 的定义；`query` 全部定义在 `query_params`；每个 `{param}` 在 `path_params` 中有该身份的非 null pattern |
| S-12 | GET/HEAD 行 `body.allow == []` 且 `write == null`；inner_path 命中 `never_allowed` 的 browser 凭据面写行（`/api/config`、`/api/login/*`）`write == null`（不审计、不带 client_ref，站点登录流程不变）；其余写行 `write` 非 null、`client_ref ∈ required`；`conditional` → `expected_revision ∈ required`；`bumps_revision` → `conditional`；`idempotency ∈ {transactional, reentrant}` |
| S-13 | HEAD 只出现在 inner_path 以 `/media/` 开头的行，且每个 HEAD 行都有同身份同路径的 GET 行；媒体行 `response.headers == media` |
| S-14 | gateway 媒体行 `budget == media`，其余 gateway 行 `budget == config` |
| S-15 | `response.headers` 是 `response_headers` 的键；`json_config_read` 只用于 accounts/channels/risks 的 GET |
| S-16 | gateway 行 `response.mask == []`（网关只删不掩）；gateway 与 browser 的 accounts 读写行分别 `omit ⊇` / `mask ⊇ [api_key, api_secret]` |
| S-17 | snapshot 恰一行 `GET /api/trading/config-snapshot`，`response.fields` 与 §9.10 白名单逐项相等 |
| S-18 | 没有 gateway 行的 `(method, inner_path)` 命中 `never_allowed` 或 `gateway_excluded`（`*` 匹配一个或多个段，`{x}` 匹配一个段） |
| S-19 | 覆盖预言（独立于 YAML 手写在脚本里，来源计划 §3）：gateway `(method, outer_path, phase)` 集合恰为 9.14.2 的 24 行 |

**9.14.2 覆盖预言（计划 §3 → gateway 24 行）**

| phase | 行 |
|---|---|
| P0（1） | GET `/v1/watcher/status` |
| P1（8） | GET `trading/messages`；GET `trading/briefings`；GET、HEAD `media/{filename}`；POST `disconnect`；POST `reconnect`；GET `trading/orders`；GET `trading/orders/active` |
| P2（11） | GET `dialogs`；POST `groups`；GET `trading/accounts`；PUT、DELETE `trading/accounts/{account_id}`；GET、POST `trading/channels`；DELETE `trading/channels/{channel_id}`；GET、POST `trading/risks`；DELETE `trading/risks/{symbol}` |
| P3（4） | GET、POST `price-alerts`；DELETE `price-alerts/{alert_id}`；GET `price-monitor/status` |

（均省略前缀 `/v1/watcher/`；另有 snapshot 1 行、browser 38 行。）

**9.14.3 生成物**（`--generate` 写出并提交；`--check` 在内存重新生成并逐字节比对）

| 生成物 | 内容 |
|---|---|
| `services/control-plane/api/watcher_gateway_routes.json` | `phase ≤ phase_max` 的 gateway 行 + snapshot 行，及 `budgets`、`secret_*`、`path_params`/`query_params`/`body_fields`、`response_headers` |
| `bridge/services/telegram-watcher/lib/watcher-routes.generated.json` | 三身份全部行（gateway 行按 `phase_max` 过滤）的 `(identity, method, inner_path, path 参数正则, query, body allow/deny/required, write)`，供 watcher 身份中间件使用 |
| `contracts/generated/caddy-watcher-gateway-paths.txt` | 每个唯一外部路径一行：`/m` + outer_path（`{param}` → `*`）与方法列表，字典序 |

规范化：JSON 键排序、2 空格缩进、结尾换行；行按 `(identity, inner_path, method)` 排序；每个文件带 `_generated_from`、`_yaml_sha256`、`_phase_max`。

**9.14.4 "生成路由与真源 diff 为空"的判定**

1. **生成物 diff**：三份生成物与内存重新生成的结果逐字节相同。
2. **控制面运行时 diff**：用夹具环境构造 `create_app("operator-query")`，枚举路径以 `/v1/watcher/` 开头的 `APIRoute`，得到 `{(method, path, name)}`；与 YAML 中 `phase ≤ phase_max` 的 gateway 行 `{(method, outer_path, watcher_gateway__<id>)}` 对称差为空；每个 endpoint 满足 `inspect.iscoroutinefunction`；同一集合不得出现在 `create_app("node-control")`、`create_app("event-ingest")`。
3. **watcher 运行时 diff**：`node bridge/services/telegram-watcher/scripts/dump-route-table.js` 输出 watcher 实际加载的身份路由表（W-0 须让 `server.js` 在 `require.main === module` 时才 `listen`，以便无副作用加载 `app`），得到 `{(identity, method, inner_path)}`，与 YAML 对称差为空；另枚举 Express 实际注册的 handler，每条 YAML inner 路由都有 handler，且不存在 YAML 之外的 `/api/*` 或 `/media/*` handler。
4. 三项全部为空且比较行数 > 0 → diff 为空。逐行属性（roles、query、body、write、budget）在集合相等后逐字段比较，任何不等都算非空。

**9.14.5 独立负例（T0-4，不从真源生成）**：百分号编码的斜杠与点（`%2F`、`%2e`）、双重编码（`%252F`）、尾斜杠、`//`、大小写（`/V1/Watcher/status`）、重复 query、未列 query、OPTIONS、非媒体 HEAD、未注册 404、未列方法 405；经网关遍历矩阵外请求时 watcher 请求计数不变；用 `gateway` token 直连 watcher 遍历矩阵外端点，第二层同样拒绝；`browser` 调 snapshot、`snapshot` 调网关写行被拒。

### 9.15 冻结时留待裁决的点（Planner / 用户）

- A-1 `/healthz` 改为要求 `browser` 代理凭证（容器健康检查带该头）——计划要求"每个请求都先校验"，但未说明健康检查如何带凭据。
- A-2 网关每 worker 8 槽按 config 4 + media 4 切分；snapshot 独立 1 槽不计入 8。
- A-3 watcher 503 `db_busy`/`media_too_large` 透传给 app，是对计划"上游 5xx 一律 `watcher_unavailable`"的收窄例外（否则 app 无法区分"未执行可重试"）。
- A-4 `default_risk_ratio` 与品种风险同样收紧到 `(0, 0.1]`（控制面 `read_api.py:8510-8512` 已对二者同一范围执行）。
- A-5 DELETE 的 `client_ref`/`expected_revision` 放 JSON body；`config_audit` 增加计划表外的 `token_fingerprint` 列（计划 §8、review P1-14 要求审计带指纹）。
- A-6 P3 价格提醒的 body/query 字段（`source/account_id/position_ref/environment`）依赖计划 §7.1 的表扩展，A-3 开工前如需调整由 Planner 召回 Architect。
- A-7 空数组 `groups` 沿用现状视为合法（停止监听全部群组），只靠 UI 确认拦截。
