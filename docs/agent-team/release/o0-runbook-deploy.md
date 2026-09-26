# O-0 分阶段部署 runbook（wac-032 草案）

> 草案。**没有执行任何生产动作**。每个带 O0-Axx 的步骤都要用户逐项授权后才执行（`o0-authorization-list.md`）。
> 范围：计划 §4.1 第 1 步"新增并验证"落到 jp-24：watcher 镜像（W-0）、控制面 operator-query（C-0 reader 开关保持关 + C-1 网关）、Caddy（浏览器入口清头注入 + `/m/v1/watcher/*` 逐路径）。**app 不需要部署**（A-0 只在 app 集成分支，真机联调另行授权 O0-A10）。快照开关在本 runbook 全程保持关闭；打开开关见 `o0-runbook-snapshot-switch.md`。
> 不做：不重启 node-control、event-ingest；不写控制面库与记账五表；不新增控制面迁移；不发 RESUME；不 reload Caddy；不下单、不平仓、不撤单。

## 0. 阶段顺序与理由

| 顺序 | 阶段 | 为什么排在这里 | 中间态是否安全 |
|---|---|---|---|
| 1 | **C：Caddy** | W-0a 上线后，浏览器请求必须带 Caddy 注入的 `X-Watcher-Proxy-Auth`，否则站点全 401。先让 Caddy 注入，旧 watcher 会忽略这个头 | 安全：旧 watcher 忽略注入头；`/m/v1/watcher/*` 暂时落到旧 operator-query，得到 404 |
| 2 | **W：watcher** | 新 watcher 需要三个当前值才能启动；浏览器身份此时已由 Caddy 提供 | 安全：旧 operator-query 读副本文件，不经 HTTP 访问 watcher；feeder 直读库不受影响 |
| 3 | **O：operator-query** | 网关要打到已经鉴权的新 watcher；先上 O 会让网关在 C/W 之间把请求转给无鉴权的旧 watcher（开发期 wac-009 🟡-7 明确禁止这种候选） | 安全：快照开关关闭，交易路径不引入快照依赖 |

Caddy 只重启一次（阶段 C），operator-query 只重启一次（阶段 O），watcher 只重建一次（阶段 W）。

**舰队状态原则**（计划 §4.2）：每个阶段开始前记录 `fleet-before-*`，结束后记录 `fleet-after-*` 并比较状态列。有任何变化：脚本以退出码 3 停下，保持现场，报用户。恢复交易（RESUME）只属于用户，不是任何阶段的步骤（见授权项 O0-A06）。

**已声明的风险**：`systemctl restart caddy` 会瞬断节点经 `172.30.1.1:8080` 到控制面的通道。2026-08-30 这导致全舰队 fail-closed HALT，2026-08-31 没有。阶段 C 可能让 ACTIVE 节点变 HALTED，这不是"意外 HALT"，而是需要用户事先接受的已知后果（决策 D-02）。

## 1. 开始前必须全部满足

| 门禁 | 证据 | 不满足 |
|---|---|---|
| 本地打包门禁 G1–G10 全部 PASS，`RELEASE.json` 的 `deploy_candidate: true` | `o0_package.sh` 输出与 `logs/` | 不申请任何生产授权。当前 `4fb0c11` 的结果是 G4/G5/G6/G7 失败（P-01、P-02、P-04、P-05） |
| 现场只读核对（`o0-site-checklist.md`）无阻断项 | L-A5 汇总页 | 同上 |
| Planner 已就 D-02（Caddy HALT 风险与窗口）、D-03（`*` 翻译方式）、D-04（共享代码目录）给出用户裁决 | 看板备注 | 同上 |
| 选定低流量窗口：避开信号密集时段；用户在场、手机能收 Telegram 告警 | 用户确认 | 顺延 |

## 2. 准备

