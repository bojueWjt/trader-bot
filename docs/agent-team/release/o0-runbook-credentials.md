# O-0 watcher 服务凭据 runbook：生成、下发、轮换、回滚（wac-032 草案，wac-045 修订，wac-105 适配 WGW-1.0.4）

> **WGW-1.0.4（契约 §9.14.6，wac-105）**：gateway 凭据的持有方从 operator-query 改为控制面新角色 **watcher-gateway**：线上文件 `/srv/trader-v3/secrets/control-plane/watcher-gateway.env`（0600，按白名单写入 `WATCHER_GATEWAY_TOKEN` 与四个 reader token，由 `o0_tool.py wgw-env` 生成，只打印变量名）；凭据集里对应的持有方文件叫 `controlplane-watcher-gateway.env`（不要与 watcher 容器自己的 `/srv/trader-secrets/watcher-gateway.env` 混淆）。operator-query **不再持有** gateway 值，只在快照切换阶段加入 snapshot 值；gateway 轮换的持有方一步只重启 watcher-gateway 单元，不重启 operator-query。下文凡与此冲突的，以本段为准。

> 草案，未执行。凭据值**全程不打印、不进命令行参数、不进日志、不提交**；工具输出只有变量名、文件名和 PASS/FAIL。app 与用户不接触这些凭据（计划 §2.1"统一密钥无感"）。
> 依据：计划 §2.1、§4.2、§9 O-0；契约 §9.2（E-02、E-15、E-16）；审查 wac-007 🟡-3、wac-001 💭-4、wac-009 🟡-5、wac-032 🟡-6/🟡-16。
> 工具：`o0_watcher_credentials.py`（`generate`/`rotate`/`retire`/`check`/`apply`/`selftest`）、`o0_tool.py http-probe`（从 env 文件在进程内读取凭据，只打印状态码与 `code`）、`o0_fleet_guard.sh`（每一步前后的舰队守卫，授权号与步骤绑定）。
> 记号：`$S` = `/srv/trader-staging/o0-<UTC>`，`$T` = `$S/bundle/tools`，`$CAT` = 每个控制面 env 文件各一个 `--catalog-env <文件>`（清单来自 S-10/S-11，至少包含 `/srv/trader-v3/secrets/control-plane/operator-query.env`）。

## 0. 凭据与持有方

| 身份 | watcher 侧（校验，当前 + 上一值） | 持有方与文件 | 出示方式 |
|---|---|---|---|
| gateway | `WATCHER_GATEWAY_TOKEN`、`WATCHER_GATEWAY_TOKEN_PREVIOUS` | **watcher-gateway**（WGW-1.0.4）：`/srv/trader-v3/secrets/control-plane/watcher-gateway.env` 的 `WATCHER_GATEWAY_TOKEN`；凭据集文件 `controlplane-watcher-gateway.env` | `Authorization: Bearer` |
| snapshot | `WATCHER_SNAPSHOT_TOKEN`、`WATCHER_SNAPSHOT_TOKEN_PREVIOUS` | operator-query：`/srv/trader-v3/secrets/control-plane/operator-query.env` 的 `WATCHER_SNAPSHOT_TOKEN`（快照切换阶段才写入） | `Authorization: Bearer` |
| browser | `WATCHER_BROWSER_PROXY_TOKEN`、`WATCHER_BROWSER_PROXY_TOKEN_PREVIOUS` | Caddy：`/etc/caddy/v3.env` 的 `WATCHER_BROWSER_PROXY_TOKEN`，Caddyfile 只引用 `{env.WATCHER_BROWSER_PROXY_TOKEN}` | `X-Watcher-Proxy-Auth` |

watcher 侧六个变量放在 `/srv/trader-secrets/watcher-gateway.env`（root 0600），由 compose `env_file` 注入（wac-040，已合入）。健康检查脚本在进程内读 `process.env`，compose `test:` 不插值（E-16）。**每个进程只持自己需要的那一个**：S-10 断言 node-control 与 event-ingest 的 EnvironmentFiles 不含 `operator-query.env`（否则 O-2 之后它们也持有 gateway/snapshot 值）。

**生成规则**（工具强制）：字母表 `[A-Za-z0-9_-]`、长度 ≥ 32（43 字符）；watcher 侧已配置值两两互异；与控制面 token 目录只按 SHA-256 摘要比对且目录为空判"无法比较"而失败；watcher 的 env 里不得出现控制面密钥变量名。

**文件与清理**：凭据集目录 `$S/creds/<set>/`（0700，文件 0600）：`watcher.env`（六个变量）、`controlplane-watcher-gateway.env`（gateway 当前值）、`operator-query.env`（snapshot 当前值）、`caddy.env`（browser 当前值）。staging 里的凭据集与含凭据的 env 备份只保留到回滚窗口结束（建议 7 天），删除属于 O0-A18。

