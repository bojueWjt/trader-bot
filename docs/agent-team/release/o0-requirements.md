# O-0 发行要求可追溯清单（wac-032 草案，wac-045 修订，wac-059 第 3 轮，wac-072 加固，wac-060 适配 WGW-1.0.2，wac-090、wac-092、wac-094、wac-097 收紧 Caddy 核对）

> 作者：watcher-app-crew-release-steward。状态：**草案，未执行任何生产动作，也没有做只读现场核对**。
> wac-045 修订：处置审查报告 `reviews/wac-032.md`（5 🔴、18 🟡、4 💭），逐条见 §7；按用户 2026-09-26 裁决（W-0b 方案 A、Caddy `*` 严格单段锚定 `path_regexp`）与 Planner 对 D-05…D-08 的决定更新。事实基线：集成分支 `f7641cb`（含 wac-040 `a881802`、W-0b）。
> wac-059 第 3 轮：处置复审报告 `reviews/wac-032-r2.md` 的 🔴-1 与 🟡-1…4，见 §8。WGW-1.0.2 的 Caddy 清单 v2 与片段适配不在本轮（另有任务 wac-060）。
> wac-072 加固（worktree `auto/wac-072`，基于集成分支 `487f6eb`）：处置终审报告 `reviews/wac-032-r3.md` §7 的剩余待办（🟡-1 路径令牌碎片、🟡-3 测试缺口）与 r2 遗留的 🟡-5、🟡-6、🟡-7，见 §9。仍不做 WGW-1.0.2 适配（wac-060）。
> wac-060（worktree `auto/wac-060`，基于集成分支 `bff84ef`，已含 WGW-1.0.2 与 wac-072）：O-0 工具链适配契约 WGW-1.0.2（清单格式 v2、已提交片段、F-04/F-10/F-12/F-13），并处置 wac-073 复审（`reviews/wac-072.md`）的 🟡-2…🟡-5，见 §10。
> 基线：worktree `auto/wac-032`，分支点 `3d58761`；写作期间集成分支已前进到 `4fb0c11`（多了 C-0 校准 `eaf333d`、C-0 测试加固 `122ef51`、A-0 加固看板），本清单按 `4fb0c11` 的事实标注状态。
> 依据：计划 `docs/plans/2026-09-11-watcher-to-app-migration.md` v0.6（下称"计划"）；契约 `contracts/backend-api.md` WGW-1.0.1 §9（下称"契约"）；审查报告 `docs/agent-team/reviews/`（wac-022 的审查报告 `wac-022.md` 在 `.worktrees/wac-integ` 中尚未提交，按其现场内容引用）；`AGENTS.md`；`docs/agent-operations.md`（主 checkout 中的未跟踪文件，集成分支没有）；协作协议 `docs/agent-team/watcher-app-crew-workflow-protocol.md`。
> 配套：`o0-site-checklist.md`（S-xx）、`o0-runbook-deploy.md`（阶段 C/W/O）、`o0-runbook-credentials.md`、`o0-runbook-snapshot-switch.md`、`o0-authorization-list.md`（O0-Axx），脚本草案在 `scripts/ops/o0/`。

## 0. 汇总

| 类别 | 条数 | 已有覆盖（清单/脚本/门禁） | 仍依赖他人 |
|---|---|---|---|
| A 计划原文 | 27 | 27 | R01、R16、R20、R27 依赖前置任务（P-02、P-10、P-11） |
| B 契约 §9 | 11 | 11 | R31 依赖 P-04/P-05 |
| C 审查报告 | 37 | 37 | R53/R54/R47 等由门禁拦截，修复在执行者任务 |
| D 运维铁律与现场教训 | 12 | 12 | — |
| 合计 | **87** | **87 条都有对应的核对、门禁或 runbook 步骤** | 其中 **26 条**挂着尚未满足或尚未现场核实的前置（R01 R03 R05 R12 R15 R16 R18 R20 R21 R26 R27 R28 R30 R31 R32 R45 R47 R49 R52 R53 R54 R56 R59 R64 R68 R73，前置见 §1） |

"覆盖"只表示清单里写了怎么验，不表示已经验过。所有现场项在用户授权前都没有执行。

## 1. 前置依赖（P-xx）

| ID | 前置 | 当前状态（以 git 与看板为证） | 由谁拦截 |
|---|---|---|---|
| P-01 | W-0b 配置写路径合入集成分支 | **done**：用户选方案 A，已合入 `f7641cb`（第 4 轮小修 wac-043 仍在进行，合入后复跑门禁） | 打包门禁 G6（现 PASS） |
| P-02 | W-0 集成补齐 wac-015（E-02 格式、`req.watcherRoute`、W6/W7、改用生成的 `gateway-routes.js`、删手写 `watcher-routes.js`、停用 `db_manager.py` 直写） | **done**：wac-015/wac-015b 已合入（集成分支 `d8342ad` 起 G7 PASS） | G7 |
| P-03 | `db_manager.py` 写命令停用（D1），且 jp-24 上没有调用者 | 代码侧属 P-02；现场由 S-15 核对 | G7 + S-15 |
| P-04 | compose 把六个 `WATCHER_*` 交给 watcher（`env_file: /srv/trader-secrets/watcher-gateway.env`），健康检查不插值 | **done**：wac-040 已合入 `a881802` | G5（现 PASS） |
| P-05 | watcher 镜像白名单 `WATCHER_RUNTIME_RELATIVE_PATHS` 补齐运行时闭包 | **部分**：wac-040 补了 `lib/auth.js`、`lib/media.js`、`lib/status.js`、两个生成文件；W-0b 在其后合入，`lib/config-store.js` 仍不在白名单（本地 `CLOSURE_NOT_WHITELISTED lib/config-store.js`）。需 Planner 派发（可并入 wac-015） | G4（现 FAIL）、G12 |
| P-06 | （建议）watcher 提供只做数据库初始化后退出的入口，供副本演练用 | 未提。没有它时，阶段 W 用完整 `server.js` 在 `--network none` 容器里跑，看到 `Web UI listening` 即停 | 阶段 W build |
| P-07 | 数据库初始化失败退出前推 Telegram 告警（wac-011-r3 🟡-2） | **Planner 已定**：wac-015 复用 `sendWatcherAlert`；O-0 保留 90 秒重启计数兜底 | D-07 |
| P-08 | 若走正式发布流水线 `make_account_stall_release.py`：保留 `host/generated/` 子目录 | 本草案的 O-0 bundle 自带目录结构，不依赖该脚本；若改走流水线，需另派 | 阶段 O manifest |
| P-09 | C-0 校准（403 锁存、4 MiB、R12、E-14 拒因）与测试加固 | **done**：`eaf333d`（wac-022，审查 wac-023 PASS）、`122ef51`（wac-033，含持久化证据测试） | — |
| P-10 | C-0 每个 worker 预热成功或锁存时写一条日志，带 `revision`、`content_sha256`、pid | **wac-041 `c9b1e3f` 审查中**：格式 `snapshot_warmup result=… revision=… content_sha256=<12> pid=… role=operator-query duration_ms=…`；切换门禁用 `o0_tool.py warmup-check` 判定“每个 worker 一行成功” | 切换 runbook §3.2.3 |
| P-11 | T0-6 三路对照工具与报告（计划 §4.1 第 2 步、§4.3 T0-6） | **未派**（看板 M5：三路对照工具待派） | 切换 runbook §2 |
| P-12 | WGW-1.0.2 勘误：Caddy 清单中 `*` 的语义为“恰好一个非空段”，翻译为锚定、区分大小写的 `path_regexp` | **done**：WGW-1.0.2 已出（F-04、F-10、F-12、F-13），清单格式 v2 + 片段已提交；O-0 工具链在 wac-060 适配 | D-03、G3 |
| P-13 | jp-24 `.venv-cp` 的 Python ≥ 3.11，且可 import `httpx` | 未核（S-12） | 阶段 O preflight |
| P-14 | jp-24 控制面代码与 `67b401a` 一致（09-22 起有热挂载史，线上 read_api 曾漂移） | 未核（S-13） | 阶段 O preflight |
| P-15 | jp-24 watcher 源码与 compose 与 `67b401a` 一致 | 未核（S-07） | 阶段 W preflight |
| P-16 | app 侧 A-0 已在 app 集成分支；app 不需要为 O-0 部署，真机联调另行授权 | A-0 与加固已合入 app 集成分支 | O0-A10 |
| P-17 | 切换回滚要用的 `sync_operator_risk_db.py` 在 jp-24 可用 | 未核（S-09）；bundle 的 `tools/` 附带一份 | 切换 runbook §5 |

## 2. A 类：计划原文

| ID | 要求（逐字或紧贴原文） | 出处 | 前置 | 验证方法 / 落点 |
|---|---|---|---|---|
| R01 | "三路对照工具与报告" | 计划 §9 O-0；§4.3 T0-6 | P-11 | 报告 B/C 等价；切换 runbook §2 |
| R02 | "服务端凭据生成、两两互异校验与 env 下发（不经 app）" | 计划 §9 O-0；§2.1 | — | `o0_watcher_credentials.py generate/check`；凭据 runbook §1–§2 |
| R03 | "每个进程只持自己需要的那一个，watcher 环境里不放任何控制面 reader token" | 计划 §2.1 | P-04 | `check` 拒绝 watcher env 出现控制面密钥名；S-05；阶段 W verify |
| R04 | "operator-query 缺 `gateway` 时只禁用网关路由并告警，不得让交易端点启动失败（上线前配置校验锁定）" | 计划 §2.1、§4.2 | C-1 R11 已实现 | WGW-1.0.4：网关在独立角色 watcher-gateway（operator-query 本来就不挂网关，交易端点不受影响）；阶段 O 的依赖检查（`python -B` import 冒烟）断言网关路由数 > 0 且 `readiness()` 为真；`/health/role` 停用态 503 只影响 8186；生产不做缺 token 负例 |
| R05 | "轮换用 `*_TOKEN` + `*_TOKEN_PREVIOUS` 双值，顺序为：watcher 先接受新旧两值，网关切到新值，确认请求与审计正常，再撤旧值" | 计划 §2.1；契约 §9.2 | P-02 | 凭据 runbook §3 |
| R06 | "确认 import 与 handler 顺序" | 计划 §9 O-0；契约 F-12、F-13 | — | S-02（含全局 `order` 与前置指令）、S-03/S-04 + `verify --before-deploy`（前瞻遮蔽检查）；阶段 C：`caddyfile-check`（片段文件全局 import、站点顶层 import 在所有 handle 之前）、F-13 (2) 人工记录、`verify` 的两步遮蔽检查（第一条 wgw 路由之前与外层每一条路由）、本机 Caddy 探针（O0-A05P） |
| R07 | "`/m` 只 strip 一次" | 计划 §9 O-0 | — | verify：每条样本恰好一次 `strip_path_prefix /m`；L-probe |
| R08 | "上游仍是 8183" | 计划 §9 O-0；**用户裁决 2026-09-29 选项 (e)**（契约 §9.14.6） | — | 计划原文按裁决改读：片段上游 `(watcher_gateway_upstream)` 恰为 `127.0.0.1:8186`（RS-11：一次 strip、无 rewrite、无 transport、无头操作）；其他 `/m/v1/*` 移动样本仍到 8183（verify `--oq-upstream`）；计划 v0.6 §2.1 由 Planner 修订 |
| R09 | "移动端 `Authorization` 保留且不被面板注入的 `system_observer` 覆盖" | 计划 §9 O-0 | — | verify：移动样本链路上无 Authorization 头操作；探针 `/m/v1/accounts` 无 token 必须 401（若被注入会变 200） |
| R10 | "浏览器 basicauth 后先清头再注入" | 计划 §9 O-0；§2.1 | — | verify：浏览器样本经 authentication，先删 `X-Watcher-Actor`/`X-Watcher-Token-Fingerprint`，再 set `X-Watcher-Proxy-Auth={env.WATCHER_BROWSER_PROXY_TOKEN}` |
| R11 | "`/media` 与其他公网入口没有绕过" | 计划 §9 O-0 | — | verify：任何代理到 9090/9100 的路由必须被浏览器样本覆盖且带认证，否则失败；S-06 只允许 127.0.0.1:9090；探针 `/media/<name>` 不得无认证返回图片 |
| R12 | "按路由真源逐路径追加"；"禁止 `/m/v1/*` 通配" | 计划 §9 O-0、§2.1 | P-12 | 直接 import 已提交的片段（wac-060 退役 render）；`check-artifacts` 从清单独立推导片段并逐字节比对（G3、C-1）；verify：`^/m/v1/watcher/` 的 (pattern, methods) 与清单对称差为空、兜底逐字相同且在最后、没有别的处理器转发该前缀 |
| R13 | "记录 watcher 端口映射为宿主 9090 对容器 9100" | 计划 §9 O-0 | — | S-05 `PortBindings`、S-06 |
| R14 | "Caddy 只改浏览器入口的清头与注入；app 不新增任何配置项" | 计划 §4.1 第 1 步 | — | 阶段 C 的脱敏 diff 人工审核（只允许浏览器块与 `wgw_*` 块变化） |
| R15 | "影子三路对照（隔离环境）……先做数据基线（三表非秘密字段导出、主键/行数/内容摘要、逐项差异与预期裁决）" | 计划 §4.1 第 2 步 | P-11 | `o0_watcher_config_baseline.py`；切换 runbook §2 |
| R16 | "所有 operator-query worker 预热成功并记录 revision → 打开新 reader 开关 → 重启对应服务（部署门禁完成后的授权窗口内）" | 计划 §4.1 第 3 步 | P-10 | 切换 runbook §3（含与 C-0 现实现的差异说明） |
| R17 | "外部路由白名单与浏览器回归通过、生产只读/dry_run 冒烟通过、失败恢复演练通过后，删 env 别名与副本（备份保留 30 天）" | 计划 §4.1 第 4 步 | — | 切换 runbook §4 |
| R18 | "关闭开关 → 恢复 env → 重启 → 清缓存；回退数据必须是回退时经校验的真库一致快照，不是 09-06 文件" | 计划 §4.1 第 4 步 | P-17 | 切换 runbook §5 |
| R19 | “记录部署前每节点状态/心跳/审计，部署后保持授权状态（HALTED 保持 HALTED，不把 RESUME 列为部署步骤）；Caddy 变更用 `validate` + `restart`（禁 reload）” | 计划 §4.2 | — | 舰队守卫：apply 首步重新记录基线（节点集合精确、心跳 < 5 秒），变更后稳定窗 + 多次采样比较状态、release、`/ready` 与心跳新鲜度；0 行、缺节点判无法比较；G10 与脚本自检拒绝禁用动词 |
| R20 | "分两段暂停 watcher 容器并实测……未实测不得宣称交易不受影响" | 计划 §4.2 | 切换后才有 61s 口径 | Tester 在隔离环境实测；生产实测是可选授权项 O0-A12 |
| R21 | "按轮换顺序轮换 `gateway` token 期间 app 无报错" | 计划 §4.2 | P-16 | 可选演练 O0-A11 |
| R22 | "浏览器 basicauth 路径回归不变" | 计划 §4.2 | — | 阶段 C/W 探针 + 用户浏览器核对 |
| R23 | "不自动 RESUME；破坏性实验只在隔离环境" | 计划 §8 | — | 全部阶段；无 RESUME 命令 |
| R24 | "生产只做只读/dry_run 冒烟，不做破坏性 CRUD" | 计划 §6.2 | — | 所有 verify 只读；切换冒烟只用 dry_run |
| R25 | "开关默认关，旧 reader 保留"（`WATCHER_CONFIG_SNAPSHOT_ENABLED` 缺省 0） | 计划 §2.2、§4.1；契约 §9.11 | — | WGW-1.0.4：阶段 O 不改 operator-query.env、不重启 operator-query，开关保持现值；快照切换 runbook 另行断言 |
| R26 | 副本"无同步单元，已漂移……切换前必须逐行比对" | 计划 §1.2 | P-17 | S-09 `sync_operator_risk_db.py --check`；基线工具 |
| R27 | "`db_manager.py` 写命令停用或改走服务；feeder 只读" | 计划 D1、§8；契约 §9.12 | P-02/P-03 | G7 + S-15 |
| R28 | "网关独立端口、独立目录与 I-2"（契约 §9.14.6，RS-1、RS-11..RS-20；wac-105） | 契约 WGW-1.0.4；裁定 R23 | wac-104 | I-2 主判据（`i2_check`：整份 adapt JSON 中片段上游之外不得出现独立 8186、数值 8186、含 8186 的端口范围；上游端口或整个 dial 取自请求作用域占位符即 `GATEWAY_PORT_EXPOSED`；解析不出的上游只输出 `UPSTREAM_UNRESOLVED`）；V-2 app 站点 `/v1/watcher/*` 由守卫 404；V-3 探针 8186 与 8183 分桩，只有表内路径打到 8186；V-4 文本中独立 8186 只在 `(watcher_gateway_upstream)` 内；V-5 外部只看状态码（O0-A05X、O0-A08X）；对 8183 的旧检查降为 `HINT`（纵深防御），同形状拨 8186 仍失败；阶段 O `o0_deploy_watcher_gateway.sh`：独立单元、`releases/watcher-gateway/<sha>` 目录、白名单 env、共享目录逐文件对 `67b401a`（漂移即停、交用户）、`NeedDaemonReload` 门、`python -B` 依赖检查、回滚先撤暴露；`cp-isolation` 四单元（RS-17）；打包 G12 含 release 清单、单元 lint、env 白名单 |

## 3. B 类：契约 §9

| ID | 要求 | 出处 | 前置 | 验证方法 / 落点 |
|---|---|---|---|---|
| R28 | "已配置"= 存在且非空；已配置值匹配 `^[\x21-\x7E]{32,}$`，全部两两互异；不合规 watcher 非零退出且只报变量名 | 契约 §9.2；E-02 | P-02 | `check`：同时校验契约格式与 O-0 字母表；自测含短值、`{`、重复值 |
| R29 | 发行侧校验 `WATCHER_*` 与控制面 token 目录两两互异（四个 reader、signal、节点 token）；operator-query 启动再做纵深检查 | 契约 §9.2；E-15 | — | `check --catalog-env` 按 SHA-256 摘要比对，目录 0 值判"无法比较"而失败 |
| R30 | 健康检查在 node 脚本内读 `process.env`，compose `test:` 不插值、不打印 | 契约 §9.2；E-16；A-1 | P-04 | G5；阶段 W verify 健康状态 |
| R31 | 镜像白名单加入 `lib/generated/gateway-routes.js`；控制面发布保留 `generated/`；"部署门禁核对两份产物的 `_meta.yaml_sha256` 与 `_meta.phase_max` 等于本次发布的已审定值" | 契约 §9.14.3；E-09 | P-04、P-05 | G3、G4；阶段 O import 冒烟比对 `RELEASE.json` |
| R32 | "生成路由与真源 diff 为空"三项（生成物 diff、控制面运行时、watcher 运行时） | 契约 §9.14.4 | P-02（watcher 运行时 diff 由 wac-015 落地） | G2 + G9 |
| R33 | 打开开关的前置：O-0 数据基线证明现存行通过 §9.10 校验，并列出六类行 | 契约 §9.11；E-14 | — | 基线工具（复刻 watcher `makeSnapshot` 校验，摘要与 W-0b 实现逐字节一致，见 §6） |
| R34 | 开关打开且缺 `WATCHER_SNAPSHOT_TOKEN` → operator-query 启动失败 | 契约 §9.2、§9.11 | — | 阶段 O 同时下发 snapshot token（开关仍关）；切换 runbook 预检 |
| R35 | `revision` 小于缓存值时记 `snapshot_revision_regressed`（真库回滚场景） | 契约 §9.11 | — | watcher 数据库恢复与切换回滚的预期告警 |
| R36 | `config_audit` 至少保留 30 天 | 契约 §9.12 | — | O-0 不清理审计表；数据库恢复会丢审计，列为破坏性授权 O0-A07R |
| R37 | Caddy 清单是 O-0 核对 Caddy 逐路径配置的输入 | 契约 §9.14.3 | — | `load_list` 解析格式 v2 并校验 `_format`；`verify`/`check-artifacts`/`probe` 只读清单与片段 |
| R38 | 缺 `WATCHER_GATEWAY_TOKEN` 或撞值 → 网关 503 `gateway_disabled`，交易端点不受影响 | 契约 §9.2、§9.3 G5 | — | 阶段 O 下发前 `check` 保证不撞值；verify 走通 200 |

## 4. C 类：审查报告

