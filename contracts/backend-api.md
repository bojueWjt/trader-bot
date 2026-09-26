# backend-api 接缝契约（v1.1，已冻结 frozen）

> 状态：**frozen（2026-08-30，G0 裁决）**——已吸收 codex 落地前 review 的 19 条阻断项（各条标注 review #N）。
> G2 以本文件 schema 造 fixtures 开工；G1 实现必须逐字段一致，偏差走 block 仲裁。
> 完整语义见设计文档 v1.1 对应小节；本文件只写死接缝形状。
> §9（watcher 网关与配置快照）单独版本化：当前 **WGW-1.0.1**（2026-09-26 勘误，勘误记录见 §9.16）；§1–§8 不受其影响。

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

> 状态：**frozen（WGW-1.0.1，2026-09-26）**——WGW-1.0（同日冻结、审查 wac-001 PASS、合入 `90e96d0`）的勘误版，写入 Planner 裁定 R1–R9 与审查 🟡-1..🟡-11，逐条见 §9.16。WGW-1.0 尚无实现，本勘误不构成对已发布接口的破坏性变更。设计真相：`docs/plans/2026-09-11-watcher-to-app-migration.md` v0.6 §2.1–§2.3、§3、§4.2/§4.3（含附录 E 吸收的 v0.3 review P1-12..P1-19、P2-03）。
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
| （WGW-1.0.1 补）`resolve_principal`：非 `"Bearer "` 开头 → `AuthRequired`；去前缀后 `strip()` 为空 → `AuthRequired`；token 目录互异校验 `assert_token_catalog_unique` 不含 `WATCHER_*` | `principal.py:111-115`、`:67-97` |
| （WGW-1.0.1 补）站点编辑账号 PUT 的 payload 含 `account_id`；编辑表单清空密钥输入（不回填掩码）；风险删除直接 `fetch`、不经 `api()` | `public/index.html:1513-1524`、`:1383-1384`、`:1806`；账号/路由删除 `:1584`、`:1745` |
| （WGW-1.0.1 补）现行 PUT 对合并后的整行重算校验（执行账号格式与唯一、子账号计数等） | `trading-api.js:496-600` |
| （WGW-1.0.1 补）watcher 镜像按显式白名单收文件 | `scripts/build_immutable_watcher_image.py:34`（`WATCHER_RUNTIME_RELATIVE_PATHS`） |
| （WGW-1.0.1 补）控制面发布把 `services/control-plane/api/*.py` 平铺到 `host/`，兄弟模块以顶层名 import | `scripts/make_account_stall_release.py:93-99`、`:217`；`read_api.py:100`（`import position_revision`） |
| （WGW-1.0.1 补）旧 reader 宽松比较：`account_type` 用 `lower(trim())`，启用值接受 `1/active/enabled/true`；执行账号 `default_risk_ratio` 为 NULL 时 `float(None)` → 503，不回落 env 默认 | `read_api.py:6298-6308`、`:6370`、`:8509-8517` |
| （WGW-1.0.1 补）控制面 `.venv-arch` 无 PyYAML | 审查 wac-001 🟡-11 实测 |

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

- **"已配置"的定义（R2）**：环境变量存在且值不是空串。未设置或为空串的 `*_TOKEN_PREVIOUS` 视为未配置，**永不参与匹配**。
- **watcher 启动**（R2）：三个当前值必须已配置；每个已配置值（含已配置的 `*_PREVIOUS`）必须匹配 `^[\x21-\x7E]{32,}$`（可打印 ASCII、无空白、≥ 32 字节）；全部已配置值（最多六个）两两互异。任一不满足 → 进程非零退出，错误信息只列变量名，不打印值。watcher 环境里不放任何控制面 reader token。
- **跨服务互异**（审查 💭-4）：O-0 发行侧校验 `WATCHER_*` 六个值与控制面 token 目录（`configured_token_values`，`principal.py:67-97`：四个 reader token、signal token、节点 token 等）两两互异。operator-query 启动时再做一次纵深检查：`WATCHER_GATEWAY_TOKEN` 或 `WATCHER_SNAPSHOT_TOKEN` 与 `configured_token_values` 任一相等 → 视同未配置（网关 503 `gateway_disabled`；快照开关打开时启动失败）并告警，告警只写变量名。
- **operator-query 启动**：缺 `WATCHER_GATEWAY_TOKEN` → 网关路由照常注册，但一律 503 `gateway_disabled` 并打告警日志；**不得**让交易端点启动失败。快照开关（§9.11）打开且缺 `WATCHER_SNAPSHOT_TOKEN` → 按计划 §2.1 启动失败（上线前配置校验锁定，开关默认关）。
- **轮换**：`*_TOKEN_PREVIOUS` 双值。顺序：watcher 先接受新旧两值 → 持有方切新值 → 确认请求与审计正常 → 清空 `*_PREVIOUS`。不是时钟过期。
- **watcher 身份判定**（每个请求，在 `server.js:68` 的 `express.json` 与全部 static/handler 之前）：
  1. 用 `req.rawHeaders` 计数（Node 会合并/丢弃重复头，不能用 `req.headers`）：`Authorization` 或 `X-Watcher-Proxy-Auth` 出现多于一条 → 401。
  2. `Authorization` 按 scheme（第一个空格之前的部分；Node 已剥掉头值首尾空白，所以 `Bearer␠` 到达时就是 `Bearer`）分流（R2）：
     - 没有 `Authorization` → 第 3 步。
     - scheme 按 ASCII 不区分大小写等于 `basic` → 忽略该头（Caddy basicauth 残留，Caddy 宜在注入前删除），进入第 3 步。
     - scheme 按 ASCII 不区分大小写等于 `bearer`：值必须恰为 `Bearer`（区分大小写）后跟一个空格（与 `principal.py:111` 相同），否则 401；去掉 `Bearer ` 后按 Python `str.strip()` 的空白集合剥离两端（与 `principal.py:113` 相同；对 latin1 头值等价于剥离 `[\t\n\v\f\r \x1C-\x1F\x85\xA0]`）。值恰为 `Bearer`、或剥离后为空串 → 401 `unauthenticated`，**不进入比较**。非空时与 gateway、snapshot 的**已配置**值逐一做恒定时间比较（对双方的 SHA-256 摘要用 `crypto.timingSafeEqual`，遍历全部已配置值、不短路）：命中 gateway → `gateway`；命中 snapshot → `snapshot`；否则 401。此时若同时带 `X-Watcher-Proxy-Auth` → 401（身份歧义）。
     - 其他 scheme → 401。
  3. 否则若带 `X-Watcher-Proxy-Auth`：按第 2 步同一规则剥离；为空 → 401；与 browser 的已配置值恒定时间比较，命中 → `browser`。
  4. 其余 → 401 `unauthenticated`。
  5. 路径规范化：`gateway`、`snapshot` 身份用原始请求路径（`req.originalUrl` 的 path 部分，未解码）匹配，含 `%`、`//`、点段、尾斜杠或路径参数不合 `gateway_pattern` → 404 `route_not_found`（与网关同规则）；`browser` 身份沿用 Express 单次解码（站点用 `encodeURIComponent`），参数按 `browser_pattern` 校验。
  6. 身份确定后查该身份的生成路由表：路径与方法都在 → 放行；路径在本身份表内但方法不在 → 405（`Allow` 头列本身份的方法）；路径只属于其他身份 → 403 `identity_forbidden`；任何身份都没有的路径 → 404 `route_not_found`。鉴权先于 404，未认证请求探测不到路由存在性。
  7. actor 头检查见 §9.8；之后才进入 body 解析与 handler。
