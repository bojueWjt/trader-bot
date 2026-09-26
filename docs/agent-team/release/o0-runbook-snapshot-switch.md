# O-0 快照切换 runbook：数据基线、影子三路对照、切换、副本移除与回滚（wac-032 草案）

> 草案，未执行。对应计划 §4.1 第 2–4 步与回滚条款；契约 §9.10、§9.11（E-01、E-14）；审查 wac-001 🟡-9/💭-2/💭-3、wac-011 第 6 点、wac-011-r2 第 6 点、wac-013 💭-3、wac-022.md（wac-023）结论。
> 本 runbook 只在 `o0-runbook-deploy.md` 三个阶段全部完成并稳定之后才开始。全程不写控制面库、不碰记账五表、不新增控制面迁移、不发 RESUME、不下单。

## 0. 前置（全部满足才申请 O0-A13 之后的授权）

| 前置 | 状态 | 说明 |
|---|---|---|
| 部署 runbook 阶段 C、W、O 完成，快照开关为 0 | 未开始 | — |
| P-02：W-0 集成（含 `db_manager.py` 直写停用）已上线 | pending | 绕过 revision 的写入者会造成"同 revision 异摘要"，C-0 锁存 `invalid` 后全部开仓被拒 |
| P-10：C-0 在每个 worker 首次成功验证与进入锁存时记一条 `(pid, revision, content_sha256, snapshot_state)` 日志 | 未派 | 计划第 3 步要求"所有 operator-query worker 预热成功并记录 revision"。C-0 现实现只在开关打开时启动预热（`start_if_enabled`），也没有逐 worker 的可观测输出；没有 P-10，只能用 dry_run 抽样间接判断，不能证明"所有 worker" |
| P-11：T0-6 三路对照工具与报告，B/C 等价 | 未派 | 计划 §4.2 对照条款：覆盖四账户、全部路由、全部配置品种、异常状态 |
| 数据基线无阻断项，或每一项都有用户裁决 | 未做 | §2.1 |
| 失败恢复演练通过（计划第 4 步前置之一） | 未做 | 建议在隔离环境演练；生产演练是 O0-A16 |
| P-17：jp-24 上有可用的 `sync_operator_risk_db.py`（回滚要用） | 未核（S-09） | bundle 的 `tools/` 附带同一份脚本 |

## 1. 名词

- **A / B / C**（计划 §4.1 第 2 步）：A = 旧 reader + 旧副本；B = 旧 reader + 真库一致快照；C = 新 reader + 同一真库的 HTTP 快照。A/B 差异是既有漂移；**B/C 必须等价**，只允许契约 §9.11 列明的有意差异。
- **真库一致快照**：用 SQLite 在线备份 API 从 `/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db` 拷出的副本（源以只读打开，一次读事务内完成），满足：`PRAGMA integrity_check = ok`；且用 `o0_watcher_config_baseline.py` 在副本上重算的 `content_sha256`，等于同一时刻 watcher 快照端点返回的 `content_sha256`。两者不等或无法比较（副本本身不合规、watcher 不可达）都**不算**"经校验"。
- **经校验的回退数据**：回退那一刻按上面方法生成并校验过的数据，写成副本文件。**不是** `/srv/trader-v3/state/operator-query-risk/trading-risk.db` 的 09-06 旧文件，也不是 30 天备份（计划 §4.1 回滚条款）。

## 2. 第 2 步：数据基线与影子三路对照