| ID | 要求 | 来源（报告与编号） | 前置 | 验证方法 / 落点 |
|---|---|---|---|---|
| R39 | O-0 基线列出存量越界或 NULL 的 `default_risk_ratio`、`risk_ratio` 行 | wac-001 🟡-9、A-4 条件 | — | 基线工具 `invalid_number`、`default_risk_ratio_null` |
| R40 | 旧 reader 宽松比较（`lower(trim())`、`1/active/enabled/true`）与 NULL `default_risk` 列为预期差异或逐一核实 | wac-001 💭-2 | — | 基线 `legacy_lenient_*`、`legacy_*_column_disables_row` |
| R41 | 确认现存 `account_id` 都匹配 gateway 路径正则，否则在 app 中不可编辑 | wac-001 💭-3 | — | 基线 `account_id_not_gateway_addressable` |
| R42 | 发行侧互异把 gateway/snapshot 与四个 reader、signal、节点 token 一起比对 | wac-001 💭-4 | — | `check --catalog-env` |
| R43 | 凭据字母表限定 `[A-Za-z0-9_-]`（`secrets.token_urlsafe(32)`） | wac-007 🟡-3 | — | `generate`；`check` 拒绝字母表外字符 |
| R44 | Caddy 用运行时占位符 `{env.WATCHER_BROWSER_PROXY_TOKEN}`，不用 `{$VAR}`（后者会把明文写进 adapt JSON 与 admin API） | wac-007 🟡-3；wac-026 🟡-1 g | — | verify：注入值必须恰为该占位符，字面值报 `<literal len=N>` 并失败 |
| R45 | 替换运行中 watcher 之前，用同一正则与互异规则预检新 env；失败即中止并保留旧容器（否则崩溃循环、信号链中断） | wac-007 🟡-3 | P-02 | 阶段 W preflight `check`；apply 失败自动回滚 |
| R46 | 跨服务互异只比较 SHA-256 摘要，输出只写变量名 | wac-007 🟡-3 | — | 工具输出只有变量名；自测断言输出不含任何生成值 |
| R47 | 镜像白名单加入 `lib/auth.js`、`lib/media.js`、`lib/status.js`、`lib/config-store.js`；加闭包测试 | wac-007 🟡-4 | P-05 | G4（`o0_tool.py closure`）；阶段 W 镜像内 `require` 冒烟 |
| R48 | 控制面发布保留 `generated/` 子目录 | wac-007 🟡-4 | P-08（仅流水线） | bundle `controlplane/api/generated/`；阶段 O manifest 校验 |
| R49 | 重启前用目标 venv 从 staged 目录 import 生成物并核对 `_meta` | wac-007 🟡-4 | P-13 | 阶段 O preflight overlay import 冒烟 |
| R50 | 生成物加载失败只停用网关，不让进程失败 | wac-007 🟡-4 → 裁定 R11 | done（C-1，见 wac-016-r2 §9） | 阶段 O 断言本次加载成功 |
| R51 | 快照 schema 升级时发布顺序"先控制面后 watcher" | wac-007 三条解读之 1 | — | 本次无 schema 变化；写入切换 runbook 的后续发布规则 |
| R52 | O-0 确认 `db_manager.py` 写命令已停用；C-0 首次加载与锁存记 `(revision, content_sha256)` 日志 | wac-007 💭-2 | P-03、P-10 | S-15；切换 runbook §3 |
| R53 | compose 未把六个 `WATCHER_*` 传进容器；O-0 补 env 注入（值不进仓库，不插值进健康检查）并在 runbook 中核对 | wac-009 🟡-5；wac-015 清单第 8 项 | P-04 | G5；阶段 W `env_file` 0600 |
| R54 | wac-011 合入之前，集成分支不得进入任何部署候选 | wac-009 🟡-7；看板 M11 | P-01 | G6；`RELEASE.json.deploy_candidate` |
| R55 | 启动迁移只跑一次：回滚到旧版 watcher 再升回来会出现"同 revision 异摘要"，C-0 锁存 `invalid`；写进 O-0 回滚 runbook | wac-011 第 6 点 | — | 阶段 W 回滚说明；切换 runbook 规则"开关开着时先关开关再回滚 watcher" |
| R56 | 首次迁移拿锁超过 5 秒（例如 `db_manager.py` 正在写）会让 watcher 整体起不来 | wac-011 第 7 点 | P-03 | S-15；apply 前确认无写者；启动失败自动回滚 |
| R57 | 存量行违反 SQLite CHECK 会让同一行上无关的修改得到 400；基线列出这类行 | wac-011-r2 第 6 点 | — | 基线 `check_constraint_violation`、`account_configs_without_check_constraints` |
| R58 | 换容器之前在生产库一致快照副本上先跑一次初始化，确认以 0 结束并通过；写明回滚 | wac-011-r3 🟡-1；看板 M11 | — | 阶段 W build：演练容器正常停止（退出码 0），演练库再经备份 API 拷出新副本后分析（看得到 WAL，🔴-4）；apply、rollback、restore-db |
| R59 | 数据库初始化失败路径的告警 | wac-011-r3 🟡-2；看板 M11 | P-07 | wac-015 代码内告警；阶段 W apply 在自动回滚窗口内断言 90 秒内重启计数为 0 |
| R60 | 启动测试防真实会话 `config.json` 造成双活（同类风险用于 O-0 演练） | wac-011-r3 🟡-3 | — | S-07 要求构建上下文无 `config.json`；演练容器 `--network none` 且挂空配置 |
| R61 | 新开 C-0 校准任务，作为 O-0 数据基线和开关打开的前置 | wac-013 🟡-6 | P-09 done | — |
| R62 | C-0 `_safe_id` 拒绝首尾空白 id（整份快照 invalid），基线补上这一项 | wac-013 💭-3 | — | 基线 `surrounding_whitespace_id` |
| R63 | 生成第三份产物 Caddy 路径清单 | wac-016 🟡-5 | done（wac-026，`be76e92`） | — |
| R64 | 确认 jp-24 `.venv-cp` 解释器 ≥ 3.11，否则网关每个请求 500（进程能起来） | wac-016 🟡-8；wac-016-r2 §7 第 2 条 | P-13 | S-12；阶段 O preflight |
| R65 | 门禁除校验脚本外也跑 P2 断言测试，或核对 `phase_max=P2` | wac-016-r2 §7 第 1 条 | — | G2（脚本已强制 P2，wac-026）+ G9 跑两份 P2 断言测试 |
| R66 | 不把 `{param}` 原样贴进 Caddyfile（未知占位符替换成空串） | wac-026 🟡-1 (a) | — | 片段里只有 `[^/]+`（`check-artifacts` 逐字节核对）；清单的 `{param}` 只作标识，每行 regex 必须等于从模板推导的值 |
| R67 | 每行用 `path`+`method` 具名匹配器；媒体行 HEAD 显式列出 | wac-026 🟡-1 (b) | — | 片段（`path_regexp` + `method`）；verify 比对方法集合，每个 wgw 匹配器恰为 `{path_regexp, method}` 且是该路由唯一的匹配器组 |
| R68 | Caddy `*` 在末尾时是前缀匹配、会跨段 | wac-026 🟡-1 (c) | P-12 | **用户裁决：锚定单段 `path_regexp`**；`--accept-prefix` 已删除；verify 对前缀写法、`(?i)`、`[^/]*`、`.+`、缺 `$`、静态行用 `path` 匹配器都判失败，负样本含多段、空段、尾斜杠、大小写变体 |
| R69 | `/m` 的 handle 块里不得有兜底 `reverse_proxy`，清单外路径落到 404 或其他既有路由 | wac-026 🟡-1 (d) | — | verify 负样本：尾斜杠、多段、空段、未列方法、未注册路径都不得到达网关 |
| R70 | 写核对脚本：解析 adapt 后 JSON，抽出 `/m/v1/watcher/` 全部 `(path, method)` 与清单逐行比较，对称差为空且行数 > 0 | wac-026 🟡-1 (e)；契约 §9.14.3 O-0 核对脚本 | — | `o0_caddy_watcher_routes.py verify`（按 v2 比对 regex 列），自测 52 种破坏（原样与骨架两种形态）全部抓住，真实 Caddy v2.10.2 adapt 的 12 种违例全部判失败 |
| R71 | 确认生产 Caddy 版本（path cleaning 自 #4407 引入） | wac-026 🟡-1 (f) | — | S-01 |
| R72 | 浏览器凭据用 `{env.*}`（同 R44） | wac-026 🟡-1 (g) | — | 同 R44 |
| R73 | WGW-1.0.2 勘误写明清单中 `*` 为"恰好一个非空段"并规定 O-0 翻译方式 | wac-026 §5 第 5 点 | P-12 | Planner |
| R74 | 就控制面代码而言开关前置已满足，O-0 数据基线仍是打开开关的前置（§9.11） | wac-022.md（审查 wac-023）结论 | — | 切换 runbook §2 |
| R75 | 正式提交的持久化证据（`risk_decisions.checks` 含 `config_snapshot`）要有回归测试 | wac-022.md 🟡-C | done（`8066776`，wac-033） | — |

## 5. D 类：运维铁律与现场教训

| ID | 要求 | 出处 | 落点 |
|---|---|---|---|
| R76 | staging 用 `/srv/trader-staging`，不用 `/tmp`（12G tmpfs） | `AGENTS.md` 已知陷阱；协议铁律 | `o0_require_stage_dir` 只接受 `/srv/trader-staging/o0-*` |
| R77 | 部署脚本 `set -eo pipefail`；部署前后 sha 校验并与基线比对 | 协议；记忆“部署脚本两条铁律” | 所有脚本 `set -Eeo pipefail`；每个落盘文件（源码、compose、Caddyfile、env、恢复的库）装后都有 sha 校验；管道末尾不用 `grep -q`，计数用 `awk` 或 `grep -c` |
| R78 | 控制面代码落盘后必须重启对应 systemd 服务才生效 | `AGENTS.md` | 阶段 O 安装后立即只重启 operator-query |
| R79 | node-control 重启会全舰队 HALT；只改 operator-query 时不要动它 | 协议；记忆 | 阶段 O verify 断言另两个单元 MainPID 与启动时间不变 |
| R80 | Caddy 只 `validate` 后 `restart`，禁止 reload；validate 要带 `v3.env` 且不能用 shell `source`（bcrypt 的 `$` 会被展开） | 协议；记忆 jp24 caddy 条目 | `o0_tool.py env-exec` 无展开加载；脚本自检拒绝 reload 字样 |
| R81 | 任何 Caddy restart 可能瞬断节点通道导致全舰队 fail-closed HALT（08-30 发生，08-31 未发生） | 记忆；`INTEGRATION_REPORT.md` §7 | D-02；阶段 C 记录并对比舰队状态，变化即停，不 RESUME |
| R82 | 门禁全部完成于停机之前；门禁失败即中止并保持原版本 | `AGENTS.md` | 机读门禁文件（`*.gate.json`）绑定候选与输入摘要，apply 第一步核对；watcher 候选镜像在停服务前核对存在与 ID；`tests/auth_gate_test.sh` 证明被拒时没有任何写入 |
| R83 | 不采信转述的"已完成"，查审计与心跳 | `AGENTS.md`；协议 | 每阶段前后心跳快照 |
| R84 | 线上 read_api 有热补丁漂移史，部署前务必与基线 sha 核对 | 记忆（09-02、09-10、09-22 热挂载） | 阶段 O preflight `controlplane-context.baseline.sha256`，漂移即中止 |
| R85 | 三个控制面服务都跑 `read_api:app`，共用代码目录 | 记忆 09-24 | D-04；import 冒烟覆盖三种角色 |
| R86 | watcher 重启是信号采集空窗；重启后要核对有无漏信号 | `docs/agent-operations.md` §1；记忆 09-04/09-25 | apply 在自动回滚窗口内断言 180 秒内出现 `[watcher] Connected, listening...`；verify 记录转发行计数；runbook 给出 feeder 计数对照命令 |
| R87 | 凭据零接触：不打印、不提交、不写日志 | 协议铁律 3 | 工具只输出变量名；现场核对默认拒绝输出，Caddy 完整配置不离开主机；泄露测试 15 个哨兵 0 泄露 |

## 6. 本地已实跑的证据（wac-072 时的数字，全部在本机，未连 jp-24；wac-060 的最新结果见 §10）

一键复跑：`O0_WAL_REPRO_DB=<审查 scratchpad/walt/w.db> O0_FLEET_REPRO_DIR=<审查 scratchpad/fleet/evidence> bash scripts/ops/o0/tests/run_all.sh`（两个环境变量可省略，省略时用内嵌的等价夹具）。

| 命令 | 结果 |
|---|---|
| `bash -n` 全部 12 个 shell 脚本；`python3 -m py_compile` 4 个 Python 工具 | `BASH_N_OK files=12`；`PY_COMPILE_OK` |
| `python3 scripts/ops/o0/o0_tool.py selftest` | `SELFTEST_OK checks=88 fleet_cases=11 redaction_shapes=3+2 path_shapes=4+18 path_pins=9 legit_paths=29 gates=14 fleet_params=12 cp_isolation=9 warmup=6`（r2 的四种与 r3 §2.2 的 16 种路径令牌形状，另加以 `-` 分隔的数字令牌与以 `.` 分隔的短段令牌在 `redact-json`、骨架、`redact_path` 与六种 Caddyfile 行上 0 泄露；9 条精确钉住：十六进制规则单独生效、12 位阈值、11 位与 15 位的规格边界、整块替换、`+`/`=` 属于令牌、分隔符去掉后才成立的十六进制、带分隔符才够 12 位的短段；29 条合法路径原样保留；`ok:false`/`"true"`/`1`/`null` 的门禁记录在候选一致的前提下被拒；守卫参数一致性 12 例（年龄区间单独钉住）；控制面单元隔离 9 例，均不打印 env 值） |
| `python3 scripts/ops/o0/o0_watcher_credentials.py selftest` | `SELFTEST_OK scenarios=13 values_checked_for_leak=19 output_lines=79` |
| `python3 scripts/ops/o0/o0_caddy_watcher_routes.py selftest` | `SELFTEST_OK good_passes=108 variants_caught=20/20 (raw and skeleton) lines=16 single_segment_strict=ok skeleton_verify=ok before_deploy_mode=ok` |
| `python3 scripts/ops/o0/o0_watcher_config_baseline.py selftest --repro-db <审查 walt/w.db>` | `SELFTEST_OK … wal_case=ok repro_refused=ok repro_copy_sees_config_revision=ok`（审查的复现文件只在私有拷贝上使用，sha 前后不变） |
| `bash scripts/ops/o0/tests/site_check_leak_test.sh` | `REDACTION_PARITY_OK corpus=74 (base 18 + r3 shapes 18 + pins 9 + legit 29)`；`LEAK_TEST_OK sections=19 sentinels=38 leaks=0 skeleton_parity=ok redaction_parity=ok path_shapes=4+7`（Caddyfile 与 adapted JSON 两处都放了 r2 的四种和 r3 的七种路径令牌；内嵌库自己的 `redact_path`/`redact_line`/骨架逐形状 0 泄露、钉住的输出一致、合法路径不动；S-01 单元行里有短 Bearer 哨兵；S-00 的节点配置与节点 env 里各有一个令牌哨兵，只输出三个数） |
| `bash scripts/ops/o0/tests/fleet_guard_test.sh`（审查的 fleet-a…d 复现文件） | `FLEET_GUARD_TEST_OK cases=7`：空对空 → 退出 2；`hb_age 1.0 → 412.7` → 退出 3；第 3 次采样才出现的 HALT → 退出 3 |
| `bash scripts/ops/o0/tests/auth_gate_test.sh` | `AUTH_GATE_TEST_OK checks=18`：绑定矩阵 475 组合 0 误判；runbook 示例 34 条全部通过绑定表；沙箱内 apply 无门禁、候选镜像缺失、凭据变了、候选变了都在写入前被拒，无状态变更类 docker 调用；步骤顺序（`STRUCTURE_OK`，见下一行第 1 部分） |
| `bash scripts/ops/o0/tests/apply_rollback_test.sh` | `APPLY_ROLLBACK_TEST_OK checks=93`：(1) plan 输出的步骤顺序（另含 restore-db 顺序与阶段 O 隔离门禁的位置）；(2) 沙箱内真脚本 `--execute`：Caddy、watcher、operator-query 各一次"替换前失败"（不 restart/不重建，文件逐字节还原）与"替换后失败"（还原后 restart/重建一次），Caddy 另有"回滚自身失败"（立即停下、不再 restart、记 `ROLLBACK FAILED`）；审查 r3 的 D（restart/重建命令本身失败）、E（守卫判变化时只停下报告、不自动回滚）、F（候选清单含 `ABSENT` 行：删除、核对、回滚复原）；restore-db 成功（原属主与权限保持）、启动失败、装回失败、装回字节不符时自动恢复到原库，移动丢了属主时恢复拒绝启动 watcher（`ROLLBACK FAILED`），重跑被拒；阶段 O 隔离门禁两种违例在写入前拒绝且不打印值；守卫参数：默认值在真实 execute 中通过并记入 `authorizations.log`，不一致被拒，一致的非默认组合生效，跳过缝离开沙箱无效；node-control/event-ingest 从未被改变状态 |
| 五个脚本 plan 模式 | 退出 0，不执行任何动作 |
| 打包门禁 `o0_package.sh --report-only --run-tests`（未带 --execute） | 见 §6.1：只剩 G7 失败，`deploy_candidate: false` |

### 6.1 打包门禁（本机，`--report-only --run-tests`，未带 --execute）

命令：`bash scripts/ops/o0/o0_package.sh --candidate 0eeca85 --out <scratchpad>/pkg-wac072 --report-only --run-tests`（候选必须是带 `scripts/ops/o0` 的提交，G11；`0eeca85` = wac-072 的代码提交，基于集成分支 `487f6eb`；wac-059 时的候选为 `ce2461a`，结果相同）。

| 门禁 | 结果 |
|---|---|
| G1、G2（`ROUTES_DIFF_EMPTY rows=63 … phase_max=P2`）、G3、G4（`CLOSURE_OK closure=13 whitelist=34`）、G5（`COMPOSE_OK env_file=True`）、G6、G8（5 个文件）、G10（17 个候选工具与 runbook 文件）、G11（14 个工具文件取自候选）、G12（`RUNTIME_MANIFEST_OK files=34`） | PASS |
| G9 | PASS：pytest `26 passed`；watcher `tests 107 / pass 107 / fail 0 / skipped 0` |
| G10 说明 | 本轮新增的 runbook 文本与脚本（W-6 手工路径、单元隔离、节点心跳参数）也在扫描范围内，无禁用动词 |
| G7 | **FAIL**：wac-015 未合入（P-02） |
| 结论 | `deploy_candidate: false`，`failed_gates=1`；不能申请 O0-A02 之后的授权 |

## 7. 审查 wac-032 的逐条处置

### 7.1 🔴

| # | 处置 | 落点与测试 |
|---|---|---|
| 🔴-1 舰队守卫空转、看不见冻结心跳 | 采样改为 §0 查询（状态、release、心跳年龄，NULL 显式打印）+ `/ready`；比较移到 `o0_tool.py fleet-compare`：节点集合必须恰好等于 `O0_FLEET_NODES`，0 行、缺节点、多节点、不可解析 → 无法比较（退出 2）；心跳 ≥ 5 秒（§0 阈值）或年龄跃升 > 5 秒 → 变化（退出 3）；基线本身不新鲜 → 拒绝开始。apply 第一步重新记录基线，preflight 的样本只用于门禁。变更后等稳定窗（默认 60 秒）再采 4 次、间隔 20 秒，每次都比较。回滚只记录回滚前样本，不设门槛 | `o0_common.sh`（`o0_fleet_baseline`、`o0_fleet_settle_compare`、`o0_fleet_record`）；`tests/fleet_guard_test.sh`；`o0_tool.py selftest` 11 个比较用例 |
| 🔴-2 授权号不绑定阶段 | `o0_expected_auth` 表：每个脚本阶段只接受一个号（caddy 全阶段 A05；watcher preflight/build A04、apply-preflight/apply/verify/rollback A07、restore-db A07R；oq 全阶段 A08；守卫按步骤名 R-x/SW-x 绑定）。restore-db 另需 `--i-understand-data-loss`，A07 的号执行不了它。`authorizations.log` 记录时间、脚本、阶段、号、候选、是否确认数据丢失。runbook 所有示例改成完整命令 | `tests/auth_gate_test.sh`（矩阵 + runbook 示例 + 沙箱端到端） |
| 🔴-3 apply 不依赖门禁结论 | preflight/build 开头删除旧门禁文件，全部通过才写 `evidence/<阶段>.gate.json`（候选、`RELEASE.json` 与 `SHA256SUMS` 摘要、输入文件摘要、候选镜像 ID）；apply 第一步 `gate-check`（存在、阶段、候选、bundle、输入摘要、≤ 1 小时 / build ≤ 24 小时），再重跑便宜门禁。watcher apply 在任何备份与停服务之前核对候选镜像存在且 ID 等于 build 门禁、演练日志未变且含 `MIGRATION_DRYRUN_OK`。Caddy apply 在 restart 之前核对已安装文件等于门禁里的候选 sha，并对已安装文件重新 adapt 与 verify | `o0_common.sh`（`o0_gate_*`）；`o0_tool.py gate-write/gate-check`；`tests/auth_gate_test.sh` 沙箱用例；selftest 10 个门禁用例 |
| 🔴-4 基线工具看不到 WAL | `open_copy` 去掉 `immutable=1`，改 `mode=ro`；发现非空 `-wal` 即拒绝（`wal_not_checkpointed`，退出 2）。`sqlite-backup` 的副本切成回滚日志模式。演练容器改为 `docker stop -t 30`（SIGTERM，退出码须为 0），分析前再用备份 API 从演练库拷出新副本，在新副本上分析；基线退出码写入证据，2 = 失败 | `o0_watcher_config_baseline.py selftest`（WAL 回归 + 审查复现文件）；`o0_deploy_watcher.sh` build |
| 🔴-5 Caddy JSON 脱敏只挡已知形状 | 现场核对不再带回整份 Caddy 配置：S-03/S-04 输出白名单骨架（只保留匹配器、handler 顺序、rewrite、上游、头名、占位符；其他字符串一律 `<literal len=N>`），文件与运行配置在主机上比较，只带回 yes/no 与不同处路径。`redact-json` 改为两层（敏感上下文默认拒绝 + 所有字符串过行规则），并新增 URL 查询值规则。S-02 大纲与 compose environment 也改为默认拒绝 | `tests/site_check_leak_test.sh`（新增头匹配器 Bearer、replace、`static_response.body`、vars、查询值、短字面值；骨架与 `o0_tool.py` 输出一致性）；`o0_tool.py selftest` |

### 7.2 🟡

