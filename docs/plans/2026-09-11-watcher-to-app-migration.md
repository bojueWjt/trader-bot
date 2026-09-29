# telegram-watcher 站点功能迁入 balen-bot app 设计 v0.6（watcher-to-app-migration）

> 状态：**v0.5。D1–D9 全部已由用户拍板（D1–D6 于 2026-09-24，D7–D9 于 2026-09-25 按推荐通过，见 §0.1）；"app 统一密钥无感"改写了 D2 的鉴权形态（§2.1）；平板验收基准为 Xiaomi Pad 9 Pro Max（13.3 英寸 3408×2272，§7A.2）**。v0.1 已吸收首轮 Codex review（2 P0 / 11 P1 / 2 P2）；v0.4 已吸收 v0.3 定向 review（`2026-09-11-watcher-to-app-migration.review-v0.3.md`：0 新 P0 / 8 P1 / 1 P2，D2 形态同意）。§7A 平板适配未经 Codex review。
> 事实来源：`bridge/services/telegram-watcher/`（server.js、lib/trading-api.js、price-monitor.js、public/index.html、`__tests__/` 62 用例全绿）、jp-24 现场（Caddy、systemd、docker 卷）、`alert-personal/apps/attention-android`（固定基线改为 `.worktrees/close-visible-result-20260918` 分支 `codex/close-visible-result-20260918` HEAD `0f7d26d`，它包含 `fd9f50e`；`main` 停在 `2c28085` 未合入，开工前先定合入策略）。
> v0 → v0.1 变更：见附录 B（逐条对应 review 编号）。

## 0. 决策清单（待拍板；每条给推荐与 review 裁决）

| # | 决策 | 推荐 | review 裁决 | 要点 |
|---|---|---|---|---|
| D1 | 配置（账号/路由/风险）的**唯一真相与唯一写入者** | 仍是 watcher 服务；app 与控制面都是它的 HTTP 客户端 | 同意 | "唯一写入者"是要落实的边界：feeder 只读；遗留 `skills/crypto-trader/scripts/db_manager.py` 的写命令停用或改走同一服务；break-glass 写入也须走审计与 revision（P2-02） |
| D2 | app 调 watcher 的鉴权 | **v0.3 改写**：app 不持有 watcher 凭据，一律经控制面 operator-query 的 `/m/v1/watcher/*` 白名单网关访问，复用 app 已有的控制面 token；watcher 只认服务端身份（`gateway`、`snapshot`、浏览器代理凭证），每个请求无条件验 token；不再开 Caddy `/w` 公网入口 | 修改 | token 未配置 → 对应入口启动失败；三种服务端身份分离：`gateway`（网关读写）、`snapshot`（控制面只读）、浏览器代理凭证；轮换=双值 + 撤销，不是时钟过期（P0-02） |
| D3 | 消灭控制面副本 | watcher 提供只读 `config-snapshot`，控制面进程内缓存 + **有限授权有效期**，超期 fail-closed；按"新增并验证 → 影子三路对照 → 切换 → 移除副本"四步走 | 修改 | 缓存也是副本，只是受控读模型；卷直读保留为备选（需比较持久挂载/组权限维护成本，未验证不排除）（P0-01、P1-02） |
| D4 | Telegram 登录/改绑/2FA/api 凭据 | 不进 app，留在站点；app 只显示状态、断开/重连 | 同意 | — |
| D5 | 站点去留 | 保留为兜底与凭据入口；两端共享新鉴权、审计、版本校验 | 同意 | 保留站点不等于恢复旧副本 |
| D6 | app 信息架构 | 新增第四个底部 Tab「信号」；配置进「设置 → 交易配置」；价格提醒从仓位创建、在信号页管理、已投递事件看告警页 | 同意 | 最终导航在固定 app 基线上验收 |
| **D7（新）** | **Binance 账号密钥的边界** | **账号创建与密钥轮换留在站点**；app 只编辑非秘密字段（启用、层级、执行账号、风险、addon）；服务端拒绝掩码占位值回写；snapshot/审计/错误响应/客户端日志全部不含 key/secret/session | 新增（P1-08） | 若坚持 app 全量 CRUD：一次性录入、不持久化不回显、提交即清空、单独权限——本文不推荐 |
| **D8（新）** | **价格告警的交付范围** | **本轮只做"记录并展示触发状态"**：app 创建/查看/删除提醒，触发后显示 `triggered_at`；**不承诺推送**。推送链路（事件发布 → attention/Telegram → 投递确认/重试/去重）另立任务 | 新增（P1-09） | 现状 `price-monitor.js` 触发仅 `UPDATE triggered` + `console.log`，无任何投递；Telegram 发送函数只服务 watcher 健康告警 |
| **D9（v0.2 新）** | **平板适配的范围** | **只做 Android 平板**（交付物本来只有 Android）；按窗口宽度三档（紧凑 <600dp / 中等 600–839dp / 宽屏 ≥840dp）自适应，不单独出平板包；横竖屏、分屏都支持；宽屏下交易与信号用"左列表右详情"双栏（R25，2026-09-30：信号页是否双栏按内容区实际宽度判定——列表 ≥400dp 且详情 ≥440dp；目标平板竖屏 909dp 扣侧栏后约 685dp，单栏为预期）。手机体验保持不变 | 未 review | 已定：验收基准 Xiaomi Pad 9 Pro Max；iPad 不在本轮 |

### 0.1 用户裁决（2026-09-24 / 2026-09-25）

| # | 裁决 | 落地 |
|---|---|---|
| D1 | 同意 | watcher 为配置唯一写入者 |
| D2 | 同意，并按 D3 行的要求改形态 | §2.1 |
| D3 | 同意；同时提出"**app 要统一密钥无感**" | 解读为：app 不新增任何密钥输入，watcher 能力跟随现有控制面凭据自动可用，失效时只有一处"交易服务连接"要处理。因此取消 mobile token 与 `/w` 入口，改为控制面网关（§2.1、§3）。本轮范围只覆盖 watcher；告警服务（attention，部署在 HK）的配对凭据仍独立，是否并入另议 |
| D4 | 不需要（Telegram 登录不进 app） | 保持 |
| D5 | 保留站点 | 保持 |
| D6 | 同意 | 第四个 Tab「信号」 |
| D7 | 按推荐（2026-09-25） | 账号创建与 Binance 密钥轮换留在站点；app 只改非秘密字段 |
| D8 | 按推荐（2026-09-25） | 价格提醒本轮只记录并展示触发状态，不推送；推送另立 N-1 |
| D9 | 按推荐（2026-09-25） | 只做 Android 平板，按窗口宽度三档自适应；真机验收基准为 **Xiaomi Pad 9 Pro Max**；iPad 与独立平板包不做 |

## 1. 现状事实（附证据）

### 1.1 站点能力清单（`bridge/services/telegram-watcher`）

