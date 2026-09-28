# O-0 分阶段部署 runbook（wac-032 草案，wac-045 修订，wac-060 适配 WGW-1.0.2，wac-090、wac-092、wac-094 收紧 Caddy 核对）

> 草案。**没有执行任何生产动作**。每个带 O0-Axx 的步骤都要用户逐项授权后才执行（`o0-authorization-list.md`）；脚本阶段与授权号一一绑定，号不对脚本直接拒绝。
> 范围：计划 §4.1 第 1 步"新增并验证"落到 jp-24：watcher 镜像（W-0）、控制面 operator-query（C-0 reader 开关保持关 + C-1 网关）、Caddy（浏览器入口清头注入 + 契约 WGW-1.0.2 的片段 `caddy-watcher-gateway.caddy`：16 条逐路径锚定路由 + 兜底 404）。**app 不需要部署**（A-0 只在 app 集成分支，真机联调另行授权 O0-A10）。快照开关在本 runbook 全程保持关闭；打开开关见 `o0-runbook-snapshot-switch.md`。
> 不做：不重启 node-control、event-ingest；不写控制面库与记账五表；不新增控制面迁移；不发 RESUME；不 reload Caddy；不下单、不平仓、不撤单。
> 下文 `$S` = `/srv/trader-staging/o0-<UTC>`，`$T` = `$S/bundle/tools`（工具取自候选提交，门禁 G11）。执行者把每条命令的完整输出存为 `… 2>&1 | tee -a $S/evidence/<阶段>.log`。

## 0. 阶段顺序、门禁机制与舰队守卫

| 顺序 | 阶段 | 为什么排在这里 | 中间态是否安全 |
|---|---|---|---|
| 1 | **C：Caddy** | W-0a 上线后，浏览器请求必须带 Caddy 注入的 `X-Watcher-Proxy-Auth`，否则站点全 401。先让 Caddy 注入，旧 watcher 会忽略这个头 | 安全：旧 watcher 忽略注入头；16 条表内路径暂时落到旧 operator-query（没有 `/v1/watcher/*` 路由，得到 404），其余 `/m/v1/watcher` 前缀路径由片段兜底直接 404 |
| 2 | **W：watcher** | 新 watcher 需要三个当前值才能启动；浏览器身份此时已由 Caddy 提供 | 安全：旧 operator-query 读副本文件，不经 HTTP 访问 watcher；feeder 直读库不受影响 |
| 3 | **O：operator-query** | 网关要打到已经鉴权的新 watcher | 安全：快照开关关闭，交易路径不引入快照依赖 |

Caddy 只重启一次（阶段 C），operator-query 只重启一次（阶段 O），watcher 只重建一次（阶段 W）。

**门禁是机读的，不靠自觉**（审查 wac-032 🔴-3）：每个阶段的 preflight（watcher 还有 build）开头删除旧门禁文件，只有全部检查通过才在最后写 `evidence/<阶段>.gate.json`，内容绑定候选提交、`RELEASE.json` 与 `SHA256SUMS` 的摘要、以及该阶段输入文件的摘要（候选 Caddyfile、线上文件、凭据片段、候选镜像 ID）。apply 的**第一步**用 `o0_tool.py gate-check` 核对：文件存在、阶段对、候选与 bundle 一致、输入摘要一致、足够新（preflight 1 小时，build 24 小时）；再重跑便宜的门禁（`SHA256SUMS`、`deploy_candidate`、线上文件等于基线、凭据检查）。任何一项不满足，在任何写入之前退出。watcher 的候选镜像必须在停任何服务之前已存在，且 ID 等于 build 门禁记录的 ID。

**舰队守卫**（计划 §4.2；审查 wac-032 🔴-1）：
- 每次采样 = `docs/agent-operations.md` §0 的查询（`node_id status release_id hb_age`）加四个 `/ready` 状态码。
- apply 的第一批步骤里重新记录基线（preflight 的样本只用于门禁）；基线必须**恰好**是 `O0_FLEET_NODES` 这组节点、每个心跳年龄 < 5 秒（§0："心跳冻结 = 节点死了而不是'没变化'"），否则拒绝开始（退出码 2）。
- 变更之后先等稳定窗（默认 60 秒），再采样 4 次、每 20 秒一次；每一次都要与基线在状态、release、`/ready` 上一致，且心跳仍新鲜（冻结或年龄跃升都算变化）。0 行、缺节点、多节点、无法解析都判"无法比较"（退出码 2），不当作"没变化"。有变化：退出码 3，停下，保持现场，报用户；**不回滚**（回滚会再重启一次），**不 RESUME**（O0-A06 只属于用户）。
- 回滚阶段只记录回滚前样本、不以它为门槛（回滚不能被舰队状态挡住），回滚后照常比较。

**守卫阈值与生产节点心跳参数**（审查 wac-032-r2 🟡-7）：阈值的依据是节点的心跳间隔与 fail-closed 判定时长。仓库 `67b401a` 里间隔是代码常量 `DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 2.0`（`services/nautilus-node/runtime/control_plane_session.py:14`，`app/node.py` 不覆盖），判定时长是每个节点配置的 `control_plane.heartbeat_timeout_seconds`（示例为 15）。**生产值以 S-00 为准**：S-00 打印每个节点容器的 `heartbeat_timeout_seconds`、运行中镜像的 `default_heartbeat_interval_seconds`、`node_py_overrides_interval`，以及约 10 秒内实测的 `observed_max_hb_age`（它的最大值近似真实间隔）。

| 参数（环境变量） | 默认 | 规则（`o0_tool.py fleet-params`） |
|---|---|---|
| `O0_NODE_HB_INTERVAL_S` | 2 | 取 S-00 的 `default_heartbeat_interval_seconds`；`node_py_overrides_interval=True` 或实测最大年龄明显高于它时，先报用户，不自行取值 |
| `O0_NODE_HB_TIMEOUT_S` | 15 | 取 S-00 各节点 `heartbeat_timeout_seconds` 的**最大值**（四个节点不一致也要报用户） |
| `O0_FLEET_MAX_HB_AGE`、`O0_FLEET_MAX_HB_JUMP` | 5、5 | 在 [2, 3] × 间隔之内，目标 2.5 × 间隔（更紧：迟到的心跳被当成冻结；更松：死节点发现得晚） |
| `O0_FLEET_SETTLE_S` | 60 | ≥ 判定时长 + 3 × 间隔（HALT 在通道恢复后的第一次心跳写回，提前采样会漏掉） |
| `O0_FLEET_SAMPLES`、`O0_FLEET_INTERVAL_S` | 4、20 | 不变 |

- **每一次 `--execute`**（三个部署脚本和 `o0_fleet_guard.sh`）在第一个步骤之前都跑这项检查：不一致就以 `FLEET_PARAMS_INCONSISTENT` 退出，并给出建议值，什么都不做。实际使用的参数写进 `evidence/authorizations.log` 的 `fleet_params=` 字段。
- **S-00 与默认值一致**：什么都不用设。
- **S-00 与默认值不一致**：(1) 不开始任何阶段，把 S-00 原文和按上表算出的整组值（两个节点参数 + 三个阈值）交 Planner 转用户；(2) **用户明确确认这组值**之后，才在执行会话里 `export` 这五个变量，确认记录写在看板备注，并与 `authorizations.log` 的 `fleet_params=` 对照；(3) 之后每个阶段都用同一组值，不在中途修改。执行者不能自行放宽阈值去"让守卫通过"。检查只拒绝不一致的组合，不替用户决定取值。

**已声明的风险**：`systemctl restart caddy` 会瞬断节点通道（S-06 确认地址，预期 `172.30.1.1:8080`）。2026-08-30 这导致全舰队 fail-closed HALT，2026-08-31 没有。阶段 C 可能让 ACTIVE 节点变 HALTED，这不是"意外 HALT"，而是需要用户事先接受的已知后果（D-02，待确认）。

## 1. 开始前必须全部满足

| 门禁 | 证据 | 不满足 |
|---|---|---|
| 本地打包门禁 G1–G12 全部 PASS，`RELEASE.json` 的 `deploy_candidate: true` | `o0_package.sh` 输出与 `logs/` | 不申请任何生产授权。当前状态见 `o0-requirements.md` §6.1（wac-060 起 G7 已通过、打包不再在 G12 前中止） |
| 现场只读核对（`o0-site-checklist.md`）无阻断项，§二的参数已按现场确认 | L-A5 汇总页 | 同上 |
| 用户已确认 D-02（Caddy HALT 风险与窗口）、D-04（共享代码目录） | 看板备注 | 同上 |
| 选定低流量窗口：避开信号密集时段；用户在场、手机能收 Telegram 告警 | 用户确认 | 顺延 |