| # | 处置 |
|---|---|
| 1 镜像构建方式 | 改用 `scripts/build_immutable_watcher_image.py`：以运行中镜像 ID 为固定基础，只 `COPY` 已审白名单文件，`--network=none --no-cache`，不联网安装依赖；bundle 带运行时清单与来源清单，打包门禁 G12 用构建器自己的校验；build 列出运行中镜像里白名单之外的文件（`BASE_ONLY`）供用户审阅，记录基础镜像的 `org.trader.*` 标签 |
| 2 Telegram 恢复无断言 | 去掉 `\|\| true`；apply 在自动回滚窗口内等待 `[watcher] Connected, listening...`（180 秒超时），`Session not authorized` 立即失败；verify 断言仍连着且重启计数 0，记录转发行计数；runbook 给出 feeder 对照命令 |
| 3 落盘文件 sha | compose 装后 `manifest-verify compose-live-vs-candidate`；Caddyfile 装后与门禁候选 sha 比对；三处 env 装后用 `check --holder-env <线上文件>` 按摘要确认；恢复的库与备份 sha 比对 |
| 4 G10 太窄 | 禁用词加入 `caddy load`、`:2019/load`、`/v1/commands`、node-control/event-ingest 的 restart/stop/kill、`docker compose down/restart/kill/rm`、节点容器 stop/kill/restart；扫描候选里的 `o0_*.sh`、`o0_*.py` 与 `docs/agent-team/release/o0-*.md`；管道末尾改 `grep -c` <!-- o0-allow --> |
| 5 工具来自工作树 | bundle 的 `tools/` 只取自候选提交（新增 G11），`RELEASE.json` 记录 `tools_sha256` |
| 6 轮换与切换是手工序列 | 每一步前后加 `o0_fleet_guard.sh`（授权号按步骤绑定）；**不处理**的部分：包成带自动回滚的 `o0_rotate.sh`、`o0_snapshot_switch.sh`。理由：这两段依赖的 wac-015（E-02 双值、`db_manager` 停用）与 wac-041（预热日志）尚未合入，接口可能再变；它们在 O0-A11/A13 之前才需要，届时单独开任务并按本轮同样的门禁与测试标准写 |
| 7 回滚 5.2 依赖 watcher | 默认路径改为不依赖 watcher：同一窗口对真库做 `sqlite-backup` 并算 `content_sha256`，再对重建的副本拷贝做 `--expect-content-sha256`；watcher 快照只作可选第三方互证；只有默认路径也无法比较时才上报用户 |
| 8 整份还原 env | 改为只 `apply` 开关与三个别名键；恢复后复跑凭据 `check` |
| 9 S-09 执行未知代码 | 主机脚本 sha 必须等于 `--expect-sync-sha256` 才执行 `--check`，否则跳过并记为待核 |
| 10 进程只持自己的凭据 | S-10 断言 node-control/event-ingest 的 EnvironmentFiles 不含 `operator-query.env`，违反即阻断阶段 O |
| 11 Caddy 探针地址 | r7 改为 `--node-channel`，默认 `172.30.1.1:8080`，列为 S-06 现场确认项 |
| 12 `GATEWAY_STARTUP_CLEAN` 没看到就算过 | apply 在 ERR trap 内直接用 `SYSTEM_OBSERVER_TOKEN` 打网关期望 200；日志窗口改为重启前记下的 `@epoch` |
| 13 restore-db 无校验 | 先断言开关为 0、备份 sha 与 `integrity_check`；装回后字节比对；启动后断言 `Web UI listening`、无 `[db] Failed`、Telegram 重连；前后舰队守卫 |
| 14 回滚不处理 env_file | apply 备份记录 env_file 原状态；回滚时原有则还原、原本没有则删除 |
| 15 catalog 只传一个文件 | 三个部署脚本支持重复 `--catalog-env`；runbook 与授权清单写明由 S-10/S-11 推导文件清单 |
| 16 "被接受"只看 400 | 探针加 `--expect-code invalid_actor_headers` |
| 17 脱敏 diff `; true` | 只容忍 `diff` 的退出码 1；脱敏失败或结果为空即失败 |
| 18 基线 `\|\| true` | 退出码写入证据，2 = 失败；迁移后分析失败即失败 |

### 7.3 💭

| # | 处置 |
|---|---|
| 1 演练 env 对齐 | 加 `HERMES_TRADER_CRON_ENABLED=0`、`SIGNAL_IMPORTER_ENABLED=0`，清空 `WATCHER_ALERT_*` |
| 2 verify 要求清掉浏览器 Authorization | 已加入，自测新增"浏览器保留 basic Authorization"变体 |
| 3 `grep -q` 在管道末尾 | `o0_forbid_patterns` 与各计数改为 `grep -c` 或 `awk` |
| 4 候选号过时 | 本文件头与 §6 按 `f7641cb` 更新 |

### 7.4 本轮新增、需要注意的限制

- 舰队守卫稳定窗默认值（60 秒 + 3 × 20 秒）没有仓库里的节点 fail-closed 判定时长可依据，列为用户确认项（授权清单 §二）。
- 以运行中镜像为基础做 overlay：候选不再携带的白名单文件（manifest 中 `ABSENT`）不会从镜像里删除；构建器要求白名单全集存在，所以目前不会出现这种情况，一旦出现需改为全量基础镜像另行审定。
- `tests/auth_gate_test.sh` 用 `O0_SANDBOX` 测试缝；它在任何存在 `/srv/trader-v3` 的主机上被拒绝，不会放宽 jp-24 上的检查。

## 8. 复审 wac-032-r2 的处置（wac-059，第 3 轮，范围按 Planner 限定）

| 项 | 处置 | 证据 |
|---|---|---|
| 🔴-1 路径里的字面秘密 | `o0_tool.py` 与 site check 内嵌库同步加 `redact_path`：按 `/` 与正则元字符切段，段长 ≥ 12 且同时含字母和数字，或 ≥ 16 位十六进制，换成 `<seg len=N>`，其余段保留。用在三处：`redact_line` 对每个含 `/` 的词（覆盖 `redact`、Caddyfile diff、S-02 大纲、S-10/S-16 单元行）、骨架的路径键（`path`、`pattern`、`uri`、`root`、`strip_path_*`）、`redact-json` 第二层。顺带让 S-01 的 `systemctl cat caddy` 行也过 `$REDACT`（新加的短 Bearer 哨兵在这里泄露过） | 审查的四种形状在 leak test（Caddyfile 与 adapted JSON）和 selftest 里 0 泄露；`^/m/v1/watcher/trading/risks/[^/]+$`、`/m/v1/watcher/media/*` 原样保留；caddy selftest `skeleton_verify=ok` 不变 |
| 🟡-1 自动回滚多余的 restart、回滚后不跑守卫 | `o0_common.sh` 新增 `o0_arm_auto_rollback` / `o0_mark_runtime_replaced` / `o0_auto_rollback`。标记放在 restart/重建步骤**之前**（restart 失败也可能已改变服务，按"已尝试"算）。回滚先还原文件（fail-fast，失败就不再 restart），只有标记之后才 restart/重建；然后对 apply 基线跑一次舰队守卫（只报告），结论写 `evidence/auto-rollback.log`，退出 1。三个脚本各自拆成 `rollback_files` 与 `rollback_runtime`；手动 `rollback` 阶段两者都做。runbook C-2、W-3、O-2 的失败处置已改 | `apply_rollback_test.sh` 7 个沙箱场景；变异 R1a–R1h 全部被抓 |
| 🟡-2 步骤顺序无测试 | plan 输出结构测试：preflight/build 首步清旧门禁、末步写门禁；apply 首步 REQUIRE 门禁；舰队基线 < 备份 < 回滚上膛 < 第一个线上写入；`RUNTIME_REPLACEMENT_BEGINS` 紧挨 restart；restart 后有守卫且默认 60 s + 4 × 20 s；演练 `docker stop -t 30`、无 `docker kill`；`dryrun.post.db` 由 `sqlite-backup` 产出且分析对象是它。`auth_gate_test.sh` 也调用这一部分，审查的原变异命令不改就能抓到 | 变异 1g、1h、3g、4d、4e、P1–P4 被抓 |
| 🟡-3 `ok:false` 断言空转 | 先把 `RELEASE.json` 改回候选 `c…c` 并断言门禁重新通过，再只改 `ok`；另测 `"true"`、`1`、`null` | 变异 3j 被抓 |
| 🟡-4 内嵌 `redact_line` 未钉住 | parity 从骨架扩到 `redact_line`、`redact_path`，用 18 条语料（短 Bearer、basic、查询值、`KEY=VALUE`、`Environment=`、四种路径令牌、合法路径）；S-01/S-10 桩里加短 Bearer 哨兵 | 变异 5e 被抓 |
| 本轮顺带发现 | watcher apply 的安装步骤 `grep '^ABSENT  ' … \| …` 在清单没有 `ABSENT` 行时（正常情况）在 pipefail 下失败，线上每次 apply 都会走进自动回滚。改为 `awk`。沙箱测试首次跑就暴露了它 | 变异 W1（改回 grep）被抓 |

变异复跑（审查的 `mut046/muts.py` 变异集，复制到 scratchpad `mut059*/`，pristine 取本轮代码）：

- 审查原测试命令（不含新测试文件）：37 个变异里 30 个被抓；3 个存活（4a、5d、5f，审查已判为等价或纵深防御）；5b、5c 的锚点因 🔴-1 改动消失，按新代码重新定锚后 2/2 被抓。
- 加上 `apply_rollback_test.sh`：结果相同。
- 本轮新增 22 个变异（B1a–B1i、R1a–R1h、W1、P1–P4）：21 个被抓；B1h（大纲 `classify` 不再按段替换）存活，属等价变异：每个 token 随后还会经过 `redact_line` 的路径规则。

未在本轮处理（按范围限定，记为待办）：🟡-5（restore-db 失败的恢复路径与原属主）、🟡-6（S-10 隔离检查进阶段 O preflight）、🟡-7（守卫阈值取自生产节点配置）、💭 1–5；WGW-1.0.2 的 Caddy 清单 v2 与片段适配（§7 of r2）归 wac-060。

## 9. 终审 wac-032-r3 剩余待办与 r2 🟡-5/6/7 的处置（wac-072，未连 jp-24）

| 项 | 挡哪一步 | 处置 | 证据 |
|---|---|---|---|
| r3 🟡-1 路径令牌碎片 | O0-A01 | `redact_path` 改为两遍（`o0_tool.py` 与 site check 内嵌库逐字相同）：先按 `/` 切块；每块去掉 `{占位符}` 与正则语法后判一次、再去掉分隔符（`.` `-` `_` `:` `~` `%` `&` `,` `;` `!` `@`）判一次，任一次像令牌就把**整块**换成 `<seg len=N>`；不像的块仍走原来的按段规则（只会多脱敏，不会少）。`+`、`=` 是 base64 字符，不去掉（审查原型差的那一位）。占位符先去掉，`/api{http.request.uri.path.1}` 这类结构不被误伤。代价：整块替换后块尾的 `$` 等正则语法也被遮住，只影响可读性，合法路径不受影响 | r3probe `newshapes.py` 复跑：N3b、N3c、N5b、N7 在六个输出面全部 0 泄露；selftest 与 leak test 覆盖 r3 的全部 16 种形状（含裸 base64）另加 2 种（`-` 分隔的数字、`.` 分隔的短段），parity 语料 74 条；29 条合法路径（生成清单 16 条 + 审查列出的 12 条 + 1 条占位符）0 误伤。规格内的残余形状（N2c、N4c、N4d、N8）已写进授权清单 O0-A01 的影响栏，由用户知悉 |
| r3 🟡-3 测试缺口 | 无（质量项） | 十六进制规则单独钉住（16 位纯数字、16 位纯字母十六进制，两份实现各自断言）；12 位阈值钉住（顺带 r3 💭-2）；审查的 D、E、F 沙箱场景并入 `apply_rollback_test.sh` | 变异 X1、X2、X3、X9 由存活变为被抓 |
| r2 🟡-5 restore-db 恢复路径与属主 | O0-A07R | 移动之前用 `stat` 记录属主与权限、文件清单与 sha（evidence）；`db-replaced/` 非空即拒绝；装回按记录的属主与权限，核对字节、属主、权限；停 watcher 之后任一步失败自动恢复：确保已停，装回失败的那份与失败启动留下的 `-wal`/`-shm` 移到 `db-failed-restore/`，原文件 `mv` 放回并核对 sha、属主、权限，再启动 watcher、检查启动日志、跑舰队守卫（只报告）；恢复本身失败不启动 watcher。runbook W-6 写明手工路径；S-05 补 `.Config.User` | 沙箱：成功、重跑被拒、启动失败、装回失败、装回字节不符、移动丢属主 6 组场景（inode 级的 `chown`/`stat` 桩：`mv` 保留属主，`cp` 生成 root 属主的新文件）；plan 结构测试钉住顺序 |
| r2 🟡-6 S-10 隔离进阶段 O | O0-A08 | 新增 `o0_tool.py cp-isolation`，在 O-1 preflight（写门禁之前）与 O-2 apply（备份之前）运行：三个单元必须 `LoadState=loaded`；node-control/event-ingest 不加载 `operator-query.env`，其 `Environment=` 与各自 env 文件里没有 `WATCHER_*TOKEN` 名字；operator-query 的 WorkingDirectory 必须是 `--cp-root` 或其 `api/`；报告是否共用代码目录（D-04 是否适用）。只读名字，不打印值 | selftest 9 例（含值不外泄断言）；沙箱两种违例在任何写入前拒绝；plan 结构测试钉住位置 |
| r2 🟡-7 守卫阈值对齐生产心跳参数 | O0-A01 采集，O0-A05 起使用 | S-00 在每个节点容器里只打印三个数（配置的 `heartbeat_timeout_seconds`、镜像代码的 `DEFAULT_HEARTBEAT_INTERVAL_SECONDS`、`node.py` 是否覆盖），再实测约 10 秒的心跳年龄最大值。读的是**运行中容器里**的文件，所以 09-22 那类热挂载也会反映出来。新增 `O0_NODE_HB_INTERVAL_S`/`O0_NODE_HB_TIMEOUT_S`（默认 2/15，守卫阈值默认值不变），每次 `--execute` 用 `fleet-params` 核对：年龄与跃升在 [2, 3] × 间隔内，稳定窗 ≥ 判定时长 + 3 × 间隔；不一致即拒绝，并给出建议值；实际参数写进 `authorizations.log`。与默认不一致时的调整与用户确认写在部署 runbook §0 | selftest 12 例；沙箱：默认值在真实 execute 中通过并记录，两种不一致被拒且不采样，一致的非默认组合传到 `fleet-compare`，跳过缝离开沙箱无效；leak test：节点配置里的字面 token 与节点 env 的 token 不外泄 |

**测试缝**：沙箱测试用 0 秒的稳定窗，与规则冲突，所以新增 `O0_FLEET_PARAMS_SANDBOX_SKIP=1`。它只在同时设置了 `O0_SANDBOX` 时生效，而 `O0_SANDBOX` 在有 `/srv/trader-v3` 的主机上直接拒绝。另有测试证明离开沙箱时这个变量不起作用。

**O0-A01 的范围变化**（已写入授权清单）：S-00 新增对每个 `trader-v3-node-*` 容器的只读 `docker exec`（只打印三个数），以及约 10 秒内 6 次只读心跳查询。

### 9.1 变异复跑（全部在 scratchpad，pristine 取本轮最终代码）

- 审查 `mut067b/muts.py`（9 个，测试集含 `apply_rollback_test.sh`）：8 个被抓。**X1、X2、X3、X9 由存活变为被抓**；X7（按段切分去掉 `\`）仍存活，审查已判为等价变异：少切只会多脱敏，而且整块规则先于按段规则判定。
- 审查 `mut067/muts.py` 与 wac-059 新增（共 57 个，测试集含 `apply_rollback_test.sh`）：
  - 4a、5d、5f、B1h 存活，审查已判为等价或纵深防御。
  - 5b、5c、R1d 锚点不再唯一：5b、5c 按审查的重定锚版本 2/2 被抓；R1d 因 restore-db 新增了一处 `o0_mark_runtime_replaced`，改锚到 apply 的那一处后被抓。
  - 其余全部被抓。
- 本轮新增 29 个（`mut072b/muts072.py`）：
  - 路径规则 9 个：两份实现各自去掉整块规则、只判一种形态、去掉 `+`、不去占位符、阈值 13、去掉十六进制规则。
  - restore-db 7 个：写死 `root:root`、去掉自动恢复、去掉重跑拒绝、去掉运行时标记、恢复不核属主、装回不核 sha、恢复不放回原文件。
  - 隔离门禁 6 个：apply、preflight 各去掉门禁，工具去掉 `Environment=` 检查、env 文件检查、工作目录检查、`LoadState` 检查。
  - 守卫参数 6 个：去掉检查调用、去掉稳定窗下限、去掉年龄区间、跳过缝不要求沙箱、S-00 打印整段配置、默认值改动。
  - 另有 R1d 重定锚 1 个。
  - 首轮暴露了 N3、R5、R6、F3 存活，分别补了测试：`-` 分隔的数字令牌与 `.` 分隔的短段（同时把块判定改为"两种形态任一"）、装回字节不符与移动丢属主两个场景、单独越界的年龄区间。
- **最终复跑结果**：
  - 本轮 29/29 被抓。
  - 审查 mut067b：8/9 被抓，X7 存活（等价）。
  - mut067 与 wac-059：57 个里 50 个被抓；4 个存活（4a、5d、5f、B1h，均为已判等价）；3 个锚点失效，重定锚后 3/3 被抓。
  - 记录在 scratchpad `mut072a/final-all.txt`、`mut072a/final-reanchor.txt`、`mut072b/final-067b.txt`、`mut072b/final-072.txt`。


## 10. wac-060：适配 WGW-1.0.2，处置 wac-073 复审（未连 jp-24，未做只读生产核对）

依据：`reviews/wac-015b.md` §7"O-0（wac-060）需适配的清单"9 项；契约 §9.14.3、§9.16 F-04、F-10、F-12、F-13；`reviews/wac-072.md`（wac-073 复审）🟡-1…🟡-5；用户裁决（Caddy 参数段严格"恰好一个非空段"，锚定 `path_regexp`，区分大小写）。

### 10.1 适配清单 9 项

| # | 项 | 处置 | 证据 |
|---|---|---|---|
| 1 | `load_list` 按 v2 解析并校验 `_format` | 头部四行按序校验（`_generated_from`、64 位十六进制 `_yaml_sha256`、`_phase_max`、`_format == watcher-gateway-caddy-paths.v2`）；每行 `<template> <regex> <METHOD…>`：模板每段是 `[a-z0-9-]+` 或 `{name}`（S-22），regex 必须等于从模板推导的 `^…$`（`{param}` → `[^/]+`），方法唯一、有序、在已知集合内，模板按字节序严格升序；全文 ASCII、LF、单个结尾换行、不含 `*`；0 行判无法比较。比对一律用清单的 regex 列 | selftest 10 种清单破坏（v1 `_format`、缺 `_format`、`*` 行、regex 与模板不符、`(?i)`、方法乱序、行乱序、CRLF、0 行、大写段）全部拒绝 |
| 2 | `render` 退役 | 删除 `render` 子命令与"不得加兜底、不得加 `(?i)`"的注释和逻辑（与 F-10 相反）。Caddyfile 直接 import 已提交的片段；工具只做 `check-artifacts`：按 §9.14.3 的片段形状从清单**独立**重新渲染（不 import 生成器）并逐字节比对，兜底按 `paths` 独立拼出 `^(?i:/m/v1/watcher)(?:[/\n]|$)` | selftest 8 种片段破坏（`_format`、旧兜底 `(?:/|$)`、删兜底、兜底 200、行级 `(?i)`、多一个方法、片段里写上游地址、yaml sha 不一致）全部拒绝；在 clone 里提交旧兜底后打包，G3 以 `CADDY_ARTIFACTS_FAILED … line 119` 独立失败（G2 也失败） |
| 3 | `verify` 重写 | (a) 全配置里 `^/m/v1/watcher/` 开头的 (pattern, methods) 与清单 (regex, methods) 对称差为空、无重复、行数 > 0；每个 wgw 匹配器恰为 `{path_regexp, method}`（兜底恰为 `{path_regexp}`）且是其路由唯一的匹配器组；其他含 `watcher` 的 `path_regexp` 一律失败。(b) 兜底恰好一次、逐字相同、只 `respond 404`、在同一列表里与逐路径路由连续且排在最后；wgw 路由必须在 app 站点的**顶层**路由列表里（`route {}`、`handle_path` 包裹即失败）。(c) F-13 两步遮蔽检查：第一条 wgw 路由之前（本层与外层每一层，以及外层容器里先于 subroute 的 handler）的每一条路由，先用三个探针判命中（`path` 按 Caddy 语义、`path_regexp` 按 RE2、`host` 可排除，其余匹配器与无法编译的正则都算命中），命中后要求 handler 全在白名单（`encode`、不带 `request` 的 `headers`、`vars`、`map`、`log_append`、`tracing`）、无 `terminal`、无 `group`、handle 非空。(d) 没有任何其他路由把前缀（含 `/m/v1/watcherx`、`\r`、`#`、大小写变体）转发到 8183 或 watcher。(e) 模拟器改用 RE2 的 `$`（只在文本末尾），按 §9.14.4 第 1 项生成负例（空段、`x/y`、尾斜杠、整路径大写、字面行 `/x`、字面行 `\n`/`#x`、每个未列方法）与正例（参数行 `\n`/`#x` 必须到网关），兜底探针必须由兜底 404 应答。原 `:453-456`、`:507-509` 的 `*` 判断随 v2 删除。打印的路径与正则一律经 `o0_tool.redact_path` | selftest：52 种破坏在原样与骨架两种形态下全部抓住（其中 9 种是只有该条检查能抓的隔离用例；外层容器 handler、清单与片段一致篡改的 regex、另一个块里多出的 import 这 3 个隔离用例在变体表之外，见 §10.5），5 种合规的两步用例（白名单 handler 命中、非白名单 handler 不命中、`not` + `encode`、其他 host）通过；真实 Caddy 见 §10.4 |
| 4 | selftest 夹具改 v2 | `_fixture` 按真实 Caddy v2.10.2 对仿生产 Caddyfile（`tests/fixtures/caddy/Caddyfile.prodlike.in`）adapt 的结构重写：站点顶层 `headers`(响应)+`encode` 路由、16 条 wgw、兜底、移动端、面板 `/v1/*`、watcher basicauth 块（先 `strip /watcher`，再认证、四条 `headers` 删头、注入）、SPA；变体按片段结构重写 | `SELFTEST_OK good_passes=241 variants_caught=52/52`；`tests/caddy_real_test.sh` 对同一夹具的真实 adapt 结果 verify 通过 |
| 5 | 打包脚本 | G3 读两份 Caddy 生成物的四行头部，校验 `_format` 与 `_yaml_sha256`/`_phase_max` 一致，再用**候选自己的** `check-artifacts` 核对片段；bundle 带 `caddy/caddy-watcher-gateway.caddy`（原样拷贝，不再 render）；RELEASE.json 从 G3 日志里找 `META_OK` 行 | 打包门禁 G1–G12 全部 PASS，见 §10.3 |
| 6 | `o0_deploy_caddy.sh` 三处 verify | 三处都改为 `verify --paths … --snippet …`；另外：preflight 加 `check-artifacts`、线上片段文件不存在或与 bundle 相同、把片段放到候选同目录（相对 import）、`caddyfile-check`，门禁记录 staging 片段的 sha；apply 在凭据合入之后安装 `/etc/caddy/caddy-watcher-gateway.caddy` 并比对字节，备份时记录它原本是否存在，回滚时恢复或删除；verify 阶段核对线上片段仍等于 bundle | `apply_rollback_test.sh`：SN1（片段安装失败：不 restart，片段被删除）、SN2（片段原本就在：restart 后失败，恢复原片段）、SN3（线上有不同的片段：门禁处拒绝，没有备份、写入或 restart）；原有 Caddy A–E 场景同时断言片段被删回 |
| 7 | `tests/apply_rollback_test.sh` 恢复 | 随第 1 项恢复；并入审查的 5 个 restore-db 场景（🟡-4）与上述 SN1–SN3、隔离门禁的 reload/unparsed 两种 | `APPLY_ROLLBACK_TEST_OK checks=138` |
| 8 | runbook 并入规则 | 部署 runbook §3.1 重写：全局位置 `import caddy-watcher-gateway.caddy`（相对路径）、手写 `(watcher_gateway_upstream)`、站点顶层在所有 `handle`/`handle_path`/`route` 之前 `import watcher_gateway_routes`；第 4 条要求按 F-13 (2) 人工记录全局 `order` 选项与站点顶层前置指令（`caddyfile-check` 与 S-02 打印底稿，参数脱敏）；C-1…C-4、故障对照同步更新 | G10 扫描 17 个候选工具与 runbook 文件无禁用动词 |
| 9 | 本机 Caddy 探针 | 新增 `o0_caddy_watcher_routes.py probe`：对生产 Caddyfile 副本只改站点地址、两个上游与全局 `admin off`/`auto_https off`/端口（逐行差异脱敏打印），Caddy 状态目录放临时目录；先 adapt + verify，再真实运行并用原始请求行发送 §9.14.4 第 1 项全部探针（含 `%0A`、`#`）与 F-13 (3) 两类用例：**字面点段与 `//` 期望 clean 后转发，不按 404 判定**；编码形式与 `#` 原样转发。生产副本的探针需要授权 O0-A05P | 仿生产夹具上 `CADDY_PROBE_OK caddy=v2.10.2 live_checks=224`；把 `handle /m/*` 放在 import 之前的副本 `CADDY_PROBE_FAILED` |