| 面板 | API | 数据 |
|---|---|---|
| Telegram 设置：api id/hash、验证码/扫码登录、2FA、改绑、断开/重连 | `/api/config` `/api/login/*` `/api/login/qr/*` `/api/disconnect` `/api/reconnect` `/api/status` | 会话串在 watcher 配置文件 |
| Watcher：群组列表加载/保存选择 | `/api/dialogs`（仅取前 100 个）`/api/groups`（写 JSON 文件；已选列表从 `/api/status.watchGroups` 读） | 配置文件 |
| 消息流 | 站点读内存 ring `/api/messages`；DB 查询 `/api/trading/messages`；`/media/*` 静态文件 | `telegram_messages`（`id` 本地自增，`msg_id` 才是 Telegram ID，时间为入库时间）、媒体目录（文件名=时间戳+消息 ID，不是凭证） |
| 交易账号：主/子账号 CRUD、计算加权额 | `/api/trading/accounts` GET/POST/PUT/DELETE（POST 必填 Binance key/secret；PUT 空凭据保留原值；GET 掩码） | `account_configs`（含密钥列；`account_id`、`execution_account_id` 唯一，允许多个 main） |
| 频道路由 | `/api/trading/channels` GET/POST(upsert)/DELETE | `channel_routing` |
| 风控配置：品种风险比例 | `/api/trading/risks` GET/POST(upsert)/DELETE（接受任意非负有限数） | `symbol_risk_configs` |
| （无 UI）订单/活跃单/简报 | `/api/trading/orders`、`/orders/active`、`/briefings` | `active_orders`（站点 INTEGER id，非 V3 身份）、`briefings`、`signal_operations`（`UNIQUE(signal_id, operation_type)`，按信号键控的执行账本，Python 端也在用） |
| （无 UI）价格告警 | `/api/price-alerts` GET/POST/DELETE、`/order/:orderId` DELETE、`/api/price-monitor/status` | `price_alerts`（`order_id INTEGER` 指站点订单，无账户/仓位/环境字段；价源取账号表首行） |

三张配置表**均无 `updated_at`**，写入不更新时间戳；PUT 无版本条件直接覆盖；DB 连接仅设 WAL/FK，无显式 `busy_timeout`；异常统一 500。

### 1.2 部署与鉴权事实（jp-24）

- 容器 `trader-watcher-1`：`node server.js`，容器内监听所有地址、宿主只映射 127.0.0.1:9090；静态 `/media` 在 API 之前注册且无鉴权；Caddy `@watcher` 匹配 `/watcher/*` 与全部站点 `/api/*` 路径，basicauth 单用户后反代。
- 真身 DB：docker 卷 `trader_signal-data` 内 `watcher-trading.db`（`root:root 0600`，299 KB，WAL）。
- 副本：`/srv/trader-v3/state/operator-query-risk/trading-risk.db`（`root:trader-v3-cp-operator-query 0640`，28 KB，mtime 09-06）；operator-query 经 `WATCHER_TRADING_DB`（另有两个别名，模块加载时解析）指向它。**无同步单元**，已漂移；文件大小差不能量化三表差异，切换前必须逐行比对。
- 读写者清单（review 补正）：`read_api.py` 读三表（风险资金 addon、渠道路由归因；仅 `open_position` 分支依赖）；`scripts/hermes_signal_feeder.py` **直读真库路由**（root 运行）；`db_manager.py` 可直写三表（运维脚本）。

### 1.3 app 现状（固定基线 `0f7d26d`）

- Tab：告警 / 交易 / 设置；Stack：Positions、PositionDetail 等。交易区已接控制面 `/m/v1/*`（RISK_ADMIN token）。设置页无 watcher 概念。
- 版本：RN 0.82.1、React 19.1.1、`@react-navigation/bottom-tabs` ^7.18（原生支持 `tabBarPosition: 'left'` 侧栏）、native-stack ^7.7、antd RN 5.4.3、react-native-svg 15。
- 响应式现状：**全局无任何断点逻辑**。`src/` 中 `useWindowDimensions` 仅 `BottomSheet`（取高度算 88% 上限）使用；`EquityWaveform` 用 `onLayout` 取宽，天然随宽度伸缩。`BottomSheet` 注释写明"全宽、贴底"；`PositionsScreen` 卡片/列表两种视图均单列；`AccountsScreen` 四张账户卡单列堆叠；仓位详情是 Stack push 全屏页。
- 方向与重建：`AndroidManifest.xml:24` 的 `configChanges` 已含 `orientation|screenLayout|screenSize|smallestScreenSize`，旋转与分屏不重建 Activity，由 JS 侧重排；未锁方向。`ios/` 目录存在但交付只走 Android。
- 近期缺陷：app 平仓缺 `channel` 归因已修（`tradingApi.ts:572`）；节点重启后拒绝 reduce_only 出场（`fix/node-exit-denied` 未部署）。Phase 2 的一致性验收在它们之后跑。

## 2. 目标架构

```
app ──/m/v1/*（RISK_ADMIN bearer）──▶ Caddy ──▶ operator-query（不变）
app ──/m/v1/watcher/*（同一个控制面 token，端点矩阵 §3）──▶ Caddy ──▶ operator-query（白名单网关）──gateway token──▶ watcher 127.0.0.1:9090
浏览器 ──/watcher + /api/*（basicauth，Caddy 注入代理凭证）──▶ watcher:9090
operator-query ──GET /api/trading/config-snapshot（snapshot token，内网）──▶ watcher   ← 取代副本文件
hermes-feeder ──直读真库（只读，不变）──▶ watcher-trading.db
```

### 2.1 鉴权与信任边界（D2 v0.4，关闭 P0-02，落实"统一密钥无感"）

- **app 只有一把钥匙。** app 继续只保存 `trading.config.v1` 里的控制面地址和 token，不新增存储键、不新增设置项。watcher 能力通过 operator-query 新增的 watcher 网关提供，token 轮换仍是今天的一处操作。
- **角色范围（P1-13）。** `require_reader` 只会返回四个角色：`system_observer`、`viewer`、`risk_admin`、`reviewer`。网关读端点四个角色都放行；写端点只放行 `risk_admin`，且只限 §3 已列的写端点。不新增角色、不新增 token。契约写明：持有同一 `risk_admin` token 的所有调用方一并获得 watcher 写权限。以后若要区分自然人，用服务端 principal 映射，不给 app 新密钥。
- **watcher 不暴露给公网 app。** 不开 Caddy `/w` 路由。watcher 只接三种服务端身份，每个请求都在所有 handler 与静态文件之前校验，否则 401：
  1. `gateway` token：仅 operator-query 网关持有，只能访问矩阵内 `gateway` 行。
  2. `snapshot` token：仅 operator-query 快照 reader 持有，只能访问 `config-snapshot`。
  3. 浏览器代理凭证：Caddy 在 basicauth 通过后注入；Caddy 对所有入口先清除外来 `X-Watcher-*` 头再注入。
  三个凭据由 O-0 在发行侧生成并校验两两互异；每个进程只持自己需要的那一个，watcher 环境里不放任何控制面 reader token。任一缺失则对应进程启动失败，但 operator-query 缺 `gateway` 时只禁用网关路由并告警，不得让交易端点启动失败（上线前配置校验锁定）。轮换用 `*_TOKEN` + `*_TOKEN_PREVIOUS` 双值，顺序为：watcher 先接受新旧两值，网关切到新值，确认请求与审计正常，再撤旧值。app 与用户不接触这些凭据。
