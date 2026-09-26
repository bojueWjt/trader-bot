# O-0 watcher 服务凭据 runbook：生成、下发、轮换、回滚（wac-032 草案）

> 草案，未执行。凭据值**全程不打印、不进命令行参数、不进日志、不提交**；工具输出只有变量名、文件名和 PASS/FAIL。app 与用户不接触这些凭据（计划 §2.1"统一密钥无感"）。
> 依据：计划 §2.1、§4.2、§9 O-0；契约 §9.2（E-02、E-15、E-16）；审查 wac-007 🟡-3、wac-001 💭-4、wac-009 🟡-5。
> 工具：`scripts/ops/o0/o0_watcher_credentials.py`（`generate`/`rotate`/`retire`/`check`/`apply`/`selftest`），探针 `scripts/ops/o0/o0_tool.py http-probe`（从 env 文件在进程内读取凭据，只打印状态码与 `code`）。

## 0. 凭据与持有方

| 身份 | watcher 侧（校验，当前 + 上一值） | 持有方与文件 | 出示方式 |
|---|---|---|---|
| gateway | `WATCHER_GATEWAY_TOKEN`、`WATCHER_GATEWAY_TOKEN_PREVIOUS` | operator-query：`/srv/trader-v3/secrets/control-plane/operator-query.env` 的 `WATCHER_GATEWAY_TOKEN` | `Authorization: Bearer` |
| snapshot | `WATCHER_SNAPSHOT_TOKEN`、`WATCHER_SNAPSHOT_TOKEN_PREVIOUS` | operator-query 同一文件的 `WATCHER_SNAPSHOT_TOKEN` | `Authorization: Bearer` |
| browser | `WATCHER_BROWSER_PROXY_TOKEN`、`WATCHER_BROWSER_PROXY_TOKEN_PREVIOUS` | Caddy：`/etc/caddy/v3.env` 的 `WATCHER_BROWSER_PROXY_TOKEN`，Caddyfile 只引用 `{env.WATCHER_BROWSER_PROXY_TOKEN}` | `X-Watcher-Proxy-Auth` |

watcher 侧六个变量放在 `/srv/trader-secrets/watcher-gateway.env`（root 0600），由 compose `env_file` 注入（前置 P-04）。健康检查脚本在进程内读 `process.env`，compose `test:` 不插值（E-16）。

**生成规则**（工具强制）：
- 字母表 `[A-Za-z0-9_-]`、长度 ≥ 32（`secrets.token_urlsafe(32)`，43 字符）。这是契约格式 `^[\x21-\x7E]{32,}$` 的子集，同时避开 Caddyfile 的 `{}`/`#`、compose 的 `$`、systemd 的 `%`（wac-007 🟡-3）。`check` 两种格式都校验。
- watcher 侧全部已配置值（最多六个）两两互异；空串等于未配置，永不参与匹配（E-02）。
- 与控制面 token 目录互异：`RISK_ADMIN_TOKEN`、`VIEWER_TOKEN`、`REVIEWER_TOKEN`、`SYSTEM_OBSERVER_TOKEN`、`NAUTILUS_NODE_TOKEN`、`SIGNAL_TOKEN_ACCOUNT_A..D`、`CONTROL_PLANE_AUTH_SECRET`、`NAUTILUS_NODE_AUTH_JSON` 里每个节点 token。只比较 SHA-256 摘要；目录为空判"无法比较"并失败（E-15、wac-001 💭-4）。
- watcher 的 env 里不得出现任何控制面密钥变量名（计划 §2.1）。

**文件与清理**：凭据集目录 `/srv/trader-staging/o0-<UTC>/creds/<set>/`（0700，文件 0600）。三份文件：`watcher.env`（六个变量）、`operator-query.env`（gateway + snapshot 当前值）、`caddy.env`（browser 当前值）。上线验证通过后，staging 里的凭据集与含凭据的 env 备份（`*.env.bak`）只保留到回滚窗口结束（建议 7 天），删除作为授权项 O0-A18 的一部分。

## 1. 首次签发（随部署进行）

