# O-0 发行要求可追溯清单（wac-032 草案）

> 作者：watcher-app-crew-release-steward。状态：**草案，未执行任何生产动作，也没有做只读现场核对**。
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
| P-01 | W-0b 配置写路径合入集成分支（`auto/wac-011` @ `59e5a59`） | **blocked**：第 3 次 FAIL，等待用户裁决（审查推荐方案 A，只补测试 env 三个假 token） | 打包门禁 G6 |
| P-02 | W-0 集成补齐 wac-015（E-02 格式、`req.watcherRoute`、W6/W7、改用生成的 `gateway-routes.js`、删手写 `watcher-routes.js`、停用 `db_manager.py` 直写） | **pending**，未开工 | G7 |
| P-03 | `db_manager.py` 写命令停用（D1），且 jp-24 上没有调用者 | 代码侧属 P-02；现场由 S-15 核对 | G7 + S-15 |
| P-04 | compose 把六个 `WATCHER_*` 交给 watcher（建议 `env_file: /srv/trader-secrets/watcher-gateway.env`），健康检查不插值 | **未派任务**。集成分支 compose 只在健康检查脚本里读 `WATCHER_BROWSER_PROXY_TOKEN`，environment 未传任何凭据（wac-009 🟡-5） | G5 |
| P-05 | watcher 镜像白名单 `WATCHER_RUNTIME_RELATIVE_PATHS` 补齐运行时闭包 | **未派任务**。本地实测 `CLOSURE_FAILED closure=12 whitelist=28 missing=4`：`lib/auth.js`、`lib/media.js`、`lib/status.js`、`lib/generated/watcher-routes.js`；W-0b 合入后还要加 `lib/config-store.js`，wac-015 后换成 `lib/generated/gateway-routes.js` | G4 |
| P-06 | （建议）watcher 提供只做数据库初始化后退出的入口，供副本演练用 | 未提。没有它时，阶段 W 用完整 `server.js` 在 `--network none` 容器里跑，看到 `Web UI listening` 即停 | 阶段 W build |
| P-07 | 数据库初始化失败退出前推 Telegram 告警（wac-011-r3 🟡-2） | 未派。二选一：wac-015 补代码，或 O-0 巡检按容器重启次数告警（待 Planner 定） | D-07 |
| P-08 | 若走正式发布流水线 `make_account_stall_release.py`：保留 `host/generated/` 子目录 | 本草案的 O-0 bundle 自带目录结构，不依赖该脚本；若改走流水线，需另派 | 阶段 O manifest |
| P-09 | C-0 校准（403 锁存、4 MiB、R12、E-14 拒因）与测试加固 | **done**：`eaf333d`（wac-022，审查 wac-023 PASS）、`122ef51`（wac-033，含持久化证据测试） | — |
| P-10 | C-0 每个 worker 预热成功或锁存时写一条日志，带 `revision`、`content_sha256`、pid（wac-007 💭-2） | **未派**。没有它，"所有 worker 预热成功并记录 revision"无法逐 worker 观测 | 切换 runbook §3 |
| P-11 | T0-6 三路对照工具与报告（计划 §4.1 第 2 步、§4.3 T0-6） | **未派**（看板 M5：三路对照工具待派） | 切换 runbook §2 |
| P-12 | WGW-1.0.2 勘误：Caddy 清单中 `*` 的语义为"恰好一个非空段"，规定翻译方式（wac-026 🟡-1 第 5 点） | 未出 | D-03 |
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
| R19 | "记录部署前每节点状态/心跳/审计，部署后保持授权状态（HALTED 保持 HALTED，不把 RESUME 列为部署步骤）；Caddy 变更用 `validate` + `restart`（禁 reload）" | 计划 §4.2 | — | 每阶段 `fleet-before/after` 对比，变化即停；脚本自检 `o0_forbid_patterns` |
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
| R58 | 换容器之前在生产库一致快照副本上先跑一次初始化，确认以 0 结束并通过；写明回滚（回退镜像，恢复迁移前备份） | wac-011-r3 🟡-1；看板 M11 | P-06（可选） | 阶段 W build（副本演练）、apply（迁移前在线备份）、rollback、restore-db |
| R59 | 数据库初始化失败路径只写容器日志，用户收不到告警：wac-015 补告警，或 O-0 巡检按容器重启次数告警 | wac-011-r3 🟡-2；看板 M11 | P-07 | D-07；阶段 W verify 检查 90 秒内重启计数为 0 |
| R60 | 启动测试防真实会话 `config.json` 造成双活（同类风险用于 O-0 演练） | wac-011-r3 🟡-3 | — | S-07 要求构建上下文无 `config.json`；演练容器 `--network none` 且挂空配置 |
| R61 | 新开 C-0 校准任务，作为 O-0 数据基线和开关打开的前置 | wac-013 🟡-6 | P-09 done | — |
| R62 | C-0 `_safe_id` 拒绝首尾空白 id（整份快照 invalid），基线补上这一项 | wac-013 💭-3 | — | 基线 `surrounding_whitespace_id` |
| R63 | 生成第三份产物 Caddy 路径清单 | wac-016 🟡-5 | done（wac-026，`be76e92`） | — |
| R64 | 确认 jp-24 `.venv-cp` 解释器 ≥ 3.11，否则网关每个请求 500（进程能起来） | wac-016 🟡-8；wac-016-r2 §7 第 2 条 | P-13 | S-12；阶段 O preflight |
| R65 | 门禁除校验脚本外也跑 P2 断言测试，或核对 `phase_max=P2` | wac-016-r2 §7 第 1 条 | — | G2（脚本已强制 P2，wac-026）+ G9 跑两份 P2 断言测试 |
| R66 | 不把 `{param}` 原样贴进 Caddyfile（未知占位符替换成空串） | wac-026 🟡-1 (a) | — | `render` 用 `[^/]+`；清单含 `{` 即拒绝 |
| R67 | 每行用 `path`+`method` 具名匹配器；媒体行 HEAD 显式列出 | wac-026 🟡-1 (b) | — | `render`；verify 比对方法集合 |
| R68 | Caddy `*` 在末尾时是前缀匹配、会跨段：改用锚定 `path_regexp`，或接受前缀并写明"Caddy 只做粗筛，精确边界在网关" | wac-026 🟡-1 (c) | P-12 | 默认锚定；`--accept-prefix` 仅在 Planner 选 D-03 (b) 时使用 |
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
| R77 | 部署脚本 `set -eo pipefail`；部署前后 sha 校验并与基线比对 | 协议；记忆"部署脚本两条铁律"（`cmd \| tail` 吞退出码） | 所有脚本 `set -Eeo pipefail`；manifest-verify 前后各一次；管道里不用 `grep -q`（SIGPIPE） |
| R78 | 控制面代码落盘后必须重启对应 systemd 服务才生效 | `AGENTS.md` | 阶段 O 安装后立即只重启 operator-query |
| R79 | node-control 重启会全舰队 HALT；只改 operator-query 时不要动它 | 协议；记忆 | 阶段 O verify 断言另两个单元 MainPID 与启动时间不变 |
| R80 | Caddy 只 `validate` 后 `restart`，禁止 reload；validate 要带 `v3.env` 且不能用 shell `source`（bcrypt 的 `$` 会被展开） | 协议；记忆 jp24 caddy 条目 | `o0_tool.py env-exec` 无展开加载；脚本自检拒绝 reload 字样 |
| R81 | 任何 Caddy restart 可能瞬断节点通道导致全舰队 fail-closed HALT（08-30 发生，08-31 未发生） | 记忆；`INTEGRATION_REPORT.md` §7 | D-02；阶段 C 记录并对比舰队状态，变化即停，不 RESUME |
| R82 | 门禁全部完成于停机之前；门禁失败即中止并保持原版本 | `AGENTS.md` | 每阶段 preflight 只读；apply 失败自动回滚 |
| R83 | 不采信转述的"已完成"，查审计与心跳 | `AGENTS.md`；协议 | 每阶段前后心跳快照 |
| R84 | 线上 read_api 有热补丁漂移史，部署前务必与基线 sha 核对 | 记忆（09-02、09-10、09-22 热挂载） | 阶段 O preflight `controlplane-context.baseline.sha256`，漂移即中止 |
| R85 | 三个控制面服务都跑 `read_api:app`，共用代码目录 | 记忆 09-24 | D-04；import 冒烟覆盖三种角色 |
| R86 | watcher 重启是信号采集空窗；重启后要核对有无漏信号 | `docs/agent-operations.md` §1；记忆 09-04/09-25 | 阶段 W 选低流量窗口；verify 看 Telegram 连接与入库 |
| R87 | 凭据零接触：不打印、不提交、不写日志 | 协议铁律 3 | 工具只输出变量名；`tests/site_check_leak_test.sh` 用 9 个哨兵值证明现场脚本 0 泄露 |