- **watcher 处理顺序（规范性，WGW-1.0.1 写全）**：W1 `rawHeaders` 计数（401）→ W2 身份（401）→ W3 路径规范化与本身份路由表匹配（404 `route_not_found` / 405 `method_not_allowed` / 403 `identity_forbidden`）→ W4 actor 与指纹头（400 `invalid_actor_headers`；gateway 写行 actor ≠ `app:risk_admin` → 403 `identity_forbidden`）→ W5 挂载 `req.watcherAuth` 与 `req.watcherRoute` → W6 query 校验（400 `invalid_query`）→ W7 body：GET/HEAD 带 body → 400 `invalid_body`；超过 64 KiB（按 `Content-Length` 或流式计数）→ 413 `payload_too_large`；再按 §9.4 同一顺序做 content-type/JSON object、秘密字段、未知键、缺键、类型检查（gateway 行用 `gateway_enum`/`gateway_pattern`，browser 行用 `type` 定义），规则取自该行的生成物条目 → W8 handler（业务校验与写，§9.12）。静态文件与媒体也是路由表中的行，在 W5 之后才交给 static。任一步失败即返回。与网关（§9.3 R3）的差别是有意的：watcher 是内部服务，身份先于路由，未认证请求探测不到路由存在性；网关的路由集合公开在 YAML 中，先做路由匹配不泄露信息。
- **W-0 两个子任务之间的接口（规范性）**：子任务 A（身份与请求校验中间件）负责 W1–W7；子任务 B（写路径 handler，§9.12）只负责 W8。两者唯一的耦合是 A 在 `req` 上挂的两个只读属性：

  ```js
  // 用 Object.defineProperty(req, name, { value: Object.freeze(v), writable: false, configurable: false, enumerable: true }) 挂载
  req.watcherAuth = {
    identity: 'gateway' | 'snapshot' | 'browser',
    role: 'system_observer' | 'viewer' | 'risk_admin' | 'reviewer' | null, // 仅 gateway：取自 W4 已校验的 X-Watcher-Actor；其余身份为 null
    actor: string,               // gateway: 'app:<role>'；browser: 'browser'；snapshot: 'snapshot'
    tokenFingerprint: string | null, // 仅 gateway：W4 已校验的 X-Watcher-Token-Fingerprint（12 位小写 hex）；其余为 null
  };
  req.watcherRoute = /* 生成物 §9.14.3 中匹配到的那一行（深冻结）：id、identity、method、inner_path、query、body、response、write */;
  ```

  - B 只读这两个属性：不重新解析 `Authorization`、`X-Watcher-*` 头，不从 body 或 query 推断身份。两者缺失或形状不符 → 500 `internal_error`（fail-closed，不取默认值）。
  - B 写审计时：`config_audit.actor = watcherAuth.actor`，`source = watcherAuth.identity`，`token_fingerprint = watcherAuth.tokenFingerprint`，`operation = watcherRoute.write.operation`。`snapshot` 身份没有写行。
  - B 收到的 `req.body` 已经过 W7 校验（只含该行 `allow` 内的键）；B 只做业务校验（§9.12 第 3 步与 R7）。
  - B 的单元测试用同样的 `defineProperty` 方式构造这两个属性；A 的测试断言挂载出的对象与上面的形状逐键相等（不多不少）。
- `/healthz` 也受身份校验：它只有 `browser` 行（YAML `br.healthz.get`），它在 `never_allowed` 中，永不经网关。容器健康检查（`docker-compose.yml:160-176`）按 A-1 的条件改造（Planner 已接受）：健康检查是一段 node 脚本，在脚本内读取 `process.env.WATCHER_BROWSER_PROXY_TOKEN` 并以 `X-Watcher-Proxy-Auth` 头发出；**不得**在 compose 的 `test:` 中写 `$VAR`/`${VAR}` 插值（插值结果会以明文出现在 `docker inspect` 的 Healthcheck 字段）；脚本不打印响应体、请求头或 token，只以退出码表示结果。

### 9.3 网关鉴权与角色 scope

- **网关处理顺序（R3，规范性）**。每个 `/v1/watcher/*` 请求按下列顺序处理，任一步失败即返回，后续步骤不执行：

  | 步 | 内容 | 失败响应 |
  |---|---|---|
  | G1 | 路由匹配（§9.4 前缀中间件）：raw path 规则、phase 门、路径参数、方法 | 404 `route_not_found`；405 `method_not_allowed`（含 OPTIONS、非媒体 HEAD） |
  | G2 | 身份认证（`resolve_principal`，映射见下表） | 401 `unauthenticated`；403 `invalid_token`；403 `insufficient_scope`（signal token）；503 `auth_unavailable` |
  | G3 | 角色（YAML 行 `roles`） | 403 `insufficient_scope` |
  | G4 | 请求校验：先 query，再 body（先大小，再 content-type/JSON object，再秘密字段、未知键、缺键、类型，顺序见 §9.4） | 400 `invalid_query`；413 `payload_too_large`；400 `secret_field_rejected` / `invalid_body` |
  | G5 | 网关就绪：`WATCHER_GATEWAY_TOKEN` 已配置且通过 §9.2 跨服务互异检查 | 503 `gateway_disabled` |
  | G6 | 准入（§9.7） | 503 `gateway_busy` |
  | G7 | 请求头清洗与 actor/指纹注入（§9.4、§9.8） | 503 `watcher_unavailable`（`details.reason=actor_injection_failed`） |
  | G8 | 上游请求与响应映射（§9.6、§9.9） | 见 §9.6 |

  - G1 不读 body；G2、G3 未通过的请求，网关**不读取 body**（不 `await request.body()`、不解析 JSON），因此未认证或无权限的调用方拿不到 `secret_field_rejected`、`invalid_body`、`payload_too_large` 这类反馈。"viewer 带 `api_key` 调写行"稳定返回 403 `insufficient_scope`。
  - 实现约束：网关 endpoint 的签名只接收 `Request`，**不得**声明 `Body`/`Form`/Pydantic 模型参数，也不得挂会读取 body 的依赖（FastAPI 会在执行依赖与 handler 之前读取并解析这类参数，并可能返回 422，破坏上述顺序）。§9.14.4 的运行时测试断言每个网关路由 `route.dependant.body_params == []` 且 `route.body_field is None`。