- **路径约定（P1-18）。** app 的交易 `baseUrl` 已以 `/m` 结尾（如 `https://…/m`），请求一律拼 `baseUrl + /v1/watcher/...`，代码常量里不得再写 `/m`。Caddy 去掉 `/m` 后到 operator-query 的 `/v1/watcher/*`。现行 Caddy 的 matcher 形态、strip 次数、上游端口只有 08-31 的记录，必须由 O-0 现场核对（清单见 §9 O-0），并按路由真源逐路径追加，禁止 `/m/v1/*` 通配。
- **路由真源（P1-15）。** 矩阵以一份机器可读文件为唯一真源（建议 `contracts/watcher-gateway-routes.yaml`），网关路由、watcher 身份校验、Caddy 路径清单都由它生成；§3 的表只是阅读版，冲突以文件为准。每行字段：`method | outer_path | inner_path | identity(gateway/snapshot/browser) | roles | query 白名单 | body 允许/拒绝字段 | response 剔除字段`。`gateway` 行一律拒绝 `api_key`、`api_secret`、`session` 等秘密字段，GET 响应删除秘密列；watcher 第二层对 `gateway` 身份同样拒绝秘密字段，不只依赖网关过滤。`browser` 行保留站点改密钥能力、GET 掩码；`snapshot` 行只读白名单字段。
- **拒绝口径（P2-03）。** 未注册路径返回 404，已注册路径的未列方法返回 405，与 FastAPI 默认一致；HEAD 需要时显式注册，不依赖隐式行为。
- **头与 body 清洗。** 网关丢弃请求中的 `Authorization`、`Cookie`、全部 `X-Watcher-*`（含重复项）与 hop-by-hop 头，只转发真源里的 query 与 body 字段，响应只回传真源里的头。
- **actor 是网关背书（P1-14）。** 网关在 `require_reader` 成功后注入恰好一条 `X-Watcher-Actor: app:<role>` 与一条 token 指纹（对规范化后的 token 取 SHA-256 前 12 位）；应注入头缺失则网关不转发。watcher 仅在 `gateway` 身份下接受这两个头，缺或多于一条一律 400，不合并、不默认。指纹只是审计标签，不作授权或唯一身份；`role` 也不是自然人。双层白名单挡不住网关进程本身失陷，这一点写入风险表。浏览器写入的 actor 为 `browser`。
- **线程隔离（P1-12）。** 独立 HTTP 连接池不等于线程隔离：FastAPI 的同步 `def` 路由会先进入共享线程池，再在里面阻塞，交易同步端点会跟着排队。所以网关路由必须是 `async def`，用异步 HTTP 客户端和异步流；准入信号量在事件循环上、进入任何线程池之前获取，满了直接 503。禁止同步 handler 转发或等待 future。上游在 `finally` 中关闭并释放槽。超时分开设：连接、读、写、取连接、总时限。预算分三份互不挤占：配置读写、媒体、snapshot（snapshot 用独立客户端与独立槽）。建议网关每 worker 8 槽，舰队在途上限 = 8 × worker 数，写进配置与测试。
- **影响范围（P1-19）。** 网关隔离只保证不依赖快照的端点（查询、平仓、止盈止损、dry_run 平仓）不受 watcher 故障影响。watcher 故障超过 60 秒后，开仓按 §2.2 fail-closed 返回 `snapshot_unavailable`，这是预期行为，不算隔离失败，也不为保开仓放宽 `max_age`。
- **媒体（P1-16）。** `/v1/watcher/media/:filename` 由 async 网关流式转发，规则如下：
  - Range 只支持单段 `bytes`，包括 `bytes=start-` 与 `bytes=-suffix`；多段与畸形 Range 返回 416，不回退为整文件 200。
  - HEAD 忽略 Range，返回整文件元数据，`Content-Length` 为真实全文件大小。
  - 以整文件 `stat` 判断 ≤ 20 MB，超限在发响应头之前返回 503。响应头发出之后出现超限、超时或断开，只能截流、关闭上游、记 `truncated` 日志，不改状态码。
  - 有总时限、块大小上限与背压；客户端取消后在 `finally` 关闭上游并释放槽。
  - 请求头只转 `Range`、`If-Range`；响应头只回 `Content-Range`、`Accept-Ranges`、`Content-Length`、`Content-Type`、`ETag`、`Last-Modified`、`Cache-Control`，并移除 `Connection` 声明的逐跳头。
  - watcher 侧用 `realpath` 确认文件落在媒体根内，禁止符号链接逃逸。上游 host 固定，不跟随重定向。
  - 响应 `Cache-Control: private, no-store`，app 另做本地缓存清理；两者都不能撤回已落盘的字节，写入风险表。
  - app 的媒体请求独立携带同一 bearer，不走 JSON `send()`。
- **错误语义（P1-17）。** 网关错误体带结构码 `code`：
  - 缺 token：401。错 token：保持现有 403，`code=invalid_token`。已有 `/v1/accounts` 等端点的状态码不改。
  - 角色不足：403，`code=insufficient_scope`。
  - watcher 返回 401/403/5xx 或超时：一律 503，`code=watcher_unavailable`，不把上游 401 原样透传。
- **app 侧"无感"的具体含义。** 信号页与交易配置页直接使用现有交易配置，首次进入自动请求 `/v1/watcher/status`，不出现任何"配置 watcher"界面。按结构码分流：
  - 401 或 `invalid_token`：走与交易页相同的"交易服务连接失效"入口。
  - `insufficient_scope`：提示"当前凭据无此权限"，不引导换密钥。
  - `watcher_unavailable`：只在信号页与交易配置页内提示"采集服务不可达"。
  - watcher 服务层使用独立的 API 实例，其写禁用状态不得传给交易的 `TradingApi`。写请求超时视为结果未知：用同一 `client_ref` 回读或重放，禁止换新 `client_ref` 重试。非 JSON 响应与读 body 超时都要有明确错误。

### 2.2 快照与缓存（D3，关闭 P0-01、P1-01）

