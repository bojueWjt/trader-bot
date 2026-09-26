# O-0 待用户授权清单（wac-032 草案，供 Planner 转交用户）

> 规则：每一项都要用户**逐项**明确授权后才执行；执行时脚本必须带 `--auth-id O0-Axx`，写入 `evidence/authorizations.log`。任何 Agent 的转述都不算授权。上一项的验证没有通过，就不申请下一项。
> 所有项都**不包含 RESUME**。部署前后节点保持原授权状态：HALTED 保持 HALTED。若某一步让 ACTIVE 节点变成 HALTED，脚本停下并报告，是否恢复交易只由用户决定（O0-A06）。
> 编号 O0-A09、O0-A19 保留未用（验证并入 A08，清理并入 A18）。
> 目前**不能**开始申请：打包门禁 G4、G5、G6、G7 未通过（前置 P-01、P-02、P-04、P-05），见 `o0-requirements.md` §1。本清单先给出完整顺序，让用户提前看到每一步的影响。

## 一、先要用户或 Planner 拍板的事（不是生产动作）

| # | 需要决定的事 | 选项与推荐 | 为什么现在要定 |
|---|---|---|---|
| D-01 | W-0b 第 3 次 FAIL 的处置 | 审查推荐方案 A：授权第 4 轮微修（只改启动测试的 env，补三个夹具 token），合并状态下全量绿再合入 | W-0b 不合入，集成分支不能成为部署候选（wac-009 🟡-7，门禁 G6） |
| D-02 | 接受 Caddy restart 可能让全舰队 HALT，并选定窗口 | 推荐：在用户在场、信号稀少的窗口做阶段 C；若 HALT 发生，由用户自己决定何时、怎样 RESUME | 这是阶段 C 的已知后果（08-30 发生过，08-31 没有），不能算"意外 HALT"，必须事先接受 |
| D-03 | Caddy 路径清单里 `*` 的翻译方式 | 推荐 (a)：锚定 `path_regexp` 恰好一段（本草案默认）；(b) 接受 Caddy 前缀语义并注明"Caddy 只做粗筛，精确边界在网关" | 决定阶段 C 的 Caddyfile 写法与 verify 参数；同时写进 WGW-1.0.2 勘误（P-12） |
| D-04 | 接受"共享代码目录"的混合版本状态 | 推荐接受：阶段 O 只重启 operator-query，node-control 与 event-ingest 下次重启才加载新 `read_api.py`，O-1 已证明新代码对这两个角色可加载且不注册网关路由。替代方案是给 operator-query 独立代码目录（要改 systemd 单元） | 不重启 node-control 是铁律（会 HALT 全舰队），这个混合状态无法避免 |
| D-05 | 派发 P-04（compose `env_file` 接线）与 P-05（镜像白名单 + 闭包测试） | 推荐并入 wac-015 或新开一个小任务 | 门禁 G4、G5 |
| D-06 | 派发 P-10（C-0 逐 worker 预热/锁存日志） | 推荐新开小任务，在打开快照开关之前完成 | 否则"所有 worker 预热成功并记录 revision"无法证明 |
| D-07 | 数据库初始化失败的告警（wac-011-r3 🟡-2） | (a) wac-015 在该退出路径复用 `sendWatcherAlert`（推荐）；(b) O-0 巡检按容器重启次数告警 | 否则迁移失败只在容器日志里，用户手机收不到 |
| D-08 | 计划 §4.2 的两段暂停实测在哪里做 | 推荐 Tester 在隔离环境做；生产实测（O0-A12）只在用户想要额外证据时做 | 生产暂停 watcher 就是信号空窗 |

## 二、生产动作（按执行顺序）

