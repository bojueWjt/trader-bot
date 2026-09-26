# O-0 发行要求可追溯清单（wac-032 草案，wac-045 修订，wac-059 第 3 轮，wac-072 加固）

> 作者：watcher-app-crew-release-steward。状态：**草案，未执行任何生产动作，也没有做只读现场核对**。
> wac-045 修订：处置审查报告 `reviews/wac-032.md`（5 🔴、18 🟡、4 💭），逐条见 §7；按用户 2026-09-26 裁决（W-0b 方案 A、Caddy `*` 严格单段锚定 `path_regexp`）与 Planner 对 D-05…D-08 的决定更新。事实基线：集成分支 `f7641cb`（含 wac-040 `a881802`、W-0b）。
> wac-059 第 3 轮：处置复审报告 `reviews/wac-032-r2.md` 的 🔴-1 与 🟡-1…4，见 §8。WGW-1.0.2 的 Caddy 清单 v2 与片段适配不在本轮（另有任务 wac-060）。
> wac-072 加固（worktree `auto/wac-072`，基于集成分支 `487f6eb`）：处置终审报告 `reviews/wac-032-r3.md` §7 的剩余待办（🟡-1 路径令牌碎片、🟡-3 测试缺口）与 r2 遗留的 🟡-5、🟡-6、🟡-7，见 §9。仍不做 WGW-1.0.2 适配（wac-060）。
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
| P-02 | W-0 集成补齐 wac-015（E-02 格式、`req.watcherRoute`、W6/W7、改用生成的 `gateway-routes.js`、删手写 `watcher-routes.js`、停用 `db_manager.py` 直写） | **pending**，未开工 | G7 |
| P-03 | `db_manager.py` 写命令停用（D1），且 jp-24 上没有调用者 | 代码侧属 P-02；现场由 S-15 核对 | G7 + S-15 |
| P-04 | compose 把六个 `WATCHER_*` 交给 watcher（`env_file: /srv/trader-secrets/watcher-gateway.env`），健康检查不插值 | **done**：wac-040 已合入 `a881802` | G5（现 PASS） |
| P-05 | watcher 镜像白名单 `WATCHER_RUNTIME_RELATIVE_PATHS` 补齐运行时闭包 | **部分**：wac-040 补了 `lib/auth.js`、`lib/media.js`、`lib/status.js`、两个生成文件；W-0b 在其后合入，`lib/config-store.js` 仍不在白名单（本地 `CLOSURE_NOT_WHITELISTED lib/config-store.js`）。需 Planner 派发（可并入 wac-015） | G4（现 FAIL）、G12 |
| P-06 | （建议）watcher 提供只做数据库初始化后退出的入口，供副本演练用 | 未提。没有它时，阶段 W 用完整 `server.js` 在 `--network none` 容器里跑，看到 `Web UI listening` 即停 | 阶段 W build |
| P-07 | 数据库初始化失败退出前推 Telegram 告警（wac-011-r3 🟡-2） | **Planner 已定**：wac-015 复用 `sendWatcherAlert`；O-0 保留 90 秒重启计数兜底 | D-07 |
| P-08 | 若走正式发布流水线 `make_account_stall_release.py`：保留 `host/generated/` 子目录 | 本草案的 O-0 bundle 自带目录结构，不依赖该脚本；若改走流水线，需另派 | 阶段 O manifest |
| P-09 | C-0 校准（403 锁存、4 MiB、R12、E-14 拒因）与测试加固 | **done**：`eaf333d`（wac-022，审查 wac-023 PASS）、`122ef51`（wac-033，含持久化证据测试） | — |
| P-10 | C-0 每个 worker 预热成功或锁存时写一条日志，带 `revision`、`content_sha256`、pid | **wac-041 `c9b1e3f` 审查中**：格式 `snapshot_warmup result=… revision=… content_sha256=<12> pid=… role=operator-query duration_ms=…`；切换门禁用 `o0_tool.py warmup-check` 判定“每个 worker 一行成功” | 切换 runbook §3.2.3 |
| P-11 | T0-6 三路对照工具与报告（计划 §4.1 第 2 步、§4.3 T0-6） | **未派**（看板 M5：三路对照工具待派） | 切换 runbook §2 |
| P-12 | WGW-1.0.2 勘误：Caddy 清单中 `*` 的语义为“恰好一个非空段”，翻译为锚定、区分大小写的 `path_regexp` | **用户已裁决 2026-09-26**，勘误待出；核对工具已按裁决收紧 | D-03 |
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
| R04 | "operator-query 缺 `gateway` 时只禁用网关路由并告警，不得让交易端点启动失败（上线前配置校验锁定）" | 计划 §2.1、§4.2 | C-1 R11 已实现 | 阶段 O preflight import 冒烟断言 `_LOAD_ERROR is None`；生产不做缺 token 负例 |
| R05 | "轮换用 `*_TOKEN` + `*_TOKEN_PREVIOUS` 双值，顺序为：watcher 先接受新旧两值，网关切到新值，确认请求与审计正常，再撤旧值" | 计划 §2.1；契约 §9.2 | P-02 | 凭据 runbook §3 |
| R06 | "确认 import 与 handler 顺序" | 计划 §9 O-0 | — | S-02、S-03/S-04 + `o0_caddy_watcher_routes.py verify`（按 Caddy 路由顺序模拟，遮蔽即失败） |
| R07 | "`/m` 只 strip 一次" | 计划 §9 O-0 | — | verify：每条样本恰好一次 `strip_path_prefix /m`；L-probe |
| R08 | "上游仍是 8183" | 计划 §9 O-0 | — | verify：dial 恰为 `127.0.0.1:8183`；S-06 |
| R09 | "移动端 `Authorization` 保留且不被面板注入的 `system_observer` 覆盖" | 计划 §9 O-0 | — | verify：移动样本链路上无 Authorization 头操作；探针 `/m/v1/accounts` 无 token 必须 401（若被注入会变 200） |
| R10 | "浏览器 basicauth 后先清头再注入" | 计划 §9 O-0；§2.1 | — | verify：浏览器样本经 authentication，先删 `X-Watcher-Actor`/`X-Watcher-Token-Fingerprint`，再 set `X-Watcher-Proxy-Auth={env.WATCHER_BROWSER_PROXY_TOKEN}` |
| R11 | "`/media` 与其他公网入口没有绕过" | 计划 §9 O-0 | — | verify：任何代理到 9090/9100 的路由必须被浏览器样本覆盖且带认证，否则失败；S-06 只允许 127.0.0.1:9090；探针 `/media/<name>` 不得无认证返回图片 |
| R12 | "按路由真源逐路径追加"；"禁止 `/m/v1/*` 通配" | 计划 §9 O-0、§2.1 | P-12 | `render` 从生成清单产出片段；verify 静态检查拒绝通配与清单外路径 |
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
| R25 | "开关默认关，旧 reader 保留"（`WATCHER_CONFIG_SNAPSHOT_ENABLED` 缺省 0） | 计划 §2.2、§4.1；契约 §9.11 | — | 阶段 O preflight 断言未设或为 0 |
| R26 | 副本"无同步单元，已漂移……切换前必须逐行比对" | 计划 §1.2 | P-17 | S-09 `sync_operator_risk_db.py --check`；基线工具 |
| R27 | "`db_manager.py` 写命令停用或改走服务；feeder 只读" | 计划 D1、§8；契约 §9.12 | P-02/P-03 | G7 + S-15 |

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
| R37 | Caddy 清单是 O-0 核对 Caddy 逐路径配置的输入 | 契约 §9.14.3 | — | `render`/`verify` 只读该清单 |
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
| R66 | 不把 `{param}` 原样贴进 Caddyfile（未知占位符替换成空串） | wac-026 🟡-1 (a) | — | `render` 用 `[^/]+`；清单含 `{` 即拒绝 |
| R67 | 每行用 `path`+`method` 具名匹配器；媒体行 HEAD 显式列出 | wac-026 🟡-1 (b) | — | `render`；verify 比对方法集合 |
| R68 | Caddy `*` 在末尾时是前缀匹配、会跨段 | wac-026 🟡-1 (c) | P-12 | **用户裁决：锚定单段 `path_regexp`**；`--accept-prefix` 已删除；verify 对前缀写法、`(?i)`、`[^/]*`、`.+`、缺 `$`、静态行用 `path` 匹配器都判失败，负样本含多段、空段、尾斜杠、大小写变体 |
| R69 | `/m` 的 handle 块里不得有兜底 `reverse_proxy`，清单外路径落到 404 或其他既有路由 | wac-026 🟡-1 (d) | — | verify 负样本：尾斜杠、多段、空段、未列方法、未注册路径都不得到达网关 |
| R70 | 写核对脚本：解析 adapt 后 JSON，抽出 `/m/v1/watcher/` 全部 `(path, method)` 与清单逐行比较，对称差为空且行数 > 0 | wac-026 🟡-1 (e) | — | `o0_caddy_watcher_routes.py verify`，自测 14 种破坏全部抓住 |
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

## 6. 本地已实跑的证据（wac-072 更新，全部在本机，未连 jp-24）

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

命令：`bash scripts/ops/o0/o0_package.sh --candidate ce2461a --out <scratchpad>/pkg-wac059 --report-only --run-tests`（候选必须是带 `scripts/ops/o0` 的提交，G11；`ce2461a` = 本轮代码提交，其中集成分支部分为 `a9900e3`）。

| 门禁 | 结果 |
|---|---|
| G1、G2（`ROUTES_DIFF_EMPTY rows=63 … phase_max=P2`）、G3、G4（`CLOSURE_OK closure=13 whitelist=34`）、G5（`COMPOSE_OK env_file=True`）、G6、G8（5 个文件）、G10（17 个候选工具与 runbook 文件）、G11（14 个工具文件取自候选）、G12（`RUNTIME_MANIFEST_OK files=34`） | PASS |
| G9 | PASS：pytest `26 passed`；watcher `tests 107 / pass 107 / fail 0 / skipped 0` |
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

