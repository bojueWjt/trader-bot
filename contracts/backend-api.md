# backend-api 接缝契约（v1.1，已冻结 frozen）

> 状态：**frozen（2026-08-30，G0 裁决）**——已吸收 codex 落地前 review 的 19 条阻断项（各条标注 review #N）。
> G2 以本文件 schema 造 fixtures 开工；G1 实现必须逐字段一致，偏差走 block 仲裁。
> 完整语义见设计文档 v1.1 对应小节；本文件只写死接缝形状。
> §9（watcher 网关与配置快照）单独版本化：当前 **WGW-1.0.4**（2026-09-29：网关独立角色 watcher-gateway、127.0.0.1:8186、独立代码目录；1.0.3 草案作废；勘误记录见 §9.16；路由 YAML 版本字段仍为 WGW-1.0.2，路由表未变）；§1–§8 不受其影响。

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
> **WGW-1.0.4（2026-09-29，勘误 H-01..H-14，第四轮修订 G-30..G-43；2026-09-30 合并后勘误一 G-44..G-51，见 §9.16）——当前有效版本**：用户于 2026-09-29 裁决采纳选项 (e)"网关独立监听端口"。`/v1/watcher/*` 只由新控制面角色 `watcher-gateway` 提供，它监听 `127.0.0.1:8186`，运行在独立的 systemd 单元中，并且按 Planner 裁定 R23 使用**独立的代码目录**；它不持有数据库凭据。operator-query（8183）及其他所有角色在路由层面都没有网关。Caddy 中只有契约片段的 `/m/v1/watcher/*` 逐路径组能拨到 8186；直连守卫 `@wgw_direct` 保留，作为纵深防御。§9.14.6 全文重写。WGW-1.0.3 草案（下一段）中的 T/D/I-1/L-1、N-1..N-5、R22 封闭白名单，以及旧版的 V-1..V-5、PC-6、U-13 全部作废，历史见 §9.16 与提交 `db79023`；它的片段格式 v2（直连守卫）与 B-1..B-5 保留。本次只改 §9 的文字：YAML 与四份生成物不变（片段 v2 的重新生成沿用 WGW-1.0.3 的 B-3）。**本阶段不动 node-control、event-ingest、operator-query 三个单元的代码目录、单元文件与 env，也不重启它们**；交易节点不动。
> **WGW-1.0.3（2026-09-28，勘误 G-01..G-10，第二轮 G-11..G-19，第三轮 G-20..G-29，见 §9.16；草案，从未合入，已被 WGW-1.0.4 取代，只有片段格式 v2 与 B-1..B-5 仍然有效）**：operator-query 的非移动入口（面板 `/v1/*` 等）不得把请求交给网关，新增 §9.14.6。本次只改 §9 的文字与 Caddy 片段的生成规则：路由真源 YAML **零改动**（其 `contract_version` 字段仍为 `WGW-1.0.2`，`yaml_sha256` 不变），两份代码生成物与 Caddy 清单逐字节不变，只有 Caddy 片段改为格式 v2（`watcher-gateway-caddy-snippet.v2`）。网关代码、角色 scope（§9.3 四角色读）、既有 `/v1` 端点都不变。

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
| （WGW-1.0.3 补）Caddy `path_regexp` 对**解码并 clean 之后**的路径匹配（`cleanPath(r.URL.Path)`，`cleanPath` = `path.Clean` 后保留尾斜杠；WGW-1.0.3 第二轮更正引文）；`reverse_proxy` 在没有改写时转发 `URL.EscapedPath()`（不 clean：原文是合法编码时即原文，否则为 `Path` 的重新编码，必含 `%`）；`reverse_proxy` handler 自带 `rewrite` 字段（`forward_auth` 与 `reverse_proxy { rewrite … }` 生成的就是它，而不是独立的 `rewrite` handler）；可以容纳路由的位置有 server 的 `routes`、`errors.routes`、`named_routes`，`subroute` 的 `routes`，`reverse_proxy`/`intercept` 的 `handle_response[].routes` | Caddy v2.10.2 `modules/caddyhttp/matchers.go:701`（`MatchPathRE.MatchWithError`）、`caddyhttp.go:302-308`（`cleanPath`）、`:139`（`handle_response` 的 `routes`）、`server.go:116`/`:124`/`:134`/`:703`、`subroute.go:40`、`reverseproxy/reverseproxy.go:167`（`rewrite`）/`:182`、`intercept/intercept.go:55`；本机实测见 §9.14.6 |
| （WGW-1.0.4 补，基线 `integ/watcher-app-crew` @ `6e80efb`）控制面三个角色同一代码目录、同一 `read_api:app`，按 `CONTROL_PLANE_APP_ROLE` 区分；端口 8181/8182/8183，OS 用户 `trader-v3-cp-<role>`，单元 `trader-v3-controlplane-<role>.service`；单元带 `CONTROL_PLANE_EXPECT_DATABASE_ROLE` 与 `pg_isready` 前置；`apply` 会重启全部角色；健康检查 `/health/role` 期望数据库身份 | `scripts/jp24-p1-control-plane.sh:12-33`、`:279-315`、`:317-329`、`:331-375` |
| （WGW-1.0.4 补）`AppRole` 只有 `all/node-control/event-ingest/operator-query`；未设 `CONTROL_PLANE_APP_ROLE` 时回落 `all`；operator-query 的路由集合 = 全部路由名减去节点与 ingest 所有的；网关路由现由模块级 `register_routes(app)` 注册在共享 app 上，所以 `all` 与 operator-query 都含网关 | `services/control-plane/api/app_roles.py:12-16`、`:63-75`、`:124-142`（`route_names_for_role`）；`read_api.py:10669-10674`、`:10690-10691` |
| （WGW-1.0.4 补）未设 `CONTROL_PLANE_EXPECT_DATABASE_ROLE` 时启动不连库；`/health/role` 需要 `DATABASE_URL`；认证只用环境变量（reader、signal token 与 `AUTH_SECRET_KEY`），目录互异检查另读节点 token | `read_api.py:307-326`、`:10633-10663`；`security/principal.py:67-97`、`:100-147` |
| （WGW-1.0.4 补）仓库内没有使用 8184–8189 的配置、脚本或文档；唯一出现是 O-0 自测把 `127.0.0.1:8184` 用作"错误的上游端口"变异。O-0 verify 的默认上游现为 `127.0.0.1:8183` | `git grep` 全仓（排除 lock 与行情测试数据）；`scripts/ops/o0/o0_caddy_watcher_routes.py:2857`、`:136` |
| （WGW-1.0.4 补）Hermes 的交易脚本经 `127.0.0.1:8080`（Caddy 默认路由到 operator-query）查询开仓路由并下单；signal worker 直接向 operator-query 的 `/v1/operator/orders` 下单。所以 operator-query 的重启窗口会让信号下单失败 | `hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py:19`、`:87`；`docs/operations/trading-reliability-cutover.md:27`；`scripts/jp24-p1-control-plane.sh:370-373` |
| （WGW-1.0.4 补）写接口要求 `risk_admin`（`_require_operator_principal` → `can_write_operator_orders`），observer 是 reader，不能写；面板前端对 `/v1/*` 的读请求在已登录时自带 `Authorization: Bearer <登录 token>`（Caddy 现把它覆盖为 observer） | `read_api.py:356-369`；`security/principal.py:190-196`；`bridge/apps/dashboard/src/utils/api.ts:616-622`、`:721-731`、`:1845-1851` |
| （WGW-1.0.4 第四轮补）生产内存中的 `67b401a` 与集成分支在 `services/control-plane/` 下相差 5 个文件：`read_api.py` +133/−17（C-0 快照在路由、addon 与风险比例中的分支，`operator_order` 开仓路径的 `open_snapshot_lease`，定量函数的新参数，operator-query 的快照启动钩子，`import watcher_config_snapshot`），新增 `watcher_config_snapshot.py`、`watcher_gateway.py` 与 `generated/*`。三个单元都带 `Restart=on-failure`。`read_api` 及其依赖按 `__file__` 的上级目录定位兄弟包 | `git diff --stat 67b401a <集成分支> -- services/control-plane/`；`infra/systemd/account-stall-control-plane-{reader,writer}.conf`；`read_api.py:32-43`、`:2536-2542` |
| （WGW-1.0.4 第四轮补）bootstrap 生成的 `operator-query.env` 只拷入 reader token、operator 配置与 sqlite 变量，不含 signal token；app 只用配置的静态 token；settings 路由用自己的静态 token 表，不认登录会话；面板以 `VITE_AUTH_DISABLED=true` 构建时从不带 token；登录服务的默认角色是 `risk_admin`；reviewer 可以审批风控决策 | `scripts/bootstrap_control_plane_roles.py:505-509`；app `apps/attention-android/src/services/tradingApi.ts:762`；`services/control-plane/settings/router.py:19-41`；`bridge/apps/dashboard/src/utils/api.ts:431-438`；`bridge/apps/api/app/security/auth.py:149`、`:187`；`read_api.py:6138-6140` |
| （WGW-1.0.3 补）O-0 现行 verify 的转发检查与探针只覆盖 `/m/v1/watcher` 前缀；O-3 只测公网 `/m/v1/watcher/status` | `scripts/ops/o0/o0_caddy_watcher_routes.py:1214-1252`；`docs/agent-team/release/o0-runbook-deploy.md:197`；审查 `docs/agent-team/reviews/wac-094.md` 🟡-1（g18、g19） |

### 9.2 拓扑与三种服务端身份

```
app ──baseUrl(/m)+/v1/watcher/*──▶ Caddy(strip /m 一次) ──▶ watcher-gateway 角色 127.0.0.1:8186 (async，WGW-1.0.4) ──Bearer gateway──▶ watcher 127.0.0.1:9090
浏览器 ──basicauth──▶ Caddy(清除外来 X-Watcher-* → 注入 X-Watcher-Proxy-Auth) ──▶ watcher
operator-query 快照 reader ──Bearer snapshot──▶ watcher GET /api/trading/config-snapshot（内网，不经网关）
面板等非移动入口 ──(/v1/*，Caddy 可能替调用方注入凭据)──▶ operator-query 127.0.0.1:8183：路由表中没有 /v1/watcher（WGW-1.0.4），Caddy 守卫另外先回 404（纵深防御，§9.14.6）
```

- （WGW-1.0.4，取代 WGW-1.0.3 的同位条款）网关只运行在控制面角色 `watcher-gateway` 中，监听 `127.0.0.1:8186`，运行在独立单元中。app 到网关的**唯一**公网入口是 Caddy 片段的 `/m/v1/watcher/*` 逐路径处理器，它们拨 8186，并保留调用方自己的 `Authorization`。Caddy 中其他任何对象的上游都不得指向 8186（§9.14.6 I-2）。**本节与 §9.3–§9.9、§9.14 中所称"operator-query 网关"、"operator-query 进程"持有 `WATCHER_GATEWAY_TOKEN`、安装前缀中间件、网关的 worker 与槽位，一律改读为 watcher-gateway 角色进程；operator-query 只保留快照 reader（`WATCHER_SNAPSHOT_TOKEN`，§9.10、§9.11）。**角色、单元、凭据、部署与检查见 §9.14.6。

- **凭据与环境变量**（名字即契约，值由 O-0 发行侧生成，两两互异，app 与用户不接触）：

  | 身份 | watcher 侧（校验用，当前+上一值） | 持有方 | 出示方式 |
  |---|---|---|---|
  | `gateway` | `WATCHER_GATEWAY_TOKEN`、`WATCHER_GATEWAY_TOKEN_PREVIOUS` | watcher-gateway 角色（WGW-1.0.4；原为 operator-query）：`WATCHER_GATEWAY_TOKEN` | `Authorization: Bearer <token>` |
  | `snapshot` | `WATCHER_SNAPSHOT_TOKEN`、`WATCHER_SNAPSHOT_TOKEN_PREVIOUS` | operator-query：`WATCHER_SNAPSHOT_TOKEN` | `Authorization: Bearer <token>` |
  | `browser` | `WATCHER_BROWSER_PROXY_TOKEN`、`WATCHER_BROWSER_PROXY_TOKEN_PREVIOUS` | Caddy | `X-Watcher-Proxy-Auth: <token>` |

- **"已配置"的定义（R2）**：环境变量存在且值不是空串。未设置或为空串的 `*_TOKEN_PREVIOUS` 视为未配置，**永不参与匹配**。
- **watcher 启动**（R2）：三个当前值必须已配置；每个已配置值（含已配置的 `*_PREVIOUS`）必须匹配 `^[\x21-\x7E]{32,}$`（可打印 ASCII、无空白、≥ 32 字节）；全部已配置值（最多六个）两两互异。任一不满足 → 进程非零退出，错误信息只列变量名，不打印值。watcher 环境里不放任何控制面 reader token。
- **跨服务互异**（审查 💭-4）：O-0 发行侧校验 `WATCHER_*` 六个值与控制面 token 目录（`configured_token_values`，`principal.py:67-97`：四个 reader token、signal token、节点 token 等）两两互异。（WGW-1.0.4）进程内纵深检查按持有方分开做：watcher-gateway 启动时检查 `WATCHER_GATEWAY_TOKEN`，operator-query 启动时检查 `WATCHER_SNAPSHOT_TOKEN`，比较对象都是本进程 env 中可见的 `configured_token_values`（watcher-gateway 的 env 只含四个 reader token，不含 signal token 与节点 token，它们只由 O-0 发行侧检查覆盖；WGW-1.0.4 第四轮）。两者与 `configured_token_values` 任一相等 → 视同未配置（网关 503 `gateway_disabled`；快照开关打开时启动失败）并告警，告警只写变量名。
- **watcher-gateway 启动**（WGW-1.0.4；原为"operator-query 启动"，下文所称"operator-query 进程不退出"同样改读为 watcher-gateway 进程不退出）：缺 `WATCHER_GATEWAY_TOKEN` → 网关路由照常注册，但一律 503 `gateway_disabled` 并打告警日志；**不得**让交易端点启动失败。（WGW-1.0.2 写入 R11）加载 Python 生成物失败（缺件、摘要自检不符、导入异常、§9.14.3 的加载期断言不成立）时同样处理：只停用 `/v1/watcher/*`（一律 503 `gateway_disabled`）并记录错误，operator-query 进程不退出，其他端点不受影响。（WGW-1.0.2 追补，F-11）这些失败统称"生成物停用态"，完整规则见 §9.14.3"网关加载期失败的处理"：网关模块自己捕获全部此类异常，`create_app()` 不得因此抛错；停用态下前缀中间件对触发的每个请求直接返回 503，先于 G1–G8。快照开关（§9.11）打开且缺 `WATCHER_SNAPSHOT_TOKEN` → 按计划 §2.1 启动失败（上线前配置校验锁定，开关默认关）。
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
  | signal token（账户级，非 reader） | 403 | `insufficient_scope`（WGW-1.0.4 第四轮：watcher-gateway 的 env 按最小权限不含 signal token，所以在网关上 signal token 得到 403 `invalid_token`；JWT 形状的会话 token 在网关不持有 `AUTH_SECRET_KEY` 时得到 401 `unauthenticated`，见 §9.14.6） |
  | 读行：四角色任一 | — | 放行 |
  | 写行（POST/PUT/DELETE）：角色 ≠ `risk_admin` | 403 | `insufficient_scope` |
  | token 目录不可用（`TokenCatalogError`） | 503 | `auth_unavailable` |

- scope 表即 YAML 每行的 `roles`：读行恰为 `[system_observer, viewer, risk_admin, reviewer]`，写行恰为 `[risk_admin]`，且仅限 YAML 已列写行。不新增角色、不新增 token。
- （WGW-1.0.3）角色 scope 回答"这把凭据能做什么"，不回答"请求从哪个入口来"。网关无法区分请求经哪个 Caddy 入口到达（§9.1），因此**不得**用角色（例如拒绝 `system_observer`）或来访请求头来区分入口；入口控制只在部署结构中实现（WGW-1.0.4：网关只存在于 watcher-gateway 角色的 8186 端口，Caddy 中只有片段能拨它，§9.14.6）。四角色读保持不变（计划 §2.1、§4 T0 验收要求 `system_observer` 可读 status）。
- **契约声明**：持有同一 `risk_admin` token（静态或会话）的所有调用方一并获得 watcher 写权限（包括停采集、改风控）。以后要区分自然人，用服务端 principal 映射，不给 app 新密钥。
- 同一个 `viewer` 写被 403 `insufficient_scope` 之后，读 status 仍须成功（无任何"禁写传染"）。

### 9.4 路径、方法与请求规范化（网关）