- `GET /api/trading/config-snapshot`（snapshot token）：同一 SQLite 读事务内读取三表**白名单字段**，按主键稳定排序；响应 `{schema_version, revision, content_sha256, generated_at, accounts[], channels[], risks[]}`。`revision` 为配置写事务共同递增的整数（新增表 `config_revision`，与每次配置写同一事务 +1）；`content_sha256` 为规范化内容摘要；`generated_at` 只表示生成时间。`accounts[]` 仅含 `account_id, kind(main/sub), parent_account_id, execution_account_id, enabled, risk_capital_addon, default_risk`，**排除 key/secret/session**。服务端校验：重复执行账号、悬空路由、非法父账号、负数/非有限数、缺字段 → 500 且不发布快照；空表与不可读表区分（`accounts: []` vs 错误）。
- 控制面缓存状态机（参数：`refresh_interval=30s`、`max_age=60s`、请求超时 2s、退避、单飞）：
  - 每个 operator-query worker 启动时预热；预热失败 → worker 就绪但 `snapshot_state=cold`。
  - `fresh`（自最近一次成功验证起 age ≤ max_age）：正常使用。刷新失败不改变状态，只延长 age。
  - 不设宽限态：age > max_age 即 `expired`，无"无限期沿用旧值"的路径。
  - `expired` / `cold` / `invalid` / `401`：**依赖快照的 `open_position` 归因一律拒绝**（`snapshot_unavailable`），展示类读取可返回带 `stale` 标记的旧值。非开仓路径不引入快照依赖（回归测试锁定）。
  - 恢复：后台刷新持续进行，不依赖请求触发；恢复后 30s 内回到 `fresh`。
  - 单次订单从同一不可变快照对象取路由、addon、risk，不跨版本；dry_run 与正式提交的证据带 `revision` 与 `age_ms`。
- 副本移除按 §4 四步；`read_api.py` 保留旧 SQLite reader 与切换开关直至影子对照通过。

### 2.3 写路径语义（P1-04、P1-05）

- 新表 `config_audit(id, idempotency_key UNIQUE, actor, source, operation, request_sha256, status_code, response_json, revision_before, revision_after, created_at)`；`idempotency_key = actor + operation + client_ref`。查重、校验、业务写、`config_revision+1`、审计、成功响应在**同一事务**提交；事务外只发响应。同键同摘要 → 返回原响应（`replay:true`）；同键异摘要 → 409。审计不落凭据明文。
- 条件写：账号（启用/层级/执行账号/风险/addon）、路由、风险的 PUT/POST 带 `expected_revision`；不匹配 → 409 并返回最新值，UI 展示最新差异后重新确认。浏览器同样发送版本，服务端不给缺版本请求开放覆盖通道。
- `busy_timeout=5000ms` 显式设置；事务短、不含网络 await；超时返回 503 `db_busy` 可重试（同 client_ref）。
- 群组保存（JSON 文件）与断开/重连（Telegram 连接）不在 SQLite 事务内：定义为可重入操作，成功标准以回读状态为准，不承诺跨存储恰好一次。

## 3. 端点矩阵（阅读版；唯一真源是 §2.1 所述路由文件。未注册路径 404、未列方法 405；HEAD 显式注册）

| 阶段/app 角色 | 方法 路径（app 视角 `baseUrl` 之后的部分为 `/v1/watcher/...`，下表沿用外部完整路径 `/m/v1/watcher/...` 便于对照；网关转发到 watcher 对应 `/api/...`） | 备注 |
|---|---|---|
| P0 四角色读 | `GET /m/v1/watcher/status` | 响应扩展见 §5 |
| P0 snapshot（服务端） | 内网 `GET /api/trading/config-snapshot` | 只读 `snapshot` token，不经网关，app 不可达 |
| P1 四角色读 | `GET /m/v1/watcher/trading/messages`、`GET /m/v1/watcher/trading/briefings` | DB 读模型；分页 `before=(created_at,id)`，上限 500 |
| P1 四角色读 | `GET /m/v1/watcher/media/:filename`（GET/HEAD/Range） | 网关流式转发；watcher 侧鉴权在 static 之前；仅合法文件名与允许类型；禁止 token 入 query；缺图不阻塞文本 |
| P1 risk_admin 写 | `POST /m/v1/watcher/disconnect`、`POST /m/v1/watcher/reconnect` | 明确为写能力 |
| P1 四角色读 | `GET /m/v1/watcher/trading/orders`、`GET /m/v1/watcher/trading/orders/active` | 站点视角只读，来源标注为 `watcher`，不当 V3 身份 |
| P2 四角色读 / risk_admin 写 | `GET /m/v1/watcher/dialogs`、`POST /m/v1/watcher/groups` | 已选从 `status.watchGroups` 读；候选截断 100 时不得丢失原已选 |
| P2 四角色读 / risk_admin 写 | `GET /m/v1/watcher/trading/accounts`、`PUT …/accounts/:id`（仅非秘密字段）、`DELETE …/accounts/:id` | **不放行 POST 创建**（D7） |
| P2 四角色读 / risk_admin 写 | `GET/POST /m/v1/watcher/trading/channels`、`DELETE …/:id`；`GET/POST /m/v1/watcher/trading/risks`、`DELETE …/:symbol` | POST 为 upsert，带 `expected_revision` |
| P2 | 写响应直接返回 `revision`；不向 app 暴露 snapshot | — |
| P3 四角色读 / risk_admin 写 | `GET/POST /m/v1/watcher/price-alerts`、`DELETE …/:id`、`GET /m/v1/watcher/price-monitor/status` | `DELETE /order/:orderId` 待身份改造后放行 |
| 永不放行 | `/api/config`、`/api/login/*`、`/api/login/qr/*`、站点静态入口、`/healthz` | 凭据面 |

拒绝证据：矩阵外请求可以进入 operator-query，由网关拒绝且 watcher 侧请求计数不变即可；只有 Caddy 本身不匹配时，控制面计数才同样不变。另用 `gateway` token 直连 watcher 遍历矩阵外端点，证明第二层同样拒绝。公网 Caddy 不新增任何指向 watcher 的路由。

## 4. Phase 0 — 地基（四步切换）

### 4.1 设计
1. **新增并验证**：watcher 鉴权中间件（§2.1）、`config-snapshot`（§2.2）、`config_revision`/`config_audit` 表、`busy_timeout`；控制面新 reader + 缓存状态机（开关默认关，旧 reader 保留）；operator-query `/m/v1/watcher/*` 网关（§2.1、§3）；Caddy 只改浏览器入口的清头与注入；app 不新增任何配置项。
2. **影子三路对照**（隔离环境）：A=旧 reader+旧副本、B=旧 reader+真库一致快照、C=新 reader+同一真库 HTTP 快照。先做数据基线（三表非秘密字段导出、主键/行数/内容摘要、逐项差异与预期裁决），再固定计算输入（real_equity、available_balance、entry、stop_loss、caps、account_id、source_channel、client_ref、dry_run）对照 `account_equity_basis`、`risk_sizing`、路由目标、`revision`。A/B 差异=既有漂移；**B/C 必须等价**（除本文明确修订的风险缺省/失败策略）。
3. **切换**：所有 operator-query worker 预热成功并记录 revision → 打开新 reader 开关 → 重启对应服务（部署门禁完成后的授权窗口内）。
4. **移除副本**：外部路由白名单与浏览器回归通过、生产只读/dry_run 冒烟通过、失败恢复演练通过后，删 env 别名与副本（备份保留 30 天）。回滚步骤：关闭开关 → 恢复 env → 重启 → 清缓存；回退数据必须是回退时经校验的真库一致快照，不是 09-06 文件。

