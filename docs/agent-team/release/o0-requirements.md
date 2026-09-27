# O-0 发行要求可追溯清单（wac-032 草案，wac-045 修订，wac-059 第 3 轮，wac-072 加固，wac-060 适配 WGW-1.0.2，wac-090 收紧 Caddy 核对）

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
| R04 | "operator-query 缺 `gateway` 时只禁用网关路由并告警，不得让交易端点启动失败（上线前配置校验锁定）" | 计划 §2.1、§4.2 | C-1 R11 已实现 | 阶段 O preflight import 冒烟断言 `_LOAD_ERROR is None`；生产不做缺 token 负例 |
| R05 | "轮换用 `*_TOKEN` + `*_TOKEN_PREVIOUS` 双值，顺序为：watcher 先接受新旧两值，网关切到新值，确认请求与审计正常，再撤旧值" | 计划 §2.1；契约 §9.2 | P-02 | 凭据 runbook §3 |
| R06 | "确认 import 与 handler 顺序" | 计划 §9 O-0；契约 F-12、F-13 | — | S-02（含全局 `order` 与前置指令）、S-03/S-04 + `verify --before-deploy`（前瞻遮蔽检查）；阶段 C：`caddyfile-check`（片段文件全局 import、站点顶层 import 在所有 handle 之前）、F-13 (2) 人工记录、`verify` 的两步遮蔽检查（第一条 wgw 路由之前与外层每一条路由）、本机 Caddy 探针（O0-A05P） |
| R07 | "`/m` 只 strip 一次" | 计划 §9 O-0 | — | verify：每条样本恰好一次 `strip_path_prefix /m`；L-probe |
| R08 | "上游仍是 8183" | 计划 §9 O-0 | — | verify：dial 恰为 `127.0.0.1:8183`；S-06 |
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

见提交说明与本节下方的证据补记（本节数字在代码提交之后补入）。

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
