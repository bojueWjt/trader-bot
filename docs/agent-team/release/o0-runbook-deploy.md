# O-0 分阶段部署 runbook（wac-032 草案，wac-045 修订）

> 草案。**没有执行任何生产动作**。每个带 O0-Axx 的步骤都要用户逐项授权后才执行（`o0-authorization-list.md`）；脚本阶段与授权号一一绑定，号不对脚本直接拒绝。
> 范围：计划 §4.1 第 1 步"新增并验证"落到 jp-24：watcher 镜像（W-0）、控制面 operator-query（C-0 reader 开关保持关 + C-1 网关）、Caddy（浏览器入口清头注入 + `/m/v1/watcher/*` 逐路径）。**app 不需要部署**（A-0 只在 app 集成分支，真机联调另行授权 O0-A10）。快照开关在本 runbook 全程保持关闭；打开开关见 `o0-runbook-snapshot-switch.md`。
> 不做：不重启 node-control、event-ingest；不写控制面库与记账五表；不新增控制面迁移；不发 RESUME；不 reload Caddy；不下单、不平仓、不撤单。
> 下文 `$S` = `/srv/trader-staging/o0-<UTC>`，`$T` = `$S/bundle/tools`（工具取自候选提交，门禁 G11）。执行者把每条命令的完整输出存为 `… 2>&1 | tee -a $S/evidence/<阶段>.log`。

## 0. 阶段顺序、门禁机制与舰队守卫

| 顺序 | 阶段 | 为什么排在这里 | 中间态是否安全 |
|---|---|---|---|
| 1 | **C：Caddy** | W-0a 上线后，浏览器请求必须带 Caddy 注入的 `X-Watcher-Proxy-Auth`，否则站点全 401。先让 Caddy 注入，旧 watcher 会忽略这个头 | 安全：旧 watcher 忽略注入头；`/m/v1/watcher/*` 暂时落到旧 operator-query，得到 404 |
| 2 | **W：watcher** | 新 watcher 需要三个当前值才能启动；浏览器身份此时已由 Caddy 提供 | 安全：旧 operator-query 读副本文件，不经 HTTP 访问 watcher；feeder 直读库不受影响 |
| 3 | **O：operator-query** | 网关要打到已经鉴权的新 watcher | 安全：快照开关关闭，交易路径不引入快照依赖 |

Caddy 只重启一次（阶段 C），operator-query 只重启一次（阶段 O），watcher 只重建一次（阶段 W）。

**门禁是机读的，不靠自觉**（审查 wac-032 🔴-3）：每个阶段的 preflight（watcher 还有 build）开头删除旧门禁文件，只有全部检查通过才在最后写 `evidence/<阶段>.gate.json`，内容绑定候选提交、`RELEASE.json` 与 `SHA256SUMS` 的摘要、以及该阶段输入文件的摘要（候选 Caddyfile、线上文件、凭据片段、候选镜像 ID）。apply 的**第一步**用 `o0_tool.py gate-check` 核对：文件存在、阶段对、候选与 bundle 一致、输入摘要一致、足够新（preflight 1 小时，build 24 小时）；再重跑便宜的门禁（`SHA256SUMS`、`deploy_candidate`、线上文件等于基线、凭据检查）。任何一项不满足，在任何写入之前退出。watcher 的候选镜像必须在停任何服务之前已存在，且 ID 等于 build 门禁记录的 ID。

**舰队守卫**（计划 §4.2；审查 wac-032 🔴-1）：
- 每次采样 = `docs/agent-operations.md` §0 的查询（`node_id status release_id hb_age`）加四个 `/ready` 状态码。
- apply 的第一批步骤里重新记录基线（preflight 的样本只用于门禁）；基线必须**恰好**是 `O0_FLEET_NODES` 这组节点、每个心跳年龄 < 5 秒（§0："心跳冻结 = 节点死了而不是'没变化'"），否则拒绝开始（退出码 2）。
- 变更之后先等稳定窗（默认 60 秒），再采样 4 次、每 20 秒一次；每一次都要与基线在状态、release、`/ready` 上一致，且心跳仍新鲜（冻结或年龄跃升都算变化）。0 行、缺节点、多节点、无法解析都判"无法比较"（退出码 2），不当作"没变化"。有变化：退出码 3，停下，保持现场，报用户；**不回滚**（回滚会再重启一次），**不 RESUME**（O0-A06 只属于用户）。
- 回滚阶段只记录回滚前样本、不以它为门槛（回滚不能被舰队状态挡住），回滚后照常比较。