### 4.2 验收标准
- 鉴权：watcher 直连无头、伪造头、重复头、旧 token 撤销、代理注入缺失，全部 401；`gateway`、`snapshot`、浏览器三种身份各自只能访问自己的行，交叉调用被拒绝；token 未配置时 watcher 拒绝启动，operator-query 缺 `gateway` 时只禁用网关。网关层：无 token 401；错 token 403 且 `code=invalid_token`；`viewer` 写 403 且 `code=insufficient_scope`，同一个 `viewer` 随后读 status 仍成功；四角色 × 路由的允许/拒绝表全部有测试；`system_observer` 与 `reviewer` 可读 status。浏览器 basicauth 路径回归不变。
- 无感：app 不改任何设置，升级后首次进入信号页即可加载；按轮换顺序轮换 `gateway` token 期间 app 无报错；控制面 token 失效时信号页与交易页进入同一个连接失效入口；watcher 返回 401 时网关给 503，随后交易 `dry_run` 仍被授权。
- 隔离：分两段暂停 watcher 容器并实测。10 秒内：平仓 `dry_run` 的 p99 与错误率相对基线无显著变化，网关满槽后第 N+1 个请求在准入处 503，客户端断开后槽回收。61 秒后：平仓仍正常，开仓 `dry_run` 返回 `snapshot_unavailable`。未实测不得宣称交易不受影响。代码审查确认没有同步 `def` 转发、没有同步 handler 等待 future。
- 快照：无敏感字段（哨兵测试）；同秒改值、删旧插新、等行数替换均改变 `revision` 与 `content_sha256`；无变更重读不变；损坏表 → 错误而非空表。
- 缓存：fake clock 验证 59/60/61s；冷/热、单飞、超时、401、重启；`expired` 时 `open_position` dry_run 返回 `snapshot_unavailable`，减仓/平仓不受影响。
- 对照：B/C 在成功值、错误码、拒因上等价（覆盖四账户、全部路由、全部配置品种、异常状态：禁用、重复执行账号、非法父账号、addon 非有限数）。
- 部署：记录部署前每节点状态/心跳/审计，部署后**保持授权状态**（HALTED 保持 HALTED，**不把 RESUME 列为部署步骤**）；Caddy 变更用 `validate` + `restart`（禁 reload）。

### 4.3 测试标准
- T0-1 watcher 中间件：无头、错头、三身份、交叉身份拒绝、轮换双值、撤销、伪造与重复入口头；actor 与指纹头仅在 `gateway` 下接受，缺或多于一条 400；`gateway` 身份提交 `api_key` 返回 400 且 DB 不变；token 未配置启动失败；指纹夹具使用规范化后的 token；秘密哨兵（响应、审计、日志不含 token 与密钥）。
- T0-1b 网关：四角色 scope 表；结构码 `invalid_token`、`insufficient_scope`、`watcher_unavailable`；请求与响应头清洗；body 与 query 白名单；PUT 带 `api_key` 返回 400；准入信号量满载 503；各类超时；上游 302 不跟随；配置、媒体、snapshot 三份预算互不挤占；隔离实测按 §4.2 两段口径。媒体：HEAD 无 body 且长度为全文件；`bytes=0-0` 返回 206 且 total 为真实大小；开放区间与后缀 Range；多段与畸形 416；超过 20 MB 在发头前 503；发头后截流有 `truncated` 日志；取消后槽回收；`realpath` 与符号链接负例。
- T0-2 快照：字段白名单哨兵；三表空/有行；同秒写、删除、等行数替换；并发写；损坏响应；`schema_version` 不匹配拒绝。
- T0-3 缓存：状态机全路径（cold/fresh/expired/invalid/401/恢复）、单飞、多 worker、fake clock；非开仓无快照依赖回归。
- T0-4 路由真源：由真源生成的路由与真源 diff 为空；独立手写负例（不从真源生成）：百分号编码的斜杠与点、双重解码、尾斜杠、大小写、重复 query、OPTIONS、HEAD；未注册 404、未列方法 405；经网关遍历矩阵外请求，watcher 计数不变；`gateway` token 直连遍历，第二层同样拒绝。
- T0-5 app：不新增存储键（`tradingStorage` 键集合快照不变）；请求 URL 为 `baseUrl + /v1/watcher/status`，没有双重 `/m`；按结构码分流三种提示；watcher API 的写禁用不影响交易 `TradingApi`；写超时不换新 `client_ref`；JSON 错误、非 JSON、读 body 超时都有明确错误；媒体请求不走 `send()`；不回退 fixtures。
- T0-6 对照脚本可重复执行并产出差异报告（步骤 2 的工具化）。

## 5. Phase 1 — 只读能力（「信号」Tab）

### 5.1 设计
- `/api/status` 扩展：`observed_at`、`connection`（connected/disconnected/needs_login）、`listener`（listening/stopped）、`last_telegram_activity_at`（最近成功 Telegram 活动，含空轮询）、`last_message_ingested_at`（最近入库消息）、`watchGroups`。状态条三态：**失活**（`observed_at - last_telegram_activity_at > 300s`）、**静默**（活性正常但无新消息）、**接口失败**；静默不当故障。
- 消息流读 DB 模型 `/api/trading/messages`：身份 `(channel_id, msg_id)`；本地列表 key 用 DB `id`（仅同数据集内稳定）；时间统一 UTC 解析，并列时间按 `id` 稳定排序；分页游标；上限 500；缩略仅对有 `media_filename` 的行；媒体经网关 `/m/v1/watcher/media/:filename` 转发，缺图不阻塞文本。前台 30s 轮询、后台停轮询、恢复即刷新。
- 简报二级页；站点订单只读区（来源标注 `watcher`）。
- 断开/重连：状态机 `pending → connected | failed | needs_login`；请求期限 5s、完成期限 60s；按钮点击后显示 pending 并回读；重复点击去重；明确断开后 watchdog 不自恢复。
- 三态渲染；禁止 fixtures 进生产代码。

### 5.2 验收标准
- 两端消息对照：相同频道、相同时间窗口、相同数量、相同截至水位下 `(channel_id,msg_id)` 集合一致。
- 状态条：断网、静默频道、正常三种情形分别显示正确状态。
- 重连：无 session 时显示"需站点登录"；成功/失败/超时三种结果都有明确 UI。
- 性能（P2-01）：固定目标真机（用户当前机型）、release 构建、200 条含图数据、Wi-Fi、60s 滚动脚本；掉帧率 < 3%、长帧（>100ms）< 5 次、内存峰值 < 300 MB。

### 5.3 测试标准
- T1-1 解析器：脱敏真实响应 fixture（status/messages/briefings/orders）；ring 与 DB 身份转换；UTC。
- T1-2 状态判定：活性/静默/失败三分支，299/300/301s 边界。
- T1-3 列表：memo 不重渲染；分页游标；乱序响应丢弃；媒体 401/404/大图/缓存。
- T1-4 断开/重连：确认框、pending、成功、失败、超时、无 session、重复点击、断开后不自恢复。
- T1-5 守卫：`noFixturesInProduction`、`noHardcodedHexColors`。