- **路径**：app 请求 `baseUrl + outer_path`，`baseUrl` 已以 `/m` 结尾，app 常量只写 `/v1/watcher/...`。Caddy 外部路径 = `/m` + outer_path，逐路径追加，禁止 `/m/v1/*` 通配。（WGW-1.0.2，用户裁决 2026-09-26）路径参数段在 Caddy 侧的语义是"恰好一个非空段"，一律用锚定、区分大小写的 `path_regexp` 实现（参数段写 `[^/]+`），**不得**用 `path` 匹配器的 `*`（Caddy 的末尾 `*` 是前缀匹配，会跨段、忽略大小写）；清单格式、片段格式与 O-0 的并入规则见 §9.14.3。
- **匹配基于 `scope["raw_path"]`**（未解码），规则：raw path 含 `%`、`//`、`.`/`..` 段、尾斜杠、大小写不符，或路径参数不匹配 YAML `path_params.*.gateway_pattern` → 404 `route_not_found`。（WGW-1.0.2 追补）另外，已触发的请求若 raw path 不以 `/` 开头，或含 `#`，同样 404 `route_not_found`（例如 `/v1/watcher/status#x`；ASGI 的 `raw_path` 不含 query，含 `#` 只可能来自不拆片段的服务器实现）。**禁止尾斜杠 307 重定向**。
- **未注册路径 404、已注册路径未列方法 405**（`Allow` 头列 YAML 中该路径的方法），与 FastAPI 默认一致；这两类判定先于鉴权（§9.3 G1）。`OPTIONS` 一律 405（无 CORS）。`HEAD` 只在 YAML 显式列出的行允许（仅媒体）；其余路径的 HEAD → 405。
- **前缀中间件（R4，规范性；替代 WGW-1.0 的"实现提示"）**：
  - G1 由一个纯 ASGI 中间件实现，它与 §9.5 错误体处理器一起，显式安装到 `create_app()` 返回的角色 app 上（`app_roles.py:145-178` 只复制 `APIRoute`，不复制中间件与异常处理器）。（WGW-1.0.4，取代"operator-query 必须安装"）只有 `watcher-gateway` 角色 app 注册网关路由并安装前缀中间件；`operator-query`、`node-control`、`event-ingest` 与 `all` 四种角色 app 既没有网关路由，也不装该中间件（§9.14.6）。
  - 触发条件：对 `scope["path"]`（已解码）和 `scope["raw_path"]`（未解码，按 latin1 解码为文本）各取路径部分（§9.14.3 `path_part`，WGW-1.0.2 追补），各判定一次，按 ASCII 不区分大小写比较；任一等于 `/v1/watcher`，或以 `/v1/watcher/`、`/v1/watcher%` 开头 → 由中间件判定。未触发的请求原样交给 FastAPI 路由，既有 `/v1/*` 行为不变。
  - **请求目标形式（WGW-1.0.2 追补，F-09）**：网关只能看到 ASGI scope。uvicorn 的 httptools 实现会从绝对形式请求目标中取出路径再放进 scope（此时与 origin 形式请求无从区分，也无需区分：下面的 `NA_gw` 在该路径上照常生效，上游请求由路由模板构造、从不转发请求目标原文）；h11 实现把整个目标放进 `raw_path`，路径部分不以 `/` 开头。路径部分不以 `/` 开头的请求不会满足触发条件，交给 FastAPI 后因所有路由模板都以 `/` 开头而得到其默认 404，不发上游请求（既有行为，§9.13 不变）；若另一侧（`path` 或 `raw_path`）满足触发条件，则按上一条的 raw path 规则返回 404 `route_not_found`。经 Caddy 到达的请求都已是 origin 形式（`reverse_proxy` 按路径重建请求行）。
  - **never_allowed（WGW-1.0.2，规范性）**：触发后、匹配路由之前，先用 Python 生成物 `PAYLOAD["never_allowed"]` 按 §9.14.3 的网关投影 `NA_gw(path)` 判定，对 `scope["path"]` 与 `scope["raw_path"]`（latin1 解码）的路径部分（WGW-1.0.2 追补）各判定一次；任一命中 → 404 `route_not_found`（§9.5 形状），与方法无关（含 OPTIONS），不带 `Allow` 头，不交给 Starlette 路由，不发上游请求。网关对凭据面还有另外两道检查：构建网关路由时，任何 gateway 行与 never_allowed 相交（§9.14.3 `NA_intersects`）→ 按 R11 停用 `/v1/watcher/*`；G8 构造上游请求时，对替换参数后的 inner path 再判一次 `NA`，命中 → 404 `route_not_found`，不发请求。三道检查都只读生成物，不读 YAML。
  - 中间件按生成物（§9.14.3）匹配 `(method, raw_path)`。不匹配时由中间件直接返回 404/405（§9.5 形状），**不交给 Starlette 路由**，因此 `redirect_slashes` 不会产生 307。匹配成功时把行 id 写入 `scope["state"]["watcher_gateway_route_id"]` 再交给路由；网关 endpoint 第一步断言该值等于自身行 id，不等（中间件未安装或被绕过）→ 404 `route_not_found`（fail-closed）。
  - **禁止**：在角色 app 或全局设置 `redirect_slashes=False`（会把既有 `/v1/*` 的尾斜杠 307 变成 404，违反 §9.13）；用 catch-all `APIRoute`（如 `/v1/watcher/{rest:path}`）实现（会破坏 §9.14.4 的运行时路由集合相等）。
  - （WGW-1.0.4 注：入口隔离已改由独立端口承担，下面这条 WGW-1.0.3 的"承重"说明降为纵深防御的说明；G1 规则本身不变）（WGW-1.0.3）上面 raw path 规则中的 `%`、`//`、点段三条，与"G1 先于认证"一起承担非移动入口的防护：Caddy 的直连守卫（§9.14.6 的 `D`）是对**解码并 clean 后**的路径判定的，它只保证覆盖"raw 形式不含 `%`、`//`、点段"的路径；触发空间内其余形状（例如 `/v1/watcher/../accounts`、`/v1/watcher%2e%2e/x`）会绕过守卫到达 operator-query，只能靠 G1 在认证之前返回 404。放宽这三条中的任何一条，或把认证挪到 G1 之前，必须在同一次契约修订中重新论证 WGW-1.0.3 草案 §9.14.6 的引理 L-1（见提交 `db79023`），并重跑 B-5 测试。
  - 回归测试（C-1 验收）：`/v1/accounts/`（尾斜杠）的状态码、`Location` 头、响应体与改动前逐字节一致；`/v1/watcher/status/` → 404 `route_not_found` 且无 `Location`；`/v1%2Fwatcher/status`、`/V1/Watcher/status`、`/v1/watcher` → 404 `route_not_found`（§9.5 形状）。
- `/v1/watcher/` 前缀（按上面的触发条件）下的 404/405 响应体使用 §9.5 统一形状；该前缀以外的 404/405/422 响应体保持 FastAPI 现状。
- **路由名**：`watcher_gateway__<YAML id 中的 . 换成 _>`；（WGW-1.0.4）只出现在 watcher-gateway 角色 app，不出现在 operator-query、node-control、event-ingest、all。
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
| 403 | `insufficient_scope` | 网关 | 角色不足或 signal token（WGW-1.0.4 第四轮：watcher-gateway 的 env 不含 signal token，signal token 在网关得到 `invalid_token`，见 §9.3） | "当前凭据无此权限"，不引导换密钥 |
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
| 503 | `gateway_disabled` | 网关 | watcher-gateway 角色（WGW-1.0.4；原为 operator-query）未配置 `WATCHER_GATEWAY_TOKEN`（或未通过跨服务互异检查）；或处于生成物停用态（R11，§9.14.3，WGW-1.0.2 追补写明） | 同 `watcher_unavailable` |
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

  网关每 worker 合计 8 槽（config + media），舰队在途上限 = 8 × watcher-gateway 角色 worker 数（WGW-1.0.4；原为 operator-query。单元沿用 uvicorn 单 worker，即上限 8；YAML `budgets` 注释中的"operator-query worker"同样改读）；该数值写入配置与测试。`config.total = 8s` 大于 watcher `busy_timeout = 5s`，保证 `db_busy` 能回到客户端而不是先超时。
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
| `services/control-plane/api/generated/watcher_gateway_routes.py`，外加手写的空文件 `services/control-plane/api/generated/__init__.py` | watcher-gateway 角色的网关（§9.3–§9.9；WGW-1.0.4 起由该角色加载，operator-query 不加载）；import 名 `generated.watcher_gateway_routes`（与 `read_api.py:100` 的兄弟模块 import 方式一致） |
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
    - 停用态只能通过修复生成物并重启 watcher-gateway 单元解除（WGW-1.0.4；原为重启 operator-query），运行中不重试加载。
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
  - 为什么另有 `(watcher_gateway_direct_guard)`：供 app 站点以外、同样转发到 operator-query 的站点**可选**使用（WGW-1.0.4：N-2 作废，不再是并入要求）。同一站点不得同时 import 两者（两者都定义命名匹配器 `@wgw_direct`，Caddy adapt 会报重复定义）。
  - 与兜底的差别：守卫的字符类多一个 `%`。Caddy 对解码后的路径匹配，解码后出现 `%` 只可能来自 `%25`；网关的触发条件包含"以 `/v1/watcher%` 开头"（§9.4），守卫把它一并挡下。兜底保持 F-10 的写法不变。
  - 为什么含 `\n`、为什么在 `re.search` 下与 RE2 同义：理由与兜底正则相同（F-10）。§9.14.4 第 1 项的探针已在本机 Python 3 与 Go `regexp` 上逐条比对，0 不一致。
  - 片段中仍不出现 `*`，不含上游地址、主机名或凭据。清单（`caddy-watcher-gateway-paths.txt`）不含守卫，格式仍为 v2，内容不变。
- **O-0 并入规则（规范性）**：
  - Caddyfile 在全局位置 `import` 该片段文件（定义 snippet），并手写 snippet `(watcher_gateway_upstream)`：恰好一次 `uri strip_prefix /m`，然后 `reverse_proxy 127.0.0.1:8186`（WGW-1.0.4：拨 watcher-gateway 角色，不再是 operator-query）；不得有任何 `Authorization` 或 `X-Watcher-*` 请求头操作；不注入 `X-Watcher-Proxy-Auth`，不含任何其他匹配逻辑。
  - 在 app 所用站点块的**顶层**（`/m` 尚未被剥离的位置）`import watcher_gateway_routes`；不得放进 `handle_path /m*` 之类会先剥前缀的块。站点中不得有其他处理器把 `/m/v1/watcher` 前缀（任意大小写）转发到 operator-query 或 watcher（沿用审查 wac-026 🟡-1 (d)）；browser 凭据沿用运行时占位符 `{env.WATCHER_BROWSER_PROXY_TOKEN}`（(g)）。
  - （WGW-1.0.4，取代 WGW-1.0.3 的同位条款）非移动入口：Caddy 中除 `(watcher_gateway_upstream)` 外，任何对象的上游都不得指向 8186（§9.14.6 I-2）。对 operator-query（8183）等其他端口的转发，本契约不作形状要求。
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
  - 本机 Caddy 探针（非生产）：用同版本 Caddy、**生产 Caddyfile 的副本**（只把上游地址换成本机桩、把站点地址换成本机地址，其余逐字不改）与桩上游，跑 §9.14.4 第 1 项的全部 Caddy 探针（行级与兜底，含换行结尾用例），结果逐条一致；另确认 `/m/v1/watcher/status`（GET）确实到达 watcher-gateway 桩（WGW-1.0.4：8186 的桩，与 8183 的桩分开；证明没有被站点里更早的处理器遮蔽）。（WGW-1.0.2 追补 F-13）另跑下方行为说明中的两类用例：字面点段与 `//`、百分号编码与 `#`，并确认转发到桩的路径与期望值逐条一致。期望值按该说明判定，字面点段与 `//` 不按 404 判定。记录 `caddy version` 与副本相对生产文件的逐行差异。
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
2. **控制面运行时 diff**（pytest，`.venv-arch`，**不** import yaml，只 import 已提交的 Python 生成物）：用夹具环境构造 `create_app("watcher-gateway")`（WGW-1.0.4；原为 operator-query。本项其余各处所称"operator-query 角色 app"都改读为 watcher-gateway 角色 app；另加 §9.16 WGW-1.0.4 B-10 的隔离断言），枚举路径以 `/v1/watcher/` 开头的 `APIRoute`，得到 `{(method, path, name)}`，与 payload 中 gateway 行的 `{(method, outer_path, watcher_gateway__<id>)}` 对称差为空；每个 endpoint 满足 `inspect.iscoroutinefunction`、`route.dependant.body_params == []`、`route.body_field is None`（§9.3 G4）；同一集合不得出现在 `create_app("node-control")`、`create_app("event-ingest")`；operator-query 角色 app 上装有前缀中间件（探针 `/v1/watcher/status/` 得到 404 `route_not_found` 且无 `Location`）。（WGW-1.0.2 追加）never_allowed：`NA_gw`、`NA`、`NA_intersects` 通过 §9.14.3 固定探针；`NA_gw` 命中的每个探针对 GET/HEAD/POST/PUT/DELETE/PATCH/OPTIONS 都得到 404 `route_not_found`、无 `Allow`、上游请求计数为 0；用 payload 的一个副本注入一条合成 gateway 行 `GET /v1/watcher/login/x → /api/login/x` 构建网关，结果必须是 `/v1/watcher/status` 返回 503 `gateway_disabled`、既有 `/v1/*` 端点照常（R11），以此证明加载期断言存在。（WGW-1.0.2 追补 F-11）停用态至少覆盖四种注入，逐一断言：(a) 上述合成相交行；(b) `never_allowed` 置为空列表；(c) 某条 `never_allowed` 的 `methods` 改为列表；(d) 生成模块不可导入（例如在 `sys.modules` 中把 `generated.watcher_gateway_routes` 置为会抛错的桩，或摘要不符）。每种情形下：`create_app("operator-query")` 正常返回、不抛错；不带任何 `Authorization` 的 `GET /v1/watcher/status` 与 `POST /v1/watcher/config` 都得到 503 `gateway_disabled`（§9.5 形状，先于认证与 never_allowed）；上游请求计数为 0；`/v1/accounts/` 等既有端点的状态码与响应体与未停用时逐字节相同；日志不含夹具 token。（追补 F-09）请求目标：§9.14.3"请求目标固定探针"中网关一组全部通过。
3. **watcher 运行时 diff**（node 测试，不读 YAML，只 require 已提交的 JS 生成物）：`node bridge/services/telegram-watcher/scripts/dump-route-table.js` 输出 watcher 实际加载的身份路由表（W-0 须让 `server.js` 只在 `require.main === module` 时 `listen`，以便无副作用加载 `app`），得到 `{(identity, method, inner_path)}`，与 payload 对称差为空；另枚举 Express 实际注册的 handler，每条 payload inner 路由都有 handler，且不存在 payload 之外的 `/api/*` 或 `/media/*` handler。（WGW-1.0.2 追加）never_allowed：中间件使用的清单与 `PAYLOAD.never_allowed` 是同一对象（手写的 `lib/generated/watcher-routes.js` 删除，仓库内不得另有清单副本）；`NA` 与 `NA_intersects` 通过 §9.14.3 固定探针；gateway 与 snapshot 身份请求 `NA` 命中的探针（含只有 never_allowed 能给出 403 的 `/api/login/anything`，以及 raw `/api/login%2Fstart`）对 GET/HEAD/POST/PUT/DELETE/PATCH/OPTIONS 都得到 403 `identity_forbidden`；browser 身份访问 `/api/login/status`、`/`、`/healthz` 不受影响；用注入合成 gateway 行 `/api/login/x` 的 payload 副本构造中间件必须抛错。（WGW-1.0.2 追补 F-09）请求目标：`path_part` 通过 §9.14.3 固定探针；§9.14.3"请求目标固定探针"中 watcher 一组全部通过，且必须经真实 socket 发送原文请求行（supertest、fetch 等会先规范化 URL，不能代替）。
4. 第 1 项证明 YAML == 生成物，第 2、3 项证明生成物 == 运行时，三项全部为空且比较行数 > 0 → diff 为空。逐行属性（roles、query、body、write、budget）在集合相等后逐字段比较，任何不等都算非空。

**9.14.5 独立负例（T0-4，不从真源生成）**：百分号编码的斜杠与点（`%2F`、`%2e`）、双重编码（`%252F`）、尾斜杠、`//`、大小写（`/V1/Watcher/status`）、重复 query、未列 query、OPTIONS、非媒体 HEAD、未注册 404、未列方法 405；经网关遍历矩阵外请求时 watcher 请求计数不变；用 `gateway` token 直连 watcher 遍历矩阵外端点，第二层同样拒绝；`browser` 调 snapshot、`snapshot` 调网关写行被拒。（WGW-1.0.2 追加）凭据面：经网关请求 `/v1/watcher/config`、`/v1/watcher/login/start`、`/v1/watcher/login/qr/status`、`/v1/watcher/healthz`、`/v1/watcher/index.html`（全部方法）一律 404 且 watcher 请求计数不变；用 `gateway`、`snapshot` token 直连 watcher 请求 `/api/config`、`/api/login/*`、`/api/login/qr/*`、`/`、`/index.html`、`/healthz`（全部方法）一律 403 `identity_forbidden`。（WGW-1.0.2 追补）同样直连，带片段（`/api/config#x`、`/healthz#x`）、绝对形式（`http://x/api/config`）、星号形式（`*`）、含反斜杠（`/api\config#x`）的请求目标一律 403 `identity_forbidden`，`/api/status#x` 为 404 `route_not_found`，handler 计数不变。

**9.14.6 网关独立监听端口与非移动入口隔离（WGW-1.0.4，规范性；用户裁决 2026-09-29 采纳选项 (e)；Planner 裁定 R23（独立代码目录）；整节取代 WGW-1.0.3 草案的 §9.14.6；第四轮复审修订见 §9.16 G-30..G-43）**