- **空 bearer（R2）**：网关侧沿用 `resolve_principal`：`Authorization` 不以 `"Bearer "` 开头、或去前缀后 `strip()` 为空 → `AuthRequired` → 401 `unauthenticated`（`principal.py:111-115`）。uvicorn 同样会剥掉头值尾随空白，`Bearer␠` 到达时为 `Bearer`，落入"不以 `Bearer ` 开头"分支。网关不另写一套 token 解析。
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
- **匹配基于 `scope["raw_path"]`**（未解码），规则：raw path 含 `%`、`//`、`.`/`..` 段、尾斜杠、大小写不符，或路径参数不匹配 YAML `path_params.*.gateway_pattern` → 404 `route_not_found`。**禁止尾斜杠 307 重定向**。
- **未注册路径 404、已注册路径未列方法 405**（`Allow` 头列 YAML 中该路径的方法），与 FastAPI 默认一致；这两类判定先于鉴权（§9.3 G1）。`OPTIONS` 一律 405（无 CORS）。`HEAD` 只在 YAML 显式列出的行允许（仅媒体）；其余路径的 HEAD → 405。
- **前缀中间件（R4，规范性；替代 WGW-1.0 的"实现提示"）**：
  - G1 由一个纯 ASGI 中间件实现，它与 §9.5 错误体处理器一起，显式安装到 `create_app()` 返回的角色 app 上（`app_roles.py:145-178` 只复制 `APIRoute`，不复制中间件与异常处理器）。operator-query 必须安装；node-control、event-ingest 无论是否安装，都不得出现网关路由。
  - 触发条件：对 `scope["path"]`（已解码）和 `scope["raw_path"]`（未解码，按 latin1 解码为文本）各判定一次，按 ASCII 不区分大小写比较；任一等于 `/v1/watcher`，或以 `/v1/watcher/`、`/v1/watcher%` 开头 → 由中间件判定。未触发的请求原样交给 FastAPI 路由，既有 `/v1/*` 行为不变。
  - 中间件按生成物（§9.14.3）匹配 `(method, raw_path)`。不匹配时由中间件直接返回 404/405（§9.5 形状），**不交给 Starlette 路由**，因此 `redirect_slashes` 不会产生 307。匹配成功时把行 id 写入 `scope["state"]["watcher_gateway_route_id"]` 再交给路由；网关 endpoint 第一步断言该值等于自身行 id，不等（中间件未安装或被绕过）→ 404 `route_not_found`（fail-closed）。
  - **禁止**：在角色 app 或全局设置 `redirect_slashes=False`（会把既有 `/v1/*` 的尾斜杠 307 变成 404，违反 §9.13）；用 catch-all `APIRoute`（如 `/v1/watcher/{rest:path}`）实现（会破坏 §9.14.4 的运行时路由集合相等）。
  - 回归测试（C-1 验收）：`/v1/accounts/`（尾斜杠）的状态码、`Location` 头、响应体与改动前逐字节一致；`/v1/watcher/status/` → 404 `route_not_found` 且无 `Location`；`/v1%2Fwatcher/status`、`/V1/Watcher/status`、`/v1/watcher` → 404 `route_not_found`（§9.5 形状）。
- `/v1/watcher/` 前缀（按上面的触发条件）下的 404/405 响应体使用 §9.5 统一形状；该前缀以外的 404/405/422 响应体保持 FastAPI 现状。
- **路由名**：`watcher_gateway__<YAML id 中的 . 换成 _>`；必须出现在 operator-query 角色 app，不出现在 node-control / event-ingest。
- **阶段门**：生成时带 `phase_max`（P0..P3），只注册 `phase ≤ phase_max` 的行；未注册行按 404 处理。`phase_max` 写入生成物并参与 §9.14 diff。
- **query**：只接受该行 `query` 列出的键，定义见 YAML `query_params`；未列键、同键重复、值不合规、`before_created_at` 与 `before_id` 不成对 → 400 `invalid_query`。媒体行不接受任何 query（token 禁止入 query）。
- **body**：
  - GET/HEAD 行带 body → 400 `invalid_body`。大于 64 KiB（`Content-Length` 超限立即拒绝，不读 body；无 `Content-Length` 时流式计数，超限即停止读取）→ 413 `payload_too_large`。写行必须 `Content-Type: application/json` 且为 JSON object，否则 400 `invalid_body`。
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
| 413 | `payload_too_large` | 网关、watcher | body > 64 KiB | 显示错误 |
| 416 | `range_not_satisfiable` | 网关、watcher | 多段/畸形/越界 Range | 缺图不阻塞文本 |
| 500 | `snapshot_invalid` / `snapshot_unreadable` | watcher（snapshot 身份） | §9.10 | app 不可达 |
| 500 | `internal_error` | watcher | 未分类异常（替换现有 `Internal server error`） | 经网关时映射为 503 |
| 503 | `watcher_unavailable` | 网关 | 上游 401/403/5xx、3xx、超时、连接失败、非 JSON、不合契约的错误体；`details.reason` | 仅在信号页/交易配置页提示"采集服务不可达" |
| 503 | `gateway_busy` | 网关 | 准入信号量已满（§9.7） | 同 `watcher_unavailable`；写请求用同一 `client_ref` 重试 |
| 503 | `gateway_disabled` | 网关 | operator-query 未配置 `WATCHER_GATEWAY_TOKEN` | 同 `watcher_unavailable` |
| 503 | `auth_unavailable` | 网关 | 控制面 token 目录不可用 | 同 `watcher_unavailable` |
| 503 | `db_busy` | watcher | SQLite 锁等待超过 `busy_timeout`；事务已回滚 | 用**同一** `client_ref` 重试 |
| 503 | `media_too_large` | 网关、watcher | 整文件 > 20 MB，发头前拒绝 | 缺图不阻塞文本 |

- **未识别的 `code`（A-3 条件，规范性）**：app 收到 503 且 `code` 不在上表 → 一律按 `watcher_unavailable` 处理（写请求用同一 `client_ref` 回读或重放）；收到其他非 2xx 且 `code` 不在上表 → 显示错误，不自动重试、不换 `client_ref`。401 仍是唯一触发"交易服务连接失效"的状态。
- **`message` 的来源（R8，规范性）**：watcher 的 `message` 只取自固定文案表（按 `code`，必要时按 `details.rule`），不得拼接异常文本（`err.message`）、输入值、文件路径或 SQL。`internal_error` 的 body 不含任何异常信息，堆栈只写经 `safe-log` 过滤的日志。`details` 只含本节与 §9.6、§9.9–§9.12 为该 `code` 定义的键。

### 9.6 上游响应映射（网关）

- 原样透传状态码（body 经下述过滤）的只有：`200`、`206`（媒体）、`400`（`code ≠ invalid_actor_headers`）、`404`（`code ≠ route_not_found`）、`409`、`416`，以及 `503` 且 `code ∈ {db_busy, media_too_large}`。
- 其余一律 503 `watcher_unavailable`：上游 401、403、405、413、400 `invalid_actor_headers` 与 404 `route_not_found`（网关自身缺陷或契约漂移）、其他 5xx、3xx（`follow_redirects=False`）、连接/读/总超时、JSON 行返回非 JSON 或超 8 MiB、错误体缺 `code`。`details.reason` ∈ `upstream_status`、`timeout`、`connect_error`、`redirect`、`malformed_response`、`response_too_large`，可带 `upstream_status`，**不回显上游 body**。上游 401 绝不透传。
- 错误体（R8）：从上游只取 `code`/`message`/`details`，丢弃其他键（含 `error`），加网关 `request_id`。
  - `code` 必须是匹配 `^[a-z][a-z0-9_]{0,63}$` 的字符串，否则按"错误体缺 `code`"处理（503 `watcher_unavailable`，`details.reason=malformed_response`）。
  - `message` 必须是不超过 200 个字符、不含控制字符的字符串，否则替换为 `code` 本身。
  - `details` 必须是 JSON object，否则删除。对**整个** `details` 递归删除键名命中 `secret_key_pattern` 的键（任意深度，数组里的对象同样处理；删除而非掩码），不只 `details.current`。