### 10.2 wac-073 复审 🟡-1…🟡-5

| 项 | 处置 | 测试 |
|---|---|---|
| 🟡-1 工具链停摆、授权清单开头的 G7 说明过时 | 本任务（§10.1）；授权清单开头改为当前门禁状态 | `run_all.sh` 全过；打包 G1–G12 PASS |
| 🟡-2 `+`/`=` 分隔的纯数字令牌（O0-A01 前） | 两份实现的 `_chunk_is_token` 第二次判定同时去掉 `+`、`=`（第一次仍把它们算作令牌字符，N7 不变）。M1/M2/M12（自身含 `/` 的令牌）写进 O0-A01 的残余风险 | M3、M4 与转义版 M3 加入 `PATH_SHAPES_R3`（六个输出面 0 泄露）；钉住 `/n/4829+1057+3829+1045` → `<seg len=19>`、`=` 版同理，15 位（去掉 `+` 后）原样保留；parity 语料 83 条 |
| 🟡-3 隔离门禁"解析不了当通过"、看不到未加载的 drop-in（O0-A08 前） | `EnvironmentFiles=` 用 `^(-?)(/.*?)(?: \(ignore_errors=(yes\|no)\))?$` 从右边切开后缀；不以 `/` 开头、后缀未知、无后缀却含空白的值都进 `unparsed` → `CP_ISOLATION_UNCOMPARABLE`；另读 `NeedDaemonReload`（不是 `no`，含缺失，即 UNCOMPARABLE）、`FragmentPath`、`DropInPaths`（写进证据行）；S-10 同步只读这三项 | selftest 新增 6 例（无 `/`、未知后缀、无后缀含空格、`NeedDaemonReload=yes`、缺失、带后缀的含空格路径正确解析并查出变量名）；沙箱 ISO-reload、ISO-unparsed 在任何写入之前拒绝 |
| 🟡-4 测试缺口 | 审查的 5 个 restore-db 场景（stop 失败、start 失败、移走途中失败、失败启动留下 `-wal`、恢复时 sha 不符，外加恢复先停 watcher 的断言）原样并入；隔离门禁补软链（pre-O2 的 `operator-query.env` 不含 watcher 名字，只有 realpath 能抓）、systemd `(ignore_errors=yes)` 写法指向不存在的文件（判 OK）、另一目录下名为 `operator-query.env` 的文件（判违例）3 例 | D2、D3、D4、D7、D11、D18 见 §10.5 |
| 🟡-5 S-00 把 NULL 心跳年龄算成 0（O0-A01 前） | awk 对非数值样本计数，输出 `observed_max_hb_age <节点> <uncomparable:NULL> samples=N non_numeric=K`；另加 `node_containers=<数>`（💭-4） | leak test 桩：account-b 全是 NULL、account-c 只有一次 NULL，两者都输出 `<uncomparable:NULL>`；account-a 仍是 `1.1` |

另：S-02 大纲新增 `order` 与全部前置指令（`order` 行只显示纯字母的指令名，含数字的词按字面长度隐藏——leak test 第一次跑就抓到了一个更宽的规则会漏出 `qctbhy0a32hucub`，已收紧）；`caddyfile-check` 的记录行只打印指令名、匹配器（路径经脱敏）和"其余 N 个参数已隐藏"。

### 10.3 本机实跑（未连 jp-24；打包门禁一律 `--report-only`，未带 `--execute`）

| 命令 | 结果 |
|---|---|
| `O0_WAL_REPRO_DB=<审查 walt/w.db> O0_CADDY_BIN=<Caddy v2.10.2> bash scripts/ops/o0/tests/run_all.sh` | 退出 0，末行 `ALL_O0_OFFLINE_CHECKS_OK real_caddy=ok`：`BASH_N_OK files=13`、`PY_COMPILE_OK`；o0_tool `SELFTEST_OK checks=104 … path_shapes=4+21 path_pins=12 legit_paths=32 … cp_isolation=19`；credentials `scenarios=13`；`CADDY_ARTIFACTS_OK lines=16 list_format=watcher-gateway-caddy-paths.v2 snippet_format=watcher-gateway-caddy-snippet.v1 … fallback_verbatim=yes`；Caddy `SELFTEST_OK good_passes=241 variants_caught=52/52 (raw and skeleton) benign_two_step=5 checks=45`；baseline `wal_case=ok repro_refused=ok`；`REDACTION_PARITY_OK corpus=83`；`LEAK_TEST_OK sections=19 sentinels=38 leaks=0`；`FLEET_GUARD_TEST_OK cases=7`；`AUTH_GATE_TEST_OK checks=18`；`APPLY_ROLLBACK_TEST_OK checks=138`；`CADDY_REAL_TEST OK checks=22 failures=0 caddy=v2.10.2`；`PLAN_MODE_OK scripts=5` |
| 同上但不设 `O0_CADDY_BIN` | 退出 0，`CADDY_REAL_TEST_SKIPPED (O0_CADDY_BIN unset)`，末行 `ALL_O0_OFFLINE_CHECKS_OK real_caddy=skipped`（跳过写在末行，不会被读成通过）。**wac-090 起**跳过时末行改为 `O0_OFFLINE_CHECKS_OK_WITHOUT_REAL_CADDY real_caddy=skipped`，不再以 `ALL_O0_OFFLINE_CHECKS_OK` 开头（审查 wac-088 💭-1），见 §11 |
| 13 个 shell 脚本逐个 `bash -n` | 13/13 |
| `bash scripts/ops/o0/o0_package.sh --candidate 3e5f1f8 --out <scratchpad> --report-only --run-tests` | **G1–G12 全部 PASS**：G2 `ROUTES_DIFF_EMPTY rows=63 … phase_max=P2`；G3 `META_OK … CADDY_ARTIFACTS_OK lines=16`；G4 `CLOSURE_OK closure=16 whitelist=36`；G5 `COMPOSE_OK env_file=True`；G7 PASS；G8 5 个文件；G9 pytest `30 passed`、watcher `tests 145 / pass 145 / fail 0 / skipped 0`；G10 17 个候选工具与 runbook 文件；G11 14 个工具文件；G12 `RUNTIME_MANIFEST_OK files=36`。末行 `PACKAGE_OK`，`RELEASE.json`：`deploy_candidate: true`、`failed_gates: 0`；bundle 里的片段与 `contracts/generated/` 逐字节相同 |
| G3 负例：在 clone 里把片段的兜底改回 `(?:/|$)` 并提交，再打包 | G2、G3 两个门禁都 FAIL（G3：`CADDY_ARTIFACTS_FAILED snippet differs … at line 119`），`PACKAGE_NOT_A_DEPLOY_CANDIDATE failed_gates=2` |

`deploy_candidate: true` 只说明本地门禁全绿，**不等于可以申请生产授权**：还需要 wac-060 审查 PASS 并合入、在合入后的集成分支提交上重跑打包门禁，以及授权清单 §四 列出的用户确认。

### 10.4 真实 Caddy v2.10.2（本机，`tests/caddy_real_test.sh`）

二进制：审查者在 scratchpad 从源码构建的 `wac058r/caddybin/caddy`（`caddy version` = `v2.10.2 h1:g/gTYjGMD0dec+UgMw8SnfmJ3I9+M2TdvoRL/Ovu6U8=`），不进仓库，测试通过 `O0_CADDY_BIN` 使用。夹具 `tests/fixtures/caddy/Caddyfile.prodlike.in` 是按 O-0 工具假定的 jp-bot 站点形态写的**仿生产**文件（不是生产文件），basic auth 哈希在测试时用一次性随机口令现算。

- 夹具：`caddyfile-check` 通过；真实 `caddy adapt` 的结果 verify 通过（`CADDY_WATCHER_ROUTES_OK mode=snippet lines=16 passes=241`）。
- 12 种违例写成真实 Caddyfile 并由真实 Caddy adapt，verify 全部失败：import 之前的 `handle /m/*`（遮蔽）、import 之后的 `handle /m/*`（前缀转发）、import 之后的顶层 `rewrite`、`uri … strip_suffix`、`method`、`request_header Authorization`、`basic_auth /m/*`、`redir /m/v1/watcher/*`、`forward_auth`、全局 `order reverse_proxy before handle` + 顶层 `reverse_proxy /m/*`、import 包进 `route { }`、包进 `handle_path /m*`。其中三种（handle 在前、`route`、`handle_path`）`caddyfile-check` 也拒绝；`order` 行出现在 F-13 (2) 记录里。
- 2 种合规写法通过：顶层 `vars` + `header -Server` + `map`（白名单）；不命中前缀的 `redir /old/*` 与 `rewrite /static/x`。
- 本机探针对夹具：`CADDY_PROBE_OK caddy=v2.10.2 live_checks=224 verify_passes=234`；逐行差异只含站点地址、上游与全局四项，不含哈希；探针运行后 `~/Library/Application Support/Caddy/autosave.json` 的 mtime 不变（状态目录在临时目录）。把 `handle /m/*` 放在 import 之前的副本：`CADDY_PROBE_FAILED`。

### 10.5 变异复跑（scratchpad `r060m/`，pristine 取本任务的提交；测试集 = 审查的集合 + `apply_rollback_test.sh`，wac-060 组另加 `caddy_real_test.sh`）

| 变异集 | 结果 |
|---|---|
| 审查 mut067（35 个） | 29 KILLED；3a、5b、5c 锚点被本轮改动移走，重定锚后 3/3 KILLED；4a、5d、5f SURVIVED（历次已判等价或纵深防御） |
| wac-059 新增（22 个） | 20 KILLED；R1d 重定锚后 KILLED；B1h SURVIVED（已判等价） |
| 审查 mut067b（9 个） | 8 KILLED；X7 SURVIVED（已判等价） |
| wac-072 `muts072`（29 个） | 25 KILLED；N3、N8、N9 锚点随 🟡-2 改动移走，重定锚后 3/3 KILLED；**I6（去掉 LoadState 检查）首轮存活**：新加的 `NeedDaemonReload` 规则把"单元不存在"的用例提前拦下了，于是把该用例改成 systemd 的真实输出（`LoadState=not-found` + `NeedDaemonReload=no`），复跑 KILLED |
| 审查 r073 `muts073`（17 个） | 13 KILLED，**r073 时存活的 D2、D3、D4（restore-db 恢复路径）与 D7、D11、D18（隔离门禁）全部转为被抓**；D12、D13、D14、D19 SURVIVED（审查已判等价或纵深防御） |
| 本任务 `muts060`（39 个：Caddy 工具 26、阶段 C 4、🟡 修复 9） | 首轮 25 KILLED、14 SURVIVED。存活的大多被模拟器或逐字节比对"顺带"抓住，单独那条检查其实没有被测到：补了 12 个只有该条检查能抓的隔离用例（方法只写 `CONNECT` 的匹配器让模拟请求不受影响、容器路由里先于 subroute 的 handler、带 body 或多一个 handler 的兜底、形状完整的清单外行、藏在兜底后面的前缀转发、同索引的嵌套拆分、清单与片段一致篡改的 regex、另一个块里多出的 import），第二、三轮复跑后 **36/39 KILLED**。剩下 3 个判为冗余：C10（兜底位置）——兜底若排在某条逐路径路由之前，该路由在模拟里必然不可达；C11（按前缀而不是逐字认兜底）——兜底探针要求应答路由的模式逐字等于兜底，片段也逐字节比对；C22（文本里的 `*`）——模板段规则、regex 推导、头部规则与片段比对已经覆盖 `*` 可能出现的每个位置 |

记录：`r060m/{mA1,mA2,mB,mC,mD,mN}/out.txt`（首轮，pristine `acc0151`）、`r060m/r2/out.txt`（第二轮，`02d364a`）、`r060m/r3/out.txt`（第三轮，`3e5f1f8`）。

### 10.6 剩余限制与未完成事项