## 2. 准备

| 步 | 授权 | 命令 | 验证 | 回滚 |
|---|---|---|---|---|
| 2.1 本地打包 | 不需要（本机） | `bash scripts/ops/o0/o0_package.sh --candidate <已审定提交> --out <本机目录> --run-tests` | 末行 `PACKAGE_OK`；记下 `o0-bundle-<sha12>.tgz.sha256` | — |
| 2.2 建 staging 并上传 | O0-A02 | `ssh … 'install -d -m 0700 /srv/trader-staging/o0-<UTC>'`；`scp -P 53222 o0-bundle-*.tgz root@100.89.58.40:/srv/trader-staging/o0-<UTC>/`；远端 `sha256sum` 与本地一致后 `tar -xzf` | `cd bundle && sha256sum -c --quiet SHA256SUMS` 退出 0 | 删除该 staging 目录 |
| 2.3 生成凭据 | O0-A03 | 凭据 runbook I-1、I-2（`--catalog-env` 按 S-10/S-11 列出的每个控制面 env 文件各写一次） | `CREDENTIAL_CHECK_OK`，`cross-service distinct … N catalog values`（N > 0） | 删除 `creds/set-initial` |

脚本阶段共同的附加参数：`--stage-dir $S`；控制面 token 目录每个文件一个 `--catalog-env <文件>`；舰队节点集合用环境变量 `O0_FLEET_NODES="<S-00 的节点 id>"`。

## 3. 阶段 C：Caddy（授权 O0-A05）

### 3.1 准备候选 Caddyfile（人工，在 staging 里完成，不碰 `/etc/caddy`）

依据：契约 `contracts/backend-api.md` §9.14.3"Caddy 片段"与"O-0 并入规则（规范性）"，§9.16 F-04、F-10、F-12、F-13。**没有 render 步骤**（wac-060 退役）：Caddyfile 直接 import 已提交的片段文件 `bundle/caddy/caddy-watcher-gateway.caddy`（与 `contracts/generated/` 中的文件逐字节相同，打包门禁 G3 用独立实现从清单重新推导并逐字节比对）。片段定义 snippet `watcher_gateway_routes`：16 个外部路径各一组锚定、区分大小写的 `path_regexp`（`{param}` = `[^/]+`，恰好一个非空段，用户裁决 2026-09-26）+ `method`，最后是兜底 `^(?i:/m/v1/watcher)(?:[/\n]|$)` → `respond 404`。

1. `cp -p /etc/caddy/Caddyfile $S/caddy/Caddyfile.candidate`。preflight 会把 bundle 的片段放到 `$S/caddy/caddy-watcher-gateway.caddy`（与候选同目录），所以候选里的相对 import 在 staging 与 `/etc/caddy` 两处都解析到同一份片段。
2. 在候选的**全局位置**（任何站点块之外、全局选项块之后）加一行 `import caddy-watcher-gateway.caddy`（相对路径，Caddy 按 Caddyfile 所在目录解析；apply 把片段装成 `/etc/caddy/caddy-watcher-gateway.caddy`），并手写上游 snippet，恰好一次 strip、不注入任何头、不含其他匹配逻辑：

   ```caddyfile
   (watcher_gateway_upstream) {
   	uri strip_prefix /m
   	reverse_proxy 127.0.0.1:8183
   }
   ```

