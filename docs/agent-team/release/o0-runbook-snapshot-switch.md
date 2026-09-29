# O-0 快照切换 runbook：数据基线、影子三路对照、切换、副本移除与回滚（wac-032 草案，wac-045 修订）

> **WGW-1.0.4 之后的前提变化（wac-109，审查 wac-108 🟡-2；以此段为准）**：阶段 O 现在只新增 watcher-gateway 单元，**不升级 operator-query 的代码（C-0 快照 reader、`snapshot_warmup` 日志），也不往 `operator-query.env` 写 `WATCHER_SNAPSHOT_TOKEN`**（契约 §9.14.6）。生产 operator-query 仍是 `67b401a`，没有快照 reader。因此本 runbook（O0-A13 … O0-A17）**要等以后单独的控制面整体升级**：那次升级把 C-0 代码装进 operator-query 实际加载的目录、写入 snapshot token、重启 operator-query，有自己的契约、门禁、窗口与用户授权。WGW-1.0.4 的 O0-A08 不提供这些，**完成 O0-A08 不等于可以开始本 runbook**。下文凡写"随阶段 O 上线""阶段 O 写入"的，一律改读为"随那次控制面整体升级上线"。在此之前本 runbook 的门禁（每个 worker 一行预热成功日志、`$OQ` 中的 snapshot token）会 fail-closed 地挡住，不会误开开关。

> 草案，未执行。对应计划 §4.1 第 2–4 步与回滚条款；契约 §9.10、§9.11（E-01、E-14）；审查 wac-001 🟡-9/💭-2/💭-3、wac-011 第 6 点、wac-011-r2 第 6 点、wac-013 💭-3、wac-022.md（wac-023）结论、wac-032 🟡-6/🟡-7/🟡-8。
> 本 runbook 只在 `o0-runbook-deploy.md` 三个阶段全部完成并稳定之后才开始。全程不写控制面库、不碰记账五表、不新增控制面迁移、不发 RESUME、不下单。
> 记号：`$S` = `/srv/trader-staging/o0-<UTC>`，`$T` = `$S/bundle/tools`，`$DB` = `/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db`（真库），`$REP` = `/srv/trader-v3/state/operator-query-risk/trading-risk.db`（副本），`$OQ` = `/srv/trader-v3/secrets/control-plane/operator-query.env`。
> **每一步前后都跑舰队守卫**（`o0_fleet_guard.sh`，步骤名与授权号绑定：SW-2 = O0-A13、SW-3 与 SW-5 = O0-A14、SW-smoke = O0-A15、SW-drill = O0-A16、SW-4 = O0-A17）。`--action before` 在节点集合不对或心跳 ≥ 5 秒时拒绝开始；`--action after` 等稳定窗后多次采样，有变化即退出码 3：停下、报用户、不 RESUME。回滚前用 `--action record`（只记录，不设门槛）。

## 0. 前置（全部满足才申请 O0-A13 之后的授权）

| 前置 | 状态 | 说明 |
|---|---|---|
| 部署 runbook 阶段 C、W、O 完成，快照开关为 0；**另立的控制面整体升级已上线**（operator-query 加载 C-0 代码、`$OQ` 含 `WATCHER_SNAPSHOT_TOKEN`；WGW-1.0.4 的阶段 O 不做这两件事） | 未开始 | wac-109：审查 wac-108 🟡-2 |
| P-02：W-0 集成（含 `db_manager.py` 直写停用）已上线 | pending（wac-015） | 绕过 revision 的写入者会造成"同 revision 异摘要"，C-0 锁存 `invalid` 后全部开仓被拒 |
| P-10：C-0 每个进程启动时写一行 `snapshot_warmup result=… revision=… content_sha256=<前 12 位> pid=… role=operator-query`，状态迁移写 `snapshot_state_transition` | wac-041（`c9b1e3f`）审查中，**须合入并随以后的控制面整体升级上线**（WGW-1.0.4 的阶段 O 不上线它） | 切换门禁 3.2.3 以"每个 worker 一行预热成功日志"为证据（Planner 决定）；没有它不能打开开关 |
| P-11：T0-6 三路对照工具与报告，B/C 等价 | 未派 | 计划 §4.2 对照条款 |
| 数据基线无阻断项，或每一项都有用户裁决 | 未做 | §2.1 |
| 失败恢复演练通过（计划第 4 步前置之一） | 未做 | 推荐隔离环境；生产演练是 O0-A16 |
| P-17：回滚要用的 `sync_operator_risk_db.py` 可用且为已审版本 | 未核（S-09 按 sha 核对） | 主机版本 sha 不符时用 `$T/sync_operator_risk_db.py` |