- 成功体过滤：按行 `response.omit` 删除键（数组响应对每个元素生效）；再对整个 JSON 递归删除键名命中 `secret_key_pattern` 的键（删除而非掩码）。
- 过滤用的正则按 YAML `regex_dialect` 编译（`secret_key_pattern.pattern` + `flags`；Python 用 `re.search(pattern, key, re.IGNORECASE)`），与 watcher 的 JS 编译结果同义（R5）。
- 响应头：只回 YAML `response_headers.<行的 headers>` 中的头，并删除 `Connection` 声明的逐跳头。JSON 行由网关设置 `Cache-Control: no-store`。配置读行（accounts/channels/risks GET）回传 watcher 的 `X-Config-Revision`。

### 9.7 async、准入与超时（P1-12）

- 网关路由全部 `async def`，使用异步 HTTP 客户端与异步流；禁止同步 `def` 转发、禁止同步 handler 等待 future、禁止在线程池内获取准入。
- 准入：每个 worker 在事件循环上非阻塞 try-acquire 对应预算的信号量，取不到立即 503 `gateway_busy`；在 `finally` 中关闭上游并释放槽（含客户端取消）。准入发生在 §9.3 的 G1–G5 之后、G7 与上游请求之前（G6）。
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
  - 校验通过的 actor、role、指纹只经 `req.watcherAuth`（§9.2）交给 handler，handler 不再读这两个头。
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
- 成功验证 = HTTP 200 ∧ `schema_version == "watcher-config-snapshot.v1"`（否则 `schema_mismatch`）∧ 重算 `content_sha256` 相等 ∧ 字段白名单与类型合规 ∧ 非"同 revision 异摘要"（后三者不满足记 `invalid`）。无效快照永不替换已缓存的有效快照。`revision` 小于缓存值时接受并记 `snapshot_revision_regressed` 告警（真库回滚场景）。
- 状态（每 worker 独立；WGW-1.0.1 按 R1 修订）：
  - `cold`：从未成功验证过（启动预热失败时 worker 仍就绪，但处于 `cold`）。
  - `fresh`：距最近一次成功验证 `age ≤ max_age`，且不处于下面两个锁存态。**不设宽限态**。
  - `expired`：`age > max_age`，且不处于锁存态。
  - `invalid`（锁存）：一次完成的刷新拿到 HTTP 200，但未通过成功验证（`schema_mismatch`，或摘要不符、白名单/类型不合规、同 revision 异摘要）。
  - `unauthorized`（锁存）：一次完成的刷新拿到 HTTP 401 或 403。403 与 401 同类处理（例如 snapshot token 被识别为其他身份，说明信任关系已变）。
  - **锁存规则**：进入 `invalid` 或 `unauthorized` 后，只有下一次**成功验证**才能离开（回到 `fresh`）。其间的超时、连接失败、5xx 都不解除锁存；即使已缓存的有效快照 `age ≤ max_age`，也不回到 `fresh`（R1：不得继续使用旧快照）。两种锁存条件先后出现时，状态取最近一次。
  - **不锁存的失败**：超时、连接失败、5xx、3xx、401/403 以外的 4xx（例如 watcher 回滚到没有该端点的版本而得到 404）、非 JSON、超过 4 MiB。这些只记 `last_refresh_error`，状态按 age 演进（R1："fresh 的定义不变"），刷新失败只让 age 增长。
  - `last_refresh_error ∈ {null, timeout, unavailable, unauthorized, invalid, schema_mismatch}` 保留为细分原因；对外 `snapshot_state ∈ {cold, fresh, expired, invalid, unauthorized}`，锁存态优先于按 age 得出的状态。
- 使用：`open_position`（含 dry_run）在状态 ≠ `fresh` 时一律拒绝，HTTP 503、body 保持既有形状 `{"detail": "snapshot_unavailable"}`。`invalid` 与 `unauthorized` 一出现就立即拒绝，不等 60 秒（R1）。非开仓路径不引入快照依赖（回归测试锁定）。展示类读取若使用快照，须带 `stale: true`。
- **消费语义（C-0，审查 💭-2）**：快照路径与旧 SQLite reader（`_load_channel_risk_route` `read_api.py:6311-6420`、`_symbol_risk_ratio` `:8492-8519`）对同一份数据必须得出同一类结果，影子对照（B/C）以此为预言：
  - 风险取值顺序不变：`risks[symbol]` → 执行账号（在 accounts 中按 `execution_account_id` 查找）的 `default_risk` → 环境变量 `OPERATOR_DEFAULT_RISK_RATIO`（缺省 `"0.01"`）；结果须在 `(0, 0.1]`，否则 503 `{"detail": "account risk ratio configuration is unavailable or invalid"}`。
  - 执行账号存在但 `default_risk` 为 `null` → 同一个 503（与旧 reader `float(None)` 的结果一致，`:8509-8517`），**不**回落到环境变量默认值；只有执行账号不存在时才用环境变量默认值。
  - 已知的有意差异：旧 reader 宽松比较 `account_type`（`lower(trim())`，`:6370`）与启用值（`1/active/enabled/true`，`:6298-6308`）；快照一侧由 watcher 严格校验（§9.10），任一行不合规会让整份快照 `snapshot_invalid`，而不是逐行宽松放行。打开 `WATCHER_CONFIG_SNAPSHOT_ENABLED` 的前置条件是 O-0 数据基线证明现存所有行通过 §9.10 的服务端校验，并列出：非精确 `main`/`subaccount` 的 `account_type`、非 0/1 的 `is_enabled`、依赖旧 `enabled`/`status` 列的行、`default_risk_ratio` 为 NULL 或越出 `(0, 0.1]` 的行、`risk_ratio` 越界的行、不匹配 `path_params.account_id.gateway_pattern` 的 `account_id`（这些账号在 app 中无法编辑，审查 💭-3）。B/C 对照报告把这些逐条列为预期差异并核实。
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
- **客户端取用 revision 的规则**（站点与 app 相同）：按资源（accounts、channels、risks）各存一份 revision，取自该资源最近一次 GET 的 `X-Config-Revision`；写某资源时用它作 `expected_revision`，不得用其他资源读到的更大值（那会让用户没看到的账号改动被覆盖）。写成功后用响应里的 `revision` 更新该资源的值，并重新 GET 该资源列表。因为 `config_revision` 是全局的，其他资源的改动也会让本资源的写得到 409，这是预期行为。
- **部分更新（R7，审查 🟡-9，规范性）**：
  - PUT（两种身份）只对**本次提交的键**做字段级校验（类型、范围、枚举、格式）。未提交的键沿用存量值；存量值越界（例如 `default_risk_ratio` 大于 0.1 或为 NULL）**不阻止**本次修改，例如只改 `is_enabled`。browser 的 `api_key`/`api_secret` 为空串视同未提交。
  - 跨行约束只在它的输入键被提交时评估，并按合并后的行判定：执行账号唯一（提交了 `execution_account_id`）；主子账号结构，即父存在、父为 main、主子环境一致、改为 subaccount 时没有子账号（提交了 `account_type`、`parent_account_id` 或 browser 的 `is_testnet` 之一）。
  - 这是对现状的有意改变：现行 PUT 对合并后的整行重算（`trading-api.js:496-600`）；W-0 按本条改。
  - SQLite 自身的 CHECK 与 UNIQUE 约束照常生效（`trading-api.js:55-74`、`:199-200`）。违反时返回 409 `constraint_conflict`（UNIQUE）或 400 `validation_failed`（CHECK），不得返回 500。
  - DELETE、POST 不受本条影响（POST 是整行创建，校验全部提交的键）。
  - 客户端：app 只提交用户改动过的键（外加 `expected_revision` 与 `client_ref`）。站点沿用整表单提交，因此在站点编辑一个存量越界的账号时，须在同一次保存中改正该值；这是站点表单的行为，不是契约例外。