| 步 | 授权 | 命令（`$S` = staging，`$T` = `$S/bundle/tools`） | 验证 | 回滚 |
|---|---|---|---|---|
| I-1 生成 | O0-A03 | `python3 $T/o0_watcher_credentials.py generate --out-dir $S/creds/set-initial` | 输出 `GENERATED set=set-initial files=watcher.env,operator-query.env,caddy.env`；`stat` 三个文件 0600 | 删除 `$S/creds/set-initial` |
| I-2 校验 | O0-A03 | `python3 $T/o0_watcher_credentials.py check --watcher-env $S/creds/set-initial/watcher.env --holder-env $S/creds/set-initial/operator-query.env --holder-env $S/creds/set-initial/caddy.env --catalog-env /srv/trader-v3/secrets/control-plane/operator-query.env --require-catalog`（其他控制面 env 文件逐个追加 `--catalog-env`，文件清单来自 S-10/S-11） | `CREDENTIAL_CHECK_OK`，并含 `cross-service distinct: 3 watcher values vs N catalog values`（N > 0） | 重新生成 |
| I-3 Caddy 持有 browser | O0-A05（阶段 C） | `apply --fragment $S/creds/set-initial/caddy.env --target /etc/caddy/v3.env --execute --backup-dir …`（由阶段 C 脚本执行） | 阶段 C verify：浏览器样本注入的是 `{env.WATCHER_BROWSER_PROXY_TOKEN}` 占位符 | 阶段 C 回滚还原 v3.env |
| I-4 watcher 接受三个身份 | O0-A07（阶段 W） | `apply --fragment …/watcher.env --target /srv/trader-secrets/watcher-gateway.env --create --execute …`，随后 compose 重建 | 无凭据 401；browser 凭据 200 | 阶段 W 回滚（旧镜像不读这些变量） |
| I-5 operator-query 持有 gateway 与 snapshot | O0-A08（阶段 O） | `apply --fragment …/operator-query.env --target /srv/trader-v3/secrets/control-plane/operator-query.env --execute …`，随后只重启 operator-query | 网关 `SYSTEM_OBSERVER_TOKEN` 探针 200 | 阶段 O 回滚还原 env 文件 |

注意：I-3 早于 I-4（Caddy 先注入，旧 watcher 忽略）；I-5 晚于 I-4（网关只打到已鉴权的 watcher）。首次签发没有 `*_PREVIOUS`。

## 2. 不打印凭据的验证方法

| 要验证什么 | 命令 | 判定 |
|---|---|---|
| 三份文件互相一致、格式合规、与控制面目录互异 | `check …`（见 I-2） | `CREDENTIAL_CHECK_OK` |
| watcher 接受某个 gateway 值 | `python3 $T/o0_tool.py http-probe --url http://127.0.0.1:9090/api/status --token-env-file <文件> --token-var WATCHER_GATEWAY_TOKEN --expect-status 400` | **400** `invalid_actor_headers` = 凭据被接受（身份已建立，缺 actor 头在 W4 被拒）；**401** = 凭据被拒。这个探针不经网关，不产生任何写 |
| watcher 接受某个 snapshot 值 | `http-probe --url http://127.0.0.1:9090/api/trading/config-snapshot --token-env-file <文件> --token-var WATCHER_SNAPSHOT_TOKEN --expect-status 200` | 200 接受 / 401 拒绝（只打印状态码，不打印快照内容） |
| watcher 接受某个 browser 值 | `http-probe --url http://127.0.0.1:9090/api/status --token-env-file <文件> --token-var WATCHER_BROWSER_PROXY_TOKEN --proxy-header X-Watcher-Proxy-Auth --expect-status 200` | 200 / 401 |
| 网关整体可用 | `http-probe --url http://127.0.0.1:8183/v1/watcher/status --token-env-file /srv/trader-v3/secrets/control-plane/operator-query.env --token-var SYSTEM_OBSERVER_TOKEN --expect-status 200` | 200；503 `watcher_unavailable` = 两边值不一致或 watcher 不可达 |
| 请求与审计正常 | watcher 日志中该窗口内的 401 计数（`docker logs --since <t> trader-watcher-1 2>&1 \| grep -c ' 401 '`，按 W-0 的日志格式调整）；operator-query 日志中 `watcher_unavailable` 计数；若窗口内有用户自然发生的写入，`config_audit` 最新行的 `source`、`actor` 正确（只读查询，只选 `id, actor, source, operation, status_code, created_at` 列） | 计数为 0；审计行形状正确。**不为验证去制造写入**（计划 §6.2：生产不做破坏性 CRUD） |

## 3. 轮换（计划 §2.1 顺序：watcher 先接受新旧两值 → 持有方切新值 → 确认请求与审计正常 → 撤旧值）

以 gateway 为例；snapshot、browser 相同，差别在"持有方"一步（见表后）。每一步单独授权（O0-A20 … O0-A23），每一步后都记录舰队状态。