## 1. 首次签发（随部署进行）

| 步 | 授权 | 命令 | 验证 | 回滚 |
|---|---|---|---|---|
| I-1 生成 | O0-A03 | `python3 $T/o0_watcher_credentials.py generate --out-dir $S/creds/set-initial` | `GENERATED set=set-initial files=watcher.env,controlplane-watcher-gateway.env,operator-query.env,caddy.env`；四个文件 0600 | 删除 `$S/creds/set-initial` |
| I-2 校验 | O0-A03 | `python3 $T/o0_watcher_credentials.py check --watcher-env $S/creds/set-initial/watcher.env --holder-env $S/creds/set-initial/controlplane-watcher-gateway.env --holder-env $S/creds/set-initial/operator-query.env --holder-env $S/creds/set-initial/caddy.env $CAT --require-catalog` | `CREDENTIAL_CHECK_OK`，含 `cross-service distinct: 3 watcher values vs N catalog values`（N > 0） | 重新生成 |
| I-3 Caddy 持有 browser | O0-A05（部署 runbook C-2） | 由阶段 C 脚本 `apply` 执行，随后用摘要确认 v3.env 持有 watcher 当前值 | 阶段 C verify：注入的是 `{env.WATCHER_BROWSER_PROXY_TOKEN}` 占位符 | 阶段 C 回滚还原 v3.env |
| I-4 watcher 接受三个身份 | O0-A07（部署 runbook W-3） | 由阶段 W 脚本写 env_file、确认两个持有方与之一致、重建 | 无凭据 401；browser 凭据 200 | 阶段 W 回滚（env_file 回到 apply 前状态） |
| I-5 watcher-gateway 持有 gateway | O0-A08（部署 runbook O-1、O-2） | 阶段 O 脚本 preflight 用 `wgw-env` 在 staging 按白名单生成 env（gateway 值取自 `controlplane-watcher-gateway.env`，四个 reader token 取自线上 operator-query.env；只打印变量名），apply 以 0600 装到 `secrets/control-plane/watcher-gateway.env` 并启动新单元；**不改 operator-query.env、不重启 operator-query** | 8186 上 `SYSTEM_OBSERVER_TOKEN` 探针 200（在自动回滚窗口内）；`wgw-env --check` | 阶段 O 回滚删除该 env 与新单元 |

I-3 早于 I-4（Caddy 先注入，旧 watcher 忽略）；I-5 晚于 I-4（网关只打到已鉴权的 watcher）。首次签发没有 `*_PREVIOUS`。I-3/I-4/I-5 的舰队守卫由阶段脚本自己完成。

## 2. 不打印凭据的验证方法

| 要验证什么 | 命令 | 判定 |
|---|---|---|
| 三份文件互相一致、格式合规、与控制面目录互异 | `check …`（见 I-2） | `CREDENTIAL_CHECK_OK` |
| watcher 接受某个 gateway 值 | `python3 $T/o0_tool.py http-probe --url http://127.0.0.1:9090/api/status --token-env-file <文件> --token-var WATCHER_GATEWAY_TOKEN --expect-status 400 --expect-code invalid_actor_headers` | **400 且 `code=invalid_actor_headers`** = 凭据被接受（身份已建立，缺 actor 头在 W4 被拒）；其他原因的 400 不算；**401** = 凭据被拒。这个探针不经网关，不产生任何写 |
| watcher 接受某个 snapshot 值 | `http-probe --url http://127.0.0.1:9090/api/trading/config-snapshot --token-env-file <文件> --token-var WATCHER_SNAPSHOT_TOKEN --expect-status 200` | 200 接受 / 401 拒绝（只打印状态码，不打印快照内容） |
| watcher 接受某个 browser 值 | `http-probe --url http://127.0.0.1:9090/api/status --token-env-file <文件> --token-var WATCHER_BROWSER_PROXY_TOKEN --proxy-header X-Watcher-Proxy-Auth --expect-status 200` | 200 / 401 |
| 网关整体可用 | `http-probe --url http://127.0.0.1:8186/v1/watcher/status --token-env-file /srv/trader-v3/secrets/control-plane/watcher-gateway.env --token-var SYSTEM_OBSERVER_TOKEN --expect-status 200`（WGW-1.0.4：网关在 8186） | 200；503 `watcher_unavailable` = 两边值不一致或 watcher 不可达 |
| 旧值已撤销 | 用撤销前集合的文件做 gateway 探针，`--expect-status 401` | 401 |
| 请求与审计正常 | watcher 日志中该窗口内的 401 计数（`docker logs --since <t> trader-watcher-1 2>&1 \| awk '/ 401 /{c++} END{print c+0}'`，按 W-0 的日志格式调整）；operator-query 日志中 `watcher_unavailable` 计数；若窗口内有用户自然发生的写入，`config_audit` 最新行的 `source`、`actor` 正确（只选 `id, actor, source, operation, status_code, created_at` 列） | 计数为 0；审计行形状正确。**不为验证去制造写入**（计划 §6.2） |