- **browser 密钥**：`br.account.put` 的 `api_key`/`api_secret` 为空 → 保留原值（现状 `trading-api.js:581-590`）；值等于当前掩码或匹配 `^\*{4}$`、`^.{4}\.\.\..{4}$` → 400 `masked_value_rejected`。gateway 身份对任何秘密字段 → 400 `secret_field_rejected`，DB 不变（watcher 第二层，不依赖网关）。
- **保留期**：`config_audit` 至少保留 30 天；幂等保证只在保留期内成立（过期后重放按新请求处理，conditional 行会因版本不符得到 409）。
- **写入者**：watcher 是三张配置表唯一写入者（D1）；`db_manager.py` 写命令停用或改走本服务。任何绕过者会造成"同 revision 异摘要"，被 §9.11 判为 `invalid`。

### 9.13 兼容性声明

- 既有 `/v1/*`、`/api/system/snapshot` 等端点的字段、状态码、错误体（`{"detail": …}`）一律不变；错 token 仍 403。`/v1/watcher/*` 是新增前缀，统一错误体只作用于此前缀。
- `/v1/operator/orders` 只增：新 `checks` 项 `config_snapshot`；快照不可用时 503 `{"detail":"snapshot_unavailable"}`（仅开关打开后、仅开仓路径）。
- watcher 站点：GET 响应形状不变（数组）；新增 `X-Config-Revision` 头；错误体新增 `code/message/details` 并保留 `error`；写请求新增必填 `client_ref` 与 `expected_revision`；风险比例校验从"非负"收紧为 `(0, 0.1]`（计划 §6.1）。
- **站点 JS 改动清单**（W-0 与站点后端改动同批交付，逐项作为验收；审查 🟡-8）：
  1. `api()`（`public/index.html:910-929`）读取 accounts/channels/risks 的 GET 响应头 `X-Config-Revision`，按资源各存一份（§9.12 客户端规则）。
  2. 所有写请求带 `client_ref`：用户每次确认生成一个新值，同一次操作的重试复用它。配置写（账号 POST/PUT/DELETE、路由 POST/DELETE、风险 POST/DELETE）另带 `expected_revision`。
  3. 账号 PUT 的 payload **不再包含 `account_id`**（现状 `:1513-1524` 会发送，而 `br.account.put` 的 deny 含它，会得到 400）；账号 POST 照常发送 `account_id`。
  4. 编辑账号时密钥输入框保持为空，**不回填** GET 返回的掩码值（现状 `:1383-1384` 已清空，改造后锁为回归测试）；空值表示"不改密钥"，回填掩码会触发 400 `masked_value_rejected`。
  5. DELETE（账号 `:1584`、路由 `:1745`、风险 `:1806`、价格提醒）改为带 JSON body `{client_ref, expected_revision?}` 与 `Content-Type: application/json`；风险删除（`:1806`）现在直接用 `fetch`，须改为走 `api()`。
  6. `groups`、`disconnect`、`reconnect`、价格提醒的写请求带 `client_ref`。
  7. 409 `revision_conflict`：提示"配置已被修改"，重新 GET 并展示最新数据，用户重新确认时用新的 `client_ref`；503 `db_busy` 用同一个 `client_ref` 重试。
  8. 错误展示继续读 `error`（等于 `message`）。
- app 不新增存储键与设置项；watcher 服务层独立实例，其写禁用不传给交易 `TradingApi`。

### 9.14 路由真源生成与校验规格（C-1 实现，本节只定规格）

**原则（R9）**：运行时不解析 YAML。路由真源在开发期由生成器生成**已提交**的 JS 与 Python 产物，watcher 只加载 JS 产物，控制面只加载 Python 产物；校验脚本判定"重新生成 == 已提交"。生成器与校验脚本只在开发环境（本机、CI）运行。

**入口**（全部位于 `scripts/contracts/`）：

| 文件 | 作用 |
|---|---|
| `scripts/contracts/watcher_gateway_routes_lib.py` | 共享实现：加载 YAML、自洽检查、规范化、渲染三份生成物（纯函数，不写文件） |
| `scripts/contracts/gen_watcher_gateway_routes.py --phase-max P<n>` | 生成器：调用 lib 渲染，覆盖写出 §9.14.3 的三份生成物；不做其他事 |
| `scripts/contracts/check_watcher_gateway_routes.py [--self-check]` | 校验：`--self-check` 只跑 9.14.1；缺省模式 = 9.14.1 + 9.14.4 第 1 项（以已提交生成物中的 `phase_max` 在内存重新生成并逐字节比对） |

**运行环境（R9，审查 🟡-11）**：系统 `python3`（≥ 3.10）加 PyYAML，另需 `node`（S-20 的 JS 编译与同义检查）。安装方式：`python3 -m pip install --user pyyaml` 或系统包管理器。**不得**把 PyYAML 加入 `.venv-arch`、控制面生产 venv 或 watcher `package.json`；watcher 与 operator-query 运行时不 import yaml、不打开 YAML 文件。PyYAML import 失败或找不到 `node` 时，脚本打印 `ENVIRONMENT_ERROR <原因>` 并以退出码 2 结束，不得跳过检查，也不得输出 `ROUTES_DIFF_EMPTY`。

**输出与退出码**：成功时最后一行为 `ROUTES_DIFF_EMPTY rows=<n> yaml_sha256=<hex> phase_max=<Pn>`，退出码 0；有差异时逐条列出，退出码 1；环境错误退出码 2。任何检查比较了 0 行即判失败（退出码 1）。所有测试夹具 token 由脚本随机生成，不读取任何真实环境值。

**9.14.1 真源自洽检查（S-01..S-20；WGW-1.0.1 修订 S-01、S-10、S-18，新增 S-20）**