| 步 | 授权 | 命令 | 验证 | 回滚 |
|---|---|---|---|---|
| 2.1 本地打包 | 不需要（本机） | `bash scripts/ops/o0/o0_package.sh --candidate <已审定提交> --out <本机目录> --run-tests` | 末行 `PACKAGE_OK`；记下 `o0-bundle-<sha12>.tgz.sha256` | — |
| 2.2 建 staging 并上传 | O0-A02 | `ssh … 'install -d -m 0700 /srv/trader-staging/o0-<UTC>'`；`scp -P 53222 o0-bundle-*.tgz root@100.89.58.40:/srv/trader-staging/o0-<UTC>/`；远端 `sha256sum` 与本地一致后 `tar -xzf` | `cd bundle && sha256sum -c --quiet SHA256SUMS` 退出 0 | 删除该 staging 目录（只影响 staging） |
| 2.3 生成凭据 | O0-A03 | `python3 bundle/tools/o0_watcher_credentials.py generate --out-dir /srv/trader-staging/o0-<UTC>/creds/set-initial`；随后 `check --watcher-env …/watcher.env --holder-env …/operator-query.env --holder-env …/caddy.env --catalog-env /srv/trader-v3/secrets/control-plane/operator-query.env --require-catalog`（另有控制面 env 文件时逐个追加 `--catalog-env`） | `CREDENTIAL_CHECK_OK`；三个文件 0600 | 删除 `creds/set-initial`（值从未离开该目录） |

凭据细节与轮换见 `o0-runbook-credentials.md`。

## 3. 阶段 C：Caddy（授权 O0-A05）

脚本：`bundle/tools/o0_deploy_caddy.sh`，阶段 `preflight` → `apply` → `verify`（失败或需要时 `rollback`）。

### 3.1 准备候选 Caddyfile（人工，在 staging 里完成，不碰 `/etc/caddy`）

1. `cp -p /etc/caddy/Caddyfile /srv/trader-staging/o0-<UTC>/caddy/Caddyfile.candidate`
2. 在 jp-bot 站点块里、现有 `/m` 移动端 handle 旁边，粘贴 `bundle/caddy/watcher-gateway.caddy`（由 `o0_caddy_watcher_routes.py render` 从生成清单产出，16 个锚定 `path_regexp` + `method` 具名匹配器）。**不要**加任何 `/m/*`、`/m/v1/*` 兜底；不要贴 `{param}`（wac-026 🟡-1 a/c/d）。若 Planner 在 D-03 选了"接受前缀"，改用前缀写法并在 verify 加 `--accept-prefix`。
3. 在现有 watcher basicauth handle 内、`basic_auth` 之后、`reverse_proxy` 之前加入（保持现有匹配器与 `uri strip_prefix /watcher` 不变）：

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

   说明：`request_header` 在指令顺序上先于 `reverse_proxy` 执行，所以是"先清后注入"；**不要**在 `reverse_proxy` 里写 `header_up -X-Watcher-*`（反代头操作中 delete 可能晚于 set 执行，会把注入值删掉；verify 会判失败）。必须用运行时占位符 `{env.…}`，不能用 `{$…}`（后者把明文写进 adapt JSON 与 admin API，wac-007 🟡-3）。
4. 若 S-03 显示 `/media/*` 目前不走 basicauth 块（例如落到 SPA），而站点要显示图片，把 `/media/*` 并入 basicauth 块的匹配器；W-0a 之后 watcher 的 `/media` 也要求代理凭证，不经该块就是 401。

### 3.2 步骤

| 步 | 内容 | 命令（staging 目录记为 `$S`） | sha / 验证 | 失败处置 |
|---|---|---|---|---|
| C-1 前置门禁（只读） | 舰队快照；Caddy 版本；记录线上 Caddyfile 与 v3.env 的 sha；候选脱敏 diff；凭据检查（含控制面目录）；带 env 的 `caddy validate`；`caddy adapt` 到 0600 JSON；按生成清单 verify | `bash $S/bundle/tools/o0_deploy_caddy.sh --execute --auth-id O0-A05 --phase preflight --stage-dir $S` | `CREDENTIAL_CHECK_OK`；`Valid configuration`；`CADDY_WATCHER_ROUTES_OK lines=16`；人工确认脱敏 diff 只动了 §3.1 的两处 | 任一失败：中止，线上未改任何东西 |
| C-2 应用 | 备份 Caddyfile 与 v3.env（0700 目录，保留属主与权限）；把 `WATCHER_BROWSER_PROXY_TOKEN` 合入 v3.env；安装候选；再次带 env validate；`systemctl restart caddy`；`is-active`；舰队快照并比较 | `… --phase apply …` | 备份 sha 写入 `evidence/caddy-backup.sha256`；`FLEET_STATUS_UNCHANGED` | 任一命令失败：ERR trap 自动回滚（还原两份文件、sha 校验回到 C-1 记录值、validate、restart）。舰队状态变化：退出码 3，**不回滚也不 RESUME**，立即报用户（HALT 是 restart 的已知后果，回滚会再 restart 一次） |
| C-3 验证（只读） | admin API 取运行中配置到 0600 JSON，再跑同一 verify；本机回环 `--resolve` 公网探针：`/m/v1/accounts` 无 token 401、伪 token 403；`/m/v1/watcher/status` 404；`/watcher/`、`/api/status` 401；`/media/<名>` 不得无认证返回图片；内部 `:8080` 有响应；舰队比较 | `… --phase verify …` | `CADDY_WATCHER_ROUTES_OK`；探针全部符合 | 验证失败：执行 C-4 回滚（仍在 O0-A05 授权内） |
| C-4 回滚 | 还原两份备份、sha 校验、带 env validate、restart、`is-active`、探针、舰队快照 | `… --phase rollback …` | `sha256sum -c evidence/caddy-live.sha256` 通过 | 回滚本身失败：Caddy 停在不可用状态 = 节点通道中断，**立即**报用户；手工恢复路径为 `cp -p backup-caddy/Caddyfile.bak /etc/caddy/Caddyfile && cp -p backup-caddy/v3.env.bak /etc/caddy/v3.env && systemctl restart caddy` |

