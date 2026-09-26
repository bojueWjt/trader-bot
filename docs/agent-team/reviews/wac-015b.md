# wac-076 审查报告：wac-015b（WGW-1.0.2 契约 + 生成物切换 + never_allowed 独立拦截）

- 被审：`auto/wac-015b` @ `4ce831f`（从 `auto/wac-049` @ `f747bce` 切出，经 `31af3ac` 合入集成分支）。一并审了 `f747bce`（F-13 契约文字，此前未审）。
- 审查人：watcher-app-crew-reviewer，2026-09-26
- 结论：**PASS（0 🔴，6 🟡，6 💭）**，已合入集成分支，合并提交 **`245de3f`**。
- 验证工具都是我自己写的，放在 scratchpad `r076/`：
  - `probe076.js`：加载真实 `server.js`，经裸 socket 发请求，统计鉴权之后每一层的调用次数；
  - `gw_probe.py`：`create_app("operator-query")` 跑在真实 uvicorn 0.54 上，httptools 与 h11 两种实现都跑，上游用 Mock 并计数；
  - `caddy_probe.py`：本机 Caddy v2.10.2 加载提交的 snippet，上游用桩；
  - `mut076.py`：50 个变异，在沙箱副本里跑。
- 本报告不提交，看板不改，没有 push，没有改 main。

## 1. 边界

| 检查 | 结果 |
|---|---|
| `git diff 8262cb6 4ce831f --stat` | 17 个文件：`contracts/`（backend-api.md、YAML、两份 Caddy 生成物）、`scripts/contracts/` 两个脚本、`scripts/build_immutable_watcher_image.py`（白名单删一行）、watcher 的 `lib/auth.js`、`lib/generated/`（JS 生成物；`watcher-routes.js` 已删）、`scripts/dump-route-table.js`、`__tests__/auth-media.test.js`、控制面 `watcher_gateway.py` 与 Python 生成物、3 个测试文件。全部在任务书范围内 |
| `lib/trading-api.js`（wac-066 的文件） | 未改动。与已合入的 wac-066、wac-078 用 `merge-tree` 预演无冲突，实际合并也无冲突 |
| 铁律 | 没有生产改动、Caddyfile、迁移或记账表改动。凭据只出现在测试夹具中，都是随机或固定的假值（`dump-route-table.js` 用 `"g"*40` 等） |
| 手写 `watcher-routes.js` | 已删除。`git grep` 只剩两处：契约 §9.1 的历史事实行，以及 O-0 G7 的"它必须不存在"断言。仓库里没有第二份清单（never_allowed 只在 `auth.js` 中从 `PAYLOAD` 解构） |

## 2. F-13（`f747bce`）契约文本审查：PASS

| 条目 | 核对方式 | 结果 |
|---|---|---|
| (1) 五种编码形式 | 只挂 `express.static` 的 Express，裸 socket 发送 | `/%2e/index.html`、`/%2E/`、`/%2Findex.html`、`/.%2findex.html`、`/x/%2e%2e/index.html` **全部返回入口文件**（`//index.html`、`/./index.html` 同样），"express.static 先解码再解析"成立。解码结果逐一核对，都不被 `NA` 命中，与正文一致。对实现跑这五个目标 × 2 身份 × 7 方法，全部 404，handler 与 static 计数都为 0 |
| (2) 指令顺序表 | `directives.go:47-84` | 列表与源码一致（`copy_response_headers` 只能出现在 `handle_response` 内，省略是对的） |
| (2) 白名单的 handler 名 | 对 25 个指令逐一 `caddy adapt` | `tracing→tracing`；`map→map`；`vars/fs/root/log_skip/skip_log/log_name→vars`；`log_append→log_append`；`header→headers{response}`；`request_header→headers{request}`；`request_body→request_body`；`redir→static_response`；`method/rewrite/uri/try_files→rewrite`；`basic_auth→authentication`；`forward_auth→reverse_proxy`；`encode→encode`；`push/intercept/templates/invoke` 同名。**与正文逐条相符**。另实测"相邻无匹配器指令合并为同一路由"：`encode`、`request_header`、`header`、`respond` 合并成 4 个 handler 的单条路由。"逐个判定 handler"的要求成立 |
| (3) `strip_prefix` 先 clean | `rewrite.go:48-53`、`:259-268`、`:479-490` | 行号与语义都准确 |
| (3) 转发结果 | 真实 Caddy 实测 | `/m/v1/watcher/x/../status`、`./status`、`//status` 都以 `/v1/watcher/status` 到达桩；`/trading/accounts/a/..` 以 `/v1/watcher/trading/accounts` 到达；`/x/../config` 由兜底返回 404，不转发；`/x/%2e%2e/status` 原样转发；`/media/a#x` 以 `/v1/watcher/media/a%23x` 转发。**全部与更正后的正文一致** |