## 6. Phase 2 — 配置编辑（设置 → 交易配置）

### 6.1 设计
- 三个子页：账号（列表；编辑非秘密字段：启用、层级、执行账号、`default_risk`、`risk_capital_addon`；「计算加权额」= `addon = target - initial`；**创建与密钥轮换跳转站点**）、路由（频道→执行账号；候选来自 `/api/dialogs`，已选合并显示，截断不丢已选）、风险（品种→比例）。
- 规则统一（服务端 + 两端 UI）：品种风险比例 **`(0, 0.1]`**，越界拒绝；无记录品种的默认策略单独定义（沿用控制面 `default_risk` 或 env 默认，UI 如实显示"使用默认 x%"）；`addon ≥ 0` 有限数；账号 ID 唯一、每个子账号恰有一个合法主账号（允许多个 main）；禁用账号语义 = 不再开仓，与交易 RESUME/HALT 无关（文案区分）。
- 写流程：本地校验 → 摘要确认（展示服务端最新值与拟改值差异）→ 提交（`client_ref` + `expected_revision`）→ 409 时展示最新差异重新确认 → 成功后回读并显示新 `revision`。
- 影响提示：改风险/addon 时按当前权益换算一笔示例名义额；明确哪些字段实际影响 V3 开仓（`default_risk` 是否被采用以控制面代码为准，UI 不得承诺未采用的字段）。
- 群组保存：空选择、非法输入、未保存取消、监听列表与路由列表独立。

### 6.2 验收标准
- 账号（非秘密字段）/路由/风险各一次增改删后，站点与 app 列表一致，`revision` 递增，`config_audit` 有对应记录（source/actor 正确，无凭据）。
- 版本冲突：两端读 v1 → 站点写 v2 → app 持 v1 提交 → 409 → 重新确认 → 成功；已成功的同 ref 重放返回 `replay:true`，不被误判为冲突。
- 幂等崩溃点：crash-before-commit（无变更、无审计）、commit-before-response（重试返回原响应）、并发同 ref、重启后重放、DELETE 重放。
- busy：独立进程持锁 < timeout → 等待后成功；> timeout → 503 可重试；无半写。
- 控制面一致性（隔离环境，固定输入）：改风险比例后 dry_run 的 `risk_sizing` 与 `revision` 反映新值；生产只做只读/dry_run 冒烟，不做破坏性 CRUD。
- 掩码占位值回写被服务端拒绝；账号删除受子账号/路由/活动单约束。

### 6.3 测试标准
- T2-1 校验：比例边界 0/0.1/0.10001/负数/NaN；addon 0/负/空串/布尔；主子账号规则；加法公式。
- T2-2 二段式：差异展示、取消不请求、409 重确认、mask 不回写。
- T2-3 幂等与事务：上述崩溃点逐一；同键异摘要 409。
- T2-4 端到端：隔离 watcher + 控制面固定输入对照。
- T2-5 群组：保存/空选/非法/取消/截断不丢已选。

## 7. Phase 3 — 价格提醒（D8：仅记录与展示）

### 7.1 设计
- `price_alerts` 表扩展身份：`source(watcher|v3)`、`account_id`、`position_ref`（V3 为 `symbol+position_side+account_id`，站点为 `order_id`）、`environment(mainnet|testnet)`、`triggered_at`、`delivered_at`（本轮恒空）；列表/删除均按身份限定，不按 symbol 推断归属。
- 校验：`target_price` 正有限数；`direction ∈ {above, below}`；symbol/note 长度限制；价源按账号环境选择并显式记录；`PRICE_MONITOR_ENABLED=false`、表不存在、价源失败在 `/api/price-monitor/status` 显式返回。
- 触发：一次性写 `triggered_at`，不删除记录；app 仓位详情显示"已触发 hh:mm"；仓位读取 stale/失败时**不**判定孤立、不清理。
- 统一 price-monitor 的 DB 路径解析到 canonical env。

### 7.2 验收标准
- 创建/查看/删除按身份作用域正确；四账户同 symbol 互不干扰；mainnet/testnet 分离。
- 隔离测试价源：above/below、等值、未达、非法值、行情缺失/超时、监控停用；触发只记录一次；重启不丢未触发记录。
- 明确无推送：UI 文案与 `status` 均说明"仅记录触发，不推送"。

### 7.3 测试标准
- T3-1 校验与身份作用域；T3-2 触发一次性与重启；T3-3 stale 仓位不清理；T3-4 状态端点的三种失效态。

## 7A. 平板适配（横切，D9；v0.2 新增）

平板不是一个新 Phase，而是一套布局约束：先落基础设施（T-0），之后 A-1..A-3 的新页面按三档写，存量页面在 A-T 里改。

### 7A.1 设计

- **断点只看窗口，不看设备。** 新增唯一入口 `useLayoutClass()`，基于 `useWindowDimensions().width`（dp）返回 `compact | medium | expanded`，阈值 600 / 840，与 Material 3 窗口尺寸类一致。分屏、自由窗口、旋转都自然生效。禁止在组件里散落 `Dimensions.get` 或自写阈值（加守卫测试）。
- **导航。** compact 保持底部 Tab。medium 与 expanded 用 bottom-tabs v7 的 `tabBarPosition: 'left'`，侧栏图标加标签；Tab 集合、testID、深链 `linking.ts` 不变。切换档位时保持当前 Tab 与 Stack 状态，不重挂载屏幕。
- **内容宽度。** 设置、表单、配对、服务连接、请求详情等单列页面在 medium/expanded 下居中，最大内容宽 720dp；不放大字号与控件，保持手机的信息密度。
- **交易 Tab。**
  - 账户页：净值波形图通栏在上；四张账户卡 medium 两列、expanded 两列或四列（按可用宽度 ≥ 1200dp 时四列）。
  - 超宽屏约束（针对 13.3 英寸横屏，宽度可能超过 1700dp）：左栏列表固定宽度不随屏幕拉伸；右栏详情内容最大宽 840dp，多出的空间留白，不把数字行拉得过长；抽屉最大宽 640dp 不变。波形图在账户页通栏显示，但高度不随宽度等比放大。
  - 仓位页 expanded：左栏仓位列表（固定约 400dp，沿用现有卡片/列表切换），右栏为仓位详情；点选即切换右栏，不 push。compact/medium 保持现在的 push 到 `PositionDetail`。选中项在旋转、分屏、列表刷新后保持；选中仓位消失时右栏显示"仓位已不存在"并禁止写操作，沿用 `0f7d26d` 的"阻止对已消失仓位写入"语义。
  - 仓位列表视图在 medium 以上增加列（保证金、强平价、止盈止损状态），列宽规则沿用现有"两列自适应、其余 `flexShrink: 0`"。