## 1. 名词

- **A / B / C**（计划 §4.1 第 2 步）：A = 旧 reader + 旧副本；B = 旧 reader + 真库一致快照；C = 新 reader + 同一真库的 HTTP 快照。A/B 差异是既有漂移；**B/C 必须等价**。
- **真库一致快照**：用 `o0_tool.py sqlite-backup`（SQLite 在线备份 API，源以只读打开，读得到 WAL；副本切成回滚日志模式）从 `$DB` 拷出的副本，满足 `integrity_check = ok`，且基线工具在副本上重算的 `content_sha256` 等于同一时刻 watcher 快照端点返回的 `content_sha256`。基线工具拒绝分析带未检查点 WAL 的文件（审查 wac-032 🔴-4）。
- **经校验的回退数据**：回退那一刻按上面方法生成并校验过的数据，写成副本文件。**不是** `$REP` 的 09-06 旧文件，也不是 30 天备份（计划 §4.1 回滚条款）。

## 2. 第 2 步：数据基线与影子三路对照（O0-A13，守卫步骤名 SW-2）

| 步 | 操作 | 预期 | 不符时 |
|---|---|---|---|
| 2.0 | `bash $T/o0_fleet_guard.sh --execute --phase SW-2 --auth-id O0-A13 --stage-dir $S --action before` | `FLEET_BASELINE_OK` | 不开始 |
| 2.1 真库一致副本 + 基线 | `python3 $T/o0_tool.py sqlite-backup --source $DB --dest $S/switch/real.copy.db`；`python3 $T/o0_watcher_config_baseline.py baseline --db $S/switch/real.copy.db --export-json $S/switch/baseline.json` | `integrity=ok`；逐条列出 `VIOLATION` 与 `BASELINE`；无违例时给出 `content_sha256=` | 有 `VIOLATION` 或 `BLOCKING`：**阻断**，由用户在站点或经网关改正数据（新 revision、带审计），再重做 2.1 |
| 2.2 与 watcher 快照互证 | `python3 $T/o0_tool.py http-probe --url http://127.0.0.1:9090/api/trading/config-snapshot --token-env-file $OQ --token-var WATCHER_SNAPSHOT_TOKEN --expect-status 200 --show-json-field revision --show-json-field content_sha256`；再 `baseline --db $S/switch/real.copy.db --expect-content-sha256 <上面的值>` | 两边相等；同时证明 operator-query 持有的 snapshot 值被 watcher 接受 | 不等：重做；反复不等说明有绕过 revision 的写入者，**阻断**（查 S-15） |
| 2.3 既有漂移（A/B） | `python3 $T/sync_operator_risk_db.py --check --source $S/switch/real.copy.db --target $REP`（只读，退出 3 = 有差异） | 差异摘要（只含非秘密列） | — |
| 2.4 隔离环境对照（B/C） | 只有**非秘密**数据离开 jp-24：`python3 $T/sync_operator_risk_db.py --source $S/switch/real.copy.db --target $S/switch/nonsecret.db --owner root:root --mode 0600`，连同 `baseline.json` 交 T0-6 执行者 | T0-6 报告：B/C 等价；有意差异与 2.1 清单逐条对上 | 不等价：**阻断** |
| 2.5 | `bash $T/o0_fleet_guard.sh --execute --phase SW-2 --auth-id O0-A13 --stage-dir $S --action after`；Release Steward 汇总 2.1–2.4 交 Planner 转用户 | `FLEET_UNCHANGED_ALL_SAMPLES`；用户签字"可以切换" | — |

## 3. 第 3 步：切换（O0-A14，守卫步骤名 SW-3；冒烟 O0-A15，SW-smoke）

### 3.1 切换前门禁（只读，任何一项失败即中止）

1. `bash $T/o0_fleet_guard.sh --execute --phase SW-3 --auth-id O0-A14 --stage-dir $S --action before`；记录三个控制面单元 MainPID；从 S-10 的 `ExecStart` 取 worker 数 `N`。
2. S-15 复核：没有 `db_manager.py` 写入者；watcher 是 W-0 集成后的版本。
3. 重做 2.1 + 2.2：副本无违例、无阻断项，`content_sha256` 与 watcher 快照相等。记为 **R0**（`revision` 与 `content_sha256`）。
4. 切换前 dry_run 基线（O0-A15，前后跑 `bash $T/o0_fleet_guard.sh --execute --phase SW-smoke --auth-id O0-A15 --stage-dir $S --action before` 与 `--action after` 守卫）：用户指定的固定输入调 `open_position` 与 `close` 的 dry_run，保存 `risk_sizing`、路由目标与拒因。
5. 备份 operator-query env：`cp -p $OQ $S/switch/operator-query.env.pre-switch`；`sha256sum $OQ > $S/evidence/oq-env-pre-switch.sha256`。