| 步 | 授权 | 操作（`$S` = `/srv/trader-staging/o0-<UTC>`，`$T` = `$S/bundle/tools`） | 预期 | 不符时 |
|---|---|---|---|---|
| 2.1 真库一致副本 + 基线 | O0-A13 | `python3 $T/o0_tool.py sqlite-backup --source <真库> --dest $S/switch/real.copy.db`；`python3 $T/o0_watcher_config_baseline.py baseline --db $S/switch/real.copy.db --export-json $S/switch/baseline.json` | `integrity=ok`；逐条列出 `VIOLATION`（watcher 会拒绝发布的行）与 `BASELINE`（E-14 六类 + 首尾空白 id + CHECK 违例 + 网关不可寻址 id）；无违例时给出 `content_sha256=` | 有 `VIOLATION`：快照会 500 `snapshot_invalid`，开关打开后全部开仓被拒。**阻断**，由用户在站点或经网关改正数据（新 revision、带审计），再重做 2.1。有 `BLOCKING` 基线项：同样阻断，逐项请用户裁决 |
| 2.2 与 watcher 快照互证 | O0-A13 | `python3 $T/o0_tool.py http-probe --url http://127.0.0.1:9090/api/trading/config-snapshot --token-env-file /srv/trader-v3/secrets/control-plane/operator-query.env --token-var WATCHER_SNAPSHOT_TOKEN --expect-status 200 --show-json-field revision --show-json-field content_sha256`；再对副本 `baseline --expect-content-sha256 <上面的值>` | 两边 `content_sha256` 相等；同时证明 operator-query 持有的 snapshot 值被 watcher 接受 | 不等：2.1 与 2.2 之间有写入，重做；反复不等说明有绕过 revision 的写入者，**阻断**（查 S-15） |
| 2.3 既有漂移（A/B） | O0-A13 | `python3 /srv/trader-v3/scripts/sync_operator_risk_db.py --check --source $S/switch/real.copy.db --target /srv/trader-v3/state/operator-query-risk/trading-risk.db`（只读，退出 3 = 有差异） | 差异摘要（只含非秘密列）；记为 A/B 既有漂移 | — |
| 2.4 隔离环境对照（B/C） | 不连生产 | 只有**非秘密**数据离开 jp-24：`python3 $T/sync_operator_risk_db.py --source $S/switch/real.copy.db --target $S/switch/nonsecret.db --owner root:root --mode 0600` 生成只含白名单列的库，连同 `baseline.json` 交给 T0-6 执行者。隔离环境里：B = 旧 reader 读 `nonsecret.db`；C = 隔离 watcher 用同一数据（`api_key`/`api_secret` 列 NOT NULL，用夹具假值补齐）经 HTTP 快照喂新 reader。固定输入 `real_equity, available_balance, entry, stop_loss, caps, account_id, source_channel, client_ref, dry_run` 对照 `account_equity_basis`、`risk_sizing`、路由目标、`revision` | T0-6 报告：B/C 在成功值、错误码、拒因上等价；有意差异（宽松 `account_type`/启用值、`default_risk` 为 NULL → 503、首尾空白 id）与 2.1 的清单逐条对上 | 不等价：**阻断**，回到 C-0 执行者 |
| 2.5 结论 | — | Release Steward 汇总 2.1–2.4，交 Planner 转用户 | 用户签字"可以切换" | — |

## 3. 第 3 步：切换

### 3.1 切换前门禁（只读，O0-A14 授权内，任何一项失败即中止）

1. 舰队快照（`fleet-before-switch`），记录三个控制面单元 MainPID。
2. S-15 复核：没有 `db_manager.py` 写入者；watcher 是 W-0 集成后的版本。
3. 重做 2.1 + 2.2（数据会变，门禁用当时的数据）：副本无违例、无阻断项，`content_sha256` 与 watcher 快照相等。把这时的 `revision` 与 `content_sha256` 记为 **R0**——这就是计划"预热成功并记录 revision"在进程外能做到的部分：证明 operator-query 手里的 snapshot 值能拿到一份有效快照。
4. 切换前 dry_run 基线（O0-A15 授权）：用用户指定的固定输入（账户、频道、品种、entry、stop_loss）调 `open_position` 的 dry_run 与一次 `close` 的 dry_run，保存响应里的 `risk_sizing`、路由目标与拒因（只保存这些字段）。**dry_run 不提交订单**；即便如此，输入由用户给出。

### 3.2 打开开关（O0-A14）

| 步 | 操作 | 验证 | 失败即 |
|---|---|---|---|
| 3.2.1 | 备份 operator-query env；用 `o0_watcher_credentials.py apply` 同样的方式合入一行 `WATCHER_CONFIG_SNAPSHOT_ENABLED=1`（片段文件 0600，不含凭据） | `apply` 输出 `replace`/`add WATCHER_CONFIG_SNAPSHOT_ENABLED` | 还原备份 |
| 3.2.2 | 只重启 `trader-v3-controlplane-operator-query` | 60 秒内 `/v1/accounts` 无 token 401；node-control、event-ingest MainPID 不变 | §5 回滚 |
| 3.2.3 | 逐 worker 确认预热：有 P-10 时，日志里 `pid` 去重数等于 S-10 记录的 worker 数，且每个都是 `fresh` 并且 `revision >= R0`；没有 P-10 时，只能对 `open_position` dry_run 连续抽样（例如 worker 数 × 5 次），每次响应的 `checks` 都含 `{"name": "config_snapshot", "passed": true, "snapshot_state": "fresh"}`，并在报告里写明"未能逐 worker 证明" | 全部 fresh | §5 回滚 |
| 3.2.4 | 冒烟（O0-A15）：只读端点；`close` dry_run 与切换前一致（非开仓不依赖快照）；`open_position` dry_run 用 3.1 第 4 步的同一输入，`risk_sizing` 与路由目标与切换前一致（除非这期间配置本身变了，以 `revision` 为证），证据里有 `revision`、`content_sha256`、`age_ms` | 一致 | §5 回滚 |
| 3.2.5 | 舰队快照并比较 | `FLEET_STATUS_UNCHANGED` | 停止，报用户 |