两处措辞见 💭-1、💭-2，都不影响行为。

## 3. 实跑（全部带 `set -o pipefail`）

| 命令 | 分支 `4ce831f` | 合并后 `245de3f` | 基线 |
|---|---|---|---|
| `check_watcher_gateway_routes.py`（默认） | exit 0，`ROUTES_DIFF_EMPTY rows=63 yaml_sha256=85eb4c88… phase_max=P2` | 同左 | — |
| `check … --self-check` | exit 0，`phase_max=self-check` | 同左 | — |
| `npm --prefix bridge/services/telegram-watcher test` | 136 pass / 0 fail / 0 skip | 145 pass（136 加上 wac-066/078 的 9 个） | 133 |
| `pytest tests/control-plane` | 875 passed + 5 failed | 875 passed + 5 failed | 868 + 5 |
| `pytest tests/deployment`（全量） | 732 passed / 45 failed / 2 skipped，失败集合与基线 `dep-merged2.failed` 逐行相同 | —（-k watcher 已跑，见下行） | 732/45/2 |
| `pytest tests/deployment -k watcher` | 38 passed | 38 passed | 38 |
| `pytest tests/bridge tests/ingress` | 44 passed | 44 passed | 44 |

补充说明：
- 控制面的 5 个失败都是预存的 `test_frozen_stage_verification_*`（`AttributeError: verify_frozen_maintenance_fence`）。新增 7 个测试。执行者自报 874，我两次实跑都是 875，数字差异见 💭-6。
- 生成物重新生成：在 `git archive` 副本里跑 `gen --phase-max P2`，四份文件 `shasum -c` 全部 OK，也就是逐字节相同。四份文件的 `yaml_sha256` 都是 `85eb4c88…`，`phase_max` 都是 P2；两份代码生成物的 `PAYLOAD_SHA256` 都是 `cbcfc68e…`。
- payload：59 行，其中 gateway 20 行，phase 只有 P0/P1/P2，**P3 为 0**；`never_allowed` 6 项。两份 Caddy 生成物不含 price-alerts 和 price-monitor。
- 镜像：O-0 G4 `CLOSURE_OK closure=16 whitelist=36`。G12 在打包脚本里因为 Caddy render 中止而没有执行到，我单独执行 G12 片段，结果 `RUNTIME_MANIFEST_OK files=36`。

## 4. 真实栈验证

**watcher（`probe076.js`，真实 `server.js` app，裸 socket 原文请求行）**
- **固定探针 574/574**：
  - 对象：§9.14.3 watcher 组的 13 个 403 与 4 个 403/404 对照、F-13 的五种编码形式，外加 §9.14.5 与 NA 固定探针（`/api/login/anything`、`/api/login%2Fstart`、`/API/CONFIG`、`/api/%ZZ`、`/%`、`/%69ndex.html` 等），共 41 个目标 × gateway/snapshot × 7 种方法；
  - 状态码与结构码逐一符合；鉴权之后任何一层（validation、static、media、所有路由）都**没有被调用**；404 与 403 都不带 `Allow`。
