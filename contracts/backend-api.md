# backend-api 接缝契约（v1.1，已冻结 frozen）

> 状态：**frozen（2026-08-30，G0 裁决）**——已吸收 codex 落地前 review 的 19 条阻断项（各条标注 review #N）。
> G2 以本文件 schema 造 fixtures 开工；G1 实现必须逐字段一致，偏差走 block 仲裁。
> 完整语义见设计文档 v1.1 对应小节；本文件只写死接缝形状。
> §9（watcher 网关与配置快照）单独版本化：当前 **WGW-1.0.2**（2026-09-26 第二次勘误，勘误记录见 §9.16）；§1–§8 不受其影响。

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

> 状态：**frozen（WGW-1.0.2，2026-09-26）**——WGW-1.0（同日冻结、审查 wac-001 PASS、合入 `90e96d0`）的第二次勘误版（含合并前追补 F-09..F-12，仍为 WGW-1.0.2）。WGW-1.0.1 写入 Planner 裁定 R1–R9 与审查 wac-001 的 🟡-1..🟡-11；WGW-1.0.2 写入 `never_allowed` 进生成物、R10–R14、Caddy 单段语义（用户裁决 2026-09-26）与 A-6 价格提醒身份作用域（P3 只定契约、不启用），逐条见 §9.16。§9 的接口都还没有部署，两次勘误都不构成对已发布接口的破坏性变更。设计真相：`docs/plans/2026-09-11-watcher-to-app-migration.md` v0.6 §2.1–§2.3、§3、§4.2/§4.3（含附录 E 吸收的 v0.3 review P1-12..P1-19、P2-03）。
> **路由字段以 `contracts/watcher-gateway-routes.yaml` 为准**（method、outer/inner path、identity、roles、query、body allow/deny/required、response omit/mask/headers、phase、budget、write 元数据）；本节规定语义（鉴权顺序、错误码、事务顺序、状态机、媒体规则）。两者冲突时路由字段听 YAML、语义听本节；任一与计划冲突，停工并由 Planner 召回 Architect。
> 本节只新增 `/v1/watcher/*` 与 watcher 内部契约；§1–§8 既有端点的字段、状态码、错误体一律不变（见 §9.13）。
> **WGW-1.0.3（2026-09-28，勘误 G-01..G-09，见 §9.16）**：operator-query 的非移动入口（面板 `/v1/*` 等）不得把请求交给网关，新增 §9.14.6。本次只改 §9 的文字与 Caddy 片段的生成规则：路由真源 YAML **零改动**（其 `contract_version` 字段仍为 `WGW-1.0.2`，`yaml_sha256` 不变），两份代码生成物与 Caddy 清单逐字节不变，只有 Caddy 片段改为格式 v2（`watcher-gateway-caddy-snippet.v2`）。网关代码、角色 scope（§9.3 四角色读）、既有 `/v1` 端点都不变。

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
| （WGW-1.0.2 补，基线 `integ/watcher-app-crew` @ `efa422f`）生成器把 `never_allowed` 排除在 payload 之外；Caddy 清单把 `{param}` 换成 `*` | `scripts/contracts/watcher_gateway_routes_lib.py:235`、`:247` |
| （WGW-1.0.2 补）watcher 现行 never_allowed 判定只对 gateway 身份、排在路径规范化之后，清单取自手写路由表 | `bridge/services/telegram-watcher/lib/auth.js:110-117`、`:157-163`；`lib/generated/watcher-routes.js`（手写，头注 "Temporary handoff"） |
| （WGW-1.0.2 补）网关秘密键判定 = 含非 ASCII 或正则命中，请求拒绝与响应删除共用 | `services/control-plane/api/watcher_gateway.py:54-55`、`:62`、`:207`、`:229` |
| （WGW-1.0.2 补）`/v1/query/channel-route` 调 `_load_channel_risk_route`，开关打开时只读快照并要求 fresh；唯一调用方在开仓/加仓之前调用 | `services/control-plane/api/operator_queries.py:12-23`；`read_api.py:6323-6336`；`hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py:87`、`:202-203` |
| （WGW-1.0.2 补）`price_alerts` 现表无身份列；列表按 `symbol, target_price` 排序、不分来源；按 id 删除不校验归属；触发只置 `triggered = 1` | `price-monitor.js:47-66`、`:93-99`；`lib/trading-api.js:445-517` |
| （WGW-1.0.2 补）app 集成分支已由 `isConnectionInvalid` 统一判定连接失效（401 或 `invalid_token`） | app `apps/attention-android/src/services/watcherApi.ts:86-90`（`integ/watcher-app-crew`） |
| （WGW-1.0.2 补）校验脚本与两份测试写死 `phase_max = P2` | `scripts/contracts/check_watcher_gateway_routes.py:50-58`；`tests/control-plane/api/test_watcher_gateway.py:34`；`tests/control-plane/test_caddy_watcher_gateway_paths.py`（`EXPECTED_P2_LINES`、`P3_PATHS`） |
| （WGW-1.0.2 追补）watcher 现取路径用 `req.originalUrl.split("?")[0]`，不截 `#`，不检查请求目标形式 | `bridge/services/telegram-watcher/lib/auth.js:157` |
| （WGW-1.0.2 追补）Express 按 `parseurl(req).pathname` 路由：目标不以 `/` 开头，或含 `#`、`\t`、`\n`、`\f`、`\r`、空格、U+00A0、U+FEFF 时，改用 Node 旧版 `url.parse`，后者会把第一个 `?`/`#` 之前的 `\` 换成 `/`，并从绝对形式中取出路径。本机实测（Node v24.13.0，parseurl 同版本）：`/api\config#x` → `/api/config`，`http://x/api/config` → `/api/config`，`/api/status#x` → `/api/status`，`*` → `*`；Node 对 `GET host:80` 直接回 400，`CONNECT` 走 `connect` 事件而不进入 Express | `bridge/services/telegram-watcher/node_modules/parseurl/index.js:95-125` |
| （WGW-1.0.2 追补）网关现行触发与 raw path 判定直接用 `scope["path"]`/`scope["raw_path"]` 整串，生成物导入失败时触发即 503 `gateway_disabled`（先于路由匹配与认证） | `services/control-plane/api/watcher_gateway.py:21-28`、`:89-101` |
| （WGW-1.0.3 补，基线 `integ/watcher-app-crew` @ `6e80efb`）面板的 Caddy `handle /v1/*` 把 `/v1/*` 原样转给 operator-query（8183），并无条件以 `header_up Authorization "Bearer {env.SYSTEM_OBSERVER_TOKEN}"` 覆盖来访凭据；仓库两份文档记载生产如此，O-0 仿生产夹具同形 | `docs/reports/2026-09-15-trading-reliability-local-implementation.md:47`；`docs/operations/trading-reliability-cutover.md:7`；`scripts/ops/o0/tests/fixtures/caddy/Caddyfile.prodlike.in:20-24` |
| （WGW-1.0.3 补）网关现行实现：触发条件与 G1 的 raw path 规则（`%`、`//`、点段、尾斜杠、`#`、不以 `/` 开头）在路由匹配之前；`resolve_principal` 在 G1 之后才调用。网关看到的只有 ASGI scope 与请求头，经 Caddy `/m` 入口剥前缀到达的请求与经面板 `/v1/*` 原样到达的请求，路径与头形状相同，无从区分 | `watcher_gateway.py:187-189`、`:198-203`、`:389` |
| （WGW-1.0.3 补）面板前端源码不调用 `/v1/watcher/*`（`git grep 'v1/watcher' -- bridge/apps/dashboard` 零命中）；计划要求 `system_observer` 可读网关 status | `bridge/apps/dashboard/`；计划 §2.1（`:80`）、§4 T0 验收（`:159`）；review v0.3 P1-13（`review-v0.3.md:50`：`system_observer` 即面板读 `/v1/accounts` 的角色） |
| （WGW-1.0.3 补）Caddy `path_regexp` 对**解码并 clean 之后**的路径匹配（`cleanPath(normalizeWindowsPath(r.URL.Path))`）；`reverse_proxy` 在没有改写时转发原始路径（不 clean） | Caddy v2.10.2 `modules/caddyhttp/matchers.go` `MatchPathRE.MatchWithError`；本机实测见 §9.14.6 |
| （WGW-1.0.3 补）O-0 现行 verify 的转发检查与探针只覆盖 `/m/v1/watcher` 前缀；O-3 只测公网 `/m/v1/watcher/status` | `scripts/ops/o0/o0_caddy_watcher_routes.py:1214-1252`；`docs/agent-team/release/o0-runbook-deploy.md:197`；审查 `docs/agent-team/reviews/wac-094.md` 🟡-1（g18、g19） |

### 9.2 拓扑与三种服务端身份

```
app ──baseUrl(/m)+/v1/watcher/*──▶ Caddy(strip /m 一次) ──▶ operator-query 网关(async) ──Bearer gateway──▶ watcher 127.0.0.1:9090
浏览器 ──basicauth──▶ Caddy(清除外来 X-Watcher-* → 注入 X-Watcher-Proxy-Auth) ──▶ watcher
operator-query 快照 reader ──Bearer snapshot──▶ watcher GET /api/trading/config-snapshot（内网，不经网关）
面板等非移动入口 ──(/v1/*，Caddy 可能替调用方注入凭据)──▶ operator-query：/v1/watcher 前缀一律在 Caddy 回 404，不得到达网关（WGW-1.0.3，§9.14.6）
```

- （WGW-1.0.3）app 到网关的**唯一**公网入口是 Caddy 片段的 `/m/v1/watcher/*` 逐路径处理器，它保留调用方自己的 `Authorization`。凡是替调用方注入或覆盖凭据的代理入口（面板 `/v1/*` 注入 `SYSTEM_OBSERVER_TOKEN` 即是），都不得把网关触发空间内的路径交给 operator-query；规则、检查与理由见 §9.14.6。

- **凭据与环境变量**（名字即契约，值由 O-0 发行侧生成，两两互异，app 与用户不接触）：

  | 身份 | watcher 侧（校验用，当前+上一值） | 持有方 | 出示方式 |
  |---|---|---|---|
  | `gateway` | `WATCHER_GATEWAY_TOKEN`、`WATCHER_GATEWAY_TOKEN_PREVIOUS` | operator-query：`WATCHER_GATEWAY_TOKEN` | `Authorization: Bearer <token>` |
  | `snapshot` | `WATCHER_SNAPSHOT_TOKEN`、`WATCHER_SNAPSHOT_TOKEN_PREVIOUS` | operator-query：`WATCHER_SNAPSHOT_TOKEN` | `Authorization: Bearer <token>` |
  | `browser` | `WATCHER_BROWSER_PROXY_TOKEN`、`WATCHER_BROWSER_PROXY_TOKEN_PREVIOUS` | Caddy | `X-Watcher-Proxy-Auth: <token>` |

- **"已配置"的定义（R2）**：环境变量存在且值不是空串。未设置或为空串的 `*_TOKEN_PREVIOUS` 视为未配置，**永不参与匹配**。
- **watcher 启动**（R2）：三个当前值必须已配置；每个已配置值（含已配置的 `*_PREVIOUS`）必须匹配 `^[\x21-\x7E]{32,}$`（可打印 ASCII、无空白、≥ 32 字节）；全部已配置值（最多六个）两两互异。任一不满足 → 进程非零退出，错误信息只列变量名，不打印值。watcher 环境里不放任何控制面 reader token。
- **跨服务互异**（审查 💭-4）：O-0 发行侧校验 `WATCHER_*` 六个值与控制面 token 目录（`configured_token_values`，`principal.py:67-97`：四个 reader token、signal token、节点 token 等）两两互异。operator-query 启动时再做一次纵深检查：`WATCHER_GATEWAY_TOKEN` 或 `WATCHER_SNAPSHOT_TOKEN` 与 `configured_token_values` 任一相等 → 视同未配置（网关 503 `gateway_disabled`；快照开关打开时启动失败）并告警，告警只写变量名。
- **operator-query 启动**：缺 `WATCHER_GATEWAY_TOKEN` → 网关路由照常注册，但一律 503 `gateway_disabled` 并打告警日志；**不得**让交易端点启动失败。（WGW-1.0.2 写入 R11）加载 Python 生成物失败（缺件、摘要自检不符、导入异常、§9.14.3 的加载期断言不成立）时同样处理：只停用 `/v1/watcher/*`（一律 503 `gateway_disabled`）并记录错误，operator-query 进程不退出，其他端点不受影响。（WGW-1.0.2 追补，F-11）这些失败统称"生成物停用态"，完整规则见 §9.14.3"网关加载期失败的处理"：网关模块自己捕获全部此类异常，`create_app()` 不得因此抛错；停用态下前缀中间件对触发的每个请求直接返回 503，先于 G1–G8。快照开关（§9.11）打开且缺 `WATCHER_SNAPSHOT_TOKEN` → 按计划 §2.1 启动失败（上线前配置校验锁定，开关默认关）。
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
  5. **never_allowed（WGW-1.0.2，规范性；WGW-1.0.2 追补 F-09 定义输入）**：身份不是 `browser`（即 `gateway`、`snapshot`，以及将来新增的任何非 browser 身份）时，先于路径规范化与身份路由表，依次做三件事，与方法无关（含 OPTIONS、PATCH 等任何方法），命中即 403 `identity_forbidden`，不带 `Allow` 头：
     - 5a **请求目标形式**：`target = req.originalUrl`（本中间件是第一个中间件，此时它就是连接上收到的请求目标原文）。`target` 不以 `/` 开头（绝对形式 `http://host/…`、authority 形式 `host:port`、星号形式 `*`）→ 403。Node 对非 CONNECT 的 authority 形式直接回 400，CONNECT 不进入 Express（§9.1），规则仍按一般情形书写。
     - 5b **路径部分**：`pp = path_part(target)`（§9.14.3：截到第一个 `?` 或 `#`，不含该字符）。`pp` 含任何不在 `0x21`–`0x7E` 内的字符，或含 `url_parse_rewrites` 集合 `` \ " ' < > ^ ` { | } ``（码点 `0x5C 0x22 0x27 0x3C 0x3E 0x5E 0x60 0x7B 0x7C 0x7D`，共十个）中的字符 → 403。原因：目标含 `#` 或空白类字符时 Express 改用 Node 旧版 `url.parse` 取路径，后者把 `\` 换成 `/`、把其余九个字符转成百分号编码、修剪空白，实际路由的路径与 `pp` 不同（§9.1 实测 `/api\config#x` 被路由到 `/api/config`；九个字符的转写为本机 Node v24.13.0 对 `0x21`–`0x7E` 逐字符实测的全集）。排除这些字符后，`pp` 与 Express 实际路由的路径逐字相同。gateway 与 snapshot 的合法路径（字面段与 `gateway_pattern`）不含这些字符，不会误伤。
     - 5c **NA 判定**：用 JS 生成物 `PAYLOAD.never_allowed` 按 §9.14.3 的 `NA(path)` 判定 `pp`（未解码）与 `pp` 经一次 `decodeURIComponent` 的结果，解码抛错视为命中，任一命中 → 403。只解码 `pp`，片段与 query 不参与；解码后出现的 `?`、`#`（来自 `%3F`、`%23`）是路径字面字符，不再截断。

     `browser` 身份不做这一步（凭据面只属于站点），5a、5b 也不作用于 browser。**这一步不依赖身份路由表**：它的输入只有 `target` 与生成物中的 `never_allowed`。因此即使将来某行被误加进 gateway 表，或该路径在任何身份的表里都不存在（例如 `/api/login/anything`），或请求目标带片段（`/api/config#x`）、是绝对形式（`http://x/api/config`），结果仍是 403。Express 会路由到凭据面 handler 或站点入口、而 5a–5c 都不命中的非 browser 请求目标，只剩路径部分 `pp` 含 `//`、点段或 `%` 的形式（WGW-1.0.2 追补 F-13 更正：原文只列了 `//` 与点段）。例如 `//index.html`、`/./index.html`，以及百分号编码形式 `/%2e/index.html`、`/%2E/`、`/%2Findex.html`、`/.%2findex.html`、`/x/%2e%2e/index.html`：express.static 先解码再解析路径，会把它们解析到入口文件；`NA` 只解码一次，之后按字面比较，不归一化点段与双斜杠（`/./index.html`、`//index.html` 都不等于 `/index.html`），所以 5c 不命中。它们由第 6 步的规范化返回 404（编码形式由其中的 `%` 规则拒绝），同样不依赖身份路由表。§9.14.3 固定探针锁定这两类的状态码。
  6. 路径规范化：`gateway`、`snapshot` 身份用路径部分 `pp`（未解码）匹配，含 `%`、`//`、点段、尾斜杠或路径参数不合 `gateway_pattern` → 404 `route_not_found`（与网关同规则）；（WGW-1.0.2 追补）`target` 含 `#`（即带片段，正常客户端从不发送）→ 404 `route_not_found`，例如 `/api/status#x`。（WGW-1.0.2 追补 F-13）对非 browser 身份，本步的 `%`、`//`、点段三条规则同时承担凭据面防护（见上一步末尾），是承重规则：不得单独放宽其中任何一条（例如为了放行 `%40` 而允许 `%`）。确需放宽时，必须在同一次契约修订中给出替代防护（例如 `NA` 的输入改为按 express.static 同一方式解码并归一化后的路径），并重跑 §9.14.3"请求目标固定探针"。`browser` 身份沿用 Express 单次解码（站点用 `encodeURIComponent`），参数按 `browser_pattern` 校验。
  7. 身份确定后查该身份的生成路由表：路径与方法都在 → 放行；路径在本身份表内但方法不在 → 405（`Allow` 头列本身份的方法）；路径只属于其他身份 → 403 `identity_forbidden`；任何身份都没有的路径 → 404 `route_not_found`。鉴权先于 404，未认证请求探测不到路由存在性。
  8. actor 头检查见 §9.8；之后才进入 body 解析与 handler。
- **watcher 处理顺序（规范性，WGW-1.0.1 写全）**：W1 `rawHeaders` 计数（401）→ W2 身份（401）→ W3 依次为 never_allowed（非 browser 身份，第 5 步的 5a 目标形式、5b 路径部分字符、5c `NA`，403 `identity_forbidden`，WGW-1.0.2）、路径规范化与本身份路由表匹配（404 `route_not_found` / 405 `method_not_allowed` / 403 `identity_forbidden`）→ W4 actor 与指纹头（400 `invalid_actor_headers`；gateway 写行 actor ≠ `app:risk_admin` → 403 `identity_forbidden`）→ W5 挂载 `req.watcherAuth` 与 `req.watcherRoute` → W6 query 校验（400 `invalid_query`）→ W7 body：GET/HEAD 带 body → 400 `invalid_body`；超过 64 KiB（按 `Content-Length` 或流式计数）→ 413 `payload_too_large`；再按 §9.4 同一顺序做 content-type/JSON object、秘密字段、未知键、缺键、类型检查（gateway 行用 `gateway_enum`/`gateway_pattern`，browser 行用 `type` 定义），规则取自该行的生成物条目 → W8 handler（业务校验与写，§9.12）。静态文件与媒体也是路由表中的行，在 W5 之后才交给 static。任一步失败即返回。与网关（§9.3 R3）的差别是有意的：watcher 是内部服务，身份先于路由，未认证请求探测不到路由存在性；网关的路由集合公开在 YAML 中，先做路由匹配不泄露信息。
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
- `/healthz` 也受身份校验：它只有 `browser` 行（YAML `br.healthz.get`），它在 `never_allowed` 中，永不经网关（gateway 身份直连 `/healthz` 按第 5 步得到 403）。容器健康检查（`docker-compose.yml:160-176`）按 A-1 的条件改造（Planner 已接受）：健康检查是一段 node 脚本，在脚本内读取 `process.env.WATCHER_BROWSER_PROXY_TOKEN` 并以 `X-Watcher-Proxy-Auth` 头发出；**不得**在 compose 的 `test:` 中写 `$VAR`/`${VAR}` 插值（插值结果会以明文出现在 `docker inspect` 的 Healthcheck 字段）；脚本不打印响应体、请求头或 token，只以退出码表示结果。