### 3.2 打开开关

| 步 | 操作 | 验证 | 失败即 |
|---|---|---|---|
| 3.2.1 | 片段文件 `$S/switch/switch-on.env`（0600，只有一行 `WATCHER_CONFIG_SNAPSHOT_ENABLED=1`）；`python3 $T/o0_watcher_credentials.py apply --fragment $S/switch/switch-on.env --target $OQ --execute --backup-dir $S/switch/env-apply-on` | `APPLY replace` 或 `APPLY add WATCHER_CONFIG_SNAPSHOT_ENABLED` | §5 |
| 3.2.2 | `date +%s > $S/evidence/switch-restart.epoch`；只重启 `trader-v3-controlplane-operator-query` | 60 秒内 `/v1/accounts` 无 token 401；node-control、event-ingest MainPID 不变 | §5 |
| 3.2.3 预热证据（每个 worker 一行） | `journalctl -u trader-v3-controlplane-operator-query --since "@$(cat $S/evidence/switch-restart.epoch)" --no-pager > $S/evidence/switch-journal.txt`；`python3 $T/o0_tool.py warmup-check --journal $S/evidence/switch-journal.txt --workers N --min-revision <R0.revision> --r0-content-sha256 <R0.content_sha256>` | `WARMUP_OK workers=N`：恰好 N 个不同 pid，每个 `result=success`，`revision ≥ R0`，同 revision 时摘要前缀相同；同一时段没有 `snapshot_state_transition … to=invalid/unauthorized` | 任何一个 worker 缺行、`cold`、多出 pid、revision 倒退：§5（不以抽样代替） |
| 3.2.4 冒烟（O0-A15） | 只读端点；`close` dry_run 与切换前一致；`open_position` dry_run 用 3.1 第 4 步的同一输入，`risk_sizing` 与路由目标一致（除非期间配置变了，以 `revision` 为证），证据里有 `revision`、`content_sha256`、`age_ms` | 一致 | §5 |
| 3.2.5 | `bash $T/o0_fleet_guard.sh --execute --phase SW-3 --auth-id O0-A14 --stage-dir $S --action after` | `FLEET_UNCHANGED_ALL_SAMPLES` | 停止，报用户 |

切换后持续观察一小时：operator-query 日志里没有 `snapshot_revision_regressed`、`to=invalid`、`to=unauthorized`；watcher 停一下会让开仓在 60 秒后返回 `snapshot_unavailable`，这是设计行为，不为此放宽 `max_age`。

## 4. 第 4 步：移除副本（O0-A17，守卫步骤名 SW-4）与 30 天备份（O0-A18）

**进入条件**（计划原文，三条全满足）：外部路由白名单与浏览器回归通过（部署 runbook C-3、W-4）；生产只读/dry_run 冒烟通过（3.2.4）；失败恢复演练通过（O0-A16，守卫步骤名 SW-drill；推荐在隔离环境做）。

| 步 | 操作 | 验证 | 回滚 |
|---|---|---|---|
| 4.0 | `bash $T/o0_fleet_guard.sh --execute --phase SW-4 --auth-id O0-A17 --stage-dir $S --action before`；`cp -p $OQ $S/switch/operator-query.env.pre-remove`；把三个别名当前的**路径值**（非秘密）记入 `$S/evidence/oq-db-aliases.txt` | — | — |
| 4.1 | 片段 `$S/switch/aliases.env` 只列三个键（值任意，`--remove` 只用键名）；`python3 $T/o0_watcher_credentials.py apply --fragment $S/switch/aliases.env --target $OQ --remove --execute --backup-dir $S/switch/env-apply-remove` | `APPLY remove` 三行 | 片段 `aliases.restore.env`（三个键 = 4.0 记录的路径）`apply` 回去 |
| 4.2 | 若 S-09 发现 `trader-v3-operator-risk-db-sync.timer` 已启用：`systemctl disable --now` 它（O0-A17 的一部分） | timer inactive | `systemctl enable --now` |
| 4.3 | 把副本**移动**到 `/srv/trader-v3/backups/o0-replica-<UTC>/trading-risk.db`（目录 0700、文件 0600），旁边写 `DELETE_AFTER=<UTC + 30 天>`；sha 记入 evidence | 原路径不存在 | 移回并恢复 `root:trader-v3-cp-operator-query 0640` |
| 4.4 | 只重启 operator-query；重复 3.2.3（`warmup-check`）与 3.2.4；`bash $T/o0_fleet_guard.sh --execute --phase SW-4 --auth-id O0-A17 --stage-dir $S --action after` | 全部 worker 预热成功；开仓 dry_run 仍 fresh；守卫不变 | §5 |
| 4.5（≥ 30 天后） | O0-A18：删除 `o0-replica-<UTC>` 与 staging 中含凭据的 env 备份 | — | 不可逆，单独授权 |