## 3. 轮换（计划 §2.1 顺序：watcher 先接受新旧两值 → 持有方切新值 → 确认请求与审计正常 → 撤旧值）

以 gateway 为例；snapshot、browser 相同，差别在"持有方"一步（见表后）。每一步单独授权（O0-A20 … O0-A23；演练用 O0-A11 与 `drill-` 步骤名），**每一步前后都跑舰队守卫**：

```text
前：bash $T/o0_fleet_guard.sh --execute --phase <步骤> --auth-id <该步骤的号> --stage-dir $S --action before
后：bash $T/o0_fleet_guard.sh --execute --phase <步骤> --auth-id <该步骤的号> --stage-dir $S --action after
```

`before` 在节点集合不对或任何心跳 ≥ 5 秒时拒绝（退出码 2）：不开始这一步。`after` 等稳定窗后多次采样，有变化即退出码 3：停下、报用户、不 RESUME。步骤的回滚之前改用 `--action record`（只记录，不设门槛），回滚之后再跑 `--action after`。

| 步 | 授权 | 操作 | 验证 | 该步回滚 |
|---|---|---|---|---|
| R-1 生成新值 | O0-A20 | `bash $T/o0_fleet_guard.sh --execute --phase R-1 --auth-id O0-A20 --stage-dir $S --action before`；`python3 $T/o0_watcher_credentials.py rotate --identity gateway --from-dir $S/creds/<当前集> --out-dir $S/creds/<新集>`；`python3 $T/o0_watcher_credentials.py check --watcher-env $S/creds/<新集>/watcher.env --holder-env $S/creds/<当前集>/operator-query.env --allow-holder-on-previous $CAT --require-catalog`；`bash $T/o0_fleet_guard.sh --execute --phase R-1 --auth-id O0-A20 --stage-dir $S --action after` | `ROTATED identity=gateway`；`CREDENTIAL_CHECK_OK`，并有 `== watcher previous (rotation step 1)`；守卫不变 | 删除新集 |
| R-2 watcher 同时接受新旧 | O0-A21 | `bash $T/o0_fleet_guard.sh --execute --phase R-2 --auth-id O0-A21 --stage-dir $S --action before`；`python3 $T/o0_watcher_credentials.py apply --fragment $S/creds/<新集>/watcher.env --target /srv/trader-secrets/watcher-gateway.env --execute --backup-dir $S/backup-rotate-R2`；`python3 $T/o0_watcher_credentials.py check --watcher-env /srv/trader-secrets/watcher-gateway.env --holder-env $S/creds/<当前集>/operator-query.env --allow-holder-on-previous $CAT --require-catalog`；`docker compose -p trader -f /srv/trader/docker-compose.yml up -d --no-deps --force-recreate --no-build watcher`；等 `[watcher] Connected, listening...`；`bash $T/o0_fleet_guard.sh --execute --phase R-2 --auth-id O0-A21 --stage-dir $S --action after` | 旧值探针、新值探针都是 400 `invalid_actor_headers`（接受）；网关探针仍 200（operator-query 还持旧值）；watcher 重启计数 0；Telegram 重连；守卫不变 | `--action record`；还原 `backup-rotate-R2` 里的 env_file 并重建 watcher（只接受旧值）；`--action after` |
| R-3 持有方切新值 | O0-A22 | `bash $T/o0_fleet_guard.sh --execute --phase R-3 --auth-id O0-A22 --stage-dir $S --action before`；`python3 $T/o0_watcher_credentials.py apply --fragment $S/creds/<新集>/operator-query.env --target /srv/trader-v3/secrets/control-plane/operator-query.env --execute --backup-dir $S/backup-rotate-R3`；`python3 $T/o0_watcher_credentials.py check --watcher-env /srv/trader-secrets/watcher-gateway.env --holder-env /srv/trader-v3/secrets/control-plane/operator-query.env --allow-holder-on-previous $CAT --require-catalog`；`systemctl restart trader-v3-controlplane-operator-query`（只这一个）；`bash $T/o0_fleet_guard.sh --execute --phase R-3 --auth-id O0-A22 --stage-dir $S --action after` | 网关探针 200；node-control/event-ingest MainPID 不变；守卫不变 | `--action record`；还原 `backup-rotate-R3` 的 env 并只重启 operator-query（watcher 仍接受旧值，所以回滚无中断）；`--action after` |
| R-4 确认 | O0-A22（只读） | `bash $T/o0_fleet_guard.sh --execute --phase R-4 --auth-id O0-A22 --stage-dir $S --action before`；观察至少 10 分钟：§2 的计数与审计核对；app 端（若用户在用）无报错；`bash $T/o0_fleet_guard.sh --execute --phase R-4 --auth-id O0-A22 --stage-dir $S --action after` | 计数为 0；守卫不变 | 回到 R-3 回滚 |
| R-5 撤旧值 | O0-A23 | `bash $T/o0_fleet_guard.sh --execute --phase R-5 --auth-id O0-A23 --stage-dir $S --action before`；`python3 $T/o0_watcher_credentials.py retire --from-dir $S/creds/<新集> --out-dir $S/creds/<撤后集>`；`python3 $T/o0_watcher_credentials.py check --watcher-env $S/creds/<撤后集>/watcher.env --holder-env /srv/trader-v3/secrets/control-plane/operator-query.env $CAT --require-catalog`；`apply --fragment $S/creds/<撤后集>/watcher.env --target /srv/trader-secrets/watcher-gateway.env --execute --backup-dir $S/backup-rotate-R5`；重建 watcher；等 Telegram 重连；`bash $T/o0_fleet_guard.sh --execute --phase R-5 --auth-id O0-A23 --stage-dir $S --action after` | 旧值探针 **401**（已撤销）；新值探针 400 `invalid_actor_headers`；网关探针 200；守卫不变 | `--action record`；还原 `backup-rotate-R5` 的 env_file（新旧都接受）并重建 watcher；`--action after` |

