# O-0 待用户授权清单（wac-032 草案，wac-045 修订，供 Planner 转交用户）

> 规则：每一项都要用户**逐项**明确授权后才执行。任何 Agent 的转述都不算授权。上一项的验证没有通过，就不申请下一项。
> **授权号与脚本阶段绑定**（审查 wac-032 🔴-2）：每个脚本阶段只接受下表"脚本阶段"列里写明的那一个授权号，别的号（包括格式正确的号）一律拒绝；绑定表在 `scripts/ops/o0/o0_common.sh` 的 `o0_expected_auth`，`tests/auth_gate_test.sh` 逐条核对表中组合与三份 runbook 里的每一条示例命令。执行记录写入 `evidence/authorizations.log`：时间、脚本、阶段、授权号、候选提交、是否带 `--i-understand-data-loss`。
> 所有项都**不包含 RESUME**。部署前后节点保持原授权状态：HALTED 保持 HALTED。某一步之后舰队有任何变化（状态、release、`/ready`、心跳冻结），脚本以退出码 3 停下并报告；是否恢复交易只由用户决定（O0-A06）。
> 编号 O0-A09、O0-A19 保留未用（验证并入 A08，清理并入 A18）。
> 目前**不能**开始申请生产授权：打包门禁 G4（`lib/config-store.js` 不在镜像白名单）、G7（wac-015 未合入）未通过，见 `o0-requirements.md` §1 与 §6。本清单先给出完整顺序，让用户提前看到每一步的影响。

## 一、决策状态

| # | 事项 | 状态 | 说明 |
|---|---|---|---|
| D-01 | W-0b 第 3 次 FAIL 的处置 | **已定（用户 2026-09-26）**：方案 A | 第 4 轮小修 wac-043 进行中；W-0b 已合入集成分支 `f7641cb`，门禁 G6 通过 |
| D-02 | 接受 Caddy restart 可能让全舰队 HALT，并选定窗口 | **待用户在上线前确认** | 推荐：用户在场、手机能收告警、信号稀少的窗口；若 A01 时舰队本来就是 HALTED，就在那个窗口做。所有 Caddy 变更合并为一次 restart。若 HALT 发生，恢复只由用户发起，不用 `resume_race.py` 的默认 intent 修复（09-10 裸仓教训） <!-- o0-allow --> |
| D-03 | Caddy 路径清单里 `*` 的翻译方式 | **已定（用户 2026-09-26）**：严格"恰好一个非空段"，锚定、区分大小写的 `path_regexp`（`[^/]+`） | 写入下次勘误 WGW-1.0.2（P-12）。`--accept-prefix` 已从核对工具删除；前缀写法、`(?i)`、`[^/]*`、`.+`、缺 `$` 都判失败 |
| D-04 | 接受"共享代码目录"的混合版本状态 | **待用户在上线前确认** | 推荐接受，附两个条件：S-10 确认三个单元确实共用目录；在运维文档与记忆中登记"下一次 node-control / event-ingest 重启即加载新 `read_api.py`"，把那次重启当作一次部署看待 |
| D-05 | compose `env_file` 与镜像白名单 | **Planner 已定**：由 wac-040 实现（已合入 `a881802`） | 白名单还缺 W-0b 之后的 `lib/config-store.js`（G4 失败），需 Planner 另行派发或并入 wac-015 |
| D-06 | C-0 逐 worker 预热日志 | **Planner 已定**：wac-041（`c9b1e3f`，审查中） | 切换门禁以"每个 worker 一行 `snapshot_warmup result=success`"为证据（切换 runbook §3.2.3，`o0_tool.py warmup-check`） |
| D-07 | 数据库初始化失败的告警 | **Planner 已定**：wac-015 在代码中复用 `sendWatcherAlert` | O-0 保留阶段 W 的 90 秒重启计数断言作兜底 |
| D-08 | 计划 §4.2 两段暂停实测在哪里做 | **Planner 已定**：只在隔离环境做 | 生产实测 O0-A12 仅在用户额外要求时 |

## 二、需要现场核对确认的参数（O0-A01 的产出，不是新动作）

| 参数 | 草案默认值 | 由哪一条确认 | 不一致时 |
|---|---|---|---|
| `O0_FLEET_NODES`（舰队守卫的精确节点集合） | `account-a account-b account-c account-d` | S-00 的节点 id | 按 S-00 设置环境变量后再跑任何阶段；节点集合不符时守卫判 UNCOMPARABLE 并拒绝开始 |
| `O0_FLEET_KNOWN_DOWN`（用户事先声明停着的节点） | 空 | S-00 + 用户确认 | 心跳冻结的节点若未声明，守卫拒绝开始（冻结 = 节点死了，不是"没变化"） |
| 节点通道地址（阶段 C 探针 r7） | `172.30.1.1:8080` | S-06 的 `:8080` 监听地址 | 用 `--node-channel <地址>` |
| 舰队守卫采样窗 | restart 后等 60 秒，再 4 次、每 20 秒一次（共约 2 分钟） | 用户确认可接受（节点 fail-closed 判定时长未在仓库中找到记载） | 调 `O0_FLEET_SETTLE_S` / `O0_FLEET_SAMPLES` / `O0_FLEET_INTERVAL_S` |
| watcher 根目录、compose 镜像名 | `/srv/trader`、`trader-watcher` | S-05（`config_image`、挂载）、S-07 | `--watcher-root`、`--compose-image` |
| 控制面 token 目录文件 | `operator-query.env` | S-10/S-11 列出的全部 EnvironmentFiles（含 `NAUTILUS_NODE_AUTH_JSON` 所在文件） | 每个文件一个 `--catalog-env` |
| 主机 `sync_operator_risk_db.py` 版本 | bundle 里的那份 | S-09（`--expect-sync-sha256`） | 不一致就不在主机上执行它，改用 bundle 的副本 |
| 离线构建用的 Python | `/srv/trader-v3/.venv-cp/bin/python` ≥ 3.11 | S-12 | `--builder-python` |