## 6. 本地已实跑的证据（全部在本机，未连 jp-24）

| 命令 | 结果 |
|---|---|
| `bash -n` 全部 7 个 shell 脚本；`python3 -m py_compile` 全部 4 个 Python 工具 | 全部通过 |
| `python3 scripts/ops/o0/o0_watcher_credentials.py selftest` | `SELFTEST_OK scenarios=13 values_checked_for_leak=19` |
| `python3 scripts/ops/o0/o0_caddy_watcher_routes.py selftest` | `SELFTEST_OK good_passes=72 variants_caught=14/14 lines=16 accept_prefix_mode=ok before_deploy_mode=ok` |
| `python3 scripts/ops/o0/o0_watcher_config_baseline.py selftest` | `SELFTEST_OK number_cases=12 ... bad_rules=5 bad_items=8` |
| JS 数字格式交叉核对：3008 个浮点数，`String(Number(x))`（node 24）对比 `js_number_string` | `mismatches 0` |
| 基线工具的 `content_sha256` 对比 W-0b `59e5a59` 的 `makeSnapshot`（同一库 43 行，含 Unicode 与怪异浮点） | 两边都是 `626e2059…1dfb42` |
| `bash scripts/ops/o0/tests/site_check_leak_test.sh`（桩化 docker/systemctl/caddy/curl 等，9 个哨兵秘密） | `LEAK_TEST_OK sections=19 sentinels=9 leaks=0`（首轮抓到一个真泄露：`sed ... \| sort < file` 的重定向绑到了 sort，已修） |
| `bash scripts/ops/o0/o0_package.sh --candidate integ/watcher-app-crew --report-only --run-tests`（候选 `4fb0c11`） | G1、G2（`ROUTES_DIFF_EMPTY rows=63 … phase_max=P2`）、G3、G8、G9（pytest 24 passed；watcher tests 77 / fail 0 / skipped 0）、G10 通过；**G4、G5、G6、G7 失败 → `deploy_candidate: false`**，与 P-01/P-02/P-04/P-05 一致 |