**已声明的风险**：`systemctl restart caddy` 会瞬断节点通道（S-06 确认地址，预期 `172.30.1.1:8080`）。2026-08-30 这导致全舰队 fail-closed HALT，2026-08-31 没有。阶段 C 可能让 ACTIVE 节点变 HALTED，这不是"意外 HALT"，而是需要用户事先接受的已知后果（D-02，待确认）。

## 1. 开始前必须全部满足

| 门禁 | 证据 | 不满足 |
|---|---|---|
| 本地打包门禁 G1–G12 全部 PASS，`RELEASE.json` 的 `deploy_candidate: true` | `o0_package.sh` 输出与 `logs/` | 不申请任何生产授权。当前（`o0-requirements.md` §6）G4、G7 失败 |
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

1. `cp -p /etc/caddy/Caddyfile $S/caddy/Caddyfile.candidate`
2. 在 jp-bot 站点块里、现有 `/m` 移动端 handle 旁边，粘贴 `bundle/caddy/watcher-gateway.caddy`（`o0_caddy_watcher_routes.py render` 从生成清单产出：16 个具名匹配器，每个是锚定的 `path_regexp` + `method`）。**用户裁决（2026-09-26，写入 WGW-1.0.2）**：清单里的 `*` = 恰好一个非空段，翻译为 `[^/]+`；四条带参数的路径是：

   ```caddyfile
   path_regexp wgw_media_param ^/m/v1/watcher/media/[^/]+$
   path_regexp wgw_trading_accounts_param ^/m/v1/watcher/trading/accounts/[^/]+$
   path_regexp wgw_trading_channels_param ^/m/v1/watcher/trading/channels/[^/]+$
   path_regexp wgw_trading_risks_param ^/m/v1/watcher/trading/risks/[^/]+$
   ```

   `path_regexp` 区分大小写（与网关路由表一致）；**不要**加 `(?i)`，不要改成 `path …/*`（Caddy 的 `path` 前缀匹配会跨段且不区分大小写），不要写 `[^/]*` 或 `.+`，不要去掉末尾 `$`。**不要**加任何 `/m/*`、`/m/v1/*` 兜底；不要贴 `{param}`。核对工具对以上每一种写法都判失败（自测 20 种变体）。
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

   说明：`request_header` 在指令顺序上先于 `reverse_proxy` 执行，所以是"先清后注入"；浏览器的 basic `Authorization` 必须清掉（核对工具现在要求这一项）。**不要**在 `reverse_proxy` 里写 `header_up -X-Watcher-*`。必须用运行时占位符 `{env.…}`，不能用 `{$…}`。
4. 若 S-03 显示 `/media/*` 目前不走 basicauth 块，而站点要显示图片，把 `/media/*` 并入 basicauth 块的匹配器。

### 3.2 步骤