> WGW-1.0.3 草案的本节已作废，全文见提交 `db79023`（`git show db79023:contracts/backend-api.md`），勘误摘要见 §9.16 的 G-01..G-29。作废的内容包括：以触发空间 T、守卫空间 D 作为入口判据，I-1，L-1 的承重地位，N-1..N-5，R22 封闭白名单 W-1..W-3，以及旧版的 V-1..V-5、PC-6、U-13。
>
> 三轮审查（`docs/agent-team/reviews/wac-096.md`）中，下列分析在本版仍然适用：
> - 守卫正则在 Python 与 RE2 下同义；
> - L-1 的论证，但它现在只作为纵深防御的依据；
> - Caddy 路由容器清单，以及 `reverse_proxy.rewrite`、`handle_response` 的语义：现在只用来说明 V-1 为什么遍历整份 JSON；
> - V-5 的发起位置，以及"守卫的 404 不带 `Content-Type`"这一发现；
> - B-5 中 spy 的补丁位置。

**问题**：网关按请求路径触发，看不出请求经哪个 Caddy 入口到达（§9.1）。面板的 `handle /v1/*` 会把 `/v1/*` 原样转给 8183，并用 `SYSTEM_OBSERVER_TOKEN` 覆盖来访凭据。本版把网关挪到一个独立角色的独立端口上：面板以及任何其他入口，无论怎样改写路径、注入凭据，只要不拨 8186，就到不了网关。

- **新控制面角色 `watcher-gateway`**
  - **角色与 app**：新增 `AppRole.WATCHER_GATEWAY = "watcher-gateway"`，与另三个角色使用同一个 `read_api:app` 模块，由单元中的 `Environment=CONTROL_PLANE_APP_ROLE=watcher-gateway` 选择角色。
  - **独立代码目录（R23，规范性）**：新角色的代码目录与现有三个单元共用的 `$TRADER_ROOT/services/control-plane` **互相独立**。
    - **位置**：`$TRADER_ROOT/releases/watcher-gateway/<release-sha>/`，单元的 `WorkingDirectory` 是其中的 `services/control-plane/api`。
    - **内容**：用 `git archive <release-sha>` 取出以下路径（在 `scripts/jp24-p1-control-plane.sh:85-95` 的子集基础上，把 `packages/execution-domain` 放宽为整个 `packages`；G-51）：`services/control-plane`、`services/nautilus-node/observability`、`packages`（整个目录）、`db/migrations`。保持仓库相对布局，原因是 `read_api.py:32-43`、`:2536-2542` 以及 `snapshot.py`、`order_management/*` 都按 `__file__` 的上级目录定位兄弟包。
    - **权限**：整个目录属 root，权限 0755，对单元用户只读。
    - **路径约束**：模块与数据文件都不得从该目录之外加载，只有 venv 与标准库例外。这一条由 B-11 与 RS-16 的审计钩子证明。
  - **共享代码目录的约束**：本阶段对共享目录 `$TRADER_ROOT/services/control-plane`（以及 `services/nautilus-node/observability`、`packages`、`db/migrations`）**一个字节都不动**：不安装、不备份覆盖、不还原。node-control、event-ingest、operator-query 三个单元的磁盘代码与内存代码，都保持现在的样子。
  - **venv**：推荐只读共用 `$TRADER_ROOT/.venv-cp`。新代码额外需要的 `httpx==0.28.1` 已经装在里面（`jp24-p1-control-plane.sh:119-126`）。**不得**向共享 venv 安装或升级任何包。RS-16 的依赖核对一旦发现缺包，就停工，由 Planner 决定是否在新目录下另建独立 venv（可选方案，本版不采用）。
  - **单元**：
    - 名称 `trader-v3-controlplane-watcher-gateway.service`，OS 用户与组都是 `trader-v3-cp-watcher-gateway`。单元名沿用现有的 `trader-v3-controlplane-<role>`，用户名沿用现有的 `trader-v3-cp-<role>`。
    - `ExecStart=$TRADER_ROOT/.venv-cp/bin/uvicorn read_api:app --host 127.0.0.1 --port 8186`，单 worker。
    - `Environment=PYTHONDONTWRITEBYTECODE=1`。
    - **不设** `CONTROL_PLANE_EXPECT_DATABASE_ROLE`，**没有** `pg_isready` 前置。
    - 资源限制条目按 `infra/systemd/account-stall-control-plane-reader.conf` 的内容写入单元文件（与 `jp24-p1-control-plane.sh:307` 的做法相同）；`EnvironmentFile` 是下文的 `watcher-gateway.env`。
  - **端口 8186**：仓库内 8184–8189 都没有被占用（§9.1）；jp-24 上的实际占用由 PC-6 (ii) 核实。8186 已被占用时停工，由 Planner 召回 Architect 改号。端口是契约值，O-0 不得自行更换。
- **路由层面隔离（规范性）**
  - 网关路由（`watcher_gateway__*`）与前缀中间件**只**在 `create_app("watcher-gateway")` 构造的角色 app 上注册、安装。`read_api.py:10673-10674` 的模块级注册，以及 `:10690-10691` 对 operator-query 的安装，都删除。结果是 `operator-query`、`node-control`、`event-ingest`、`all` 四种角色 app 的路由表里都没有网关路由，也不装这个中间件；它们对 `/v1/watcher/*` 返回 FastAPI 默认的 404 `{"detail":"Not Found"}`。§9.4 规定的"该前缀下 404/405 用 §9.5 形状"只在 watcher-gateway 角色成立。
  - watcher-gateway 角色 app 的路由名集合恰为以下两部分之和，不含任何交易、查询、节点或 ingest 路由：
    - 网关路由名：取自**生成物 payload**，即 `identity == gateway` 且 `phase ≤ phase_max` 的行。它们不在共享 app 上，所以不能从共享 app 推导；
    - `role_database_health`。
  - 该 app 设 `docs_url=None`、`redoc_url=None`、`openapi_url=None`。它带有与其他角色相同的 `bind_request_role` 中间件（`app_roles.py:171-177`），所以 `current_app_role()` 在请求内总是 `watcher-gateway`，健康检查不会因为 env 缺失而落进 `all` 分支。
  - `AppRole.ALL` 不含网关。生产单元缺 `CONTROL_PLANE_APP_ROLE` 时会回落到 `all`（`app_roles.py:66`），这样设计后也不会意外对外提供网关。
  - 这一节的代码改动只进入新目录。共享目录在本阶段不更新，所以现有三个单元的行为不受影响，无论它们是否重启、何时重启。
- **数据库：不给凭据（最小权限，规范）**
  - 网关路径不访问 Postgres（§9.4"不包 `_envelope()`"），认证只用 env（`principal.py:100-148`，会话校验是纯 HMAC，`session_auth.py:35-71`），快照 reader 留在 operator-query。
  - 所以不新建 DB 角色，也不复用 `trader_v3_operator_query`；不设 `DATABASE_URL`。
  - 取库一律 fail-closed：`pools._role_name` 对不支持的角色本来就会抛 `PoolConfigurationError`（`db/pools.py:69-74`），`checkout_role_connection`、`_database_connection` 在该角色下同样抛错；`close_role_pools(watcher-gateway)` 为空操作；`database_role_name` 返回 False。
- **健康检查**：`GET /health/role` 在 watcher-gateway 角色下不连库：
  - 可用：200 `{"status":"healthy","app_role":"watcher-gateway","database":"none","gateway":"enabled"}`；
  - 停用态（R11）或 G5 条件成立：503 `{"status":"unhealthy","app_role":"watcher-gateway","database":"none","gateway":"disabled"}`。原因只写日志，不进响应。
  
  它只在回环上可达。
- **凭据与 env 文件（规范；最小权限）**
  - **文件**：`$TRADER_ROOT/secrets/control-plane/watcher-gateway.env`，权限 0600，属主 root。
  - **写入的变量**（按变量名白名单）：
    - `WATCHER_GATEWAY_TOKEN`，以及可选的 `WATCHER_GATEWAY_URL`、`WATCHER_GATEWAY_CONFIG_SLOTS`、`WATCHER_GATEWAY_MEDIA_SLOTS`；
    - `RISK_ADMIN_TOKEN`、`VIEWER_TOKEN`、`REVIEWER_TOKEN`、`SYSTEM_OBSERVER_TOKEN`：§9.3 的四角色 scope 需要它们。
  - **值的来源**：
    - 四个 reader token 取自现行 `operator-query.env` 中的同名变量。它们由 `scripts/bootstrap_control_plane_roles.py:505-509` 从 `.env.v3` 拷入，这样网关与 8183 接受同一组静态 token；
    - `WATCHER_GATEWAY_TOKEN` 取自 O-0 凭据阶段。（G-45 澄清）它在 O-0 凭据集中的持有方文件叫 `controlplane-watcher-gateway.env`，这只是凭据集内部的文件名，用来和 watcher 容器自己的 `/srv/trader-secrets/watcher-gateway.env` 区分。部署到线上时，文件名仍是本节规定的 `$TRADER_ROOT/secrets/control-plane/watcher-gateway.env`，没有改名；凭据集内部的文件名不属于本契约的规定范围；
    - 读取与写入都只在 jp-24 上、由部署脚本以 root 完成，只打印变量名。
  - **默认不写入**：
    - `SIGNAL_TOKEN_ACCOUNT_*`：它们能在 operator-query 上下单，不为一个错误码放进网关进程；
    - `AUTH_SECRET_KEY`：它能签发任意角色的会话。
  - **不得写入**：`DATABASE_URL`、`WATCHER_SNAPSHOT_TOKEN`、`NAUTILUS_NODE_TOKEN`、`NAUTILUS_NODE_AUTH_JSON`、`CONTROL_PLANE_AUTH_SECRET`、`CONTROL_PLANE_EXPECT_DATABASE_ROLE`，以及任何交易所或 Telegram 凭据。
  - **对错误码的影响**（修订 §9.3 的映射表，只对 watcher-gateway 生效）：
    - signal token 在网关得到 **403 `invalid_token`**，而不是 403 `insufficient_scope`。两者都是拒绝，app 从不持有 signal token；
    - 登录会话 token（JWT 形状）在没有 `AUTH_SECRET_KEY` 时，由 `verify_session_token` 判为验签失败，得到 **401 `unauthenticated`**（`principal.py:132-136`）。
    
    app 只用配置的静态 token（app `apps/attention-android/src/services/tradingApi.ts:762`），不受影响。只有同时满足两个条件时，才由 U-13 (iii) 改为写入 `AUTH_SECRET_KEY`：PC-6 (v) 核实生产 `operator-query.env` 含这个变量名；用户确认 app 需要会话 token。
  - **不改 operator-query.env**：不写入 `WATCHER_GATEWAY_TOKEN`；快照需要的 `WATCHER_SNAPSHOT_TOKEN` 留到快照切换阶段再加。
  - **跨服务互异**：O-0 发行侧的全量检查不变（§9.2）；网关进程内的纵深检查只比较本 env 中可见的 reader token。
- **Caddy**
  - `(watcher_gateway_upstream)`（O-0 手写，§9.14.3）：恰好一次 `uri strip_prefix /m`，然后 `reverse_proxy 127.0.0.1:8186`。它的 `reverse_proxy` 不带 `rewrite` 对象，也不带 `transport` 覆盖。
  - 片段 `(watcher_gateway_routes)` 的格式 v2 不变：生成物里不含上游地址，所以改端口不需要重新生成。
  - 直连守卫 `@wgw_direct` 保留，作为纵深防御。`(watcher_gateway_direct_guard)` 对其他站点**可选**。
  - 对 operator-query（8183）或任何其他端口的转发，**本契约不作形状判定**：8183 上本来就没有网关。面板 `handle /v1/*`、`@mobile`、`forward_auth`、`handle_errors`、`invoke`、`handle_response`、`handle_path`、unix socket 上游、占位符主机名、`dynamic` 上游等现有或常见写法，只要不触发下面的 I-2，都不会因本契约失败。V-1 (c) 对其中与 `/m/v1/watcher` 前缀或片段顺序有关的形状，最多给出 `HINT DEFENSE_IN_DEPTH` 提示，不阻断（G-44）。唯一的例外是请求头操作：带 `copy_headers` 的 `forward_auth` 排在片段之前时，按 V-1 (a) 判失败。
- **入口不变式 I-2（取代 I-1；第四轮按用户"别卡太死"收敛）**：
  - **范围**：`caddy adapt` 之后的**整份** JSON（全部 app、全部 server、任意深度），但排除 V-1 (a) 认定的**每一个**片段逐路径组中，由 `(watcher_gateway_upstream)` 产生的 `reverse_proxy` 对象（仿生产片段约有 20 个；G-51）。
  - **判失败的情形**，出现任一即判 `GATEWAY_PORT_EXPOSED`：
    - (1) 任何字符串值含有独立的 `8186`，允许带前导零（G-50；正则 `(?<![0-9])0*8186(?![0-9])`。Go 把 `:08186` 当作端口 8186，审查 wac-108 已活体证实，网关桩收到了带注入凭据的请求）。这一条覆盖 dial、`upstreams`、`transport.network_proxy.url`、`forward_proxy_url`、健康检查的 `upstream`、非 http 的 app 等全部写法；
    - (2) 任何数值恰为 8186；
    - (3) 任何形如 `<数字>-<数字>` 的端口范围包含 8186（adapt 通常会把范围展开，展开后由 (1) 命中）；
    - (4) （G-46，收录 wac-096 第五轮 🟡-A）**端口可能由请求决定**：dial 的端口部分，或整个 dial，含有 `{env.*}`、`{system.*}` 以外的任何占位符。这包括请求作用域的 `{http.request.*}`、`{http.regexp.*}`、`{http.matchers.*}`、`{http.vars.*}`，它们的简写 `{header.*}`、`{query.*}`、`{path.*}`、`{re.*}`、`{vars.*}`、`{cookie.*}` 等，也包括 `map` 定义的自定义输出。理由是客户端可以借此把上游端口指到 8186，审查 wac-096 第五轮已活体证实。`dynamic` 上游的 `port` 字段按同一规则判定。
    - (5) （G-50）**代理 URL 按 dial 规则判定**：`transport.network_proxy.url`、`forward_proxy_url` 这类代理 URL，先用标准 URL 解析取出 host:port（缺省端口按 scheme 补齐），再按上面的 dial 规则判定：端口整数值为 8186 就失败，端口或整个 URL 含请求作用域占位符也失败。
  - **不因 I-2 失败**的写法：
    - unix socket 上游：到 8186 需要本机进程转发，这由 PC-6 (iii) 覆盖；
    - 主机名是占位符、端口是字面值且不是 8186；
    - `dynamic a`/`aaaa` 的 `port` 是字面值且不是 8186；
    - 其他解析不出、字符串中又不含 8186 的上游，且只含 `{env.*}` 或 `{system.*}` 占位符，例如 `{env.OQ}`；代理 URL 中同样只含这两类占位符的也算。这类对象由 V-1 输出为 `UPSTREAM_UNRESOLVED` 信息行，交 PC-6 (i) 人工确认它不会解析到 8186。（G-46：端口含请求作用域占位符的不在此列，由 (4) 判失败。）
  - **为什么这样就够**：
    - 网关只存在于 8186，而 8186 只监听回环；
    - 能把公网请求送到回环 8186 的，只有 Caddy 中写有 8186 的对象（dial、代理 URL 等，由 (1)–(3) 覆盖），以及 Caddy 以外的本机转发（由 PC-6 (iii) 覆盖）；
    - Caddy 中唯一合法写 8186 的是片段的上游，它不做任何请求头操作，保留调用方自己的 `Authorization`；
    - 面板等注入 observer 的入口只能到 8183，那里没有网关。
    
    本版不再依赖 T、D、L-1 与 G1 的规则来承担入口隔离。
