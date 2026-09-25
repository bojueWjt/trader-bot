# watcher-to-app-migration v0.3 定向 review

审查日期：2026-09-24。对象：`docs/plans/2026-09-11-watcher-to-app-migration.md` v0.3，范围仅 §2.1、§3、§4.2、§4.3、§8、§9 与模块 C-1 / O-0 / A-0。

**总结论：修改后开工。** 原 P0-02「缺入口头即免鉴权」的设计漏洞已关闭，实现未验收。本轮 **0 新 P0 / 8 P1 / 1 P2**。P1 未写入设计前不要派 C-1。

## 审查边界与证据口径

- 只读静态核验。未运行测试、未访问 jp-24、未改其他项目文件、未提交。唯一改动是本文件。
- 已核对：`services/control-plane/api/read_api.py`、`contracts/backend-api.md`、`bridge/services/telegram-watcher/server.js`、`bridge/services/telegram-watcher/lib/trading-api.js`。
- app 固定基线 `0f7d26d`（`alert-personal/.worktrees/close-visible-result-20260918`）。下文别名：
  - `app/tradingApi.ts` = `/Users/balen/projects/working/alert-personal/.worktrees/close-visible-result-20260918/apps/attention-android/src/services/tradingApi.ts`
  - `app/tradingStorage.ts` = `/Users/balen/projects/working/alert-personal/.worktrees/close-visible-result-20260918/apps/attention-android/src/services/tradingStorage.ts`
  - `app/tradingScreenWiring.test.tsx` = `/Users/balen/projects/working/alert-personal/.worktrees/close-visible-result-20260918/apps/attention-android/__tests__/tradingScreenWiring.test.tsx`
  - `app/validation.ts` = `/Users/balen/projects/working/alert-personal/.worktrees/close-visible-result-20260918/apps/attention-android/src/utils/validation.ts`
- 辅证：`services/control-plane/security/permissions.py`、`services/control-plane/db/pools.py`、`services/control-plane/settings/router.py`、`INTEGRATION_REPORT.md` §7、`bridge/docker-compose.yml`；本机 `.venv-arch/lib/python3.12/site-packages/starlette/concurrency.py`、`starlette/responses.py`、`fastapi/routing.py`。行号以当前工作区为准。
- Caddy 现行配置、jp-24 环境变量、生产流量均未核。`INTEGRATION_REPORT.md` 的 `/m` 片段是 2026-08-31 记录，不能代替现场证明。本会话不执行 O-0 现场清单。
- 未实现的网关不构成已发生事故。

## 一、P0 状态

### P0-02（设计已关，实现未验收）

v0 把「无 `X-Watcher-Entry: mobile`」当成浏览器免 token。v0.3 §2.1 改为：watcher 只认 `gateway` / `snapshot` / 浏览器代理三种服务端身份；每个请求在全部 handler 与静态文件之前校验，否则 401；不开公网 `/w`；缺 token 则对应进程拒绝启动；Caddy 先清外来 `X-Watcher-*` 再注入。这关闭了「缺头 = 未鉴权」的设计漏洞。

现行代码仍是旧面：`server.js:68-70` 先挂 `express.static` 与 `/media`，`server.js:863` 才注册交易 API，进程内无鉴权中间件。宿主映射 `127.0.0.1:9090:9100`（`docker-compose.yml:157`、`:178`）只说明公网默认进不来，不证明实现已按 §2.1 封闭。W-0 仍须按 T0-1 验收。

本轮没有新的、可用当前代码证实的 P0。

## 二、P1 必修

### P1-12：独立连接池不是线程池隔离

**证据：** §2.1 写「read_api 是同步 handler，代理调用会占用线程池，所以网关使用独立连接池与并发上限（建议 8）」（设计 `:86`）。§8 把独立连接池当隔离手段（设计 `:258`）。T0-1b 要求线程池占用测试（设计 `:147`）。