### 9.3 网关鉴权与角色 scope

- **网关处理顺序（R3，规范性）**。每个 `/v1/watcher/*` 请求按下列顺序处理，任一步失败即返回，后续步骤不执行：

  | 步 | 内容 | 失败响应 |
  |---|---|---|
  | G1 | 路由匹配（§9.4 前缀中间件）：取路径部分（WGW-1.0.2 追补）、never_allowed 投影（WGW-1.0.2）、raw path 规则、phase 门、路径参数、方法 | 404 `route_not_found`（never_allowed 命中时与方法无关）；405 `method_not_allowed`（含 OPTIONS、非媒体 HEAD） |
  | G2 | 身份认证（`resolve_principal`，映射见下表） | 401 `unauthenticated`；403 `invalid_token`；403 `insufficient_scope`（signal token）；503 `auth_unavailable` |
  | G3 | 角色（YAML 行 `roles`） | 403 `insufficient_scope` |
  | G4 | 请求校验：先 query，再 body（先大小，再 content-type/JSON object，再秘密字段、未知键、缺键、类型，顺序见 §9.4） | 400 `invalid_query`；413 `payload_too_large`；400 `secret_field_rejected` / `invalid_body` |
  | G5 | 网关就绪：`WATCHER_GATEWAY_TOKEN` 已配置且通过 §9.2 跨服务互异检查 | 503 `gateway_disabled` |
  | G6 | 准入（§9.7） | 503 `gateway_busy` |
  | G7 | 请求头清洗与 actor/指纹注入（§9.4、§9.8） | 503 `watcher_unavailable`（`details.reason=actor_injection_failed`） |
  | G8 | 上游请求与响应映射（§9.6、§9.9） | 见 §9.6 |

  - （WGW-1.0.2 追补，F-11）生成物停用态（R11，§9.14.3"网关加载期失败的处理"）先于 G1：前缀中间件触发即返回 503 `gateway_disabled`，不做路由匹配、不认证、不读 body、不发上游请求。G5 只处理 token 未配置或未通过跨服务互异检查的情形。
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
- （WGW-1.0.3）角色 scope 回答"这把凭据能做什么"，不回答"请求从哪个入口来"。网关无法区分请求经哪个 Caddy 入口到达（§9.1），因此**不得**用角色（例如拒绝 `system_observer`）或来访请求头来区分入口；入口控制只在 Caddy 侧（§9.14.6）。四角色读保持不变（计划 §2.1、§4 T0 验收要求 `system_observer` 可读 status）。
- **契约声明**：持有同一 `risk_admin` token（静态或会话）的所有调用方一并获得 watcher 写权限（包括停采集、改风控）。以后要区分自然人，用服务端 principal 映射，不给 app 新密钥。
- 同一个 `viewer` 写被 403 `insufficient_scope` 之后，读 status 仍须成功（无任何"禁写传染"）。

### 9.4 路径、方法与请求规范化（网关）

- **路径**：app 请求 `baseUrl + outer_path`，`baseUrl` 已以 `/m` 结尾，app 常量只写 `/v1/watcher/...`。Caddy 外部路径 = `/m` + outer_path，逐路径追加，禁止 `/m/v1/*` 通配。（WGW-1.0.2，用户裁决 2026-09-26）路径参数段在 Caddy 侧的语义是"恰好一个非空段"，一律用锚定、区分大小写的 `path_regexp` 实现（参数段写 `[^/]+`），**不得**用 `path` 匹配器的 `*`（Caddy 的末尾 `*` 是前缀匹配，会跨段、忽略大小写）；清单格式、片段格式与 O-0 的并入规则见 §9.14.3。
- **匹配基于 `scope["raw_path"]`**（未解码），规则：raw path 含 `%`、`//`、`.`/`..` 段、尾斜杠、大小写不符，或路径参数不匹配 YAML `path_params.*.gateway_pattern` → 404 `route_not_found`。（WGW-1.0.2 追补）另外，已触发的请求若 raw path 不以 `/` 开头，或含 `#`，同样 404 `route_not_found`（例如 `/v1/watcher/status#x`；ASGI 的 `raw_path` 不含 query，含 `#` 只可能来自不拆片段的服务器实现）。**禁止尾斜杠 307 重定向**。
- **未注册路径 404、已注册路径未列方法 405**（`Allow` 头列 YAML 中该路径的方法），与 FastAPI 默认一致；这两类判定先于鉴权（§9.3 G1）。`OPTIONS` 一律 405（无 CORS）。`HEAD` 只在 YAML 显式列出的行允许（仅媒体）；其余路径的 HEAD → 405。
- **前缀中间件（R4，规范性；替代 WGW-1.0 的"实现提示"）**：
  - G1 由一个纯 ASGI 中间件实现，它与 §9.5 错误体处理器一起，显式安装到 `create_app()` 返回的角色 app 上（`app_roles.py:145-178` 只复制 `APIRoute`，不复制中间件与异常处理器）。operator-query 必须安装；node-control、event-ingest 无论是否安装，都不得出现网关路由。
  - 触发条件：对 `scope["path"]`（已解码）和 `scope["raw_path"]`（未解码，按 latin1 解码为文本）各取路径部分（§9.14.3 `path_part`，WGW-1.0.2 追补），各判定一次，按 ASCII 不区分大小写比较；任一等于 `/v1/watcher`，或以 `/v1/watcher/`、`/v1/watcher%` 开头 → 由中间件判定。未触发的请求原样交给 FastAPI 路由，既有 `/v1/*` 行为不变。
  - **请求目标形式（WGW-1.0.2 追补，F-09）**：网关只能看到 ASGI scope。uvicorn 的 httptools 实现会从绝对形式请求目标中取出路径再放进 scope（此时与 origin 形式请求无从区分，也无需区分：下面的 `NA_gw` 在该路径上照常生效，上游请求由路由模板构造、从不转发请求目标原文）；h11 实现把整个目标放进 `raw_path`，路径部分不以 `/` 开头。路径部分不以 `/` 开头的请求不会满足触发条件，交给 FastAPI 后因所有路由模板都以 `/` 开头而得到其默认 404，不发上游请求（既有行为，§9.13 不变）；若另一侧（`path` 或 `raw_path`）满足触发条件，则按上一条的 raw path 规则返回 404 `route_not_found`。经 Caddy 到达的请求都已是 origin 形式（`reverse_proxy` 按路径重建请求行）。
  - **never_allowed（WGW-1.0.2，规范性）**：触发后、匹配路由之前，先用 Python 生成物 `PAYLOAD["never_allowed"]` 按 §9.14.3 的网关投影 `NA_gw(path)` 判定，对 `scope["path"]` 与 `scope["raw_path"]`（latin1 解码）的路径部分（WGW-1.0.2 追补）各判定一次；任一命中 → 404 `route_not_found`（§9.5 形状），与方法无关（含 OPTIONS），不带 `Allow` 头，不交给 Starlette 路由，不发上游请求。网关对凭据面还有另外两道检查：构建网关路由时，任何 gateway 行与 never_allowed 相交（§9.14.3 `NA_intersects`）→ 按 R11 停用 `/v1/watcher/*`；G8 构造上游请求时，对替换参数后的 inner path 再判一次 `NA`，命中 → 404 `route_not_found`，不发请求。三道检查都只读生成物，不读 YAML。
  - 中间件按生成物（§9.14.3）匹配 `(method, raw_path)`。不匹配时由中间件直接返回 404/405（§9.5 形状），**不交给 Starlette 路由**，因此 `redirect_slashes` 不会产生 307。匹配成功时把行 id 写入 `scope["state"]["watcher_gateway_route_id"]` 再交给路由；网关 endpoint 第一步断言该值等于自身行 id，不等（中间件未安装或被绕过）→ 404 `route_not_found`（fail-closed）。
  - **禁止**：在角色 app 或全局设置 `redirect_slashes=False`（会把既有 `/v1/*` 的尾斜杠 307 变成 404，违反 §9.13）；用 catch-all `APIRoute`（如 `/v1/watcher/{rest:path}`）实现（会破坏 §9.14.4 的运行时路由集合相等）。
  - （WGW-1.0.3）上面 raw path 规则中的 `%`、`//`、点段三条，与"G1 先于认证"一起承担非移动入口的防护：Caddy 的直连守卫（§9.14.6 的 `D`）是对**解码并 clean 后**的路径判定的，它只保证覆盖"raw 形式不含 `%`、`//`、点段"的路径；触发空间内其余形状（例如 `/v1/watcher/../accounts`、`/v1/watcher%2e%2e/x`）会绕过守卫到达 operator-query，只能靠 G1 在认证之前返回 404。放宽这三条中的任何一条，或把认证挪到 G1 之前，必须在同一次契约修订中重新论证 §9.14.6 的引理 L-1，并重跑 B-5 测试。
  - 回归测试（C-1 验收）：`/v1/accounts/`（尾斜杠）的状态码、`Location` 头、响应体与改动前逐字节一致；`/v1/watcher/status/` → 404 `route_not_found` 且无 `Location`；`/v1%2Fwatcher/status`、`/V1/Watcher/status`、`/v1/watcher` → 404 `route_not_found`（§9.5 形状）。
- `/v1/watcher/` 前缀（按上面的触发条件）下的 404/405 响应体使用 §9.5 统一形状；该前缀以外的 404/405/422 响应体保持 FastAPI 现状。
- **路由名**：`watcher_gateway__<YAML id 中的 . 换成 _>`；必须出现在 operator-query 角色 app，不出现在 node-control / event-ingest。
- **阶段门**：生成时带 `phase_max`（P0..P3），只注册 `phase ≤ phase_max` 的行；未注册行按 404 处理。`phase_max` 写入生成物并参与 §9.14 diff。（WGW-1.0.2 写入 R13）当前审定值 `phase_max = P2`：网关与 watcher 的生成物都不注册 P3 行；测试不得写死 P3 与总行数，期望值从真源按 `phase_max` 计算（独立预言锁定 P2 精确集合的除外，见 §9.12.1 的 P3 启用条件）。
- **query**：只接受该行 `query` 列出的键，定义见 YAML `query_params`；未列键、同键重复、值不合规、`before_created_at` 与 `before_id` 不成对 → 400 `invalid_query`。媒体行不接受任何 query（token 禁止入 query）。
- **body**：
  - GET/HEAD 行带 body → 400 `invalid_body`。大于 64 KiB（`Content-Length` 超限立即拒绝，不读 body；无 `Content-Length` 时流式计数，超限即停止读取）→ 413 `payload_too_large`。写行必须 `Content-Type: application/json` 且为 JSON object，否则 400 `invalid_body`。
  - **秘密键谓词（WGW-1.0.2 写入 R10，规范性）**：`is_secret_key(key)` = 键名含任何非 ASCII 字符（任一码点 > 0x7F）**或** `secret_key_pattern` 按 `flags` 编译后 search 命中。先查非 ASCII，再跑正则。请求侧的"秘密字段" = `key ∈ secret_fields ∨ is_secret_key(key)`；响应侧（§9.6）只用 `is_secret_key`（`secret_fields` 里的 `code` 不命中正则，响应体里的 `code` 键不被删除）。网关、watcher 两层使用同一谓词。
  - 判定顺序：键是秘密字段（上一条的请求侧定义）→ 400 `secret_field_rejected`（`details.fields` 只列键名，不回显值）；键不在 `allow`（含 `deny` 中非秘密键）→ 400 `invalid_body`（`details.unknown_fields`）；缺 `required` → 400 `invalid_body`（`details.missing_fields`）；类型/格式不符 YAML `body_fields`（gateway 行用 `gateway_enum`/`gateway_pattern`；数字必须是 JSON number、布尔必须是 JSON boolean）→ 400 `invalid_body`（`details.field`、`details.rule`）。
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
| 401 | `unauthenticated` | 网关、watcher | 缺/坏凭据 | 连接失效（`isConnectionInvalid` 为真，见下） |
| 403 | `invalid_token` | 网关 | 未知控制面 token（保持现有 403） | 连接失效（`isConnectionInvalid` 为真，见下） |
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
| 503 | `gateway_disabled` | 网关 | operator-query 未配置 `WATCHER_GATEWAY_TOKEN`（或未通过跨服务互异检查）；或处于生成物停用态（R11，§9.14.3，WGW-1.0.2 追补写明） | 同 `watcher_unavailable` |
| 503 | `auth_unavailable` | 网关 | 控制面 token 目录不可用 | 同 `watcher_unavailable` |
| 503 | `db_busy` | watcher | SQLite 锁等待超过 `busy_timeout`；事务已回滚 | 用**同一** `client_ref` 重试 |
| 503 | `media_too_large` | 网关、watcher | 整文件 > 20 MB，发头前拒绝 | 缺图不阻塞文本 |

- **未识别的 `code`（A-3 条件，规范性）**：app 收到 503 且 `code` 不在上表 → 一律按 `watcher_unavailable` 处理（写请求用同一 `client_ref` 回读或重放）；收到其他非 2xx 且 `code` 不在上表 → 显示错误，不自动重试、不换 `client_ref`。（WGW-1.0.2 写入 R14）状态为 503 而 body 不是 JSON，或 JSON 中没有字符串 `code` → 同样按 `watcher_unavailable` 处理，写请求标记为结果未知（用同一 `client_ref` 回读或重放）。
- **连接失效（WGW-1.0.2 写入 R14，规范性；替换 WGW-1.0.1 "401 是唯一触发状态"的说法）**：app 的"交易服务连接失效"只由 watcher 服务层导出的 `isConnectionInvalid(error)` 判定：HTTP 状态为 401，**或**结构码为 `invalid_token`（网关实际以 403 返回），任一成立即为失效。服务层内部为 401 合成的错误码（app 现为 `unauthorized`）视同 401。所有屏幕与服务都调用这个函数，不得自行比较 `status`；`403 insufficient_scope` 不是连接失效（"当前凭据无此权限"，不引导换密钥）。app 现状：`apps/attention-android/src/services/watcherApi.ts:86-90`。
- **`message` 的来源（R8，规范性）**：watcher 的 `message` 只取自固定文案表（按 `code`，必要时按 `details.rule`），不得拼接异常文本（`err.message`）、输入值、文件路径或 SQL。`internal_error` 的 body 不含任何异常信息，堆栈只写经 `safe-log` 过滤的日志。`details` 只含本节与 §9.6、§9.9–§9.12 为该 `code` 定义的键。

### 9.6 上游响应映射（网关）

- 原样透传状态码（body 经下述过滤）的只有：`200`、`206`（媒体）、`400`（`code ≠ invalid_actor_headers`）、`404`（`code ≠ route_not_found`）、`409`、`416`，以及 `503` 且 `code ∈ {db_busy, media_too_large}`。
- 其余一律 503 `watcher_unavailable`：上游 401、403、405、413、400 `invalid_actor_headers` 与 404 `route_not_found`（网关自身缺陷或契约漂移）、其他 5xx、3xx（`follow_redirects=False`）、连接/读/总超时、JSON 行返回非 JSON 或超 8 MiB、错误体缺 `code`。`details.reason` ∈ `upstream_status`、`timeout`、`connect_error`、`redirect`、`malformed_response`、`response_too_large`，可带 `upstream_status`，**不回显上游 body**。上游 401 绝不透传。
- 错误体（R8）：从上游只取 `code`/`message`/`details`，丢弃其他键（含 `error`），加网关 `request_id`。
  - `code` 必须是匹配 `^[a-z][a-z0-9_]{0,63}$` 的字符串，否则按"错误体缺 `code`"处理（503 `watcher_unavailable`，`details.reason=malformed_response`）。
  - `message` 必须是不超过 200 个字符、不含控制字符的字符串，否则替换为 `code` 本身。
  - `details` 必须是 JSON object，否则删除。对**整个** `details` 递归删除 `is_secret_key`（§9.4，WGW-1.0.2）为真的键（任意深度，数组里的对象同样处理；删除而非掩码），不只 `details.current`。
- 成功体过滤：按行 `response.omit` 删除键（数组响应对每个元素生效）；再对整个 JSON 递归删除 `is_secret_key` 为真的键（删除而非掩码；含非 ASCII 字符的键同样删除）。
- 过滤用的正则按 YAML `regex_dialect` 编译（`secret_key_pattern.pattern` + `flags`；Python 用 `re.search(pattern, key, re.IGNORECASE)`，JS 用 `new RegExp(pattern, 'u' + flags)`）。两层"同义"的范围按 R10 限定（WGW-1.0.2 修正）：正则只作用于纯 ASCII 键名，在这个范围内两种编译结果相同；含非 ASCII 字符的键在进入正则之前就被判为秘密，两边都拒绝（请求）或删除（响应）。因此整个谓词 `is_secret_key` 在 Python 与 JS 中同义，这一点不依赖两种引擎对非 ASCII 字符的大小写折叠（例如 U+212A、U+017F）是否一致；S-20(g) 仍用固定探针锁定正则本身的一致性，作为附加检查。
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
- **`/v1/query/channel-route` 归为开仓依赖（WGW-1.0.2 写入 R12，规范性）**：它只在开仓/加仓之前调用（`v3_trade.py:87`、`:202-203`），因此不属于"非开仓路径"，也不是展示类读取。开关打开时：只读快照，状态 ≠ `fresh`（含 `cold`、`expired`、`invalid`、`unauthorized`）→ HTTP 503、body `{"detail": "snapshot_unavailable"}`，与 `open_position` 相同；**不**返回带 `stale: true` 的旧值，**不**回落旧 SQLite reader。数据层拒因（路由冲突、目标账号禁用、风险比例不合规等，`read_api.py:6312-6320` 的映射）保持原有状态码与 `detail`，不改写成 `snapshot_unavailable`。鉴权先于快照判断（`require_reader` 在前，`operator_queries.py:21-23`）：无 token 仍是 401、错 token 仍是 403。开关关闭时行为与现状逐字节一致。
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

#### 9.12.1 价格提醒身份作用域（P3，A-6 召回；WGW-1.0.2 只定契约，不启用）

本小节落实计划 §7.1"列表/删除均按身份限定，不按 symbol 推断归属"与审查 wac-001 🟡-7。**`phase_max` 保持 P2（R13）**：本小节定义的 gateway 行语义在 P3 启用前不注册、不生效；browser 行的改动随 P3 的 watcher 实现一起交付。