- **检查（Release Steward 实现，见 §9.16 WGW-1.0.4 RS 清单）**
  - **V-1 verify 静态检查**：
    - (a) wgw 结构检查照旧（逐路径组与清单一致，兜底与守卫逐字，import 位置按 F-12），另外要求三点：
      - `(watcher_gateway_upstream)` 生成的 `reverse_proxy` 只拨 `127.0.0.1:8186`（verify 的 `--upstream` 默认值改为 `127.0.0.1:8186`），恰好一次 `strip_path_prefix /m`，不带 `rewrite`，不带 `transport` 覆盖；
      - 从 server 根到片段 `reverse_proxy` 的整条处理链上（含站点顶层的 `request_header`），没有任何请求头操作。这与现有 verify 的"top-level request_header"变异及"不带 `Authorization`"探针（`o0_caddy_watcher_routes.py:2815`、`:2016-2019`）一致；
      - 守卫组存在，且逐字一致。
    - (b) **主判据 I-2**：命中即输出 `GATEWAY_PORT_EXPOSED <JSON 路径> <命中的值>`（值只截取端口附近，不输出头值），verify 失败。解析不出的上游输出 `UPSTREAM_UNRESOLVED <JSON 路径>`，这只是信息行，不判失败。
    - (c) 现有的 `/m/v1/watcher` 前缀遮蔽检查（F-12、F-13、wac-094）与 9090/9100 检查（R11）仍然阻断。（G-44 修订：消除与上面 Caddy 条款"对 8183 不作形状判定"的矛盾，按用户"不要限制得那么死"的要求降级）判定口径如下：
      - **仍判失败（阻断）**：
        - 片段之前的请求头操作，包括带 `copy_headers` 的 `forward_auth`，它 adapt 后在 2xx 分支中生成 `headers.request`，属于 V-1 (a) 所说的请求头操作；
        - 片段之前、可能把路径改进 `/m/v1/watcher` 空间的规则；
        - 片段之前、命中 `/m/v1/watcher` 前缀空间，并且含有白名单外 handler（`static_response`、`redir`、`basic_auth`、`subroute` 等）的路由，但下面列出的提示形状除外；
        - 把 `/m/v1/watcher` 前缀转发到 9090、9100 或 8186 的非片段路由；
        - 与 8186 有关的一切（I-2）。
      - **降为 `HINT DEFENSE_IN_DEPTH`**（不阻断；写进结果行；由 PC-6 (i) 逐条人工确认它不会指向 8186）：
        - (i) 片段之前的**普通** `forward_auth`：adapt 后是一个 `reverse_proxy`，不命中 I-2，不带 `dynamic_upstreams`，`handle_response` 路由里只有 `vars` 或不带 `request` 的 `headers`。它在 2xx 时原样放行原请求，碰不到 8186，也不改调用方的 `Authorization`；
        - (ii) 片段之前、只可能把路径改进 `/v1/watcher` 空间（而不是 `/m/v1/watcher` 空间）的规则，因为 8183 上没有网关；
        - (iii) 位于兜底与守卫之后、工具无法求值的匹配器或处理器，例如 SPA 的 `try_files`，因为这些路由已被兜底与守卫的 404 截断；
        - (iv) 指向 8183 或其他既不是 8186、也不是 9090/9100 的端口的旧形状：把前缀转发给 8183、改路径后转发给 8183、上游动态或解析不出（其中 8186 的部分由 I-2 判定）、未知或递归的 `invoke`、只是提到 `watcher` 的 `path_regexp`。
      
      以上提示形状一旦改拨 8186，或者其中出现 8186，都由 I-2 判失败。本条只规定结果口径；工具已有的精确判定（例如具体的 handler 白名单）照旧。
    - (d) 对指向 8183 或其他端口的路由，**不作任何导致失败的形状判定**，最多按 (c) 给出不阻断的提示（G-44）。WGW-1.0.3 草案 RS-2 的白名单**不得实现**。
  - **V-2 verify 模拟**：沿用现有模拟：表内路径经片段到达上游 8186，路径去掉 `/m`。另在 app 站点模拟 `GET /v1/watcher/status`、`/V1/WATCHER/status`、`//v1/watcher/status`、`/v1/watcher/dialogs`，期望由守卫回 404。
  - **V-3 探针活体检查**：8183 与 8186 各用独立的桩，断言两点：
    - (1) 清单每行、每种方法都经片段到达 **8186 桩**，路径去掉 `/m`，`Authorization` 保持请求方原值；
    - (2) 除 (1) 以外的所有探针都**不得**命中 8186 桩。这包括 V-2 的四个 `/v1/watcher` 请求、面板 `/v1/*` 的样本、现有的前缀空间与参数值探针，以及每个其他站点 host 上的同组 `/v1/watcher` 请求。
    
    其他 server 由 V-1 覆盖。WGW-1.0.3 的逐转发器标记探针如果已经实现，保留为**不阻断的信息输出**，不删除。
  - **V-4 caddyfile-check（文本）**：F-12 的 import 规则不变。文本中独立的 `8186` 只允许出现在 `(watcher_gateway_upstream)` 内。V-4 任一失败都使 verify 失败（阻断），V-1 另行独立判定。
  - **V-5 生产公网只读检查**：
    - **发起方式**：沿用 WGW-1.0.3 G-14，从 jp-24 以外的机器发起，公网主机名，真实 DNS，不用 `--resolve`、hosts 覆盖或代理；不带凭据，不带 Cookie；只输出状态码、`content_type`、`size_download`，不落盘，不打印 body。每个执行点都逐点列入授权清单，由用户授权后执行。
    - **请求与期望**（只看状态码）：
      - `GET /v1/watcher/status`、`GET /v1/watcher/dialogs`、`GET /V1/WATCHER/status`（面板入口）：必须**恰为 404**，其他任何状态都算失败；
      - `GET /m/v1/watcher/status` 不带凭据：网关单元运行后必须是 **401 或 403**；阶段 C 之后、单元启动之前必须是 **502**。其他任何状态都算失败；
      - `GET /m/v1/accounts` 不带凭据：必须**恰为 401**，作为回归对照。
    - **执行点**：
      - O0-A05 verify：外部检查，外加 jp-24 上回环 `--resolve` 的补充检查；
      - O0-A08 apply：单元启动后做回环补充检查；
      - O0-A08 O-3：外部检查必做。拿不到授权就输出 `DIRECT_GUARD_UNVERIFIED`，结论为"未完成"，不得宣告上线完成（WGW-1.0.3 G-27）。
      
      O-1 不要求外部证据。
- **部署与重启（规范；交易零影响是硬约束）**
  - **不动的部分**：
    - node-control（8181）、event-ingest（8182）、operator-query（8183）：不重启，不改单元与 env，**也不改它们的磁盘代码**；
    - 交易节点；
    - 共享代码目录与共享 venv。
    
    所以本阶段**不产生 D-04**（WGW-1.0.4 首版把 D-04 用于本阶段，并称"磁盘新代码对 operator-query 只少了网关路由"，这一说法不实，已删除）。
  - **与集成分支的真实差异**：生产内存中是 `67b401a`，集成分支与它在 `services/control-plane/` 下相差 5 个文件（`read_api.py` +133/−17、新增 `watcher_config_snapshot.py` 576 行、`watcher_gateway.py`、`generated/*`）。`read_api.py` 的差异包括四部分：
    - C-0 快照在 `_load_channel_risk_route`、`_account_risk_capital_addon`、`_symbol_risk_ratio` 中的分支；
    - `operator_order` 开仓路径中的 `open_snapshot_lease` 与 `checks` 追加，以及 `_size_entry_batch`、`_size_open_order` 的新参数；
    - operator-query 的 `watcher_config_snapshot.start_if_enabled` 启动钩子；
    - 模块顶部的 `import watcher_config_snapshot`。
    
    这些都属于**以后单独的一次控制面升级**，届时要有自己的门禁、窗口与授权，不在本阶段。另外，三个单元都带 `Restart=on-failure`，所以共享目录中的代码一旦被替换，任何一次崩溃或 OOM 都会让新代码在无人值守时生效（`infra/systemd/account-stall-control-plane-{reader,writer}.conf`）。这就是本阶段不碰共享目录的原因。
  - **部署前只读核对（证据）**：
    - 共享目录中每个受版本控制的文件，逐个核对 sha256 与 `67b401a` 基线一致，一致则输出 `MANIFEST_OK cp-shared-vs-67b401a`。（G-47，收录 wac-096 第五轮 🟢-3 与 wac-108 🟡-6，修订 G-32 的"不一致即停工"）不一致时：
      - 输出 `MANIFEST_DRIFT cp-shared-vs-67b401a` 并停工，不修复、不覆盖、不还原任何文件；
      - 漂移清单每行为 `MODIFIED <相对路径> <当前 sha256>` 或 `MISSING <相对路径>`，按路径排序；`drift_sha256` 是整份清单文本的 sha256，因此**同时绑定了路径与内容**；
      - 由用户按 O0-A08D 决定：(A) 接受恰好这份差异，preflight 加 `--accept-shared-drift <drift_sha256>` 重跑；差异的路径或任一文件内容有任何变化，都会得到另一个 `drift_sha256`，仍然停工；(B) 先另立授权任务，把共享目录对齐。
      
      无论选哪条路，preflight 之后对共享目录的全量快照（每个文件的 sha256、mode、uid、gid）在 apply 前、apply 后、回滚后都必须逐项相同，这是硬门禁。本阶段本来就不读写共享目录，真正的不变式正是"前后逐字相同"；
    - 全部单元的 `NeedDaemonReload`，范围至少包括 `trader-v3-*`、`caddy` 与交易节点相关单元，最好直接用 `systemctl list-units --all` 取全量。任一为 `yes` 就停工，不执行 daemon-reload，输出 `DAEMON_RELOAD_PENDING units=<单元列表>`，由用户按 O0-A08R 决定是否单独授权一次 reload（wac-096 第五轮 🟢-4）。这维持 wac-060 已定的规则：`daemon-reload` 作用于整台主机，会把别处待加载的改动一并加载；
    - 记录另三个控制面单元的 MainPID 与启动时间；
    - 8186 未被监听；
    - 共享 venv 中新代码所需的依赖齐全。（G-48，收录 wac-096 第五轮 🟡-C）这一项**就是**下面这次 import 冒烟，不另跑 `pip check`、`pip install --dry-run`，也不以 root 身份不带 `-B` 做 import，因为这些做法可能写入 pip 缓存或 `.pyc`：
      - 以新单元的 OS 用户身份运行 `.venv-cp/bin/python -B`，`env -i` 只放白名单 env 与 `CONTROL_PLANE_APP_ROLE=watcher-gateway`，工作目录是 staging 中展开的新目录下的 `services/control-plane/api`；
      - 执行 `import read_api` 与 `create_app("watcher-gateway")`，要求网关路由数大于 0；
      - `ImportError`/`ModuleNotFoundError` 输出 `DEPENDENCY_MISSING <模块名>` 并停工，其他非零退出同样停工；
      - 事后断言共享 venv 与新目录的文件清单、元数据都没有变化（没有生成 `__pycache__`）。
      
      这次冒烟同时承担新单元步骤第 4 步的审计钩子检查。
  - **新单元步骤**：
    1. 上面的只读核对全部通过；
    2. 创建 OS 用户；
    3. 用 `git archive` 展开新目录，按清单做 sha 校验，权限设为 root 0755；
    4. 用新目录与白名单 env 做 import 冒烟，并用审计钩子证明没有从新目录与 venv 以外加载模块或文件；
    5. 按白名单生成 env 文件（只打印变量名）；
    6. 安装单元文件；
    7. 再核对一次 `NeedDaemonReload`：除第 6 步装的新单元外全为 `no`；
    8. `systemctl enable --no-reload trader-v3-controlplane-watcher-gateway.service`；
    9. **紧接着**执行本阶段唯一一次 `systemctl daemon-reload`，它同时加载新单元与 wants 链接；
    10. 再核对一次 `NeedDaemonReload`，这次全部单元（含新单元）都必须为 `no`，以此证明第 9 步已清掉 manager 级的过期标志；
    11. 核对另三个单元的 MainPID 与启动时间都没有变；
    12. `systemctl start trader-v3-controlplane-watcher-gateway.service`；
    13. `127.0.0.1:8186/health/role` 返回 `gateway: enabled`；
    14. 回环探针；
    15. 再核对一次共享目录的全量快照（与 preflight 逐项相同）；
    16. 舰队守卫。
    
    （G-49，收录 wac-108 🟡-1 与 wac-096 第五轮 🟡-B）为什么这样排序：
    - 在 systemd ≥ 255 上，只要 enable/disable 真的改了单元文件状态，就会置位 manager 级标志 `unit_file_state_outdated`（v255 `src/core/dbus-manager.c:2524` 等处）；
    - 标志置位之后，`unit_need_daemon_reload()` 对**每一个**单元都返回真（`src/core/unit.c:3915`），直到下一次 daemon-reload 才清零（`manager.c:3586`）；v254 及以前的版本没有这个行为；
    - 如果按旧顺序"先 reload、后 `enable --no-reload`"，apply 之后全机 NeedDaemonReload 都会变成 `yes`，verify 的隔离门禁就再也分辨不出是否有别的单元真的待加载；
    - 不带 `--no-reload` 的 `enable`/`disable` 会隐式执行一次 daemon-reload（systemctl(1)），所以本阶段一律不用 `--now` 形式。
  - **影响窗口**：
    - 面板与 app 的现有读请求经 operator-query，零影响；
    - `/m/v1/watcher/*` 是新功能：阶段 C 之后、新单元启动之前返回 502；
    - Caddy 重启（阶段 C）沿用 D-02，本修订没有增加重启次数。
  - **回滚**：只删新增的东西。（G-49 按"先撤暴露，reload 门看 disable 之前的快照"重排）
    1. **记录**一次全量 `NeedDaemonReload` 快照。它只作记录，不是门槛，所以回滚永远不会在这一步卡住；
    2. **无条件撤掉暴露**：`systemctl stop` 新单元，`systemctl disable --no-reload`，并断言新单元不处于 active 或 activating；删除 env 文件与新目录。这一步不经过 reload；
    3. **reload 门按第 1 步的快照判定**（排除新单元）：
       - 快照干净：删除单元文件与系统用户，执行 `daemon-reload`，然后再核对一次，全部单元都必须为 `no`；
       - 快照不干净：输出 `DAEMON_RELOAD_PENDING units=<列表>`，保留一个已停止、已禁用的单元文件，由用户按 O0-A08R 决定。这时暴露已经撤掉，方向是安全的。
    
    回滚前后都核对另三个单元的 MainPID 与共享目录的全量快照没有变。回滚不触碰共享目录，也不触碰另三个单元。
  - `scripts/jp24-p1-control-plane.sh` 的 `apply` 会覆盖共享目录，并重启全部角色，**本阶段不得使用**。RS-18 只给它的数组与模板补上新角色，并在头注释中写明本阶段禁用，供以后的整体控制面升级使用。
- **PC-6（O0-A05P 之前确认；取代 WGW-1.0.3 版）**：
  - (i) 候选 Caddyfile 上 V-1 的输出：没有 `GATEWAY_PORT_EXPOSED`；逐项确认每个 `UPSTREAM_UNRESOLVED` 在生产中不会解析到 8186。
  - (ii) jp-24 上 8186 空闲，并记录 8184–8189 的占用情况（取自 O0-A01 S-06 的 `ss -ltnp` 输出，不需要新动作）。
  - (iii) jp-24 上除 Caddy 外，没有进程或规则把外部流量送到 `127.0.0.1:8186`（其他反向代理、SSH 隧道、iptables DNAT 或端口转发，含 unix socket 转发）。上线后由 S-06 复核 8186 只监听回环。可选的佐证是只读的 `ss -tnp`，需要授权。
  - (iv) 公网前面有没有 CDN 或中间层。V-5 只看状态码；如果中间层会改写状态码，由用户决定怎么处理。
  - (v) 只读核实生产 `operator-query.env` 中是否含 `AUTH_SECRET_KEY` 这个变量名：只看变量名，不看值，供 U-13 (iii) 决定。
  - (vi) 部署前只读核对的输出：共享目录与 `67b401a` 一致，或者 O0-A08D 已就 `MANIFEST_DRIFT` 作出决定（G-47）；`NeedDaemonReload` 全为 `no`，或者 O0-A08R 已作出决定。
  - (vii) （G-49）jp-24 的 `systemctl --version` 第一行，取自 O0-A01 S-01 的只读输出。它决定上面第 8–10 步的理由是否成立（≥ 255）；无论版本高低，步骤都按 G-49 的顺序执行。
- **U-13（取代 WGW-1.0.3 版；O0-A08 的前置）**：
  - (i) 选项 (e) 与 R23 已定，作记录；
  - (ii) 用户确认部署方式：新增单元与独立目录；不重启、不改动三个现有单元及其代码目录；本阶段没有 D-04；
  - (iii) watcher-gateway 是否持有 `AUTH_SECRET_KEY`：**默认不持有**，只在 PC-6 (v) 与"app 需要会话 token"两个条件同时成立时才持有；
  - (iv) (c′) 不再是网关的前置，撤销。
  
  O0-A08 的前置有四项：WGW-1.0.4 的实现（B、RS 清单）经审查合入；U-13 (ii)、(iii) 已确认；PC-6 已确认；O0-A05 的 V-5 已通过。O0-A05P 要等 B-1..B-3（片段 v2）与 RS 清单合入之后再申请。
- **方案比较（更新）**：
  - (e) **采纳**（用户裁决），代码目录按 R23 独立。
  - (a) 守卫降为纵深防御。
  - R22 封闭白名单**作废**。
  - (b) 否决、(c) 不采纳，结论不变。
- **残余风险**：
  1. Caddy 以外的本机转发，以及 `UPSTREAM_UNRESOLVED` 的实际取值，都只能靠人工确认（PC-6 (i)、(iii)）。
  2. 以后改 Caddyfile 时，误写 8186 的对象要到下一次跑 V-1 或 V-5 才会被发现，所以每次改 Caddy 后都应该跑一次 V-1（只读，本机即可完成）。
  3. 生产上存在两套控制面代码：共享目录中的 `67b401a`，以及新目录中的集成分支。以后整体升级控制面时，要把新目录并回共享目录，或者对它单独升级，由那次升级的契约与 runbook 规定；新目录在命名上带 release sha，便于识别。
  4. watcher-gateway 持有四个 reader token。它一旦失陷，影响相当于泄漏了这四个 token，但它不持有数据库凭据、signal token、快照 token、节点 token，默认也不持有 `AUTH_SECRET_KEY`。
  5. 面板 `/v1/*` 对其他端点的匿名 observer 读（已证实，§9.15）不因本修订改变。
  6. 计划 v0.6 §2.1 需要由 Planner 按用户裁决修订（§9.15）。

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

**WGW-1.0.4 裁决与待确认**（Planner 转达；任何 Agent 的转述都不算确认）：

- **已裁决（用户 2026-09-29）**：入口隔离采用选项 (e)，即网关独立监听端口（§9.14.6）。WGW-1.0.3 草案中的 R22 封闭白名单与 (c′) 随之撤销。**计划 v0.6 §2.1 的拓扑（"operator-query 网关"）需要由 Planner 按此裁决修订**；在修订之前，这一处以契约与用户裁决为准，不算"契约与计划冲突"。
- **U-13**（O0-A08 的前置）与 **PC-6**（O0-A05P 的前置）：内容见 §9.14.6（WGW-1.0.4 版）。
- **D-04**（WGW-1.0.4 第四轮更正）：watcher-gateway 使用独立代码目录（R23），本阶段不改共享代码目录，也不重启任何现有单元，所以**本阶段不产生 D-04**。首版称"磁盘新代码对 operator-query 只少了网关路由"，这一说法不实，已删除；真实差异见 §9.1 与 §9.14.6。