**gateway 的持有方一步（WGW-1.0.4）**：R-1 的 `check` 用 `--holder-env $S/creds/<当前集>/controlplane-watcher-gateway.env`；R-3 = 用新集的 `controlplane-watcher-gateway.env` 重新生成并替换 `/srv/trader-v3/secrets/control-plane/watcher-gateway.env`（`o0_tool.py wgw-env` 写到 staging，`install -m 0600` 装上，逐字节比对），再 `check --holder-env /srv/trader-v3/secrets/control-plane/watcher-gateway.env`，然后**只重启 watcher-gateway 单元**（`systemctl restart trader-v3-controlplane-watcher-gateway.service`，新单元，不影响交易路径；绝不重启 operator-query）；验证用 §2"网关整体可用"（8186）；回滚 = 装回备份的 env、只重启 watcher-gateway。R-5 的 `check` 同样换成这份文件。表中 R-1…R-5 命令里的 `operator-query.env` 对 gateway 身份一律改读为上述文件（snapshot 身份仍是 operator-query.env）。

**不同身份的持有方一步**：
- snapshot：持有方同为 operator-query 文件中的 `WATCHER_SNAPSHOT_TOKEN`。若快照开关已打开，持有方一旦出示 watcher 不接受的值，快照会锁存 `unauthorized` 并立即拒绝开仓（契约 §9.11）。所以**绝不能**在 R-2 之前做 R-3，也绝不能在 R-3 之前做 R-5。
- browser：持有方是 Caddy。R-3 = `apply` 到 `/etc/caddy/v3.env` + 带 env `caddy validate` + `systemctl restart caddy`（禁 reload）。**这一步带 D-02 的舰队 HALT 风险**，需在授权里单独写明；守卫的稳定窗与多次采样正是为这一步准备的。

**顺序错误的后果与处置**：
- R-5 早于 R-3：网关 503 `watcher_unavailable`；snapshot 则锁存 `unauthorized` 拒开仓。处置：立即把 watcher env_file 还原到 R-2 状态并重建。
- 持有方拿到的是 watcher 不认识的值：R-3 的 `check` 在重启之前报 `matches no configured watcher value`，不应走到重启。

**疑似泄露的紧急撤销**（需用户单独决定）：跳过双值期，直接生成新值后先改 watcher（只接受新值）再改持有方。代价是两次重启之间网关 503（或快照锁存拒开仓）。每一步照样跑舰队守卫。

## 4. 与控制面 token 轮换的交叉影响

控制面 token 以后任何一次轮换，都要重跑 `check $CAT`，确认新的控制面值没有与 `WATCHER_*` 相撞；operator-query 启动时的纵深检查（E-15）会在撞值时把网关视为未配置（503 `gateway_disabled`），快照开关打开时甚至启动失败。