- **模糊对照 25,700 次请求**：
  - 5,075 个目标（后缀、前缀、点段、编码形式、逐字节插入 0x00–0xFF）× 两种身份；
  - 与我按契约独立写的参考模型（第 5–7 步）对比：状态不一致 0；参考判拒但请求到达后续层 0；凭据面 handler 或入口文件被送达 0；token 泄漏 0。
- **反例对照**：同一探针对 `31af3ac`（改动前）跑，得到 FIXED 217/574、不一致 3,680、错误到达 15，说明探针能抓到问题。
- **R17**：
  - browser `GET /`、`/index.html` 200 并返回入口 HTML；`/api/login/status` 200；`/healthz` 到达 handler（503，因为未连接 Telegram）；
  - gateway `/api/status`、`/api/trading/accounts`、`HEAD /media/…` 与 snapshot `/api/trading/config-snapshot` 正常到达 handler。

**网关（`gw_probe.py`，真实 uvicorn 0.54，httptools 与 h11 两种实现都跑，1,263 项检查全部通过）**
- **NA_gw 组**：28 个目标 × 7 种方法 × 3 种认证状态（无、viewer、risk_admin）。目标包括 `config`、`login*`、`/v1/watcher/`、`index.html`、`healthz`、`#x` 与 `http://x/…` 形式、`%2F`、`%63onfig`、点段与 `//`。结果全部为 404 `route_not_found`（或 FastAPI 默认 404），**不带 `Allow`，上游请求 0**。
- **httptools 下的片段**：`/v1/watcher/status#x` 在 httptools 下到达合法路由并返回 200。这是契约 §9.4 明文接受的行为：httptools 在 ASGI 之前剥掉片段，`raw_path` 不含 `#`。h11 下同一请求为 404。
- **正常路径**：GET status 200，上游 1 次；DELETE status 405，`Allow: GET`。
- **停用态**：六种注入，分别是 (a) 合成相交行、(b) `never_allowed` 为空、(c) `methods` 改成列表、(d) 导入失败，另加 budget 越界、路径参数正则无法编译。每种注入下：
  - 7 类请求都得到 503 `gateway_disabled`，键恰为 `code/message/request_id`，上游 0。7 类包括带认证与不带认证、大小写变体、`%2F`、`#`、无尾部的前缀；
  - `/v1/accounts/`、`/v1/accounts`、`/healthz`、`/v1/nope` 的状态码、响应头（去掉 Date）和响应体与未停用时**逐字节相同**；
  - 恢复后 GET status 回到 200。
- **日志**：只有 `ValueError never_allowed_empty` 这类"异常类型 + 检查名"的行，没有 token。
- **真实导入**：在副本中把生成物改成摘要不符、或删掉生成物，然后真实 `import read_api`，再调 `create_app("operator-query")`。两种情况下 `create_app` 都正常返回，`/v1/watcher/*` 返回 503，`/v1/accounts/` 行为不变。

**Caddy v2.10.2（`caddy_probe.py`，直接 import 提交的 `caddy-watcher-gateway.caddy`，208 项检查全部通过）**
- **adapt 结构**：adapt 后抽出的 16 组 `(pattern, methods)` 与清单 v2 逐行相等。兜底模式恰为 `^(?i:/m/v1/watcher)(?:[/\n]|$)`（JSON 中是反斜杠加 `n`），而且是最后一个匹配器。片段不含 `*`。
- **逐行探针**：每一行 × 7 种方法。
  - 表内方法以剥掉 `/m` 的路径到达桩；
  - 表外方法、全大写、尾部加 `/`、参数为空或为 `x/y`、字面行加 `/x`，都由兜底返回 404，空 body；
  - F-10 换行结尾：字面行加 `%0A` 由兜底返回 404，参数行加 `%0A` 原样转发。
- **兜底固定探针**：以下 11 个都得到兜底 404：`/m/v1/watcher`、`/`、`/M/V1/WATCHER/login/start`、`/m/v1/watcher%0A`、`/M/V1/WATCHER%0A`、`status%0A`、`config`、`healthz`、`index.html` 等。`/m/v1/watcherx`、`/m/v1/other`、`watcherx%0A`、`watcher%0D`、`watcher#x` 都落到站点的 CATCHALL。与契约一致。
- **F-13 (3) 两类用例**：同第 2 节，全部一致。

