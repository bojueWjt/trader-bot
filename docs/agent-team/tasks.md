# Account Stall Hardening 任务表

状态枚举：`pending`、`in_progress`、`review`、`done`、`blocked`。

## 2026-08-09 Meta-Review 有效任务

当前只授权 Phase A。下表是现行任务状态；后续历史里程碑和开放修复项全部保持
`NO-GO hold`，直到 A7 Reviewer 明确授权。

| ID | Owner | Status | Deliverable | Evidence / Next Gate |
|---|---|---|---|---|
| A0 | planner | done | 从 `7368641` 创建干净 worktree `codex/account-stall-phase-a` | `/Users/balen/projects/trader-bot-account-stall-phase-a` 初始 status 为空 |
| A1 | planner | done | 冻结 169 项 directory-collapsed source status、373 项展开 status、tracked patch、domain hashes 与四组 test inventory | `docs/evidence/2026-08-09-account-stall-phase-a-source-freeze.md`；`docs/evidence/inventory/` |
| A2 | scope-integration-auditor | done | in-scope dependency closure、混合 hunk、导入顺序 | `docs/evidence/2026-08-09-account-stall-scope-integration-audit.md`；A6 使用符号级最小 runtime closure |
| A3 | runtime-diagnostician | done | 当前代码 stall red-capable seam 与假设排序 | `docs/evidence/2026-08-09-account-stall-runtime-diagnosis.md` |
| A4 | runtime-resource-contract-auditor | done | Node/manifest optionality matrix 与 Phase B interface | `docs/evidence/2026-08-09-account-stall-runtime-resource-contract-audit.md` |
| A5 | historical-evidence-auditor | done | 历史/offload lineage、Redis 强度、mirror drift、test inventory 审核 | `docs/evidence/2026-08-09-account-stall-history-and-evidence-audit.md` |
| A5-T | planner/integrator | pending | 固化完整 replay package 与部署脚本测试根策略 | 依赖制品 hash、继承环境、raw output、exit code、JUnit、warning/skip；`TRADER_ROOT` override 或完整入口排除 |
| A6 | planner/integrator | in_progress | 导入最小 runtime dependency closure，实现并运行 fault-injection loop | A2、A3 |
| A7 | reviewer | pending | GAP-0 结论与 Phase B/Phase B-S 授权 | A3、A4、A5、A6 |

## 历史任务记录

以下任务保留为 2026-08-08 实现记录，状态不代表当前授权。

| ID | 里程碑 | Owner | Depends On | Status | Deliverable | Evidence / Commit | Next Owner |
|---|---|---|---|---|---|---|---|
| M0 | 冻结代码与线上基线 | planner | - | review | 基线报告、风险台账、版本与资源快照 | `docs/incidents/2026-08-08-account-node-stall.md`；最早 stall=2026-07-02 14:18:15 UTC；生产只读取证截至 2026-08-08 06:38 UTC | reviewer |
| M1 | 目标架构与 ADR | architect | M0 | review | ADR、模块图、状态机、迁移顺序 | `docs/adr/2026-08-08-account-node-stall-hardening.md`；lane/queue/watchdog/Redis/release/rollout decisions | reviewer |
| M2 | 不可变镜像与 release identity | redis-release-executor | M1 | pending | image digest、manifest、`/version`、A/B gate | - | reviewer |
| M3 | Redis fenced generation 与容量治理 | redis-release-executor | M1 | in_progress | stable lease resource、generation namespace、registry、janitor、limits | UUID4 generation 与 Nautilus 接线已通过聚焦测试；等待 janitor 集成与 reviewer | reviewer |
| M4 | 控制面关键路径隔离 | control-plane-executor | M1 | pending | node-control、event-ingest、query API 与 pools | - | reviewer |
| M5 | NodeControlPlaneSession 独立 lanes | node-runtime-executor | M1 | pending | heartbeat、command、ACK、intent、event lanes | - | reviewer |
| M6 | 健康状态、watchdog 与 incidents | node-runtime-executor | M4,M5 | pending | tick liveness、四维状态、incident 状态机 | - | reviewer |
| M7 | 指标、告警与版本偏差检测 | redis-release-executor | M2,M3,M4,M5,M6 | pending | metrics exporter、SLO、alerts | - | reviewer |
| M8 | 真实 Redis/Nautilus 集成恢复 | chaos-tester | M2,M3,M4,M5,M6,M7 | pending | skipped tests 转为可复跑 PASS | - | evidence-auditor |
| M9 | 故障注入矩阵 | chaos-tester | M8 | pending | latency、ACK、PG、Redis、freeze、restart 报告 | - | evidence-auditor |
| M10 | account-a 不可变 canary 发布 | planner | M9 | pending | HALTED 发布、对账、回滚验证 | - | live-trade-verifier |
| M11 | 真实小额交易闭环 | live-trade-verifier | M10 | pending | 撤单验证、微型 round trip、归零 | - | evidence-auditor |
| M12 | 最终审计与运行手册 | evidence-auditor | M11 | pending | 四层证据、最终 PASS/FAIL | - | documenter |
| M13 | 文档收口 | documenter | M12 | pending | ADR、runbook、事故记录、复跑命令 | - | planner |

