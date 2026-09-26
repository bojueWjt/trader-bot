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
| 本地打包门禁 G1–G12 全部 PASS，`RELEASE.json` 的 `deploy_candidate: true` | `o0_package.sh` 输出与 `logs/` | 不申请任何生产授权。当前（`o0-requirements.md` §6.1）只剩 G7 失败 |
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
| C-2 应用 | **先**核对门禁（同一候选 Caddyfile、线上两个文件与凭据片段未变、≤ 1 小时）、bundle 复核、再 validate 候选、记录舰队基线；然后备份两份文件（0700）；合入 `WATCHER_BROWSER_PROXY_TOKEN` 并用摘要确认 v3.env 持有 watcher 当前值；安装候选；**已安装文件与门禁里的候选 sha 一致**；带 env validate；对已安装文件 adapt 并 verify 路由；`systemctl restart caddy`；`is-active`；舰队守卫（稳定窗 + 多次采样） | `bash $T/o0_deploy_caddy.sh --execute --phase apply --auth-id O0-A05 --stage-dir $S` | `GATE_OK`；备份 sha 写入 `evidence/caddy-backup.sha256`；`CADDY_WATCHER_ROUTES_OK`（已安装文件）；`FLEET_UNCHANGED_ALL_SAMPLES` | 门禁不符：退出，线上未改。备份之后、restart 之前的任何失败：ERR trap 自动回滚只还原两份文件、sha 校验回到 C-1 记录值、validate，**不 restart**（运行中的 Caddy 从未变过，restart 只会平添 D-02 风险）。restart 及之后的失败：还原、validate、再 restart、`is-active`。两种情况回滚后都跑舰队守卫（对比 `before-caddy`，只报告不设门槛），结论写入 `evidence/auto-rollback.log` 与 `fleet-after-auto-rollback-*.verdict.txt`，脚本退出 1；`fleet_rc` 非 0 立即报用户。舰队变化：退出码 3，**不回滚也不 RESUME**，立即报用户 |
| C-3 验证（只读） | admin API 取运行中配置到 0600 JSON，再跑同一 verify；本机回环 `--resolve` 公网探针：`/m/v1/accounts` 无 token 401、伪 token 403；`/m/v1/watcher/status` 404；两段路径不到网关；`/watcher/`、`/api/status` 401；`/media/<名>` 不得无认证返回图片；节点通道有响应；舰队守卫 | `bash $T/o0_deploy_caddy.sh --execute --phase verify --auth-id O0-A05 --stage-dir $S --node-channel <S-06 地址>` | `CADDY_WATCHER_ROUTES_OK`；探针全部符合；`FLEET_UNCHANGED_ALL_SAMPLES` | 验证失败：执行 C-4 |
| C-4 回滚 | 记录回滚前舰队样本（不设门槛）；还原两份备份、sha 校验、带 env validate、restart、`is-active`、探针、舰队守卫 | `bash $T/o0_deploy_caddy.sh --execute --phase rollback --auth-id O0-A05 --stage-dir $S` | `sha256sum -c evidence/caddy-live.sha256` 通过 | 回滚本身失败：Caddy 不可用 = 节点通道中断，**立即**报用户；手工恢复路径为 `cp -p backup-caddy/Caddyfile.bak /etc/caddy/Caddyfile && cp -p backup-caddy/v3.env.bak /etc/caddy/v3.env && systemctl restart caddy` |

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
| 网关 503 `gateway_disabled` / `watcher_unavailable` | operator-query 没拿到或撞值；两边值不一致或 watcher 不可达 | O-2 内已自动回滚；重跑凭据 `check` |
| 网关每个请求 500 | `.venv-cp` Python < 3.11 | O-4 回滚；S-12 本应拦住 |