## 5. 变异（50 个，沙箱副本；suite = 执行者提交的测试，probe = 我的真实栈探针）

**被抓住的 36 个**

| 组 | 变异 | 抓住者 |
|---|---|---|
| watcher | W01 NA 放到规范化之后；W02 5b 漏掉 `\`；W04 解码失败不算命中；W05 snapshot 不做 NA；W06 browser 也做 NA；W07 path_part 只截 `?`；W08 删 5a；W09 删 `#` 规则；W11 NA 去掉 `t+"/"`；W12 `/*` 不覆盖基础路径；W13 NA 区分大小写；W17 5c 不判解码结果；W23 static 挂在鉴权之前 | suite 与 probe 都抓住 |
| watcher | W14 `createAuthMiddleware` 不断言 source；W15 加载期不查 gateway 相交；W16 never_allowed 用副本而不是 PAYLOAD 对象；W21 新增字面 `/api/rogue` handler | suite |
| watcher | W10 删掉第 6 步的 `%` 规则（F-13 所说的承重规则） | **只有 probe 抓住**，见 🟡-3 |
| 网关 | G01 NA_gw 返回 403；G02 NA_gw 带 `Allow`；G05 停用态放行；G06 停用态只拦带认证头的请求（未认证请求放行，也就是"停用态仍认证"）；G07 加载时不校验；G08 不查 `methods == "*"`；G09 不查相交；G10 删 raw `#` 规则；G13 path_part 只截 `?`；G14 加载异常外抛 | suite 与 probe |
| 网关 | G12 触发判定用整串而不是 path_part；G15 删掉 budget 与正则的构建期检查 | 只有 probe |
| 生成器与校验 | C01 payload 不带 never_allowed；C02 兜底改回 `(?:/|$)`；C04 片段丢掉 method 行（这三个在"改 lib 并同步重新生成"的场景下，校验脚本的独立断言也能抓住）；C03 行级探针改用 `re.search`（校验对正确生成物报错）；C05 = C02 加删除兜底逐字比较；C06 = C01 加删除 never_allowed 显式断言 | suite（C05、C06 在同步重新生成后分别由 `test_caddy…` 的兜底字符串断言、watcher 加载期断言兜住） |

**存活的 14 个**
- **等价变异（有更深一层防护，行为不变，4 个）**：
  - W18：重新加回 `/` 特例。第 7 步对 `/` 给 403，因为 `/` 只属于 browser；
  - G03：删掉 NA_gw。路由匹配本来不含凭据面，结果仍是 404；
  - G04：删掉 G8。加载期断言保证不可达；
  - G11：删掉"raw path 必须以 `/` 开头"。NA_gw 或路由匹配仍给 404。
- **测试缺口（10 个）**：
  - W03：5b 删掉非 ASCII 分支，见 🟡-2；
  - W19：watcher 加载期接受空 never_allowed，见 🟡-4；
  - W20：`dump-route-table` 不比较 handler，见 🟡-1；
  - W22：新增 `app.post("/api/:rogue")` 不被发现，见 🟡-1；
  - G16：停用日志泄漏整个环境变量表，见 🟡-5；
  - S21、S22、S23、S11、S10：删掉对应的自洽检查后，被篡改的 YAML 能通过生成与校验，见 🟡-6。**对照组**：不删检查时，五种篡改分别被 `S-21 never_allowed path`、`S-22 caddy_external_prefix`、`S-23 source scope`、`S-11 gateway_enum subset`、`S-10 gw.trading_messages.get` 拒绝。实现本身正确，只是没有测试锁定。

## 6. 分级意见

### 🔴 无

### 🟡