| 步 | 内容 | 命令 | sha / 验证 | 失败处置 |
|---|---|---|---|---|
| C-1 前置门禁（只读） | 清旧门禁；舰队基线（完整、新鲜）；bundle 复核；Caddy 版本；记录线上 Caddyfile 与 v3.env 的 sha；候选存在且不同于线上；脱敏 diff（只容忍 `diff` 退出码 1，脱敏失败即失败）；凭据检查（含控制面目录）；带 env 的 `caddy validate`；`caddy adapt` 到 0600 JSON；按生成清单 verify；**全部通过才写 `caddy-preflight` 门禁** | `bash $T/o0_deploy_caddy.sh --execute --phase preflight --auth-id O0-A05 --stage-dir $S` | `FLEET_BASELINE_OK`；`CREDENTIAL_CHECK_OK`；`Valid configuration`；`CADDY_WATCHER_ROUTES_OK lines=16`；`GATE_WRITTEN stage=caddy-preflight`；人工确认脱敏 diff 只动了 §3.1 的两处 | 任一失败：中止，线上未改，也没有门禁文件 |
| C-2 应用 | **先**核对门禁（同一候选 Caddyfile、线上两个文件与凭据片段未变、≤ 1 小时）、bundle 复核、再 validate 候选、记录舰队基线；然后备份两份文件（0700）；合入 `WATCHER_BROWSER_PROXY_TOKEN` 并用摘要确认 v3.env 持有 watcher 当前值；安装候选；**已安装文件与门禁里的候选 sha 一致**；带 env validate；对已安装文件 adapt 并 verify 路由；`systemctl restart caddy`；`is-active`；舰队守卫（稳定窗 + 多次采样） | `bash $T/o0_deploy_caddy.sh --execute --phase apply --auth-id O0-A05 --stage-dir $S` | `GATE_OK`；备份 sha 写入 `evidence/caddy-backup.sha256`；`CADDY_WATCHER_ROUTES_OK`（已安装文件）；`FLEET_UNCHANGED_ALL_SAMPLES` | 门禁不符：退出，线上未改。restart 之前的任何失败：ERR trap 自动回滚（还原两份文件、sha 校验回到 C-1 记录值、validate、restart）。舰队变化：退出码 3，**不回滚也不 RESUME**，立即报用户 |
| C-3 验证（只读） | admin API 取运行中配置到 0600 JSON，再跑同一 verify；本机回环 `--resolve` 公网探针：`/m/v1/accounts` 无 token 401、伪 token 403；`/m/v1/watcher/status` 404；两段路径不到网关；`/watcher/`、`/api/status` 401；`/media/<名>` 不得无认证返回图片；节点通道有响应；舰队守卫 | `bash $T/o0_deploy_caddy.sh --execute --phase verify --auth-id O0-A05 --stage-dir $S --node-channel <S-06 地址>` | `CADDY_WATCHER_ROUTES_OK`；探针全部符合；`FLEET_UNCHANGED_ALL_SAMPLES` | 验证失败：执行 C-4 |
| C-4 回滚 | 记录回滚前舰队样本（不设门槛）；还原两份备份、sha 校验、带 env validate、restart、`is-active`、探针、舰队守卫 | `bash $T/o0_deploy_caddy.sh --execute --phase rollback --auth-id O0-A05 --stage-dir $S` | `sha256sum -c evidence/caddy-live.sha256` 通过 | 回滚本身失败：Caddy 不可用 = 节点通道中断，**立即**报用户；手工恢复路径为 `cp -p backup-caddy/Caddyfile.bak /etc/caddy/Caddyfile && cp -p backup-caddy/v3.env.bak /etc/caddy/v3.env && systemctl restart caddy` |

## 4. 阶段 W：watcher（O0-A04 门禁与构建，O0-A07 上线，O0-A07R 仅在需要时恢复数据库）