**范围外，但需告知用户（WGW-1.0.4 更新：已证实）**：本勘误不处理，也不改 §1–§8；修复另立任务。

- **事实**（来源：Planner 转述。按 AGENTS.md"不采信转述"，请 Planner 把当时的原始输出（状态码、`content_type`、字节数，不含 body）附进任务记录）：Planner 于 2026-09-29 从本机匿名（不带任何凭据）请求生产站点，得到以下结果（正文未留存）：

  | 请求 | 结果 |
  |---|---|
  | `GET https://jp-bot.balen.wang/v1/accounts` | `200 application/json`，1237 B |
  | `GET /v1/positions` | `200`，6862 B |
  | `GET /v1/watcher/status` | 404 |
  | `GET /m/v1/accounts` | 401 |

  结论：面板的 `handle /v1/*` 确实无条件注入 `SYSTEM_OBSERVER_TOKEN`，站点前面没有别的认证层，**公网匿名可读**账户、持仓等 reader 可读的 `/v1/*` 交易数据。
- **没有写入风险**：写接口要求 `risk_admin`（`_require_operator_principal` → `can_write_operator_orders`，§9.1），observer 是 reader，写请求得到 403。
- **与本契约的关系**：网关在 8186，上线后这一暴露也不会扩大到 watcher 数据（§9.14.6）。但现存的交易数据暴露不因本修订改变，建议优先处理。
- **推荐修复方向**：这是**独立任务**，需要用户**单独授权**，不属于本勘误，也不属于 O-0。涉及 Caddy 重启时，按 D-02 与阶段 C 合并为一次重启。
  1. **首选：面板 `/v1/*` 改为透传浏览器自己的凭据，不再注入 observer。** 面板前端已登录时，读请求本来就自带 `Authorization: Bearer <登录 token>`（§9.1，`api.ts:616-622`、`:721-731`），控制面也能校验登录服务签发的 HS256 会话（`principal.py:132-146`）。于是 Caddy 只需删掉 `header_up Authorization …`，未登录的请求就会得到 401。需要一并处理的兼容项如下（WGW-1.0.4 第四轮补全）：
     - 实时流 `/v1/stream` 用的是 `EventSource`（`hooks/useRealtime.ts:66`），浏览器无法为它设置 `Authorization`。要改用带凭据的 fetch 流，或者改用短期 query token 或 Cookie。
     - **写操作会真正生效**。已登录用户经 `/v1/*` 发出的写请求，会从"被 observer 覆盖而 403"变成真正生效：
       - `risk_admin` 的 `/v1/commands`、`/v1/operator/orders` 会生效，而登录服务的默认角色就是 `risk_admin`，所以这涉及全部已登录用户；
       - 已登录的 reviewer 对 `/v1/risk/decisions/*/approve|reject` 的审批也会生效（`read_api.py:6138-6140`）。
       
       这是行为变化，需要用户认可；或者在 Caddy 上只对 GET/HEAD 放行。
     - **settings 路由不认登录会话**。`/v1/order-management/settings*` 用自己的静态 token 表（`settings/router.py:19-41`），改为透传浏览器凭据之后，面板的设置页（包括读取）会得到 403 `forbidden`，需要让 settings 路由接受会话，或者单独处理。
     - **operator-query 可能要重启**。会话校验要求 operator-query 持有与登录服务相同的 `AUTH_SECRET_KEY`，而按仓库的 bootstrap，它默认不持有（§9.1）。如果生产上也没有，就要改 operator-query 的 env 并重启它。这是一个**交易影响窗口**：Hermes 与 signal worker 经 operator-query 下单（§9.1），所以必须选在信号稀少、用户在场的窗口，并按部署门禁执行。
     - **面板构建可能不带 token**。以 `VITE_AUTH_DISABLED=true` 构建的面板从不带 token（`utils/api.ts:431-438`），需要先确认生产构建没有设置这个变量，否则改完后面板全部 401。
     - 前端对 401 的处理（跳转登录页）。
  2. **备选：先认证、再注入。** 在面板 `/v1/*` 前加 Caddy 层的认证（`forward_auth` 到登录校验端点，或 `basic_auth`），只有通过后才注入 observer。改动小，但 observer 仍是共享身份，审计上分不出是谁。
  3. **不推荐**：只收窄注入的路径或方法。数据仍然匿名可读。
- **核实**：修复后，用与上表相同的四个匿名请求复核：`/v1/accounts`、`/v1/positions` 应为 401；`/m/v1/accounts` 仍为 401；`/v1/watcher/status` 仍为 404。每次都需要用户授权，body 不落盘。

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

**WGW-1.0.2 → WGW-1.0.3（2026-09-28；草案，从未合入，已被 WGW-1.0.4 取代。以下各轮 G-01..G-29 与两份清单仅存档，仍然有效的只有片段格式 v2（G-02、G-03）、B-1..B-5、RS-1，以及 V-5 的发起位置（G-14）与 O-3"未完成"规则（G-27），见 WGW-1.0.4 的 H 表）**。来源：审查报告 `docs/agent-team/reviews/wac-094.md`（审查任务 wac-095）的 🟡-1（面板 `/v1/*` 等非移动入口把 `/v1/watcher/*` 连同注入的 observer token 交给 operator-query）、同报告 §9 的 PC-6 与 U-13 建议；任务 wac-096。§9 的接口仍未部署。**§1–§8 未改动；YAML 零改动**（`contract_version` 字段仍为 `WGW-1.0.2`，`yaml_sha256` 不变，已冻结的路由行 P0–P3 全部不变）；网关代码、角色 scope、结构码不变；既有 `/v1` 端点的字段、状态码与语义不变。唯一对外可见的变化：经 Caddy 非 `/m` 入口请求 `/v1/watcher` 前缀，由现在的 operator-query FastAPI JSON 404（`{"detail":"Not Found"}`）变为 Caddy 的空 body 404。`/v1/watcher/*` 从来不是既有端点，所以这不属于 §9.13 所说的破坏性变更。

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

**（WGW-1.0.3 存档；B-1..B-5 由 WGW-1.0.4 继续沿用，B-6、B-7 被 WGW-1.0.4 的 B-13、B-14 取代）后端执行者清单（B-1..B-7；文件范围：`scripts/contracts/`、`contracts/generated/caddy-watcher-gateway.caddy`、`tests/control-plane/`；不改服务代码）**

1. **B-1 生成器**：`scripts/contracts/watcher_gateway_routes_lib.py` 的 `_render_caddy`（`:303` 起）按 §9.14.3"格式 v2"输出：`_format watcher-gateway-caddy-snippet.v2`；`(watcher_gateway_routes)` 在兜底组之后追加 `@wgw_direct` 组；随后输出 `(watcher_gateway_direct_guard)`。守卫正则由 `paths.app_outer_prefix` 拼出，不写死。`validate()` 的 S-22 检查（`:274`）把 `wgw_direct` 加入保留名。
2. **B-2 校验脚本**：`scripts/contracts/check_watcher_gateway_routes.py`（现行片段断言在 `:78-84`）按 §9.14.4 第 1 项的 WGW-1.0.3 条目实现：v2 形状、守卫正则逐字（脚本内独立拼出）、匹配与不匹配探针、两个前缀正则互不相交、"G1 ⇒ D"取值池预言（某个参数过滤后没有取值即失败）。比较行数为 0 即失败。
3. **B-3 重新生成**：`python3 scripts/contracts/gen_watcher_gateway_routes.py --phase-max P2`。提交前用 `git diff --stat` 确认四份生成物中只有片段变化；`python3 scripts/contracts/check_watcher_gateway_routes.py` 末行 `ROUTES_DIFF_EMPTY`，`rows` 与 `yaml_sha256` 都与改动前相同，`phase_max=P2`。
4. **B-4 片段测试**：`tests/control-plane/test_caddy_watcher_gateway_paths.py` 改为期望 v2：`_format`、两个 snippet 的顺序、守卫组逐字、片段不含 `*`；`EXPECTED_P2_LINES` 与 `P3_PATHS` 不变；守卫正则的探针在测试里独立写一份，不引用 lib。
5. **B-5 网关侧回归测试**（`tests/control-plane/api/test_watcher_gateway.py` 新增用例，`.venv-arch`，不 import yaml）：
   - (1) **G1 ⇒ D**：对每条已注册的 gateway 行，用 B-2 同一取值池（测试内独立写一份）构造能通过 G1 的路径，先断言它确实通过 G1（到达 endpoint，`scope["state"]` 中有该行 id），再断言测试内独立写出的守卫正则用 `re.search` 命中它。
   - (2) **残余形状在认证之前被拒**：在 ASGI scope 层构造 `path`/`raw_path`：`/v1/watcher/../accounts`、`/v1/watcher/..%2Faccounts`、`/v1/watcher%2e%2e/accounts`、`/v1/watcher%25`、`/v1/watcher//status`、`/v1/watcher/./status`、`/v1/watcher/status/..`，每个请求都带夹具生成的**合法静态 `SYSTEM_OBSERVER_TOKEN`**，方法取 GET 与 HEAD。期望 404 `route_not_found`，`resolve_principal` 的调用计数为 0，上游请求计数为 0。（WGW-1.0.3 第二轮，审查 wac-098 🟡-5）计数补丁必须打在 **`watcher_gateway.resolve_principal`** 上，即 `monkeypatch.setattr(watcher_gateway, "resolve_principal", spy)`，其中 `spy` 包装原函数、计数后照常返回。原因是 `watcher_gateway.py:21` 用 `from security.principal import … resolve_principal` 按名字导入，打在 `security.principal.resolve_principal` 上的补丁永远计 0，测试恒绿却什么也没测。**正向对照**（同一个测试、同一个 spy、同一个夹具）：带同一个 token 请求能通过 G1 的 `GET /v1/watcher/status`，计数必须恰好变成 1，并且到达上游桩。正向对照不成立，本条判失败。
   - (3) 锁定计划：直连 operator-query、用静态 `SYSTEM_OBSERVER_TOKEN` 请求 `GET /v1/watcher/status`，经上游桩得到 200。现有四角色测试已覆盖的，确认它仍在即可，不重复。
   - 若 (2) 在现有代码上失败，说明 L-1 的前提不成立：停工上报 Planner，不得自行改网关语义。
6. **B-6 不改**：`services/control-plane/api/watcher_gateway.py`、`read_api.py`、`security/*`、`contracts/watcher-gateway-routes.yaml`、两份代码生成物、Caddy 清单、watcher 代码。不加任何按角色、按来访头区分入口的逻辑。
7. **B-7 验证命令**：`python3 scripts/contracts/check_watcher_gateway_routes.py`；`.venv-arch/bin/python -m pytest tests/control-plane/test_caddy_watcher_gateway_paths.py tests/control-plane/api/test_watcher_gateway.py -q`；watcher 的 node 测试照跑一遍，确认结果与改动前相同（生成的 JS 未变）。所有命令都带 `set -o pipefail`，不连 jp-24。

**（WGW-1.0.3 存档；只有 RS-1 由 WGW-1.0.4 沿用，其余被 WGW-1.0.4 的 RS-11..RS-20 取代，尤其 RS-2 的白名单**不得实现**）Release Steward 清单（RS-1..RS-10；文件范围：`scripts/ops/o0/`、`docs/agent-team/release/`；与 wac-097 的 🟡-2、🟡-3 同批）**

1. **RS-1 生成物核对**：`o0_caddy_watcher_routes.py` 的 `expected_snippet`、`load_snippet`、`cmd_check_artifacts`（`:290-338`）按格式 v2，从清单独立推导两个 snippet 与守卫组并逐字节比对；`_find_wgw`、`_check_wgw_structure`（`:1129-1202`）认出 `wgw_direct` 路由（正则逐字，handler 为 `static_response`、状态 404），要求它与兜底同处 wgw 连续块的尾部，顺序为兜底、守卫。R12 的核对表述"兜底逐字相同且在最后"改为"兜底之后紧跟直连守卫"。
2. **RS-2 V-1（WGW-1.0.3 第三轮按 R22 重写：白名单形状匹配，不模拟改写语义）**：以 `_forwarder_check`（`:1214-1252`）为起点，改为 §9.14.6 N-3 的封闭白名单：
   - **遍历**：全部 server 的 `routes`、`errors.routes`、`named_routes`，以及其下任意深度的 `subroute`、`reverse_proxy`/`intercept` 的 `handle_response[].routes`。任何其他含路由的字段，或 server 级未知键，都判 UNCOMPARABLE。
   - **转发器**：`reverse_proxy` 且某个 dial 的端口等于 `--upstream` 的端口，沿用 `dial_endpoint`；解析不了判 UNCOMPARABLE。
   - **判定顺序**：
     - (1) 容器不是主链 → FAIL；
     - (2) 已认定的片段转发路由 → `SNIPPET`；
     - (3) 按 N-3 的"链"定义算出链；互斥只认相同的非空 `group`，或字面 `host` 集合不相交；
     - (4) F 带 `rewrite` 对象 → 只能是 W-3，按 W-3 的逐项条件判定；
     - (5) 链上没有白名单外的 handler，且守卫在前 → W-1；
     - (6) 链上唯一的白名单外 handler 是 `rewrite{strip_path_prefix:"/m"}` → 按 W-2 的匹配器与头部条件判定；
     - (7) 其余 → FAIL，原因写明第一个不合格项。
   - **禁止**：保留现行"只认 `handler == "rewrite"`"的写法（`:1275`、`:1471`、`:1649`），以及任何按 `rewrite` 字段语义推算上游路径的逻辑。白名单之外不得有任何"算得出就放行"的分支。
   - N-3 与 wac-097 🟡-2 的前置遮蔽检查各自独立：后者管 `/m/v1/watcher` 前缀空间，前者管 I-1。
3. **RS-3 V-2**：对含 operator-query 转发的每个（server，host）组合，模拟 `P_D` 与反向对照 `/v1/watcherx`。（WGW-1.0.3 第三轮）模拟器遇到 `invoke`（含链式与成环）直接判 UNCOMPARABLE，不展开；selftest 覆盖链式与成环两种情形。
4. **RS-4 V-3**：扩展 `_probe_live_checks`（`:1930` 起）与 `pin_probe_config`（约 `:1751`）。现行代码只运行探针站点所在的 server、丢弃其他 server；改为运行每一个含 operator-query 转发路由的 server。无法运行的，输出 `DIRECT_ENTRY_NOT_PROBED server=…`，探针判失败（WGW-1.0.3 第二轮）。按 host 发送 `P_D`、百分号形式、`PROBE_PARAM_VALUES` 取值与 `//` 变体，期望 404、body 长度 0、`Content-Type` 为空、两类桩计数不变；残余形状按 V-3 的扩充列表发送（`#` 与 `\` 形式用原始 socket），只允许以"不在 T 内"或"含 `%`、`//`、点段"的 raw path 到达桩；另记录 `/v1/accounts` 的 `injected=yes|no`，不作断言，不打印头值。（WGW-1.0.3 第三轮）另实现 V-3 的"逐转发器标记探针"：按 V-1 列出的转发器与其匹配器构造触发请求，断言经非片段转发器到达桩的 raw path 不在 T 内，或者含 `%`、`//`、点段。
5. **RS-5 V-4**：`caddyfile_check`（`:1559` 起）增加 N-2 的文本检查：import 位置在所有 `handle`/`handle_path`/`route` 之前；同一站点 import 了两个 snippet 判失败；端口识别不了判 UNCOMPARABLE。
6. **RS-6 自测与真实 Caddy 测试（WGW-1.0.3 第三轮按 R22 重写；每个用例都用真实 Caddy v2.10.2 adapt 后交给 V-1，并做 V-3"已知旁路回放"）**：
   - 期望 V-1 判 PASS，活体上没有 G1 能放行的 T 内路径到达桩：
     - 仿生产夹具加 v2 片段：`@mobile` → W-2，面板 → W-1，片段 → SNIPPET；
     - 只有守卫的站点中，站点顶层 `reverse_proxy /v1/* 127.0.0.1:8183 { header_up Authorization … }` → W-1，import 写在它之后也一样；
     - 站点顶层不带匹配器的 `forward_auth 127.0.0.1:8183 { uri /v1/auth }` → W-3；
     - `handle /foo/* { reverse_proxy 127.0.0.1:8183 { rewrite /v1/ping } }` → W-3；
     - 第二站点 import 守卫并用 `handle /v1/*` 注入 → W-1。
   - 期望 V-1 判 FAIL，而且活体确有旁路（桩收到 G1 能放行的 T 内路径）：
     - `handle /foo/* { reverse_proxy 127.0.0.1:8183 { rewrite /v1/watcher/dialogs; header_up Authorization … } }`，触发 `/foo/x`；
     - q1 `handle_path /x/* { reverse_proxy 127.0.0.1:8183 { rewrite ?a=1; header_up … } }`，触发 `/x/v1/watcher/dialogs`；
     - q2 `handle_errors { reverse_proxy 127.0.0.1:8183 { rewrite ?a=1; header_up … } }`，主链 `forward_auth` 到不可达端口，触发 `/v1/watcher/dialogs`；
     - q3 `handle_errors { reverse_proxy <另一个桩> { rewrite /ping; @ok status 2xx; handle_response @ok { reverse_proxy 127.0.0.1:8183 { header_up … } } } }`，触发同 q2；
     - q4 `handle_path /x/* { reverse_proxy 127.0.0.1:8183 { rewrite "#frag"; header_up … } }`；
     - `handle_errors { reverse_proxy 127.0.0.1:8183 { header_up … } }`；
     - `handle_path /x/* { invoke obs }`，`&(obs)` 转发到 8183；
     - `@m path /m/v1/*` 加 `uri strip_prefix /m` 转发到 8183；
     - `handle_path /p/* { reverse_proxy 127.0.0.1:8183 }`；
     - 合成 JSON：`forward_auth` 的 `rewrite` 为 `{"method":"GET","uri":"/v1/auth","uri_substring":[{"find":"auth","replace":"watcher/dialogs"}]}`，用 `caddy run --config <json>` 运行，任意请求都触发；
     - wac-094 的 g16、g17、g18。
   - 期望 V-1 判 FAIL，属于保守失败（只断言静态 FAIL，活体结果记录为"没有 G1 能放行的旁路"）：
     - `forward_auth 127.0.0.1:8183 { uri /v1/auth }` 排在面板 `handle /v1/*` 之前时的面板；
     - 站点顶层 `reverse_proxy /v1/* …`，同站点另有 `handle /w/* { uri strip_prefix /w; reverse_proxy … }`；
     - `reverse_proxy 127.0.0.1:8183 { rewrite v1/watcher/dialogs }`、`{ rewrite /v1/./watcher/dialogs }`、`{ rewrite /v1/watcher/../watcher/dialogs }`；
     - `handle /v1/* { invoke obs }`；
     - `reverse_proxy 127.0.0.1:8183 { rewrite /v1/watcher{path} }`；
     - `forward_auth 127.0.0.1:8183 { uri /v1/watcher/status }`、`{ uri {uri} }`；
     - 链上有 `method GET` 指令的面板（`rewrite` 只带 `method`）。
   - 期望 UNCOMPARABLE（判 FAIL）：
     - 合成 JSON 中某个 handler 带未知的路由列表字段；
     - server 级未知键；
     - dial 写成占位符或 unix socket。
   - 结构类用例（沿用第一轮）：删去守卫组、守卫正则去掉 `%` 或 `(?i:`、`import watcher_gateway_routes` 挪到 `handle /v1/*` 之后 → FAIL；同一站点 import 两个 snippet → adapt 失败，判 FAIL。
   - 夹具 `Caddyfile.prodlike.in` 不需要为守卫改动。