- **信号 Tab（A-1 按三档直接写）。** expanded：左栏消息流，右栏消息详情（全文、媒体大图、来源频道、`(channel_id,msg_id)`）；状态条横跨两栏顶部。简报与站点订单同样列表加详情。
- **交易配置（A-2）。** expanded：左栏账号/路由/风险分组列表，右栏编辑表单；二段式确认与 409 差异展示放在右栏内，不弹全屏。
- **抽屉。** `BottomSheet` 在 medium 与 expanded 下改为居中贴底、最大宽 640dp，两侧露出遮罩；高度上限仍为窗口高度 88%。横屏时底部安全区与键盘避让重新验收（止盈止损与平仓输入框）。不改为居中对话框（antd `popup+transparent` 居中的旧问题已知）。
- **图表。** `EquityWaveform` 已按 `onLayout` 取宽，宽屏只需验收：横坐标标签密度随宽度增加而不重叠，实时点与"现在"标签位置正确，旋转后缓存的触摸点按已有逻辑丢弃。
- **不做。** 不锁方向；不单独打平板包；不引入平板专属功能；不改颜色、字号体系与 FLAG_SECURE 策略。

### 7A.2 验收标准

- 三档在同一构建里切换：手机竖屏、平板竖屏、平板横屏、平板分屏半屏四种窗口下，导航位置、内容宽度、栏数符合 §7A.1。
- compact 档与改前逐屏对照截图无回归（Tab 位置、卡片、抽屉、波形）。
- 仓位双栏：点选切换详情、旋转后选中保持、刷新后选中保持、选中仓位被平掉后右栏进入不可写态；平仓与止盈止损抽屉在双栏内可用，提交链路与手机一致（仍走 dry_run → 确认 → 提交）。
- 抽屉：平板上最大宽生效；横屏打开键盘时输入框不被遮挡；返回键关闭。
- 性能：expanded 双栏下仓位列表 60s 滚动脚本，掉帧率 < 3%（与 §5.2 同口径，设备换成目标平板）。
- 验收设备：Xiaomi Pad 9 Pro Max（13.3 英寸 LCD，3408×2272，308 PPI，3:2，最高 144Hz，HyperOS 4）。dp 宽度 = 像素 × 160 ÷ 系统密度，系统密度由厂商设定和用户的"显示大小"决定，不等于物理 PPI，所以只能预估：

  | 系统密度 | 横屏宽 | 竖屏宽 | 横屏分屏半屏 |
  |---|---|---|---|
  | 308 | 1770dp | 1180dp | 885dp |
  | 400 | 1363dp | 909dp | 682dp |
  | 440 | 1239dp | 826dp | 620dp |
  | 480 | 1136dp | 757dp | 568dp |

  **实测（2026-09-26，adb，型号 M367FC，Android 17，默认"显示大小"）：Physical size 2272x3408，Physical density 400，无刘海。** 换算：竖屏宽 909dp（宽屏档），横屏宽 1363dp（宽屏档，超宽屏约束生效），横屏左右分屏半屏约 680dp（中等档），竖屏上下分屏仍为 909dp（宽屏档）。"显示大小"放大一级后需在验收时再实测一次密度（若为 440：竖屏 826dp 落中等档、横屏分屏约 620dp 仍为中等档）。

  结论：横屏在任何密度下都是宽屏档，而且远宽于普通平板；竖屏落在中等或宽屏，取决于密度；横屏分屏半屏可能落到手机档。T-0 开工前在真机上执行下面两条命令，换算实际 dp 并写回本表，作为验收基准。验收覆盖默认"显示大小"和放大一级两种设置。

```bash
adb shell wm size
adb shell wm density
```
- 真机证据：目标平板横竖屏各一组截图（账户、仓位双栏、信号双栏、交易配置双栏、抽屉），外加 Android 模拟器 Pixel Tablet 同组截图。

### 7A.3 测试标准

- T7A-1 `useLayoutClass`：599/600/839/840 边界；宽度变化触发重算；无窗口尺寸时返回 compact。
- T7A-2 守卫：`src/` 中除 `useLayoutClass` 与 `BottomSheet` 高度外不得出现 `Dimensions.get`、硬编码断点数字。
- T7A-3 导航：三档下 Tab 集合、testID、深链解析一致；档位切换不重挂载当前屏（渲染计数断言）。
- T7A-4 仓位双栏：选中保持、选中消失、列表刷新、切回 compact 时回到 push 模式并保留选中仓位的详情页。
- T7A-5 抽屉：三档最大宽度；安全区；键盘避让（横屏）。
- T7A-6 新页面约束：A-1、A-2 的每个屏幕都有 compact 与 expanded 两个渲染用例，作为各自验收的一部分。

## 8. 安全与风险

| 风险 | 处置 |
|---|---|
| 控制面 token 权限扩大 | app 的 `risk_admin` token 现在也能改风控配置、停采集、断开采集，泄露影响随之扩大；缓解：写端点只放 `risk_admin`、每写审计带 token 指纹、watcher 不开公网入口、轮换仍是一处操作 |
| 网关拖累交易端点 | async 路由 + 事件循环上准入 + 三份独立预算；两段实测锁定（§4.2、T0-1b） |
| 网关进程失陷 | 双层白名单挡不住；缓解：网关只持 `gateway` 一个 watcher 凭据、秘密字段在 watcher 第二层也拒绝、审计带指纹、轮换流程可在一处撤销 |
| 媒体已落盘字节 | `no-store` 与本地清理不能撤回已缓存内容；媒体为频道图片，不含凭据，接受此残留 |
| 快照失效 | 有限授权 60s + fail-closed 只拒开仓；预热/单飞/后台刷新缩短拒绝窗；`revision/age` 进证据 |
| 副本切换 | 三路对照 + 开关 + 完整回滚步骤 + 30 天备份；副本最后移除 |
| 凭据 | Binance 密钥与 Telegram 会话不经 app；掩码不回写；哨兵测试覆盖 snapshot/审计/错误/日志 |
| 并发写 | `expected_revision` 条件写 + 幂等键 + 显式 busy_timeout |
| 遗留写入旁路 | `db_manager.py` 写命令停用或改走服务；feeder 只读 |
| 部署 | 不自动 RESUME；破坏性实验只在隔离环境 |
| 记账红线 | 不触碰记账五表；无控制面迁移 |

## 9. 交付拆分（含 owner 与依赖）