| 步 | 内容 | 命令 | sha / 验证 | 失败处置 |
|---|---|---|---|---|
| W-1 前置门禁（只读） | 清旧门禁；舰队基线；bundle 复核；**线上源码与 compose 等于 `67b401a` 基线**；构建上下文无 `config.json`；凭据检查（含目录）；Caddy 已注入浏览器凭据；磁盘余量；写 `watcher-preflight` 门禁 | `bash $T/o0_deploy_watcher.sh --execute --phase preflight --auth-id O0-A04 --stage-dir $S` | `MANIFEST_OK watcher-live-vs-baseline`、`MANIFEST_OK compose-live-vs-baseline`、`CREDENTIAL_CHECK_OK`、`CADDY_INJECTS_BROWSER_CREDENTIAL`、`GATE_WRITTEN` | 中止；线上未改 |
| W-2 离线构建与副本演练 | 清旧门禁；构建器自己校验运行时清单（白名单全集、sha、大小）；以**运行中镜像 ID** 为固定基础（记录其 `org.trader.*` 标签），列出运行中镜像里白名单之外的文件（`BASE_ONLY`，交用户审阅，`config.json` 不得在内）；`scripts/build_immutable_watcher_image.py` 离线构建（`FROM` 固定基础，只 `COPY` 已审文件，`docker build --network=none --no-cache`，不 `apt`、不 `pip`、不 `npm`）并打 `trader-watcher:o0-candidate-<UTC>`；镜像内 `require` 冒烟；备份 API 做真库一致副本（副本切成回滚日志模式）；迁移前基线（退出码写入证据，2 = 失败）；在副本上首次启动：`--network none`、空 `config.json`、一次性假凭据、`PRICE_MONITOR_ENABLED=0`、`HERMES_TRADER_CRON_ENABLED=0`、`SIGNAL_IMPORTER_ENABLED=0`、清空 `WATCHER_ALERT_*`；看到 `Web UI listening` 后 **`docker stop` 正常停止**（SIGTERM，退出码必须 0）；再用备份 API 从演练库做一份新副本（读得到 WAL）并在**新副本**上分析：`config_revision` 从 0 开始、三表摘要对比；写 `watcher-build` 门禁（候选镜像 ID） | `bash $T/o0_deploy_watcher.sh --execute --phase build --auth-id O0-A04 --stage-dir $S` | `RUNTIME_MANIFEST`（构建器校验通过）；`BASE_ONLY` 清单；`REQUIRE_OK`；`SQLITE_BACKUP … integrity=ok`；`dryrun_exit_code=0`；`MIGRATION_DRYRUN_OK`；`revision_after= 0`；三表 `UNCHANGED` 或 `CHANGED_BY_MIGRATION` | 任何失败：中止；只留下 staging 里的镜像与副本。`CHANGED_BY_MIGRATION` 或 `BASE_ONLY` 非空：交用户确认后再申请 O0-A07 |
| W-3a 上线前复核（只读） | 与 W-1 相同的门禁，在上线窗口内重跑（门禁有效期 1 小时） | `bash $T/o0_deploy_watcher.sh --execute --phase apply-preflight --auth-id O0-A07 --stage-dir $S` | 同 W-1 | 中止 |
| W-3 上线 | **先**：两份门禁核对（凭据片段与线上 compose 未变）；候选镜像存在且 ID = build 门禁；演练日志 sha 未变且含 `MIGRATION_DRYRUN_OK`；bundle 复核；线上源码与 compose 仍等于基线；凭据复核；舰队基线。**然后**：备份（旧镜像打 `o0-rollback-<UTC>`、被覆盖与新增源码清单与 tar、compose、env_file 状态（有则备份、无则记"原本不存在"）、真库在线备份及其 sha）；写 `/srv/trader-secrets/watcher-gateway.env`（0600）并用摘要确认两个持有方与之一致；安装候选源码 → `MANIFEST_OK watcher-live-vs-candidate`；安装候选 compose → `MANIFEST_OK compose-live-vs-candidate`；`compose config --quiet`；镜像名指向候选；记录重建时间；`up -d --no-deps --force-recreate --no-build watcher`；启动日志 `Web UI listening`、无 `[db] Failed`、无凭据错误、90 秒内重启计数 0；**180 秒内出现 `[watcher] Connected, listening...`**（出现 `Session not authorized` 立即失败）；无凭据 401、浏览器凭据 200；真库 `config_revision` 存在；舰队守卫 | `bash $T/o0_deploy_watcher.sh --execute --phase apply --auth-id O0-A07 --stage-dir $S` | 上列各行 + `TELEGRAM_RECONNECTED` + `FLEET_UNCHANGED_ALL_SAMPLES` | 门禁不符或镜像缺失：退出，任何服务都没动。重建之后的任何失败（含 Telegram 未重连）：ERR trap 自动回滚（W-5）。舰队变化：退出码 3，报用户 |
| W-4 验证（只读） | 健康检查变 healthy；Telegram 仍连着、容器仍在跑且重启计数 0、重建以来的转发行计数（只计数，不含正文）写入证据；容器 env 中没有控制面 token 名；舰队守卫；**用户浏览器核对** | `bash $T/o0_deploy_watcher.sh --execute --phase verify --auth-id O0-A07 --stage-dir $S` | `health=healthy`；`forwarded_lines_since_recreate=`；`NO_CONTROL_PLANE_TOKENS_IN_WATCHER` | 执行 W-5 |
| W-5 回滚 | 记录回滚前舰队样本；还原被覆盖的源码、删除新增文件、还原 compose；env_file 回到 apply 前的状态（原有则还原，原本没有则删除）；源码与 compose 回到基线 sha；镜像名指回 `o0-rollback-<UTC>`；`--no-build` 重建；旧 watcher `/api/status` 200；舰队守卫 | `bash $T/o0_deploy_watcher.sh --execute --phase rollback --auth-id O0-A07 --stage-dir $S` | `MANIFEST_OK`（基线）；`ENV_FILE_RESTORED` 或 `ENV_FILE_REMOVED_AS_BEFORE_APPLY`；`api/status=200` | 数据库**不回滚**（两张新表旧代码忽略）。若快照开关已打开，**必须先关开关**再回滚 watcher（wac-011 第 6 点） |
| W-6 恢复数据库（仅限数据被迁移破坏时） | 断言快照开关为 0（只检查，不改）；备份 sha 与 `integrity_check` 正确；舰队基线；停 watcher；把当前库文件移到备份目录；装回迁移前在线备份并证明字节一致；启动；`Web UI listening`、无 `[db] Failed`、Telegram 重连；舰队守卫 | `bash $T/o0_deploy_watcher.sh --execute --phase restore-db --auth-id O0-A07R --i-understand-data-loss --stage-dir $S` | `RESTORED_DB_SHA_OK`；`RESTORE_DB_WATCHER_UP` | **破坏性**：上线后写入的配置与 Telegram 消息会丢失，审计保留期（契约 §9.12 ≥ 30 天）被打断。O0-A07 的号执行不了这一步 |