7. **RS-7 V-5 @ O0-A05**：`o0_deploy_caddy.sh --phase verify` 加入 V-5 的回环补充检查；另提供一个在外部机器上运行的只读命令（例如 `o0_tool.py public-direct-check --host <公网主机名>`）：用真实 DNS，不用 `--resolve`、不用代理，五个 GET 只输出状态码、`size_download`、`content_type`，不落盘、不打印 body。它的输出作为证据交给 verify。判据为 404、长度 0、`content_type` 为空。任一不通过即 verify 失败，按现行处置办理。
8. **RS-8 V-5 @ O0-A08**：`o0_deploy_operator_query.sh` 的三处检查：O-1 preflight 要求外部检查证据作为输入，并按 V-5 的四条逐项核对（WGW-1.0.3 第三轮）：内容、`t_sha` 与 `t_ext` 相差 ≤ 5 分钟、证据产生后 ≤ 60 分钟、当场用 admin API 取的运行配置 sha 与证据一致。缺失、过期、不符或不通过，都输出 `DIRECT_GUARD_MISSING` 并阻断，不写任何东西。取 sha 的只读命令须另行提供，与外部检查同时运行，只输出 sha 与时刻；O-2 apply 在重启之后、自动回滚窗口之内做回环补充检查，不通过即 O-4；O-3 verify 外部检查必做，不通过即 O-4；拿不到授权或做不成时，输出 `DIRECT_GUARD_UNVERIFIED`，结论为"未完成"，脚本不得输出任何"上线完成"类标记（WGW-1.0.3 第三轮）。公网请求一律不带凭据与 Cookie，只输出状态码、`size_download`、`content_type`。授权清单逐点写明外部请求。
9. **RS-9 打包门禁**：`o0_package.sh` 对片段 `_format` 的期望改为 v2；其余门禁不变。A05P 与 A05 的绑定改用新的 `snippet_sha256`。
10. **RS-10 文档**：
    - `o0-runbook-deploy.md`：§3.1 第 7 条加入 PC-6（文字取自 §9.14.6）；阶段 C 的 verify 行与 O-1、O-2、O-3 行加入 V-5。
    - `o0-authorization-list.md`：第四节加入 U-13 与"范围外告知"备注；O0-A05P 的前置加上"WGW-1.0.3 实现已合入、PC-6 已确认"；O0-A05 加上 V-5；O0-A08 的前置加上"U-13 (i)(ii) 已确认、O0-A05 的 V-5 已通过"。
    - `o0-requirements.md`：新增一行"非移动入口不触达网关"，映射到 §9.14.6 的 I-1 与 V-1..V-5。
    - `o0-site-checklist.md`：加一条公网检查（A05 之后，期望守卫的空 404）。
    - 验证：`run_all.sh` 带与不带 `O0_CADDY_BIN` 各跑一次；`o0_package.sh --candidate <提交> --report-only --run-tests`（不带 `--execute`）；不连 jp-24。

**WGW-1.0.3 第二轮（2026-09-28，版本号不变）**。来源：审查报告 `docs/agent-team/reviews/wac-096.md`（审查任务 wac-098，FAIL：1 🔴、5 🟡、8 🟢）。WGW-1.0.3 尚未合入集成分支，也没有任何实现按它上线，所以不另起版本号，首轮的 G-01..G-10 仍然有效，本轮条目优先。只改 §9 的文字：§1–§8、YAML、四份生成物全部不变；既有 `/v1` 端点的字段、状态码与语义不变。本轮的本机实测用真实 Caddy v2.10.2 完成，`HOME`/`XDG_*` 指向临时目录，token 随机生成，没有连接 jp-24，也没有访问生产 URL。

| # | 来源 | 改动 | 位置 |
|---|---|---|---|
| G-11 | wac-098 🔴-1 | 新增"改路径"定义：(i) `rewrite` handler，或 (ii) `reverse_proxy` handler 自带的 `rewrite` 对象，带 `uri`/`strip_path_prefix`/`strip_path_suffix`/`uri_substring`/`path_regexp` 任一字段即算，只带 `method` 的不算；删去"`forward_auth` 生成 `rewrite` handler"的错误括注。N-3 的范围包括 `reverse_proxy.rewrite`；N-3 (b) 适用于 `reverse_proxy.rewrite.uri`，自身 `rewrite` 对象中的常量 `uri` 按 (b) 判定后不再适用 N-4，由此消除 RS-6 中"`forward_auth { uri /v1/auth }` 期望 PASS"与 N-4 的矛盾；落在 T 内的常量、带占位符的 `uri` 判失败；N-4 只适用于链上完全没有改路径的转发器。RS-2 要求同时识别两种改写；RS-6 补用例，含审查的 `reverse_proxy { rewrite /v1/watcher/dialogs; header_up … }` 旁路 | §9.1、§9.14.6 定义、L-1、N-3、N-4、本机实测；§9.16 RS-2、RS-6 |
| G-12 | wac-098 🟡-1 | 转发路由的枚举范围逐一写明：`routes`、`errors.routes`、`named_routes`（经 `invoke`）、`subroute`、`handle_response[].routes`，未知容器判 UNCOMPARABLE。新增 N-5：错误链里无法再放一份守卫（本机实测：adapt 报 `@wgw_direct` 重复定义），所以错误链中的转发器只能按 N-3 通过；命名路由按调用方上下文判定；`handle_response` 按外层上下文判定。V-1 按该范围输出；V-4 以 V-1 为准；RS-6 补 `handle_errors` 与 `invoke` 用例 | §9.14.6 定义、N-5、V-1、V-4；§9.16 RS-2、RS-6 |
| G-13 | wac-098 🟡-2 | V-5 的媒体探针由 HEAD 改为 GET；五个请求的判据统一为"404、`size_download == 0`、`content_type` 为空"（守卫的 404 没有 `Content-Type`，本机实测）；V-3 的期望同样加上 `Content-Type` 为空 | §9.14.6 V-3、V-5；§9.16 RS-4、RS-7 |
| G-14 | wac-098 🟡-3 | V-5 的发起位置：必做的是外部检查（jp-24 以外的机器、公网主机名、真实 DNS、不用 `--resolve`/`--connect-to`/hosts/代理）；jp-24 回环 `--resolve`（现行 C-3）只能作补充。各执行点的分工：A05 verify 两者都做；O-1 以外部证据为必需输入；O-2 窗口内做回环检查；O-3 外部检查必做。每个执行点的外部请求逐点写进授权清单，由用户授权 | §9.14.6 V-5；§9.16 RS-7、RS-8 |
| G-15 | wac-098 🟡-4 | V-3 必须覆盖每一个含 operator-query 转发路由的 server；未被探测的输出 `DIRECT_ENTRY_NOT_PROBED` 并判失败，不得静默跳过 | §9.14.6 V-3；§9.16 RS-4 |
| G-16 | wac-098 🟡-5 | B-5 (2) 的计数补丁打在 `watcher_gateway.resolve_principal` 上（模块按名字导入，`watcher_gateway.py:21`）；加正向对照：同一 spy 对 `GET /v1/watcher/status` 计数必须恰为 1，否则本条判失败 | §9.16 B-5 |
| G-17 | wac-098 🟢-1、🟢-2、🟢-3 | §9.1 的 Caddy 引文更正为 `cleanPath(r.URL.Path)`（`matchers.go:701`、`caddyhttp.go:302-308`，本机 v2.10.2 源码核对），补上 `EscapedPath`、`reverse_proxy.rewrite` 与路由容器的源码位置；L-1 改为"raw path 等于原文，或是它的重新编码（必含 `%`）"；T 的定义按代码的顺序改写（先解码、再取 `path_part`），与 `watcher_gateway.py:184-189` 逐步相同 | §9.1、§9.14.6 定义与 L-1 |
| G-18 | wac-098 🟢-4、🟢-6、🟢-7 | 采纳：V-3 的残余形状补上 `%3F`、`%23`、`%00`、`%0D`、`%20`、`%5C`、`;`、原始 `#` 与 `\`，判据改为"不在 T 内，或含 `%`/`//`/点段"；范围外告知补"作为 L-P7 并入 A01"的选项（由用户决定）；PC-6 (iii) 补可选的只读 `ss -tnp` 佐证（需授权） | §9.14.6 V-3、PC-6；§9.15 |
| G-19 | wac-098 🟢-5、🟢-8 | 属于 Planner 的事项，契约只记录建议，不改规范：把 (e)"网关单独占一个监听端口"登记为 O-0 之后的加固项；B 清单与 RS 清单同批推进，并以 B-1..B-3 合入为 RS 的前提，缩短"契约写 v2、生成物仍是 v1、check 照样通过"的窗口 | §9.16（本条） |

本轮对两份实现清单的增量都已直接写进上文 B-5、RS-2、RS-4、RS-6、RS-7、RS-8 的条目。首轮 G-06 表格中"HEAD `/v1/watcher/media/…`"与"判据为 404 且长度 0"的写法，以 G-13 为准。

**WGW-1.0.3 第三轮（2026-09-28，版本号不变）**。来源：审查报告 `docs/agent-team/reviews/wac-096.md`"第二轮"（复审 wac-098 r2，FAIL：2 🔴、1 🟡、4 🟢）与其 Gap Analysis；Planner 裁定 R22（N-3/N-4/N-5 收敛为封闭白名单）。WGW-1.0.3 仍未合入，不另起版本号；本轮条目优先于第一、二轮中与之冲突的内容，第一、二轮的 N-3、N-4、N-5 条文作废，G-04、G-11、G-12 中与之相关的描述仅作历史记录。只改 §9 文字：§1–§8、YAML、四份生成物不变；既有 `/v1` 端点不变。本轮实测条件与前两轮相同：真实 Caddy v2.10.2，`HOME`/`XDG_*` 指向临时目录，token 随机生成，上游全部是本机桩，不连 jp-24，不访问生产 URL。

| # | 来源 | 改动 | 位置 |
|---|---|---|---|
| G-20 | R22；r2 Gap Analysis 方向 1 | N-3 改为封闭白名单：非片段的 operator-query 转发器只允许 W-1（面板形状）、W-2（`/m` 剥前缀形状）、W-3（`uri`/`method` 常量改写形状）三种，其余与无法判定的一律 FAIL；给出"链"（含互斥的两种情形）与链 handler 白名单的精确定义；"改路径"改为"改路径动作"，不区分字段；N-4、N-5 并入 N-3。R22 第 2 条"逐字等于生成物"只对片段转发路由成立，`@mobile` 按 W-2 的形状精确判定 | §9.14.6 定义、N-3、N-4/N-5 |
| G-21 | r2 🔴-A | 路径分量为空的 `uri`（`?a=1`、`#frag`）、不以 `/` 开头的 `uri`、键超出 `{uri, method}` 的 `rewrite` 对象（如 `uri` 加 `uri_substring`），都不满足 W-3，判 FAIL，并写进 N-3 反例；W-3 另排除 `//` 与点段 | §9.14.6 N-3 W-3 与反例 |
| G-22 | r2 🔴-B | 删去"外层 `reverse_proxy` 的 `rewrite` 算在 `handle_response` 链上"的错误表述；改为容器规则：`handle_response`（`reverse_proxy` 与 `intercept`）、错误链、命名路由中的转发器一律 FAIL，不区分语境 | §9.14.6 N-3 容器规则；删除第二轮 N-5 |
| G-23 | r2 🟡-A | O-1 的外部证据：内容（含 jp-24 上 admin API 取得的运行配置 sha，取得时刻与外部检查相差 ≤ 5 分钟）、时效（≤ 60 分钟）、绑定（O-1 当场取的 sha 必须一致），任一不满足即 `DIRECT_GUARD_MISSING` | §9.14.6 V-5；§9.16 RS-8 |
| G-24 | r2 🟢-A | V-2 模拟器遇到 `invoke`（含链式与成环）判 UNCOMPARABLE，不展开；V-1 不需要展开 `invoke` | §9.14.6 V-2；§9.16 RS-3 |
| G-25 | r2 🟢-B | 由 W-3 的"以 `/` 开头"覆盖；不以 `/` 开头的常量判 FAIL，不再依赖上游服务器不规范化请求目标 | §9.14.6 W-3 |
| G-26 | r2 🟢-C | PC-6 (iv)：确认公网前面有没有会改写 404 的 CDN 或中间层；有的话由用户决定让它直通，或召回 Architect 修订判据，O-0 不得临时放宽 | §9.14.6 PC-6 |
| G-27 | r2 🟢-D | O-3 的外部检查拿不到授权或做不成时，结论为"未完成"（`DIRECT_GUARD_UNVERIFIED`），不得宣告上线完成，由用户决定补授权还是执行 O-4 | §9.14.6 V-5；§9.16 RS-8 |
| G-28 | Planner 要求；r2 Gap Analysis 方向 2 | V-3 增加"逐转发器标记探针"（第二道防线）与"已知旁路回放"（每个 ★ 形状都要静态 FAIL，并在活体上证明确有旁路；保守失败的形状只断言静态 FAIL）；RS-2 改为白名单形状匹配的实现要求，RS-6 改为按 PASS、FAIL 有旁路、FAIL 保守、UNCOMPARABLE、结构五类列全部用例 | §9.14.6 V-3；§9.16 RS-2、RS-4、RS-6 |
| G-29 | r2 Gap Analysis 方向 3；wac-098 🟢-5 | (e)"网关单独占一个监听端口"在 §9.15 登记为 O-0 之后的加固选项，由用户决定是否立项，Architect 不自行启动 | §9.15 |

白名单的代价（写入 N-3"失败方向与代价"）：它是保守的，生产写法若落在三种形状之外，即使实际无害也会挡住 A05、A08。处置只有两种：改候选使其落入白名单，或召回 Architect 扩充白名单。本机原型对两轮全部已知形状的判定，以及真实 Caddy 的活体结果，见 §9.14.6"本机实测"第三轮一条。

对 B 清单无增量（B-1..B-7 不变）。RS 清单的增量已直接写进 RS-2、RS-3、RS-4、RS-6、RS-8。

**WGW-1.0.3 → WGW-1.0.4（2026-09-29）**。来源：wac-096 三轮复审 FAIL（`docs/agent-team/reviews/wac-096.md`）；用户裁决（2026-09-29）：入口隔离改为选项 (e)，即网关独立监听端口；协调者转达的设计要求 1–5。WGW-1.0.3 从未合入，WGW-1.0.4 在它之上修订。§1–§8 未改动；YAML 零改动（`contract_version` 字段仍为 `WGW-1.0.2`，`yaml_sha256 = 85eb4c88…6082`），两份代码生成物与 Caddy 清单逐字节不变；片段格式 v2 沿用 WGW-1.0.3，其重新生成由 B-3 完成。既有 `/v1` 端点的字段、状态码与语义不变。operator-query 唯一的变化是路由表中不再有网关路由：生产上的 operator-query 本来就没有网关，所以对外行为无变化。本版没有新增本机 Caddy 实测，理由是：新判据 I-2 只看上游端口；首轮已实测，守卫对 `/v1/watcher` 前缀有效；第三轮已实测，各类旁路形状拨 8183 时确实到达 8183（在 WGW-1.0.4 下它们不再构成风险）。