- **表扩展**（watcher 自己的 SQLite，库路径先按 §9.12 首条统一到 canonical env；watcher 启动时幂等执行，列已存在则跳过；不涉及控制面迁移与记账表）：

  ```sql
  ALTER TABLE price_alerts ADD COLUMN source TEXT NOT NULL DEFAULT 'watcher' CHECK (source IN ('watcher', 'v3'));
  ALTER TABLE price_alerts ADD COLUMN account_id TEXT;
  ALTER TABLE price_alerts ADD COLUMN position_ref TEXT;
  ALTER TABLE price_alerts ADD COLUMN environment TEXT CHECK (environment IS NULL OR environment IN ('mainnet', 'testnet'));
  ALTER TABLE price_alerts ADD COLUMN triggered_at TEXT;   -- UTC 'YYYY-MM-DD HH:MM:SS'，与 created_at 同格式
  ALTER TABLE price_alerts ADD COLUMN delivered_at TEXT;   -- 本轮恒为 NULL（D8：不推送）
  CREATE INDEX IF NOT EXISTS idx_price_alerts_identity ON price_alerts (source, account_id, environment, position_ref);
  -- 存量行：source 取默认 'watcher'；有 order_id 的行回填 position_ref = CAST(order_id AS TEXT)；其余新列保持 NULL
  UPDATE price_alerts SET position_ref = CAST(order_id AS TEXT)
   WHERE source = 'watcher' AND position_ref IS NULL AND order_id IS NOT NULL;
  ```

- **提醒的身份**是四元组 `(source, account_id, environment, position_ref)`，`symbol` 不参与归属判定。
  - `source = 'v3'`（app 经网关创建）：四个字段都非空；`position_ref` 匹配 `body_fields.position_ref.gateway_pattern`（`<symbol>:<LONG|SHORT|BOTH>:<account_id>`），且其中的 `<symbol>` 等于行的 `symbol`、`<account_id>` 等于行的 `account_id`；`order_id` 为 NULL。
  - `source = 'watcher'`（站点创建）：`position_ref` 为站点 `order_id` 的十进制文本或 NULL；`account_id`、`environment` 可为 NULL。
  - watcher 在插入时校验上述不变式（不另加表级 CHECK，避免重建表）；不满足 → 400 `validation_failed`，`details.rule ∈ {position_ref_mismatch, identity_incomplete}`。
- **触发**（price monitor）：`UPDATE price_alerts SET triggered = 1, triggered_at = <now> WHERE id = ? AND triggered = 0`，只写一次，不删除记录；`delivered_at` 本轮不写。`triggered` 列保留，与 `triggered_at IS NOT NULL` 同步（站点兼容）。
- **gateway 身份（app）**：
  - `gw.price_alerts.get`：watcher 对 gateway 身份**始终**加条件 `source = 'v3'`，与 query 无关；query `source` 只接受 `v3`（YAML `query_params.source.gateway_enum`，其他值 → 400 `invalid_query`，网关与 watcher 同规则）；`account_id`、`environment`、`position_ref`、`triggered` 为可选的等值过滤，同时出现时取交集；不提供按 `symbol` 过滤或归属。排序 `created_at DESC, id DESC`。响应仍是数组，每行至少含 `id, source, account_id, position_ref, environment, symbol, target_price, direction, note, triggered, triggered_at, delivered_at, created_at`，其余列可出现，app 忽略未知字段。app 在仓位详情中查询时必须同时带 `account_id`、`environment`、`position_ref`（客户端规则，保证四账户同 symbol 互不干扰、mainnet/testnet 分离）。
  - `gw.price_alerts.post`：`source` 必须为 `v3`（`body_fields.source.gateway_enum`）；业务校验按上面的 v3 不变式与计划 §7.1（`target_price` 正有限数、`direction ∈ {above, below}`、`note` ≤ 200）。成功响应 `{ok: true, id, revision, replay}`。
  - `gw.price_alert.delete`（WGW-1.0.2 改 body）：body 必带 `account_id`、`position_ref`、`environment`、`client_ref`。watcher 在 §9.12 的 transactional 流程第 4 步执行 `DELETE FROM price_alerts WHERE id = ? AND source = 'v3' AND account_id = ? AND position_ref = ? AND environment = ?`；影响 0 行 → 回滚、404 `not_found`（不写审计、不占幂等键），不区分"不存在"与"不属于该身份"（不泄露站点提醒的存在性）。已触发的提醒可以删除。成功响应 `{ok: true, id, revision, replay}`；同 `client_ref` 重放返回原响应（§9.12 第 1 步）。
- **browser 身份（站点，basicauth 后的管理面）**：
  - `br.price_alerts.get`：可见全部来源，`source` 等 query 为可选过滤；排序保持现状 `symbol, target_price`（站点兼容）。
  - `br.price_alerts.post`：`source` 缺省为 `watcher`；站点提交 `source = 'v3'` 时按 v3 不变式校验；`source = 'watcher'` 且同时带 `order_id` 与 `position_ref` 时两者必须一致，否则 400 `validation_failed`（`position_ref_mismatch`）；只带 `order_id` 时由 watcher 回填 `position_ref`。
  - `br.price_alert.delete`：按 id 删除任意来源（管理面，保持现状）。
  - `br.price_alerts_by_order.delete`：只删 `source = 'watcher' AND order_id = ?` 的行（v3 行没有 `order_id`，此条件让语义显式化）。
- **待 Planner 确认：站点创建的提醒（`source = 'watcher'`）app 能否删除。** 推荐：**不能**，app 也看不到。理由：(1) 计划 §7.1 要求列表与删除按身份限定；(2) 计划 §3 把站点订单定为"站点视角只读，来源标注为 watcher，不当 V3 身份"，站点提醒的 `position_ref` 是站点 `order_id`，与 V3 仓位没有映射；(3) 按订单删除（`DELETE /order/:orderId`）已在 `gateway_excluded` 中，"待身份改造后放行"，按 id 删除站点提醒应同样等待；(4) 站点提醒仍可在站点删除，不丢能力。备选 B：app 只读可见站点提醒、仍不能删（改 `query_params.source.gateway_enum` 为 `[watcher, v3]`，gateway 列表不再强制 `source = 'v3'`，删除条件不变）；备选 C：app 可删（删除条件去掉 `source = 'v3'`，body 仍须与行内三元组相等，站点提醒的 `account_id`/`environment` 常为 NULL，实际多数删不掉，不推荐）。本契约按推荐写定；Planner 若选 B 或 C，由 Architect 出 WGW-1.0.3 勘误。
- **P3 启用条件**（全部满足才允许把 `phase_max` 改为 P3）：(1) 上一条由 Planner 确认；(2) watcher 按本小节完成表扩展、handler 与 price monitor 改造，DB 路径统一到 canonical env；(3) 用户明确授权解除 P3；(4) 同一提交内修改三处写死的 P2 并重新审定：`scripts/contracts/check_watcher_gateway_routes.py:50-58` 的门禁值、`tests/control-plane/test_caddy_watcher_gateway_paths.py` 的 `EXPECTED_P2_LINES`/`P3_PATHS`、`tests/control-plane/api/test_watcher_gateway.py:34`；部署门禁的已审定 `phase_max` 同步更新（§9.14.3 打包）；(5) app A-3 按本小节实现（删除带三元组、仓位详情查询带三元组）。

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
| `scripts/contracts/watcher_gateway_routes_lib.py` | 共享实现：加载 YAML、自洽检查、规范化、渲染四份生成物（纯函数，不写文件；WGW-1.0.2 由三份改为四份） |
| `scripts/contracts/gen_watcher_gateway_routes.py --phase-max P<n>` | 生成器：调用 lib 渲染，覆盖写出 §9.14.3 的四份生成物；不做其他事 |
| `scripts/contracts/check_watcher_gateway_routes.py [--self-check]` | 校验：`--self-check` 只跑 9.14.1；缺省模式 = 9.14.1 + 9.14.4 第 1 项（以已提交生成物中的 `phase_max` 在内存重新生成并逐字节比对） |

**运行环境（R9，审查 🟡-11）**：系统 `python3`（≥ 3.10）加 PyYAML，另需 `node`（S-20 的 JS 编译与同义检查）。安装方式：`python3 -m pip install --user pyyaml` 或系统包管理器。**不得**把 PyYAML 加入 `.venv-arch`、控制面生产 venv 或 watcher `package.json`；watcher 与 operator-query 运行时不 import yaml、不打开 YAML 文件。PyYAML import 失败或找不到 `node` 时，脚本打印 `ENVIRONMENT_ERROR <原因>` 并以退出码 2 结束，不得跳过检查，也不得输出 `ROUTES_DIFF_EMPTY`。

**输出与退出码**：成功时最后一行为 `ROUTES_DIFF_EMPTY rows=<n> yaml_sha256=<hex> phase_max=<Pn>`，退出码 0；有差异时逐条列出，退出码 1；环境错误退出码 2。任何检查比较了 0 行即判失败（退出码 1）。所有测试夹具 token 由脚本随机生成，不读取任何真实环境值。

**9.14.1 真源自洽检查（S-01..S-23；WGW-1.0.1 修订 S-01、S-10、S-18，新增 S-20；WGW-1.0.2 修订 S-10、S-11、S-18，新增 S-21..S-23）**

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
| S-10 | `secret_key_pattern` 是恰含 `pattern`、`flags` 两键的映射；gateway 行：`secret_fields ⊆ body.deny`；`body.allow ∩ secret_fields = ∅`；`body.allow` 与 `query` 的每个名字 `is_secret_key`（§9.4，WGW-1.0.2：含非 ASCII 或正则按 `flags` search 命中）为假；所有行 `allow ∩ deny = ∅`、`required ⊆ allow` |
| S-11 | `body.allow`（经 `field_overrides`）全部定义在 `body_fields`；gateway 行不得引用 `secret: true` 或 `browser_only: true` 的定义；`query` 全部定义在 `query_params`；每个 `{param}` 在 `path_params` 中有该身份的非 null pattern；（WGW-1.0.2）`body_fields.*` 与 `query_params.*` 中出现的 `gateway_enum` 是非空列表且 ⊆ 同一定义的 `enum` |
| S-12 | GET/HEAD 行 `body.allow == []` 且 `write == null`；inner_path 命中 `never_allowed` 的 browser 凭据面写行（`/api/config`、`/api/login/*`）`write == null`（不审计、不带 client_ref，站点登录流程不变）；其余写行 `write` 非 null、`client_ref ∈ required`；`conditional` → `expected_revision ∈ required`；`bumps_revision` → `conditional`；`idempotency ∈ {transactional, reentrant}` |
| S-13 | HEAD 只出现在 inner_path 以 `/media/` 开头的行，且每个 HEAD 行都有同身份同路径的 GET 行；媒体行 `response.headers == media` |
| S-14 | gateway 媒体行 `budget == media`，其余 gateway 行 `budget == config` |
| S-15 | `response.headers` 是 `response_headers` 的键；`json_config_read` 只用于 accounts/channels/risks 的 GET |
| S-16 | gateway 行 `response.mask == []`（网关只删不掩）；gateway 与 browser 的 accounts 读写行分别 `omit ⊇` / `mask ⊇ [api_key, api_secret]` |
| S-17 | snapshot 恰一行 `GET /api/trading/config-snapshot`，`response.fields` 与 §9.10 白名单逐项相等 |
| S-18 | `never_allowed` 每项 `methods == "*"`（R6）；没有 gateway 行的 `inner_path` 与 `never_allowed` 相交（**与方法无关**；WGW-1.0.2 起用 §9.14.3 的 `NA_intersects` 判定，与运行时同一定义，比 WGW-1.0.1 的"`*` 匹配一个或多个段"更宽：`/*` 也覆盖基础路径本身，比较不区分 ASCII 大小写，`{x}` 段与任何非空字面段相容）；没有 gateway 行的 `(method, inner_path)` 命中 `gateway_excluded`（`methods` 为 `"*"` 时与方法无关；`gateway_excluded` 的路径匹配沿用：`*` 匹配一个或多个段，`{x}` 匹配一个段） |
| S-19 | 覆盖预言（独立于 YAML 手写在脚本里，来源计划 §3）：gateway `(method, outer_path, phase)` 集合恰为 9.14.2 的 24 行 |
| S-20 | 正则方言（R5）。对象为 `path_params.*.*_pattern`、`query_params.*.pattern`、`body_fields.*.pattern`/`gateway_pattern`、`actor_headers.*_pattern` 与 `secret_key_pattern.pattern`：(a) 不含 `(?`，`(?:` 除外；(b) 不含 `\d \w \s \b \D \W \S \B`；(c) 字符类之外没有未转义的 `.`；(d) `secret_key_pattern.flags` 的每个字符属于 `regex_dialect.allowed_flags`；(e) 除 `secret_key_pattern` 外全部以 `^` 开头、以 `$` 结尾；(f) Python `re.compile(p, flags)` 与 node `new RegExp(p, 'u' + flags)` 都能编译；(g) 同义探针：对固定探针集（至少含 `""`、`"a"`、`"A"`、`"0"`、`"abc\n"`、`"\n"`、`"a b"`、`"%2F"`、`".."`、`"ſ"`（U+017F）、`"K"`（U+212A）、`"ſession"`、`"toKen"`（K 为 U+212A）、一个星平面字符、`"api_key"`、`"API_KEY"`、`"rapid_x"`，外加 YAML 中出现的全部枚举值）逐一比较 Python（锚定模式用 `re.fullmatch`，`secret_key_pattern` 用 `re.search`）与 node（`.test`）的结果，任一不一致即失败 |
| S-21 | （WGW-1.0.2）`never_allowed` 是非空列表；每项键集合恰为 `inner_path, methods, reason`；`reason` 为非空字符串；`inner_path` 两两不同，且要么恰为 `/`，要么由 `/` 开头的字面段组成（每段匹配 `^[A-Za-z0-9._-]+$`），可选地以一个 `/*` 结尾（`*` 只允许出现在这里）；不含 `{`、`}`、`%`；每项至少与一条 browser 行相交（`NA_intersects` 施于 browser 行的 `inner_path`；否则是失效条目） |
| S-22 | （WGW-1.0.2，Caddy 单段语义）`paths.caddy_external_prefix` 与 `paths.app_outer_prefix` 都由 `/` 开头、匹配 `^[a-z0-9-]+$` 的字面段组成；每条 gateway 行（全部 phase）的 `outer_path` 在前缀之后的每一段，要么匹配 `^[a-z0-9-]+$`，要么恰为 `{<name>}` 且 `name` 匹配 `^[a-z][a-z0-9_]*$`（因此生成的 `path_regexp` 无需转义）；§9.14.3 推导出的 Caddy 匹配器名两两不同，且都不等于 `wgw_fallback`；（WGW-1.0.3）也都不等于 `wgw_direct` |
| S-23 | （WGW-1.0.2，A-6 身份作用域；独立于 YAML 手写在脚本里的预言）`body_fields.source.gateway_enum == [v3]`；`query_params.source.gateway_enum == [v3]`；`gw.price_alert.delete` 的 `body.allow` 与 `body.required` 都恰为 `[account_id, position_ref, environment, client_ref]`；`gw.price_alerts.post` 的 `body.required` ⊇ `{source, account_id, position_ref, environment, client_ref}`；`gw.price_alerts.get` 的 `query` 不含 `symbol`；这三行的 `phase == P3` |

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
| `contracts/generated/caddy-watcher-gateway-paths.txt`（WGW-1.0.2 改为格式 v2） | O-0 核对 Caddy 逐路径配置（核对脚本的比对基准） |
| `contracts/generated/caddy-watcher-gateway.caddy`（WGW-1.0.2 新增；WGW-1.0.3 改为格式 v2） | O-0 并入 Caddyfile 的片段（定义 snippet `watcher_gateway_routes`；WGW-1.0.3 另定义 `watcher_gateway_direct_guard`） |

- **payload**：两份代码生成物内嵌**同一个** payload。它由 YAML 顶层键中除 `invariants`、`gateway_excluded` 之外的全部内容组成（WGW-1.0.2：`never_allowed` 进入 payload，按 YAML 原样保留全部条目、每条的 `inner_path`/`methods`/`reason` 与原顺序，不按 `phase_max` 过滤，因此 `payload_sha256` 覆盖它；两份 Caddy 生成物不含它），其中 `routes` = snapshot 行 + browser 行 + `phase ≤ phase_max` 的 gateway 行，按 `(identity, inner_path, method)` 排序；另加 `_meta: {contract_version, schema_version, yaml_sha256, phase_max, generator: "scripts/contracts/gen_watcher_gateway_routes.py"}`，其中 `yaml_sha256` 是 YAML 文件字节的 SHA-256。
- **规范化**：`payload_text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`；以 `json.dumps(payload_text)` 得到的字符串字面量嵌入两份文件（ASCII-only 的 JSON 字符串字面量同时是合法的 Python 与 JS 字符串字面量），因此两份文件里的 payload 字面量逐字节相同。`payload_sha256 = sha256(payload_text 的 UTF-8)`。
- **文件形状**（两份代码生成物；两份 Caddy 生成物的形状见下）：首行注释 `GENERATED by scripts/contracts/gen_watcher_gateway_routes.py from contracts/watcher-gateway-routes.yaml — DO NOT EDIT`；LF 换行；结尾一个换行。除解析 payload、核对摘要、冻结、编译正则外不含任何逻辑。
  - JS（CommonJS）导出 `{ PAYLOAD, PAYLOAD_SHA256, SECRET_KEY_REGEX }`：`PAYLOAD` 为 `JSON.parse` 后深冻结的对象（键沿用 YAML 的 snake_case）；`SECRET_KEY_REGEX = new RegExp(pattern, 'u' + flags)`（按 `regex_dialect`，不带 `g`/`y`）。
  - Python 导出 `PAYLOAD`、`PAYLOAD_SHA256`、`SECRET_KEY_REGEX = re.compile(pattern, re.IGNORECASE)`（按 `flags` 映射）；只 import `json`、`hashlib`、`re`。
  - 两者在加载时重算 payload 摘要，与 `PAYLOAD_SHA256` 不等即抛错（防手改）。其他路径参数、query、body 正则由消费方按 `regex_dialect` 编译：JS 恒加 `u` 标志，Python 用 `re.fullmatch`；pattern 只作用于已确认是字符串的值（先类型后正则）。