| 编号 | 检查 |
|---|---|
| S-01 | `yaml.safe_load` 成功；顶层键恰为 `schema_version, contract_version, frozen_at, amended_at, source_plan, paths, roles, identities, actor_headers, regex_dialect, secret_fields, secret_key_pattern, budgets, path_params, query_params, body_fields, response_headers, media_request_headers, invariants, never_allowed, gateway_excluded, routes`；`schema_version == watcher-gateway-routes.v1`；`contract_version` 形如 `WGW-<主>.<次>[.<勘误>]`；`frozen_at`、`amended_at` 为 ISO 日期 |
| S-02 | 每行键集合恰为 `id, phase, identity, method, outer_path, inner_path, roles, query, body, response, budget, write`；`body` 含 `allow/deny/required`（可选 `field_overrides`）；`response` 含 `omit/mask/headers`（snapshot 行另含 `fields`） |
| S-03 | `id` 唯一，形如 `^(gw\|snap\|br)\.[a-z0-9_]+\.(get\|head\|post\|put\|delete)$`，前缀与 identity 对应、后缀与 method 对应 |
| S-04 | identity ∈ {gateway, snapshot, browser}，三者各至少一行 |
| S-05 | phase ∈ {P0, P1, P2, P3}；snapshot 与 browser 行为 P0 |
| S-06 | method ∈ {GET, HEAD, POST, PUT, DELETE}；`(identity, method, inner_path)` 唯一；gateway `(method, outer_path)` 唯一 |
| S-07 | gateway 行：outer_path 以 `paths.app_outer_prefix + "/"` 开头；设 `rest = outer_path[len(prefix):]`，若 `rest` 以 `/media/` 开头则 `inner_path == rest`，否则 `inner_path == "/api" + rest`；两侧路径参数名与顺序一致 |
| S-08 | 非 gateway 行：`outer_path`、`roles` 为 null；browser 行 `budget` 为 null；snapshot 行 `budget == snapshot` |
| S-09 | gateway GET/HEAD 行 `roles` 与 `roles.all_readers` 集合相等；POST/PUT/DELETE 行 `roles == [risk_admin]` |
| S-10 | `secret_key_pattern` 是恰含 `pattern`、`flags` 两键的映射；gateway 行：`secret_fields ⊆ body.deny`；`body.allow ∩ secret_fields = ∅`；`body.allow` 与 `query` 的每个名字都不被 `secret_key_pattern`（按 `flags` 编译，search 语义）命中；所有行 `allow ∩ deny = ∅`、`required ⊆ allow` |
| S-11 | `body.allow`（经 `field_overrides`）全部定义在 `body_fields`；gateway 行不得引用 `secret: true` 或 `browser_only: true` 的定义；`query` 全部定义在 `query_params`；每个 `{param}` 在 `path_params` 中有该身份的非 null pattern |
| S-12 | GET/HEAD 行 `body.allow == []` 且 `write == null`；inner_path 命中 `never_allowed` 的 browser 凭据面写行（`/api/config`、`/api/login/*`）`write == null`（不审计、不带 client_ref，站点登录流程不变）；其余写行 `write` 非 null、`client_ref ∈ required`；`conditional` → `expected_revision ∈ required`；`bumps_revision` → `conditional`；`idempotency ∈ {transactional, reentrant}` |
| S-13 | HEAD 只出现在 inner_path 以 `/media/` 开头的行，且每个 HEAD 行都有同身份同路径的 GET 行；媒体行 `response.headers == media` |
| S-14 | gateway 媒体行 `budget == media`，其余 gateway 行 `budget == config` |
| S-15 | `response.headers` 是 `response_headers` 的键；`json_config_read` 只用于 accounts/channels/risks 的 GET |
| S-16 | gateway 行 `response.mask == []`（网关只删不掩）；gateway 与 browser 的 accounts 读写行分别 `omit ⊇` / `mask ⊇ [api_key, api_secret]` |
| S-17 | snapshot 恰一行 `GET /api/trading/config-snapshot`，`response.fields` 与 §9.10 白名单逐项相等 |
| S-18 | `never_allowed` 每项 `methods == "*"`（R6）；没有 gateway 行的 `inner_path` 命中 `never_allowed`（**与方法无关**）；没有 gateway 行的 `(method, inner_path)` 命中 `gateway_excluded`（`methods` 为 `"*"` 时与方法无关）。路径匹配：`*` 匹配一个或多个段，`{x}` 匹配一个段 |
| S-19 | 覆盖预言（独立于 YAML 手写在脚本里，来源计划 §3）：gateway `(method, outer_path, phase)` 集合恰为 9.14.2 的 24 行 |
| S-20 | 正则方言（R5）。对象为 `path_params.*.*_pattern`、`query_params.*.pattern`、`body_fields.*.pattern`/`gateway_pattern`、`actor_headers.*_pattern` 与 `secret_key_pattern.pattern`：(a) 不含 `(?`，`(?:` 除外；(b) 不含 `\d \w \s \b \D \W \S \B`；(c) 字符类之外没有未转义的 `.`；(d) `secret_key_pattern.flags` 的每个字符属于 `regex_dialect.allowed_flags`；(e) 除 `secret_key_pattern` 外全部以 `^` 开头、以 `$` 结尾；(f) Python `re.compile(p, flags)` 与 node `new RegExp(p, 'u' + flags)` 都能编译；(g) 同义探针：对固定探针集（至少含 `""`、`"a"`、`"A"`、`"0"`、`"abc\n"`、`"\n"`、`"a b"`、`"%2F"`、`".."`、`"ſ"`（U+017F）、`"K"`（U+212A）、`"ſession"`、`"toKen"`（K 为 U+212A）、一个星平面字符、`"api_key"`、`"API_KEY"`、`"rapid_x"`，外加 YAML 中出现的全部枚举值）逐一比较 Python（锚定模式用 `re.fullmatch`，`secret_key_pattern` 用 `re.search`）与 node（`.test`）的结果，任一不一致即失败 |

**9.14.2 覆盖预言（计划 §3 → gateway 24 行）**

| phase | 行 |
|---|---|
| P0（1） | GET `/v1/watcher/status` |
| P1（8） | GET `trading/messages`；GET `trading/briefings`；GET、HEAD `media/{filename}`；POST `disconnect`；POST `reconnect`；GET `trading/orders`；GET `trading/orders/active` |
| P2（11） | GET `dialogs`；POST `groups`；GET `trading/accounts`；PUT、DELETE `trading/accounts/{account_id}`；GET、POST `trading/channels`；DELETE `trading/channels/{channel_id}`；GET、POST `trading/risks`；DELETE `trading/risks/{symbol}` |
| P3（4） | GET、POST `price-alerts`；DELETE `price-alerts/{alert_id}`；GET `price-monitor/status` |

（均省略前缀 `/v1/watcher/`；另有 snapshot 1 行、browser 38 行。）

**9.14.3 生成物**（R9；`gen` 写出并提交；`check` 在内存重新生成并逐字节比对）

| 生成物 | 消费方 |
|---|---|
| `bridge/services/telegram-watcher/lib/generated/gateway-routes.js` | watcher 身份与请求校验中间件（§9.2 W1–W7） |
| `services/control-plane/api/generated/watcher_gateway_routes.py`，外加手写的空文件 `services/control-plane/api/generated/__init__.py` | operator-query 网关（§9.3–§9.9）；import 名 `generated.watcher_gateway_routes`（与 `read_api.py:100` 的兄弟模块 import 方式一致） |
| `contracts/generated/caddy-watcher-gateway-paths.txt` | O-0 核对 Caddy 逐路径配置 |