| # | 来源 | 改动 | 位置 |
|---|---|---|---|
| H-01 | 用户裁决 2026-09-29 | 新控制面角色 `watcher-gateway`：单元 `trader-v3-controlplane-watcher-gateway.service`，用户 `trader-v3-cp-watcher-gateway`，监听 `127.0.0.1:8186`；共享代码目录，同一 `read_api:app`，按 `CONTROL_PLANE_APP_ROLE` 区分 | （"共享代码目录"**已被 G-30 取代**）§9.14.6；§9.2 |
| H-02 | 设计要求 1 | 路由层面隔离：网关路由与前缀中间件只在 `create_app("watcher-gateway")` 上注册与安装；删除模块级注册；operator-query、node-control、event-ingest、all 都没有网关路由，`/v1/watcher/*` 得到 FastAPI 默认 404 | （"共享代码目录"部分**已被 G-30 取代**，代码改动只进入独立目录）§9.14.6；§9.4；§9.14.4 第 2 项 |
| H-03 | 设计要求 1（最小权限） | 不给数据库凭据：不新建 DB 角色，不复用 `trader_v3_operator_query`，不设 `DATABASE_URL` 与 `CONTROL_PLANE_EXPECT_DATABASE_ROLE`；取库一律 fail-closed；健康检查不连库 | §9.14.6 |
| H-04 | 设计要求 1 | env 文件按变量名白名单写入（网关 token、四个 reader token、四个 signal token、可选 `AUTH_SECRET_KEY`），列明禁止写入的变量；operator-query.env 不写入网关 token；跨服务互异检查改为按持有方分开 | （signal token 与 `AUTH_SECRET_KEY` 的部分**已被 G-36 取代**）§9.14.6；§9.2 |
| H-05 | 设计要求 2 | `(watcher_gateway_upstream)` 拨 `127.0.0.1:8186`，不做请求头操作；守卫保留为纵深防御；`(watcher_gateway_direct_guard)` 对其他站点改为可选；对 8183 等其他端口的转发不作形状判定 | §9.14.3；§9.14.6 |
| H-06 | 设计要求 2 | 入口不变式 I-2 取代 I-1：除片段外，任何对象的上游不得指向 8186（包括 `dynamic_upstreams` 与无法解析的 dial），遍历整份 JSON，不按容器建模 | （"dynamic/无法解析即失败"**已被 G-33、G-34 取代**）§9.14.6 |
| H-07 | 设计要求 2、4 | V-1..V-5 重写：V-1 的主判据改为 I-2；V-3 用 8183 与 8186 两个桩，只允许片段命中 8186；V-5 只看状态码（`/v1/watcher/*` → 404，`/m/v1/watcher/*` 不带凭据 → 401 或 403，阶段 C 后单元启动前为 502），发起位置沿用 G-14，O-3"未完成"沿用 G-27，O-1 外部证据的时效与绑定（G-23）作废 | （V-5 判据**已被 G-40 取代**）§9.14.6 |
| H-08 | 设计要求 3 | （**已被 G-30..G-32 取代**：本阶段不改共享目录，没有 D-04）部署与重启：不重启、不修改 node-control、event-ingest、节点；本阶段不重启 operator-query（信号链路经它下单，§9.1），按 D-04 记录；给出新单元步骤、影响窗口、回滚；`jp24-p1-control-plane.sh apply` 不得用于本阶段 | §9.14.6；§9.15 |
| H-09 | 设计要求 4 | PC-6 改为：V-1 输出、8186 与 8184–8189 的占用、本机代理与 DNAT、中间层 | §9.14.6 |
| H-10 | 设计要求 4 | U-13 改为：记录 (e) 已拍板、部署方式、`AUTH_SECRET_KEY` 的去留；撤销 (c′) 作为网关前置 | §9.14.6；§9.15 |
| H-11 | 设计要求 5 | 范围外告知更新为已证实（Planner 2026-09-29 的匿名请求结果），并给出推荐修复方向（首选透传浏览器凭据，含 `EventSource` 与写请求两个兼容项；备选先认证再注入）与修复后的复核方法 | §9.15 |
| H-12 | 设计要求 1、4 | §9.2、§9.3–§9.9、§9.14 中"operator-query 网关"一律改读为 watcher-gateway 角色；§9.7 在途上限按 watcher-gateway 的 worker 数计；§9.14.3 的停用态解除改为重启 watcher-gateway | §9.2；§9.5；§9.7；§9.14.3 |
| H-13 | 自查 | §9.1 补充事实：部署脚本的角色与单元模式、`AppRole` 与回落 `all`、启动与健康检查对数据库的依赖、8184–8189 在仓库内的占用、信号链路经 operator-query、写接口的权限、面板前端自带登录 token | §9.1 |
| H-14 | 自查 | WGW-1.0.3 草案的条款标注作废，保留历史（本节各表与提交 `db79023`）；R22 封闭白名单撤销；(e) 由"O-0 之后的加固选项"改为已采纳；提示计划 §2.1 需要由 Planner 修订 | §9 顶部；§9.15；§9.16 |

**后端执行者清单（WGW-1.0.4，第四轮修订后的最终版：沿用 B-1..B-5，新增 B-8..B-14）**

文件范围：`services/control-plane/api/app_roles.py`、`read_api.py`、`watcher_gateway.py`，`services/control-plane/db/pools.py`（仅在需要时），`scripts/contracts/`，`contracts/generated/caddy-watcher-gateway.caddy`，`tests/control-plane/`。不改 `security/*`、YAML，也不改另三个角色的业务路由。

说明：这些改动是仓库代码的改动。按 R23，本阶段只把它们部署到 watcher-gateway 的独立目录，共享目录要等以后的控制面整体升级才更新。所以 B-10 (c) 锁定另三个角色的路由集合，是为那次升级准备的回归保护。

1. **B-1..B-5**：按 WGW-1.0.3 的条目实现，B-5 的用例改在 **watcher-gateway 角色 app** 上构造。spy 仍然打在 `watcher_gateway.resolve_principal` 上，并带正向对照（计数恰为 1）。第四轮修订了两条期望值：
   - signal token 在 watcher-gateway 上得到 403 `invalid_token`（env 中不放 signal token）；
   - JWT 形状的会话 token 在没有 `AUTH_SECRET_KEY` 时得到 401 `unauthenticated`。
2. **B-8 角色**（`app_roles.py`）：
   - 新增 `AppRole.WATCHER_GATEWAY = "watcher-gateway"`。`resolve_app_role` 接受 `watcher-gateway` 与 `watcher_gateway` 两种写法。
   - 不把新角色加进 `_DATABASE_ROLE_NAMES` 与 `_ROLLBACK_ONLY_PERMISSION_PROBES`。
   - 网关路由名**取自生成物 payload**：`identity == gateway` 且 `phase ≤ phase_max` 的行，按 `watcher_gateway__<id>` 规则命名。它们不能从共享 app 推导。
   - `route_names_for_role(OPERATOR_QUERY)` 显式排除 `watcher_gateway__*`，作为双保险。
3. **B-9 挂载**（`read_api.py`）：
   - 删除模块级的 `register_routes` 与 `install_middleware`（`:10673-10674`），以及对 operator-query 的 `install_middleware`（`:10690-10691`）。
   - `create_app("watcher-gateway")` 另建一个 FastAPI 实例：
     - 设 `docs_url=None`、`redoc_url=None`、`openapi_url=None`；
     - 带 `bind_request_role` 中间件，与 `build_role_app` 相同；
     - 只放健康检查路由和网关路由；
     - 安装前缀中间件与 §9.5 错误处理器。
   - 不挂 DB 启动校验、快照钩子、DB 重试处理器。
4. **B-10 隔离测试**（`.venv-arch`，不 import yaml）：
   - (a) `create_app(r)`，`r ∈ {operator-query, node-control, event-ingest, all}`：
     - 没有 `watcher_gateway__*` 路由；
     - 没有以 `/v1/watcher` 开头的路径（不区分大小写）；
     - 没有 `GatewayPathMiddleware`；
     - operator-query 与 all 对 `GET /v1/watcher/status`、`/v1/watcher/status/`、`/V1/WATCHER/status` 返回 404 `{"detail":"Not Found"}`，上游计数为 0；
     - `/v1/accounts/` 的尾斜杠行为与改动前逐字节相同。
   - (b) `create_app("watcher-gateway")`：
     - §9.14.4 第 2 项的集合相等与 endpoint 断言；
     - 路由名集合恰为"网关路由名 + `role_database_health`"；
     - 没有 `/openapi.json`、`/docs`、`/redoc`；
     - 装有前缀中间件；
     - `/v1/accounts` 与 `/v1/operator/orders` 返回 404；
     - 不设 `CONTROL_PLANE_APP_ROLE` 时，直接调用 `create_app("watcher-gateway")` 得到的 app，其 `/health/role` 仍按 watcher-gateway 分支应答（验证 `bind_request_role` 生效）。
   - (c) 用改动前提交生成的路由名单作为独立预言，锁定另三个角色的非网关路由名集合。
5. **B-11 不连库、不越界读文件**：
   - 在 `env -i` 下只放白名单 env（四个 reader token、网关 token、`WATCHER_GATEWAY_URL`），设 `CONTROL_PLANE_APP_ROLE=watcher-gateway`，import `read_api`，走完 startup，再发一组请求：
     - 经上游桩得到 200；
     - 不带凭据得到 401；
     - 随机 token 得到 403 `invalid_token`；
     - 夹具生成的 signal 形状 token（不在 env 中）得到 403 `invalid_token`；
     - 媒体 GET 与 HEAD；
     - 健康检查。
   - 断言对 psycopg `connect`、`checkout_role_connection`、`_database_connection` 与连接池的 spy 计数都为 0；在该角色下主动调用这些函数必须抛错。
   - 再用 `sys.addaudithook` 记录 `open` 与 `import` 事件，断言所有被打开的非标准库文件都位于"测试构造的代码根"或 venv 之下。这证明在独立目录中运行时，不会读到共享目录。
6. **B-12 健康检查**：按 §9.14.6，200 表示 `gateway: enabled`，503 表示 `gateway: disabled`，不连库，也不给出原因细节。覆盖的情形：缺 token、token 撞值，以及停用态的四种注入。另三个角色的健康检查不变。
7. **B-13 不改**：`security/*`；node-control 与 event-ingest 的一切；operator-query 除去掉网关以外的一切；YAML 与两份代码生成物；watcher 代码。不加任何按角色或来访头区分入口的逻辑（§9.3）。
8. **B-14 验证命令与判据**（全部带 `set -o pipefail`，不连 jp-24）：
   - `python3 scripts/contracts/check_watcher_gateway_routes.py` 输出 `ROUTES_DIFF_EMPTY`。
   - 全量测试：`LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv-arch/bin/python -m pytest tests/control-plane -q -p no:cacheprovider -rfE`。本机需要 PostgreSQL 16 的二进制（例如 Homebrew `postgresql@16`，`initdb` 在 PATH 上）。不设 UTF-8 locale 时，`initdb` 会失败，出现几百个 error，这类结果**不作数**。
   - **判据不是"全绿"**：在改动前的提交（集成基线）与改动后的提交上各跑一次同一条命令。要求 failed 与 error 的集合逐项相同，passed 数不减少，新增的测试全部通过。
   - 已知的基线失败共 5 个，全部是既有问题：`trader_control_plane_migrate` 没有 `verify_frozen_maintenance_fence`。它们是 `tests/control-plane/db/test_maintenance_fence.py` 中的：
     - `test_frozen_stage_verification_accepts_stale_heartbeats`
     - `test_frozen_stage_verification_rejects_evidence_hash_drift`
     - `test_frozen_stage_verification_rejects_owner_token_drift`
     - `test_frozen_stage_verification_rejects_expired_lease`
     - `test_frozen_stage_verification_rejects_redis_epoch_drift`
     
     审查者在集成基线上的结果是 880 passed、5 failed。
   - watcher 的 node 测试照跑，结果与改动前相同。

**Release Steward 清单（WGW-1.0.4，第四轮修订后的最终版：沿用 RS-1，新增 RS-11..RS-20）**

文件范围：`scripts/ops/o0/`，`scripts/jp24-p1-control-plane.sh`（只改数组、模板、健康检查期望与头注释），`docs/agent-team/release/`。与 wac-097 的 🟡-2、🟡-3 同批。

1. **RS-1**：片段 v2 生成物核对，按 WGW-1.0.3 的条目。
2. **RS-11 verify 上游**：
   - `DEFAULT_GATEWAY_UPSTREAM`（`o0_caddy_watcher_routes.py:136`）改为 `127.0.0.1:8186`。
   - `(watcher_gateway_upstream)` 必须恰好一次 `strip_path_prefix /m`，只拨 `127.0.0.1:8186`，没有 `rewrite`，没有 `transport` 覆盖。
   - 从 server 根到片段 `reverse_proxy` 的整条处理链上（含站点顶层的 `request_header`），不得有任何请求头操作。
   - 自测增加两个变异：片段上游改成 8183 → 失败；片段上游加 `transport http { … }` → 失败。
3. **RS-12 V-1 主判据 I-2**：递归遍历整份 adapt JSON，排除 RS-11 认定的那个片段上游对象，按 §9.14.6 I-2 的 (1)–(5) 判 `GATEWAY_PORT_EXPOSED`：
   - 独立的 `8186` 字符串，允许前导零；
   - 数值 8186；
   - 端口范围含 8186；
   - 端口或整个 dial 含 `{env.*}`、`{system.*}` 以外的占位符；
   - 代理 URL 解析出 host:port 后按 dial 规则判定。
   
   排除的片段上游是**每一个**片段逐路径组中的 `reverse_proxy`。解析不出、又只含 `{env.*}`、`{system.*}` 的，只输出 `UPSTREAM_UNRESOLVED` 信息行。对 8183 的转发**不作任何导致失败的形状判定**，按 §9.14.6 V-1 (c) 最多给 `HINT DEFENSE_IN_DEPTH`（G-44、G-46、G-50）。V-4 的文本规则同步允许前导零。
4. **RS-13 V-2、V-3**：
   - 在 app 站点模拟四个 `/v1/watcher` 请求，期望守卫返回 404。
   - 探针用 8183 与 8186 两个独立的桩：表内路径只命中 8186 桩，`Authorization` 保持原值；其余所有探针都不命中 8186 桩。
   - WGW-1.0.3 的逐转发器标记探针如已实现，改为不阻断的信息输出。
5. **RS-14 V-4**：`caddyfile_check`（`:1559` 起）增加一条：文本中独立的 `8186` 只能出现在 `(watcher_gateway_upstream)` 内。V-4 的任何失败都使 verify 失败。
6. **RS-15 自测与真实 Caddy 测试**：
   - **PASS（证明不误拦）**：
     - 仿生产夹具，片段上游为 8186；
     - 以下各形状拨 8183：面板 `handle /v1/*` 注入 observer、q1–q4、`handle_errors` 转发、`forward_auth`、`reverse_proxy { rewrite /v1/watcher/dialogs }`、`handle_path … invoke`、`handle_response` 内转发、站点顶层 `reverse_proxy /v1/*`、`@m path /m/v1/*`、`handle_path /p/*`；
     - `reverse_proxy unix//run/app.sock`；
     - `reverse_proxy {env.OQ_HOST}:8183`；
     - `dynamic a { name localhost; port 8183 }`（后两种按"端口是字面值"判定）。
   - **FAIL（`GATEWAY_PORT_EXPOSED`）**：
     - 上面各形状改拨 8186；
     - `localhost:8186`、`[::1]:8186`；
     - 端口范围 `8180-8189`；
     - 面板拨 8183 但带 `transport http { network_proxy url http://127.0.0.1:8186 }`，或 `forward_proxy_url http://127.0.0.1:8186`；
     - `dynamic a { name localhost; port 8186 }`；
     - 主动健康检查的 `upstream` 指向 `127.0.0.1:8186`；
     - 合成 JSON 中非 http app 的 dial 为 8186。
   - （G-46、G-50）**FAIL** 追加：
     - `reverse_proxy 127.0.0.1:08186`；
     - `transport http { network_proxy url http://127.0.0.1:08186 }`，要求 V-4 也失败；
     - `127.0.0.1:{http.request.header.X-Port}`、`reverse_proxy {http.request.header.X-Up}`、`127.0.0.1:{vars.p}`；
     - `map` 的输出用作端口；
     - `network_proxy url http://127.0.0.1:{http.request.header.X-Port}`。
     
     其中前导零 `network_proxy` 与 `X-Port` 两例，另做活体证明：8186 桩确实被命中。
   - **信息（`UPSTREAM_UNRESOLVED`，不失败）**：`reverse_proxy {env.OQ}`（端口也是占位符）、`network_proxy url {env.PROXY}`。
   - （G-44）**提示（`HINT DEFENSE_IN_DEPTH`，不失败）**：
     - 站点顶层普通 `forward_auth 127.0.0.1:8183 { uri /v1/auth }`；
     - 片段之后的 SPA `try_files`；
     - 片段之前改路径进 `/v1/watcher` 空间的规则；
     - 把前缀转发给 8183 的旧形状。
     
     对照组：同一个 `forward_auth` 加 `copy_headers Authorization` → **FAIL**（请求头操作）；上述任一形状改拨 8186 → **FAIL**。仿生产夹具仍为 0 失败、0 提示。
   - **FAIL（结构）**：片段上游拨 8183；片段上游带 `header_up Authorization …`；站点顶层 `request_header Authorization …`；删去守卫组。
   - **活体**：每个 PASS 用例都证明 8186 桩只被表内路径命中；每个拨 8186（含 `network_proxy`）的 FAIL 用例都证明 8186 桩被非表内请求命中。