## 4. 阶段 W：watcher（授权 O0-A04 构建与演练，O0-A07 上线，O0-A07R 仅在需要时恢复数据库）

脚本：`bundle/tools/o0_deploy_watcher.sh`，阶段 `preflight` → `build` → `apply` → `verify`（`rollback`、`restore-db`）。

| 步 | 内容 | 命令 | sha / 验证 | 失败处置 |
|---|---|---|---|---|
| W-1 前置门禁（只读） | 舰队快照；bundle 完整性与 `deploy_candidate`；**线上源码与 compose 等于 `67b401a` 基线**（不覆盖未知代码）；构建上下文无 `config.json`；凭据检查；确认阶段 C 已让 Caddy 注入浏览器凭据；磁盘余量 | `bash $S/bundle/tools/o0_deploy_watcher.sh --execute --auth-id O0-A07 --phase preflight --stage-dir $S`（preflight 只读，也可在 O0-A04 下先跑） | `MANIFEST_OK watcher-live-vs-baseline`、`MANIFEST_OK compose-live-vs-baseline`、`CREDENTIAL_CHECK_OK`、`CADDY_INJECTS_BROWSER_CREDENTIAL` | 中止；线上未改 |
| W-2 构建与副本演练 | 在 staging 里拼构建上下文（线上源码副本 + 候选文件，排除 `node_modules`、`config.json`），校验候选文件 sha；`docker build` 出 `trader-watcher:o0-candidate-<UTC>`（不碰运行中容器）；镜像内 `require` 冒烟；用 SQLite 备份 API 从真库做一致副本；**在副本上跑首次启动迁移**：`--network none`、挂空 `config.json`、一次性假凭据、`PRICE_MONITOR_ENABLED=0`，看到 `Web UI listening` 且无 `[db] Failed` 即停；比较迁移前后三表非秘密摘要，`config_revision` 从 0 开始 | `… --auth-id O0-A04 --phase build …` | `MANIFEST_OK staged-context`；`REQUIRE_OK`；`SQLITE_BACKUP … integrity=ok`；`MIGRATION_DRYRUN_OK`；`revision_after= 0`；三表 `UNCHANGED` 或列出 `CHANGED_BY_MIGRATION` 供审阅 | 任何失败：中止；只留下 staging 里的镜像与副本，线上无变化。`CHANGED_BY_MIGRATION`：把差异（非秘密）交用户确认后再申请 O0-A07 |
| W-3 上线 | 备份：把当前镜像打 `trader-watcher:o0-rollback-<UTC>`；记录要覆盖与新增的源码文件并打包；备份 compose 与已有 env_file；真库在线备份 `backup-watcher/watcher-trading.pre-o0.db`。然后：写入 `/srv/trader-secrets/watcher-gateway.env`（0600，六个变量）；安装候选源码并删除候选不再携带的白名单文件；源码 sha = 候选清单；安装候选 compose（`env_file` 接线，P-04）；`compose config --quiet`；把 compose 镜像名指向**已演练过的**候选镜像；`up -d --no-deps --force-recreate --no-build watcher` | `… --auth-id O0-A07 --phase apply …` | `MANIFEST_OK watcher-live-vs-candidate`；启动日志有 `Web UI listening`，无 `[db] Failed`、无凭据错误；90 秒内重启计数 0；无凭据请求 `127.0.0.1:9090/api/status` 为 401，带浏览器凭据（进程内读取，只打印状态码）为 200；真库 `config_revision` 存在；舰队状态不变 | 任一失败：ERR trap 自动回滚（W-5）。舰队状态变化：退出码 3，报用户 |
| W-4 验证（只读） | 健康检查变 healthy（健康检查在 node 脚本里读 `process.env`，E-16）；Telegram 连接与入库日志计数；容器 env 中没有控制面 token 名；舰队比较；**用户浏览器核对**（basicauth 登录后站点、群组、账号、路由、风险、图片都正常） | `… --phase verify …` | `health=healthy`；`NO_CONTROL_PLANE_TOKENS_IN_WATCHER`；用户确认 | 执行 W-5 |
| W-5 回滚 | 还原被覆盖的源码、删除新增文件、还原 compose；源码与 compose 回到基线 sha；镜像名指回 `o0-rollback-<UTC>`；`--no-build` 重建；旧 watcher `/api/status` 回到 200（旧代码无鉴权） | `… --auth-id O0-A07 --phase rollback …` | `MANIFEST_OK`（基线）；`api/status=200` | 数据库**不回滚**：`config_revision`、`config_audit` 是新增表，旧代码忽略。若快照开关已打开，**必须先关开关**再回滚 watcher（wac-011 第 6 点：旧版每次启动都跑迁移、站点写不涨 revision，会造成"同 revision 异摘要"而锁存 invalid） |
| W-6 恢复数据库（仅限数据被迁移破坏时） | 停 watcher；把当前库文件移到备份目录；装回迁移前在线备份；启动 | `… --auth-id O0-A07R --phase restore-db …` | 启动日志正常 | **破坏性**：上线后写入的配置与收到的 Telegram 消息会丢失，审计保留期（契约 §9.12 ≥ 30 天）被打断；快照开关若已打开，会出现 `snapshot_revision_regressed` 或 invalid。单独授权 |