| 模块 | 内容 | 仓库 | 依赖 |
|---|---|---|---|
| W-0 | 鉴权中间件（`gateway`/`snapshot`/浏览器三身份）、actor 头、`config-snapshot`、`config_revision`/`config_audit`、条件写、busy_timeout、受保护媒体路由、`status` 扩展、db_manager 写命令停用 | trader-bot `bridge/services/telegram-watcher` | — |
| C-0 | 新 reader + 缓存状态机 + 开关，非开仓回归 | trader-bot `services/control-plane` | W-0 契约 |
| C-1 | watcher 网关：路由真源文件与生成器、async 路由与准入、四角色 scope、结构码、头/body/query 清洗、actor 注入、三份预算、媒体流式转发全规则、契约写入 `contracts/backend-api.md` | trader-bot `services/control-plane` + `contracts/` | W-0 契约；与 C-0 文件范围互斥或串行 |
| O-0 | 三路对照工具与报告；服务端凭据生成、两两互异校验与 env 下发（不经 app）；预热切换；副本移除。Caddy 现场清单（现场执行需另行授权）：确认 import 与 handler 顺序、`/m` 只 strip 一次、上游仍是 8183、移动端 `Authorization` 保留且不被面板注入的 `system_observer` 覆盖、浏览器 basicauth 后先清头再注入、`/media` 与其他公网入口没有绕过；按路由真源逐路径追加；记录 watcher 端口映射为宿主 9090 对容器 9100；按 §2.1 顺序轮换并验证回滚 | jp-24 + trader-bot scripts | W-0、C-0、C-1 |
| A-0 | `watcherApi` 服务层：复用交易配置但独立实例、按结构码分流、写超时语义、独立媒体请求；设置页只读的"采集服务"状态行，无新输入项 | alert-personal | C-1、O-0 |
| A-1 | 信号 Tab：状态条、消息流、简报、站点订单、断开/重连、媒体 | alert-personal | A-0, W-0 |
| A-2 | 交易配置三子页 + 群组选择 | alert-personal | A-0, W-0, C-0（一致性验收） |
| A-3 | 价格提醒（D8 范围） | alert-personal + W-0 表扩展 | A-0 |
| T-0 | 平板基础设施：`useLayoutClass`、侧栏导航、单列页最大宽、`BottomSheet` 宽屏形态、守卫测试 | alert-personal | 固定基线；可与 W-0/C-0 并行 |
| A-T | 存量页面平板改造：账户多列、仓位双栏与宽列表、图表宽屏验收、真机截图 | alert-personal | T-0 |
| N-1（另立） | 价格事件投递链路（attention/Telegram、确认/重试/去重） | 待定 | D8 之后 |

派发顺序：**先冻结 §2/§3 契约 → W-0、C-0、T-0 并行，C-1 紧随 W-0 契约 → O-0 对照通过 → A-0..A-3（新页面按三档写），A-T 在 T-0 之后任意时点插入**。T-0 先于 A-1，避免信号页先写成手机单列再返工。每模块六段式任务书；验收证据式（diff、测试实跑、真机截图、对照报告）。

## 附录 A. v0 自查勘误（已并入正文）
- `signal_operations` 非通用审计表 → 改用 `config_audit`（§2.3）。站点测试基线 62/0（2026-09-11）。

## 附录 B. v0 → v0.1 变更对应 review 编号
- P0-01 → §2.2 缓存状态机（`max_age` 60s fail-closed、预热、单飞、不可变快照、证据带 revision）。
- P0-02 → §2.1 三身份无条件鉴权、Caddy 清头注入、缺 token 启动失败。
- P1-01 → §2.2 `config_revision` + `content_sha256` + 白名单字段 + 校验。
- P1-02 → §4 四步切换与三路对照、回滚步骤。
- P1-03 → §6.1 规则统一（`(0,0.1]`、加法、主子账号规则、默认策略）。
- P1-04 → §2.3 `config_audit` 幂等事务、busy_timeout。
- P1-05 → §2.3 `expected_revision` 条件写。
- P1-06 → §3 端点矩阵、受保护媒体路由（v0.3 起经网关转发）。
- P1-07 → §5 status 扩展、身份 `(channel_id,msg_id)`、重连状态机。
- P1-08 → D7。
- P1-09 → D8、§7。
- P1-10 → §7.1 身份模型与价源环境。
- P1-11 → §4.2 部署验收不含 RESUME、隔离实验、§9 owner/依赖闭合（群组入 A-2，订单入 A-1，推送另立 N-1）。
- P2-01 → §5.2 可测量性能指标。
- P2-02 → D1 写入者清单、db_manager 停用。

## 附录 C. v0.1 → v0.2
- 新增 D9 与 §7A 平板适配；§9 增加 T-0、A-T 两个模块并调整派发顺序。
- app 固定基线由 `fd9f50e` 改为 `0f7d26d`（后者包含前者，并带 09-17/09-18 的平仓结果与已消失仓位写保护）；§1.3 补充版本与响应式现状。
- §7A 未经 Codex review；如需要，与 v0.1 的复核一起派发。

## 附录 D. v0.2 → v0.3
- 记录用户对 D1–D6 的裁决（§0.1）。
- 按"app 统一密钥无感"改写 D2 与 §2.1：取消 mobile token 与 Caddy `/w` 入口，app 经 operator-query `/m/v1/watcher/*` 网关访问，复用现有控制面 token；watcher 只认 `gateway`、`snapshot`、浏览器三种服务端身份。
- 同步改写 §3 矩阵、§4 验收与测试、§8 风险、§9 模块（新增 C-1，A-0 改为无输入项）。
- 此次改写落在 P0-02 所在的信任边界，开工前应让 Codex 针对 §2.1 与 §3 做一次定向复核。

## 附录 E. v0.3 → v0.4（吸收 v0.3 定向 review）
- P1-12 → §2.1 线程隔离：async 路由、事件循环准入、三份预算、舰队在途上限；§4.2 两段实测。
- P1-13 → §2.1 角色范围：四角色读、`risk_admin` 写、同 token 一并获权写入契约。
- P1-14 → §2.1 actor：网关背书、缺或多头 400、指纹仅审计标签、发行侧互异、各进程只持自己的凭据。
- P1-15 → §2.1 路由真源文件、三身份字段集、watcher 第二层拒秘密字段；T0-4 独立负例。
- P1-16 → §2.1 媒体规则（单段 Range、416、发头前后分流、头白名单、realpath、不跟随重定向、no-store 残留）。
- P1-17 → §2.1 错误语义与 app 分流：错 token 保持 403 + `invalid_token`，上游错误统一 503，watcher API 独立实例。
- P1-18 → §2.1 路径约定 `baseUrl + /v1/watcher`；O-0 依赖加 C-1 与完整 Caddy 现场清单；缺 `gateway` 不拖垮交易启动。
- P1-19 → §2.1 影响范围：隔离不覆盖开仓，61 秒后开仓 `snapshot_unavailable` 为预期。
- P2-03 → 未注册 404、未列方法 405、HEAD 显式注册。

## 附录 F. v0.4 → v0.5
- D7、D8、D9 按推荐通过（2026-09-25），§0.1 记录；平板验收基准定为 Xiaomi Pad 9 Pro Max，§7A.2 增加真机尺寸实测步骤。

## 附录 G. v0.5 → v0.6
- §7A.2 写入 Xiaomi Pad 9 Pro Max 屏幕参数与四种系统密度下的 dp 预估；真机实测命令；"显示大小"两档验收。
- §7A.1 增加超宽屏约束：左栏定宽、右栏内容最大宽 840dp、波形图高度不随宽度放大。