## 三、生产动作（按执行顺序）

| 授权 ID | 脚本阶段（只接受这个号） | 动作（一句话） | 影响 | 回滚方式 | 前置 |
|---|---|---|---|---|---|
| **O0-A01** | `o0_site_check.sh --execute` | 在 jp-24 跑只读现场核对（S-00…S-18），并从本机发 6 个不带凭据的公网 GET（L-P1…L-P6） | 只读；不在主机写文件；Caddy 完整配置**不离开主机**，只带回白名单骨架与主机上算出的"运行配置是否等于文件"结论；主机上的 `sync_operator_risk_db.py` 只有 sha 等于已审版本才以 `--check` 运行 | 无需回滚 | 无 |
| **O0-A02** | （人工命令，runbook §2.2） | 建 `/srv/trader-staging/o0-<UTC>`（0700），上传 bundle 并校验 sha | 只写 staging | 删除该目录 | A01 无阻断项；本地门禁全 PASS |
| **O0-A03** | （人工命令，凭据 runbook I-1、I-2） | 在 staging 生成三组 watcher 凭据并校验（含与控制面 token 目录按摘要比对） | 只写 staging；不输出任何值 | 删除凭据集目录 | A02 |
| **O0-A04** | `o0_deploy_watcher.sh`：`preflight`、`build` | watcher 只读门禁；用仓库离线白名单构建器（以**运行中镜像**为固定基础、只叠加已审文件、`--network=none`）构建候选镜像；列出运行中镜像里白名单之外的文件供审阅；真库一致副本；在副本上跑首次启动迁移（`--network none`、一次性假凭据、空 Telegram 配置、正常停止）；写 `watcher-build` 门禁 | 新增一个镜像、两份库副本；运行中的 watcher、真库、Caddy、控制面都不变 | `docker rmi` 候选镜像，删除 staging 副本 | A03；S-07 无 `config.json`；S-12 Python ≥ 3.11；S-14 磁盘够 |
| **O0-A05** | `o0_deploy_caddy.sh`：`preflight`、`apply`、`verify`、`rollback` | 阶段 C：Caddy 加浏览器清头与注入、加 16 条 `/m/v1/watcher/*` 锚定单段路由；apply 先核对 preflight 门禁文件（同一候选 Caddyfile、同一线上文件、同一凭据），装好后对**已安装文件**再 adapt 与核对一遍，再 `validate` 与 `systemctl restart caddy`（不 reload）；失败自动回滚 | **可能让全舰队 fail-closed HALT**（D-02）；公网站点瞬断；`/m/v1/watcher/*` 从 SPA 变为控制面 404 | 自动/手动：还原 Caddyfile 与 v3.env 备份、校验 sha、validate、restart | A04；D-02 已确认 |
| O0-A06 | （条件项，不是部署步骤） | 若某阶段后节点状态变化，是否以及何时恢复交易 | 只属于用户。Release Steward 不写恢复命令，只提供守卫的前后对比证据 | — | — |
| **O0-A07** | `o0_deploy_watcher.sh`：`apply-preflight`、`apply`、`verify`、`rollback` | 阶段 W：上线前重跑只读门禁（门禁有效期 1 小时）；apply 先核对两份门禁文件并确认**已测试的候选镜像存在且 ID 一致**，之后才备份、写 env_file、装源码与 compose（各自装后 sha 校验）、用该镜像重建 watcher；启动、90 秒重启计数、**Telegram 重新连上**（180 秒内）都在自动回滚窗口内断言 | watcher 重建期间 Telegram 采集中断约 1 分钟（可能漏信号，事后按 §1 核对，只报告不补单）；真库新增 `config_revision`、`config_audit`；浏览器站点从此要求 Caddy 注入的凭据 | 自动/手动：旧镜像 + 旧源码 + 旧 compose + env_file 回到 apply 前状态后重建；新增两张表保留（旧代码忽略） | A05 已验证；`MIGRATION_DRYRUN_OK`；S-15 无 `db_manager.py` 写入者；用户已审阅"运行中镜像里白名单之外的文件"清单 |
| O0-A07R | `o0_deploy_watcher.sh`：`restore-db`，另须 `--i-understand-data-loss` | （仅在迁移破坏数据时）把真库换回 A07 前的在线备份；先断言快照开关为 0、备份 sha 与 `integrity_check` 正确 | **破坏性**：A07 之后写入的配置、审计与 Telegram 消息会丢失 | 被换下的库文件留在备份目录，可再换回 | 用户单独决定；O0-A07 的号**不能**执行这一步 |
| **O0-A08** | `o0_deploy_operator_query.sh`：`preflight`、`apply`、`verify`、`rollback` | 阶段 O：operator-query env 加入 `WATCHER_GATEWAY_TOKEN`、`WATCHER_SNAPSHOT_TOKEN`（开关保持 0），安装五个文件（装后 sha 校验），**只重启 operator-query**；在自动回滚窗口内用 `SYSTEM_OBSERVER_TOKEN`（进程内读取，只打印状态码）经网关打一次新 watcher，期望 200 | app 与运维查询端点重启约数秒；网关上线；node-control 与 event-ingest 不重启，但磁盘上已是新代码（D-04） | 自动/手动：还原文件与 env（sha 校验）、只重启 operator-query | A07 已验证；S-12；S-13 无漂移；D-04 已确认 |
| O0-A10 | — | 真机联调 A-0（手机或 Xiaomi Pad 9 Pro Max） | 只在用户说"现在可以用"时进行 | — | A08 |
| O0-A11 | `o0_fleet_guard.sh`：`drill-R-2`、`drill-R-3`、`drill-R-5` | （可选）gateway 凭据轮换演练：R-1 → R-2 → R-3 → R-4 → R-5，每一步前后舰队守卫 | watcher 重建两次；operator-query 重启一次 | 每一步各有回滚（凭据 runbook §3） | A08 |
| O0-A12 | — | （可选，默认在隔离环境做）生产上分两段暂停 watcher 容器实测 | 暂停期间无信号采集 | `docker unpause` | 开关打开之后；D-08 |
| O0-A13 | `o0_fleet_guard.sh`：`SW-2` | 快照切换第 2 步：真库一致副本、数据基线、与 watcher 快照互证、A/B 漂移；只含非秘密列的库交隔离环境做 B/C 对照 | 只读真库与副本；只写 staging | 删除 staging 文件 | A08 稳定运行；P-02、P-11 |
| O0-A14 | `o0_fleet_guard.sh`：`SW-3`、`SW-5` | 快照切换第 3 步：开关置 1，只重启 operator-query，**每个 worker 一行预热成功日志**且 revision ≥ R0（`warmup-check`）；失败自动回滚到"回退时经校验的真库一致快照" | 此后开仓依赖快照：watcher 故障 60 秒后开仓返回 `snapshot_unavailable`（设计行为） | 关开关、重建并校验副本、只改开关与别名键、只重启 operator-query | A13 结论"可以切换"；wac-041 已合入并上线 |
| O0-A15 | `o0_fleet_guard.sh`：`SW-smoke` | 切换前后的只读与 dry_run 冒烟，输入由用户指定 | dry_run 不提交订单 | 无 | 与 A14 同窗口 |
| O0-A16 | `o0_fleet_guard.sh`：`SW-drill` | 失败恢复演练（推荐隔离环境） | 生产演练 = 两次 operator-query 重启 | 演练本身就是回滚路径 | A14 |
| O0-A17 | `o0_fleet_guard.sh`：`SW-4` | 快照切换第 4 步：删 env 三个 DB 别名；停用同步 timer（若启用）；副本**移动**到 30 天备份目录；只重启 operator-query | 旧 reader 从此没有数据源 | 恢复这几个键、把副本移回、恢复 timer、只重启 operator-query | A15、A16 通过；部署 runbook C-3、W-4 通过 |
| O0-A18 | — | （≥ 30 天后）删除副本备份与 staging 中含凭据的 env 备份、凭据集目录 | 不可逆 | 无 | A17 满 30 天且无回滚需求 |
| O0-A20 … A23 | `o0_fleet_guard.sh`：`R-1`（A20）、`R-2`（A21）、`R-3`/`R-4`（A22）、`R-5`（A23） | 以后任何一次例行凭据轮换：A20 生成新值、A21 watcher 接受新旧、A22 持有方切新值并观察、A23 撤旧值 | 见凭据 runbook §3；browser 的持有方一步要重启 Caddy，带 D-02 的风险 | 每一步都有该步回滚 | 用户发起 |

## 四、交给用户时附带的材料

1. `o0-requirements.md`：要求清单、前置、审查 wac-032 的逐条处置、本地实跑证据。
2. `o0-site-checklist.md`：A01 要跑的每一条命令、预期、不符时的处置。
3. `o0-runbook-deploy.md`、`o0-runbook-credentials.md`、`o0-runbook-snapshot-switch.md`。
4. `scripts/ops/o0/` 脚本草案（默认只打印计划；执行需 --execute、--phase 与该阶段绑定的授权号），测试在 `scripts/ops/o0/tests/`。