7. **RS-16 部署脚本（阶段 O，按 §9.14.6"部署与重启"与 R23 重写）**：新增 `o0_deploy_watcher_gateway.sh`，分 `preflight`、`apply`、`verify`、`rollback` 四个阶段。**任何阶段都不写共享目录、不写共享 venv、不重启另三个单元。**
   - **preflight**（只读，只写 staging）：
     - 共享目录逐文件核对 sha256 是否等于 `67b401a`：一致时输出 `MANIFEST_OK cp-shared-vs-67b401a`。不一致时输出 `MANIFEST_DRIFT` 并停工，漂移清单每行带当前 sha256，`drift_sha256` 同时绑定路径与内容；用户按 O0-A08D 选择 `--accept-shared-drift <drift_sha256>` 或先对齐。之后记录共享目录的全量快照（sha256、mode、uid、gid），作为后续的硬门禁（G-47）；
     - 全部单元的 `NeedDaemonReload` 都必须为 `no`，否则输出 `DAEMON_RELOAD_PENDING units=<列表>` 并停工，交 O0-A08R；
     - 8186 没有监听，新单元、系统用户与新目录都不存在（或与 bundle 一致）；
     - 记录另三个单元的 MainPID 与启动时间；
     - 依赖检查：就是 §9.14.6 G-48 规定的 `python -B` import 冒烟（新单元的 OS 用户、`env -i` 白名单、staging 中的新目录），缺模块时输出 `DEPENDENCY_MISSING <模块名>`；事后共享 venv 与新目录的元数据不变；
     - 在 staging 中展开 `git archive <release-sha>`，按清单核对 sha；
     - 做 import 冒烟：新目录、白名单 env、审计钩子，断言没有从新目录与 venv 以外加载任何东西；网关路由数大于 0；
     - 按白名单拼出变量名清单（只打印名字），并做 reader token 互异检查；
     - 写门禁。
   - **apply**：
     1. 核对门禁；
     2. 再核对一次 `NeedDaemonReload`；
     3. 创建系统用户；
     4. 把新目录从 staging 以 root 0755 安装到 `$TRADER_ROOT/releases/watcher-gateway/<release-sha>/`，并做 sha 校验；
     5. 生成 0600 的 env；
     6. 安装单元；
     7. `NeedDaemonReload` 核对（排除新单元）；
     8. `systemctl enable --no-reload` 新单元；
     9. 紧接着唯一一次 `daemon-reload`；
     10. 再核对 `NeedDaemonReload`，全部为 `no`；
     11. 核对另三个单元的 MainPID 与启动时间不变；
     12. `systemctl start` 新单元；
     13. 30 秒内 `/health/role` 返回 `gateway: enabled`；
     14. 回环探针：在 8186 上，用 `SYSTEM_OBSERVER_TOKEN` 请求 `/v1/watcher/status` 得到 200（token 在进程内读取，只打印状态码），不带 token 得到 401；在 8183 上，`/v1/watcher/status` 得到 404；另做 Caddy 回环 `--resolve` 的 V-5 补充检查；
     15. 再核对共享目录的全量快照，必须与 preflight 逐项相同；
     16. 舰队守卫。
     
     第 7–12 步的顺序按 G-49：systemd ≥ 255 的 `unit_file_state_outdated` 语义，且全程不用 `--now`。
     
     失败时自动回滚，回滚只针对新增的东西。
   - **verify**（O-3）：V-5 外部检查的证据是必需输入，没有就输出 `DIRECT_GUARD_UNVERIFIED`；另三个单元的 MainPID 不变；共享目录的 sha 不变；舰队守卫。
   - **rollback**（G-49）：
     1. 记录一次全量 `NeedDaemonReload` 快照（不作门槛）；
     2. 无条件 `stop`、`disable --no-reload`，断言新单元不处于 active 或 activating，删除 env 与新目录；
     3. reload 门按第 1 步快照（排除新单元）判定：干净则删除单元文件与系统用户、`daemon-reload`，再核对全部为 `no`；不干净则输出 `DAEMON_RELOAD_PENDING`，保留已停止、已禁用的单元文件，交 O0-A08R；
     4. 最后核对另三个单元的 MainPID 与共享目录的全量快照都不变。
   - **测试桩**：`apply_rollback_test.sh` 的桩要模拟"manager 已过期"：任何带改动的 `enable`/`disable --no-reload` 之后，所有单元都报 `yes`，直到 `daemon-reload` 为止。apply 与回滚按这个语义重跑，并补一个用例："apply 成功后，verify 的隔离检查通过"。
8. **RS-17 隔离门禁**（`o0_tool.py cp-isolation`，S-10）扩展到四个单元：
   - watcher-gateway 的 env 变量名只能是白名单的子集：默认不含 signal token，也不含 `AUTH_SECRET_KEY`，除非 U-13 (iii) 批准；
   - 另三个单元的 env 与 `Environment=` 中没有 `WATCHER_GATEWAY_TOKEN`；
   - 每个 `trader-v3-controlplane-*` 单元都显式设置了非 `all` 的 `CONTROL_PLANE_APP_ROLE`；
   - 只有新单元的角色是 watcher-gateway，它监听 `127.0.0.1:8186`，`WorkingDirectory` 位于 `$TRADER_ROOT/releases/watcher-gateway/` 之下；
   - 另三个单元的 `WorkingDirectory` 仍是共享目录；
   - 新单元不设 `CONTROL_PLANE_EXPECT_DATABASE_ROLE`。
9. **RS-18 `scripts/jp24-p1-control-plane.sh`**：只为以后的控制面整体升级补上新角色（数组、无库模板、健康检查期望），并在头注释中写明：它的 `apply` 会覆盖共享目录并重启全部角色，**O-0 本阶段禁止使用**。
10. **RS-19 V-5 外部检查工具**：期望值按 §9.14.6 V-5 的封闭集合判定：面板入口必须恰为 404；`/m/v1/watcher/status` 必须是 401 或 403（单元启动之前则必须是 502）；`/m/v1/accounts` 必须恰为 401；其他任何状态都算失败。执行点与 `DIRECT_GUARD_UNVERIFIED` 的规则同 §9.14.6。
11. **RS-20 文档与打包**：
    - runbook 的阶段 O 按 RS-16 重写，删除"只重启 operator-query"与"安装五个文件到共享目录"；
    - 授权清单中 O0-A08 的范围、风险与回滚按 §9.14.6 更新；PC-6、U-13 按 WGW-1.0.4 版；
    - `o0-requirements.md` 新增一行："网关独立端口、独立目录与 I-2"；
    - `o0-site-checklist.md` 在 S-06 中记录 8184–8189 的占用，上线后复核 8186 只监听回环；
    - 打包门禁（G12 runtime manifest）加入独立目录的清单、单元模板与 env 白名单。

**WGW-1.0.4 第四轮复审修订（2026-09-29，版本号不变）**

来源：审查报告 `docs/agent-team/reviews/wac-096.md`"第四轮（WGW-1.0.4）"（FAIL：1 🔴、6 🟡、8 🟢），以及 Planner 裁定 R23。

范围：只改 §9 文字。§1–§8、YAML、四份生成物都不变。第 6 行 preamble 仍写 WGW-1.0.2，按 Planner 指示保留到合并时处理。上面的 B、RS 清单已直接改为最终版，首版的 B-8..B-14、RS-11..RS-20 以本版为准。

| # | 来源 | 改动 | 位置 |
|---|---|---|---|
| G-30 | 🔴-1；R23 | watcher-gateway 使用独立代码目录 `$TRADER_ROOT/releases/watcher-gateway/<sha>/`，路径子集同 p1 脚本并保持仓库布局，root 0755 只读；共享 venv 只读共用，缺依赖即停工；本阶段对共享目录与共享 venv 一个字节都不动 | §9.14.6 新角色 |
| G-31 | 🔴-1 | 删除"磁盘新代码对 operator-query 只少了网关路由"等不实表述；写明 `67b401a` 与集成分支的真实差异（C-0 快照接入 `operator_order` 等四项，新增 `watcher_config_snapshot.py`），以及 `Restart=on-failure` 会让被替换的代码在无人值守时生效；这些属于以后单独的控制面升级；本阶段没有 D-04 | §9.14.6 部署与重启；§9 顶部；§9.15 |
| G-32 | 🔴-1；R23 | 部署前只读核对"共享目录逐文件 sha 与 `67b401a` 一致"，作为证据（apply 后与回滚后再核对一次，必须逐字相同）；回滚只删新单元、新目录、env 与用户 | §9.14.6；RS-16 |
| G-33 | 🟡-1 | I-2 补漏：独立的 `8186` 字符串（覆盖 `network_proxy`、`forward_proxy_url`、健康检查 `upstream`、非 http app）、数值 8186、端口范围含 8186 都判失败；片段上游不得带 `transport` 覆盖；V-4 失败即阻断 | §9.14.6 I-2、V-1、V-4；RS-11、RS-12、RS-14、RS-15 |
| G-34 | 🟡-2 | I-2 放宽：unix socket、占位符主机名加非 8186 的字面端口、`dynamic` 加非 8186 的字面端口不判失败；解析不出且不含 8186 的只输出 `UPSTREAM_UNRESOLVED` 信息行，由 PC-6 (i) 人工确认 | §9.14.6 I-2、PC-6；RS-12、RS-15 |
| G-35 | 🟡-3 | `daemon-reload` 之前（包括回滚时），任一单元 `NeedDaemonReload=yes` 即停工（`DAEMON_RELOAD_PENDING`），维持 wac-060 的规则 | §9.14.6；RS-16 |
| G-36 | 🟡-4 | env 白名单去掉 signal token；`AUTH_SECRET_KEY` 默认不持有（PC-6 (v) 与 U-13 (iii) 两个条件都满足才写入）；写明 env 值的来源；写明对错误码的影响：signal token 得 403 `invalid_token`，会话 token 得 401 `unauthenticated`，app 只用静态 token，不受影响 | §9.14.6 凭据；§9.3；B-1、B-11；RS-17 |
| G-37 | 🟡-5 | B-14 写明 UTF-8 locale、PostgreSQL 16 依赖、"与改动前基线的 failed 与 error 集合逐项相同"的判据，并列出 5 个已知失败 | §9.16 B-14 |
| G-38 | 🟡-6 | §9.15 修复方向 1 补充：settings 路由只认静态 token；operator-query 可能要补 `AUTH_SECRET_KEY` 并重启，这是交易影响窗口；`VITE_AUTH_DISABLED` 构建不带 token；已登录的 reviewer 审批也会生效；登录服务的默认角色是 `risk_admin`；明确这是独立任务，需要用户单独授权 | §9.15 |
| G-39 | 🟢-1、🟢-2、🟢-3 | 采纳：网关路由名取自生成物 payload；watcher-gateway app 带 `bind_request_role`，并关闭 docs、redoc、openapi | §9.14.6；B-8、B-9、B-10 |
| G-40 | 🟢-4 | 采纳：V-5 期望改为封闭集合（面板入口恰为 404；`/m/v1/watcher/status` 为 401 或 403，单元启动前为 502；`/m/v1/accounts` 恰为 401），其他任何状态都算失败 | §9.14.6 V-5；RS-19 |
| G-41 | 🟢-5 | 采纳：请求头操作的检查范围扩到"从 server 根到片段 `reverse_proxy` 的整条处理链，含站点顶层 `request_header`" | §9.14.6 V-1 (a)；RS-11 |
| G-42 | 🟢-6、🟢-7 | 🟢-6：§9.15 标明匿名请求结果来自 Planner 的转述，请 Planner 把原始输出（状态码、字节数，不含 body）附进任务记录；🟢-7：逐转发器标记探针保留为不阻断的信息输出 | §9.15；§9.14.6 V-3；RS-13 |
| G-43 | 🟢-8 | 第 6 行 preamble 按 Planner 指示，由 Planner 在合并时更新（§1–§8 之外、§9 之前的文字不在本勘误的改动范围内） | — |

**WGW-1.0.4 合并后勘误一（2026-09-30，任务 wac-110；版本号不变）**

来源：
- 复审报告 `docs/agent-team/reviews/wac-105.md`（wac-108，PASS）的 §4：执行者提出的契约冲突 (1)、(2)，以及建议收录的 E-2..E-4；
- `docs/agent-team/reviews/wac-096.md` 第五轮的 🟡-A、🟡-B、🟡-C、🟢-1..🟢-4；其中 🟢-3 即共享目录漂移由用户决定。

这些内容执行者（wac-104、wac-105、wac-109）都已经按任务书实现了，本次只是把契约文字补齐。

版本号仍为 **WGW-1.0.4**，不另起 1.0.4a。原因是"1.0.4a"不符合 S-01 规定的 `WGW-<主>.<次>[.<勘误>]` 格式，而且本次改动不涉及 YAML、生成物，也不改任何接口。

改动范围：只改 §9 的文字。§1–§8、YAML（`yaml_sha256` 不变）与四份生成物都不变；既有 `/v1` 端点、网关代码、角色 scope 都不变。文件第 6 行 preamble 在 §9 之外，由 Planner 在合并时决定是否提及本次勘误。

| # | 来源 | 改动 | 位置 |
|---|---|---|---|
| G-44 | wac-108 §4 (1)；用户要求"不要限制得那么死" | 消除 V-1 (c)"遮蔽检查不变"与 Caddy 条款"对 8183 不作形状判定"之间的矛盾。<br>仍判失败：片段之前的请求头操作（含带 `copy_headers` 的 `forward_auth`）、改路径进 `/m/v1/watcher` 空间、把前缀转发到 9090/9100/8186，以及一切与 8186 有关的形状。<br>降为 `HINT DEFENSE_IN_DEPTH`：片段之前的普通 `forward_auth`、只可能改进 `/v1/watcher` 空间的规则、兜底与守卫之后无法求值的形状（SPA `try_files` 等）、指向 8183 的旧形状。<br>V-1 (d) 改为"不作导致失败的形状判定，最多给提示"。 | §9.14.6 Caddy 条款、V-1 (c)(d)；RS-12、RS-15 |
| G-45 | wac-108 §4 (2) | 凭据文件名澄清：线上文件仍是 `$TRADER_ROOT/secrets/control-plane/watcher-gateway.env`，没有改名；凭据集内部的持有方文件 `controlplane-watcher-gateway.env` 只是为了和 watcher 容器的 `/srv/trader-secrets/watcher-gateway.env` 区分，不属于契约的规定范围 | §9.14.6 凭据"值的来源" |
| G-46 | wac-096 第五轮 🟡-A（E-2） | I-2 新增 (4)：dial 的端口部分或整个 dial 含 `{env.*}`、`{system.*}` 以外的占位符（请求作用域、`map` 输出等）即判失败，`dynamic` 的 `port` 同样处理；`UPSTREAM_UNRESOLVED` 只留给 `{env.*}`、`{system.*}` | §9.14.6 I-2；RS-12、RS-15 |
| G-47 | wac-096 第五轮 🟢-3；wac-108 🟡-6（E-2） | 共享目录漂移：输出 `MANIFEST_DRIFT` 并停工，不修复任何文件；清单带每个文件的当前 sha256，`drift_sha256` 同时绑定路径与内容；由用户按 O0-A08D 决定用 `--accept-shared-drift <drift_sha256>` 重跑，还是先对齐；前后全量快照逐项相同仍是硬门禁。修订 G-32 的"不一致即停工" | §9.14.6 部署前只读核对、PC-6 (vi)；RS-16 |
| G-48 | wac-096 第五轮 🟡-C（E-2） | 依赖检查就是 `python -B` import 冒烟：以新单元用户身份，`env -i` 白名单，staging 新目录，`create_app("watcher-gateway")` 的网关路由数大于 0；缺模块输出 `DEPENDENCY_MISSING`；事后共享 venv 不变；不得用 `pip check` 之类的做法 | §9.14.6 部署前只读核对；RS-16 |
| G-49 | wac-108 🟡-1（E-3）；wac-096 第五轮 🟡-B、🟢-4 | 适配 systemd ≥ 255 的 `unit_file_state_outdated` 语义：<br>apply 顺序改为：核对（排除新单元）→ `enable --no-reload` → 紧接着唯一一次 `daemon-reload` → 再核对全部为 `no` → 另三个单元 PID 不变 → `start`，全程不用 `--now`。<br>回滚改为：记录快照（不作门槛）→ 无条件 stop、`disable --no-reload`、删 env 与目录，先撤暴露 → reload 门看 disable 之前的快照。<br>`DAEMON_RELOAD_PENDING` 列出单元交给 O0-A08R；PC-6 (vii) 记录 `systemctl --version`；RS-16 的测试桩模拟 manager 过期标志 | §9.14.6 新单元步骤、回滚、PC-6；RS-16 |
| G-50 | wac-108 🟡-5（E-4） | I-2 (1) 的正则允许前导零：`(?<![0-9])0*8186(?![0-9])`；新增 (5)：代理 URL 先解析出 host:port，再按 dial 规则判定；V-4 同步修改 | §9.14.6 I-2；RS-12、RS-15 |
| G-51 | wac-096 第五轮 🟢-1、🟢-2 | 独立目录内容的措辞改为"在 p1 子集基础上取整个 `packages`"；I-2 排除的对象改为"每一个片段逐路径组中的 `reverse_proxy`"（复数） | §9.14.6 独立代码目录、I-2；RS-12 |

没有收录的提示：wac-108 的 💭-4（新单元加 systemd 加固项）留给下一版契约考虑，本次不改单元 lint 的固定项；其余 💭 属于执行侧，由 wac-109 处理。