- **never_allowed 的运行时定义（WGW-1.0.2，规范性；watcher 与网关逐字实现同一算法，只读生成物中的 `never_allowed`）**：
  - （WGW-1.0.2 追补，F-09）`path_part(s)`：`s` 中第一个 `?` 或 `#` 之前的前缀（不含该字符）；两者都没有时为 `s` 本身。它只作用于未解码的请求目标（watcher：`req.originalUrl`；网关：`scope["path"]` 与 latin1 解码的 `scope["raw_path"]`），不作用于解码后的文本。`NA` 与 `NA_gw` 的输入一律先经 `path_part`，再按消费方规则（watcher §9.2 第 5 步、网关 §9.4）解码。
  - `ascii_lower(s)`：只把 `A`–`Z` 映射为 `a`–`z`，其余码点不变。`segs(p)`：去掉开头的 `/` 后按 `/` 切分（`segs("/") == [""]`）。
  - `NA(path)`（watcher，输入 inner 路径）：`p = ascii_lower(path)`；对每项 `e`，`t = ascii_lower(e.inner_path)`：若 `t` 以 `/*` 结尾，令 `b = t[:-2]`，`p == b` 或 `p` 以 `b + "/"` 开头即命中；否则 `p == t` 或 `p == t + "/"` 即命中。与方法无关，`methods` 恒为 `"*"`（S-18）。
  - `gw_outer(inner) = paths.app_outer_prefix + (inner[4:] if inner 以 "/api/" 开头 else inner)`（S-07 映射的逆：`/api/config` → `/v1/watcher/config`，`/api/login/*` → `/v1/watcher/login/*`，`/` → `/v1/watcher/`，`/healthz` → `/v1/watcher/healthz`）。`NA_gw(path)`（网关，输入外部路径）= 把每项的 `inner_path` 换成 `gw_outer(inner_path)` 后按 `NA` 同一算法判定。
  - `NA_intersects(template)`（生成器 S-18/S-21、两个运行时的加载期断言共用）：模板的段为字面量或 `{name}`。段相容 = 两个字面段 `ascii_lower` 后相等，或一方为 `{name}` 而另一方为非空字面段。对每项 `e`：若以 `/*` 结尾，令 `k = len(segs(b))`，`len(segs(template)) ≥ k` 且前 `k` 段两两相容即相交；否则两者段数相等且逐段相容即相交。
  - **加载期断言**（两个消费方各做一次，失败即 fail-closed：watcher 抛错使进程非零退出，与摘要不符同样处理；网关按 R11 只停用 `/v1/watcher/*`，见下一条）：`never_allowed` 是非空数组，每项满足 S-21 的形状且 `methods == "*"`；payload 中没有 gateway 行满足 `NA_intersects(inner_path)`。
  - **网关加载期失败的处理（WGW-1.0.2 追补 F-11，规范性；R11）**：下列任一情形都使网关进入"生成物停用态"：(1) import `generated.watcher_gateway_routes` 失败（缺件、语法错误、任何导入异常）；(2) 生成物自检的 payload 摘要与 `PAYLOAD_SHA256` 不符（生成物在导入时抛错，归入 (1)）；(3) 上一条的任一加载期断言不成立（`never_allowed` 为空、形状不合 S-21、`methods != "*"`、有 gateway 行与之相交）；(4) 网关按 payload 构建路由时的其他一致性检查失败（例如路径参数正则无法编译、`budget` 不在 `budgets` 中）。规则：
    - 这些检查在网关模块导入时还是构建路由时执行由实现决定，但抛出的异常一律由网关模块自己捕获并记录为停用原因，**不得**传播到 `create_app()`：`create_app("operator-query")` 正常返回，operator-query 进程不退出，其他端点的行为逐字节不变。日志只写异常类型与失败的检查名，不写 token、请求头或 payload 内容。
    - 停用态下前缀中间件仍然安装；对满足触发条件的每个请求直接返回 503 `gateway_disabled`（§9.5 形状，带 `request_id`，不带 `details`），先于 G1–G8：不做 `NA_gw`（payload 不可信）、不做路由匹配、不认证、不读 body、不发上游请求。停用态下网关 `APIRoute` 是否注册不作要求（中间件先返回）；§9.14.4 第 2 项的路由集合相等只对非停用态的夹具断言。
    - 停用态只能通过修复生成物并重启 operator-query 解除，运行中不重试加载。
    - 与 G5 的区别：G5（token 未配置或未通过跨服务互异检查）发生在 G2–G4 之后，未认证请求先得到 401/403；停用态发生在 G1 之前，任何触发的请求都得到 503。两者都不影响既有 `/v1/*`。
  - 固定探针（消费方单元测试的独立预言，按当前 YAML 的 6 项）：`NA` 命中 `/api/config`、`/API/CONFIG`、`/api/config/`、`/api/login`、`/api/login/`、`/api/login/start`、`/api/login/anything/else`、`/api/login/qr/status`、`/`、`/index.html`、`/healthz`、`/healthz/`；不命中 `/api/configs`、`/api/loginx`、`/api/login-x`、`/api/status`、`/index.htm`、`/healthzz`、`/media/1-1.jpg`。`NA_gw` 命中 `/v1/watcher/config`、`/v1/watcher/login`、`/v1/watcher/login/start`、`/V1/WATCHER/LOGIN/START`、`/v1/watcher/login/qr/status`、`/v1/watcher/`、`/v1/watcher/index.html`、`/v1/watcher/healthz`；不命中 `/v1/watcher/status`、`/v1/watcher/configs`、`/v1/watcher/trading/accounts`。`NA_intersects` 为真：`/api/login/{x}`、`/{x}`、`/api/{x}`、`/api/{x}/start`；为假：`/api/status`、`/api/trading/{x}`、`/media/{filename}`、`/api/price-alerts/{alert_id}`。（WGW-1.0.2 追补）`NA` 另命中 `//`（`/` 条目的 `t + "/"`，属预期，不是缺陷）。
  - **请求目标固定探针（WGW-1.0.2 追补，F-09；消费方测试的独立预言）**：
    - `path_part`：`/api/config#x` → `/api/config`；`/api/config?a#b` → `/api/config`；`/api/status#/../config` → `/api/status`；`/#x` → `/`；`/healthz?a` → `/healthz`；`/api/status` → `/api/status`；`http://x/api/config` → `http://x/api/config`（不以 `/` 开头，交给 5a）。
    - watcher，`gateway` 与 `snapshot` 两种身份，GET/HEAD/POST/PUT/DELETE/PATCH/OPTIONS 七种方法，经真实 HTTP 连接发送原文请求目标（不能经会预先规范化 URL 的客户端库），期望：
      - 403 `identity_forbidden`（第 5 步）：`/api/config#x`、`/api/login/start#x`、`/healthz#x`、`/#x`、`/index.html#x`（5c：路径部分命中 `NA`）；`http://x/api/config`、`http://x/api/status`、`*`（5a：非 origin 形式，与路径是否属于凭据面无关）；`/api\config#x`、`/api\status`、`/api/status"`、`/api/{x}`（5b）；`//`（5c）。
      - 404 `route_not_found`（第 6 步）：`/api/status#x`（带片段）；`//index.html`、`/./index.html`、`///`（`NA` 不命中，由规范化拒绝）；（WGW-1.0.2 追补 F-13）`/%2e/index.html`、`/%2E/`、`/%2Findex.html`、`/.%2findex.html`、`/x/%2e%2e/index.html`（5a–5c 都不命中：`pp` 只含可打印 ASCII，且不含十字符；解码成功，解码结果分别是 `/./index.html`、`/./`、`//index.html`、`/./index.html`、`/x/../index.html`，都不被 `NA` 命中。由第 6 步的 `%` 规则拒绝。去掉第 6 步时，这些目标会被 express.static 解析到入口文件，所以它们专门锁定 `%` 规则的承重作用）。
      - 以上每个请求都不得到达任何 handler 或 static（handler 与 static 调用计数为 0）。`browser` 身份不受 5a、5b 与片段规则影响，行为沿用现状，不在本探针内断言。
    - 网关（在 ASGI scope 层直接构造 `path`/`raw_path`，因为 uvicorn 两种 HTTP 实现对请求目标的处理不同），七种方法，上游请求计数恒为 0：`path = raw_path = /v1/watcher/config#x` → 404 `route_not_found`（`NA_gw` 命中路径部分）；`/v1/watcher/healthz#x` → 404；`/v1/watcher/status#x` → 404（raw path 含 `#`）；`path = /v1/watcher/config`、`raw_path = http://x/v1/watcher/config` → 404 `route_not_found`；`path = raw_path = http://x/v1/watcher/status` → 不触发中间件，FastAPI 默认 404（`{"detail":"Not Found"}`）。