| 步 | 授权 | 操作 | 验证 | 该步回滚 |
|---|---|---|---|---|
| R-1 生成新值 | O0-A20 | `python3 $T/o0_watcher_credentials.py rotate --identity gateway --from-dir $S/creds/<当前集> --out-dir $S/creds/<新集>`；`check --watcher-env <新集>/watcher.env --holder-env <当前集>/operator-query.env --allow-holder-on-previous --catalog-env … --require-catalog` | `ROTATED identity=gateway`；`CREDENTIAL_CHECK_OK`，并有 `== watcher previous (rotation step 1)` | 删除新集 |
| R-2 watcher 同时接受新旧 | O0-A21 | `apply --fragment <新集>/watcher.env --target /srv/trader-secrets/watcher-gateway.env --execute --backup-dir …`；`docker compose -p trader -f /srv/trader/docker-compose.yml up -d --no-deps --force-recreate --no-build watcher` | 旧值探针 400（接受）、新值探针 400（接受）；网关探针仍 200（operator-query 还持旧值）；watcher 重启计数 0 | 还原 env_file 备份并重建 watcher（只接受旧值） |
| R-3 持有方切新值 | O0-A22 | `apply --fragment <新集>/operator-query.env --target /srv/trader-v3/secrets/control-plane/operator-query.env --execute --backup-dir …`；`systemctl restart trader-v3-controlplane-operator-query`（只这一个） | 网关探针 200；node-control/event-ingest MainPID 不变 | 还原 operator-query env 备份并只重启 operator-query（watcher 仍接受旧值，所以回滚无中断） |
| R-4 确认 | O0-A22（只读） | 观察至少 10 分钟：§2 的计数与审计核对；app 端（若用户在用）无报错（计划 §4.2"无感"） | 计数为 0 | 回到 R-3 回滚 |
| R-5 撤旧值 | O0-A23 | `python3 $T/o0_watcher_credentials.py retire --from-dir $S/creds/<新集> --out-dir $S/creds/<撤后集>`；`check --watcher-env <撤后集>/watcher.env --holder-env <撤后集>/operator-query.env --catalog-env … --require-catalog`；`apply` 到 watcher env_file；重建 watcher | 旧值探针 **401**（已撤销，计划 §4.2"旧 token 撤销"）；新值探针 400；网关探针 200 | 还原 R-2 后的 env_file（新旧都接受）并重建 watcher |

**不同身份的持有方一步**：
- snapshot：持有方同为 operator-query 文件中的 `WATCHER_SNAPSHOT_TOKEN`。若快照开关已打开，R-3 之前和之后都看 C-0 状态：持有方一旦出示 watcher 不接受的值，快照会锁存 `unauthorized` 并立即拒绝开仓（契约 §9.11，直到下一次成功验证）。所以**绝不能**在 R-2 之前做 R-3，也绝不能在 R-3 之前做 R-5。
- browser：持有方是 Caddy。R-3 = `apply` 到 `/etc/caddy/v3.env` + 带 env `caddy validate` + `systemctl restart caddy`（禁 reload）。**这一步带 D-02 的舰队 HALT 风险**，需在授权里单独写明；验证用阶段 C 的公网探针与浏览器核对。

**顺序错误的后果与处置**：
- R-5 早于 R-3（撤掉了持有方正在用的值）：网关 503 `watcher_unavailable`（app 的信号页与交易配置页提示"采集服务不可达"；交易端点不受影响）；snapshot 则锁存 `unauthorized` 拒开仓。处置：立即把 watcher env_file 还原到 R-2 状态（新旧都接受）并重建。
- 持有方拿到的是 watcher 不认识的值：`check --holder-env` 会在 R-3 之前报 `matches no configured watcher value`，不应走到重启。

**疑似泄露的紧急撤销**（需用户单独决定）：跳过双值期，直接生成新值后先改 watcher（只接受新值）再改持有方。代价是两次重启之间网关 503（或快照锁存拒开仓），换来旧值立即失效。browser 值泄露的影响有限（外部请求仍要先过 basic auth），gateway 值泄露意味着拿到它的人可以绕过网关直接调用 watcher（仅限在 jp-24 本机或能访问 127.0.0.1:9090 的地方）。

## 4. 与控制面 token 轮换的交叉影响

控制面 token（`RISK_ADMIN_TOKEN` 等）以后任何一次轮换，都要重跑 `check --catalog-env …`，确认新的控制面值没有与 `WATCHER_*` 相撞；operator-query 启动时的纵深检查（E-15）会在撞值时把网关视为未配置（503 `gateway_disabled`），快照开关打开时甚至启动失败。