FastAPI 对非协程 path operation 在 `run_endpoint_function` 里丢进线程池：`.venv-arch/lib/python3.12/site-packages/fastapi/routing.py:344-354`（`is_coroutine` 则 `await`，否则 `run_in_threadpool`）。`run_in_threadpool` 即 `anyio.to_thread.run_sync`（`starlette/concurrency.py:32-34`）。同步 `StreamingResponse` 的 body 不是异步可迭代时，按块走 `iterate_in_threadpool`（`starlette/responses.py:233-236`，`concurrency.py:51-57`）。官方说明：[Path Operation Functions](https://fastapi.tiangolo.com/async/#path-operation-functions)（`def` 在线程池，`async def` 在事件循环）。

现行交易写是同步 `def operator_order`（`read_api.py:7583-7594`）。Postgres 角色池（`db/pools.py:61`，operator-query `max_size=4`）不是 anyio 线程池。`v1_stream` 已是 `async def`（`read_api.py:367-388`）。

**失败场景：** 同步网关路由在**已经进入共享线程池之后**再阻塞等信号量或 HTTP 连接池，则「大于该 worker 实际默认线程容量」的并发会占满共享池，交易同步 `def` 跟着排队。这不是「8 个请求必然耗尽默认线程池」：默认容量按 **worker** 计，8 只是设计建议的网关槽；8×worker 才是舰队在途上限，必须单独约束。8 个慢流只证明占了连接/线程/槽，**不证明**交易 p99 已坏，要实测。同步 handler 里 `future.result()` 等专用 executor，仍占着默认线程，等于没隔离。

**修法：** 网关必须是 `async def`，异步 client、异步流。准入信号量在事件循环上、进共享线程池之前获取，满则 503。禁止默认同步线程池做流式转发。专用 executor 也必须先由 async 路由准入再 `submit`，禁止同步 handler 等待 future。占用到 `finally` 取消上游、释放槽。超时拆开：connect / read / write / pool acquire / 总 deadline。snapshot 独立 client 与独立槽，不与媒体共用。8×worker 总额写入配置与测试。

**验收：** 慢流与满槽用例记录占用（线程/槽/连接），并**实测** `close_position` dry_run 的 p99/错误率相对基线；未测不得宣称交易不受影响。第 N+1 个网关请求在准入处 503。客户端断开后槽回收。代码审查：无同步 `def` 流；无同步 handler 等 future。

### P1-13：写明四角色，不要「viewer 及以上」

**证据：** §2.1「读端点允许 `viewer` 及以上」（设计 `:77`）。`require_reader` 只从四个环境变量映射角色并返回这四个字符串（`read_api.py:85-90`、`:309-328`）：`system_observer`、`viewer`、`risk_admin`、`reviewer`。它**不能**返回 `nautilus_node` 或 `operator`。`settings/router.py:19-24` 另有 `OPERATOR_TOKEN → operator`，与 `require_reader` 不是同一张表。`operator_order` 在四角色之后再要求 `risk_admin`（`read_api.py:7592-7594`）。§8 承认 watcher 写扩大同一把钥匙的半径（设计 `:257`）。

**失败场景：** 「viewer 及以上」没有全序。若只放行字面 `viewer`（加写给 `risk_admin`），则 `system_observer`（面板读 `/v1/accounts`）和 `reviewer` 读 `/v1/watcher/status` 会 403，和「无感复用控制面 token」冲突。若默认「除 viewer 外都算以上」，又没有书面范围。D2 已同意复用现有控制面 token：同一 `risk_admin` 扩大到 watcher 写端点，**可接受**，无需新角色或新密钥。但所有持有这把 token 的调用方一并获权；正文若不写 endpoint scope 表，实现会漏放或超放。

**修法：** 按四个 `READER_TOKEN_ENV` 角色列 endpoint scope，不发明格子。读：四角色都允许矩阵内读。写：仅 `risk_admin`，且仅已列写端点。不新增 token。若以后要区分自然人，用服务端 principal 映射，不给 app 新密钥。

**验收：** 四角色 × 矩阵方法/路径允许/拒绝表有测试。`system_observer` 与 `reviewer` 可读 status。`viewer` 写 → 403 `insufficient_scope`。契约写明「不新增密钥；同 token 持有者一并获权」。

### P1-14：actor 是网关背书；缺头/多头契约；指纹只是审计标签

**证据：** §2.1 网关写入 `X-Watcher-Actor` 与 SHA-256 前 12 位指纹；watcher 仅在 `gateway` 身份下信任该头（设计 `:85`）。`require_reader` 对 token 做 `Bearer` 剥离 + `strip()`（`read_api.py:318-328`），不处理重复头。`permissions.py:105-107` 要求控制面 token 互异，但 `read_api` 启动不调用；`_reader_tokens()`（`:309-315`）重复值后写覆盖。

**失败场景：** actor **伪造绕过尚未证实**。缺口是缺头/多头时取第一份、合并或默认 `unknown` 的契约不完整，审计对不上。12 位指纹只是审计标签，不能当授权或唯一身份；`role` 不是自然人。`gateway` 与 `snapshot` 配成同一字符串时，两套身份无法区分（只读快照 vs 网关写），隔离被削弱。双层白名单挡不住 **gateway 进程自身失陷**。

**修法：** 写明 actor 是 `require_reader` 成功后的背书，不是密码学独立证明。网关丢弃外来 `X-Watcher-*`（含重复）再注入恰好一条；缺应注入头则不转发（实现 5xx）。watcher 在 `gateway` 身份下：这些头必须恰好一个；缺/多 → 400/401，不合并。指纹哈希**验证后**规范化 token（与 `require_reader` 同一剥离 + `strip()`），仅写入审计字段 `source`/`role`/`token_fingerprint`/`operation`。凭据互异在 **O-0 发行侧**校验：`gateway` ≠ `snapshot` ≠ 浏览器代理；**不要**为交叉校验把 reader 秘密下发 watcher。各进程只持必要秘密（网关持 gateway，快照 reader 持 snapshot，watcher 持三身份中自己的）。

**验收：** T0-1 覆盖缺头、多头，不把「伪造已证实」写进报告。指纹夹具归一 strip。O-0 发行记录证明三身份互异；watcher 环境无 reader token。审计可 grep 12 位标签。秘密哨兵测试**通过**（响应/审计/日志不含 token 与密钥）。

### P1-15：机器可验证单一真源；按身份列 query/body/response

**证据：** §3 是手写 Markdown，「对应 `/api/...`」（设计 `:111`）。媒体外路径 `/m/v1/watcher/media/:filename`（设计 `:116`），现行内路径是 `/media`（`server.js:70`）。GET `SELECT account.*` 再掩码，仍含 `api_key`/`api_secret`（`trading-api.js:344-374`、`:1002-1007`）。PUT 非空即改密钥（`:581-590`、`:620-635`）。

**失败场景：** 另一份手写 Markdown/附录会被当成第二真源，与代码再漂一次。只写 `gateway`、不写 `snapshot`/`browser` 字段集，浏览器改密钥或快照读三表会各写一套。编码斜杠、点段、双解码、尾斜杠、大小写、重复 query、OPTIONS、HEAD 若不做独立负例，规范化路径会绕过表。

**修法：** 单一机器真源（生成路由的 JSON/YAML，禁止再手写一份矩阵当规范）。每行：`method | outer_path | inner_path | identity(gateway|snapshot|browser) | roles | query | body allow/deny | response omit`。`snapshot`/`browser` 同样写全。`gateway`：拒 `api_key`/`api_secret`/`session`；GET 删除秘密列。watcher 第二层对 `gateway` 身份同样拒绝秘密字段，不得只靠网关过滤。`browser`：维持站点可改密钥、GET 可掩码。`snapshot`：只读白名单字段，无密钥。query 与 response 的 secret 白名单保持（禁止密钥入 query，响应 omit 秘密列）。路由由该表生成；另加独立负例，不与生成表互相证明。

**验收：** 生成路由与表 diff 为空。独立负例：百分号编码斜杠与点、双重解码、尾斜杠、大小写、重复 query、OPTIONS、HEAD。PUT 带 `api_key` 经网关 400，DB 不变；watcher 对 `gateway` 身份直连同样 400。`gateway` token 直连遍历矩阵（矩阵外拒绝、计数可证）。`browser`/`snapshot` 交叉拒绝（browser 调 snapshot 路径、snapshot 调网关写路径）。GET 经网关无秘密键（哨兵通过）。

### P1-16：媒体 Range/HEAD、头白名单、截流而非事后 503

**证据：** 设计只写流式、HEAD/Range、20 MB、禁止 token 入 query（设计 `:87`、`:116`、`:147`）。`server.js:70` 无鉴权 static。`app/tradingApi.ts:758-797` 超时在拿到响应头后 `clearTimeout`，再 `response.text()`，超时只到头。

**失败场景：** 头已发出后改 503 无效（HTTP 状态已定），只能截流、清理、记 `truncated`；发头前的错误才 503。multi-range/畸形 Range 未定义会 200 整文件或崩溃。开放区间或后缀 Range 未定义会行为漂移。无总流 deadline、chunk 上限与背压时，慢客户端长期占槽。客户端取消后若不在 `finally` 关上游，槽不释放。用户可控 host + 跟随 redirect 会打到非固定上游。`no-store` 和本地清理不能保证撤回已缓存字节。A-0 若走 JSON `send()`，媒体会当文本读完。媒体与配置写、snapshot 争用同一预算时，采集图会挤占开仓刷新或配置提交。

**修法：**

- Range **仅单段** `bytes`；须支持开放区间与后缀（`bytes=start-`、`bytes=-suffix`）或在契约中明确拒绝。multi 与畸形：明确拒绝（建议 416），禁止当整文件 200。
- HEAD 可忽略 Range，返回整文件元数据，不承诺部分 body；`Content-Length` = 真实全文件大小。
- 每次以整文件 `stat` 或可信 `Content-Range` total 验证 ≤ 20 MB；另列全局/每 token 的带宽与并发预算。重复下载不把多次传输累计成「同一文件大小」。
- 有限总流 deadline、chunk 上限、背压。客户端取消后 `finally` 关闭上游并释放槽。
- 发头前失败 → 503；发头后超限/超时/断开 → 截流、关上游、记 truncated，不改状态码。
- 请求头白名单：`Range`、`If-Range`。响应头白名单：`Content-Range`、`Accept-Ranges`、`Content-Length`、`Content-Type`、`ETag`、`Last-Modified`、`Cache-Control`。`Connection` 列出的 hop-by-hop 头移除。
- 路径 `realpath` 必须落在媒体根内，禁止符号链接逃逸。
- `Cache-Control: private, no-store`，另做本地缓存清理；二者不能保证撤回已缓存字节。
- 上游 host 固定，禁止用户改 host；`follow_redirects=False`。
- 独立媒体预算，不得挤占配置写与 snapshot。
- RN 媒体请求独立携带 bearer，与交易配置同一套更新，**不走** JSON `send()`。

**验收：** HEAD 无 body、长度为全文件。单段 `0-0` → 206 且 total 为真实大小。开放区间/后缀按选定政策（支持或明确 416）。multi/畸形按选定政策。整文件 >20 MB 在发头前 503。发头后截流有 truncated 日志。取消后槽回收。302 不跟随。realpath/symlink 负例。媒体打满时配置写与 snapshot 仍有独立预算。媒体客户端非 `send()`。

### P1-17：结构码区分失败类；错 token 保持 403 可接受

**证据：** §4.2「无 token 401、错 token 403、viewer 写 403」（设计 `:137`）。§2.1 / T0-5 把 401/403 绑成同一「连接失效」（设计 `:88`、`:151`）。`require_reader`：缺头 401，未知 token 403 `forbidden`（`read_api.py:318-328`；`tests/control-plane/api/test_snapshot_and_api.py:211-212`）。`app/tradingApi.ts:510-516`、`:810-824`：仅 401 禁写；403 为泛 `request_failed`；超时 `status:0`。

**失败场景：** 上游 401 若原样返回，且 A-0 **复用**同一 `TradingApi` 实例的 `writeDisabled`，禁写会传到交易请求；**未证明**现网已有跨实例全 app 禁写。viewer 写 403 被当成掉线去轮换密钥。写超时结果未知时若自动换新 `client_ref` 重试，可能双写。非 JSON / body 超时与 JSON 错误未覆盖。

**修法：** 必须有结构码 `invalid_token` / `insufficient_scope` / `watcher_unavailable`。**错 token 保持 HTTP 403 可接受**，只要 body 带 `invalid_token`；不定为必须改 401。若推荐 401，只允许网关适配层映射，**不改**既有 `/v1/accounts` 等 API。上游 401/403/5xx/超时 → 503 `watcher_unavailable`。A-0：401/`invalid_token` 才走连接失效；403/`insufficient_scope` 显示权限不足；503 只在采集页。写超时：结果未知，禁止自动新 `client_ref` 重试。覆盖 JSON 错误、非 JSON、body 超时。

**验收：** `viewer` 写 → 403 `insufficient_scope`，**同一 viewer** 随后读 status 仍成功（不是拿 `risk_admin` 打 operator 200）。已验证的 `risk_admin` 遭上游 401（网关 503）后，再 `dry_run` 仍应被授权。契约含 `code`。禁止 §2.1「401/403 同一提示」。

### P1-18：`baseUrl+/v1`；O-0 依赖 C-1；Caddy 清单不执行

**证据：** 契约旁证不足以证明当前现场 `/m` strip（设计 `:83`；`backend-api.md:122` 无 strip 规则）。`INTEGRATION_REPORT.md:79-85` 为 08-31 逐路径 matcher + `strip_prefix /m` → `127.0.0.1:8183`，不能代替现行 Caddy。app 为 `baseUrl + '/v1/...'`（`app/tradingApi.ts:370`、`:657-665`、`:773`；`app/tradingStorage.ts:5-8`、`:28-31`；夹具 `https://example.invalid/m` 见 `app/tradingScreenWiring.test.tsx:14-16`）。O-0 表依赖缺 C-1（设计 `:274` vs `:283`）。

**失败场景：** 路径常量再写 `/m/v1` 会双重 `/m`。矩阵外可以进 operator-query，由网关拒绝即可，不必要求 Caddy 挡下；**Caddy 不匹配**时才是控制面计数也不变。未列方法在 FastAPI 上常为 405，不能一律写成 404（P2-03）。O-0 只列「核对」会漏：import/顺序、strip 两次、上游不是 8183、移动 `Authorization` 被面板 `system_observer` 注入覆盖、浏览器未清头、`/media` 另有公网入口。

**修法：** A-0 常量只写 `/v1/watcher/...`。O-0 依赖 `W-0, C-0, C-1`。Caddy 按生成表追加路径，禁止 `/m/v1/*` 通配。端口：宿主 9090 → 容器 9100，写入 O-0 说明，不另分级。

**验收（本会话不执行现场项）：** 单测 URL = `baseUrl + /v1/watcher/status`。矩阵外：operator-query 拒绝且 **watcher 计数不变**；仅当 Caddy 不匹配时控制面计数也不变。未列方法按 P2-03 选定的 405 或 404。O-0 清单必须含：有效配置（import、顺序、`handle_path` 或 strip **只一次**）、上游 **8183 当前**核对、移动 `Authorization` 保留且不被 `system_observer` 注入覆盖、浏览器 basicauth 后清头再注入、`/media` 覆盖且无其他公网绕过、双 token 轮换顺序（watcher 先接受新旧 → gateway 切新 → 确认请求/审计 → 撤旧；各进程重启/加载方式与回滚验证）。缺任一身份按设计拒启动，但 operator-query 与交易服务的启动耦合必须在上线前做配置校验，不能因漏 `gateway` env 让交易服务停机。缺 C-1 不得标 O-0 done。

### P1-19：挂起 60s 后 snapshot 影响开仓；禁止全称「交易不受影响」

**证据：** §2.1 / §4.2 / T0-1b 的全称（设计 `:86`、`:139`、`:147`）与 §2.2 `max_age=60s` 后 `open_position` 拒绝（设计 `:97`）冲突。开仓才读 watcher SQLite（`read_api.py:7625-7640`、`:5940-5956`、`:6111-6118`、`:7003-7020`）。

**失败场景：** 把 fail-closed 开仓拒绝当隔离回归失败；或为保开仓放宽 max_age。

**修法：** 网关隔离只覆盖不依赖快照的列名端点。开仓在 60s 过期后必须 `snapshot_unavailable`。禁止「全部交易不受影响」。

**验收：** 挂起 10s 与 61s 分段报告 close vs open。

## 三、C-1 / O-0 / A-0

| 模块 | 方案现状 | 本 review |
|---|---|---|
| C-1 | 网关 + 连接池 + Markdown 矩阵 | 吸收 P1-12..17：async 准入、四角色 scope、actor 缺/多头、机器真源、媒体截流与头白名单、结构码（403+`invalid_token` 可接受） |
| O-0 | 依赖缺 C-1；现场只列 | 依赖加上 C-1；清单见 P1-18（本会话不执行）；发行侧凭据互异（P1-14）；端口 9090/9100 写进说明 |
| A-0 | 复用错误语义 | 路径只拼 `/v1/...`；媒体独立 bearer；写超时不新 `client_ref`；复用 `writeDisabled` 才会把 401 传到交易 |

## 四、D2 裁决

| 决策 | 裁决 | 条件 |
|---|---|---|
| D2 v0.3 网关 + 复用控制面 token + 三身份 + 不开 `/w` | **修改后开工** | 形态同意。P0-02 设计洞关闭、实现未验收。0 新 P0。把 P1-12..19 与 P2-03 写入设计后再派本范围的 C-1 / O-0 / A-0。 |

同意：一把钥匙、无感、三身份、双层默认拒绝、snapshot 与 gateway 分离、同 `risk_admin` 扩写（须有 scope 表）。

必须改：独立连接池不能替代线程隔离、「viewer 及以上」、手写矩阵当真源、媒体缺完整流生命周期和缓存约束、401/403 同一提示、O-0 缺 C-1、交易全称不受影响、actor 当独立证明。错 token 的 HTTP 403 **可保留**（须有 `invalid_token` 结构码）。

## 五、分层结论

### 必修项（P1，不修订不得派 C-1）

| 条目 | 方案错在哪 | 正确定位 |
|---|---|---|
| P1-12 | 独立 HTTP 池不能替代线程隔离 | async 准入；8×worker 总额；慢流要实测交易影响 |
| P1-13 | `viewer` 及以上 | 四角色 scope；同 token 一并获权 |
| P1-14 | actor/指纹/交叉秘密 | 背书；缺/多头；发行侧互异；指纹仅审计标签 |
| P1-15 | 手写 Markdown 当矩阵 | 生成表单一真源；含 snapshot/browser；watcher 第二层拒秘密字段；独立负例 |
| P1-16 | 媒体缺完整流生命周期和缓存约束 | 单段 Range（开放区间/后缀须明示）；发头前后分流；独立媒体预算；no-store 不保证撤回已缓存字节 |
| P1-17 | 401/403 同一 UI | 结构码；403+`invalid_token` 可接受；上游 503 |
| P1-18 | 缺现场路由证据；O-0 缺 C-1 | `baseUrl+/v1`；契约旁证不足；清单含轮换顺序与启动校验 |
| P1-19 | 「交易不受影响」 | 隔离≠开仓 |

### 清理项（P2 × 1）

| 条目 | 证据 | 失败场景 | 建议 |
|---|---|---|---|
| P2-03 默认 405 vs 设计全 404 | 设计 `:109`、`:126` 未列方法/路径拒绝且未注册路由 404。FastAPI 装饰器把 `methods` 设为 GET（`.venv-arch/lib/python3.12/site-packages/fastapi/routing.py:1020-1021`）；Starlette 对方法不匹配返回 405（`starlette/routing.py:275-277`）。`fastapi/routing.py:344-354` 只证明同步 `def` 进线程池，不能证明 405 | 验收把 HEAD 404 当成功，实际是未注册；或把合法 405 改成 404 与框架默认不一致 | 设计显式二选一（未注册路径 404 + 已注册未列方法 405，或网关前置一律 404）。HEAD 必须显式注册 |

### 本审范围结论

**D2：修改后开工。** 不在此对范围外模块做可开工裁决。

## 六、本文件与剩余风险

- **changed files：** 仅 `docs/plans/2026-09-11-watcher-to-app-migration.review-v0.3.md`。未改设计正文、代码、契约；未提交。
- **未核（本会话未访生产、未跑测试）：** 现行 Caddyfile（matcher、strip 次数、上游是否仍为 8183、`/media` 与其它公网入口）；operator-query 运行时 worker 数、线程池/网关槽容量与负载下交易 p99；真机媒体磁盘缓存及 token 轮换后是否仍可读本地字节。O-0 清单不得在本会话执行。
- **实现未验收：** `server.js:70` 仍无鉴权。
- **升格：** 若落地后**实测**拖慢或阻断交易写路径，或复用客户端状态把采集 401 传成禁写，再评估是否升 P0。当前无生产/测试证明，不作绝对升格。