watcher 重建期间 Telegram 采集中断（通常 1 分钟内恢复）。W-4 之后按 `docs/agent-operations.md` §1 核对重建窗口内有无漏信号；漏了只报告，不补单。

## 5. 阶段 O：operator-query（授权 O0-A08）

脚本：`bundle/tools/o0_deploy_operator_query.sh`，阶段 `preflight` → `apply` → `verify`（`rollback`）。只重启 `trader-v3-controlplane-operator-query`。

| 步 | 内容 | 命令 | sha / 验证 | 失败处置 |
|---|---|---|---|---|
| O-1 前置门禁（只读） | 舰队快照；记录三个控制面单元 MainPID 与启动时间；bundle 完整性；`.venv-cp` Python ≥ 3.11 且有 `httpx`；**线上 `api/*.py`、`security/*.py` 等于 `67b401a` 基线**（热挂载漂移即中止）；快照开关未设或为 0；凭据检查（operator-query 持有 watcher 当前值，与控制面目录互异）；在 staging 做 overlay（线上树副本 + 候选五个文件），用单元的 `PYTHONPATH` 与 env 文件做 import 冒烟：网关生成物加载成功，`phase_max=P2`、`yaml_sha256` 等于 `RELEASE.json`，只有 operator-query 角色有 `/v1/watcher` 路由 | `bash $S/bundle/tools/o0_deploy_operator_query.sh --execute --auth-id O0-A08 --phase preflight --stage-dir $S` | `MANIFEST_OK cp-live-vs-baseline`；`IMPORT_OK operator-query watcher_routes=N`（N > 0，P2 网关行），`IMPORT_OK node-control watcher_routes=0`，`IMPORT_OK event-ingest watcher_routes=0` | 中止；线上未改。import 冒烟若因缺少单元里 `Environment=` 的其他变量而失败，先补齐冒烟环境再跑，不能跳过 |
| O-2 上线 | 备份被覆盖文件（tar）、记录新增文件、备份 env 文件；把 `WATCHER_GATEWAY_TOKEN`、`WATCHER_SNAPSHOT_TOKEN` 合入 operator-query env（开关仍关）；安装五个文件（`api/read_api.py`、`api/watcher_gateway.py`、`api/watcher_config_snapshot.py`、`api/generated/__init__.py`、`api/generated/watcher_gateway_routes.py`）；安装后 sha = 候选清单；**只重启 operator-query**；60 秒内 `/v1/accounts` 无 token 返回 401；启动日志没有生成物加载失败或网关停用告警 | `… --phase apply …` | `MANIFEST_OK cp-installed`；`GATEWAY_STARTUP_CLEAN` | ERR trap 自动回滚（O-4） |
| O-3 验证 | 网关：无 token 401 `unauthenticated`；伪 token 403 `invalid_token`；`SYSTEM_OBSERVER_TOKEN`（进程内读取，只打印状态码）200 且经过新 watcher；尾斜杠 404 `route_not_found` 无 `Location`；P3 路径 `/v1/watcher/price-alerts` 404；既有 `/v1/accounts` 200；公网 `/m/v1/watcher/status` 无 token 401；node-control 与 event-ingest 的 MainPID 与启动时间不变；舰队比较 | `… --phase verify …` | 全部 `PROBE` 行无 `PROBE_FAIL`；`OTHER_UNITS_UNCHANGED`；`FLEET_STATUS_UNCHANGED` | 执行 O-4 |
| O-4 回滚 | 还原被覆盖文件、删除新增文件与 `generated/`；代码 sha 回到基线；还原 env 文件；只重启 operator-query；`/v1/accounts` 401 | `… --phase rollback …` | `MANIFEST_OK cp-restored` | 回滚失败：operator-query 不可用意味着 app 与运维查询中断，但节点心跳走 node-control 不受影响；立即报用户 |