W-3 之后按 `docs/agent-operations.md` §1 核对重建窗口内有无漏信号：`journalctl -u trader-v3-hermes-feeder --since <evidence/watcher-recreate.at> | grep -c 'Triggered job: signal'`，与 W-4 的转发行计数对照；周末美股代币类频道静默时 0 不代表故障；漏了只报告，不补单。

## 5. 阶段 O：operator-query（授权 O0-A08）

| 步 | 内容 | 命令 | sha / 验证 | 失败处置 |
|---|---|---|---|---|
| O-1 前置门禁（只读） | 清旧门禁；舰队基线；记录三个控制面单元 MainPID 与启动时间；bundle 复核；`.venv-cp` Python ≥ 3.11 且有 `httpx`；**线上 `api/*.py`、`security/*.py` 等于 `67b401a` 基线**；快照开关未设或为 0；凭据检查；overlay import 冒烟（三种角色）；写 `oq-preflight` 门禁 | `bash $T/o0_deploy_operator_query.sh --execute --phase preflight --auth-id O0-A08 --stage-dir $S` | `MANIFEST_OK cp-live-vs-baseline`；`IMPORT_OK operator-query watcher_routes=N`（N > 0），另两个角色 `watcher_routes=0`；`GATE_WRITTEN` | 中止；线上未改。import 冒烟若因缺少单元里 `Environment=` 的其他变量而失败，先补齐冒烟环境再跑，不能跳过 |
| O-2 上线 | **先**：门禁核对（凭据片段与线上 env 未变）；bundle、venv、基线、开关、凭据复核；舰队基线。**然后**：备份被覆盖文件（tar）、新增文件清单、env 文件及其 sha；合入 `WATCHER_GATEWAY_TOKEN`、`WATCHER_SNAPSHOT_TOKEN` 并用摘要确认线上 env 持有 watcher 当前值、仍不与目录撞值、开关仍关；安装五个文件 → `MANIFEST_OK cp-installed`；记录重启时刻；**只重启 operator-query**；60 秒内 `/v1/accounts` 无 token 401；重启以来的日志没有生成物加载失败或网关 token 缺失/撞值；**在自动回滚窗口内**用 `SYSTEM_OBSERVER_TOKEN` 经网关请求 `/v1/watcher/status` 期望 200；舰队守卫 | `bash $T/o0_deploy_operator_query.sh --execute --phase apply --auth-id O0-A08 --stage-dir $S` | `GATE_OK`；`MANIFEST_OK cp-installed`；`gateway_startup_error_lines=0`；`PROBE … status=200`；`FLEET_UNCHANGED_ALL_SAMPLES` | 门禁不符：退出，线上未改。重启后任何失败（含网关 503 `gateway_disabled`/`watcher_unavailable`）：ERR trap 自动回滚（O-4） |
| O-3 验证（只读） | 网关：无 token 401、伪 token 403、`SYSTEM_OBSERVER_TOKEN` 200、尾斜杠 404 无 `Location`、P3 路径 404；既有 `/v1/accounts` 200；公网 `/m/v1/watcher/status` 无 token 401；另两个单元 MainPID 与启动时间不变；舰队守卫 | `bash $T/o0_deploy_operator_query.sh --execute --phase verify --auth-id O0-A08 --stage-dir $S` | 无 `PROBE_FAIL`；`OTHER_UNITS_UNCHANGED`；`FLEET_UNCHANGED_ALL_SAMPLES` | 执行 O-4 |
| O-4 回滚 | 记录回滚前舰队样本；还原被覆盖文件、删除新增文件与空的 `generated/`；代码 sha 回到基线；还原 env 并核对 sha；只重启 operator-query；`/v1/accounts` 401；舰队守卫 | `bash $T/o0_deploy_operator_query.sh --execute --phase rollback --auth-id O0-A08 --stage-dir $S` | `MANIFEST_OK cp-restored` | 回滚失败：app 与运维查询中断，但节点心跳走 node-control 不受影响；立即报用户 |