3. 在 jp-bot 站点块的**顶层**、该站点块里**所有** `handle`、`handle_path`、`route` **之前**写 `import watcher_gateway_routes`（F-12）。不得放进 `handle_path /m*`、`handle`、`route { }` 之类的块；不得写两次；站点里不得有其他处理器把 `/m/v1/watcher` 前缀（任意大小写，含 `/m/v1/watcherx` 这类紧跟其他字符的路径）转发到 operator-query 或 watcher（例如 `handle /m/* { reverse_proxy 127.0.0.1:8183 }` 不论写在 import 前后都判失败）。**不要**自己改片段、不要贴 `{param}`、不要加 `(?i)`、不要删兜底。
4. **人工记录 F-13 (2)**（import 位置管不到的两类指令）：preflight 的 `caddyfile-check` 会打印两类 `RECORD` 行，执行者逐条补全后存为 `$S/evidence/caddy-f13-record.txt`，交用户与 Reviewer 审阅：
   - 全局块里的每一条 `order` 选项：它把哪条指令移到了 `handle` 之前？
   - jp-bot 站点块顶层每一条默认排在 `handle` 之前的指令（`tracing`、`map`、`vars`、`fs`、`root`、`log_append`、`skip_log`/`log_skip`、`log_name`、`header`、`request_body`、`redir`、`method`、`rewrite`、`uri`、`try_files`、`basicauth`/`basic_auth`、`forward_auth`、`request_header`、`encode`、`push`、`intercept`、`templates`、`invoke`）：指令名、匹配器、它是否可能命中 `/m/v1/watcher` 前缀（任意大小写）、为什么不构成遮蔽。
   机器兜底在 adapt 后的 JSON 上做（下一条的 verify），它已经反映了全局 `order` 与指令排序的结果；人工记录是契约额外要求的第二道。
   **审阅要点（U-6，wac-090）**：逐条看记录行里的匹配器，凡含 `?`、`[`、`]`、`\`、`{`、`}`、`%` 之一，一律先当作"可能命中前缀"处理，写明理由：
   - `?`、`[...]`、`\` 是 Caddy `path` 匹配器的 glob（Go `path.Match`）：`/m/v1/w?tcher/*`、`/m/v1/[w]atcher/dialogs` 都命中表内路径（匹配不区分大小写）。例外：只有一个 `*` 且在开头或结尾、或者恰好首尾各一个 `*` 时，Caddy 走快速前缀/后缀/子串比较，其中的 `?`、`[` 按字面处理（`/m/v1/w?tcher*` 不命中）。
   - `{…}` 是占位符，按每个请求替换（不认识的替换成空串），`/m/v1/{http.request.uri.query.zz}watcher/status` 对不带该参数的请求就是 `/m/v1/watcher/status`。
   - `%` 让 Caddy 在转义空间里比较。
   **审阅要点补充（U-6，wac-092，审查 wac-090 🟡-1…🟡-3）**：以下三类同样一律先当作"可能命中前缀"，写明理由：
   - **匹配器里有任何非 ASCII 字符**（例如 `İ`、开尔文符号 `K`、`ſ`）：Caddy 用 Go 的 `strings.ToLower` 转小写，`İ` 变成 `i`、`K` 变成 `k`，所以 `/m/v1/watcher/tradİng/accounts` 命中表内路径 `/m/v1/watcher/trading/accounts`；肉眼与 Python 都看不出来。
   - **路径里有 `//`**：模式含 `//` 时 Caddy **不合并**请求路径里的重复斜杠，而网关行的 `path_regexp` 总是先 clean。`request_header /m//v1/watcher/trading/accounts …` 对干净路径不生效，却会改写 `GET /m//v1/watcher/trading/accounts` 这个请求，它 clean 之后照样到达网关。
   - **`host` 匹配器里有 `*` 或 `{`**：`*` 按标签逐段比较，可以出现在任意一段（`jp-bot.*.wang` 命中 `jp-bot.balen.wang`）；`{…}` 按请求替换（`{http.request.host}` 命中任何主机）。另外，站点块**里面**的 `host` 匹配器，即使写的是别的名字，也按"可能命中"处理：站点可能应答多个名字，Host 头由调用方决定。
   **审阅要点补充（U-6，wac-094，审查 wac-093 🟡-1）**：以下三类也一律先当作"可能命中前缀"，写明理由。它们都**没有打中任何固定样本路径**，但真实 Caddy 会对表内路径生效（审查用 v2.10.2 实测注入了 observer token）：
   - **匹配器写的是某一个具体参数值**：例如 `/m/v1/watcher/trading/accounts/account-a`、`…/risks/BTCUSDT`。表内的 `{account_id}`、`{symbol}`、`{channel_id}`、`{filename}` 可以取任何值，写死一个值照样命中那一次请求。
   - **后缀 glob 或中段 glob**：例如 `*.png`、`/m/v1/watcher/media/*.png`（`GET /m/v1/watcher/media/x.png` 被注入，而 media 允许 `system_observer`，不带凭据的调用方就能读图）、`*/risks/btcusdt`（匹配不区分大小写）、`/m/v1/watcher/trading/accounts/a*`，以及只有一个尾部 `*` 但前半段写到了参数位置的写法（`…/channels/[0-9]*` 在快速前缀比较里按字面处理，但 channel id 本身就可以以 `[0-9]` 开头）。
   - **不锚定的 `path_regexp`**：没有以 `^` 开头（`account-a$`、`%2[fF]`）、带 `(?i)` 之类标志、顶层有 `|`（`^/api|watcher` 等于"以 /api 开头或含 watcher"），或 `^` 之后的字面前缀可能是 `/m/v1/watcher…` 的开头。
   wac-094 起 verify 的遮蔽第 1 步不再只拿样本比较，而是按**整个 `/m/v1/watcher` 前缀空间**判定（任何参数值、任何后缀、任何大小写）：上面三类在 verify 里一律算命中；只有字面部分能排除整个前缀空间的写法（`/static/?ld`、`/old/*`、`^/static/`、`/m/v1/watcherx*`）才算不命中。`@static path *.js *.css` 配只改响应头的 `header` 仍然通过（白名单）。探针另外对每条带参数的清单行用一组常见取值（`account-a`、`ACCOUNT-A`、`BTCUSDT`、`btcusdt`、`-1001234567890`、`x.png`、`x.PNG`、`x.jpg`、长值、带点的值等 35 个）逐个方法各发一次。人工记录仍是第二道。
   verify 自 wac-090 起按 Caddy v2.10.2 `MatchPath` 的算法判定 `?`、`[...]`、`\`。执行者当时做的 18 万组差分**只用了 ASCII 字母表**，所以没有发现非 ASCII 的分歧；审查 wac-091 用真实 Caddy 做黑盒差分（69,750 组，含非 ASCII），发现 12 处漏判，全部来自 `İ`。wac-092 起，占位符、`%`、写坏的 glob、**任何非 ASCII 字符、`//`** 在遮蔽第 1 步一律算命中，在逐行模拟里判 UNCOMPARABLE；`host` 只在 server 顶层（选站点）按 Caddy `MatchHost` 比较，站点内的 `host` 匹配器从不排除。同一黑盒差分在 wac-092 上复跑：危险分歧 0 处。人工记录仍是契约要求的第二道，审阅时不因"工具已通过"而跳过这几类写法。
5. 在现有 watcher basicauth handle 内、`basic_auth` 之后、`reverse_proxy` 之前加入（保持现有匹配器与 `uri strip_prefix /watcher` 不变）：

   ```caddyfile
   request_header -X-Watcher-Actor
   request_header -X-Watcher-Token-Fingerprint
   request_header -X-Watcher-Proxy-Auth
   request_header -Authorization
   ```

   并在该 handle 的 `reverse_proxy 127.0.0.1:9090 { … }` 里加：

   ```caddyfile
   header_up X-Watcher-Proxy-Auth {env.WATCHER_BROWSER_PROXY_TOKEN}
   ```

   说明：`request_header` 在指令顺序上先于 `reverse_proxy` 执行，所以是"先清后注入"；浏览器的 basic `Authorization` 必须清掉（核对工具要求这一项）。**不要**在 `reverse_proxy` 里写 `header_up -X-Watcher-*`。必须用运行时占位符 `{env.…}`，不能用 `{$…}`。这几行在 basicauth handle **内部**，不是站点顶层，不属于第 4 条的记录范围。
6. 若 S-03 显示 `/media/*` 目前不走 basicauth 块，而站点要显示图片，把 `/media/*` 并入 basicauth 块的匹配器。
7. **本机 Caddy 探针（非生产，F-12、F-13 (3)；授权 O0-A05P，见授权清单；wac-090 重写，wac-092、wac-094 收紧）**：**时机**：只在 wac-094 经 Reviewer PASS 并合入集成分支之后申请（在那之前，审查 wac-093 的 v15–v19 这类只对某个参数值、后缀或不锚定正则生效的注入，verify 与探针都看不见；而探针完成后副本即被删除，修完还得重新放一次副本）。在 C-1 之前，用与生产同版本的 Caddy（S-01 的 `caddy version`，当前按 v2.10.2 准备）对**候选 Caddyfile 的副本**跑探针。副本含 basic auth 的 bcrypt 哈希，所以副本怎么离开 jp-24 需要用户选定：
   - **推荐方案 (b)：用户自己放置副本。** 用户在本机建一个 0700 目录（例如 `~/o0-a05p-<UTC>/`：不在任何 git 仓库里、不在同步盘里），把 jp-24 上的 `$S/caddy/Caddyfile.candidate` 原样拷进去（文件名不变），再把本仓库的 `contracts/generated/caddy-watcher-gateway.caddy`（与 bundle 里的片段逐字节相同）放在同一目录。**Agent 不连 jp-24、不取副本、不打开或打印副本**；探针由用户运行，或由用户明确同意的执行者只运行下面这一条命令、只看它的输出（输出不含哈希，`caddy_real_test.sh` 对每一次探针输出都断言没有 `$2a$`）。
   - 方案 (a)：执行者用 `scp -P 53222` 取回副本。只在用户**另行授权** Agent 以 SSH 读取 jp-24、并接受哈希落到开发机时使用；本机目录与删除要求同 (b)。
   - 两种方案的共同前提：若 S-02/S-03 显示 Caddyfile 里有字面的头值或 token（`<literal len=N>` 而不是 `{env.…}` 占位符），副本就含明文凭据，**两种方案都先停下**，由用户决定是否先把字面值改成占位符。

   **探针前确认项 PC-1…PC-5（审查 wac-093 §9 的 C-1…C-5；为免与阶段 C 的"C-1 前置门禁"混淆，本文改称 PC）**：放副本之前逐项确认，记进 O0-A05P 的授权记录。任一项答不上来就先停。
   - **PC-1 生产 Caddy 版本**：S-01 的 `caddy version` 必须是 v2.10.2（探针用的二进制也必须是同一版本；探针会打印 `caddy version`）。版本不同，探针与 verify 的结论都不作数：先用同版本二进制重跑，或报 Planner 由 Architect 重新评估匹配语义。
   - **PC-2 jp-bot 站点块里有没有 `host` 匹配器**（O0-A01 S-03 的 inventory 可以看出来，也可以由用户直接告知）。若有，并且它排在片段之前、用了白名单以外的处理器（`redir`、`rewrite`、`uri`、`respond`、`request_header`、`abort`、`basic_auth`、`reverse_proxy` 等），或者不论位置把流量转发到 8183、9090、9100，verify 会失败。处置是把它移到单独的站点块，这会改变候选，要重新探针；不放宽检查。
   - **PC-3 站点头那一行的字面写法**：决定 `--site-address` 与 `--host`。裸的 `jp-bot.balen.wang {` 用默认值即可；写成 `{$CADDY_DOMAIN}`、带 scheme（`https://…`）、带端口或多个地址时，探针加 `--site-address <那一行里的字面地址记号> --host jp-bot.balen.wang`（`{$…}` 另加 `--adapt-env NAME=jp-bot.balen.wang`）。探针找不到这个地址记号会失败，不会猜。
   - **PC-4 副本放置用方案 (b)**，并约定跑完后由用户做三项检查（见下面"完成之后"）：`ls -A` 副本目录、`ls -d "$TMPDIR"/o0-caddy-probe-xdg-*`、`pgrep -fl o0-caddy-probe-xdg`；然后删除副本目录。
   - **PC-5 生产 Caddyfile 在 `/m/v1/watcher` 前缀下有没有写具体参数值、后缀或中段 glob、不锚定的 `path_regexp` 的匹配器**（第 4 条的 U-6 审阅要点）。wac-094 起 verify 按前缀空间把它们判为命中，这一项是第二道；若有，照实记录并说明它为什么不构成遮蔽，verify 失败时改候选，不放宽检查。

   命令（本机，不连 jp-24；探针发现自己跑在有 `/srv/trader-v3` 的主机上会拒绝运行）：

   `umask 077; python3 scripts/ops/o0/o0_caddy_watcher_routes.py probe --caddy <caddy v2.10.2> --caddyfile ~/o0-a05p-<UTC>/Caddyfile.candidate [--host jp-bot.balen.wang] [--adapt-env NAME=<假值> …] 2>&1 | tee ~/o0-a05p-evidence-<UTC>.txt`

   探针做什么（wac-090 起）：
   - 先打印 `PROBE_INPUT candidate_sha256=<副本> snippet_sha256=<片段>`。
   - 在副本旁写 `Caddyfile.candidate.o0probe`（0600）：只改站点地址（wac-092 起 → `http://<生产主机名>:<空闲端口>`，主机名默认取 `--site-address`，即 `jp-bot.balen.wang`，可用 `--host` 指定；监听仍由 `default_bind 127.0.0.1` 限定在回环，之后每个探针请求都带 `Host: <生产主机名>`，站点里依赖 `host` 的路由与生产行为一致）与全局选项 `admin off`、`persist_config off`、`auto_https off`、`default_bind 127.0.0.1`、`http_port`、`https_port`，逐行差异脱敏打印。上游地址**不在文本里改**。
   - 对 `.o0probe` 做 `caddy adapt`，用**生产上游地址与生产主机名**跑与 C-1 相同的 verify（operator-query 按端口 8183 识别，写成 `localhost:8183`、`[::1]:8183` 也算）。
   - 然后生成真正运行的配置：只保留探针站点那一个 server，只监听 `127.0.0.1:<端口>`；**每一个** `reverse_proxy` 上游（含 subroute、`handle_response`、错误路由里的）都改到本机桩：operator-query → 桩 oq，watcher → 桩 watcher，其他一切（别的服务、Tailscale 地址、unix socket、占位符）→ 桩 sink；删除主动健康检查与转发代理；不带任何其他 server 和 app（tls、pki、logging…）；admin 关闭、配置不落盘。逐条打印 `PROBE_PIN dial <原地址> -> <桩>`。运行前机器核对"只监听回环、只拨三个桩"，不满足就不启动 Caddy（`dynamic_upstreams` 无法改写，直接判失败）。Caddy 进程拿不到 `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY` 与 `OTEL_*` 环境变量，状态目录（autosave、数据目录）在临时目录。
   - 真实启动后用原始请求行发送：§9.14.4 第 1 项的全部行级与兜底探针（含 `%0A` 换行结尾、`#`）；F-13 (3) 两类用例——**字面点段与 `//` 期望被 clean 后转发到桩**（`/m/v1/watcher/x/../status`、`/m/v1/watcher/./status`、`/m/v1/watcher//status` → 桩收到 `/v1/watcher/status`；`…/accounts/a/..` → `/v1/watcher/trading/accounts`；`/m/v1/watcher/x/../config` → 兜底 404），**不按 404 判定**；百分号编码形式与 `#` 原样转发（`/m/v1/watcher/x/%2e%2e/status` → `/v1/watcher/x/%2e%2e/status`，`/m/v1/watcher/media/a#x` → `/v1/watcher/media/a%23x`，到网关后由 `%` 规则 404）。**wac-092**：每条清单行再在每个斜杠处各多加一个 `/` 发一次（`//m/v1/watcher/status`、`/m//v1/watcher/status`、`/m/v1//watcher/status`、`/m/v1/watcher//status` 等），期望被 clean 后转发、`Authorization` 原样到达；`/m//v1/watcher/trading/accounts` 另发一次不带 `Authorization` 的。**wac-094**：每条带参数的清单行（`media/{filename}`、`accounts/{account_id}`、`channels/{channel_id}`、`risks/{symbol}`）再用 35 个参数值各发一次，每个允许的方法都发：真实账户名与大写（`account-a`…`account-d`、`ACCOUNT-A`）、交易对的大小写（`BTCUSDT`、`btcusdt`、`1000PEPEUSDT`）、Telegram 形式的 id（`-1001234567890`）、短值、200 字符的长值、带点的值、`x.png`/`x.PNG`/`x.jpg`/`x.JPG`/`x.jpeg`/`x.gif`/`x.webp`/`x.mp4` 等文件名，都期望原样转发、`Authorization` 原样到达。**每一次**转发到 operator-query 都带一个假的调用方 `Authorization`，桩必须原样收到、且没有 `X-Watcher-Proxy-Auth`（任何改写表内路径请求头的遮蔽都会在这里暴露，包括只对 `//` 请求或只对生产 Host 生效的）；不带 `Authorization` 的 `/m/v1/watcher/status` 与 `/m/v1/watcher/trading/accounts` 到桩时也不得多出一个；`/m/v1/watcherx`、`/m/v1/other` 等不得到达 oq 或 watcher 桩；`/m/v1/accounts` 保留调用方 `Authorization`；`/watcher/`、`/api/status`、`/media/<名>` 无凭据 401 且不到任何桩。
   - 清理（wac-092 如实描述，审查 wac-090 🟡-4）：正常结束、检查失败、异常，以及收到 SIGINT（Ctrl-C）、SIGTERM、SIGHUP 时，`.o0probe` 与临时目录都会被删除，Caddy 进程组被停掉；清理顺序是先删 `.o0probe`，再停 Caddy，再删临时目录（里面是运行用的 JSON），最后才关本机桩。清理进行中再收到信号（第二次 Ctrl-C 等）会立即强杀 Caddy、删掉两处文件并退出（退出码 128+信号号，输出 `CADDY_PROBE_INTERRUPTED … during cleanup`）。**做不到的**：探针进程本身被 SIGKILL（`kill -9`，包括被系统 OOM 杀掉）或断电。审查 wac-093 实测，这时会留下**三样东西**：副本旁的 `.o0probe`（0600，含 bcrypt 哈希）；`$TMPDIR/o0-caddy-probe-xdg-<随机>/` 临时目录（里面的 `probe-run.json` 同样含哈希）；以及**一个仍在运行的真实 Caddy**（探针让它在新会话里启动，以便整组停止，所以它不随探针退出），它带着钉住的配置监听一个回环端口，上游是已经不存在的本机桩。
   wac-094 起的自清理：探针每次建临时目录时先在里面写一份属主记录 `o0-probe-owner.json`（探针自己的 pid、`.o0probe` 的路径与写入内容的 sha256、它启动的每个子进程 pid），**下一次运行探针时先扫描 `$TMPDIR`**：
   - 属主记录有效、记录里的探针已经不在运行：停掉命令行里带这个临时目录路径的进程（即那个 `caddy run --config <目录>/probe-run.json`，先 SIGTERM 整组，5 秒后 SIGKILL），`.o0probe` 的内容与记录的 sha256 相同才删除，再删临时目录；打印 `PROBE_RESIDUE_CLEANED dir=<目录名> processes_stopped=<n> o0probe=removed|absent|kept(...) dir_removed=True`。
   - 记录里的探针还在运行（另一次探针正在跑）：`PROBE_RESIDUE_BUSY`，不动。
   - 没有本工具的属主记录的目录（wac-094 之前的版本留下的，或别的工具的）：`PROBE_RESIDUE_FOREIGN dir=<目录名>`，**不动**，由用户确认后自己删除（里面可能有哈希）。
   - 命令行里带 `o0-caddy-probe-xdg-` 但不属于任何已证明目录的进程：`PROBE_RESIDUE_ORPHAN pid=<n>`，**不动**，由用户确认后自己 `kill`。
   - 末尾一行汇总：`PROBE_RESIDUE_SCAN cleaned=<n> busy=<n> foreign=<n> orphans=<n> unproven=<n>`。只打印目录名与 pid，不打印路径内容。
   - 扫描之后若副本旁已经有 `.o0probe`（被杀的探针留下、但内容与记录不符，或上次用了 `--keep`），探针**拒绝运行**：`CADDY_PROBE_FAILED Caddyfile.candidate.o0probe already exists …`，由用户看过后删除再重跑。
   自清理只在"再跑一次探针"时发生，而且只清理能由属主记录证明是自己的东西；所以下面"完成之后"的三项检查**每次都要做**。`--keep` 只用于排错，会保留含哈希的 `.o0probe`，用完自己删。

   期望输出：`PROBE_LOCAL_ONLY listen=127.0.0.1:<端口> host=jp-bot.balen.wang admin=off persist=off servers=1 apps=http dials=stubs_only`，末行 `CADDY_PROBE_OK caddy=v2.10.2 live_checks=<n> … host=jp-bot.balen.wang … stub_hits=oq:<n>,watcher:<n>,sink:<n> candidate_sha256=<…> snippet_sha256=<…>`（仿生产夹具上 wac-094 起 `live_checks=840`、`oq:327`；wac-092 时是 420、117）。另有一行 `PROBE_RESIDUE_SCAN …`（见上面的清理说明），正常情况下 `cleaned=0 busy=0 foreign=0 orphans=0`。被信号打断时末行是 `CADDY_PROBE_INTERRUPTED signal=<名>…`，退出码 128+信号号：不算通过，确认 `ls -A` 后重跑。

   完成之后：
   - 三项检查（PC-4，用户在运行探针的同一个 shell 里做，`$TMPDIR` 要与探针运行时相同；macOS 上是每个用户自己的 `/var/folders/…/T/`）：
     1. `ls -A ~/o0-a05p-<UTC>/` 应只剩 `Caddyfile.candidate` 与 `caddy-watcher-gateway.caddy`（没有 `.o0probe`）。有 `.o0probe` → 它含哈希，确认后 `rm -f`。
     2. `ls -d "$TMPDIR"/o0-caddy-probe-xdg-* 2>/dev/null` 应无输出。有 → 看探针输出里对应的 `PROBE_RESIDUE_*` 行；确认是探针留下的（目录名以 `o0-caddy-probe-xdg-` 开头，里面是 `probe-run.json`、`caddy-run.err`、`config/`、`data/` 之类）后 `rm -rf` 这些目录。
     3. `pgrep -fl o0-caddy-probe-xdg` 应无输出。有 → 命令行里是 `caddy run --config …/o0-caddy-probe-xdg-…/probe-run.json` 的，就是探针留下的孤儿 Caddy，`kill <pid>`（不退出再 `kill -9 <pid>`）；命令行不是这种形状的，不要动，报 Planner。
     三项都干净之后 `rm -rf ~/o0-a05p-<UTC>/`，**删除副本**（方案 (b) 由用户删除）。
   - 证据 `~/o0-a05p-evidence-<UTC>.txt` **只存本机**（不写到 jp-24 的 `$S/evidence/`，审查 wac-088 💭-4）：它含脱敏差异、上游地址与两个 sha，不含哈希。把它的 sha256 与末行的两个 sha 记进 O0-A05 的授权记录。
   - 两个 sha 是 C-1 的必填参数（`--probe-candidate-sha256`、`--probe-snippet-sha256`）：preflight 核对 staging 里的候选与 bundle 片段就是被探针测过的那两份（`PROBE_BINDING_OK`），不一致即中止；门禁文件记录这两个值，apply 再用手上的文件核对一遍。**探针之后候选有任何改动，都要重新做 O0-A05P。**
   - 失败处置：`CADDY_PROBE_FAILED` 或出现 `FAIL` 行 → 不申请 O0-A05；按 FAIL 行改候选（在 jp-24 的 staging 里，由用户或经授权的执行者），重新放副本、重跑探针。verify 与活体检查的 FAIL 行分开各打印最多 40 条（末行给出 `verify_failures=`、`live_failures=`）。`FAIL pin: …`（有无法改写到桩的上游）→ 报 Planner 与用户，不做变通。verify 报站点里某条带 `host` 匹配器的路由（遮蔽或转发）：wac-092 起站点内的 `host` 匹配器从不排除，若它确实只为别的主机名服务，把它移到单独的站点块，不放宽检查。

   本仓库的 `tests/caddy_real_test.sh` 用同一工具对仿生产夹具跑过（`O0_CADDY_BIN=<v2.10.2>`），包括：调用者的 HOME/XDG 目录在探针后仍为空、`.o0probe` 的全局选项与 0600 权限、一个挂在副本兜底路由后面的"生产服务"金丝雀在探针全程收到 0 个请求（含主动健康检查与第二个站点）、只改运行配置的 Caddy 包装器证明活体检查单独就能抓到兜底 404 带 body、表内路径的 `Authorization` 被改写或删除、`/m/v1/watcherx` 被转发，以及 adapt 失败时不留 `.o0probe` 与临时目录。wac-092 另加：非 ASCII、`//`、站点内 `host` 匹配器的注入副本在 verify 与活体检查里**各自**失败；只改运行配置的包装器证明活体检查单独就能抓到只对 `//` 请求、只对生产 Host 生效的改写；检查失败的探针同样不留 `.o0probe` 与临时目录；在 `caddy adapt` 期间、`caddy run` 启动期间、清理等待 Caddy 停止期间发信号（含先 SIGHUP 再 SIGINT），都不留 `.o0probe`、临时目录与 Caddy 进程；调用者的代理与 `OTEL_*` 变量到不了 Caddy。

### 3.2 步骤

| 步 | 内容 | 命令 | sha / 验证 | 失败处置 |
|---|---|---|---|---|
| C-1 前置门禁（只读，只写 staging） | 清旧门禁；舰队基线（完整、新鲜）；bundle 复核；Caddy 版本；记录线上 Caddyfile 与 v3.env 的 sha；**候选必须在 `$S` 之内**（`--candidate` 指向 `/etc/caddy` 或任何线上配置目录、含 `..`、或与线上 Caddyfile 同目录时，脚本在做任何事之前拒绝；preflight 里再按解析软链后的真实路径核对一次，`CANDIDATE_IN_STAGING`）；候选存在且不同于线上；**候选与 bundle 片段的 sha256 等于 O0-A05P 探针末行的两个值（`PROBE_BINDING_OK`）**；**bundle 清单（格式 v2）与片段（snippet.v1）自检，片段等于从清单推导的形状**；**线上 `/etc/caddy/caddy-watcher-gateway.caddy` 不存在或与 bundle 相同**（wac-092：存在但不同、或是悬空软链，输出 `LIVE_SNIPPET_DIFFERS` 并中止）；把片段放到候选同目录并比对字节；**候选文本检查**（片段文件在全局位置恰好 import 一次，站点顶层 `import watcher_gateway_routes` 在所有 handle 之前，打印 F-13 (2) 记录行）；脱敏 diff（只容忍 `diff` 退出码 1，脱敏失败即失败）；凭据检查（含控制面目录）；带 env 的 `caddy validate`；`caddy adapt` 到 0600 JSON；**verify**：`^/m/v1/watcher/` 的 (pattern, methods) 与清单对称差为空、兜底逐字相同且在最后、F-13 两步遮蔽检查（第一条 wgw 路由之前、以及外层的每一条路由）、没有别的处理器转发该前缀、逐行逐方法模拟、浏览器清头注入；**全部通过才写 `caddy-preflight` 门禁**（含候选、线上两文件、凭据片段、staging 片段的 sha，以及探针的两个 sha） | `bash $T/o0_deploy_caddy.sh --execute --phase preflight --auth-id O0-A05 --stage-dir $S --probe-candidate-sha256 <CADDY_PROBE_OK 的 candidate_sha256> --probe-snippet-sha256 <CADDY_PROBE_OK 的 snippet_sha256>` | `CANDIDATE_IN_STAGING`；`FLEET_BASELINE_OK`；`PROBE_BINDING_OK`；`CADDY_ARTIFACTS_OK lines=16`；`LIVE_SNIPPET_ABSENT`（或 `…EQUALS_BUNDLE`）；`SNIPPET_STAGED`；`CADDYFILE_CHECK_OK`；`CREDENTIAL_CHECK_OK`；`Valid configuration`；`CADDY_WATCHER_ROUTES_OK mode=snippet lines=16`；`GATE_WRITTEN stage=caddy-preflight`；人工确认脱敏 diff 只动了 §3.1 的 2、3、5 条；§3.1 第 4 条的记录与第 7 条的本机探针证据齐全 | 任一失败：中止，线上未改，也没有门禁文件。缺探针 sha 或候选不在 `$S` 内：脚本启动即拒绝。`PROBE_BINDING_FAILED`：staging 里的候选不是探针测过的那份，重新做 O0-A05P。verify 报 `shadow …`：按 §3.1 第 3、4 条改候选，不得给白名单加例外。verify 报 `UNCOMPARABLE … dial …`：有转发前缀的路由上游写成占位符、unix socket 或端口范围，改成明确的 `host:port` |
| C-2 应用 | **先**核对门禁（同一候选 Caddyfile、线上两个文件、凭据片段与 staging 片段未变、门禁里探针的两个 sha 等于手上的候选与 bundle 片段、≤ 1 小时）、bundle 复核、staging 片段仍等于 bundle 且线上片段文件不存在或相同、再 validate 候选、记录舰队基线；然后备份两份文件（0700）并记录片段文件原本是否存在；合入 `WATCHER_BROWSER_PROXY_TOKEN` 并用摘要确认 v3.env 持有 watcher 当前值；**安装片段为 `/etc/caddy/caddy-watcher-gateway.caddy` 并比对字节**；安装候选；**已安装文件与门禁里的候选 sha 一致**；带 env validate；对已安装文件 adapt 并 verify 路由；`systemctl restart caddy`；`is-active`；舰队守卫（稳定窗 + 多次采样） | `bash $T/o0_deploy_caddy.sh --execute --phase apply --auth-id O0-A05 --stage-dir $S` | `GATE_OK`；备份 sha 写入 `evidence/caddy-backup.sha256`；`CADDY_WATCHER_ROUTES_OK`（已安装文件）；`FLEET_UNCHANGED_ALL_SAMPLES` | 门禁不符（含线上已有一份**不同**的片段文件）：退出，线上未改。备份之后、restart 之前的任何失败：ERR trap 自动回滚还原两份文件、把片段文件恢复到 apply 前的状态（原本不存在就删除，`SNIPPET_REMOVED_AS_BEFORE_APPLY`）、sha 校验回到 C-1 记录值、validate，**不 restart**（运行中的 Caddy 从未变过，restart 只会平添 D-02 风险）。restart 及之后的失败：还原、validate、再 restart、`is-active`。两种情况回滚后都跑舰队守卫（对比 `before-caddy`，只报告不设门槛），结论写入 `evidence/auto-rollback.log` 与 `fleet-after-auto-rollback-*.verdict.txt`，脚本退出 1；`fleet_rc` 非 0 立即报用户。舰队变化：退出码 3，**不回滚也不 RESUME**，立即报用户 |
| C-3 验证（只读） | admin API 取运行中配置到 0600 JSON，再跑同一 verify；线上片段文件仍等于 bundle；本机回环 `--resolve` 公网探针：`/m/v1/accounts` 无 token 401、伪 token 403；`/m/v1/watcher/status` 404（阶段 O 之后 401）；两段路径不到网关；`/watcher/`、`/api/status` 401；`/media/<名>` 不得无认证返回图片；节点通道有响应；舰队守卫 | `bash $T/o0_deploy_caddy.sh --execute --phase verify --auth-id O0-A05 --stage-dir $S --node-channel <S-06 地址>` | `CADDY_WATCHER_ROUTES_OK`；探针全部符合；`FLEET_UNCHANGED_ALL_SAMPLES` | 验证失败：执行 C-4 |
| C-4 回滚 | 记录回滚前舰队样本（不设门槛）；还原两份备份与片段文件状态、sha 校验、带 env validate、restart、`is-active`、探针、舰队守卫 | `bash $T/o0_deploy_caddy.sh --execute --phase rollback --auth-id O0-A05 --stage-dir $S` | `sha256sum -c evidence/caddy-live.sha256` 通过 | 回滚本身失败：Caddy 不可用 = 节点通道中断，**立即**报用户；手工恢复路径为 `cp -p backup-caddy/Caddyfile.bak /etc/caddy/Caddyfile && cp -p backup-caddy/v3.env.bak /etc/caddy/v3.env && { [ -e backup-caddy/snippet.absent ] && rm -f /etc/caddy/caddy-watcher-gateway.caddy || cp -p backup-caddy/snippet.bak /etc/caddy/caddy-watcher-gateway.caddy; } && systemctl restart caddy` |

## 4. 阶段 W：watcher（O0-A04 门禁与构建，O0-A07 上线，O0-A07R 仅在需要时恢复数据库）

| 步 | 内容 | 命令 | sha / 验证 | 失败处置 |
|---|---|---|---|---|
| W-1 前置门禁（只读） | 清旧门禁；舰队基线；bundle 复核；**线上源码与 compose 等于 `67b401a` 基线**；构建上下文无 `config.json`；凭据检查（含目录）；Caddy 已注入浏览器凭据；磁盘余量；写 `watcher-preflight` 门禁 | `bash $T/o0_deploy_watcher.sh --execute --phase preflight --auth-id O0-A04 --stage-dir $S` | `MANIFEST_OK watcher-live-vs-baseline`、`MANIFEST_OK compose-live-vs-baseline`、`CREDENTIAL_CHECK_OK`、`CADDY_INJECTS_BROWSER_CREDENTIAL`、`GATE_WRITTEN` | 中止；线上未改 |
| W-2 离线构建与副本演练 | 清旧门禁；构建器自己校验运行时清单（白名单全集、sha、大小）；以**运行中镜像 ID** 为固定基础（记录其 `org.trader.*` 标签），列出运行中镜像里白名单之外的文件（`BASE_ONLY`，交用户审阅，`config.json` 不得在内）；`scripts/build_immutable_watcher_image.py` 离线构建（`FROM` 固定基础，只 `COPY` 已审文件，`docker build --network=none --no-cache`，不 `apt`、不 `pip`、不 `npm`）并打 `trader-watcher:o0-candidate-<UTC>`；镜像内 `require` 冒烟；备份 API 做真库一致副本（副本切成回滚日志模式）；迁移前基线（退出码写入证据，2 = 失败）；在副本上首次启动：`--network none`、空 `config.json`、一次性假凭据、`PRICE_MONITOR_ENABLED=0`、`HERMES_TRADER_CRON_ENABLED=0`、`SIGNAL_IMPORTER_ENABLED=0`、清空 `WATCHER_ALERT_*`；看到 `Web UI listening` 后 **`docker stop` 正常停止**（SIGTERM，退出码必须 0）；再用备份 API 从演练库做一份新副本（读得到 WAL）并在**新副本**上分析：`config_revision` 从 0 开始、三表摘要对比；写 `watcher-build` 门禁（候选镜像 ID） | `bash $T/o0_deploy_watcher.sh --execute --phase build --auth-id O0-A04 --stage-dir $S` | `RUNTIME_MANIFEST`（构建器校验通过）；`BASE_ONLY` 清单；`REQUIRE_OK`；`SQLITE_BACKUP … integrity=ok`；`dryrun_exit_code=0`；`MIGRATION_DRYRUN_OK`；`revision_after= 0`；三表 `UNCHANGED` 或 `CHANGED_BY_MIGRATION` | 任何失败：中止；只留下 staging 里的镜像与副本。`CHANGED_BY_MIGRATION` 或 `BASE_ONLY` 非空：交用户确认后再申请 O0-A07 |
| W-3a 上线前复核（只读） | 与 W-1 相同的门禁，在上线窗口内重跑（门禁有效期 1 小时） | `bash $T/o0_deploy_watcher.sh --execute --phase apply-preflight --auth-id O0-A07 --stage-dir $S` | 同 W-1 | 中止 |
| W-3 上线 | **先**：两份门禁核对（凭据片段与线上 compose 未变）；候选镜像存在且 ID = build 门禁；演练日志 sha 未变且含 `MIGRATION_DRYRUN_OK`；bundle 复核；线上源码与 compose 仍等于基线；凭据复核；舰队基线。**然后**：备份（旧镜像打 `o0-rollback-<UTC>`、被覆盖与新增源码清单与 tar、compose、env_file 状态（有则备份、无则记"原本不存在"）、真库在线备份及其 sha）；写 `/srv/trader-secrets/watcher-gateway.env`（0600）并用摘要确认两个持有方与之一致；安装候选源码 → `MANIFEST_OK watcher-live-vs-candidate`；安装候选 compose → `MANIFEST_OK compose-live-vs-candidate`；`compose config --quiet`；镜像名指向候选；记录重建时间；`up -d --no-deps --force-recreate --no-build watcher`；启动日志 `Web UI listening`、无 `[db] Failed`、无凭据错误、90 秒内重启计数 0；**180 秒内出现 `[watcher] Connected, listening...`**（出现 `Session not authorized` 立即失败）；无凭据 401、浏览器凭据 200；真库 `config_revision` 存在；舰队守卫 | `bash $T/o0_deploy_watcher.sh --execute --phase apply --auth-id O0-A07 --stage-dir $S` | 上列各行 + `TELEGRAM_RECONNECTED` + `FLEET_UNCHANGED_ALL_SAMPLES` | 门禁不符或镜像缺失：退出，任何服务都没动。备份之后、重建之前的失败：ERR trap 自动回滚只还原源码、compose、env_file 状态与镜像名标签，**不重建容器**（旧容器一直在跑）。重建及之后的任何失败（含 Telegram 未重连）：还原后用旧镜像重建（W-5）。两种情况回滚后都跑舰队守卫并写入 `evidence/auto-rollback.log`（只报告），脚本退出 1。舰队变化：退出码 3，报用户 |
| W-4 验证（只读） | 健康检查变 healthy；Telegram 仍连着、容器仍在跑且重启计数 0、重建以来的转发行计数（只计数，不含正文）写入证据；容器 env 中没有控制面 token 名；舰队守卫；**用户浏览器核对** | `bash $T/o0_deploy_watcher.sh --execute --phase verify --auth-id O0-A07 --stage-dir $S` | `health=healthy`；`forwarded_lines_since_recreate=`；`NO_CONTROL_PLANE_TOKENS_IN_WATCHER` | 执行 W-5 |
| W-5 回滚 | 记录回滚前舰队样本；还原被覆盖的源码、删除新增文件、还原 compose；env_file 回到 apply 前的状态（原有则还原，原本没有则删除）；源码与 compose 回到基线 sha；镜像名指回 `o0-rollback-<UTC>`；`--no-build` 重建；旧 watcher `/api/status` 200；舰队守卫 | `bash $T/o0_deploy_watcher.sh --execute --phase rollback --auth-id O0-A07 --stage-dir $S` | `MANIFEST_OK`（基线）；`ENV_FILE_RESTORED` 或 `ENV_FILE_REMOVED_AS_BEFORE_APPLY`；`api/status=200` | 数据库**不回滚**（两张新表旧代码忽略）。若快照开关已打开，**必须先关开关**再回滚 watcher（wac-011 第 6 点） |
| W-6 恢复数据库（仅限数据被迁移破坏时） | 断言快照开关为 0（只检查，不改）；备份 sha 与 `integrity_check` 正确；**在任何移动之前**用 `stat` 记录线上库的属主与权限（`evidence/watcher-db-owner.txt`，数字 uid:gid 与八进制权限）、要移走的文件清单与各自 sha（`watcher-db-replaced.{files,sha256}`）；`backup-watcher/db-replaced/` 已有文件就拒绝（不覆盖上一次移走的原库）；舰队基线；上膛自动恢复；停 watcher；把当前库文件（含 `-wal`/`-shm`）移到 `db-replaced/`；装回迁移前在线备份，**按记录的属主与权限** `chown`/`chmod`，证明字节、属主、权限三者一致；启动；`Web UI listening`、无 `[db] Failed`、Telegram 重连；舰队守卫 | `bash $T/o0_deploy_watcher.sh --execute --phase restore-db --auth-id O0-A07R --i-understand-data-loss --stage-dir $S` | `RESTORED_DB_SHA_OK`；`RESTORED_DB_OWNER_MODE_OK`；`RESTORE_DB_WATCHER_UP` | **破坏性**：上线后写入的配置与 Telegram 消息会丢失，审计保留期（契约 §9.12 ≥ 30 天）被打断。O0-A07 的号执行不了这一步。**停 watcher 之后任何一步失败**（装回、属主、启动、Telegram 未重连）：自动恢复到 restore-db 开始前的状态：确保 watcher 已停；装回失败的那份移到 `db-failed-restore/`，失败启动留下的 `-wal`/`-shm` 也移过去；`db-replaced/` 里的原文件用 `mv` 放回（属主、权限、inode 不变）；按 `watcher-db-replaced.sha256` 核对字节，按 `watcher-db-owner.txt` 核对属主与权限（`RESTORE_DB_RECOVERED_FILES`）；启动 watcher 并检查启动日志；舰队守卫（只报告），写 `evidence/auto-rollback.log`，脚本退出 1。**自动恢复本身失败**（`ROLLBACK FAILED`，例如原库放回后字节或属主、权限与记录不符：跨文件系统的移动可能丢属主，此时**不启动** watcher）：立即报用户；手工路径见表下 |

**W-6 自动恢复失败时的手工路径**（仍在 O0-A07R 授权范围内，每一步贴输出；`D=/var/lib/docker/volumes/trader_signal-data/_data`，`B=$S/backup-watcher`）：

1. `docker compose --project-name trader --file /srv/trader/docker-compose.yml stop watcher`
2. `mkdir -p -m 0700 $B/db-failed-restore; for s in '' -wal -shm; do [ -e "$D/watcher-trading.db$s" ] && mv "$D/watcher-trading.db$s" $B/db-failed-restore/; done`（先把现场的文件挪开，不删除）
3. `while read -r n; do mv "$B/db-replaced/$n" "$D/$n"; done < $S/evidence/watcher-db-replaced.files`
4. `(cd $D && sha256sum -c $S/evidence/watcher-db-replaced.sha256)` 全部 `OK`；`stat -c '%u:%g %a' $D/watcher-trading.db` 等于 `cat $S/evidence/watcher-db-owner.txt`。不一致就停在这里报用户，不启动。
5. `docker compose … start watcher`；`docker logs --since <刚才的时间> trader-watcher-1 2>&1 | grep -cE 'Web UI listening|\[watcher\] Connected, listening'` ≥ 2，`grep -c '\[db\] Failed'` = 0。
6. 按 `docs/agent-operations.md` §0 的查询手工采样一次舰队（只读），与 `evidence/fleet-before-restore-db.txt` 逐节点对照状态、release、心跳年龄，结论报用户。独立守卫脚本的号与 O0-A07R 不同，这里不用它。

W-3 之后按 `docs/agent-operations.md` §1 核对重建窗口内有无漏信号：`journalctl -u trader-v3-hermes-feeder --since <evidence/watcher-recreate.at> | grep -c 'Triggered job: signal'`，与 W-4 的转发行计数对照；周末美股代币类频道静默时 0 不代表故障；漏了只报告，不补单。

## 5. 阶段 O：operator-query（授权 O0-A08）

| 步 | 内容 | 命令 | sha / 验证 | 失败处置 |
|---|---|---|---|---|
| O-1 前置门禁（只读） | 清旧门禁；舰队基线；记录三个控制面单元 MainPID 与启动时间；bundle 复核；`.venv-cp` Python ≥ 3.11 且有 `httpx`；**线上 `api/*.py`、`security/*.py` 等于 `67b401a` 基线**；快照开关未设或为 0；凭据检查；**控制面单元隔离（S-10 的机读门禁，`o0_tool.py cp-isolation`）**：node-control 与 event-ingest 的 EnvironmentFiles 不含 `operator-query.env`、它们的 `Environment=` 与各自 env 文件里没有任何 `WATCHER_*TOKEN` 名字、operator-query 的 WorkingDirectory 是 `--cp-root` 或其 `api/`（只读名字，不打印值）；overlay import 冒烟（三种角色）；写 `oq-preflight` 门禁 | `bash $T/o0_deploy_operator_query.sh --execute --phase preflight --auth-id O0-A08 --stage-dir $S` | `MANIFEST_OK cp-live-vs-baseline`；两行 `ENVFILE_ISOLATION ok`、`CP_ISOLATION_OK shared_code_dir=…`（共用目录时 D-04 适用）；`IMPORT_OK operator-query watcher_routes=N`（N > 0），另两个角色 `watcher_routes=0`；`GATE_WRITTEN` | 中止；线上未改，也没有门禁文件。`CP_ISOLATION_FAILED`（`ENVFILE_ISOLATION VIOLATION` 或 `CODE_DIR_MISMATCH`）：**阻断阶段 O**，报用户；不在 O-0 里改单元文件。`CP_ISOLATION_UNCOMPARABLE`（单元不存在、env 文件读不到）：同样阻断，核对 S-10。import 冒烟若因缺少单元里 `Environment=` 的其他变量而失败，先补齐冒烟环境再跑，不能跳过 |
| O-2 上线 | **先**：门禁核对（凭据片段与线上 env 未变）；bundle、venv、基线、开关、凭据、**单元隔离**复核；舰队基线。**然后**：备份被覆盖文件（tar）、新增文件清单、env 文件及其 sha；合入 `WATCHER_GATEWAY_TOKEN`、`WATCHER_SNAPSHOT_TOKEN` 并用摘要确认线上 env 持有 watcher 当前值、仍不与目录撞值、开关仍关；安装五个文件 → `MANIFEST_OK cp-installed`；记录重启时刻；**只重启 operator-query**；60 秒内 `/v1/accounts` 无 token 401；重启以来的日志没有生成物加载失败或网关 token 缺失/撞值；**在自动回滚窗口内**用 `SYSTEM_OBSERVER_TOKEN` 经网关请求 `/v1/watcher/status` 期望 200；舰队守卫 | `bash $T/o0_deploy_operator_query.sh --execute --phase apply --auth-id O0-A08 --stage-dir $S` | `GATE_OK`；`MANIFEST_OK cp-installed`；`gateway_startup_error_lines=0`；`PROBE … status=200`；`FLEET_UNCHANGED_ALL_SAMPLES` | 门禁或单元隔离不符：退出，线上未改（隔离检查在备份之前）。备份之后、重启之前的失败：ERR trap 自动回滚只还原代码与 env，**不重启**。重启及之后的任何失败（含网关 503 `gateway_disabled`/`watcher_unavailable`）：还原后只重启 operator-query（O-4）。两种情况回滚后都跑舰队守卫并写入 `evidence/auto-rollback.log`（只报告），脚本退出 1 |
| O-3 验证（只读） | 网关：无 token 401、伪 token 403、`SYSTEM_OBSERVER_TOKEN` 200、尾斜杠 404 无 `Location`、P3 路径 404；既有 `/v1/accounts` 200；公网 `/m/v1/watcher/status` 无 token 401；另两个单元 MainPID 与启动时间不变；舰队守卫 | `bash $T/o0_deploy_operator_query.sh --execute --phase verify --auth-id O0-A08 --stage-dir $S` | 无 `PROBE_FAIL`；`OTHER_UNITS_UNCHANGED`；`FLEET_UNCHANGED_ALL_SAMPLES` | 执行 O-4 |
| O-4 回滚 | 记录回滚前舰队样本；还原被覆盖文件、删除新增文件与空的 `generated/`；代码 sha 回到基线；还原 env 并核对 sha；只重启 operator-query；`/v1/accounts` 401；舰队守卫 | `bash $T/o0_deploy_operator_query.sh --execute --phase rollback --auth-id O0-A08 --stage-dir $S` | `MANIFEST_OK cp-restored` | 回滚失败：app 与运维查询中断，但节点心跳走 node-control 不受影响；立即报用户 |

**共享代码目录（D-04，待用户确认）**：三个控制面单元都跑 `read_api:app`，且（待 S-10 确认）共用同一目录。正因为共用目录，把网关值留在 operator-query 里的唯一边界是 env 文件；这条边界现在由 O-1、O-2 的单元隔离门禁机读检查（审查 wac-032-r2 🟡-6），违例即阻断阶段 O。O-2 之后 node-control 与 event-ingest 仍在内存里跑旧代码，但磁盘上已是新 `read_api.py`，它们下次重启（任何原因）就会加载新代码。O-1 的 import 冒烟已证明新代码对这两个角色可加载、且不注册网关路由。

## 6. 上线后

- 证据包（都在 `$S/evidence/`，不含凭据）：`authorizations.log`、`*.gate.json`、各阶段 `fleet-*.txt` 与 `*.verdict.txt`、`caddy-live.sha256`、`caddy-backup.sha256`、`caddy-diff.redacted.txt`、`watcher-base-*.txt`、`watcher-build-attestation.json`、`watcher-dryrun.log`、`watcher-dryrun-{pre,post}.rc`、`watcher-rollback-image.txt`、`watcher-db-backup.sha256`、（仅 W-6）`watcher-db-owner.txt`、`watcher-db-replaced.files`、`watcher-db-replaced.sha256`、`watcher-recreate.at`、`watcher-ingest-count.txt`、`oq-env-backup.sha256`、`cp-units-before/after.txt`，以及执行者保存的各阶段完整输出。
- app：不部署。A-0 真机联调只在用户明确说"现在可以用"后进行（O0-A10）。
- 快照开关仍为 0；副本仍在；打开开关按 `o0-runbook-snapshot-switch.md`。

## 7. 故障对照

| 现象 | 最可能的原因 | 处置 |
|---|---|---|
| 守卫报 `FLEET_BASELINE_STALE` 或 `FLEET_UNCOMPARABLE`，拒绝开始 | 有节点心跳冻结，或节点集合与 `O0_FLEET_NODES` 不一致 | 不开始；按 `docs/agent-operations.md` 排障或核对 S-00，由用户决定 |
| 阶段 C 之后守卫报 `FLEET_CHANGED` | Caddy restart 瞬断节点通道（已知） | 停止后续阶段，报用户；恢复只由用户决定（O0-A06） |
| apply 报 `GATE_FAIL` | 没跑 preflight/build、门禁过期、候选或输入文件变了 | 重跑对应的门禁阶段；不要绕过 |
| 任何 `--execute` 一开始就报 `FLEET_PARAMS_INCONSISTENT` | 导出的节点心跳参数或守卫阈值不满足 §0 的规则 | 按 §0 算出整组值，用户确认后再导出；不单独放宽某个阈值 |
| O-1/O-2 报 `CP_ISOLATION_FAILED` 或 `CP_ISOLATION_UNCOMPARABLE` | node-control/event-ingest 加载了 `operator-query.env` 或带 `WATCHER_*TOKEN`；operator-query 不在 `--cp-root` 下运行；单元不存在 | 阻断阶段 O，报用户；对照 S-10、S-13 |
| W-6 报 `DB_REPLACED_NOT_EMPTY` | 上一次 restore-db 移走的原库还在 `db-replaced/` | 先查清那一次的结果（`auto-rollback.log`），按 W-6 手工路径处理完再重跑；不要删除那些文件 |
| W-6 报 `ROLLBACK FAILED` | 自动恢复中某一步失败（原库 sha 或属主不符、启动失败） | 立即报用户；按 W-6 手工路径 |
| apply 报 `CANDIDATE_IMAGE_MISSING` | build 没跑或镜像被清理 | 回到 W-2；此时没有任何服务被停 |
| 阶段 C 之后站点 401 循环 | basic auth 块被改坏 | C-4 回滚 |
| 新 watcher 反复重启或 `TELEGRAM_NOT_RECONNECTED` | 凭据不合规、DB 路径变量冲突、首次迁移拿锁超时、会话失效 | 自动回滚已触发；读 `docker logs` 首行错误（只含变量名）；对照 S-05、S-15 |
| 站点正常但图片不显示 | `/media/*` 没进 basicauth 块 | 修候选 Caddyfile，重走阶段 C（新授权） |
| C-1 verify 报 `shadow …` 或 `wgw routes are not in the top-level route list` | `import watcher_gateway_routes` 写在某个 handle 之后或包进了块里；站点顶层有 `rewrite`/`uri`/`redir`/`basic_auth`/`forward_auth`/`request_header` 等可能命中前缀的指令；全局 `order` 把某条指令移到了 handle 之前 | 按 §3.1 第 3、4 条改候选；白名单只能经契约修订扩充，O-0 不加例外 |
| C-1 启动即报 `--candidate must be inside the stage dir` 或缺 `--probe-candidate-sha256` | 候选指向了 `/etc/caddy` 或 `$S` 之外；没带 O0-A05P 探针的两个 sha | 候选只放 `$S/caddy/`；从探针证据末行抄两个 sha。什么都没写 |
| C-1 报 `PROBE_BINDING_FAILED` | staging 里的候选（或 bundle 片段）不是探针测过的那份：探针之后改过候选，或放错了副本 | 对当前候选重做 O0-A05P；不要改 sha 参数去凑 |
| C-1 报 `LIVE_SNIPPET_DIFFERS`（wac-092） | `/etc/caddy/caddy-watcher-gateway.caddy` 已存在但不是 bundle 的片段，或是悬空软链 | 什么都没写。报用户：先弄清这个文件从哪来，不覆盖、不删除 |
| O0-A05P 末行 `CADDY_PROBE_INTERRUPTED …`（wac-092） | 探针被 Ctrl-C、SIGTERM 或 SIGHUP 打断 | 不算通过。`ls -A` 确认副本目录里没有 `.o0probe`，再重跑探针 |
| O0-A05P 没有末行、终端被关或探针被 `kill -9`（wac-094） | SIGKILL 或断电：`.o0probe`、`$TMPDIR/o0-caddy-probe-xdg-*`、一个孤儿 Caddy 都可能还在 | 做 §3.1 第 7 条"完成之后"的三项检查；或直接在同一 shell 里重跑探针，它会先打印 `PROBE_RESIDUE_CLEANED` 并清掉自己的残留，重跑之后仍做三项检查 |
| O0-A05P 报 `CADDY_PROBE_FAILED … .o0probe already exists`（wac-094） | 副本旁有上一次留下的 `.o0probe`（被杀或 `--keep`），内容无法证明是本工具这次记录的 | 它含哈希：用户看过后 `rm -f`，再重跑；探针不代删 |
| O0-A05P 打印 `PROBE_RESIDUE_FOREIGN` 或 `PROBE_RESIDUE_ORPHAN`（wac-094） | `$TMPDIR` 里有不带本工具属主记录的探针目录（旧版本留下的），或有命令行带 `o0-caddy-probe-xdg-` 却无法证明归属的进程 | 不影响本次探针结论；按"完成之后"第 2、3 项由用户确认后删除或 `kill`，Agent 不代删 |
| verify 报 `shadow … hits 'the /m/v1/watcher prefix space …'`（wac-094） | 站点顶层某条可能改写请求的指令，匹配器只对某个参数值、后缀、中段 glob 或不锚定正则生效 | 改候选（把它限定到前缀之外，或移出 jp-bot 站点块）；不放宽检查 |
| O0-A05P 报 `FAIL pin: …` 或 `CADDY_PROBE_FAILED … caddy was NOT started` | 副本里有无法改写到本机桩的上游（`dynamic_upstreams`）或其他不能证明只在本机的配置 | 报 Planner 与用户；不删配置去凑 |
| C-1 报 `LIVE_SNIPPET` 不符或 SN3 类拒绝 | `/etc/caddy/caddy-watcher-gateway.caddy` 已存在且不是本 bundle 的片段 | 不覆盖；查清来源后报用户 |
| 网关 503 `gateway_disabled` / `watcher_unavailable` | operator-query 没拿到或撞值；两边值不一致或 watcher 不可达 | O-2 内已自动回滚；重跑凭据 `check` |
| 网关每个请求 500 | `.venv-cp` Python < 3.11 | O-4 回滚；S-12 本应拦住 |