切换后持续观察一小时：operator-query 日志里没有 `snapshot_revision_regressed`、`invalid`、`unauthorized`；watcher 停一下会让开仓在 60 秒后返回 `snapshot_unavailable`，这是设计行为（计划 §2.1 影响范围），不为此放宽 `max_age`。

## 4. 第 4 步：移除副本（O0-A17）与 30 天备份（O0-A18）

**进入条件**（计划原文，三条全满足）：外部路由白名单与浏览器回归通过（部署 runbook C-3、W-4）；生产只读/dry_run 冒烟通过（3.2.4）；失败恢复演练通过（O0-A16，推荐在隔离环境做，生产演练就是完整走一遍 §5 再切回 §3.2）。

| 步 | 操作 | 验证 | 回滚 |
|---|---|---|---|
| 4.1 | 备份 operator-query env；删除三个别名 `TRADER_TRADING_DB_PATH`、`WATCHER_TRADING_DB`、`TRADING_DB_PATH`（`apply --remove`，片段只列这三个键） | `APPLY remove` 三行 | 还原 env 备份 |
| 4.2 | 若 S-09 发现 `trader-v3-operator-risk-db-sync.timer` 已启用：先 `systemctl disable --now` 它（否则会把副本写回来）。这是额外的授权点，写进 O0-A17 | timer inactive | `systemctl enable --now` |
| 4.3 | 把副本**移动**（不删除）到 `/srv/trader-v3/backups/o0-replica-<UTC>/trading-risk.db`，目录 0700、文件 0600，旁边写 `DELETE_AFTER=<UTC + 30 天>` | 原路径不存在；备份 sha 记入 evidence | 移回并恢复 `root:trader-v3-cp-operator-query 0640` |
| 4.4 | 只重启 operator-query；重复 3.2.3–3.2.5 | 开仓 dry_run 仍 fresh；旧 reader 已无数据源（开关若被误关，会 fail-closed，这是预期） | §5 |
| 4.5（≥ 30 天后） | O0-A18：删除 `o0-replica-<UTC>` 备份与 staging 中含凭据的 env 备份 | — | 不可逆，单独授权 |

## 5. 回滚：关开关，回到"回退时经校验的真库一致快照"

触发：§3/§4 任一验证失败；开仓出现非预期的 `snapshot_unavailable`；用户要求。授权：切换阶段的 O0-A14 包含自动回滚；独立回滚按 O0-A14 同等授权。

| 步 | 操作 | 验证 |
|---|---|---|
| 5.1 生成回退数据 | `python3 /srv/trader-v3/scripts/sync_operator_risk_db.py --source <真库> --target /srv/trader-v3/state/operator-query-risk/trading-risk.db`（源在一个读事务里读取，只写白名单列，原子替换，属主与权限 `root:trader-v3-cp-operator-query 0640`）。副本已在 §4 移走时，这一步会重建它 | 紧接着 `--check` 输出 `unchanged` |
| 5.2 校验回退数据 | `python3 $T/o0_watcher_config_baseline.py baseline --db /srv/trader-v3/state/operator-query-risk/trading-risk.db --expect-content-sha256 <此刻 watcher 快照的 content_sha256>`（2.2 的探针） | `PASS content_sha256 equals the expected (live snapshot) value`。watcher 不可达时结果是"无法比较"：记录，由用户决定是否在未互证的情况下继续（它仍然比 09-06 文件新） |
| 5.3 恢复 env | 还原 operator-query env 备份：`WATCHER_CONFIG_SNAPSHOT_ENABLED` 回到 0 或删除；若 §4 已删别名，别名随备份一起恢复 | `sed -n` 只看这四个键 |
| 5.4 重启即清缓存 | 只重启 operator-query（C-0 缓存在进程内，重启即清空） | `/v1/accounts` 401；node-control、event-ingest MainPID 不变 |
| 5.5 验证 | `open_position` dry_run 的 `checks` 不再含 `config_snapshot`（走旧 reader）；同一输入的 `risk_sizing` 与路由目标和 3.1 第 4 步一致（或能用期间的配置变化解释）；舰队快照比较 | 一致；`FLEET_STATUS_UNCHANGED` |

回滚相关的硬规则：
- **开关开着时不要回滚 watcher 到 W-0b 之前的版本**。旧版每次启动都跑迁移、站点写入不涨 revision，会产生"同 revision 异摘要"，C-0 锁存 `invalid` 并拒绝全部开仓（wac-011 第 6 点）。顺序永远是：先 §5 关开关，再按部署 runbook W-5 回滚 watcher。
- watcher 数据库被恢复到旧备份（部署 runbook W-6）时，`revision` 会变小，C-0 记 `snapshot_revision_regressed`（契约 §9.11），这是预期告警；开关开着时同样先关开关。
- 以后若 watcher 快照 schema 升级，发布顺序是先控制面、后 watcher（wac-007 三条解读之 1：控制面不认识新 schema 时会立即判 `invalid` 拒开仓）。