1. **生产 Caddyfile 未见过**：夹具是按工具假定的形态写的。生产副本的 verify、`caddyfile-check`、本机探针要等 O0-A01（S-02/S-03 的脱敏大纲与骨架）和 O0-A05P（副本）之后才能做；在那之前，工具对真实生产结构的判断只经过骨架与仿生产夹具。
2. **F-13 (2) 人工记录**：工具只打印底稿（指令名、匹配器、隐藏的参数个数），"为什么不构成遮蔽"必须由执行者逐条写、用户审阅。
3. **Caddy 版本**：探针与真实测试用的是 v2.10.2；生产版本以 S-01 为准，不一致时要用同版本重跑 `caddy_real_test.sh` 与探针。
4. **模拟器的范围**：只评估 `host`、`path`、`path_regexp`、`method`、`not`、`protocol` 匹配器与 `rewrite` 的 strip 前后缀；其他匹配器与 handler 字段一律 `UNCOMPARABLE` 判失败（宁可误拦）。**更正（wac-090，审查 wac-088 🟡-1）**：wac-060 时"其他一律 UNCOMPARABLE"对 `path` 模式并不成立——`path` 里的 `?`、`[...]`、`\`、`{占位符}` 当时被当成字面字符，结果是"判不命中"而不是 UNCOMPARABLE。wac-090 起 `path` 按 Caddy v2.10.2 `MatchPath` 判定，占位符、`%`、写坏的 glob 在模拟里判 UNCOMPARABLE、在遮蔽第 1 步算命中，见 §11。`--before-deploy` 的前瞻遮蔽检查把"第一个带 `group` 的路由"当作 import 将来的位置，这是近似，阶段 C 的 verify 才是准的。
5. **awk 可移植性**：S-00 的新 awk 只用 POSIX 语法，本机只在 BSD awk 上跑过；jp-24 上的 mawk/gawk 未实测（本机没有）。
6. **脱敏残余**：纯字母令牌、短段、自身含 `/` 的令牌（M1/M2/M12）仍会原样出现，已写进授权清单 O0-A01。

## 11. wac-090：Caddy 核对收紧（O0-A05P 与 O0-A05 的前置；处置审查 wac-088 🟡-1…🟡-6、💭-1/2/4/5；未连 jp-24，未做只读生产核对）

依据：`reviews/wac-060.md`（wac-088 审查报告）§2.3、§4.2、§5、§7 与 §9；契约 §9.14.3、§9.16 F-10、F-12、F-13；Caddy v2.10.2 `modules/caddyhttp/matchers.go` `MatchPath.MatchWithError`；Go `path.Match`。

### 11.1 八项处置

| # | 项（任务书） | 处置 | 测试 |
|---|---|---|---|
| 1 | 🟡-1 `path` 匹配器按 Caddy glob 语义 | `_caddy_path_match` 改为照搬 Caddy v2.10.2 `MatchPath`：模式与路径都转小写；单独 `*`；恰好首尾各一个 `*` 走子串、只有一个 `*` 且在开头/结尾走后缀/前缀（这三种快速比较里其余字符**按字面**，与 Caddy 相同）；其余交给新写的 `go_path_match`（逐行照搬 Go `path.Match` 的 `scanChunk`/`matchChunk`/`getEsc`：`*` 不跨 `/`、`?` 一个非 `/` 字符、`[...]` 含 `^` 与区间、`\` 转义）。模式里有 `{占位符}`（按请求替换，不认识的替换成空串）、`%`（Caddy 在转义空间比较）或 glob 写坏时抛 `Uncomparable`：遮蔽第 1 步算命中（`_path_may_hit`），逐行模拟判 UNCOMPARABLE 失败。另外遮蔽第 1 步的探针在 F-13 的三个之外加上每条清单行的样例路径（原样与大写，`shadow_probes`），只命中某一条表内路径的非白名单 handler 也会进第 2 步 | **差分**：scratchpad `w090/gomatch/`（Go 程序直接调用 Caddy v2.10.2 的 `caddyhttp.MatchPath` 与 Go `path.Match`，go1.26.1）对 3 × 60011 组随机模式/路径与审查的定向形状比对：`path_match_diffs=0 caddy_matchpath_diffs=0`；工具判 UNCOMPARABLE 的约 1.5 万组（写坏的 glob）Caddy 一组也没有命中。selftest 钉住 12 组 MatchPath 结果、4 种 UNCOMPARABLE、4 个新违例（`?`、`[w]`、占位符、§2.3 的 `request_header` 注入）与 1 个只命中一条表内路径的隔离违例；良性两例（`/static/?ld`、单尾 `*` 里的字面 `?`）通过。真实 Caddy：`rewrite /m/v1/w?tcher/status`、`/m/v1/[w]atcher/dialogs`、`/m/v1/{http.request.uri.query.zz}watcher/status`、`/m/v1/%77atcher/status`（保守判失败）、`/M/V1/Watcher/status`、§2.3 的 `request_header /m/v1/w?tcher/trading/accounts Authorization "Bearer {env.SYSTEM_OBSERVER_TOKEN}"` 全部 verify 失败，§2.3 这份副本在探针里 verify 与活体检查**各自**失败 |
| 2 | 🟡-2 上游归一化 | 新增 `dial_endpoint`：去掉 `tcp/`、`tcp4/`、`tcp6/` 前缀，`localhost`、`*.localhost`、`127.0.0.0/8`、`::1`/`[::1]`、`::ffff:127.x`、空主机、`0.0.0.0`/`::` 都归为 loopback；占位符、unix socket 与其他网络、缺端口或端口范围抛 `Uncomparable`。前缀转发检查（原 `:777`）按**端口**识别 operator-query（8183，任何主机写法）与 watcher（9090/9100）；读不懂的 dial、`dynamic_upstreams`、没有静态上游的 `reverse_proxy` 判 UNCOMPARABLE 失败；"可能看到前缀"除了固定探针，还包括 `path` 模式为前缀而写（字面头以 `/m/v1/watcher` 开头，或带通配且字面头是前缀的非平凡开头），所以兜底后面的死配置 `handle /m/v1/watcher/extra` 也会被抓。负例"是否到达网关"、移动端样本、浏览器与 watcher 路由识别同样改用归一化比较 | selftest 钉住 10 种写法与 6 种 UNCOMPARABLE；变体：兜底之后的 `localhost:8183`、`[::1]:8183`、unix socket 的 `/m/*` 转发，以及只有这条规则能抓的隔离用例（兜底后面的死配置 `/m/v1/watcher/extra`，分别写成 `127.0.0.1:8183`、`localhost:8183`、unix socket、`dynamic_upstreams`）；良性：移动端 handle 写成 `localhost:8183` 通过。真实 Caddy：`handle /m/* { … reverse_proxy localhost:8183 }`、`[::1]:8183`、`unix//run/oq.sock`、死配置 `handle /m/v1/watcher/extra` 全部失败，移动端写 `localhost:8183` 通过 |
| 3 | 🟡-3 探针替换全部上游、只监听回环 | 文本副本不再替换上游，只改站点地址与全局 `admin off`、`persist_config off`、`auto_https off`、`default_bind 127.0.0.1`、`http_port`、`https_port`。adapt 后先用**生产上游地址**跑 verify；再由 `pin_probe_config` 生成真正运行的 JSON：只保留探针站点的 server、`listen` 固定为 `127.0.0.1:<端口>`、关闭自动 HTTPS、去掉 TLS 连接策略与访问日志配置；递归改写**每一个** `reverse_proxy` 上游（含 subroute、`handle_response`、错误路由、命名路由）：operator-query → 桩 oq、watcher → 桩 watcher、其他一切 → 桩 sink，删除主动健康检查与转发代理；丢掉其他 server、其他 app（tls、pki…）与 logging/storage；admin 关闭且不落盘。`assert_probe_pinned` 在启动前机器核对（顶层只有 admin/apps、只有 http app、恰好一个 server、只监听 `127.0.0.1:<端口>`、JSON 里每个 `dial` 都是三个桩之一、没有健康检查与转发代理），不满足就不启动 Caddy；`dynamic_upstreams` 无法改写，直接失败。Caddy 进程拿不到 `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY`/`FTP_PROXY` 与 `OTEL_*`。`not_forwarded` 改为"不得到达 oq 或 watcher 桩"（到 sink 可以），逐条打印 `PROBE_PIN dial <原地址> -> <桩>` 与 `stub_hits=oq:…,watcher:…,sink:…` | selftest：一份带 `0.0.0.0:8080` 第二 server、Tailscale 地址、`[::1]:8183`、unix socket、`localhost:9090`、主动健康检查、`network_proxy`、`handle_response` 嵌套上游、tls/logging app 的配置：原样核对报出问题，改写后 0 问题且映射逐一正确；admin 未关、`dynamic_upstreams` 都拒绝。真实 Caddy 金丝雀：副本的兜底 `handle` 与一条带 `health_interval 200ms` 的路由都指向一个本机"生产服务"，另有一个 `bind 0.0.0.0` 的第二站点也指向它，`@mobile` 写成 `localhost:8183`：探针 `CADDY_PROBE_OK`，金丝雀全程 **0 个请求**，兜底流量落到 sink（5 次），`localhost:8183` 被改到 oq 桩 |
| 4 | 🟡-4 失败清理与 sha 绑定 | `cmd_probe` 在写 `.o0probe` 之前就进入 `try`，`finally` 在所有路径上停 Caddy、关桩、删 `.o0probe`（0600 创建）与临时状态目录（`shutil.rmtree`；运行用的 JSON 也在里面）；`--keep` 只用于排错并提示自行删除。探针先打印 `PROBE_INPUT candidate_sha256=<> snippet_sha256=<>`，成功与失败的末行都带这两个值。阶段 C preflight 新增必填参数 `--probe-candidate-sha256`、`--probe-snippet-sha256`（execute 模式缺失或不是 64 位十六进制即拒绝）；新步骤核对 staging 候选与 bundle 片段的 sha 等于它们（`PROBE_BINDING_OK`，否则 `PROBE_BINDING_FAILED`，在第一次写 staging 之前）；门禁记录 `probe_candidate_sha256`、`probe_snippet_sha256`；apply 的 REQUIRE 用手上的候选与 bundle 片段再核对这两个字段。探针另在有 `/srv/trader-v3` 的主机上拒绝运行（💭-5） | 真实 Caddy：adapt 失败的副本 `CADDY_PROBE_FAILED adapt rc=1`，之后既没有 `.o0probe` 也没有临时目录；探针打印的两个 sha 等于文件实际 sha。沙箱 apply 新增 PB：门禁里的探针 sha 不是手上的候选 → 在门禁处拒绝，没有备份、写入或 restart。计划结构检查：preflight 的候选目录核对紧跟清门禁，探针绑定在第一次写 staging 之前，门禁写入两个字段，apply 的 REQUIRE 核对它们 |
| 5 | 🟡-5 八个测试缺口 | G10 大小写混写的 `CONNECT` 隔离用例 `/M/V1/Watcher/*`（另有 MatchPath 钉子）；G13 兜底之后的 `(?i)^/m/v1/watcher/extra$`（selftest 隔离用例 + 真实 Caddy）；G17 兜底之后、同时服务 `/healthz` 的正确浏览器路由里多列一个 `/m/v1/watcher/*`（只有 watcher 端口规则能抓）；G18 上游片段里的 basic auth；G24 用哨兵 HOME/XDG 跑探针，断言哨兵目录仍为空、每一次 Caddy 调用（version、adapt、run，经记录环境变量的包装器）都用临时状态目录、临时目录事后消失；G25 `--keep` 读 `.o0probe`：`admin off`、`persist_config off`、`default_bind 127.0.0.1`、`auto_https off`、权限 0600，且 `PROBE_LOCAL_ONLY … admin=off persist=off` 行存在；G26 只改**运行**配置的 Caddy 包装器给兜底 404 加 body，verify 通过、活体检查单独报 `fallback … body=1B`；G37 `--before-deploy` 的 `CONNECT` 隔离用例。包装器另外三种模式证明活体检查单独能抓：表内路径的 `Authorization` 被改写、被删除（证明每次转发默认带假 `Authorization` 有意义），`/m/v1/watcherx` 被转发到 oq | 变异复跑见 §11.3：8 个缺口变异全部 KILLED；G24 由"哨兵目录被写入"与"有一次 Caddy 调用没用临时目录"两条同时抓住 |
| 6 | 🟡-6 preflight 拒绝线上目录的候选 | `o0_deploy_caddy.sh` 在解析参数后、任何步骤之前：`--candidate` 必须在 `$O0_STAGE_DIR/` 之内、不含 `..` 或空格、staging 片段不等于线上片段路径、候选目录不等于线上 Caddyfile 目录，否则 `o0_die`；preflight 第 2 步再按 `pwd -P` 解析后的真实路径核对候选目录在 stage dir 内、不是线上 Caddy 目录、候选与 staging 片段都不是软链（`CANDIDATE_IN_STAGING`）。preflight 仍只写 staging | `auth_gate_test.sh` 新增 6 例：候选在沙箱 `/etc/caddy`、真实 `/etc/caddy`、stage dir 之外（apply 同样拒绝）、含 `..`、缺探针 sha、探针 sha 格式不对，全部在写任何文件之前拒绝（"refused runs created no file outside evidence/" 仍通过） |
| 7 | `run_all.sh` 末行 | 真实 Caddy 跑过且通过：`ALL_O0_OFFLINE_CHECKS_OK real_caddy=ok`；未设 `O0_CADDY_BIN`：`O0_OFFLINE_CHECKS_OK_WITHOUT_REAL_CADDY real_caddy=skipped`。仓库里依赖这个字符串的只有本文件 §10.3 的记录（已加注）与审查的变异脚本（它匹配的是 `… real_caddy=ok`，不受影响） | 见 §11.2 |
| 8 | runbook 与授权清单 | 部署 runbook §3.1 第 4 条加 U-6 审阅要点（`?`、`[`、`]`、`\`、`{`、`}`、`%`）；第 7 条重写：推荐方案 (b)（用户自己放置副本到本机 0700 目录，Agent 不连 jp-24、不接触哈希），(a) 需另行授权 SSH 读取；探针做什么、期望输出、完成后删除副本目录、证据只存本机（💭-4）、两个 sha 作为 C-1 必填参数；C-1/C-2 与故障对照同步。授权清单：O0-A05P 行、O0-A05 前置、§四 的 U-5、U-6、U-12 | `auth_gate_test.sh` 的 runbook 示例核对（文档里每一条带授权号参数的示例命令都与绑定表一致）通过 |

### 11.2 本机实跑（未连 jp-24；打包门禁 `--report-only`，未带 `--execute`）

代码提交 `eb7deee`（`auto/wac-090`，基于集成分支 `00405e3`）。二进制同 §10.4（审查者构建的 Caddy v2.10.2，经 `O0_CADDY_BIN` 使用，不进仓库）。

| 命令 | 结果 |
|---|---|
| `O0_CADDY_BIN=<v2.10.2> O0_WAL_REPRO_DB=<审查 walt/w.db> O0_FLEET_REPRO_DIR=<审查 fleet/evidence> bash -o pipefail scripts/ops/o0/tests/run_all.sh` | 退出 0，末行 `ALL_O0_OFFLINE_CHECKS_OK real_caddy=ok`：`BASH_N_OK files=13`、`PY_COMPILE_OK`；o0_tool `SELFTEST_OK checks=104`；credentials `scenarios=13`；`CADDY_ARTIFACTS_OK lines=16 … fallback_verbatim=yes`；Caddy 工具 `SELFTEST_OK good_passes=241 variants_caught=68/68 (raw and skeleton) benign_two_step=8 checks=86 … probe_pinning=ok matchpath_pins=ok`；baseline `wal_case=ok repro_refused=ok`；`REDACTION_PARITY_OK corpus=83`；`LEAK_TEST_OK sections=19 sentinels=38 leaks=0`；`FLEET_GUARD_TEST_OK cases=7`；`AUTH_GATE_TEST_OK checks=24`（原 18）；`APPLY_ROLLBACK_TEST_OK checks=139`（原 138，另有计划结构的探针绑定检查）；`CADDY_REAL_TEST OK checks=49 failures=0 caddy=v2.10.2`（原 22）；`PLAN_MODE_OK scripts=5` |
| 同上但不设 `O0_CADDY_BIN` | 退出 0，`CADDY_REAL_TEST_SKIPPED (O0_CADDY_BIN unset)`，末行 `O0_OFFLINE_CHECKS_OK_WITHOUT_REAL_CADDY real_caddy=skipped` |
| 13 个 shell 脚本逐个 `bash -n` | 13/13 |
| 真实 Caddy 探针（夹具） | `CADDY_PROBE_OK caddy=v2.10.2 live_checks=260 verify_passes=241 lines=16 … stub_hits=oq:37,watcher:0,sink:0 candidate_sha256=… snippet_sha256=…`；金丝雀副本 `sink:5`、金丝雀 0 个请求 |
| 本机 `~/Library/Application Support/Caddy/` | 最终两次 `run_all.sh` 前后逐文件 `stat` 修改时间与 sha256 完全相同（`USER_CADDY_DIR_UNCHANGED`） |
| `bash scripts/ops/o0/o0_package.sh --candidate HEAD(eb7deee) --out <scratchpad> --report-only --run-tests`（未带 `--execute`） | **G1–G12 全部 PASS**：G2 `ROUTES_DIFF_EMPTY rows=63 … phase_max=P2`；G3 `META_OK`；G4 `CLOSURE_OK closure=16 whitelist=36`；G5 `COMPOSE_OK env_file=True`；G8 5 个文件；G9 pytest `35 passed`、watcher `tests 155 / pass 155 / fail 0 / skipped 0`；G10 17 个候选工具与 runbook 文件无禁用动词；G11 14 个工具文件；G12 `RUNTIME_MANIFEST_OK files=36`。`PACKAGE_OK`，`RELEASE.json`：`candidate eb7deee…`、`deploy_candidate: true`、`failed_gates: 0`、`phase_max P2` |

证据文件（scratchpad `w090/`）：`runall-caddy-final.log`、`runall-nocaddy-final.log`、`caddyreal.log`、`pkg.log`、`pkg/`、`gomatch/`（差分程序与脚本）、`mut/`（`muts090.py`、`one.sh`、`results.txt`、`results-r2.txt`、`w/<变异>/out.txt`）、`caddyhome-final-{before,after}.txt`。

### 11.3 变异复跑（scratchpad `w090/mut/`，pristine = 本任务代码，测试集 = 带真实 Caddy 的完整 `run_all.sh`）

| 变异集 | 结果 |
|---|---|
| 审查 r088 `muts088` 的 40 个（代码移动的 G17、G24、G30、G37 重定锚） | **32 KILLED**。§4.2 的 8 个测试缺口 G10、G13、G17、G18、G24、G25、G26、G37 全部转为被抓。存活 8 个：G9、G14、G15、G21、G30、G38、G39 与审查的判定相同（等价或冗余）；**G29**（preflight 的线上片段比对改成恒真）在审查记录里是 KILLED（`AUTH_GATE_TEST_FAILED failures=1`），但在审查自己的 pristine `58e0ad9` 上单独复跑 `auth_gate_test.sh` 两次都是 `AUTH_GATE_TEST_OK checks=18`，那次击杀是并行运行的偶发失败；G29 与 G30 同类：preflight 从未在沙箱里执行，apply 在任何写入之前用 `cmp` 再比一次线上片段（SN3 与 G31 覆盖），判为纵深防御 |
| 本任务 `muts090` 的 35 个（glob 与占位符 8、遮蔽探针 1、前缀转发与归一化 9、探针改写与核对 9、清理与环境 4、阶段 C 绑定与候选目录 4） | 首轮 30 KILLED；N09（遮蔽探针不加清单路径）、N10（按字面上游识别 operator-query）、N11（读不懂的 dial 放行）存活——已有用例都被负例模拟"顺带"抓住。补了 3 个只有该条规则能抓的隔离用例（只命中一条表内路径的 `CONNECT` rewrite、兜底后面写成 `localhost:8183` 与 unix socket 的死转发）后复跑，3/3 KILLED，合计 **33/35**。剩下 2 个判为等价：N17（负例遇到读不懂的 dial 不算到达网关）——凡是负例能到达的转发路由都会被前缀转发检查以 UNCOMPARABLE 拦下；N29（探针把代理环境变量传给 Caddy）——所有上游都已改成回环地址的桩，而 Go 的 `ProxyFromEnvironment` 对回环地址从不走代理，行为上不可区分，保留为纵深防御 |

副作用记录：G24 变异按设计去掉了探针的状态目录隔离，它在跑 `caddy_real_test.sh` 时刷新了本机 `~/Library/Application Support/Caddy/last_clean.json` 的时间戳与 `locks/` 目录的修改时间（内容只是清理时间戳，不含配置或哈希）；`autosave.json` 的 sha 在本任务全部运行前后不变（`persist_config off` 在变异下仍然生效）。未变异的工具不碰该目录（§11.2 的最终运行前后核对）。

### 11.4 剩余限制

1. **`%` 模式保守判命中**：Caddy 在转义空间比较含 `%` 的 `path` 模式，工具没有照搬这段算法，一律算命中/UNCOMPARABLE。审查 §2.3 的 `%77` 例子以前两边都判不命中，现在 verify 会拒绝它（宁可误拦）。生产若真有这类匹配器，需要改写或由 Architect 裁定。
2. **转发检查更严**：可能看到前缀的路由里，上游写成运行时占位符、unix socket 或端口范围都会 UNCOMPARABLE 失败；兜底后面为前缀而写的死路由只要转发到 8183/9090/9100 也失败。生产副本若因此失败，按 runbook 故障对照改候选，不放宽检查。
3. **探针运行的是裁剪后的配置**：只运行探针站点的 server；其他站点、tls/pki/logging app 不运行。它们对 `/m/v1/watcher` 路由的影响由 verify 在未裁剪的 adapt 结果上判定（与 C-1 相同）；活体检查只证明探针站点本身。
4. **C-1 的 sha 比对步骤只在计划输出里核对过结构**：preflight 没有沙箱执行路径；真正挡住写入的是 apply 的门禁字段核对（沙箱 PB 用例已测）。
5. 其余同 §10.6（生产 Caddyfile 未见过、F-13 (2) 人工记录、Caddy 版本、awk 可移植性、脱敏残余）。

## 12. wac-092：Caddy 核对第二轮收紧（O0-A05P 与 O0-A05 的前置；处置审查 wac-090（报告 `reviews/wac-090.md`，审查任务 wac-091）🟡-1…🟡-5 与 💭-1、💭-2；未连 jp-24，未做只读生产核对）

依据：`reviews/wac-090.md` §2、§3.3、§4、§5、§7；契约 §9.14.3、§9.16 F-10、F-12、F-13；Caddy v2.10.2 `modules/caddyhttp/matchers.go` 的 `MatchPath.MatchWithError`（`mergeSlashes := !strings.Contains(matchPattern, "//")`）、`MatchPathRE.MatchWithError`（先 `cleanPath`）、`MatchHost.Provision`/`MatchWithError`（IDNA、按标签的 `*`、占位符替换）；Go `strings.ToLower`（简单大小写映射）。

### 12.1 六项处置

| # | 项（任务书） | 处置 | 测试（selftest / 真实 Caddy） |
|---|---|---|---|
| 1 | 🟡-1 非 ASCII 路径模式 | 选"保守处理"，不复刻 Go 的 `strings.ToLower`：Python `str.lower` 用完整映射（`İ` → `i̇` 两个码点）并有词尾 sigma 规则，Go 用简单映射（`İ` → `i`、开尔文符号 → `k`），两边的 Unicode 版本也不同，逐码点复刻仍可能漏。`_caddy_path_match` 遇到模式里**任何**非 ASCII 字符即抛 `Uncomparable`：遮蔽第 1 步算命中（`_path_may_hit`），逐行模拟判 UNCOMPARABLE 失败。💭-2 一并处理：请求路径含非 ASCII 同样 UNCOMPARABLE。`_literal_head` 在非 ASCII 字符处截断，"为前缀而写"的判定不因它漏掉 | selftest：审查者的 12 个危险用例（逐字照抄，含重复）加开尔文符号、`ſ`、路径侧非 ASCII，全部 UNCOMPARABLE 且算命中；变体 `request_header /m/v1/watcher/tradİng/accounts` 注入（n07）与只有这条规则能抓的隔离用例（`CONNECT` + `risKs`）在原始 JSON 与骨架上都判失败。真实 Caddy：`request_header …tradİng…`、`rewrite …risKs` verify 失败；n07 作为探针副本，verify 与活体检查**各自**失败。审查的黑盒差分（`r091/globdiff/diff.py`，3 个种子 × 1500 模式，69,750 组，oracle 是运行中的 Caddy v2.10.2）在本分支复跑：**工具判不命中而 Caddy 命中：0**（原 3/6/3），工具判命中而 Caddy 不命中：0 |
| 2 | 🟡-2 含 `//` 的路径模式 | `_caddy_path_match` 遇到含 `//` 的模式（`*` 单独除外）即抛 `Uncomparable`（算命中 / 模拟 UNCOMPARABLE）：Caddy 对这种模式不合并请求路径的重复斜杠，而网关行的 `path_regexp` 先 clean，所以 `/m//v1/watcher/<行>` 能到达网关却不等于任何干净探针。探针活体检查新增：每条清单行在每个斜杠处各多加一个 `/`（`//m/…`、`/m//v1/…`、`/m/v1//watcher/…`、`/m/v1/watcher//…` 等，`double_slash_variants`），期望 clean 后转发、调用方 `Authorization` 原样到达；另发一次不带 `Authorization` 的 `/m//v1/watcher/trading/accounts`。真实 Caddy 实测这些变体全部被 clean 并转发（仿生产夹具 `live_checks=420`，原 260） | selftest：4 种 `//` 模式 UNCOMPARABLE 且算命中；`double_slash_variants` 钉子；变体 n08 注入、隔离的 `CONNECT` + `/m/v1//watcher/status`、兜底后面的 `//` 转发都失败。真实 Caddy：`request_header /m//v1/…`（n08）、`rewrite /m/v1//watcher/status`（n19）、`handle /m//v1/watcher/*` 转发 8183 都 verify 失败；n08 探针副本 verify 与活体检查各自失败（`FAIL forward GET '/m//v1/watcher/trading/accounts' … Authorization changed`）；包装器只在**运行**配置里插一条 `//` 注入（verify 通过），活体检查单独抓到 |
| 3 | 🟡-3 host 匹配器 | 两种办法都做，理由见下。(A) verify：`_host_match` 按 Caddy `MatchHost` 重写（按标签数逐段比较，恰好是 `*` 的标签匹配任意一段，其余标签按字面、不区分大小写，`jp-*` 只匹配字面 `jp-*`；`*.wang` 不再误配 `jp-bot.balen.wang`）；占位符与非 ASCII 主机名 UNCOMPARABLE（第 1 步算命中）。**遮蔽检查与前缀转发检查里，`host` 只在 server 顶层（Caddyfile 的选站点那一层）比较；站点里面的 `host` 匹配器从不排除**（`_host_may_exclude`），于是站点内任何带 `host`（与本来就不排除的 `header`、`remote_ip`、`client_ip`、`expression`、`not` 一样）的、会改写或终止请求的路由都要过 F-13 白名单，否则失败。(B) 探针：副本的站点头改成 `http://<生产主机名>:<端口>`（默认 `--site-address`，可用 `--host`；只允许普通主机名），`default_bind 127.0.0.1` 仍把监听限定在回环（`assert_probe_pinned` 照旧核对），每个请求带 `Host: <生产主机名>`，探针里的 verify 也用生产主机名。理由：(A) 是 C-1 在生产 adapt 结果上的门禁，必须不依赖探针；只按 `--host` 精确比较仍会漏掉"站点应答多个名字、Host 由调用方决定"的情形（任何人用别名访问就会被注入 observer token），所以站点内一律按可能命中。(B) 让活体检查对生产主机名复现 host 路由，给 (A) 一个独立的第二道 | selftest：9 个 `MatchHost` 钉子、3 种 UNCOMPARABLE、"顶层比较 / 站点内不排除"两条钉子；变体 n17b（`jp-bot.*.wang` 注入）、n18（`{http.request.host}` 注入）、隔离用例"别名 host + `CONNECT` rewrite"（只有站点内不排除这条规则能抓）、"兜底后面别名 host 的 `/m/*` → 8183"（只有转发检查的 host 规则能抓）；良性"另一个站点在前"仍通过；探针副本主机名钉子与非法 `--host` 拒绝。真实 Caddy：`@h host jp-bot.*.wang`、`@h host {http.request.host}` 注入与 `@alias host alias.balen.wang` + `redir`（保守判失败）verify 失败；n17b 探针副本 verify 与活体检查各自失败；包装器只在运行配置里插一条 `host jp-bot.*.wang` 注入，活体检查单独抓到（证明 Host 头生效）；`.o0probe` 里是 `http://jp-bot.balen.wang:<端口>`，`PROBE_LOCAL_ONLY … host=jp-bot.balen.wang` |
| 4 | 🟡-4 探针在信号下的清理 | 新增 `_ProbeRun`：`.o0probe` 路径、临时目录、子进程、桩都先登记再创建（登记与创建之间收到的信号先挂起，出了临界区再处理）。SIGINT/SIGTERM/SIGHUP 第一次到达时抛 `_ProbeInterrupted`（`BaseException`，不会被 `except OSError` 之类吞掉），`finally` 的**第一条语句**置 `cleaning`，再 `cleanup()`：先删 `.o0probe`，再对 Caddy 进程组发 SIGTERM（10 秒后 SIGKILL），再删临时目录，最后关桩（桩的 `poll_interval` 改为 50 ms）。`cleaning` 置位后再来任何信号走 `emergency()`：删 `.o0probe`、SIGKILL 所有子进程组、最多等 2 秒、删临时目录、`os.write` 一行 `CADDY_PROBE_INTERRUPTED … during cleanup`、以 128+信号号退出。`caddy version`/`adapt`/`run` 都在新进程组里启动（包装脚本与它启动的 Caddy 一起停）。被打断时末行 `CADDY_PROBE_INTERRUPTED signal=<名>: …`，退出码 128+n。文档改为如实描述（SIGKILL 与断电不在覆盖范围，runbook 保留 `ls -A`）。💭-1：探针给 Caddy 设 `OTEL_SDK_DISABLED=true` | selftest（假 `caddy`，不需要 `O0_CADDY_BIN`）：探针作为独立进程跑 5 个场景——检查失败正常结束（R06）、`caddy adapt` 挂住时 SIGTERM、活体检查中 SIGINT、清理等待 Caddy 停止时 SIGTERM、活体检查中 SIGHUP 后在清理中再 SIGINT；每个都断言退出码与末行、没有 `.o0probe`、`TMPDIR` 为空、子进程都已不在、输出无 `$2a$`。真实 Caddy：包装器在 `adapt` 挂住时 SIGTERM、`caddy run` 启动期间 SIGINT、清理等待 Caddy 停止时 SIGTERM、先 SIGHUP 再 SIGINT，四种都不留 `.o0probe`、临时目录、包装器与 Caddy 进程 |
| 5 | 🟡-5 测试缺口 | **preflight 沙箱执行**（`apply_rollback_test.sh` PF1–PF9）：在 `O0_SANDBOX` 里真正执行 `--phase preflight`（桩化 `id`/`docker`/`caddy`/`install`）。PF1 全部标记（`CANDIDATE_IN_STAGING`、`FLEET_BASELINE_OK`、`PROBE_BINDING_OK`、`LIVE_SNIPPET_ABSENT`、`SNIPPET_STAGED`、`CADDYFILE_CHECK_OK`、`CADDY_WATCHER_ROUTES_OK`、`GATE_WRITTEN`），门禁的两个探针 sha 等于输入，线上目录逐字节不变，**随后 apply 接受这份门禁并成功**；PF2（R13）片段 sha 不对、PF3 候选 sha 不对、PF4（R16）候选是软链、PF5（R16）staging 片段是指向 `/etc/caddy` 的悬空软链、PF6（G29）线上已有**不同**的片段、PF8 `$S/caddy` 是指向线上目录的软链（`pwd -P` 那一层）、PF9 线上片段路径是悬空软链：全部在写 staging 片段之前拒绝，无门禁、线上目录不变、没有 validate/adapt/restart；PF7 线上片段相同：通过。带 `O0_CADDY_BIN` 时另有 PF-REAL：真实 Caddy 对仿生产候选做 validate + adapt，门禁写出；n08 候选被 verify 拒绝、无门禁；两次输出都没有 `$2a$`。preflight 的线上片段步骤改为失败时输出 `LIVE_SNIPPET_DIFFERS`（原来静默失败），悬空软链不再算"不存在"；apply 的片段门禁同样拒绝悬空软链（新增 SN4）。**R05**：`assert_probe_pinned` 对 `0.0.0.0:443`、`:443`、多监听、`[::]:443` 都报 listen。**R06**：见第 4 项，另有真实 Caddy 的 6 个包装器模式与 3 个探针副本在失败后断言没有 `.o0probe` 与临时目录。**R10**：selftest 设 `HTTP_PROXY`/`https_proxy`/`ALL_PROXY`/`no_proxy`/`OTEL_*` 调用 `_probe_env`；真实 Caddy 设这些变量跑探针，包装器记录每次 Caddy 调用拿到的变量名：`proxy=[] otel=[OTEL_SDK_DISABLED=true,]`。**R12**：selftest 打桩 `Path.exists` 让 `/srv/trader-v3` 存在，`cmd_probe` 输出 `CADDY_PROBE_REFUSED`、返回 1、不写 `.o0probe` | 见 §12.3 变异 |
| 6 | runbook 与授权清单 | 部署 runbook §3.1 第 4 条 U-6 审阅要点加非 ASCII、含 `//` 的路径、`host` 里的 `*` 或 `{`，并说明站点内 `host` 匹配器一律按可能命中；"18 万组 0 不一致"更正为"只覆盖 ASCII 字母表"，并记下审查的 69,750 组黑盒差分与本分支复跑 0 处危险分歧。第 7 条：O0-A05P 的时机（wac-092 合入之后）、探针保留生产主机名与 `--host`、`//` 变体、清理的如实描述（信号覆盖，SIGKILL/断电不覆盖，`ls -A` 保留）、`CADDY_PROBE_INTERRUPTED` 不算通过、verify 与活体 FAIL 分开计数。C-1 行与故障对照加 `LIVE_SNIPPET_DIFFERS`、`CADDY_PROBE_INTERRUPTED`。授权清单：页首 wac-092 说明、O0-A05P 行（探针说明、清理措辞、前置改为 wac-092 已合入、`CADDY_PROBE_INTERRUPTED` 不算）、O0-A05 前置、§四 U-5 时机与 U-6 三类要点、站点内 `host` 路由会被判失败的告知 | `auth_gate_test.sh` 的 runbook 示例核对仍通过（`AUTH_GATE_TEST_OK checks=24`）；G10 禁用动词扫描见 §12.2 |

探针输出另一处改动：verify 与活体检查的 FAIL 行分开各打印最多 40 条（原来合计 40 条，UNCOMPARABLE 匹配器让 verify 对每个模拟请求都失败，会把活体失败挤掉），末行加 `verify_failures=`、`live_failures=`。

### 12.2 本机实跑（未连 jp-24；打包门禁 `--report-only`，未带 `--execute`）

代码提交 `c21259a`（`auto/wac-092`，基于集成分支 `b7c688c`，含 wac-090 合并 `dbbc29e`）。Caddy 二进制同 §10.4（审查者构建的 v2.10.2，经 `O0_CADDY_BIN` 使用，不进仓库）。

| 命令 | 结果 |
|---|---|
| `O0_CADDY_BIN=<v2.10.2> O0_WAL_REPRO_DB=<审查 walt/w.db> O0_FLEET_REPRO_DIR=<审查 fleet/evidence> bash -o pipefail scripts/ops/o0/tests/run_all.sh` | 退出 0，末行 `ALL_O0_OFFLINE_CHECKS_OK real_caddy=ok`：`BASH_N_OK files=13`、`PY_COMPILE_OK`；o0_tool `SELFTEST_OK checks=104`；credentials `scenarios=13`；`CADDY_ARTIFACTS_OK lines=16`；Caddy 工具 `SELFTEST_OK good_passes=241 variants_caught=77/77 (raw and skeleton) benign_two_step=8 checks=130 … non_ascii=uncomparable double_slash=uncomparable host_matchpins=ok probe_cleanup_signals=ok`（原 68/68、86）；`REDACTION_PARITY_OK corpus=83`；`LEAK_TEST_OK sections=19 sentinels=38 leaks=0`；`FLEET_GUARD_TEST_OK cases=7`；`AUTH_GATE_TEST_OK checks=24`；`APPLY_ROLLBACK_TEST_OK checks=152`（原 139）；`CADDY_REAL_TEST OK checks=68 failures=0 caddy=v2.10.2`（原 49）；`PLAN_MODE_OK scripts=5` |
| 同上但不设 `O0_CADDY_BIN` | 退出 0，`CADDY_REAL_TEST_SKIPPED`、`APPLY_ROLLBACK_TEST_OK checks=150`（PF-REAL 跳过），末行 `O0_OFFLINE_CHECKS_OK_WITHOUT_REAL_CADDY real_caddy=skipped` |
| 13 个 shell 脚本逐个 `bash -n` | 13/13 |
| 真实 Caddy 探针（仿生产夹具） | `CADDY_PROBE_OK caddy=v2.10.2 live_checks=420 verify_passes=241 lines=16 host=jp-bot.balen.wang … stub_hits=oq:117,watcher:0,sink:0`（原 `live_checks=260`、`oq:37`；多出的是 `//` 变体） |
| 审查 r091 的 68 个真实 Caddy 变体（`cv/gen.py`，复制到本任务目录重跑，未改审查证据） | n07、n08、n15、n17、n18、n19 由 verify 通过变为失败（wac-094 更正，审查 wac-093 💭-1：原文把 n15b 也列进来，不准确；r091 的 run1、run3 里 n15b 已经是 FAIL）；其余与审查记录相同（包括审查已说明预期写错或由 `caddyfile-check` 抓住的 6 个），期望通过的 6 个仍通过 |
| 审查 r091 的黑盒 glob 差分（`globdiff/diff.py`，种子 1–3 × 1500 模式，69,750 组） | 三个种子都是 `DANGEROUS tool=F caddy=HIT: 0`、`CONSERVATIVE tool=T caddy=MISS: 0`（审查时危险分歧 3/6/3） |
| 本机 `~/Library/Application Support/Caddy/` | 本任务开工前快照（逐文件 `stat` 修改时间、大小、权限与 sha256，与审查 `caddyhome-final.txt` 一致）；之后经历全部 `run_all`、真实 Caddy 测试、探针、差分、49 次完整运行（变异 48、基线 1）与打包门禁，最终快照与开工前逐文件相同（`USER_CADDY_DIR_UNCHANGED`）。只做了检查，没有删除任何东西 |
| `bash scripts/ops/o0/o0_package.sh --candidate c21259a --out <scratchpad> --report-only --run-tests`（未带 `--execute`） | **G1–G12 全部 PASS**：G2 `ROUTES_DIFF_EMPTY rows=63 … phase_max=P2`；G3 `META_OK`；G4 `CLOSURE_OK closure=16 whitelist=36`；G5 `COMPOSE_OK env_file=True`；G8 5 个文件；G9 pytest `35 passed`、watcher `tests 155 / pass 155 / fail 0 / skipped 0`；G10 17 个候选工具与 runbook 文件无禁用动词；G11 14 个工具文件；G12 `RUNTIME_MANIFEST_OK files=36`。`PACKAGE_OK`，`RELEASE.json`：`candidate c21259a…`、`deploy_candidate: true`、`failed_gates: 0`、`phase_max P2` |
| 残留进程 | 全部运行之后没有残留的 Caddy、包装器或探针进程 |

证据文件（scratchpad `w092/`）：`runall-caddy-final.log`、`runall-nocaddy-final.log`、`caddyreal-2.log`、`applyrb-2.log`、`pkg.log`、`pkg/`、`cv/run-final.txt`、`globdiff/final-{1,2,3}.txt`、`mut/`（`muts092.py`、`runmut.sh`、`results.txt`、`results-r2.txt`）、`caddyhome-{before,final-before,final-after}.stat`。

### 12.3 变异复跑（scratchpad `w092/mut/`，pristine = 本任务代码，测试集 = 带真实 Caddy 的完整 `run_all.sh`，串行）

| 变异集 | 结果 |
|---|---|
| 基线（未变异的 pristine） | 全绿 |
| 审查 r091 `muts091` 的 16 个（R06 因清理代码改写重定锚为"cleanup 不删 `.o0probe`"）+ r088 的 G29（重定锚为"线上片段比对恒真"） | **17/17 KILLED**。审查时存活的 R05、R06、R10、R12、R13、R16 与 G29 全部转为被抓：R05 由 `assert_probe_pinned` 的 4 个 listen 负例，R06 由 selftest 假 Caddy 场景与真实 Caddy 的失败后断言，R10 由 `_probe_env` 单测，R12 由打桩 `Path.exists`，R13/R16/G29 由 preflight 沙箱 PF2、PF4/PF5、PF6 |
| 本任务 N01–N25（非 ASCII 3、`//` 3、host 8、清理与信号 8、输出与门禁 3） | 首轮 21/25 KILLED。存活：N05（去掉每行 `//` 变体）与 N06（去掉不带 `Authorization` 的 `//` 请求）互相掩护——包装器注入的都是同一条路径；把包装器的 `//` 注入改到 `/m/v1//watcher/status`（只有每行变体会发），并新增"只在调用方不带 `Authorization` 时注入"的模式后复跑，两个都 KILLED。N16（紧急路径不删 `.o0probe`）：`cleanup()` 第一步已删掉，第二个信号落进来时文件早已不在；新增对 `emergency()` 的独立子进程单测后复跑，N16 与 N17 都 KILLED。**N01–N25 合计 24/25 KILLED**，只剩 N22（`finally` 第一条语句的 `cleaning = True`；`cleanup()` 自己第一句也会置位，只差进入 `finally` 到调用之间的几条字节码），判为纵深防御。另写了更严的 N20b（临界区的信号挂起整个去掉；首轮 N20 是因计数失衡被抓，不能说明挂起本身被测到）：**存活**，信号恰好落在"创建与登记之间"的时序测试无法稳定复现，见 §12.4 第 3 条。全部变异合计 41/43 KILLED，2 个存活均已说明 |

### 12.4 剩余限制

1. **更保守 = 可能误拦生产写法**：站点内任何带 `host` 的、会改写或终止请求的路由（例如 `@alias host 别名` + `redir`）、任何含非 ASCII 或 `//` 的 `path` 匹配器，在 C-1 都会失败。这是有意的：若生产副本因此失败，把别名移到单独的站点块或改写匹配器，不放宽检查；需要例外时由 Architect 裁定。
2. **探针只复现一个主机名**：活体检查用生产主机名（默认 `jp-bot.balen.wang`）；站点应答的其他名字、以及 `header`、`remote_ip`、`client_ip`、`expression` 等依赖请求来源或头的条件，探针复现不了，只由 verify 的保守规则把关（站点内这些条件都不排除命中）。
3. **信号清理不覆盖 SIGKILL 与断电**：探针进程被 `kill -9` 或机器断电时 `.o0probe`（0600）可能残留；runbook 保留 `ls -A` 确认。（wac-094 更正，审查 wac-093 🟡-2：这里只写了一半。实际还会留下含哈希的临时目录与一个仍在运行的 Caddy，处置见 §13。）临界区的"先登记后创建"只能用测试间接覆盖（见 §12.3 N20），信号恰好落在临界区里的时序无法稳定复现。
4. **骨架里被脱敏的路径段**：L-A1 的 `--before-deploy` 核对跑在骨架上，令牌形状的路径段被换成 `<seg len=N>` 后按字面比较；C-1 在未脱敏的 adapt 结果上判定，不受影响（沿用 §10.6 的骨架说明）。
5. 其余同 §11.4 第 1–3 条与 §10.6（`%` 保守判命中、转发检查更严、探针运行裁剪后的配置、生产 Caddyfile 未见过、F-13 (2) 人工记录、Caddy 版本、awk 可移植性、脱敏残余）。§11.4 第 4 条（preflight 没有沙箱执行路径）已由本节第 5 项解决。

## 13. wac-094：Caddy 核对第三轮收紧（O0-A05P 与 O0-A05 的前置；处置审查 wac-092（报告 `reviews/wac-092.md`，审查任务 wac-093）🟡-1、🟡-2 与 💭-1…💭-3；未连 jp-24，未做只读生产核对）

依据：`reviews/wac-092.md` §3.2、§4、§7、§8、§9；契约 §9.14.3、§9.16 F-10、F-12、F-13（F-13 第 1 步写的是三个探针；工具从 wac-090 起比契约更严：样本路径，现在是整个前缀空间。契约第 1 步"遇到无法评估的一律视为命中"的精神不变，本轮不改契约）。Caddy v2.10.2 `MatchPath` 的快速前缀、后缀、子串比较与 Go `path.Match`；`MatchPathRE` 的 RE2（区分大小写、非多行）。

### 13.1 三项处置

| # | 项（任务书） | 处置 | 测试（selftest / 真实 Caddy） |
|---|---|---|---|
| 1 | 🟡-1 遮蔽第 1 步按样本判定 | **verify**：遮蔽检查里，一条路由若没有打中任何样本路径，再按**整个 `/m/v1/watcher` 前缀空间**判定（`_mset_may_hit_space`）。前缀空间 = `/m/v1/watcher` 本身、`/m/v1/watcher/<任意>`、`/m/v1/watcher\n<任意>`，任意大小写。`path`（`_path_pattern_may_hit_space`，按 Caddy 的结构分支）：`_caddy_path_match` 判不了的（非 ASCII、占位符、`//`、`%`、写坏的 glob）算命中；`*`、`*x*`（子串）、`*x`（后缀）一律命中；`x*`（快速前缀，`x` 按字面）在 `x` 可能是某个空间路径的开头时命中；不含 glob 字符的模式是精确路径，落在空间里就命中（`…/accounts/account-a`）；其余 glob 看字面头（到第一个 glob 字符为止），字面头为空（`?x`、`[x]…`）或可能是空间路径的开头时命中（`…/accounts/a*`、`…/channels/[0-9]*`）。`path_regexp`（`_regexp_may_hit_space`）：只有以 `^` 开头、没有 `(?` 标志、顶层没有 `|`、且 `^` 之后的字面前缀（到第一个正则元字符为止；`\` 加标点算那个字符；后面跟量词时去掉最后一个字符）按不区分大小写比较**不可能**是空间路径开头时才不命中；`account-a$`、`%2[fF]`、`^.*watcher`、`^/api|watcher`、`(?i)^/M/` 都命中。`host` 与其他匹配器的处理不变。前缀转发检查的"为前缀而写"也并入同一规则（死在兜底后面的 `@png path *.png` → 8183 判失败）。**探针**：每条带参数的清单行再用 35 个参数值（`PROBE_PARAM_VALUES`：`account-a`…`account-d`、`ACCOUNT-A`、`BTCUSDT`、`btcusdt`、`1000PEPEUSDT`、`-1001234567890`、200 字符的长值、带点的值、`x.png`/`x.PNG`/`x.jpg`/`x.JPG`/`x.jpeg`/`x.gif`/`x.webp`/`x.mp4` 等）按该行的每个方法各发一次，期望原样转发、`Authorization` 原样到达（仿生产夹具 `live_checks` 420 → 840，`oq` 117 → 327）。误报面：`@static path *.js *.css` 只配 `header`（响应头）仍通过；`^/api/(v1|v2)/old$`、`/m/v1/watcherx*`、`/static/?ld` 不命中 | selftest：22 个"必须命中"与 14 个"不可能命中"的 `path` 钉子、15 个与 7 个 `path_regexp` 钉子、`method`/`host` 组合钉子；变体 v14–v20 与"兜底后面的 `*.png` 转发"8 个，原始 JSON 与骨架上都判失败（`variants_caught=85/85`，原 77/77）；良性 3 个（静态后缀只改响应头、锚定在别处且组内有 `|` 的正则、`/m/v1/watcherx*` 上的 redir）通过（`benign_two_step=11`）。真实 Caddy（`caddy_real_test.sh`）：v14–v20 与 `@png` 转发写成真实 Caddyfile，verify 全部失败；两个良性写法 verify 通过；v15–v19 作为探针副本，verify 与活体检查**各自**失败（例如 `FAIL forward GET '/m/v1/watcher/media/x.png' … Authorization changed`）；包装器只在运行配置里对 `media/*.png` 注入（verify 通过），活体检查单独抓到。审查 r093 的 92 个变体全部重跑：只有 v14–v20 由 verify 通过变为失败，其余 85 个 verify 结论与审查记录完全相同（活体失败数因检查增多而变大）；v15–v19 的活体失败分别是 4、8、6、2、8；v14（`%252F`）、v20（字面 `[0-9]` 开头的 channel id）活体复现不了，由 verify 兜住。另写了前缀空间的**黑盒可靠性差分**（`w094/spacediff.py`，oracle 是运行中的 Caddy v2.10.2，模式生成沿用审查 r091 的 glob 变异器，另加正则形状）：种子 1、2 各 1200 个 `path` 模式 + 400 个 `path_regexp`，共 197,361 个请求，**工具判"不可能命中前缀空间"而 Caddy 命中了空间内路径：0**；对照组（把后缀 glob 与不锚定正则故意改成不命中的副本）报出 14 处危险分歧，说明差分能看见问题 |
| 2 | 🟡-2 SIGKILL 之后的残留 | **如实写明**（runbook §3.1 第 7 条、授权清单 O0-A05P 行与 U-5、工具 docstring）：SIGKILL 或断电时留下 `.o0probe`、`$TMPDIR/o0-caddy-probe-xdg-*`（`probe-run.json` 含哈希）与**一个仍在运行的真实 Caddy**（新会话，不随探针退出）。**属主记录**：探针建临时目录时立即写 `o0-probe-owner.json`（探针 pid、`.o0probe` 路径、写入前先记录的内容 sha256、每个子进程 pid；在临界区内原子替换）。**启动扫描**（`scan_probe_residue`，在写任何东西之前）：属主记录有效且记录的探针已不在运行 → 停掉命令行里带该目录路径的进程，以及命令行带记录的 `.o0probe` 路径的已记录子进程（组长是已记录 pid 时整组 SIGTERM，否则单个 SIGTERM；5 秒后 SIGKILL），`.o0probe` 内容与记录的 sha 相同才删，再删目录，打印 `PROBE_RESIDUE_CLEANED`；记录的探针仍在运行 → `BUSY`，不动；没有本工具属主记录的目录 → `FOREIGN`，不动；命令行带 `o0-caddy-probe-xdg-` 但无法证明归属的进程 → `ORPHAN pid=`，不动；进程表不可用且记录的子进程还活着 → `UNPROVEN`，不动；汇总一行 `PROBE_RESIDUE_SCAN`。扫描之后副本旁仍有 `.o0probe` → `CADDY_PROBE_FAILED … already exists`，拒绝运行（它可能是用户还在看的 `--keep` 副本）。只打印目录名与 pid。**runbook**：完成后三项检查（`ls -A`、`ls -d "$TMPDIR"/o0-caddy-probe-xdg-*`、`pgrep -fl o0-caddy-probe-xdg`）与处置；故障对照加三行 | selftest（假 caddy）：探针在活体检查中被 SIGKILL → 断言留下 `.o0probe`、带属主记录的目录与仍在运行的假 Caddy；再放一个无记录目录、一个"探针仍在运行"的目录、一个命令行只带前缀的进程；再跑探针 → `CLEANED … processes_stopped=1 o0probe=removed dir_removed=True`、假 Caddy 已停、目录已删，`FOREIGN`/`BUSY`/`ORPHAN` 各一行且三者都原样保留；无记录的 `.o0probe` → 拒绝运行且文件不变；内容已改的 `.o0probe` → 目录删、文件留（`o0probe=kept`）；`mkdtemp()` 之后属主记录立即存在；不属于已记录组长、忽略 SIGTERM 的进程 → 先收到 SIGTERM、5 秒后被 SIGKILL；`caddy adapt` 挂住时 SIGKILL 探针 → 孤儿 adapt 只在命令行里带 `.o0probe`，靠已记录的 pid 被停掉。真实 Caddy：SIGKILL 探针后真实 Caddy 仍在运行（记录 pid），同一 `TMPDIR` 再跑探针 → `PROBE_RESIDUE_CLEANED … processes_stopped=1 o0probe=removed`、那个 Caddy 已停、目录与 `.o0probe` 都没了，本次 `CADDY_PROBE_OK` |
| 3 | 探针前确认项与 U-6 | runbook §3.1 第 7 条新增"探针前确认项 PC-1…PC-5"（即审查的 C-1…C-5；改名是为了不与阶段 C 的"C-1 前置门禁"混淆）：生产 Caddy 版本、jp-bot 站点块里的 `host` 匹配器、站点头字面写法（决定 `--site-address` 与 `--host`）、方案 (b) 与跑完后的三项检查、`/m/v1/watcher` 下有没有具体参数值 / 后缀或中段 glob / 不锚定正则。授权清单 U-5 列出同样五项，O0-A05P 前置加"PC-1…PC-5 已确认"、时机改为 wac-094 合入之后；U-6 与 runbook 第 4 条审阅要点加"具体参数值、后缀或中段 glob、不锚定 `path_regexp`"。**PC-3 顺带修了一个工具缺陷**：`probe_caddyfile` 原来跳过以 `{` 开头的行，站点头写成 `{$CADDY_DOMAIN} {` 时 `--site-address` 永远找不到（审查建议的写法实际会失败）；改为只跳过裸的全局 `{`，并加钉子（占位符、带 scheme、带端口、多地址四种站点头；找不到地址时失败而不是猜） | selftest 四种站点头钉子 + 找不到时 `ArtifactError`；`auth_gate_test.sh`（runbook 示例命令核对）仍通过 |

💭 处置：💭-1 已在 §12.2 更正（n15b 在 r091 已是 FAIL）；💭-2 桩写响应时吞掉 `BrokenPipeError`/`ConnectionResetError`；💭-3 `run.install()` 提到导入 `redact_line` 之前（扫描与导入期间的 Ctrl-C 统一输出 `CADDY_PROBE_INTERRUPTED`）；💭-4（变异运行隔离 `TMPDIR`、按路径兜底杀进程）本轮的变异脚本照做，见 §13.3。

### 13.2 本机实跑（未连 jp-24；打包门禁 `--report-only`，未带 `--execute`）

代码提交 `3a3cc93`（主体）与 `58dd56c`（变异补测），`auto/wac-094`，基于集成分支 `8bb1806`（含 wac-092 合并 `a175631`）。Caddy 二进制同 §10.4（审查者构建的 v2.10.2，经 `O0_CADDY_BIN` 使用，不进仓库）。`run_all.sh` 用本机默认 `$TMPDIR` 跑（为了核对真实临时目录没有新增残留）；变体、差分与变异各用 scratchpad 里单独的 `TMPDIR`。

| 命令 | 结果 |
|---|---|
| `O0_CADDY_BIN=<v2.10.2> bash -o pipefail scripts/ops/o0/tests/run_all.sh`（`58dd56c`） | 退出 0，末行 `ALL_O0_OFFLINE_CHECKS_OK real_caddy=ok`：`BASH_N_OK files=13`、`PY_COMPILE_OK`；o0_tool `SELFTEST_OK checks=104`；credentials `scenarios=13`；`CADDY_ARTIFACTS_OK lines=16`；Caddy 工具 `SELFTEST_OK good_passes=241 variants_caught=85/85 (raw and skeleton) benign_two_step=11 checks=199 … prefix_space=ok probe_residue=ok`（原 77/77、8、130）；`REDACTION_PARITY_OK corpus=83`；`LEAK_TEST_OK leaks=0`；`FLEET_GUARD_TEST_OK cases=7`；`AUTH_GATE_TEST_OK checks=24`；`APPLY_ROLLBACK_TEST_OK checks=152`；`CADDY_REAL_TEST OK checks=86 failures=0 caddy=v2.10.2`（原 68）；`PLAN_MODE_OK scripts=5` |
| 同上但不设 `O0_CADDY_BIN` | 退出 0，`CADDY_REAL_TEST_SKIPPED`、`APPLY_ROLLBACK_TEST_OK checks=150`，末行 `O0_OFFLINE_CHECKS_OK_WITHOUT_REAL_CADDY real_caddy=skipped` |
| 13 个 shell 脚本逐个 `bash -n` | 13/13 |
| 真实 Caddy 探针（仿生产夹具） | `CADDY_PROBE_OK caddy=v2.10.2 live_checks=840 verify_passes=241 lines=16 host=jp-bot.balen.wang … stub_hits=oq:327,watcher:0,sink:0`（原 420、117；多出的是参数值）；`PROBE_RESIDUE_SCAN cleaned=0 busy=0 foreign=0 orphans=0 unproven=0` |
| 审查 r093 的全部变体（r091 `cv/gen.py` 的 67 个 + r093 `cv/gen2.py` 的 25 个，共 92 个；`run.sh` 复制到 `w094/cv/`，只改工作目录与分支路径，审查证据未动；每个都跑 verify 与完整探针） | 见 §13.1 第 1 项：v14–v20 由 verify 通过变为失败（v15–v19 活体也失败），其余 85 个 verify 结论不变；所有 `v.out`、`p.out` 无 `$2a$`/`$2b$`；无 `.o0probe` 残留；87 次探针的扫描行都是 `cleaned=0 busy=0 foreign=0 orphans=0` |
| 前缀空间黑盒可靠性差分（`w094/spacediff.py`，种子 1、2） | 197,361 个请求，危险分歧 0；对照组（故意改坏的副本）危险分歧 14 |
| `o0_package.sh --candidate 3a3cc93 … --report-only --run-tests`，以及 `--candidate 58dd56c`（都未带 `--execute`） | 两次都是 **G1–G12 全部 PASS**：G2 `ROUTES_DIFF_EMPTY rows=63 … phase_max=P2`；G3 `META_OK`；G4 `CLOSURE_OK closure=16 whitelist=36`；G5 `COMPOSE_OK env_file=True`；G8 5 个文件；G9 pytest `35 passed`、watcher `tests 155 / pass 155 / fail 0 / skipped 0`；G10 17 个候选工具与 runbook 文件无禁用动词；G11 14 个工具文件；G12 `RUNTIME_MANIFEST_OK files=36`。`PACKAGE_OK`，`RELEASE.json`：`deploy_candidate: true`、`failed_gates: 0`、`phase_max P2` |
| 本机 `~/Library/Application Support/Caddy/` | 开工前逐文件快照（`stat` 修改时间、大小、权限与 sha256，3 个文件）；经历全部 `run_all`、真实 Caddy 测试、92 个变体探针、差分、44 次变异流程的完整运行（本任务 24 + 复跑 5、审查 13 的重建、基线 2）与两次打包门禁之后，最终快照逐文件相同（`USER_CADDY_DIR_UNCHANGED`）。`~/.config/caddy` 不存在。只做了检查，没有删除 |
| 本机 `$TMPDIR` 与进程 | 开工前 `$TMPDIR` 下有 8 个 `o0-*` 条目：7 个 `o0-caddy-probe-xdg-*`（09-27 23:57，wac-090 执行者的变异留下）与 1 个 `o0-tool-selftest-80jw8ghi`（09-27 15:24，测试夹具）。本任务全部运行之后仍是这 8 个，逐项相同，没有新增，也没有动它们（探针运行时会把前 7 个报成 `PROBE_RESIDUE_FOREIGN`）；`w094-spacediff-*`、`o0-caddy-real.*`、`o0-probe-selftest-*`、`o0-caddy-selftest-*` 均为 0；`pgrep -fl o0-probe-selftest`、`pgrep -fl o0-caddy-probe-xdg`、`pgrep -fl 'caddy run'` 都无输出；scratchpad 里各隔离 `TMPDIR` 为空，没有 `.o0probe` |

证据文件（scratchpad `w094/`）：`runall-{caddy,nocaddy}-final.log`、`caddyreal-2.log`、`pkg.log`、`pkg-final.log`、`pkg/`、`pkg-final/`、`cv/`（`run.sh`、`full-run.txt`、`work/case-*`）、`spacediff.py`、`spacediff-{1,2}.txt`、`mut/`（`muts094.py`、`muts093r.py`、`runmut*.sh`、`out094.txt`、`out094-r2.txt`、`out093r.txt`）、`caddyhome-{before,final}.stat`、`tmpdir-o0-{before,final}.txt`。

### 13.3 变异（scratchpad `w094/mut/`，pristine = 本任务代码，测试集 = 带真实 Caddy 的完整 `run_all.sh`，串行；每个变异都用隔离的 `TMPDIR`，结束后按路径杀掉命令行带该变异目录的进程并删目录，审查 💭-4）

| 变异集 | 结果 |
|---|---|
| 本任务 W01–W24（前缀空间 10、探针参数值 1、残留扫描 12、PC-3 站点头 1） | 首轮 19/24 KILLED。存活 W13（扫描不发 SIGTERM）、W24（非组长不 SIGKILL）、W18（不记录子进程 pid）、W23（`mkdtemp` 时不写属主记录）：前两者被整组 SIGKILL 兜底掩盖，W18 只在孤儿进程不带目录路径时才有区别，W23 被后续写入掩盖。补了三个 selftest 场景（`58dd56c`：`mkdtemp` 之后立即有记录；不属于已记录组长、忽略 SIGTERM 的进程先收 SIGTERM、5 秒后被 SIGKILL；`caddy adapt` 挂住时 SIGKILL 探针，孤儿 adapt 靠已记录 pid 被停）后复跑，四个都 KILLED。**W08（忽略 `(?i)` 等标志）判为等价变异**：`(?` 总以 `(` 开头，而 `(` 本身就结束字面前缀，所以标志检查只是纵深防御，去掉它不改变任何判定。合计 23/24 KILLED，1 个等价 |
| 审查 wac-093 的 M01–M13 与 M09b | 审查的 `muts093.py` 只保留了 M09b，其余 12 个按 `reviews/wac-092.md` §6 与 `r093/mut/out093.txt` 的描述重建并在本分支代码上重新定锚（`w094/mut/muts093r.py`）：**13/13 KILLED**（M01、M02 现在先被前缀空间的钉子抓到） |
| 基线（未变异，与变异同一流程） | 全绿 |

### 13.4 剩余限制

1. **更保守 = 可能误拦生产写法**：站点顶层任何可能改写或终止请求的指令，只要匹配器是后缀 glob（`*.php` 上的 `redir`）、中段 glob、以 `*` / `?` / `[` 开头，或是不锚定的正则，在 C-1 都会失败，哪怕它实际只服务于前缀之外的路径。这是有意的，与 §12.4 第 1 条同一取舍：生产副本因此失败时改写匹配器（例如加上排除前缀的路径条件，或移出 jp-bot 站点块），不放宽检查；例外由 Architect 裁定。只改响应头的 `header`、`encode` 等白名单处理器不受影响。
2. **探针的参数值是有限集合**：35 个值覆盖常见拼写，但复现不了"只对某个罕见值生效"的写法（v14 的 `%252F`、v20 的字面 `[0-9]` 开头的 id）；这类只由 verify 的前缀空间规则把关，人工记录（U-6、PC-5）是第二道。
3. **自清理只在"再跑一次探针"时发生，而且只清理能证明的部分**：wac-094 之前的版本留下的目录、属主记录丢失的目录（例如在写入记录前被 SIGKILL 的极短窗口）、别的工具留下的进程，都只报告不动；探针从不删用户目录里不属于它的东西。runbook 的三项检查仍是必做项。`pgrep`/`ps` 不可用时扫描只处理能用 `kill -0` 证明已死的记录，其余报 `UNPROVEN`。
4. **扫描在同一 `$TMPDIR` 里进行**：换了 shell、换了用户或 `TMPDIR` 不同，上一轮的残留就不在扫描范围内；runbook 要求三项检查在运行探针的同一个 shell 里做。
5. 其余同 §12.4 第 2、4、5 条。§12.4 第 3 条（SIGKILL 只写了一半）已由本节第 2 项更正。

## 14. wac-097：Caddy 核对第四轮（改路径的处理器、正则字符类；处置审查 wac-095（报告 `reviews/wac-094.md`）🟡-2、🟡-3 与 💭-4，以及 Planner 转达的复审 wac-098（报告 `reviews/wac-096.md`）🔴-1、🟡-1；未连 jp-24，未做只读生产核对）

依据：`reviews/wac-094.md` §3、§3.3、§7 🟡-2、🟡-3、§8 💭-4、§9 U-6；`reviews/wac-096.md` 🔴-1、🟡-1；契约 §9.14.3 F-10、F-13（遮蔽第 1 步"无法评估的一律视为命中"）、§9.3 前缀中间件触发条件；Caddy v2.10.2 `modules/caddyhttp/rewrite/rewrite.go`（`Rewrite.Rewrite` 的 uri 拆分、`CleanPath` 之后 `trimPathPrefix`、`changePath`）、`caddyhttp.CleanPath`；Go `regexp/syntax` `parseClass`、`parseNamedClass` 与 `\Q…\E`。审查 wac-095 🟡-1（面板 `/v1/*` 注入 observer）属 wac-096（WGW-1.0.3），本任务不处理：仿生产夹具与 g19 仍判通过。

### 14.1 处置

| # | 项 | 处置 | 测试（selftest / 真实 Caddy） |
|---|---|---|---|
| 1 | 🟡-2 改路径的处理器 | **遮蔽第 1 步**（`path_change_into_space`）：路由里只要有改路径的处理器——`rewrite` handler 带 `uri`（路径部分）、`strip_path_prefix`、`strip_path_suffix`、`uri_substring`、`path_regexp`，或 `reverse_proxy` 自带的 `rewrite`——**不论匹配器是什么都算命中**，除非证明改写结果进不了 `/m/v1/watcher` 前缀空间与 operator-query 的 `/v1/watcher` 网关空间。可证明的只有三种：`uri` 的路径部分是不含占位符、`%`、glob 字符、`#`、`?`、`//`、点段、空白的常量绝对路径且不在两个空间内（p03 `rewrite /healthz /api/status` 放行）；`strip_prefix P` 的每一种剪法都在空间外；`strip_suffix` 作用在不可能以两个前缀开头的路径上。只改方法不算改路径；路径部分为空的 `uri`（`?a=1`、`#frag`）不改路径，但不当证明，按可能命中（见第 5 项）。可能的路径用"条目集合"表示（`path` 模式、`path_regexp`、"不可能以前缀开头"），匹配器把它替换成自己的条目（当前路径必须满足它，所以是交集的超集），`uri` 常量替换成该常量，`strip_prefix` 加上剪过的模式。**转发检查**（`_rewrite_forwarder_check`）：`reverse_proxy` 到 8183（按端口）之前、在它自己的链上改过路径（`handle_path`、`handle` 里先 `uri` 再转发、`reverse_proxy { rewrite }`、`forward_auth` 的 `uri`），改写结果同样要证明在两个空间外（g18）；9090/9100 不在此判（浏览器检查要求每条 watcher 路由有 basic auth 并被样本覆盖，watcher 不服务 `/m/…`、`/v1/…`）。**模拟器**支持常量 `uri`（💭-4 的 p03 不再 UNCOMPARABLE）。**探针**：从 adapt 后的配置构造"改写入口"（`strip_prefix P` → `P` + 6 个尾巴，多段 P 另加 `%2e%2e` 形式；`replace F R` → 把尾巴里的 R 换成 F；常量改写进任一空间 → 按匹配器填出的路径；`strip_suffix S` → 前缀 + S），带与不带假 `Authorization` 各发一次，到达 operator-query 的请求头必须与调用方发出的相同，到达 watcher 即失败。 | selftest：函数级 21 个"算命中"、13 个"证明在外"；verify 级新增变体 g15、g16、g17、g18、handle_path 不注入（只有转发规则能抓）、`reverse_proxy` 自带 rewrite、CONNECT 的 strip_prefix / path_regexp 改写 / 占位符 uri / `watcherx` 的 strip_suffix（只有第 1 步新规则能抓）、原 benign 的 `^/static/` + `strip_prefix` 改列变体；新 benign：p03、p05、p06、`/api/v2/*` 剥 `/api`、只改方法、`handle` 里剥 `/reports` 再转发。真实 Caddy：g15–g18、handle_path 不注入、`handle_path /reports/daily/*`（Caddy 剥掉整个 `/reports/daily`）、`reverse_proxy { rewrite /m/v1/watcher/status }`、`uri /m/v1/watcherx* strip_suffix x`、`uri /m/m/* strip_prefix /m/v1` verify 失败；p03、p05、p06、`uri /api/v2/* strip_prefix /api`、`handle /reports/daily/* { uri strip_prefix /reports … }` 通过；g16、g17、g18 与多段剥前缀作为探针副本，verify 与活体检查各自失败；夹具本身 10 个入口全部通过 |
| 2 | 🟡-3 正则扫描器 | `_re_top_level_alternation` 与 `_re_class_end` 照搬 `_re2_compile` / Go `parseClass` 的规则：`[` 之后可选 `^`，紧跟的 `]` 是字面成员；类内 `\` 转义下一个字符；`[:` 到下一个 `:]` 是 POSIX 类；`\Q` 到 `\E`（或结尾）全是字面；类不闭合或 `)` 多余按命中。`_regexp_may_hit_space` 与新的 `_regexp_may_start_prefix` 共用 `_regexp_literal_head` | selftest：13 个必须看到顶层 `|` 的模式（g01c、g04c、g04d、`a\.bmp$` 形式、`\Q(\E|…`、`[[:alpha:](]|…`、`[\]]|…` 等）、8 个不得误判的（`\Q(|x` 未闭合、`[]|]x`、`[^]|]x`、`[[:alpha:]|]x`、`\|x` 等）；verify 级变体 g01c、g04c、g04d、`a\.bmp$`（探针参数值里没有 `.bmp`，只能靠 verify）、CONNECT 的 `[^](][^]]|/status$`。真实 Caddy：g01c、g04c、g04d、`a\.bmp$`、`\Q`、POSIX 六个 verify 失败 |
| 3 | 复审 wac-098 🔴-1（Planner 追加） | `reverse_proxy` 的 `rewrite` 字段在第 1 步、转发检查、探针入口里都按改路径处理；改写结果也对 `/v1/watcher`（等于它，或以 `/v1/watcher/`、`/v1/watcher%` 开头，另加 `\n`，任意大小写）判定 | selftest 变体：`handle /foo/* { reverse_proxy <oq> { rewrite /v1/watcher/dialogs; header_up observer } }`、`rewrite /v1/watcher{path}`、`forward_auth /bar/* { uri /v1/watcher/status }`；benign：`forward_auth /bar/* { uri /v1/auth }`。真实 Caddy 同上四项，第一项作为探针副本活体也失败（`/foo/x` 以 `/v1/watcher/dialogs` 带注入头到达 oq 桩） |
| 4 | 复审 wac-098 🟡-1（Planner 追加） | 路由枚举覆盖 `routes`、`subroute.routes`、`errors.routes`（run_verify 对错误链另跑转发检查与改路径转发检查）、`invoke` → `named_routes`（按调用处上下文，名字找不到或递归按 UNCOMPARABLE）、`reverse_proxy.handle_response[].routes`；其他带嵌套路由的 handler 在 wac-097 版本里只在特定条件下判 UNCOMPARABLE（复审 wac-099 🔴-2，已在返工中改为一律判失败，见 §14.5）；watcher 覆盖检查也枚举这些容器（错误链、命名路由里转发到 watcher 而没被浏览器样本覆盖的判失败）；模拟器遇到 `invoke` 判 UNCOMPARABLE | selftest 变体：`handle_errors` 转发 oq 带 observer、`handle_path /x/* { invoke obs }`、`/m/* { invoke obs }`、未知 `invoke` 名字、`handle_errors` 把 `/api/*` 转给 watcher、sink 的 `handle_response` 转发 oq、未知容器，以及两个只有容器规则能抓的（兜底之后的死路由 `/m/v1/watcher/extra` 上的 `invoke obs` 与未知容器，模拟器碰不到它们）；benign：`handle_errors { respond 502 }`、`/reports/*` invoke 到别的服务。真实 Caddy：`handle_errors`、`handle_path` + `invoke`、`/m/*` + `invoke`、死路由 `/m/v1/watcher/extra` + `invoke`、`handle_response` 五项 verify 失败，`handle_errors { respond }` 通过；`handle_errors` 作为探针副本，活体里"浏览器路径不带凭据应 401 且不到任何桩"一项失败（401 进入错误链后被转发） |
| 5 | 复审 wac-098 第二轮 🔴-A、🔴-B（Planner 第二次追加；R22 封闭白名单属后续 RS 任务） | `uri` 的路径部分为空（`?a=1`、`#frag`）时 Caddy 不改路径、原路径照转：不再当作"不改"，按可能命中（改路径集合置为未知）；`reverse_proxy` 自带的 `rewrite` 只作用于发往上游的副本，`handle_response` 路由按原请求（外层改写之后、该 `rewrite` 之前）判定；同一个 `rewrite` 对象里 `uri` 之后的 `uri_substring` 等照 Caddy 顺序计算 | selftest：函数级 `?v=1`、`#frag`、`uri` 加 `uri_substring`、改到 `/V1/Watcher/…`、`handle_response` 里的剥前缀、`invoke` 的剥前缀、未知 `invoke`、未知容器都算命中；verify 级变体 q1、q2、q3、q4、`/foo/*` 上的 `rewrite ?a=1`（只有新规则能抓）、`handle_path` 加 `handle_response` 原请求、`uri` 加 `uri_substring`。真实 Caddy：q1–q4 与 `handle_path` 加 `handle_response` 五项 verify 失败；q1 作为探针副本活体失败；只在调用方**不带** `Authorization` 时注入的写法，由入口的第二次发送（不带头）在活体里抓到 |

**本轮用真实 Caddy 找到并修掉的一处**：最初按"剥掉整个 P 或不剥"建模 `strip_prefix`。Caddy 先对**未解码**路径 clean，再在解码后的字符上剪 P，`%2e%2e` 因此能在剪完之后只弹掉 P 的一部分：`uri /m/m/* strip_prefix /m/v1` 下，`/m/v1/%2e%2e/m/v1/watcher/status` 被匹配成 `/m/m/v1/watcher/status`，剪完却是 `/m/v1/watcher/status`。现在对 P 的每一段前缀（`/m`、`/m/v1`）都加一份剪过的模式，剩余以 `.` 开头或 P 含点段时按未知处理；selftest 与真实 Caddy（含探针的 `%2e%2e` 入口）都有这个用例。

**与 WGW-1.0.3 草案的一处差异（交 Architect）**：复审 wac-098 建议 RS-6 期望"站点顶层**不带匹配器**的 `forward_auth 8183 { uri /v1/auth }` → PASS"。本工具的 F-13 遮蔽检查仍按契约白名单判它失败（`reverse_proxy` 不在白名单，F-13 原文把 `forward_auth` 列为失败类型）；带匹配器且匹配器打不中前缀的 `forward_auth /bar/* { uri /v1/auth }` 现在通过。两者如何统一由 wac-096 定。

### 14.2 本机实跑（未连 jp-24；打包门禁 `--report-only`，未带 `--execute`）

代码提交 `0cafdfc`（`auto/wac-097`，基于集成分支 `6e80efb`，含 wac-094 合并 `8ab8f43`）。Caddy 二进制同 §10.4（审查者构建的 v2.10.2，经 `O0_CADDY_BIN` 使用，不进仓库）。`run_all.sh` 用本机默认 `$TMPDIR`；变体、差分与变异各用 scratchpad 里单独的 `TMPDIR`。

| 命令 | 结果 |
|---|---|
| `O0_CADDY_BIN=<v2.10.2> bash -o pipefail scripts/ops/o0/tests/run_all.sh` | 退出 0，末行 `ALL_O0_OFFLINE_CHECKS_OK real_caddy=ok`：`BASH_N_OK files=13`；Caddy 工具 `SELFTEST_OK good_passes=242 variants_caught=120/120 (raw and skeleton) benign_two_step=18 checks=279 … path_changes=ok re2_classes=ok rewrite_entries=ok`；`LEAK_TEST_OK leaks=0`；`AUTH_GATE_TEST_OK checks=24`；`APPLY_ROLLBACK_TEST_OK checks=152`；`CADDY_REAL_TEST OK checks=131 failures=0 caddy=v2.10.2`；`PLAN_MODE_OK scripts=5` |
| 同上但不设 `O0_CADDY_BIN` | 退出 0，`CADDY_REAL_TEST_SKIPPED`，末行 `O0_OFFLINE_CHECKS_OK_WITHOUT_REAL_CADDY real_caddy=skipped` |
| 13 个 shell 脚本逐个 `bash -n` | 13/13 |
| 仿生产夹具的真实探针（`caddy_real_test` 第 3 段） | `CADDY_PROBE_OK caddy=v2.10.2 live_checks=860 verify_passes=242 …`（wac-094 的 840 加 10 个改写入口各发两次）；`PROBE_REWRITE_ENTRIES targets=10`，全部通过 |
| `o0_package.sh --candidate 0cafdfc --out <scratchpad> --report-only --run-tests`（未带 `--execute`） | **G1–G12 全部 PASS**（13 行）：G2 `ROUTES_DIFF_EMPTY rows=63 … phase_max=P2`；G4 `CLOSURE_OK closure=16 whitelist=36`；G8 5 个文件；G9 pytest `35 passed`、watcher `155/155`；G10 17 个文件无生产动词；G12 `RUNTIME_MANIFEST_OK files=36`；`PACKAGE_OK`；`RELEASE.json` `deploy_candidate: true`、`failed_gates: 0` |
| 审查 r095 的全部变体（r091 的 67 + r093 的 25 + r095 的 36，`cv/run.sh` 复制到 `w097/cv/`，只改工作目录与分支路径；每个都跑 verify 与完整探针） | 判定改变的恰好 9 个：g01c、g04c、g04d、g15、g16、g17、g18、`imp-at-end-after-catchall`（审查期望 FAIL，现在因为 import 之前的浏览器 `handle` 带 `strip_prefix /watcher` 而失败）由通过变为失败，p03 由失败变为通过；g16、g18 活体 12 处失败、g17 8 处（改写入口）；其余 118 个不变，g19（面板 `/v1/*`，wac-096 的范围）仍通过。所有 `v.out`、`p.out` 无 `$2a$`；无 `.o0probe`；122 次探针的扫描行都是 `cleaned=0 busy=0 foreign=0 orphans=0 unproven=0` |
| 审查 wac-095 差分里的 216 个 `path_regexp` 漏判（`spacediff/out-*.txt`，本任务的 `w097/re54/check.py` 用旧工具 `8ab8f43` 与新工具对照） | 旧工具：函数层面 216/216 判"不命中"，整条 verify 放过 54 个（全是以 `]` 开头的字符类）；新工具：函数层面 0 个漏判，整条 verify 放过 0 个 |
| 审查者自己的前缀空间差分 `sd.py`（种子 1、2、3、7、11，未改动，指向本分支） | 1,186,015 次请求，**危险分歧 0**（审查时同样的种子为 216 个 re 模式）；path 部分仍为 0 |
| 本任务的改路径差分 `w097/pcdiff/pcdiff.py`（独立写的黑盒：每个 `(匹配器, 改写)` 在真实 Caddy 里先由对照路由确认请求原本在两个空间之外，再看改写后是否落进 `/m/v1/watcher` 或 `/v1/watcher`；工具判"证明在外"的，Caddy 一次命中即为危险） | 种子 1、2、3、5、7、11，各 400 条路由，共 421,979 次请求，**危险分歧 0**。第一版在种子 3、5 各发现 2 个危险（`uri /m/m/* strip_prefix /m/v1` 遇 `%2e%2e`），修复后为 0（见 14.1）。对照组：只剪整个 P、`strip_suffix` 一律在外、常量 `uri` 一律在外、去掉 `/v1/watcher` 空间，四个故意改坏的副本分别报出 3、399、2456、866 个危险 |
| 本机 `~/Library/Application Support/Caddy/` | 开工前逐文件快照（修改时间、大小、权限、sha256，3 个文件）；全部运行之后逐文件相同（`USER_CADDY_DIR_UNCHANGED`）。`~/.config/caddy` 不存在。只做了检查，没有删除 |
| 本机 `$TMPDIR` 与进程 | 开工前 `$TMPDIR` 下 8 个 `o0-*` 条目（7 个 `o0-caddy-probe-xdg-*`、1 个 `o0-tool-selftest-80jw8ghi`，都是早先留下的，见 U-12），之后仍是这 8 个，逐项相同，没有新增，也没有动它们；本任务用到的各个 scratchpad `TMPDIR` 最后都为空；没有遗留的 caddy、探针或差分进程 |

证据文件（scratchpad `w097/`）：`runall-{caddy,nocaddy}-final.log`、`real5.txt`、`pkg.log`、`pkg/`、`cv/`（`run.sh`、`full-run-final.txt`、`work/case-*`）、`re54/`、`spacediff2/`、`pcdiff/`（`pcdiff.py`、`out4-*.txt`、`out3-broken*-3.txt`）、`mut/`（`muts.py`、`run.sh`、`out-final.txt`）、`caddyhome-{before,final}.txt`、`tmpdir-o0-{before,final}.txt`。

### 14.3 变异（scratchpad `w097/mut/`，只针对本轮新增代码；每个变异一份副本、一个隔离的 `TMPDIR`，先跑 selftest，存活的再跑真实 Caddy 的 `caddy_real_test.sh`，结束后按路径杀掉命令行带该副本目录的进程）

38 个变异 **38 个被杀**：扫描器 4 个（以 `]` 开头的类、`\Q…\E`、POSIX 类、`)` 多余时的顶层 `|`），遮蔽第 1 步 3 个（不看改路径、站点内不看、匹配器不收窄），`strip_prefix` 4 个（只剪整个 P、丢掉未剪的模式、放过以 `.` 开头的剩余、放过"头是 P 的前缀"），`strip_suffix` 2 个，常量 `uri` 3 个（一律在外、放过点段、路径部分为空当作不改），`uri_substring`/`path_regexp` 当作不改，转发检查 7 个（改路径规则整体关闭、忽略 `reverse_proxy` 自带 rewrite、收窄失效、不跟 `handle_response`、不跟 `invoke`、不查错误链、放过未知容器），`handle_response` 用改写后的副本推断路径 2 个，去掉 `/v1/watcher` 空间，watcher 覆盖只看主链，模拟器不做常量 `uri`，探针入口 7 个（不发入口、只带头发一次、不比较 `Authorization`、没有 `%2e%2e` 形式、不看 `uri_substring`、不看 `reverse_proxy` 自带 rewrite、没有 `/v1/watcher` 尾巴）。首轮有 3 个存活，都补了测试：N20（只带头发送）补"只在调用方不带 `Authorization` 时注入"的真实 Caddy 探针副本；N27（转发检查不跟 `invoke`）与 N33（放过未知容器）原来的用例同时被模拟器和移动端样本抓住，补了只有容器规则能抓的死路由用例（兜底之后的 `/m/v1/watcher/extra`）。

### 14.4 剩余限制

1. **更保守 = 可能误拦生产写法**：站点顶层的 `rewrite`、`uri`、`try_files`，只要工具证明不了改写结果在两个空间之外（占位符、`replace`、`path_regexp` 改写、剥前缀后可能是任意路径、只改查询串的 `uri`），C-1 都会失败；`handle_path … { reverse_proxy <8183> }` 在 `reverse_proxy` 不带自己的匹配器时一律失败；带了匹配器（`reverse_proxy /v1/* …`、内层 `handle /v1/* { … }`）时，改路径之后的匹配器只收窄路径集合、不撤销"已改过路径"，收窄后的路径仍可能落进 `/m/v1/watcher` 或 `/v1/watcher` 就失败（复审 wac-099 A1、A6），能证明在两个空间之外（`reverse_proxy /reports/* …`）才放行；错误链、命名路由里转发到 watcher 的路由因为浏览器样本覆盖不到也会失败。这是有意的，与 §13.4 第 1 条同一取舍：改候选，不放宽检查。骨架（`caddy_skeleton`）会把含疑似令牌的 `uri` 值藏起来，站点核对阶段这类改写也按可能命中处理。
2. **探针的改写入口是有限集合**：`path_regexp` 改写与占位符构造不出入口，只由 verify 把关（复审 wac-099 的 A7、A8 正是这两种写法，wac-097 版本的 verify 也放过了它们，返工后 verify 判失败，见 §14.5）；入口只用 GET。
3. **错误链的活体检查是间接的**：探针没有专门制造错误，`handle_errors` 转发只在"浏览器路径不带凭据应 401 且不到任何桩"那一项里被看到（401 进入错误链）；`forward_auth` 失败进入错误链的情形，探针里的桩总是成功，看不到，只由 verify 把关。
4. **F-13 与 WGW-1.0.3 草案的一处不一致**（见 14.1 末段）：站点顶层不带匹配器的 `forward_auth` 本工具判失败，草案 RS-6 期望通过；由 Architect 在 wac-096 定。Caddy 转发 8183 改为封闭白名单（Planner 裁定 R22）归后续 RS 任务，本轮没有实现。
5. 审查 wac-095 的 💭-1、💭-2、💭-3、💭-5 本轮没有处理（不在任务书范围）；💭-4 已处理（p03）。

### 14.5 返工：复审 wac-099（报告 `reviews/wac-097.md`）🔴-1、🔴-2（追加提交，不 amend；未连 jp-24）

Planner 两次指示：只按最小改动修两条 blocker、补对应用例；🟡/🟢 建议可以跳过；不要新增会误拦正常生产写法的规则（用户已定入口隔离改为网关独立监听端口 WGW-1.0.4，对 8183 的判定之后降为纵深防御，verify 主判据由后续 RS 任务改写）。R22 白名单与 `contracts/` 都没有动。

| # | 问题 | 修改（`scripts/ops/o0/o0_caddy_watcher_routes.py`） | 用例 |
|---|---|---|---|
| 1 | 🔴-1 改路径之后的匹配器把"已改过路径"清零（A1、A6、A7、A8 放过；A7、A8 探针也放过） | `_rewrite_forwarder_check` 改为同时带两组路径：未改过的 U 与改过的 C（`[]` = 没有，`None` = 任意）。匹配器分别收窄两组（`_narrow_keep`：匹配器自己的条目能证明在两个空间外时用它，否则当前集合能证明在外时保留当前集合，否则用匹配器条目；都是真实交集的超集），**不再清零**；改路径把 U∪C 移入 C；到 `reverse_proxy` 时只判 C。兄弟路由之间的传递只在该路由可能把请求交下去时发生（`_route_may_continue`：链上没有必然应答的 reverse_proxy、static_response、error、copy_response、不带 pass_thru 的 file_server，或以此结尾的无条件 subroute），而且不传给同一 `group` 的后续路由（Caddy 同组互斥）。`_forwarder_check`：外层匹配器打不中前缀、但路由会改路径进空间（`path_change_into_space`）时不再跳过，内层按全部前缀探针判定，只判 8183（watcher 端口仍归浏览器检查）；文档字符串里"改路径后由 `_forwarder_check` 判定"的错误说法已删 | selftest 变体 A1、A6、A7、A8（原始 JSON 与骨架）；良性：`handle_path /x/* { reverse_proxy /reports/* <oq> }`（匹配器证明在外）、"一条会应答的 handle_path 改路径后不影响另一组的 /v1/* 转发"。真实 Caddy：A1、A6、A7、A8 verify 失败，A1 作为探针副本活体也失败；`handle_path /x/* { reverse_proxy /reports/* 8183 }` 通过 |
| 2 | 🔴-2 未知容器只在特定条件下判 UNCOMPARABLE（C1 verify 与探针都放过） | 新增 `_container_check`：对服务器里每一个 handler（主路由、subroute、reverse_proxy 与 intercept 的 handle_response、错误链、每一个命名路由），只要带有本工具不跟进的嵌套路由或处理器（`_nested_route_keys`：未知 handler 的 `routes`/`handle_response`/`errors` 键或任何带 `handler`/`routes` 的嵌套对象；已知 handler 在跟进范围之外的同类内容，例如 subroute 自带的 `errors`），**不论匹配器、不论是否改过路径**都判 UNCOMPARABLE 失败。`intercept` 的 `handle_response` 按原请求在 `_path_changes`、`_forwarder_check`、`_rewrite_forwarder_check`、`rewrite_entry_targets`、`_walk_all_routes` 五处跟进；原来带条件的 `and changed` 分支删掉 | selftest 变体 C1、C1b（handle_response 里转发带 `/v1/*` 匹配器，只有跟进 intercept 的改路径规则能抓）、`/zzz/*` 上的未知容器与 subroute 自带 `errors`（只有 `_container_check` 能抓）；函数层面 C1 与 subroute `errors`。真实 Caddy：C1 verify 失败、探针副本活体失败；站点顶层只应答的 `intercept`（C4）通过（原来因未知容器被误拦，现在按已知容器跟进） |
| 3 | 🟡-1 三个存活变异的测试缺口（顺带补测，不新增规则） | 无代码改动 | E1、E1b（错误链里的 handle_path，后者只有错误链的改路径规则能抓）、E2（精确字面 `^/v1/watcherx$` 加 `strip_suffix x`，函数层面与 CONNECT 变体）、服务器顶层"另一主机 \| 本站 + /x/*"两个匹配器集合的路由（`_host_excludes_route` 的 all/any）。真实 Caddy：E1、E2 verify 失败 |

文档更正：`o0-authorization-list.md` 第 11 行与本文 §14.4 第 1 条"`handle_path … { reverse_proxy <8183> }` 一律失败"改为按代码实际行为描述（不带自己的匹配器时一律失败；带匹配器时按收窄后的路径判定，能证明在外才放行）；§14.1 第 4 项"其他带嵌套路由的 handler 按 UNCOMPARABLE"与 §14.4 第 2 条注明返工前的缺口；runbook 同步加一段说明。

**实跑**（scratchpad `w097/`）：
- selftest `SELFTEST_OK good_passes=243 variants_caught=133/133 (raw and skeleton) benign_two_step=21 checks=286`；
- `caddy_real_test.sh` `CADDY_REAL_TEST OK checks=142 failures=0 caddy=v2.10.2`，仿生产夹具探针 `CADDY_PROBE_OK … live_checks=860`；
- `run_all.sh` 带 Caddy 退出 0（`ALL_O0_OFFLINE_CHECKS_OK real_caddy=ok`），不带退出 0（`real_caddy=skipped`）；`bash -n` 13/13（`runall-{caddy,nocaddy}-rework.log`）。
- 审查者 r099 的全部用例（`gen.py`、`live.py`、`run.sh` 复制到 `w097/r099cv/`，只改目录）：审查期望 FAIL 的 A1–A9、B2、B5、C1、C2、C3、C5、C6、E1、E2 全部 verify 失败；活体里带着注入头到达 oq 的 16 个用例全部 verify 失败，verify 通过的用例没有一个在活体里被注入。判定改变的：C4 由失败变为通过（`intercept` 现在按已知容器跟进）；A5（`handle_path /x/* { reverse_proxy /m/* 8183 }`，oq 收到 `/m/…`，无害）由通过变为失败（合并规则：前缀不得由片段以外转发到 8183）。P1、P2、P10（`try_files`）、P4、P7、P11、B7 的保守判定与返工前相同。

**变异**（只针对返工代码，`w097/mut/muts2.py`，17 个，加原 38 个在返工后的复跑）：R01（匹配器清零改过的集合，即 🔴-1 本身）、R02（关掉 `_container_check`）、R03（改路径转发规则不跟 intercept）、R05（传给同组）、R06（所有路由都可能交下去）、R07、R08（错误链不跑改路径转发规则，即审查 M09）、R09（all→any，即审查 M10）、R10、R11、R12、R13、R15 被杀；**R04、R14、R16、R17 存活，属冗余**：它们各自关掉两道转发检查之一的某一步，被关掉的那一类写法仍由另一道判失败（`_forwarder_check` 对改路径路由的新规则与 `_rewrite_forwarder_check` 在 8183 上重叠），合起来的 verify 结论不变。原 38 个里 N05、N25（step 1 的改路径规则）返工后因为面板 `/v1/*` 也能抓到而存活，补了去掉面板的 g16 变体后被杀；N06、N16、N17、N32、N33、N34、N37 的锚点随代码改变，已按新代码改写为 R13、R14、R01、R15、R02、R16、R17。其余 29 个仍被杀。

**遗留（按 Planner 指示跳过）**：审查 🟢-1（`_re2_compile` 不认 POSIX 类，与扫描器不一致）、🟢-2（空 `\Q\E` 后跟量词的函数层漏判，整条 verify 能兜住）、🟢-3（oq 分隔符 `%` 冗余的注释）、🟡-2（站点顶层不带匹配器的 `forward_auth` 与 RS-6 的差异）、🟡-3（`try_files {path} /index.html` 的既有误拦写进 U-6）。这几项都没有改代码或文档，交给后续 RS 任务或 Planner。