**共享代码目录（D-04，待用户确认）**：三个控制面单元都跑 `read_api:app`，且（待 S-10 确认）共用同一目录。O-2 之后 node-control 与 event-ingest 仍在内存里跑旧代码，但磁盘上已是新 `read_api.py`，它们下次重启（任何原因）就会加载新代码。O-1 的 import 冒烟已证明新代码对这两个角色可加载、且不注册网关路由。

## 6. 上线后

- 证据包（都在 `$S/evidence/`，不含凭据）：`authorizations.log`、`*.gate.json`、各阶段 `fleet-*.txt` 与 `*.verdict.txt`、`caddy-live.sha256`、`caddy-backup.sha256`、`caddy-diff.redacted.txt`、`watcher-base-*.txt`、`watcher-build-attestation.json`、`watcher-dryrun.log`、`watcher-dryrun-{pre,post}.rc`、`watcher-rollback-image.txt`、`watcher-db-backup.sha256`、`watcher-recreate.at`、`watcher-ingest-count.txt`、`oq-env-backup.sha256`、`cp-units-before/after.txt`，以及执行者保存的各阶段完整输出。
- app：不部署。A-0 真机联调只在用户明确说"现在可以用"后进行（O0-A10）。
- 快照开关仍为 0；副本仍在；打开开关按 `o0-runbook-snapshot-switch.md`。

## 7. 故障对照

| 现象 | 最可能的原因 | 处置 |
|---|---|---|
| 守卫报 `FLEET_BASELINE_STALE` 或 `FLEET_UNCOMPARABLE`，拒绝开始 | 有节点心跳冻结，或节点集合与 `O0_FLEET_NODES` 不一致 | 不开始；按 `docs/agent-operations.md` 排障或核对 S-00，由用户决定 |
| 阶段 C 之后守卫报 `FLEET_CHANGED` | Caddy restart 瞬断节点通道（已知） | 停止后续阶段，报用户；恢复只由用户决定（O0-A06） |
| apply 报 `GATE_FAIL` | 没跑 preflight/build、门禁过期、候选或输入文件变了 | 重跑对应的门禁阶段；不要绕过 |
| apply 报 `CANDIDATE_IMAGE_MISSING` | build 没跑或镜像被清理 | 回到 W-2；此时没有任何服务被停 |
| 阶段 C 之后站点 401 循环 | basic auth 块被改坏 | C-4 回滚 |
| 新 watcher 反复重启或 `TELEGRAM_NOT_RECONNECTED` | 凭据不合规、DB 路径变量冲突、首次迁移拿锁超时、会话失效 | 自动回滚已触发；读 `docker logs` 首行错误（只含变量名）；对照 S-05、S-15 |
| 站点正常但图片不显示 | `/media/*` 没进 basicauth 块 | 修候选 Caddyfile，重走阶段 C（新授权） |
| 网关 503 `gateway_disabled` / `watcher_unavailable` | operator-query 没拿到或撞值；两边值不一致或 watcher 不可达 | O-2 内已自动回滚；重跑凭据 `check` |
| 网关每个请求 500 | `.venv-cp` Python < 3.11 | O-4 回滚；S-12 本应拦住 |