- **payload**：两份代码生成物内嵌**同一个** payload。它由 YAML 顶层键中除 `invariants`、`never_allowed`、`gateway_excluded` 之外的全部内容组成，其中 `routes` = snapshot 行 + browser 行 + `phase ≤ phase_max` 的 gateway 行，按 `(identity, inner_path, method)` 排序；另加 `_meta: {contract_version, schema_version, yaml_sha256, phase_max, generator: "scripts/contracts/gen_watcher_gateway_routes.py"}`，其中 `yaml_sha256` 是 YAML 文件字节的 SHA-256。
- **规范化**：`payload_text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`；以 `json.dumps(payload_text)` 得到的字符串字面量嵌入两份文件（ASCII-only 的 JSON 字符串字面量同时是合法的 Python 与 JS 字符串字面量），因此两份文件里的 payload 字面量逐字节相同。`payload_sha256 = sha256(payload_text 的 UTF-8)`。
- **文件形状**：首行注释 `GENERATED by scripts/contracts/gen_watcher_gateway_routes.py from contracts/watcher-gateway-routes.yaml — DO NOT EDIT`；LF 换行；结尾一个换行。除解析 payload、核对摘要、冻结、编译正则外不含任何逻辑。
  - JS（CommonJS）导出 `{ PAYLOAD, PAYLOAD_SHA256, SECRET_KEY_REGEX }`：`PAYLOAD` 为 `JSON.parse` 后深冻结的对象（键沿用 YAML 的 snake_case）；`SECRET_KEY_REGEX = new RegExp(pattern, 'u' + flags)`（按 `regex_dialect`，不带 `g`/`y`）。
  - Python 导出 `PAYLOAD`、`PAYLOAD_SHA256`、`SECRET_KEY_REGEX = re.compile(pattern, re.IGNORECASE)`（按 `flags` 映射）；只 import `json`、`hashlib`、`re`。
  - 两者在加载时重算 payload 摘要，与 `PAYLOAD_SHA256` 不等即抛错（防手改）。其他路径参数、query、body 正则由消费方按 `regex_dialect` 编译：JS 恒加 `u` 标志，Python 用 `re.fullmatch`；pattern 只作用于已确认是字符串的值（先类型后正则）。
- **Caddy 清单**：头部三行注释 `# _generated_from`、`# _yaml_sha256`、`# _phase_max`；之后每个唯一外部路径一行（`/m` + outer_path，`{param}` → `*`，后接空格分隔、字典序的方法列表），整体字典序。
- **打包**（审查补充，W-0 / C-1 / O-0 验收）：watcher 镜像白名单 `WATCHER_RUNTIME_RELATIVE_PATHS`（`scripts/build_immutable_watcher_image.py:34`）必须加入 `lib/generated/gateway-routes.js`；控制面发布若把 `api/*.py` 平铺到 `host/`（`scripts/make_account_stall_release.py:93-99`、`:217`），须保留子目录，打入 `host/generated/__init__.py` 与 `host/generated/watcher_gateway_routes.py`。部署门禁核对两份产物的 `_meta.yaml_sha256` 与 `_meta.phase_max` 等于本次发布的已审定值。

**9.14.4 "生成路由与真源 diff 为空"的判定**（R9：按运行环境拆成三项，由传递性合成）

1. **生成物 diff**（`scripts/contracts/check_watcher_gateway_routes.py`，系统 `python3` + PyYAML）：三份生成物与内存重新生成的结果逐字节相同；三份的 `phase_max` 与 `yaml_sha256` 相同。
2. **控制面运行时 diff**（pytest，`.venv-arch`，**不** import yaml，只 import 已提交的 Python 生成物）：用夹具环境构造 `create_app("operator-query")`，枚举路径以 `/v1/watcher/` 开头的 `APIRoute`，得到 `{(method, path, name)}`，与 payload 中 gateway 行的 `{(method, outer_path, watcher_gateway__<id>)}` 对称差为空；每个 endpoint 满足 `inspect.iscoroutinefunction`、`route.dependant.body_params == []`、`route.body_field is None`（§9.3 G4）；同一集合不得出现在 `create_app("node-control")`、`create_app("event-ingest")`；operator-query 角色 app 上装有前缀中间件（探针 `/v1/watcher/status/` 得到 404 `route_not_found` 且无 `Location`）。
3. **watcher 运行时 diff**（node 测试，不读 YAML，只 require 已提交的 JS 生成物）：`node bridge/services/telegram-watcher/scripts/dump-route-table.js` 输出 watcher 实际加载的身份路由表（W-0 须让 `server.js` 只在 `require.main === module` 时 `listen`，以便无副作用加载 `app`），得到 `{(identity, method, inner_path)}`，与 payload 对称差为空；另枚举 Express 实际注册的 handler，每条 payload inner 路由都有 handler，且不存在 payload 之外的 `/api/*` 或 `/media/*` handler。
4. 第 1 项证明 YAML == 生成物，第 2、3 项证明生成物 == 运行时，三项全部为空且比较行数 > 0 → diff 为空。逐行属性（roles、query、body、write、budget）在集合相等后逐字段比较，任何不等都算非空。

**9.14.5 独立负例（T0-4，不从真源生成）**：百分号编码的斜杠与点（`%2F`、`%2e`）、双重编码（`%252F`）、尾斜杠、`//`、大小写（`/V1/Watcher/status`）、重复 query、未列 query、OPTIONS、非媒体 HEAD、未注册 404、未列方法 405；经网关遍历矩阵外请求时 watcher 请求计数不变；用 `gateway` token 直连 watcher 遍历矩阵外端点，第二层同样拒绝；`browser` 调 snapshot、`snapshot` 调网关写行被拒。

### 9.15 裁决状态、召回待办与执行期注意事项

**已裁决**（Planner 2026-09-26，依据审查 wac-001 复核；条件已写进正文）：

| # | 内容 | 裁决 | 条件落点 |
|---|---|---|---|
| A-1 | `/healthz` 要求 `browser` 代理凭证，容器健康检查带该头 | 接受（有条件） | §9.2：node 脚本在脚本内读 `process.env`，不经 compose `$VAR` 插值，不打印响应 |
| A-2 | 网关每 worker 8 槽 = config 4 + media 4；snapshot 独立 1 槽不计入 | 接受 | §9.7、YAML `budgets` |
| A-3 | watcher 503 `db_busy` / `media_too_large` 透传给 app（对"上游 5xx 一律 `watcher_unavailable`"的收窄例外） | 接受（有条件） | §9.5：app 把未识别的 503 `code` 一律当作 `watcher_unavailable`；§9.6：只透传 `code`/`message`/`details` 并按 R8 过滤 |
| A-4 | `default_risk_ratio` 与品种风险同样收紧到 `(0, 0.1]`（`read_api.py:8510-8512`） | 接受（有条件） | §9.12 R7 只校验提交的键；§9.11 O-0 数据基线列出存量越界或 NULL 的行 |
| A-5 | DELETE 的 `client_ref`/`expected_revision` 放 JSON body；`config_audit.token_fingerprint` | 接受 | §9.12 |
| A-6 | P3 价格提醒字段依赖计划 §7.1 表扩展 | 记录；召回待办见下 | — |
| A-7 | 空数组 `groups` 合法（停止监听全部群组） | 接受（有条件） | app 必须二次确认并写明"将停止监听全部群组"；T2-5 覆盖空选路径；app 截断到 100 条时不丢已选（审查 💭-6，A-2 验收） |
| A-8 | 快照 `invalid` / `401` 何时拒绝开仓 | R1：立即拒绝 | §9.11 |