**🟡-1 `scripts/dump-route-table.js:36-48` 的 handler 比对偏宽，不能完整证明"不存在 payload 之外的 `/api/*`、`/media/*` handler"（§9.14.4 第 3 项）。**
- `sameShape` 把任何以 `:` 开头的 Express 段当成能匹配 payload 中任意字面段。变异 W22 加了 `app.post("/api/:rogue")`，因为 payload 里有 `POST /api/config` 这类两段行，它不会被判为 unexpected。
- static 与 media 路径写死为 `["/", "/index.html"]`、`["/media/{filename}"]`，没有枚举 `app.use("/api/x", fn)` 这类中间件层。
- 测试只断言 `missing`、`unexpected` 为空，没有注入一个 rogue handler 去证明比对真的会失败。W20 把比对整个置空，测试照样通过。

安全上有第二层：鉴权中间件先查身份路由表，表外路径到不了 handler，W22 的 handler 实际不可达。但这项检查本身是契约规定的 diff 判定。

建议：
- `:x` 只与 `{x}` 相容，字面段只与同名字面段相容；
- 枚举 `app.router.stack` 中带路径的非路由层；
- 测试中挂一个 rogue 字面 handler 和一个 rogue 参数 handler，断言脚本以退出码 1 失败。

**🟡-2 5b 的非 ASCII 与控制字符分支没有函数级单元断言**（`auth.js:225` 的 `[^\x21-\x7E]`；wac-058 清单第 6 项、💭-3 明确要求）。
- 变异 W03 删掉这个分支后，suite 和我的探针都没抓到。原因是 Node 的 llhttp 在 Express 之前就对这类字节返回 400，所以经 socket 测不到。
- 建议：用伪造的 `req`（`originalUrl` 含 `\u00a0`、`\u3000`、`é`、`\x7f`）直接调中间件，断言 403 `identity_forbidden`，并断言 `next` 没有被调用。

**🟡-3 第 6 步 `%` 规则的承重作用在实现层没有被测试锁定。**
- 变异 W10 删掉 `pathname.includes("%")` 后，suite 仍然全绿：五种编码形式照样得到 404，因为第 7 步身份路由表里没有这些路径。
- 我的模糊对照能抓住它，但只是间接的：`/api/trading/accounts/acct-1%23x` 从 404 变成 403。
- F-13 正文说这五个目标"专门锁定 `%` 规则"，这在"只有第 5 步"的参考模型下成立，在真实中间件里不成立（见 💭-2）。
- 建议：把第 6 步抽成可单测的纯函数（输入 `target`、`pp`，输出是否 404），直接断言这五个目标命中 `%` 规则；或者在测试里注入一张包含 `/index.html` 的 gateway 表，证明 404 来自第 6 步而不是第 7 步。

**🟡-4 watcher 加载期断言只测了"gateway 相交"一种失败。**
- `auth.js:150-174` 的其余分支都没有负例：空列表、形状不合、`methods` 不是 `*`、路径不合 S-21、重复、与 browser 行不相交。变异 W19 存活。
- 网关一侧四种都测了（F-11），两边不对称。
- 建议：对 PAYLOAD 的副本逐一篡改，断言 `assertPayload` 抛错。

**🟡-5 停用态测试中的"日志不含夹具 token"断言恒为真**（`tests/control-plane/api/test_watcher_gateway.py`，`test_artifact_disable_four_sources_preserves_existing_routes` 的最后几行）。
- 这个测试没有用 `setup` fixture，环境变量里没有 `FAKE` token，探测请求也不带 `Authorization`。所以即使日志把整个环境或请求头都写出来，断言也不会失败。变异 G16 让停用日志输出 `os.environ`，测试照样通过。
- 实现本身没问题：我的 `gw_probe` 在环境里设了真实形状的假 token，并发送带 `Authorization` 的请求，日志只有"类型 + 检查名"。
- 建议：先 `monkeypatch.setenv` 四个角色 token，探测请求带上 `Authorization`，再断言日志不含这些 token。"断言写了但不可能失败"是我们反复踩过的坑（参见"无法比较≠相等"）。