## 开放 P0/P1 修复

| ID | Severity | Owner | Status | Deliverable | Acceptance Evidence |
|---|---|---|---|---|---|
| R1 | P0 | transport-fencing-executor | in_progress | lane hard deadline 触发 fatal process termination；HTTP connect/read/total deadline | 永久阻塞故障注入后 `process_liveness=false`，进程退出 hook 仅触发一次，session stop 有界 |
| R2 | P1 | transport-fencing-executor | in_progress | 全部 node 读写路径统一校验 Redis epoch、runtime generation、lease fencing token | replacement 接管后旧 writer 的 intent、command、ACK、event 请求均返回 409，并触发本地 fatal fence |
| R3 | P1 | intent-recovery-executor | review | 通用 intent durable inbox、management terminal state 与 exchange-confirmed recovery | execution/nautilus `491 passed`；management 清空内存去重后重放零 replacement/zero cancel |
| R4 | P1 | command-ack-executor | in_progress | pending ACK durable outbox 启动恢复；journal entry/byte 双上限 | command poll 不重投时 ACK 仍恢复发送；成功后 durable 删除；backpressure 保持 HALTED |
| R5 | P1 | release-resource-executor | in_progress | hardening 发布强制 immutable image 与 control-plane isolation；legacy 仅限 HALTED emergency rollback | transition bind mount、isolation disable、identity/hash drift 的正常 rollout 全部 fail-closed |
| R6 | P1 | release-resource-executor | in_progress | Redis safety 与 journal byte capacity 进入 release-bound JSON 和 manifest hash | 错误阈值、环境漂移、manifest/config hash 漂移测试 PASS |
| R7 | P0 | transport-fencing-executor + release-resource-executor | in_progress | account-a canary 在 `account_a_canary` 阶段获得受限 RESUME，account-b 完成后进入 `fleet_complete` | canary permit 可完成 mainnet round trip；常规 RESUME 仍要求 fleet complete；B gate 仍要求签名 A 证据 |
| R8 | P0 | intent-recovery-executor | in_progress | canary 持仓期间持续 mark-to-market 亏损监控 | 无新增 fill 时越过 1.5 USDT cap 仍触发 HALT 与精确 reduce-only close |
| R9 | P1 | planner | in_progress | 真实 Redis fixture 初始化固定 epoch marker并复跑 | `test_redis_namespace_lease_real.py` 8 项实际执行 PASS，零 skip |
| R10 | P1 | transport-fencing-executor + intent-recovery-executor | in_progress | permit expiry、单 ACTIVE writer、consumed baseline drift 全链路 fail-closed | 过期 permit、双 ACTIVE、消费后漂移均阻止执行并保留 incident/HALT 证据 |
| R11 | P1 | release-resource-executor | in_progress | rollback 恢复安全证明与四类 account-b evidence 内容验证 | 旧 manifest、HALTED/readiness、reconciliation、Redis epoch、writer identity 全部复核；空报告拒绝 |
| R12 | P0 | release-closure-executor + release-resource-executor | in_progress | release 收录 `reviewed_release_rollout.py`；node bundle 收录 `/app/risk/__init__.py` | release exact-set、bundle transitive import、recreate/deploy target 集合与 SHA256SUMS 全部 PASS |
| R13 | P0 | intent-recovery-executor | done | PREPARE 后实时 HALT gate、strategy stopping latch、sticky HALT 晚到结果丢弃 | 三个竞态回归 PASS；stopped worker 无法由 timer 复活；risk-increasing submit=0 |
| R14 | P1 | intent-recovery-executor | review | management 在首个 side effect 前 durable dispatch，terminal 后 durable complete | async `refresh -> fsync -> replacement/cancel -> terminal fsync -> replay` 回归 PASS |
| R15 | P0 | release-resource-executor | in_progress | 单一 operation lock、Postgres 可恢复备份、migration/rollback 分阶段 recovery image | deploy/rebaseline/isolation/rollout/permit 不可交叠；backup hash 与 restore validation PASS |
| R16 | P0 | live-trade-verifier | in_progress | 审计执行器机械化 permit、restricted RESUME、SOLUSDT round trip、HALT、evidence | 硬绑定 account-a/SOLUSDT/12 USDT/loss < 1.5；异常 finally close+HALT |

## 当前任务约束

- Planner 每次只把一个里程碑或一组无冲突任务改为 `in_progress`。
- 每个 Executor 任务必须写明允许修改路径、测试命令和回滚面。
- Reviewer PASS 后由 Planner 填写 commit，并将任务切到 `done`。
- 任何 P0/P1 finding 将对应任务切为 `blocked` 或重新创建修复任务。