**召回待办**（A-3 开工前由 Planner 召回 Architect 处理；本版不定字段）：

- **A-6 / 审查 🟡-7：价格提醒删除按身份限定**。计划 §7.1 要求"列表/删除均按身份限定"。WGW-1.0.1 的 `gw.price_alert.delete` 只带 `client_ref`，app 可以按 id 删掉站点创建的 `source=watcher` 提醒。召回时要定下：gateway 删除时 watcher 校验该行 `source=v3`；body 是否带 `account_id`、`position_ref`、`environment` 并与行内值比对（不一致按 404 处理）；`gw.price_alerts.get` 是否默认按 `source=v3` 过滤。P3 受 `phase_max` 门控，当前生成物不注册这些行，因此不阻塞 P0–P2。

**执行期注意事项**（不改契约，由对应任务书写进验收）：

- 审查 💭-1：`secret_key_pattern` 未锚定，按"键名包含"判定，`api[_-]?id` 会命中 `rapid_*` 之类的键。误伤只会多拒、多删（安全方向）。将来加列时注意命名。
- 审查 💭-7：review P1-16 提到的"全局/每 token 带宽预算"，计划 §2.1 未采纳，本契约也不写，按计划为准。
- 审查 💭-6：`groups` 没有条件写，站点与 app 同时编辑会互相覆盖（计划 §2.3 已接受：可重入，以回读为准）。
- 打包白名单与部署门禁见 §9.14.3（watcher 镜像、控制面平铺发布）。
- R5 的跨语言差异：Python `re.IGNORECASE` 与 JS `iu` 都做 Unicode 简单大小写折叠；万一两边对某个键名结论不同，只会让其中一层多拒或多删（安全方向）。S-20(g) 用固定探针锁定当前一致性。

### 9.16 勘误记录

**WGW-1.0 → WGW-1.0.1（2026-09-26）**。来源：Planner 裁定 R1–R9（附 A-1..A-7 条件）、审查报告 `docs/agent-team/reviews/wac-001.md`（11 条 🟡、7 条 💭）。WGW-1.0 尚无实现，本次改动不涉及已发布接口；§1–§8 未改动。

| # | 来源 | 改动 | 位置 |
|---|---|---|---|
| E-01 | R1 / 🟡-1（A-8） | 快照新增锁存态 `invalid`、`unauthorized`（401 与 403），出现即拒绝开仓，只有下一次成功验证才能解除；超时、5xx 等不锁存，`fresh` 定义不变；`schema_mismatch` 归入 `invalid` | §9.11 |
| E-02 | R2 / 🟡-2 | "已配置"定义；空 bearer（含 Node/uvicorn 剥掉尾随空白后的 `Bearer`）一律 401 且不进入比较；未配置的 `*_PREVIOUS` 永不参与匹配；已配置值（含 `_PREVIOUS`）须 ≥ 32 字节、可打印 ASCII、两两互异；剥离规则与 `principal.py:111-115` 相同；摘要恒定时间比较 | §9.2、§9.3 |
| E-03 | R3 / 🟡-3 | 网关处理顺序 G1–G8 写死；G2/G3 未通过不读 body；endpoint 不得声明 Body 参数；watcher 顺序 W1–W8 同时写全 | §9.3、§9.2、§9.4、§9.7 |
| E-04 | R4 / 🟡-4 | 前缀中间件规范化：触发条件、自行返回 404/405、`scope.state` 行 id 断言、禁止全局 `redirect_slashes=False` 与 catch-all 路由、`/v1/accounts/` 回归测试；"实现提示"改为规范性要求 | §9.4 |
| E-05 | R5 / 🟡-5 | `secret_key_pattern` 改为 `{pattern, flags}`，去掉 `(?i)`；新增 `regex_dialect`（JS 恒加 `u`、Python 用 `re.fullmatch`、禁用构造、先类型后正则）；新增 S-20 | YAML `regex_dialect`、`secret_key_pattern`；§9.6、§9.14.1、§9.14.3 |
| E-06 | R6 / 🟡-6 | `never_allowed` 与方法无关（`methods: "*"`）；`gateway_excluded` 中 config-snapshot 也改为 `"*"`；S-18 同步 | YAML `never_allowed`、`gateway_excluded`、`invariants`；§9.14.1 |
| E-07 | R7 / 🟡-9 | 部分更新只校验提交的键；跨行约束只在其输入键被提交时评估；app 只提交改动的键 | §9.12 |
| E-08 | R8 / 🟡-10 | 网关对整个上游 `details` 递归删除秘密键；`code`/`message`/`details` 形状校验；watcher `message` 只取固定文案 | §9.5、§9.6 |
| E-09 | R9 / 🟡-11 | 运行时不解析 YAML；生成器、校验与共享实现放在 `scripts/contracts/`，用系统 `python3` + PyYAML，环境错误退出码 2；生成物改为 `lib/generated/gateway-routes.js` 与 `api/generated/watcher_gateway_routes.py`（同一 payload）；运行时 diff 拆到 `.venv-arch` pytest 与 node 测试；打包白名单与部署门禁 | §9.14 |
| E-10 | 🟡-7 | 价格提醒删除按身份限定：记为 A-6 召回待办，本版不定字段 | §9.15 |
| E-11 | 🟡-8 | 站点 JS 改动清单 8 项（PUT 不发 `account_id`、不回填掩码、`expected_revision` + `client_ref`、读 `X-Config-Revision`、DELETE 带 body 等）；客户端按资源保存 revision | §9.13、§9.12 |
| E-12 | 任务 wac-007 第 3 项 | W-0 两个子任务的接口：`req.watcherAuth = {identity, role, actor, tokenFingerprint}` 与 `req.watcherRoute`，只读、缺失即 500 | §9.2、§9.8 |
| E-13 | A-1..A-7 条件 | 条件写进正文；§9.15 由"待裁决"改为"裁决状态" | §9.2、§9.5、§9.15 |
| E-14 | 💭-2、💭-3 | C-0 消费语义（风险取值顺序、NULL `default_risk` → 503、宽松比较差异）与打开快照开关前的 O-0 数据基线 | §9.11 |
| E-15 | 💭-4 | `WATCHER_*` 与控制面 token 目录跨服务互异（O-0 发行侧 + operator-query 启动纵深检查） | §9.2 |
| E-16 | 💭-5 | 健康检查不经 compose 插值、不打印（A-1 条件） | §9.2 |
| E-17 | 💭-1、💭-6、💭-7 | 记为执行期注意事项 | §9.15 |
| E-18 | 自查 | watcher 侧 413 `payload_too_large`（W7）；body 大小检查先于解析；§9.1 补充事实（`principal.py`、`index.html`、镜像白名单、平铺发布、旧 reader 宽松比较） | §9.1、§9.4、§9.5 |

审查 11 条 🟡 的去向：🟡-1 → E-01；🟡-2 → E-02；🟡-3 → E-03；🟡-4 → E-04；🟡-5 → E-05；🟡-6 → E-06；🟡-7 → E-10；🟡-8 → E-11；🟡-9 → E-07；🟡-10 → E-08；🟡-11 → E-09。