**🟡-6 S-10、S-11、S-21、S-22、S-23 没有篡改测试。**
- 这五项新增或修订的检查都能正确拒绝对应的篡改（见第 5 节对照组），但 suite 里没有一条测试会在删掉它们时变红（五个变异全部存活）。
- 建议：在 `tests/control-plane/test_caddy_watcher_gateway_paths.py` 的 `isolated_root` 模式下，对 YAML 做五种最小篡改，断言 `check` 退出码为 1，且输出中含对应的 `S-xx`。篡改内容可以直接用 `mut076.py` 里的 `TAMPER`。

### 💭

- **💭-1（F-13 (2) 文字）**：白名单中 `headers` 一条括号里写"或 `header` 的请求头操作"。实测 `header` 指令只生成 `response` 键，请求头操作只来自 `request_header`（`header >X` 也是 `response`）。判定规则"带 `request` 键判失败"本身正确，只是括号里的来源说明不准。
- **💭-2（F-13 (1) 文字）**："所以它们专门锁定 `%` 规则的承重作用"只在没有第 7 步的模型下成立（见 🟡-3）。下次勘误可以改成"锁定第 6 步与第 7 步至少一层拒绝；要锁定 `%` 规则本身，需要对第 6 步做函数级断言"。
- **💭-3（`auth.js:223`）**：browser 身份的路径计算从 `split("?")[0]` 改成了 `pathPart`，也会截掉 `#`。这使鉴权看到的路径与 Express 实际路由的路径一致，是改进。站点从不发送片段，实际行为没有变化。
- **💭-4（`check_watcher_gateway_routes.py`，`_independent_caddy` 中的 `bad` 列表）**：兜底负例用的是 `/m/other`，契约 §9.14.4 第 1 项写的是 `/m/v1/other`。两者对当前正则的结论相同，建议与契约逐字对齐。
- **💭-5（`watcher_gateway.py`，`load_route_artifact` 的检查名集合）**：路径参数正则无法编译（`re.error`）和 `KeyError` 在日志中都记为 `artifact_load`，没有具体检查名（F11-8 要求记检查名）。建议给 `_match_template` 与 `compile_path` 的失败单独起名，例如 `route_regex`。
- **💭-6**：执行者自报控制面 874 passed，我两次实跑都是 875。差异不影响结论，只提醒以实跑为准。

### 值得表扬

- `auth.js` 第 5 步严格按 F-09 三段式实现，放在规范化之前，对 gateway 和 snapshot 一视同仁。在 25,700 次模糊对照中，与我的独立参考模型 0 处不一致。改动前的版本在同一探针下有 3,680 处不一致，这次改动的价值可以直接量化。
- 网关停用态做成了"可重入加载入口 + 统一标志 + 模块内捕获"：
  - 六种注入（多于契约要求的四种）都得到 503，既有端点逐字节不变；
  - 真实地删掉或篡改生成物后，`create_app` 照常返回。
  - R11 从一句话变成了可以测试的行为。
- 校验脚本的 Caddy 独立探针严格只用 `fullmatch`，兜底正则先逐字比较、再跑探针，never_allowed 做了显式深相等。C01–C04 在"改 lib 并同步重新生成"的场景下都能被抓住。这正是 §9.14.4 "不依赖同源重新生成"要达到的效果。
- `EXPECTED_P2_LINES` 保持为与 YAML 无关的独立预言，并已按 v2 格式重写。

## 7. O-0（wac-060）需适配的清单（不属于本任务，不作为 FAIL 理由；本任务范围内的测试均未受影响）

实跑结果：
- `scripts/ops/o0/tests/run_all.sh` 在 `o0_caddy_watcher_routes.py selftest` 处以退出码 1 中止，报错 `list contains {param}; regenerate with the contract generator`；
- 其后各项逐个单独跑：`o0_watcher_config_baseline selftest`、`site_check_leak_test`、`fleet_guard_test`、`auth_gate_test` 都通过；`apply_rollback_test` 因同一原因失败；五个脚本的 plan 模式都返回 0；
- `o0_package.sh --report-only --run-tests`：G1–G11 全部 PASS（G9 pytest 30 passed，watcher 136），随后在 render 步骤（`o0_package.sh:232`）以同一报错中止，G12 没有执行到（我单独执行 G12，结果 OK）。