## 5. 回滚：关开关，回到"回退时经校验的真库一致快照"（O0-A14，守卫步骤名 SW-5）

触发：§3/§4 任一验证失败；开仓出现非预期的 `snapshot_unavailable`；用户要求。

| 步 | 操作 | 验证 |
|---|---|---|
| 5.0 | `bash $T/o0_fleet_guard.sh --execute --phase SW-5 --auth-id O0-A14 --stage-dir $S --action record`（只记录，不挡回滚） | 样本已写入 |
| 5.1 生成回退数据 | 同一时刻做两件事：`python3 $T/o0_tool.py sqlite-backup --source $DB --dest $S/switch/rollback.real.copy.db`，以及 `python3 $T/sync_operator_risk_db.py --source $DB --target $REP`（源在一个读事务里读取，只写白名单列，原子替换，属主与权限 `root:trader-v3-cp-operator-query 0640`；副本已在 §4 移走时这一步会重建它） | `sqlite-backup … integrity=ok`；紧接着 `sync_operator_risk_db.py --check --source $DB --target $REP` 输出 unchanged |
| 5.2 校验回退数据（默认路径，不依赖 watcher） | `baseline --db $S/switch/rollback.real.copy.db` 得到 `content_sha256`（记为 X）；再用 `sqlite-backup --source $REP --dest $S/switch/rollback.replica.copy.db` 拷出副本的一致拷贝并 `baseline --db … --expect-content-sha256 X`；两份导出三表摘要相同 | `PASS content_sha256 equals the expected …`：副本与同一窗口的真库一致。若 watcher 可达，再加 2.2 的快照探针作第三方互证（可选）。只有这条默认路径也"无法比较"（真库副本本身有违例、不可读）时才上报用户，由用户决定 |
| 5.3 只恢复该改的键 | 片段 `switch-off.env`（`WATCHER_CONFIG_SNAPSHOT_ENABLED=0`）`apply` 到 `$OQ`；若 §4 已删别名，再 `apply` `aliases.restore.env`（4.0 记录的三个路径）。**不整份还原 env 备份**：切换之后若做过凭据轮换，整份还原会把 gateway/snapshot 值退回已撤销的旧值（网关 503） | `sed -n` 只看这四个键；`o0_watcher_credentials.py check --watcher-env /srv/trader-secrets/watcher-gateway.env --holder-env $OQ $CAT --require-catalog` 仍通过 |
| 5.4 重启即清缓存 | 只重启 operator-query（C-0 缓存在进程内，重启即清空） | `/v1/accounts` 401；node-control、event-ingest MainPID 不变 |
| 5.5 验证 | `open_position` dry_run 的 `checks` 不再含 `config_snapshot`（走旧 reader）；同一输入的 `risk_sizing` 与路由目标和 3.1 第 4 步一致（或能用期间的配置变化解释）；`bash $T/o0_fleet_guard.sh --execute --phase SW-5 --auth-id O0-A14 --stage-dir $S --action after` | 一致；`FLEET_UNCHANGED_ALL_SAMPLES`（与 5.0 的记录比较） |

回滚相关的硬规则：
- **开关开着时不要回滚 watcher 到 W-0b 之前的版本**（wac-011 第 6 点）。顺序永远是：先 §5 关开关，再按部署 runbook W-5 回滚 watcher。
- watcher 数据库被恢复到旧备份（部署 runbook W-6）时 `revision` 会变小，C-0 记 `snapshot_revision_regressed`；W-6 本身会先断言开关为 0。
- 以后若 watcher 快照 schema 升级，发布顺序是先控制面、后 watcher（wac-007 三条解读之 1）。
- 本 runbook 的步骤仍是手工命令序列加守卫；把它们包成带自动回滚的脚本（`o0_snapshot_switch.sh`）列为待办，理由见 `o0-requirements.md` §7（🟡-6）。
