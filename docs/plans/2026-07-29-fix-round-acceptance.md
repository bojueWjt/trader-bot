# 2026-07-29 修复轮验收报告（对照 07-28 元复核）

- 验收对象：`work/hermes-data-v3` 上 `2273c0c..2f3f53a` 共 21 个提交（约 7200 行）+ smartness-p0 新 4 提交 + 各 codex/* worktree
- 验收方式：3 个只读 review agent 按域并行（monitor / report+recorder / 部署+控制面），全部实跑测试；对照 `docs/plans/2026-07-28-freeze-audit-metareview.md` 的可开工项与红线
- **07-29 判定：主树提交质量整体过关、可合入；TP 策略侧维持发布冻结。**
- **07-30 终态：阻断项已在提交 `0702863 fix(trading): enforce authorized order management` 收敛并部署生产；LIMIT→MARKET_IF_TOUCHED 改造未进入发布物料。**

> **⚠️ 2026-08-03 勘误（依据 docs/plans/2026-08-03-five-day-work-review.md）**：上行"LIMIT→MARKET_IF_TOUCHED 未进入发布物料"与仓库/线上事实不符。MIT 改造由 `cba1dab`（07-29 21:44）进入主线，`0702863` 是其后裔（`git show 0702863:services/nautilus-node/strategy/intent_execution_planner.py` :598 即 `MARKET_IF_TOUCHED`），线上 container-patches 策略文件 md5 与主树一致——**MIT 自 07-30 起在生产运行**。§二.2"保持冻结"、§四"继续等待拒绝原因取证"两处同此勘误。07-31 BZUSDT TP 连拒 9 次（-2021）及后续 30.5h 裸仓即 MIT 上线后的直接后果，详见五日审计报告第一节。

---

## 〇、2026-07-30 生产收敛与部署验收

### 代码与回归

- 发布提交：`0702863c2f25c993e65fded17a11347ff79730a5`
- 订单管理、TP tombstone、live mirror：`182 passed, 3 skipped`
- 报表与部署：`60 passed`
- 控制面安全：`35 passed`
- deployment fail-closed shell：PASS
- 发布包：22 个文件，`SHA256SUMS` 全量校验通过

### 收敛内容

1. 用户订单授权由已认证 `risk_admin`、`X-Request-Id/client_ref`、`control-plane` 生成；频道授权继续要求真实频道归属证据。
2. 授权证据统一写入 `raw_messages.raw_payload`、`hermes_decisions.evidence`、`trade_intents.order_plan`、`outbox_events.payload`、`audit_events.payload`。
3. dry-run 执行完整授权与归属校验，且不写 attribution shadow 日志；同幂等键更换授权证据返回 `409`。
4. 管理动作显式匹配账户、instrument、position side、intent 与系统订单标签；外部/手工订单保持不可自动撤销。
5. `disable-tps` 先持久化 `cancel_pending`，镜像恢复后自动重试撤销；重启后 tombstone 继续生效。
6. 报表仅消费 ≤180 秒的新鲜 `exchange_state_mirror`；空镜像与 stale 镜像 fail-closed，停止回退旧 projection。
7. 两节点 clean recreate 显式挂载 14 个只读补丁；`lifecycle.py`、`binance_adapter_config.py`、`nautilus_actors.py` 已统一进入 `container-patches`。

### 生产部署

- 目标：HK `100.104.27.123`
- 回滚备份：`/srv/trader-v3/backups/deploy-20260730T024925Z-0702863`
- 部署窗口：节点全程保持 HALTED；文件、数据库、容器 inspect、服务日志、MU/BTC 订单现场均已备份
- RESUME 命令：`e59f14d8-cc9b-4693-8d9b-dfbbeccd9d6b`
- ACK：`nautilus-node-account-a=acked`、`nautilus-node-account-b=acked`
- 终态：8081/8082 均 `ready=true`、`trading_state=ACTIVE`；control-plane、exchange-state、report、lifecycle、feeder 均 active

### 业务验收

| 项目 | 生产结果 |
|---|---|
| 账户下单链路 | account-a/account-b 均完成 Binance API key 认证、交易权限确认；`recv_window_ms=30000` 生效；控制面 dry-run 均成功 |
| BTC 自动止盈禁用 | 用户授权 intent `3748d8aa-dc2b-4637-96f5-ee98ea27c2e7` 已执行；tombstone=`disabled`，`pending_cancel_ids=[]` |
| BTC 重挂观察 | RESUME 后完整镜像周期内系统 TP 数量为 0 |
| BTC 外部 TP | `aos_rVDgj2VD2dMS8p4HefHO` 无系统 intent/projection 归属，作为外部/手工单保留 |
| MUUSDT | 交易所镜像为零持仓、零订单 |
| 日报 | `trade_count=0` 与源表 0 一致；当前持仓 5 与镜像 5 一致；`missing_data=[]` |
| 周报 | `trade_count=4` 与源表 4 一致；当前持仓 5 与镜像 5 一致；`missing_data=[]` |
| 报表依赖 | database、trade_outcomes、exchange_state_mirror 全部 `ok`；镜像年龄 37 秒，阈值 180 秒 |
| 授权证据 | BTC disable-tps 的 raw、decision、intent、outbox、audit 五层字段完全一致 |

部署过程出现两次验收脚本误报：macOS `._*` 元数据进入 staging，以及挂载计数表达式错误。两次均触发 fail-closed，节点保持 HALTED；修正验收命令后完成 clean recreate 与 RESUME。

---

## 一、验收通过项（对照元复核可开工项）

| 元复核条目 | 落点 | 判定 |
|---|---|---|
| PendingCancel 48h 误淘汰 still_open | 2b3da96 + 636eb14：超窗未解决台账持续参与镜像判定，无淘汰时钟；stale/查询失败均保留；3 条专项测试 | **PASS** |
| TTL/naked prompt 补 --account/--side | monitor `:999-1000, :1315-1316`，另注入完整溯源参数；account-b 场景测试断言 | **PASS** |
| 报表 freshness gate（禁用 computed_at） | ac5a1f5 + 594484c：新表 `trade_outcome_job_runs`（migration 0009）做 job-run 水位，仅成功置 completed_at；stale → 503 不出报告 | **PASS**（正确避开已知陷阱） |
| mirror 查询失败进 missing_data | report_service.py:626-640，原字面量 `[]` 缺陷已除 | **PASS** |
| BINANCE_EXEC_DST fail-closed | 4c508e6：探测失败/为空/非绝对路径/容器内不存在四路全 FATAL；shell+py 测试锁定 | **PASS** |
| 预检清单补全 contracts/projection_actor/binance_execution | hk-root-window-20260724.sh:20-32，逐文件删缺测试 | **PASS** |
| event_mapper 补 position_side + mark_price | smartness-p0 `0ffd5e6`（此前我误报"未做"，实际已在 worktree 完成），20260729 部署 bundle 含 event_mapper | **PASS** |
| recorder 补 markPrice | `.live-mirror/tools/exchange_state_recorder.py:191`；报表直读 mirror，链路已通 | **PASS**（但见双实现风险） |
| 回滚/审计窗口质量 | 20260729 三脚本 set -Eeuo + trap + flock、备份先于变更、SHA 校验、DB 事务重放、终态 HALTED；红线检查无明文密码（stdin 管入、0600 EnvironmentFile） | **PASS** |
| 控制面 equity/sizing（deea888/127f6ea） | 只认 ≤180s 新鲜投影、stale 不回退 env、来源打标 + hotfix 脚本验证条件闭环，14 测试 | **PASS** |
| 红线：不碰策略 reduce_only | 主树 diff 零策略文件改动 | **PASS** |

## 二、07-29 阻断项的 07-30 处置结果

1. **半应用 worktree 已隔离**：`.rej` 与未提交 worktree 文件均未进入发布包；最终 planner/strategy 改动以 `0702863` 的已提交版本为唯一来源。
2. **LIMIT→MARKET_IF_TOUCHED 保持冻结**：该方案未进入 `0702863`，生产继续保留原订单类型语义。
3. **disable-tps 生产者/消费者已收敛**：read API、CLI、planner、strategy 同批发布；生产已验证 tombstone 持久化、重启恢复和镜像周期内零系统 TP 重挂。
4. **tp-live-combined worktree 已排除**：其未提交文件未进入部署物料。

## 三、遗留修补项（不阻塞合入，需排期）

1. **strategy 发布真身已收编**：`services/nautilus-node/strategy/intent_execution_strategy.py` 与 planner 已进入提交 `0702863`；发布包和线上 `container-patches` SHA256 已逐文件对齐。
2. **recorder 双实现已分叉**：`.live-mirror/tools/` 版与 smartness-p0 版的 accounts_projection 写法语义不同；部署 bundle 靠人工组装、SHA 自指校验，无 repo→bundle 出处 pin。建议 bundle 组装脚本化。
3. **缺日报常驻告警未实现**：healthz 是被动状态，Hermes 不调 POST /reports 时无人知晓（7/21、7/24 场景会重演）。
4. **cron 死因未取证**：20260729 脚本直接 `rm /etc/cron.d/...` 迁 systemd，无诊断步骤。缓解项已到位（日志落 /var/log 避开 journald 盲区、DATABASE_URL 走 0600 文件），但建议执行前后抓一次 syslog CRON 记录，否则死因永远无解。
5. 存量 intent 无授权字段：授权 gating 上线后，早于写入器的 intent 会拒绝派生管理动作。生产启动时已观察到两条 `protection_parent_intent_missing`，说明 fail-closed 生效；仍需 backfill 或人工清理 runbook。
6. 小项：`prune_state` 不清 `authalert:*` 键（状态文件单调涨）；`_position_parent_authorizations` 全史扫描无窗口 + blocked-naked 每 15s 重扫 + 可能认错父 intent；watermark 不校验报表窗口覆盖；root-window 版 watermark 比较未同步 continuation 的严格版；20260729 脚本零文档、batch-deploy 手册仍指旧流程；测试 venv 缺 python-multipart；IN-list 拼接建议补 hex 校验。

## 四、07-30 最终结论

`0702863` 已通过代码回归、生产 fail-closed 部署、双账户认证/dry-run、报表源数据对账、BTC TP tombstone、五层授权证据和双节点 RESUME ACK 验收。发布闸门已开放并完成生产部署。

后续排期聚焦四项：缺日报主动告警、recorder 双实现收敛、legacy intent 授权 backfill/runbook、0005 migration 历史漂移治理。LIMIT→MARKET_IF_TOUCHED 继续等待拒绝原因取证。