需要适配的项：
1. `o0_caddy_watcher_routes.py:91-112` 的 `load_list`：按 v2 格式解析 `<template> <regex> <METHOD…>`，比对时使用清单里的 regex 列，不再从 `*` 推导（`:77`、`:82-88`）；头部增加 `_format` 校验。
2. `render`（`:116-`）应当退役：Caddyfile 直接 import 提交的 `contracts/generated/caddy-watcher-gateway.caddy`，并手写 `(watcher_gateway_upstream)`。现有 render 的注释"Do NOT add any /m/* or /m/v1/* fallback"和"do not add (?i)"**与 WGW-1.0.2 的 F-10 兜底相反**，必须删除。
3. `verify`：
   - 对称差只比较以 `^/m/v1/watcher/` 开头的 `(pattern, methods)`；
   - 兜底必须存在、与片段逐字相同，并且排在最后；
   - 遮蔽检查按 F-13 实现两步判定：先判命中，再判 handler 类型白名单（`encode`、不带 `request` 的 `headers`、`vars`、`map`、`log_append`、`tracing`），并且路由不得带 `terminal` 或 `group`；
   - 现有 `:453-456`、`:507-509` 关于 `*` 路径匹配器的判断需要按 v2 重写。
4. selftest 夹具（`:629-` 起，以及 `:707-729` 的变体）改为 v2 格式和片段结构。
5. `o0_package.sh`：
   - G3（`:97-114`）目前只读三份生成物的前三行头部，需要加入片段与 `_format`；
   - bundle 需要带上 `caddy-watcher-gateway.caddy`；
   - `:229-233` 的 render 步骤改为拷贝片段，以免 `set -e` 在 G12 之前中止。
6. `o0_deploy_caddy.sh:90-92`、`:130-132`、`:143-144` 的三处 `verify --paths` 调用，随第 1、3 项一起调整。
7. `tests/apply_rollback_test.sh` 随第 1 项恢复。
8. runbook `docs/agent-team/release/o0-runbook-deploy.md:52` 写的是"在现有 `/m` 移动端 handle 旁边粘贴 render 输出"，与 F-12 冲突。应改为：全局位置 import 片段文件；站点块顶层在所有 `handle`、`handle_path`、`route` 之前 `import watcher_gateway_routes`。另外按 F-13 (2) 人工记录全局 `order` 选项和顶层前置指令。
9. 本机 Caddy 探针：使用生产 Caddyfile 副本，包含 `%0A` 结尾用例和 F-13 (3) 的两类用例。字面点段与 `//` **不按 404 判定**。可以参考本次的 `r076/caddy_probe.py`。

## 8. 处置

- **合并**：在 `.worktrees/wac-integ` 执行 `git merge --no-ff auto/wac-015b`，得到合并提交 `245de3f`，没有冲突。wac-066 与 wac-078 已先合入，两者文件不相交。
- **合并后复跑**（数字见第 3 节）：生成物校验两种模式都通过；watcher 145/145；控制面 875 passed + 5 预存失败；deployment -k watcher 38；bridge+ingress 44。三个真实栈探针（watcher、网关、Caddy）在合并树上重跑也全部通过。
- **清理**：
  - node_modules 符号链接：wac-integ 中临时建的与 wac-015b 中原有的都已删除，只删了链接本身；
  - `.worktrees/wac-015b` 与分支 `auto/wac-015b` 已删除；
  - 确认 `auto/wac-049` 已是集成分支的祖先（`merge-base --is-ancestor`）后，删除了 `.worktrees/wac-049` 与分支 `auto/wac-049`；
  - `git branch -d` 以当前检出分支为参照判断是否已合并，所以两个分支都在确认包含于集成分支之后用 `-D` 删除。
- **未动**：wac-integ 中 Planner 的工作区（合并前是干净的）；看板；main；远端。
- **建议**：🟡-1 至 🟡-6 开一个小修任务，只补测试，外加 `dump-route-table` 的比对收紧；O-0 的第 7 节清单交给 wac-060。