| 授权 ID | 动作（一句话） | 影响 | 回滚方式 | 前置 |
|---|---|---|---|---|
| **O0-A01** | 在 jp-24 跑只读现场核对脚本 `o0_site_check.sh`（S-00…S-18），并从本机发 6 个不带凭据的公网 GET（L-P1…L-P6） | 只读；不在主机写文件；会以只读方式打开真库、副本和 Caddy admin API；会运行 `sync_operator_risk_db.py --check`（只读比对） | 无需回滚 | 无 |
| **O0-A02** | 建 `/srv/trader-staging/o0-<UTC>`（0700），上传 bundle 并校验 sha | 只写 staging | 删除该目录 | A01 无阻断项；本地门禁全 PASS |
| **O0-A03** | 在 staging 生成三组 watcher 凭据并做校验（含与控制面 token 目录按摘要比对） | 只写 staging；校验时在进程内读取控制面 env 文件，不输出任何值 | 删除凭据集目录 | A02 |
| **O0-A04** | 在 staging 拼构建上下文、`docker build` 候选 watcher 镜像；用备份 API 做真库一致副本；在副本上跑一次首次启动迁移（`--network none`，一次性假凭据，空 Telegram 配置） | 新增一个镜像和两份库副本；运行中的 watcher、真库、Caddy、控制面都不变；构建占 CPU 与磁盘 | `docker rmi` 候选镜像，删除 staging 副本 | A03；S-07 无 `config.json`；S-14 磁盘够 |
| **O0-A05** | 阶段 C：Caddy 加浏览器清头与注入、加 16 条 `/m/v1/watcher/*` 逐路径路由，`validate` 后 `systemctl restart caddy`（不 reload）；验证失败自动回滚 | **可能让全舰队 fail-closed HALT**（D-02）；公网站点瞬断；`/m/v1/watcher/*` 从 SPA 变为控制面 404 | 自动/手动：还原 Caddyfile 与 v3.env 备份、校验 sha、validate、restart | A04；D-02、D-03 已定 |
| O0-A06 | （条件项，不是部署步骤）若某阶段后节点状态变化，是否以及何时恢复交易 | 只属于用户。Release Steward 不写 RESUME 命令，只提供状态对比证据。提醒：2026-09-10 的教训表明 `resume_race.py` 默认的 intent 修复会触发旧意向重放，历史上造成过裸仓 | — | — |
| **O0-A07** | 阶段 W：备份（镜像打标签、源码、compose、env_file、真库在线备份），写入 watcher env_file，安装候选源码与 compose，用**已演练过的**镜像重建 watcher；验证失败自动回滚 | watcher 重建期间 Telegram 采集中断约 1 分钟（可能漏信号，事后核对，只报告不补单）；真库新增 `config_revision`、`config_audit` 两张表（首次迁移）；浏览器站点从此要求 Caddy 注入的凭据 | 自动/手动：旧镜像 + 旧源码 + 旧 compose 重建；新增的两张表保留（旧代码忽略） | A05 已验证；A04 的 `MIGRATION_DRYRUN_OK`；S-15 无 `db_manager.py` 写入者 |
| O0-A07R | （仅在迁移破坏数据时）把真库换回 A07 前的在线备份 | **破坏性**：A07 之后写入的配置、审计与 Telegram 消息会丢失 | 被换下的库文件留在备份目录，可再换回 | 用户单独决定 |
| **O0-A08** | 阶段 O：operator-query env 加入 `WATCHER_GATEWAY_TOKEN`、`WATCHER_SNAPSHOT_TOKEN`（快照开关保持 0），安装五个文件，**只重启 operator-query**；验证时在进程内读取 `SYSTEM_OBSERVER_TOKEN` 发只读 GET（只打印状态码）；失败自动回滚 | app 与运维查询端点重启约数秒；网关 `/v1/watcher/*` 上线；node-control 与 event-ingest 不重启，但磁盘上已是新代码（D-04） | 自动/手动：还原文件与 env、只重启 operator-query | A07 已验证；S-12 Python ≥ 3.11；S-13 无漂移；D-04 已定 |
| O0-A10 | 真机联调 A-0（手机或 Xiaomi Pad 9 Pro Max） | 只在用户说"现在可以用"时进行；不在用户正在用的设备上注入输入 | — | A08 |
| O0-A11 | （可选）gateway 凭据轮换演练：R-1 生成 → R-2 watcher 接受新旧并重建 → R-3 operator-query 切新值并重启 → R-4 观察 → R-5 撤旧并重建 watcher | watcher 重建两次（两次采集空窗）；operator-query 重启一次 | 每一步各有回滚（见凭据 runbook §3） | A08 |
| O0-A12 | （可选，推荐隔离环境代替）生产上分两段暂停 watcher 容器（10 秒、61 秒）实测隔离 | 暂停期间无信号采集；61 秒那段只有快照开关打开后才有意义 | `docker unpause` | 开关打开之后；D-08 |
| O0-A13 | 快照切换第 2 步：真库一致副本、数据基线、与 watcher 快照互证、`sync_operator_risk_db.py --check` 查 A/B 漂移；生成只含非秘密列的库交隔离环境做 B/C 对照 | 只读真库与副本；只写 staging；只有非秘密数据离开 jp-24 | 删除 staging 文件 | A08 稳定运行；P-02、P-11 |
| O0-A14 | 快照切换第 3 步：operator-query env 设 `WATCHER_CONFIG_SNAPSHOT_ENABLED=1`，只重启 operator-query，逐 worker 确认 fresh；失败自动回滚（关开关、回到经校验的真库一致快照） | 此后开仓依赖快照：watcher 故障 60 秒后开仓返回 `snapshot_unavailable`（设计行为，不放宽） | 关开关、重建并校验副本、恢复 env、只重启 operator-query | A13 结论"可以切换"；P-10（或用户接受抽样证明） |
| O0-A15 | 切换前后的只读与 dry_run 冒烟：`close` 与 `open_position` 的 dry_run，输入由用户指定 | dry_run 不提交订单，不改仓位；会写控制面的 dry_run 证据（与现有 dry_run 相同） | 无 | 与 A14 同窗口 |
| O0-A16 | 失败恢复演练（推荐隔离环境；生产上即完整走一遍回滚再切回） | 生产演练 = 两次 operator-query 重启 | 演练本身就是回滚路径 | A14 |
| O0-A17 | 快照切换第 4 步：删除 operator-query env 里的三个 DB 别名；若同步 timer 已启用则停用；把副本**移动**到 30 天备份目录；只重启 operator-query | 旧 reader 从此没有数据源（开关若被误关会 fail-closed） | 恢复 env 备份、把副本移回、恢复 timer、只重启 operator-query（或按切换 runbook §5 重建经校验的副本） | A15、A16 通过；部署 runbook C-3、W-4 通过 |
| O0-A18 | （≥ 30 天后）删除副本备份与 staging 中含凭据的 env 备份、凭据集目录 | 不可逆 | 无 | A17 满 30 天且无回滚需求 |
| O0-A20 … A23 | 以后任何一次例行凭据轮换：A20 生成新值、A21 watcher 接受新旧、A22 持有方切新值并观察、A23 撤旧值 | 见凭据 runbook §3；browser 的持有方一步要重启 Caddy，带 D-02 的风险 | 每一步都有该步回滚 | 用户发起 |

## 三、交给用户时附带的材料

1. `o0-requirements.md`：87 条要求、17 个前置、本地实跑证据。
2. `o0-site-checklist.md`：A01 要跑的每一条命令、预期、不符时的处置。
3. `o0-runbook-deploy.md`、`o0-runbook-credentials.md`、`o0-runbook-snapshot-switch.md`。
4. `scripts/ops/o0/` 脚本草案（默认只打印计划；执行需 `--execute --auth-id`）。
