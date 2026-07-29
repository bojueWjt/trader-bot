# 窗口 C 验收执行计划（合并后逐项跑，证据→翻 gate）

> 每个场景产出可复跑命令 + 四层证据（交易所/Nautilus event/PostgreSQL projection/Dashboard）+ commit SHA，写回对应 `release-gate.json` 条目。
> 实现脚本（harness/chaos/testnet runner/migration）交 Codex；C 定义场景、判据、证据格式并验收。

## 执行顺序（依赖见 TaskList）

C-00 合并 → C-01 wiring → (C-02 迁移 ∥ C-03 corpus) → C-04 harness → C-05 回放 → C-06 面板对账 → C-07 chaos → C-08 testnet → C-09 kill-switch → C-10 安全 → C-11 切换/回滚 → C-12 报告+gate。

## C-04/C-05 真实图文回放（→ G7 G8 S1 S2 S4 S6 S7）

路径（**禁用 importer 旁路**）：真实 watcher/ingress → Postgres raw → 真 Hermes（HK gateway） → Gateway → Nautilus sandbox → projection → Dashboard。
- 3 次干净库回放 + 2 次同库重复回放；edit/乱序/延迟媒体/重复 update 都要覆盖。
- 判据：四类安全指标 S1/S2/S4/S6=0、S7=100%、G7 无重复订单、G8 全通过。
- 稳定性：三次中 message_type/action/instrument/side 必须稳定；不稳样本强制 needs_review，记残余风险。
- 产物：`REPLAY_ACCEPTANCE_REPORT.md` + `metrics.json` + JUnit/HTML。

## C-06 Dashboard 真数据逐层对账（→ G3 G4 S8 S9）

随机 ≥20 时间点比对：Nautilus/venue→event→Postgres→API→UI 的账户/订单/成交/持仓/PnL/风险/trace 一致。
- 生产 bundle 扫描：无 FakeAdapter/fixture/固定账户值（G3）。
- 调用图：Dashboard/Hermes 只调 control-plane API；API 只读 PG（G4）。
- stale / node missing 正确显示并阻断新增风险。
- 产物：`DASHBOARD_REAL_DATA_REPORT.md` + 截图 + JSON 快照。

## C-07 故障与混沌（→ G2 S3 S11）

覆盖：Hermes timeout/5xx/invalid-JSON/schema-mismatch；图片丢失/损坏/超时/mime 不符；Postgres 短暂不可用；Redis 重启清空；node 重启；Binance WS 断连；startup/continuous reconciliation；部分成交/撤单拒绝/订单拒绝；重复 fill/乱序 event；control-plane↔node 断网；projection spool 补发；kill switch 单节点未 ack；SSE 断流恢复；source ts 过期但 ingest 新；edited 覆盖旧语义；同币两账户不同 routing。
- 判据：所有故障期间新增风险命令 = 0（fail closed）；恢复后无重复订单/事件。
- 产物：`CHAOS_REPORT.md` + 故障时间线日志。

## C-08 Binance testnet 全场景（→ G6 G9 S5）

14 场景：limit 开 / market 开 / 部分成交后撤 / 未成交超时 / stop-market / 分批 TP / move stop to entry / partial close / close position / cancel all / close all / node restart+reconcile / WS 断线恢复 / 两账户隔离。
- 每场景四层证据；两账户隔离（G6/S5 错路由=0）。
- 产物：`TESTNET_ACCEPTANCE_REPORT.md` + 场景证据索引。

## C-09 kill-switch / REDUCING / cancel-all / close-all 演练（→ G5 S10）

- 全节点 ack；单节点未 ack 显示 partial/failed。
- HALTED 无新增风险；REDUCING 只减仓；cancel-all/close-all 终态与交易所一致。
- 产物：`KILL_SWITCH_DRILL.md` + command/ack 审计导出。

## C-10 安全 / 依赖 / 秘密审计（→ G1 G10）

- `scripts/check_no_semantic_regex.py`（A 交付）+ 全仓搜索：无 `raw→regex→approved`、无 `--approve-parsed`/`--refresh-window`/`importer`（G1，含本评估 §3 的旧脚本）。
- secret scan：无 test-token fallback、无匿名 watcher 写、无明文交易所密钥、无默认 live 凭据（G10）。
- SCA/容器扫描、authz 矩阵、端口暴露。
- 产物：`SECURITY_REPORT.md` + SBOM + 网络暴露报告。

## C-11 切换/回滚 → `docs/runbooks/CUTOVER_ROLLBACK.md`

## C-12 最终报告 + gate

汇总全部证据/缺陷/残余风险 → `FINAL_ACCEPTANCE_REPORT.md`；默认 gate ≤ `testnet_only`；仅 operator 签名升 `live_small`；artifact hash + 最终 commit SHA 完整。

## 阻塞器（gate 保持 blocked）

任一 G1–G10 不满足、任一 S1–S11 不达标、任一 P0/P1 未关闭、回放/ testnet 报告未过、kill-switch/rollback 演练未过 → `blocked`。