**共享代码目录（D-04）**：三个控制面单元都跑 `read_api:app`，且（待 S-10 确认）共用同一目录。O-2 之后 node-control 与 event-ingest 仍在内存里跑旧代码，但磁盘上已是新 `read_api.py`，它们下次重启（任何原因）就会加载新代码。O-1 的 import 冒烟已证明新代码对这两个角色可加载、且不注册网关路由；这个混合状态需要用户知情接受。不接受的替代方案是给 operator-query 单独的代码目录，这要改 systemd 单元，是更大的变更。

## 6. 上线后

- 证据包（都在 `$S/evidence/`，不含凭据）：`authorizations.log`、各阶段 `fleet-*.txt`、`caddy-live.sha256`、`caddy-backup.sha256`、`caddy-diff.redacted.txt`、`watcher-dryrun.log`、`watcher-rollback-image.txt`、`cp-units-before/after.txt`，以及三个脚本的完整输出（执行者用 `… 2>&1 | tee $S/evidence/<阶段>.log` 保存）。
- app：不部署。A-0 真机联调只在用户明确说"现在可以用"后进行（O0-A10）。
- 快照开关仍为 0；副本仍在；打开开关按 `o0-runbook-snapshot-switch.md`。
- 可选：gateway 轮换演练（O0-A11，验证计划 §4.2"无感"）。

## 7. 故障对照

| 现象 | 最可能的原因 | 处置 |
|---|---|---|
| 阶段 C 之后舰队变 HALTED | Caddy restart 瞬断节点通道（已知） | 停止后续阶段，报用户；恢复只由用户决定（O0-A06） |
| 阶段 C 之后站点 401 循环 | basic auth 块被改坏 | C-4 回滚 |
| 新 watcher 反复重启 | 凭据不合规（非零退出）、DB 路径变量冲突、首次迁移拿锁超时 | 自动回滚已触发；读 `docker logs` 首行错误（只含变量名）；对照 S-05、S-15 |
| 站点正常但图片不显示 | `/media/*` 没进 basicauth 块，watcher 返回 401 | 修候选 Caddyfile，重走阶段 C（新授权） |
| 网关 503 `gateway_disabled` | operator-query 没拿到 `WATCHER_GATEWAY_TOKEN` 或与目录撞值 | 查 O-2 的 env 合并输出（只有变量名）；重跑凭据 `check` |
| 网关 503 `watcher_unavailable` | watcher 不接受网关凭据（两边值不一致）或 watcher 不可达 | 凭据 `check --holder-env operator-query.env`；看 watcher 日志 401 计数 |
| 网关每个请求 500 | `.venv-cp` Python < 3.11（`asyncio.timeout_at`） | O-4 回滚；S-12 本应拦住 |