- **Caddy 单段语义（WGW-1.0.2，用户裁决 2026-09-26）**：外部路径中的 `{param}` 表示"恰好一个非空段"。两份 Caddy 生成物由同一个中间表产生：对 `phase ≤ phase_max` 的 gateway 行，按外部路径 `template = paths.caddy_external_prefix + outer_path`（`{param}` 原样保留）分组，方法取并集。
  - `regex(template) = "^" + template 逐段拼接 + "$"`：字面段原样输出（S-22 保证只含 `[a-z0-9-]`，无需转义），`{param}` 段输出 `[^/]+`。例：`/m/v1/watcher/trading/accounts/{account_id}` → `^/m/v1/watcher/trading/accounts/[^/]+$`。语义以 Go RE2（Caddy）为准：`$` 只匹配文本末尾，区分大小写。
  - `matcher_name(template)` = `"wgw_r_"` 接上：outer_path 去掉 `paths.app_outer_prefix + "/"` 后的各段以 `_` 连接（`{name}` 段取 `name`，`-` 换成 `_`）。例：`wgw_r_trading_accounts_account_id`、`wgw_r_price_alerts_alert_id`、`wgw_r_status`。
  - 兜底正则（WGW-1.0.2 追补 F-10 修订）`fallback = "^(?i:" + caddy_external_prefix + app_outer_prefix + ")(?:[/\n]|$)"`，即 `^(?i:/m/v1/watcher)(?:[/\n]|$)`（`\n` 是正则里的两个字符 `\` 与 `n`，按原样写进片段；Caddyfile 对不带引号的 token 不处理反斜杠）。前缀部分不区分大小写，用作用域标志组而不是开头的 `(?i)`（Python 3.11 起拒绝不在开头的全局标志，且 token 不以 `(` 开头，避免与 Caddyfile snippet 定义语法混淆）。语义：前缀之后是 `/`、换行或文本末尾。
    - 为什么含 `\n`：Python 的 `$`（未开 MULTILINE）在文本末尾**以及末尾换行之前**都成立，RE2 的 `$` 只在文本末尾成立，所以 WGW-1.0.2 首版 `(?:/|$)` 对 `/m/v1/watcher\n`（请求 `%0A` 结尾，Caddy 解码后）Python 判真、RE2 判假（审查 wac-049 🟡-2 实测）。加入 `\n` 分支后，凡是 Python 靠"末尾换行之前"成立的情形，`[/\n]` 在两个引擎里都先成立，因此对任意输入两者结果相同（本机 Python 3 与 Go 1.26 `regexp` 对 120 个探针逐一比对，0 不一致）。两者都没有可移植的"仅文本末尾"锚点（RE2 只有 `\z`，Python 3.14 以前只有 `\Z`），所以不用换锚点的办法。
    - 行级正则 `^…$` 不需要改：它们只能用 `re.fullmatch` 检查（§9.14.4 第 1 项），`fullmatch` 要求吃完整个文本，Python `$` 的换行特例不影响结果；用 `re.search`/`re.match` 检查行级正则是错误的（会让 `…/status\n` 判真，与 RE2 不同）。
- **Caddy 清单**（`caddy-watcher-gateway-paths.txt`，格式 v2，替换 WGW-1.0.1 的 `*` 写法）：ASCII、LF、结尾一个换行。头部四行 `# _generated_from contracts/watcher-gateway-routes.yaml`、`# _yaml_sha256 <hex>`、`# _phase_max <Pn>`、`# _format watcher-gateway-caddy-paths.v2`；之后每个唯一外部路径一行 `<template> <regex> <METHOD>[ <METHOD>…]`（单个空格分隔，方法按字典序），各行按 `template` 的字节序升序。清单中不出现 `*`。例：`/m/v1/watcher/trading/accounts/{account_id} ^/m/v1/watcher/trading/accounts/[^/]+$ DELETE PUT`。`template` 只作标识，**不得**贴进 Caddyfile（Caddy 会把未知占位符 `{account_id}` 替换为空串）。
- **Caddy 片段**（`caddy-watcher-gateway.caddy`）：ASCII、LF、结尾一个换行，缩进为 tab。头部四行与清单相同，只是 `_format` 为 `watcher-gateway-caddy-snippet.v1`（WGW-1.0.3 起为 `watcher-gateway-caddy-snippet.v2`，见下方"格式 v2"）。随后恰好定义一个 snippet，块内按清单行序为每个外部路径输出一组，最后输出兜底组（v1 的形状；v2 在此基础上追加，见下方）：

  ```caddyfile
  (watcher_gateway_routes) {
  	@wgw_r_dialogs {
  		path_regexp ^/m/v1/watcher/dialogs$
  		method GET
  	}
  	handle @wgw_r_dialogs {
  		import watcher_gateway_upstream
  	}
  	# ……按清单行序，其余每个外部路径一组，形状相同（例如 @wgw_r_media_filename 的 method 行为 "method GET HEAD"）……
  	@wgw_fallback {
  		path_regexp ^(?i:/m/v1/watcher)(?:[/\n]|$)
  	}
  	handle @wgw_fallback {
  		respond 404
  	}
  }
  ```

  除上列行外不输出任何内容（组之间不留空行，`# ……` 一行只是本文示意，不输出）。片段中不出现 `*`，不含上游地址、主机名或凭据。
- **Caddy 片段格式 v2（WGW-1.0.3，规范性；替换上面 v1 的"恰好定义一个 snippet"）**：头部 `_format` 为 `watcher-gateway-caddy-snippet.v2`，其余三行不变。文件依次恰好定义两个 snippet：
  1. `(watcher_gateway_routes)`：v1 的全部内容原样保留（逐路径组、兜底组），在兜底组之后、闭合 `}` 之前追加**直连守卫组**；
  2. `(watcher_gateway_direct_guard)`：只含同一个直连守卫组。

  直连守卫正则 `direct = "^(?i:" + app_outer_prefix + ")(?:[/\n%]|$)"`，即 `^(?i:/v1/watcher)(?:[/\n%]|$)`（`\n` 是两个字符，写法与兜底正则相同）。v2 文件尾部逐字为（`……` 表示 v1 已有的逐路径组，本文示意，不输出）：

  ```caddyfile
  (watcher_gateway_routes) {
  	……
  	@wgw_fallback {
  		path_regexp ^(?i:/m/v1/watcher)(?:[/\n]|$)
  	}
  	handle @wgw_fallback {
  		respond 404
  	}
  	@wgw_direct {
  		path_regexp ^(?i:/v1/watcher)(?:[/\n%]|$)
  	}
  	handle @wgw_direct {
  		respond 404
  	}
  }
  (watcher_gateway_direct_guard) {
  	@wgw_direct {
  		path_regexp ^(?i:/v1/watcher)(?:[/\n%]|$)
  	}
  	handle @wgw_direct {
  		respond 404
  	}
  }
  ```

  - 为什么守卫放进 `(watcher_gateway_routes)`：app 站点已经按 F-12 把这个 import 写在所有 `handle`/`handle_path`/`route` 之前，守卫因此自动先于面板的 `handle /v1/*`；不增加新的 import 位置要求，app 站点的候选 Caddyfile 不需要为守卫改动。
  - 为什么另有 `(watcher_gateway_direct_guard)`：供 app 站点以外、同样转发到 operator-query 的站点使用（§9.14.6 N-2）。同一站点不得同时 import 两者（两者都定义命名匹配器 `@wgw_direct`，Caddy adapt 会报重复定义）。
  - 与兜底的差别：守卫的字符类多一个 `%`。Caddy 对解码后的路径匹配，解码后出现 `%` 只可能来自 `%25`；网关的触发条件包含"以 `/v1/watcher%` 开头"（§9.4），守卫把它一并挡下。兜底保持 F-10 的写法不变。
  - 为什么含 `\n`、为什么在 `re.search` 下与 RE2 同义：理由与兜底正则相同（F-10）。§9.14.4 第 1 项的探针已在本机 Python 3 与 Go `regexp` 上逐条比对，0 不一致。
  - 片段中仍不出现 `*`，不含上游地址、主机名或凭据。清单（`caddy-watcher-gateway-paths.txt`）不含守卫，格式仍为 v2，内容不变。
- **O-0 并入规则（规范性）**：
  - Caddyfile 在全局位置 `import` 该片段文件（定义 snippet），并手写 snippet `(watcher_gateway_upstream)`：恰好一次 `uri strip_prefix /m`，然后 `reverse_proxy` 到 operator-query；不注入 `X-Watcher-Proxy-Auth`，不含任何其他匹配逻辑。
  - 在 app 所用站点块的**顶层**（`/m` 尚未被剥离的位置）`import watcher_gateway_routes`；不得放进 `handle_path /m*` 之类会先剥前缀的块。站点中不得有其他处理器把 `/m/v1/watcher` 前缀（任意大小写）转发到 operator-query 或 watcher（沿用审查 wac-026 🟡-1 (d)）；browser 凭据沿用运行时占位符 `{env.WATCHER_BROWSER_PROXY_TOKEN}`（(g)）。
  - （WGW-1.0.3）非移动入口：面板 `/v1/*` 等其他转发到 operator-query 的路由，受 §9.14.6 的入口不变式 I-1 与并入规则 N-1..N-4 约束；app 站点经上一条的 import 已带直连守卫，其他转发到 operator-query 的站点须 `import watcher_gateway_direct_guard`。
  - **import 的位置（WGW-1.0.2 追补 F-12，审查 wac-049 🟡-3）**：该 `import` 必须写在该站点块中**所有** `handle`、`handle_path`、`route` 指令之前。依据 Caddy v2.10.2 `httpcaddyfile` 的路由排序（`caddyconfig/httpcaddyfile/directives.go:437-530`）：同为 `handle` 时，只有两边都是单个路径匹配器才按路径长度排序；片段用的是命名匹配器，与站点里的 `handle /m/*` 之类比较时按出现顺序稳定排序，`handle` 块之间互斥、先匹配者生效。因此写在 import 之前的 `handle /m/*` 会先吃掉 `/m/v1/watcher/*`，片段里的逐路径处理器与兜底都不再生效，而"兜底在逐路径处理器之后"这一检查照样通过。不得把 import 包进 `route { … }` 来"固定顺序"：`route` 指令整体排在 `handle` 之后，结果更糟。
  - **import 位置管不到的指令（WGW-1.0.2 追补 F-13，审查 wac-058 🟡-2 (a)）**：import 的位置只决定片段在 `handle` 组内的先后。Caddyfile 先按指令顺序排序，写在站点块里的先后不影响这一步。Caddy v2.10.2 默认指令顺序（`caddyconfig/httpcaddyfile/directives.go:47-84`）中排在 `handle` 之前的指令，无论写在 import 之前还是之后，都先于片段执行。这些指令是：`tracing`、`map`、`vars`、`fs`、`root`、`log_append`、`skip_log`/`log_skip`、`log_name`、`header`、`request_body`、`redir`、`method`、`rewrite`、`uri`、`try_files`、`basicauth`/`basic_auth`、`forward_auth`、`request_header`、`encode`、`push`、`intercept`、`templates`、`invoke`。此外，全局 `order` 选项可以把任何指令（含插件指令）移到 `handle` 之前。其中改写路径或方法的指令（`rewrite`、`uri`、`method`、`try_files`）会让片段看到改写后的请求；审查 wac-058 用真实 Caddy 实测：import 之后写的顶层 `rewrite /m/v1/watcher/dialogs /m/v1/watcher/status` 仍然先生效，`GET /m/v1/watcher/dialogs` 被转成 `GET /v1/watcher/status`。其余指令中，`redir`、`basic_auth`、`forward_auth` 可能直接终结请求，`request_header` 会改写 `Authorization` 等网关鉴权输入。规则：站点块顶层这一类指令的匹配器不得命中 `/m/v1/watcher` 前缀（任意大小写）；无匹配器、或匹配器可能命中该前缀的，只允许下一条遮蔽检查白名单中的 handler 类型。这条规则由遮蔽检查在 adapt 后的 JSON 上机械兜底，JSON 已经反映了全局 `order` 与指令排序的结果。O-0 另须人工核对并记录：Caddyfile 全局块中的全部 `order` 选项，以及 app 站点块顶层所有排在 `handle` 之前的指令（逐条列出指令名、匹配器，说明它是否可能命中 `/m/v1/watcher` 前缀、为何不构成遮蔽）。
  - O-0 核对脚本：`caddy adapt` 后解析 JSON，抽取 `path_regexp` 模式以 `^/m/v1/watcher/` 开头的全部 `(pattern, methods)`，与清单的 `(regex, methods)` 逐行比较，对称差为空且行数 > 0；兜底匹配器存在（模式与片段逐字相同）且在这些处理器之后求值。（WGW-1.0.2 追补 F-12）另加**遮蔽检查**：在 app 站点的路由链中（adapt 后该站点 host 匹配下的 subroute 路由列表，以及其中与 wgw 路由同处一层的列表），检查第一条 wgw 路由之前的每一条路由。（WGW-1.0.2 追补 F-13 修订，审查 wac-058 🟡-2 (b)：原文要求"匹配器对探针都不得命中"，对无匹配器的 `encode`、`header` 会误判失败；改为按 handler 类型判定）判定分两步：
    1. **是否命中**：用该路由的匹配器评估探针 `/m/v1/watcher`、`/m/v1/watcher/status`、`/M/V1/WATCHER/dialogs`。按 Caddy 匹配器语义评估：`path` 不区分大小写，`*` 为前缀、后缀或通配；`path_regexp` 按 RE2。无匹配器即视为命中全部探针。遇到脚本无法评估的匹配器类型（如 `expression`、`not`、自定义模块）一律视为命中，不得当作不命中。三个探针都不命中的路由通过。
    2. **命中后是否遮蔽**：命中任一探针的路由，只有同时满足以下三个条件才不算遮蔽：该路由的 `handle` 数组非空，且其中**每一个** handler 都在下面的白名单内；该路由没有 `"terminal": true`；该路由没有 `group` 字段（`handle` 块生成的路由带 `group`，同组互斥，会遮蔽片段）。否则判失败。一条 adapt 后的路由可能由几个相邻的无匹配器指令合并而成（例如 `request_header` 与 `encode` 合并为同一条路由的两个 handler），所以必须逐个判定 handler。白名单（按 adapt 后 JSON 的 `handler` 值，只列不改请求路径与方法、不终结请求、不改写请求头的类型）：
       - `encode`；
       - `headers`，但仅当它没有 `request` 键（只操作响应头；`header` 指令生成的就是这种）。带 `request` 键（`request_header` 指令，或 `header` 的请求头操作）判失败，因为它可能改写 `Authorization` 等网关鉴权输入；
       - `vars`（`vars`、`root`、`log_skip` 等指令生成）；
       - `map`；
       - `log_append`；
       - `tracing`。

       白名单之外的类型一律判失败，包括 `rewrite`（`rewrite`、`uri`、`method`、`try_files`）、`static_response`（`redir`、`respond`）、`authentication`（`basic_auth`）、`reverse_proxy`（含 `forward_auth`）、`subroute`、`request_body`、`invoke`、`templates`、`intercept`、`push`、`file_server`、`error`，以及任何未知或插件类型。白名单只能通过契约修订扩充，O-0 不得临时加例外。以上 handler 名已用本机 Caddy v2.10.2 的 `caddy adapt` 对相应指令逐一核对。
  - 本机 Caddy 探针（非生产）：用同版本 Caddy、**生产 Caddyfile 的副本**（只把上游地址换成本机桩、把站点地址换成本机地址，其余逐字不改）与桩上游，跑 §9.14.4 第 1 项的全部 Caddy 探针（行级与兜底，含换行结尾用例），结果逐条一致；另确认 `/m/v1/watcher/status`（GET）确实到达 operator-query 桩（证明没有被站点里更早的处理器遮蔽）。（WGW-1.0.2 追补 F-13）另跑下方行为说明中的两类用例：字面点段与 `//`、百分号编码与 `#`，并确认转发到桩的路径与期望值逐条一致。期望值按该说明判定，字面点段与 `//` 不按 404 判定。记录 `caddy version` 与副本相对生产文件的逐行差异。
  - 行为说明：Caddy 对解码并 clean 后的路径做匹配（`%2F` 解码为 `/`、`//` 合并、`..` 解析）。（WGW-1.0.2 追补 F-13 更正，审查 wac-058 🟡-3：原文写"`reverse_proxy` 转发的是原始路径，所以 `/m/v1/watcher/x/../status` 这类请求会通过 Caddy，再由网关按 raw path 规则返回 404"，这对本契约规定的 upstream 写法不成立。）`(watcher_gateway_upstream)` 的 `uri strip_prefix /m` 在剥前缀之前，会先对**未解码**的路径（`EscapedPath`）做 clean：合并连续斜杠、解析字面点段、保留尾斜杠（Caddy v2.10.2 `modules/caddyhttp/rewrite/rewrite.go:48-53` 注释、`:259-268` 调 `caddyhttp.CleanPath`、`:479-490` `changePath`）。`reverse_proxy` 转发的是 clean 之后的路径。结果分两类：
    - 字面点段与 `//`：`/m/v1/watcher/x/../status`、`/m/v1/watcher/./status`、`/m/v1/watcher//status` 都以 `/v1/watcher/status` 转发，网关按正常路由处理，结果与直接请求 `/v1/watcher/status` 相同（审查实测与本次复核均为 200）；`/m/v1/watcher/trading/accounts/a/..` 以 `/v1/watcher/trading/accounts` 转发。这不是安全绕过：Caddy 的行级正则本来就是对 clean 后的路径匹配的，网关收到的路径与 Caddy 放行时匹配的是同一条合法路由。clean 后不在表内的路径（如 `/m/v1/watcher/x/../config`）由兜底返回 404，不转发。
    - 百分号编码形式（`%2e%2e`、`%2e`、`%2F`、`%0A`）不被 clean 改动，原样转发，例如 `/m/v1/watcher/x/%2e%2e/status` 以 `/v1/watcher/x/%2e%2e/status` 转发；请求目标中的 `#` 由 Go 编码为 `%23` 后转发（`/m/v1/watcher/media/a#x` → `/v1/watcher/media/a%23x`）。这些请求到达网关后，按 raw path 的 `%` 规则返回 404（§9.4）。

    以上两类都已用本机 Caddy v2.10.2 实测。因此经 Caddy 的探针（O-0 本机探针、Tester 的端到端用例）对字面点段与 `//` 的期望值**不得**写成 404，应按 clean 后的路径判定：clean 后是表内路由的，期望到达网关并得到该路由的正常结果；否则期望兜底 404。raw path 的点段、`//` 规则只能直接对网关测试（§9.14.5 就是这样做的，不受影响）。Caddy 只做粗筛，精确边界在网关（§9.4）。经 Caddy 访问时，表外路径与表外方法由兜底返回 404（空 body，不是 §9.5 形状）；§9.4 的 404/405 结构体语义在网关层成立，§9.14.5 负例直接对网关测试。（WGW-1.0.2 追补 F-10）兜底只覆盖"前缀之后是 `/`、换行或结尾"：`/m/v1/watcher#x`、`/m/v1/watcher%0D`、`/m/v1/watcherx` 这类前缀之后紧跟其他字符的路径既不命中逐路径处理器也不命中兜底，落到站点的其他处理器；按上面的并入规则，站点中没有其他处理器把这类路径转发到 operator-query 或 watcher，所以这不影响边界，只是粗筛的覆盖范围。
- **打包**（审查补充，W-0 / C-1 / O-0 验收）：watcher 镜像白名单 `WATCHER_RUNTIME_RELATIVE_PATHS`（`scripts/build_immutable_watcher_image.py:34`）必须加入 `lib/generated/gateway-routes.js`；控制面发布若把 `api/*.py` 平铺到 `host/`（`scripts/make_account_stall_release.py:93-99`、`:217`），须保留子目录，打入 `host/generated/__init__.py` 与 `host/generated/watcher_gateway_routes.py`。部署门禁核对两份产物的 `_meta.yaml_sha256` 与 `_meta.phase_max` 等于本次发布的已审定值。

**9.14.4 "生成路由与真源 diff 为空"的判定**（R9：按运行环境拆成三项，由传递性合成）

1. **生成物 diff**（`scripts/contracts/check_watcher_gateway_routes.py`，系统 `python3` + PyYAML）：四份生成物与内存重新生成的结果逐字节相同；四份的 `phase_max` 与 `yaml_sha256` 相同。（WGW-1.0.2 追加）另做三项显式断言，不依赖"同源重新生成"：
   - 两份代码生成物的 `PAYLOAD["never_allowed"]` 与 YAML 的 `never_allowed` 深相等（含顺序），且非空；
   - 两份 Caddy 生成物不含字符 `*`，头部 `_format` 分别为 `watcher-gateway-caddy-paths.v2`、`watcher-gateway-caddy-snippet.v1`；片段中 `path_regexp`/`method` 的有序序列与清单逐行一致，最后一组是兜底；
   - **Caddy 探针（独立预言，写在脚本里，不调 lib）**：对清单每行用 Python `re.fullmatch(regex, s)` 检查（WGW-1.0.2 追补 F-10 限定"同义"的范围：本语法子集在 `fullmatch` 下与 RE2 `MatchString` 同义，因为 `fullmatch` 必须吃完整个文本；**不得**用 `re.search`/`re.match` 检查行级正则）：把每个 `{param}` 换成 `x` 的路径必须匹配；任一 `{param}` 换成空串或 `x/y`、整条路径追加 `/`、整条路径转大写、字面行追加 `/x`，以及（追补）最后一段是字面段的行追加 `\n` 或 `#x`，都必须不匹配；（追补）最后一段是 `{param}` 的行追加 `\n` 或 `#x` 必须**匹配**（追加的字符落进参数段，`[^/]+` 在两个引擎里都匹配换行与 `#`；这类请求到达网关后按 raw path 的 `%`/`#` 规则返回 404，属预期）。兜底正则先断言它逐字等于脚本内按 `paths` 独立拼出的 `^(?i:/m/v1/watcher)(?:[/\n]|$)`，再用 `re.search` 检查（该正则在 `re.search` 下与 RE2 同义，理由见 §9.14.3 兜底正则说明）：`/m/v1/watcher`、`/m/v1/watcher/`、`/M/V1/WATCHER/login/start`、`/m/v1/watcher/login/start`、（追补）`/m/v1/watcher\n`、`/M/V1/WATCHER\n`、`/m/v1/watcher/status\n`、`/m/v1/watcher/status#x` 匹配；`/m/v1/watcherx`、`/m/v1/other`、（追补）`/m/v1/watcherx\n`、`/m/v1/watcher\r`、`/m/v1/watcher#x` 不匹配。以上预期值已在 Go RE2 上逐条核对，O-0 的本机 Caddy 探针再用真实 Caddy 复核一次。比较行数为 0 即失败。
   - **（WGW-1.0.3，片段格式 v2；替换上面第二条中片段的 `_format` 期望值与"最后一组是兜底"）**：片段头部 `_format` 为 `watcher-gateway-caddy-snippet.v2`；文件恰好定义 `(watcher_gateway_routes)`、`(watcher_gateway_direct_guard)` 两个 snippet，顺序如此；前者中逐路径组的 `path_regexp`/`method` 有序序列与清单逐行一致，其后依次恰为兜底组、直连守卫组（逐字等于 §9.14.3"格式 v2"），后者逐字只含直连守卫组。直连守卫正则先断言它逐字等于脚本内按 `paths.app_outer_prefix` 独立拼出的 `^(?i:/v1/watcher)(?:[/\n%]|$)`，再用 `re.search` 检查：`/v1/watcher`、`/v1/watcher/`、`/V1/WATCHER/status`、`/v1/watcher/status`、`/v1/watcher\n`、`/V1/WATCHER\n`、`/v1/watcher/status\n`、`/v1/watcher%`、`/v1/watcher%x`、`/v1/watcher/media/1700000000000-1.png`、`/v1/watcher/status#x` 匹配；`/v1/watcherx`、`/v1/watcherx\n`、`/v1/watcher\r`、`/v1/watcher#x`、`/v1/other`、`/v1/accounts`、`/xv1/watcher`、`/m/v1/watcher/status`、`//v1/watcher/status` 不匹配（最后一项是未 clean 的原文；真实 Caddy 会先 clean 成 `/v1/watcher/status` 再匹配，所以活体探针对它期望守卫 404，见 §9.14.6 V-3）。另断言两个前缀正则互不相交：兜底正则对上面全部"匹配"探针都不匹配，守卫正则对 `/m/v1/watcher`、`/m/v1/watcher/status` 不匹配。**G1 ⇒ D（独立预言）**：对清单每行，把 `{param}` 换成 `x`，以及换成脚本内取值池中按 `re.fullmatch` 符合该参数 `gateway_pattern` 的每个取值（池至少含 `account-a`、`A.b@c-1`、`BTCUSDT`、`-1001234567890`、`1700000000000-1.png`、`1`；某个参数过滤后没有取值即失败），去掉 `paths.caddy_external_prefix` 之后的路径都必须被守卫正则 `re.search` 匹配。预期值已在 Go `regexp` 上逐条核对（WGW-1.0.3 本机实测）。
2. **控制面运行时 diff**（pytest，`.venv-arch`，**不** import yaml，只 import 已提交的 Python 生成物）：用夹具环境构造 `create_app("operator-query")`，枚举路径以 `/v1/watcher/` 开头的 `APIRoute`，得到 `{(method, path, name)}`，与 payload 中 gateway 行的 `{(method, outer_path, watcher_gateway__<id>)}` 对称差为空；每个 endpoint 满足 `inspect.iscoroutinefunction`、`route.dependant.body_params == []`、`route.body_field is None`（§9.3 G4）；同一集合不得出现在 `create_app("node-control")`、`create_app("event-ingest")`；operator-query 角色 app 上装有前缀中间件（探针 `/v1/watcher/status/` 得到 404 `route_not_found` 且无 `Location`）。（WGW-1.0.2 追加）never_allowed：`NA_gw`、`NA`、`NA_intersects` 通过 §9.14.3 固定探针；`NA_gw` 命中的每个探针对 GET/HEAD/POST/PUT/DELETE/PATCH/OPTIONS 都得到 404 `route_not_found`、无 `Allow`、上游请求计数为 0；用 payload 的一个副本注入一条合成 gateway 行 `GET /v1/watcher/login/x → /api/login/x` 构建网关，结果必须是 `/v1/watcher/status` 返回 503 `gateway_disabled`、既有 `/v1/*` 端点照常（R11），以此证明加载期断言存在。（WGW-1.0.2 追补 F-11）停用态至少覆盖四种注入，逐一断言：(a) 上述合成相交行；(b) `never_allowed` 置为空列表；(c) 某条 `never_allowed` 的 `methods` 改为列表；(d) 生成模块不可导入（例如在 `sys.modules` 中把 `generated.watcher_gateway_routes` 置为会抛错的桩，或摘要不符）。每种情形下：`create_app("operator-query")` 正常返回、不抛错；不带任何 `Authorization` 的 `GET /v1/watcher/status` 与 `POST /v1/watcher/config` 都得到 503 `gateway_disabled`（§9.5 形状，先于认证与 never_allowed）；上游请求计数为 0；`/v1/accounts/` 等既有端点的状态码与响应体与未停用时逐字节相同；日志不含夹具 token。（追补 F-09）请求目标：§9.14.3"请求目标固定探针"中网关一组全部通过。
3. **watcher 运行时 diff**（node 测试，不读 YAML，只 require 已提交的 JS 生成物）：`node bridge/services/telegram-watcher/scripts/dump-route-table.js` 输出 watcher 实际加载的身份路由表（W-0 须让 `server.js` 只在 `require.main === module` 时 `listen`，以便无副作用加载 `app`），得到 `{(identity, method, inner_path)}`，与 payload 对称差为空；另枚举 Express 实际注册的 handler，每条 payload inner 路由都有 handler，且不存在 payload 之外的 `/api/*` 或 `/media/*` handler。（WGW-1.0.2 追加）never_allowed：中间件使用的清单与 `PAYLOAD.never_allowed` 是同一对象（手写的 `lib/generated/watcher-routes.js` 删除，仓库内不得另有清单副本）；`NA` 与 `NA_intersects` 通过 §9.14.3 固定探针；gateway 与 snapshot 身份请求 `NA` 命中的探针（含只有 never_allowed 能给出 403 的 `/api/login/anything`，以及 raw `/api/login%2Fstart`）对 GET/HEAD/POST/PUT/DELETE/PATCH/OPTIONS 都得到 403 `identity_forbidden`；browser 身份访问 `/api/login/status`、`/`、`/healthz` 不受影响；用注入合成 gateway 行 `/api/login/x` 的 payload 副本构造中间件必须抛错。（WGW-1.0.2 追补 F-09）请求目标：`path_part` 通过 §9.14.3 固定探针；§9.14.3"请求目标固定探针"中 watcher 一组全部通过，且必须经真实 socket 发送原文请求行（supertest、fetch 等会先规范化 URL，不能代替）。
4. 第 1 项证明 YAML == 生成物，第 2、3 项证明生成物 == 运行时，三项全部为空且比较行数 > 0 → diff 为空。逐行属性（roles、query、body、write、budget）在集合相等后逐字段比较，任何不等都算非空。

**9.14.5 独立负例（T0-4，不从真源生成）**：百分号编码的斜杠与点（`%2F`、`%2e`）、双重编码（`%252F`）、尾斜杠、`//`、大小写（`/V1/Watcher/status`）、重复 query、未列 query、OPTIONS、非媒体 HEAD、未注册 404、未列方法 405；经网关遍历矩阵外请求时 watcher 请求计数不变；用 `gateway` token 直连 watcher 遍历矩阵外端点，第二层同样拒绝；`browser` 调 snapshot、`snapshot` 调网关写行被拒。（WGW-1.0.2 追加）凭据面：经网关请求 `/v1/watcher/config`、`/v1/watcher/login/start`、`/v1/watcher/login/qr/status`、`/v1/watcher/healthz`、`/v1/watcher/index.html`（全部方法）一律 404 且 watcher 请求计数不变；用 `gateway`、`snapshot` token 直连 watcher 请求 `/api/config`、`/api/login/*`、`/api/login/qr/*`、`/`、`/index.html`、`/healthz`（全部方法）一律 403 `identity_forbidden`。（WGW-1.0.2 追补）同样直连，带片段（`/api/config#x`、`/healthz#x`）、绝对形式（`http://x/api/config`）、星号形式（`*`）、含反斜杠（`/api\config#x`）的请求目标一律 403 `identity_forbidden`，`/api/status#x` 为 404 `route_not_found`，handler 计数不变。

**9.14.6 operator-query 的非移动入口（WGW-1.0.3，规范性；审查 wac-094 🟡-1）**

问题：operator-query 的网关按请求路径触发（§9.4），它看不出请求经哪个 Caddy 入口到达（§9.1）。面板的 `handle /v1/*` 把 `/v1/*` 原样转给 8183，并用 `SYSTEM_OBSERVER_TOKEN` 覆盖来访凭据（§9.1）。网关一旦上线（O0-A08），公网上不带任何凭据的 `GET /v1/watcher/dialogs` 等请求就会以 `system_observer` 身份通过 G2、G3，拿到 P0–P2 的全部 11 条读路由（`status`、`trading/messages`、`trading/briefings`、`media/{filename}` 的 GET 与 HEAD、`trading/orders`、`trading/orders/active`、`dialogs`、`trading/accounts`、`trading/channels`、`trading/risks`）。写路由只对 `risk_admin` 开放，observer 得到 403。计划依据：计划 §9 O-0（`:316`）要求"`/media` 与其他公网入口没有绕过"。

- **定义**
  - **触发空间 T**（上游路径的集合）：路径 `p` 满足：`path_part(p)`，或它经一次百分号解码的结果，按 ASCII 不区分大小写等于 `/v1/watcher`，或以 `/v1/watcher/`、`/v1/watcher%` 开头。T 与 §9.4 的触发条件相同。
  - **直连守卫空间 D**：Caddy 对解码并 clean 后的请求路径，用直连守卫正则 `^(?i:/v1/watcher)(?:[/\n%]|$)`（§9.14.3 格式 v2）判定为命中的集合。
  - **operator-query 转发路由**：`caddy adapt` 后的 JSON 中，任一 server、任一嵌套深度的路由，只要其 handler 链含 `reverse_proxy`（含 `forward_auth` 生成的那种），且某个 dial 的端口等于 operator-query 的端口（verify 的 `--upstream` 的端口，现为 8183；按 verify 现行的 `dial_endpoint` 识别，任何 host 写法都算；解析不了按 UNCOMPARABLE 判失败），就属于这一类。
  - **片段转发路由**：`(watcher_gateway_routes)` 的逐路径组（经 `watcher_gateway_upstream` 转发）。
- **入口不变式 I-1**：除片段转发路由之外，任何 operator-query 转发路由都不得把 T 内的上游路径交给 operator-query，**无论它是否注入凭据**。I-1 的范围是 Caddyfile 中的全部站点与 server，不限于 app 站点。jp-24 上 Caddy 以外、能到达 `127.0.0.1:8183` 的代理进程，机器检查看不到，由 PC-6 (iii) 人工确认。
- **引理 L-1（为什么守卫加 G1 就够了）**：设某条转发路由在转发前**不改写路径**，于是 operator-query 收到的 raw path 就是客户端请求的原始路径。网关在认证之前（G1）拒绝 raw path 含 `%`、`//`、点段的请求，所以能通过 G1、进入认证的请求，其原始路径不含这三类形式。对这样的路径，Caddy 的解码结果等于原文，clean 也不改变它，所以守卫正则判定的正是原文。原文以 `/v1/watcher/` 开头（通过 G1 的都是表内路由），属于 D，因此在 Caddy 被守卫回 404，到不了该转发路由。T 中其余的形状（例如 `/v1/watcher/../accounts`、`/v1/watcher/..%2Faccounts`、`/v1/watcher%2e%2e/x`；`/v1/watcherx` 则根本不在 T 内，网关不触发）可能绕过守卫到达 operator-query，但都会在 G1 得到 404 `route_not_found`，先于认证，不发上游请求。**改写路径的转发路由不适用 L-1**：改写可以把 D 以外的请求路径（例如 `/x/v1/watcher/dialogs`，经 `handle_path /x/*`）变成 T 内的上游路径，所以要单独限制（N-3）。L-1 的前提由 §9.4 的承重说明与 B-5 测试锁定。
- **本机实测**（Caddy v2.10.2，O-0 仿生产夹具加格式 v2 片段，另加一个只 import `watcher_gateway_direct_guard`、同样用 `handle /v1/*` 注入 observer 的第二站点；上游换成本机桩，`HOME`/`XDG_*` 指向临时目录，不连 jp-24）。两个站点对以下请求都回 404 空 body，桩计数为 0：`/v1/watcher/status`、`/v1/watcher/dialogs`、`/V1/WATCHER/status`、`//v1/watcher/status`、`/v1//watcher/status`、`/v1/./watcher/status`、`/x/../v1/watcher/status`、`/v1/watcher/media/1700000000000-1.png`（GET 与 HEAD）、`/v1/watcher`、`/v1/watcher/`、`/v1/watcher%2Fstatus`、`/v1%2Fwatcher/status`、`/v1/watcher%0A`、`/v1/watcher%25`、`/v1/watcher%25x`、`/v1/watcher/status?x=1`、`PUT /v1/watcher/trading/accounts/account-a`、`/v1/watcher/login/start`、`/v1/watcher/status#x`。以下请求带着注入的 token 到达桩：`/v1/watcherx`（不在 T 内）；`/v1/watcher/../accounts`、`/v1/watcher/..%2Faccounts`、`/v1/watcher%2e%2e/accounts`（桩收到的 raw path 含点段或 `%`，G1 会返回 404）。`/v1/accounts` 仍按现状被注入并转发。`/m/v1/watcher/status` 以 `/v1/watcher/status` 到达桩，没有注入。同一站点同时 import 两个 snippet 时，adapt 报 `matcher is defined more than once: @wgw_direct`。
- **并入规则（O-0，规范性）**
  - **N-1** app 站点：沿用 F-12，站点顶层、所有 `handle`/`handle_path`/`route` 之前 `import watcher_gateway_routes`。格式 v2 的守卫已经包含在内，不另加 import。
  - **N-2** 其他站点：Caddyfile 中含任何 operator-query 转发路由的其他站点块，都在站点顶层、所有 `handle`/`handle_path`/`route` 之前 `import watcher_gateway_direct_guard`。同一站点不得同时 import 两个 snippet。
  - **N-3** 改写路径的转发路由：对某条非片段的 operator-query 转发路由，同一请求上在 `reverse_proxy` 之前可能执行的改路径 handler，都算它的改写（包括它自身 handle 链中的、外层路由中的，以及同层或外层排在它之前、匹配器可能同时命中的非互斥路由中的；判不了是否同时命中，就算同时命中）。改路径 handler 指 `rewrite` handler 带 `uri`、`strip_path_prefix`、`strip_path_suffix`、`uri_substring` 或 `path_regexp` 中任一字段（`handle_path`、`uri`、`rewrite`、`try_files`、`forward_auth` 都会生成它）。有改写时，只有下面两种白名单形式可以通过：
    - (a) 只有 `strip_path_prefix S`（`handle_path`、`uri strip_prefix`），并且该路由的匹配器只是 `path`，每个模式都是纯 ASCII，不含占位符。模式要么是字面路径，要么只在末尾有一个 `*`（前缀 glob）。把每个模式 `P` 做 `ascii_lower`，要求它以 `ascii_lower(S)` 开头（否则按 UNCOMPARABLE 判失败），令 `H = P[len(S):]`，去掉末尾的 `*`。字面模式要求 `H` 不在 T 内；前缀 glob 要求 `ascii_lower(H)` 既不以 `/v1/watcher` 开头，也不是 `/v1/watcher` 的前缀（`H` 为空或为 `/` 时判失败）。现行的 `@mobile` 块（`/m/v1/accounts`、`/m/v1/operator/orders/*` 等）按这条通过。
    - (b) 只有常量 `uri`（不含 `{`），且其路径部分（`?` 之前）不在 T 内。例：`forward_auth` 生成的 `uri /v1/auth`，以及 `rewrite /healthz /api/status`。
    
    其他情形一律判失败，包括 `strip_path_suffix`、`uri_substring`、`path_regexp` 替换、带占位符的 `uri`、白名单以外的匹配器（`path_regexp`、`expression`、`not`、插件等）、算不出改写结果，以及多个改写叠加。白名单只能经契约修订扩充。审查 wac-094 的 g16、g17、g18 按这条都判失败。
  - **N-4** 不改写路径的转发路由：匹配器可能命中 D 的（按 verify 现行的"判不了就算命中"），必须排在守卫之后：同一个 host 的路由链中，`wgw_direct` 守卫路由位于同一列表的更早位置，或位于某个外层列表中、排在包含它的那条路由之前。排在守卫之前、又不满足 N-3 的，判失败。面板的 `handle /v1/*`，以及站点顶层不包在 `handle` 里的 `reverse_proxy /v1/* …`（`reverse_proxy` 指令排在 `handle` 之后，守卫的 `respond` 会终结请求），按这条都通过。
- **检查（由 Release Steward 实现，见 §9.16 WGW-1.0.3 的 RS 清单）**
  - **V-1 verify 静态检查**（全部 server）：逐条列出 operator-query 转发路由，每条输出一行 `DIRECT_ENTRY`：server、站点 host、trail、匹配器摘要、改写摘要、`Authorization` 操作（只写头名与 `{env.NAME}` 占位符名；字面值一律写成 `<literal len=N>`）、判定。然后按 N-2、N-3、N-4 判定，任一违例输出 `DIRECT_ENTRY_FAIL`，verify 失败。列出的行同时是 PC-6 的机器底稿。
  - **V-2 verify 模拟**：对含 operator-query 转发路由的每个（server，host）组合（host 取自 host 匹配器；没有 host 匹配器的站点用 `--host`），模拟探针集 `P_D`。`P_D` 包括：`/v1/watcher`、`/v1/watcher/`、`/v1/watcher/status`、`/V1/WATCHER/status`、`/V1/Watcher/Status`、`//v1/watcher/status`、`/v1//watcher/status`、`/v1/./watcher/status`、`/x/../v1/watcher/status`、`/v1/watcher/config`、`/v1/watcher/login/start`、`/v1/watcher/healthz`，以及清单每行去掉 `/m`、参数取 `x` 的路径（逐一用该行的全部方法，另加 GET；媒体行另加 HEAD）。每个探针都必须由 `wgw_direct` 路由的静态 404 应答，不到达任何上游。反向对照：`GET /v1/watcherx` 不得由守卫应答（证明守卫不比规定的宽）。百分号形式不进 `P_D`（模拟器按解码后的路径工作，这类形式只在 V-3 活体检查）。
  - **V-3 探针活体检查**（本机 Caddy，上游全部是桩）：对含 operator-query 转发路由的每个 host，都带该 host 的 `Host` 头发送 `P_D`，外加 `/v1/watcher%2Fstatus`、`/v1%2Fwatcher/status`、`/v1/watcher%0A`、`/v1/watcher%25`、`/v1/watcher%25x`、`/v1/watcher/status?x=1`，以及清单每行去掉 `/m` 后，参数逐个取 `PROBE_PARAM_VALUES` 的路径与它的 `//` 变体（该行的全部方法）。期望：状态码 404、body 长度 0、operator-query 桩与 watcher 桩计数都不变。**残余形状**：`/v1/watcher/../accounts`、`/v1/watcher/..%2Faccounts`、`/v1/watcher%2e%2e/accounts` 可以到达桩，但桩收到的 raw path 必须含 `%`、`//` 或点段（即 G1 会拒绝）；不含就判失败。另记录（不作断言）`GET /v1/accounts` 是否到达 operator-query 桩、到达时是否被加了 `Authorization`（只输出 `injected=yes|no`），作为 PC-6 与范围外告知的证据。已有检查不变，包括 `/m/v1/watcher/status` 以 `/v1/watcher/status` 到达桩、且不被注入。
  - **V-4 caddyfile-check（文本）**：N-1 沿用 F-12。N-2：文本中含指向 operator-query 端口的 `reverse_proxy` 或 `forward_auth` 的每个站点块，都必须在站点顶层、所有 `handle`/`handle_path`/`route` 之前 import 两个 snippet 之一；import 了两个的判失败。上游写成占位符、无法识别端口的，判 UNCOMPARABLE。
  - **V-5 生产公网检查**（只读；不带 `Authorization`、不带 Cookie；只输出状态码、`size_download`、`content_type`，**不落盘、不打印 body**）。对 jp-bot 站点，以及 PC-6 列出的、其他转发到 8183 的站点主机名，发送 `GET /v1/watcher/status`、`GET /V1/WATCHER/status`、`GET //v1/watcher/status`、`GET /v1/watcher/dialogs`、`HEAD /v1/watcher/media/1700000000000-1.png`，期望全部为 404 且 `size_download == 0`（守卫的空 404）。网关上线之前，没有守卫的路径会得到 operator-query 的 FastAPI JSON 404（`{"detail":"Not Found"}`）；上线之后会得到网关的 JSON 或 200。所以 body 长度为 0 能把守卫与这两种情况区分开。执行点：
    - O0-A05 的 `--phase verify`（Caddy 重启之后）：不通过即 verify 失败，按 O0-A05 verify 失败的现行处置办理；修好并重新通过之前，不得申请 O0-A08。
    - O0-A08 的 O-1 preflight：不通过即输出 `DIRECT_GUARD_MISSING`，阻断阶段 O，不写任何东西。
    - O0-A08 的 O-2 apply：重启 operator-query 之后、在自动回滚窗口内检查。不通过说明匿名读已经暴露，按重启之后的失败处理：自动回滚（O-4）。
    - O0-A08 的 O-3 verify：并入现有检查（现有的"公网 `/m/v1/watcher/status` 无 token 401"保留）。
    - 以后任何 Caddyfile 变更之后，都建议重跑 V-5（只读，但仍须单独授权）。
- **PC-6（探针前确认项，O0-A05P 授权记录的一部分；契约依据为 I-1、N-2、N-3）**：放副本之前逐项确认，任一项答不上来就先停。
  - (i) 候选 Caddyfile 中**全部**把请求转发到 8183 的块（任何 host 写法；包括面板 `handle /v1/*`、`handle_path`、`route`、站点顶层 `reverse_proxy`、`forward_auth`、其他站点与其他 server 中的），逐块写明：所在站点地址、指令与匹配器原文、是否改写路径（改成什么）、是否设置、覆盖或删除 `Authorization`（只写占位符名，字面值写成 `<literal len=N>`）。来源是 O0-A01 S-02 大纲、L-A5 的"面板注入块的匹配器"，以及 `o0_caddy_watcher_routes.py inventory` 与 V-1 的 `DIRECT_ENTRY` 行，不需要新的生产动作。
  - (ii) 每个含这类块的站点，按 N-1、N-2 import 了哪个 snippet，写在什么位置。
  - (iii) jp-24 上 Caddy 以外，是否还有进程把外部请求代理到 `127.0.0.1:8183`（例如别的反向代理、SSH 隧道、端口转发）。由用户确认；S-06 只证明 8183 只监听回环，证明不了没有本机代理。
- **U-13（用户确认项，O0-A08 的硬前置；契约依据为本小节）**：(i) 用户确认生产 jp-bot 站点面板 `/v1/*` 块的真实写法（是否无条件注入 `SYSTEM_OBSERVER_TOKEN`），以及 PC-6 (i)–(iii) 的结果；(ii) 用户认可本小节的修法：Caddy 直连守卫加 I-1 的机器检查（V-1 至 V-5），网关与角色 scope 不变。(iii) 用户决定是否另外采用下面"方案比较"中的 (c′)（默认不采用）。**WGW-1.0.3 的实现经审查合入之前，U-13 (i)、(ii) 确认之前，O0-A05 的 V-5 通过之前，都不得申请 O0-A08。**同时，O0-A05P 要等这批实现合入之后才申请：片段改为格式 v2 会改变 `snippet_sha256`；如果 PC-6 显示有其他站点，候选 Caddyfile 还要加 N-2 的 import，`candidate_sha256` 也会变。
- **方案比较与裁定依据**
  - **(a) Caddy 侧对非 `/m` 入口的 `/v1/watcher` 前缀回 404**。**采纳**，写法是守卫组进片段、另附独立 snippet，并配 I-1 与 V-1 至 V-5。理由：(1) 网关看不出请求来自哪个入口，所以边界只能画在能看到入口的地方，也就是 Caddy；(2) 守卫由真源生成、随片段做逐字节 diff，F-12 已经规定了 import 位置，app 站点的候选 Caddyfile 不需要为守卫改动；(3) 按 L-1，守卫加上现有 G1 就能覆盖全部不改写路径的转发，改写的情形由 N-3 的白名单机器判定；(4) 不改任何接口、角色、token 或网关代码；(5) 在网关上线（A08）之前就能用 V-5 在生产上验证（区分空 404 与 FastAPI JSON 404）。"只收窄面板匹配器"（`not path_regexp …`）不作为主修法：它是逐站点手写的，不来自真源，漏改一处就重新暴露。不过它满足 I-1 时不禁止。
  - **(b) 网关只接受经 `/m` 入口到达的请求（Caddy 在 `/m` 入口注入专用头，网关校验）**。**否决**。不带秘密的标记头，公网调用方可以自己加上；面板 `/v1/*` 会把它原样转发，再替它注入 observer。要堵住，就得让每个非 `/m` 入口都删掉这个头。这与 (a) 一样要逐站点改 Caddy，而且失败方向更差：漏删一处，就又是匿名读。带秘密的标记头则是一把新的服务端凭据（要生成、互异校验、轮换、放进 Caddy env），违反计划 §2.1"不新增 token"。此外，§9.2 拓扑、§9.3 的 G 步都要改。
  - **(c) 网关拒绝 `system_observer` 角色**。**不采纳（超出 Architect 权限）**。计划 §2.1（`:80`）规定读端点四角色都放行，§4 T0 验收（`:159`）要求"`system_observer` 与 `reviewer` 可读 status"。O-2、O-3 也用 `SYSTEM_OBSERVER_TOKEN` 做经网关的 200 检查。面板前端不调用 `/v1/watcher/*`（§9.1），所以面板没有正当需求，这一点不构成阻碍。但 (c) 保护的依据是"代理恰好注入了哪把凭据"，而不是"调用方有没有出示凭据"：面板改为注入 `viewer`，或别的入口注入别的角色，暴露就会回来。
  - **(c′) 只拒绝静态 `SYSTEM_OBSERVER_TOKEN`**（`kind=reader`、`role=system_observer`、`session_subject is None`；会话 token 中的 `system_observer` 照常放行）。它可以作为 (a) 之外的第二层：日后若有人手改 Caddy 又没跑 verify，面板注入的仍是这把 token，这一层还能挡住。它同样要改计划的四角色读，并把 O-2、O-3 的 200 检查换成 `REVIEWER_TOKEN`（或 `VIEWER_TOKEN`）。**是否采用由用户决定（U-13 (iii)），WGW-1.0.3 不采用。**用户采用时，先修订计划，再由 Planner 召回 Architect 出 WGW-1.0.4（§9.3 映射表加一行 403 `insufficient_scope`、B 清单加测试）。
  - **(d) 组合（纵深防御）**：本次采纳的是 (a) 加上三处独立检查：静态与模拟（V-1、V-2）、本机活体（V-3）、生产公网（V-5）。另有网关侧的 B-5 测试把 L-1 的前提（G1 先于认证、`%`/`//`/点段规则）锁成回归测试。角色层的第二道防线 (c′) 留给用户决定。
  - **(e) 网关单独占一个监听端口，只有片段的上游拨这个端口**。这在结构上最强：误把请求转给 8183 也到不了网关。但它要新增 systemd 单元或监听、改 §9.2 拓扑、§9.7 的 worker 与槽位核算、S-06 端口检查和整个阶段 O，超出勘误范围。记为以后的选项，本次不写入。
- **残余风险**
  1. 部署之后有人手改 Caddyfile、又没跑 verify 与 V-5，可能重新引入绕过。缓解：本小节要求任何 Caddyfile 变更后重跑 V-5；可选的 (c′) 可以兜住面板这一种形状。
  2. Caddy 之外的本机代理（PC-6 (iii)）不在机器检查范围内，只能靠用户确认。
  3. 自带合法凭据的内部调用方（Hermes、运维脚本）按四角色读能访问 `/v1/watcher/*`，这是设计如此（计划 §2.1），不属于本问题。
  4. 守卫的 404 没有 §9.5 结构体，与兜底相同。app 只走 `/m`，正常使用不会碰到。
  5. T 中绕过守卫的残余形状（L-1），安全性完全依赖 G1 的 `%`/`//`/点段规则先于认证。B-5 与 V-3 的"残余形状"检查锁定这一点；放宽 G1 必须按 §9.4 的承重说明重新论证。
  6. 面板 `/v1/*` 对其他 `/v1/*` 端点的匿名 observer 读取不在本小节范围内，见 §9.15"范围外告知"。

### 9.15 裁决状态、召回待办与执行期注意事项

**已裁决**（Planner 2026-09-26，依据审查 wac-001 复核；条件已写进正文）：

| # | 内容 | 裁决 | 条件落点 |
|---|---|---|---|
| A-1 | `/healthz` 要求 `browser` 代理凭证，容器健康检查带该头 | 接受（有条件） | §9.2：node 脚本在脚本内读 `process.env`，不经 compose `$VAR` 插值，不打印响应 |
| A-2 | 网关每 worker 8 槽 = config 4 + media 4；snapshot 独立 1 槽不计入 | 接受 | §9.7、YAML `budgets` |
| A-3 | watcher 503 `db_busy` / `media_too_large` 透传给 app（对"上游 5xx 一律 `watcher_unavailable`"的收窄例外） | 接受（有条件） | §9.5：app 把未识别的 503 `code` 一律当作 `watcher_unavailable`；§9.6：只透传 `code`/`message`/`details` 并按 R8 过滤 |
| A-4 | `default_risk_ratio` 与品种风险同样收紧到 `(0, 0.1]`（`read_api.py:8510-8512`） | 接受（有条件） | §9.12 R7 只校验提交的键；§9.11 O-0 数据基线列出存量越界或 NULL 的行 |
| A-5 | DELETE 的 `client_ref`/`expected_revision` 放 JSON body；`config_audit.token_fingerprint` | 接受 | §9.12 |
| A-6 | P3 价格提醒字段依赖计划 §7.1 表扩展 | WGW-1.0.2 召回完成：字段与身份作用域已定，P3 仍不启用（R13）；站点提醒 app 能否删除**待 Planner 确认** | §9.12.1 |
| A-7 | 空数组 `groups` 合法（停止监听全部群组） | 接受（有条件） | app 必须二次确认并写明"将停止监听全部群组"；T2-5 覆盖空选路径；app 截断到 100 条时不丢已选（审查 💭-6，A-2 验收） |
| A-8 | 快照 `invalid` / `401` 何时拒绝开仓 | R1：立即拒绝 | §9.11 |

**WGW-1.0.2 写入的裁定**（Planner 2026-09-26；用户裁决 2026-09-26）：

| # | 内容 | 落点 |
|---|---|---|
| R10 | 秘密键判定先查非 ASCII，含非 ASCII 的键一律按秘密处理，两层同义 | §9.4 `is_secret_key`、§9.6 |
| R11 | 网关生成物加载失败只停用 `/v1/watcher/*`（503 `gateway_disabled`），operator-query 不退出 | §9.2、§9.3、§9.14.3"网关加载期失败的处理"（追补 F-11） |
| R12 | `/v1/query/channel-route` 在快照开关打开时归为开仓依赖 | §9.11 |
| R13 | `phase_max = P2`，不注册 P3；测试不写死 P3 与总行数 | §9.4 阶段门、§9.12.1 P3 启用条件 |
| R14 | app 连接失效 = HTTP 401 或 `invalid_token`，统一用 `isConnectionInvalid` | §9.5 |
| 用户裁决 | Caddy 清单中路径参数按"恰好一个非空段"，锚定 `path_regexp` | §9.4、§9.14.3 |

**待确认**（WGW-1.0.2；不阻塞 P0–P2）：

- **A-6 余项：站点创建的提醒（`source = 'watcher'`）app 能否删除**。契约按推荐写定为"不能，也看不到"（§9.12.1，含备选 B/C 与各自需要改动的位置）。待 Planner 确认；若改选 B 或 C，召回 Architect 出 WGW-1.0.3。

**WGW-1.0.3 待用户确认**（审查 wac-094 🟡-1；Planner 转达，任何 Agent 的转述都不算确认）：

- **U-13**（O0-A08 的硬前置）：(i) 面板 `/v1/*` 的真实写法与 PC-6 (i)–(iii)；(ii) 认可 §9.14.6 的修法（(a) 加 V-1 至 V-5，网关与角色不变）；(iii) 是否另外采用 (c′)（默认不采用；采用需先修订计划，再出 WGW-1.0.4）。依据与前置顺序见 §9.14.6。
- **PC-6**（O0-A05P 的探针前确认项）：见 §9.14.6。

**范围外，但需告知用户**（WGW-1.0.3 不处理，也不改 §1–§8；由用户判断是否另开任务）：

- **可能的现存暴露**：如果生产面板的 `handle /v1/*` 确实如 §9.1 所载，无条件注入 `SYSTEM_OBSERVER_TOKEN`，并且站点在它前面没有别的认证层（站点级 `basic_auth`/`forward_auth`、上游的访问控制、IP 白名单等），那么**现在**公网上不带任何凭据，就能以 `system_observer` 身份读取 `/v1/accounts` 等全部对 reader 开放的 `/v1/*` 只读端点（账户、持仓、订单、成交、风控状态等交易数据）。这与网关无关，今天就可能存在。仓库文档（§9.1）只记载了注入，没有记载前面有没有认证层，所以这一点**尚未证实**。Agent 没有、也不会自行核实。
- **获得用户授权后的只读核实方法**（二选一，都不改任何状态）：
  1. **看配置，不发请求**：从 O0-A01 的 S-02 大纲与 L-A5（或 PC-6 (i) 的记录）读出 jp-bot 站点中，面板 `/v1/*` 块之前有没有 `basic_auth`、`forward_auth` 等认证指令，以及全局或站点级的访问限制。这一步不需要新的生产动作。
  2. **公网只读请求一次**（需要用户明确授权这一条）：在任意一台外部机器上，不带 `Authorization`、不带 Cookie，执行 `curl -s -o /dev/null -w '%{http_code} %{content_type} %{size_download}\n' https://jp-bot.balen.wang/v1/accounts`。**body 不落盘、不打印**（`-o /dev/null`），因为其中就是交易数据。结果为 `200 application/json` 且长度大于 0，即证实匿名可读；401、403 或登录页，说明前面有认证层。只发这一个 GET，不枚举其他端点，不带任何 token。
- **若证实**：修法属于面板与控制面鉴权的范围，不属于本契约。可选方向由用户裁决，例如：面板 `/v1/*` 改为透传浏览器自己的登录凭据，而不是注入 observer（`docs/operations/trading-reliability-cutover.md:7` 已说明控制面能校验登录服务签发的 JWT）；或者在站点前加认证层。无论哪种，都会改变面板的现有行为，需要单独立项与授权。WGW-1.0.3 的守卫只挡 `/v1/watcher` 前缀，不改变其他 `/v1/*` 的现状。

**执行期注意事项**（不改契约，由对应任务书写进验收）：

- 审查 💭-1：`secret_key_pattern` 未锚定，按"键名包含"判定，`api[_-]?id` 会命中 `rapid_*` 之类的键。误伤只会多拒、多删（安全方向）。将来加列时注意命名。
- 审查 💭-7：review P1-16 提到的"全局/每 token 带宽预算"，计划 §2.1 未采纳，本契约也不写，按计划为准。
- 审查 💭-6：`groups` 没有条件写，站点与 app 同时编辑会互相覆盖（计划 §2.3 已接受：可重入，以回读为准）。
- 打包白名单与部署门禁见 §9.14.3（watcher 镜像、控制面平铺发布）。
- R5 的跨语言差异（WGW-1.0.2 按 R10 修正，替换 WGW-1.0.1 "两边同义"的说法）：单看 `secret_key_pattern`，Python `re.IGNORECASE` 与 JS `iu` 对非 ASCII 字符的大小写折叠是否完全一致，契约不作保证；判定同义靠的是 `is_secret_key` 先把含非 ASCII 字符的键一律判为秘密，正则只处理纯 ASCII 键名，在这个范围内两者相同。S-20(g) 的固定探针继续锁定正则本身的现状，作为附加检查。
- Caddy 兜底返回的 404 没有 §9.5 结构体；app 只调用清单内的路径与方法，正常使用不会碰到。O-0 若希望兜底也返回 JSON，需要另行裁定，不在本契约内。

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

**WGW-1.0.1 → WGW-1.0.2（2026-09-26）**。来源：执行者 wac-015 停工报告（never_allowed 不在生成物中）、Planner 裁定 R10–R14、用户裁决（2026-09-26，Caddy 单段语义）、审查报告 `docs/agent-team/reviews/wac-001.md`（🟡-7）、`wac-009.md`（🟡-2、💭-1）、`wac-016.md`（🔴-2）、`wac-026.md`（🟡-1、💭-1）、`wac-013.md`（🟡-3）、`wac-027.md`（🟡-1、🟡-2）、`wac-007.md`（🟡-1）；审查编号按报告文件名（即被审任务号）引用。§9 的接口都还没有部署，本次改动不涉及已发布接口；§1–§8 未改动；既有 `/v1` 端点的字段、状态码与语义不变。

| # | 任务项 | 来源 | 改动 | 位置 |
|---|---|---|---|---|
| F-01 | 1 | wac-015 停工报告；E-06；审查 wac-009 🟡-2、💭-1 | `never_allowed` 进入两份代码生成物的 payload（全部条目、原顺序，摘要覆盖），两份 Caddy 生成物不含它。规范化 `NA`、`NA_gw`、`NA_intersects` 三个算法与固定探针；两个消费方加载期断言（watcher 抛错退出，网关按 R11 停用）。watcher：对 browser 以外的身份，在路径规范化与身份路由表之前判定，命中 403 `identity_forbidden`，与方法无关（因此 gateway 直连 `/` 由 404 统一为 403，消解 wac-009 💭-1 指出的两条规则冲突）。网关：前缀中间件在路由匹配前按 `NA_gw` 返回 404，另有构建期与转发前两道检查。S-18 改用 `NA_intersects`；新增 S-21；§9.14.4 第 2、3 项与 §9.14.5 加 never_allowed 用例 | §9.2 第 5 步与 W3、§9.3 G1、§9.4、§9.14.1、§9.14.3、§9.14.4、§9.14.5；YAML `never_allowed` 注释、`invariants` |
| F-02 | 2 | R12；审查 wac-013 🟡-3 | `/v1/query/channel-route` 在快照开关打开时归为开仓依赖：非 fresh → 503 `snapshot_unavailable`，不返回 stale 旧值、不回落 SQLite；数据层拒因与鉴权顺序不变；开关关闭时不变 | §9.11 |
| F-03 | 3 | R14；审查 wac-027 🟡-1、🟡-2 | 连接失效 = HTTP 401 或 `invalid_token`（含 403），统一由 `isConnectionInvalid` 判定；删除 A-3 条款中"401 是唯一触发状态"的矛盾表述；非 JSON 或缺 `code` 的 503 归入 `watcher_unavailable`，写请求标记为结果未知 | §9.5 |
| F-04 | 4 | 用户裁决 2026-09-26；审查 wac-026 🟡-1 | Caddy 中 `{param}` = 恰好一个非空段，用锚定、区分大小写的 `path_regexp`（`[^/]+`），禁用 `*`。清单改为格式 v2（`<template> <regex> <methods>`）；新增生成物 `contracts/generated/caddy-watcher-gateway.caddy`（snippet `watcher_gateway_routes`，每路径一组 `path_regexp` + `method` 与 `handle`，末尾兜底 404）；O-0 并入规则与核对要求；check 脚本加独立 Caddy 探针；新增 S-22 | §9.4 路径、§9.14 入口表、§9.14.1、§9.14.3、§9.14.4 第 1 项 |
| F-05 | 5 | A-6 召回；审查 wac-001 🟡-7、wac-016 🔴-2、wac-026 💭-1；R13 | 价格提醒表扩展（`source`、`account_id`、`position_ref`、`environment`、`triggered_at`、`delivered_at`）与四元组身份；gateway 列表始终 `source = 'v3'`、删除按 id + 三元组 + `source = 'v3'` 限定（不符 404）；browser 语义；站点提醒 app 不可见、不可删（推荐，**待 Planner 确认**，附备选）；P3 启用条件（含同时改三处写死的 P2）。**唯一改动的已冻结路由行字段**：`gw.price_alert.delete` 的 `body.allow`/`body.required` 由 `[client_ref]` 改为 `[account_id, position_ref, environment, client_ref]`，这是 A-6 身份限定删除的必要条件；该行为 P3，当前不注册。定义层新增 `query_params.source.gateway_enum: [v3]`。新增 S-23；S-11 覆盖 query 的 `gateway_enum`。`phase_max` 仍为 P2 | §9.12.1、§9.4 阶段门、§9.14.1、§9.15；YAML `query_params.source`、`gw.price_alert.delete` |
| F-06 | 6 | R10（审查 wac-007 🟡-1） | 定义谓词 `is_secret_key` = 含非 ASCII 或正则命中，请求拒绝（另加 `secret_fields`）与响应删除共用；§9.6 与 §9.15 中"Python 与 JS 同义"改为"谓词同义，正则只处理 ASCII 键名"；S-10 同步 | §9.4、§9.6、§9.14.1、§9.15 |
| F-07 | 随 F-01 | R11 | 网关生成物加载失败（含 F-01 的加载期断言）只停用 `/v1/watcher/*`，operator-query 不退出 | §9.2 |
| F-08 | 自查 | — | §9.1 补充事实（生成器排除 never_allowed、Caddy `*`、watcher 现行 never_allowed 位置、网关秘密谓词、channel-route 调用链、`price_alerts` 现表、app `isConnectionInvalid`、写死 P2 的三处）；§9.15 增"WGW-1.0.2 写入的裁定"表与 Caddy 兜底 404 形状说明 | §9.1、§9.15 |

生成物影响：YAML 字节变化使 `yaml_sha256` 改变，且 payload 新增 `never_allowed`、Caddy 清单改格式、新增 Caddy 片段，因此已提交的生成物必须由生成器按 WGW-1.0.2 重新生成（`phase_max = P2` 不变），由 wac-015b 实现；在此之前 `check_watcher_gateway_routes.py` 对 WGW-1.0.2 的真源报差异属于预期。

**WGW-1.0.2 合并前追补（2026-09-26，版本号不变）**。来源：审查报告 `docs/agent-team/reviews/wac-049.md`（勘误内容 PASS，0 🔴）的 🟡-1..🟡-4，Planner 决定在 WGW-1.0.2 合并前写入（任务 wac-058）。WGW-1.0.2 尚未合入集成分支，也没有任何实现按它上线，因此不另起版本号。§1–§8 未改动；YAML 路由行（P0–P3）零改动，只改注释与 `invariants`（不进 payload）；既有 `/v1` 端点的字段、状态码与语义不变。

| # | 来源 | 改动 | 位置 |
|---|---|---|---|
| F-09 | 审查 wac-049 🟡-1 | 定义 `path_part`（截到第一个 `?` 或 `#`，只作用于未解码的请求目标）。watcher 对非 browser 身份在 `NA` 之前加两项检查：5a 请求目标不以 `/` 开头（绝对形式、authority 形式、`*`）→ 403；5b 路径部分含 `0x21`–`0x7E` 以外的字符或 `url.parse` 会改写的十个字符（`\` 等）→ 403；`NA` 的输入改为路径部分；第 6 步规范化新增"目标含 `#` → 404"。"这一步不依赖身份路由表"改为以输入界定并写明 403/404 的分界（`//index.html`、`/./index.html` 由规范化返回 404）。网关：触发条件、`NA_gw` 与路由匹配都作用于路径部分，raw path 不以 `/` 开头或含 `#` → 404；写明 uvicorn 两种实现对绝对形式的差异及为何不影响判定。新增"请求目标固定探针"（watcher 与网关两组），`NA` 探针补 `//`（审查 💭-2）；§9.14.4 第 2、3 项与 §9.14.5 引用它们；§9.1 补 `auth.js:157`、parseurl 回退与实测、网关现行触发位置 | §9.1、§9.2 第 5–6 步与 W3、§9.3 G1、§9.4、§9.14.3、§9.14.4、§9.14.5；YAML 头注释、`never_allowed` 注释、`invariants` |
| F-10 | 审查 wac-049 🟡-2 | 兜底正则由 `^(?i:/m/v1/watcher)(?:/\|$)` 改为 `^(?i:/m/v1/watcher)(?:[/\n]\|$)`，使 Python 与 RE2 对任意输入同义（Python `$` 在末尾换行前也成立，RE2 不成立；两者没有可移植的"仅文本末尾"锚点）；删除"对本语法子集与 RE2 同义"的笼统说法，改为：行级正则只在 `re.fullmatch` 下同义、禁止用 `re.search`/`re.match` 检查行级正则，兜底正则在 `re.search` 下同义；Caddy 探针补换行结尾与 `#` 用例，兜底正则逐字断言；写明兜底的覆盖范围 | §9.14.3 兜底正则、片段示例、行为说明；§9.14.4 第 1 项 |
| F-11 | 审查 wac-049 🟡-4；R11 | 网关加载期失败统称"生成物停用态"并列出四类来源（导入失败、摘要不符、never_allowed 加载期断言、构建路由时的其他一致性检查）；异常由网关模块自己捕获，`create_app()` 不抛错，进程不退出；停用态下前缀中间件触发即 503 `gateway_disabled`，先于 G1–G8；与 G5 的顺序差别；§9.14.4 第 2 项的停用态测试扩为四种注入 | §9.1、§9.2、§9.3、§9.5 `gateway_disabled` 行、§9.14.3、§9.14.4 第 2 项、§9.15 R11 行 |
| F-12 | 审查 wac-049 🟡-3 | O-0 并入规则：`import watcher_gateway_routes` 必须写在站点块所有 `handle`/`handle_path`/`route` 之前（Caddy v2.10.2 路由排序依据），不得包进 `route`；核对脚本加遮蔽检查（无法评估的匹配器判失败）；本机 Caddy 探针必须用生产 Caddyfile 副本并确认请求到达 operator-query 桩 | §9.14.3 O-0 并入规则 |

未处理（不在本次追补范围，留给后续召回）：审查 wac-049 💭-1（S-21 显式排除 `.`/`..` 段）、💭-3（§9.5"所有屏幕与服务"措辞限定为 watcher 服务层）。

追补对生成物与实现的影响（交 wac-015b）：YAML 字节再次变化，`yaml_sha256` 随之改变；payload 内容不变（改动只在注释与 `invariants`）；Caddy 片段的兜底行改为新正则；其余要求见 F-09、F-11 的正文位置。

**WGW-1.0.2 合并前追补二（2026-09-26，版本号不变）**。来源：审查报告 `docs/agent-team/reviews/wac-058.md`（审查任务 wac-064；契约文本 PASS，0 🔴）的 🟡-1..🟡-3，Planner 决定在 WGW-1.0.2 合并前写入（任务 wac-069）。只改本文件 §9 的正文文字；§1–§8 未改动；YAML 零改动（字节摘要仍为 `85eb4c88…`，与 e391a56 相同），payload 不变，生成物不需要因本条重新生成；既有 `/v1` 端点的字段、状态码与语义不变。

| # | 来源 | 改动 | 位置 |
|---|---|---|---|
| F-13 | 审查 wac-058（wac-064）🟡-1、🟡-2、🟡-3 | 三处文字修正。**(1)** F-09 正文"5a–5c 都不命中、仍能到达凭据面或入口的只剩含 `//` 或点段的形式"不完整。改为"`pp` 含 `//`、点段或 `%`"，并说明原因：express.static 先解码再解析路径，`NA` 解码一次后按字面比较、不归一化。第 6 步写明 `%`、`//`、点段三条规则承担凭据面防护，不得单独放宽。"请求目标固定探针"的 404 组加入 `/%2e/index.html`、`/%2E/`、`/%2Findex.html`、`/.%2findex.html`、`/x/%2e%2e/index.html`，由第 6 步的 `%` 规则拒绝，handler 与 static 计数为 0。**(2)** O-0 并入规则补写：import 位置管不到默认指令顺序中排在 `handle` 之前的指令（`directives.go:47-84`，含 `rewrite`、`uri`、`method`、`try_files`、`redir`、`basic_auth`、`forward_auth`、`request_header` 等），也管不到全局 `order` 选项；O-0 须人工核对并记录这两类。遮蔽检查改为两步：先判定路由是否命中探针，命中后再按 handler 类型判定是否遮蔽。白名单为 `encode`、不带 `request` 的 `headers`、`vars`、`map`、`log_append`、`tracing`，且路由不得带 `terminal` 或 `group`；白名单外的类型与未知类型判失败；白名单只能经契约修订扩充。**(3)** 更正"`/m/v1/watcher/x/../status` 会通过 Caddy，再由网关返回 404"：`uri strip_prefix` 会先对未解码路径做 clean，字面点段与 `//` 以 clean 后的合法路由转发（例如 `/v1/watcher/status`，网关正常处理），这不是安全绕过；百分号编码形式与 `#`（Go 编码为 `%23`）原样转发，由网关的 `%` 规则返回 404。经 Caddy 的探针不得对字面点段与 `//` 期望 404；O-0 本机探针加跑这两类用例 | §9.2 第 5 步末段与第 6 步；§9.14.3 请求目标固定探针（watcher 组 404）；§9.14.3 O-0 并入规则（新增"import 位置管不到的指令"一条、遮蔽检查、本机 Caddy 探针、行为说明） |

对 wac-015b 清单（审查 wac-058 第 8 节）的增量：第 6 项 watcher 测试的"🟡-1 的三个编码形式 → 404"扩为上列五个目标，并入"请求目标固定探针"watcher 组，经真实 socket 发送，覆盖 gateway 与 snapshot 两种身份和七种方法。其余各项不变。第 16 项（O-0，wac-060）按本条 (2)(3) 执行：遮蔽检查使用白名单规则，人工记录 `order` 选项与顶层前置指令，本机探针加跑两类用例，且不对字面点段与 `//` 期望 404。

**WGW-1.0.2 → WGW-1.0.3（2026-09-28）**。来源：审查报告 `docs/agent-team/reviews/wac-094.md`（审查任务 wac-095）的 🟡-1（面板 `/v1/*` 等非移动入口把 `/v1/watcher/*` 连同注入的 observer token 交给 operator-query）、同报告 §9 的 PC-6 与 U-13 建议；任务 wac-096。§9 的接口仍未部署。**§1–§8 未改动；YAML 零改动**（`contract_version` 字段仍为 `WGW-1.0.2`，`yaml_sha256` 不变，已冻结的路由行 P0–P3 全部不变）；网关代码、角色 scope、结构码不变；既有 `/v1` 端点的字段、状态码与语义不变。唯一对外可见的变化：经 Caddy 非 `/m` 入口请求 `/v1/watcher` 前缀，由现在的 operator-query FastAPI JSON 404（`{"detail":"Not Found"}`）变为 Caddy 的空 body 404。`/v1/watcher/*` 从来不是既有端点，所以这不属于 §9.13 所说的破坏性变更。

| # | 来源 | 改动 | 位置 |
|---|---|---|---|
| G-01 | 🟡-1 | 定义触发空间 T、直连守卫空间 D、operator-query 转发路由与片段转发路由；入口不变式 I-1（除片段外，任何转发路由都不得把 T 内的上游路径交给 operator-query，无论是否注入凭据；范围是全部站点与 server）；引理 L-1（不改写路径的转发路由，靠守卫加 G1 就足以覆盖）；计划依据为计划 §9 O-0"其他公网入口没有绕过" | §9.14.6 |
| G-02 | 🟡-1 修法 | Caddy 片段改为格式 v2：`(watcher_gateway_routes)` 在兜底组之后追加直连守卫组 `@wgw_direct`（`^(?i:/v1/watcher)(?:[/\n%]|$)` → `respond 404`）；另定义 `(watcher_gateway_direct_guard)`，只含同一守卫组。同一站点不得同时 import 两者。S-22 增加"匹配器名不等于 `wgw_direct`"。清单与代码生成物不变 | §9.14 入口表、§9.14.1 S-22、§9.14.3 格式 v2 |
| G-03 | 🟡-1 修法 | check 脚本的片段形状断言改为 v2；守卫正则按 `paths` 独立拼出并逐字断言；`re.search` 匹配与不匹配探针（本机 Python 与 Go `regexp` 逐条一致）；两个前缀正则互不相交；"G1 ⇒ D"独立预言（清单每行按 `gateway_pattern` 取值后必被守卫命中） | §9.14.4 第 1 项 |
| G-04 | 🟡-1；审查 wac-094 g18、g19 | O-0 并入规则 N-1..N-4：app 站点沿用 F-12 的 import（守卫已含在内）；其他含 operator-query 转发的站点 import `watcher_gateway_direct_guard`；改写路径的转发路由只允许两种白名单形式（字面或前缀 glob 的 `path` 加 `strip_path_prefix`，且剥后头部与 T 不相交；常量 `uri` 且不在 T 内）；不改写的转发路由必须排在守卫之后 | §9.14.6、§9.14.3 O-0 并入规则 |
| G-05 | 🟡-1；🟡-2 的 g16、g17 | 检查 V-1（verify 静态，全部 server，输出 `DIRECT_ENTRY` 行）、V-2（verify 模拟 `P_D`，含反向对照）、V-3（探针活体：`P_D`、百分号形式、参数取值与 `//` 变体，残余形状只允许以 G1 必拒的形式到达桩，另记录 `/v1/accounts` 是否被注入）、V-4（caddyfile-check 的 import 规则） | §9.14.6 |
| G-06 | 🟡-1 修法（O-3 公网检查） | V-5 生产公网只读检查：五个请求不带凭据，期望 404 且 body 长度为 0。执行点为 O0-A05 verify、O0-A08 的 O-1（`DIRECT_GUARD_MISSING` 阻断）、O-2（自动回滚窗口内，不通过即 O-4）、O-3；body 不落盘、不打印 | §9.14.6 |
| G-07 | 审查 wac-094 §9：PC-6、U-13 | PC-6（i 全部转发到 8183 的块及其匹配器、改写与 `Authorization` 操作；ii 各站点的 import；iii Caddy 以外的本机代理）作为 O0-A05P 前置；U-13 (i)(ii)(iii) 与 O0-A08 硬前置；A05P 要等 WGW-1.0.3 的实现合入之后（`snippet_sha256` 会变，候选也可能变） | §9.14.6、§9.15 |
| G-08 | 自查 | 写明网关无法区分入口，所以不得用角色或来访头区分入口，四角色读不变；G1 的 `%`、`//`、点段规则与"G1 先于认证"对非移动入口是承重规则；方案比较（(a) 采纳，(b) 否决，(c) 超出权限，(c′) 交用户决定，(d) 即本次组合，(e) 记为以后的选项）与残余风险 | §9.2、§9.3、§9.4、§9.14.6 |
| G-09 | 🟡-1 顺带提醒 | 范围外告知：面板 `/v1/*` 可能已让 `/v1/accounts` 等只读交易数据在公网匿名可读（未证实）；写明两种只读核实方法（看配置；经用户授权后发一次不带凭据的公网 GET，body 不落盘），Agent 不自行核实 | §9.15 |
| G-10 | 自查 | §9.1 补充事实（面板注入的文档与夹具行号、网关触发与认证顺序、面板前端不调用 `/v1/watcher`、Caddy `path_regexp` 对 clean 后的路径匹配、O-0 现行检查范围）；§9 顶部状态说明 | §9 顶部、§9.1 |

生成物影响：只有 `contracts/generated/caddy-watcher-gateway.caddy` 需要由生成器按格式 v2 重新生成（其头部 `_yaml_sha256`、`_phase_max` 不变，`_format` 改为 v2）。`caddy-watcher-gateway-paths.txt`、`lib/generated/gateway-routes.js`、`api/generated/watcher_gateway_routes.py` 逐字节不变，watcher 镜像与控制面发布物的内容不因本次改变。片段字节变化使 O0-A05P 探针绑定的 `snippet_sha256` 改变，所以探针要在本次实现合入之后跑。

**后端执行者清单（B-1..B-7；文件范围：`scripts/contracts/`、`contracts/generated/caddy-watcher-gateway.caddy`、`tests/control-plane/`；不改服务代码）**

1. **B-1 生成器**：`scripts/contracts/watcher_gateway_routes_lib.py` 的 `_render_caddy`（`:303` 起）按 §9.14.3"格式 v2"输出：`_format watcher-gateway-caddy-snippet.v2`；`(watcher_gateway_routes)` 在兜底组之后追加 `@wgw_direct` 组；随后输出 `(watcher_gateway_direct_guard)`。守卫正则由 `paths.app_outer_prefix` 拼出，不写死。`validate()` 的 S-22 检查（`:274`）把 `wgw_direct` 加入保留名。
2. **B-2 校验脚本**：`scripts/contracts/check_watcher_gateway_routes.py`（现行片段断言在 `:78-84`）按 §9.14.4 第 1 项的 WGW-1.0.3 条目实现：v2 形状、守卫正则逐字（脚本内独立拼出）、匹配与不匹配探针、两个前缀正则互不相交、"G1 ⇒ D"取值池预言（某个参数过滤后没有取值即失败）。比较行数为 0 即失败。
3. **B-3 重新生成**：`python3 scripts/contracts/gen_watcher_gateway_routes.py --phase-max P2`。提交前用 `git diff --stat` 确认四份生成物中只有片段变化；`python3 scripts/contracts/check_watcher_gateway_routes.py` 末行 `ROUTES_DIFF_EMPTY`，`rows` 与 `yaml_sha256` 都与改动前相同，`phase_max=P2`。
4. **B-4 片段测试**：`tests/control-plane/test_caddy_watcher_gateway_paths.py` 改为期望 v2：`_format`、两个 snippet 的顺序、守卫组逐字、片段不含 `*`；`EXPECTED_P2_LINES` 与 `P3_PATHS` 不变；守卫正则的探针在测试里独立写一份，不引用 lib。
5. **B-5 网关侧回归测试**（`tests/control-plane/api/test_watcher_gateway.py` 新增用例，`.venv-arch`，不 import yaml）：
   - (1) **G1 ⇒ D**：对每条已注册的 gateway 行，用 B-2 同一取值池（测试内独立写一份）构造能通过 G1 的路径，先断言它确实通过 G1（到达 endpoint，`scope["state"]` 中有该行 id），再断言测试内独立写出的守卫正则用 `re.search` 命中它。
   - (2) **残余形状在认证之前被拒**：在 ASGI scope 层构造 `path`/`raw_path`：`/v1/watcher/../accounts`、`/v1/watcher/..%2Faccounts`、`/v1/watcher%2e%2e/accounts`、`/v1/watcher%25`、`/v1/watcher//status`、`/v1/watcher/./status`、`/v1/watcher/status/..`，每个请求都带夹具生成的**合法静态 `SYSTEM_OBSERVER_TOKEN`**，方法取 GET 与 HEAD。期望 404 `route_not_found`，`resolve_principal` 的调用计数为 0（用 monkeypatch 计数），上游请求计数为 0。
   - (3) 锁定计划：直连 operator-query、用静态 `SYSTEM_OBSERVER_TOKEN` 请求 `GET /v1/watcher/status`，经上游桩得到 200。现有四角色测试已覆盖的，确认它仍在即可，不重复。
   - 若 (2) 在现有代码上失败，说明 L-1 的前提不成立：停工上报 Planner，不得自行改网关语义。
6. **B-6 不改**：`services/control-plane/api/watcher_gateway.py`、`read_api.py`、`security/*`、`contracts/watcher-gateway-routes.yaml`、两份代码生成物、Caddy 清单、watcher 代码。不加任何按角色、按来访头区分入口的逻辑。
7. **B-7 验证命令**：`python3 scripts/contracts/check_watcher_gateway_routes.py`；`.venv-arch/bin/python -m pytest tests/control-plane/test_caddy_watcher_gateway_paths.py tests/control-plane/api/test_watcher_gateway.py -q`；watcher 的 node 测试照跑一遍，确认结果与改动前相同（生成的 JS 未变）。所有命令都带 `set -o pipefail`，不连 jp-24。

**Release Steward 清单（RS-1..RS-10；文件范围：`scripts/ops/o0/`、`docs/agent-team/release/`；与 wac-097 的 🟡-2、🟡-3 同批）**

1. **RS-1 生成物核对**：`o0_caddy_watcher_routes.py` 的 `expected_snippet`、`load_snippet`、`cmd_check_artifacts`（`:290-338`）按格式 v2，从清单独立推导两个 snippet 与守卫组并逐字节比对；`_find_wgw`、`_check_wgw_structure`（`:1129-1202`）认出 `wgw_direct` 路由（正则逐字，handler 为 `static_response`、状态 404），要求它与兜底同处 wgw 连续块的尾部，顺序为兜底、守卫。R12 的核对表述"兜底逐字相同且在最后"改为"兜底之后紧跟直连守卫"。
2. **RS-2 V-1**：把 `_forwarder_check`（`:1214-1252`）从"只管 `/m/v1/watcher` 前缀"推广到 I-1。覆盖全部 server，对每条非片段的 operator-query 转发路由输出 `DIRECT_ENTRY` 行，按 N-2、N-3、N-4 判定，违例输出 `DIRECT_ENTRY_FAIL`。N-3 的"改写模型"与 wac-097 的 🟡-2 共用一套实现：凡是改路径的 handler 都算命中，常量 `uri` 例外。判不了即失败。
3. **RS-3 V-2**：对含 operator-query 转发的每个（server，host）组合，模拟 `P_D` 与反向对照 `/v1/watcherx`。
4. **RS-4 V-3**：扩展 `_probe_live_checks`（`:1930` 起）。按 host 发送 `P_D`、百分号形式、`PROBE_PARAM_VALUES` 取值与 `//` 变体，期望 404、body 长度 0、两类桩计数不变；残余形状只允许以含 `%`、`//` 或点段的 raw path 到达桩；另记录 `/v1/accounts` 的 `injected=yes|no`，不作断言，不打印头值。
5. **RS-5 V-4**：`caddyfile_check`（`:1559` 起）增加 N-2 的文本检查：import 位置在所有 `handle`/`handle_path`/`route` 之前；同一站点 import 了两个 snippet 判失败；端口识别不了判 UNCOMPARABLE。
6. **RS-6 自测与真实 Caddy 测试**（selftest、`tests/caddy_real_test.sh`，每个判定都用真实 Caddy v2.10.2 复核）：
   - 仿生产夹具加 v2 片段 → PASS。
   - 删去守卫组、守卫正则去掉 `%` 或 `(?i:`、把 `import watcher_gateway_routes` 挪到 `handle /v1/*` 之后 → FAIL。
   - 第二站点用 `handle /v1/*` 转发到 8183 并注入：不 import 守卫 → FAIL，import `watcher_gateway_direct_guard` → PASS，两个 snippet 都 import → FAIL。
   - g16、g17、g18 → FAIL；`handle_path /p/*` 转发到 8183（剥后头部为 `/`）→ FAIL。
   - 站点顶层 `reverse_proxy /v1/* 127.0.0.1:8183 { header_up Authorization … }` 加守卫（import 写在它之后也算）→ PASS。本机实测：守卫仍先生效。
   - `forward_auth 127.0.0.1:8183 { uri /v1/auth }` → PASS；`{ uri /v1/watcher/status }` → FAIL。
   - 仿生产 `@mobile` 块 → PASS。
   - 夹具 `Caddyfile.prodlike.in` 不需要为守卫改动。
7. **RS-7 V-5 @ O0-A05**：`o0_deploy_caddy.sh --phase verify` 加入 V-5；不通过即 verify 失败，按现行处置办理。
8. **RS-8 V-5 @ O0-A08**：`o0_deploy_operator_query.sh` 的 O-1 preflight（不通过输出 `DIRECT_GUARD_MISSING` 并阻断，不写任何东西）、O-2 apply（重启之后、自动回滚窗口之内；不通过即 O-4）、O-3 verify。公网请求一律不带凭据与 Cookie，只输出状态码、`size_download`、`content_type`。
9. **RS-9 打包门禁**：`o0_package.sh` 对片段 `_format` 的期望改为 v2；其余门禁不变。A05P 与 A05 的绑定改用新的 `snippet_sha256`。
10. **RS-10 文档**：
    - `o0-runbook-deploy.md`：§3.1 第 7 条加入 PC-6（文字取自 §9.14.6）；阶段 C 的 verify 行与 O-1、O-2、O-3 行加入 V-5。
    - `o0-authorization-list.md`：第四节加入 U-13 与"范围外告知"备注；O0-A05P 的前置加上"WGW-1.0.3 实现已合入、PC-6 已确认"；O0-A05 加上 V-5；O0-A08 的前置加上"U-13 (i)(ii) 已确认、O0-A05 的 V-5 已通过"。
    - `o0-requirements.md`：新增一行"非移动入口不触达网关"，映射到 §9.14.6 的 I-1 与 V-1..V-5。
    - `o0-site-checklist.md`：加一条公网检查（A05 之后，期望守卫的空 404）。
    - 验证：`run_all.sh` 带与不带 `O0_CADDY_BIN` 各跑一次；`o0_package.sh --candidate <提交> --report-only --run-tests`（不带 `--execute`）；不连 jp-24。
