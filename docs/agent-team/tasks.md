# Account Stall Hardening 任务表

状态枚举：`pending`、`in_progress`、`review`、`done`、`blocked`。

## 2026-08-09 Canary 收敛任务

| ID | Owner | Status | Deliverable | Evidence / Next Gate |
|---|---|---|---|---|
| C-P2 | planner/runtime | done | FILTERED/HALTED 优先级、pending session wake、公平清理 wrapper degradation | projection 37 passed/4 skipped；调度竞态 50/50 |
| C-GATE | planner/live-trade-verifier | done | 可用性 gate 分层、节点遥测与交易所 authority 解耦、独立 loss monitor、身份重验、完整财务证明 | 当前 executor 191 passed、adapter 129 passed、recorder/deployment 6-file selection 45 passed；close ACK 累计 cap、post-HALT recoverable、recoverable evidence atomic replace 和 preflight phase-bound drift 回归通过；runtime/projection/http focused 73 passed；heartbeat API 14 passed；execution+nautilus 422 passed/11 skipped/2 subtests；deployment 387 passed；control-plane API 86 passed |
| C-BASELINE | planner/runtime | done | 非目标组合基线按明确结构字段 allowlist 计算，行情刷新和未知扩展字段不阻断；recorder 保留杠杆、保证金模式与自动追加保证金配置 | deployment + recorder 421 passed；双独立 reviewer `P0=0 P1=0` |
| C-DEPLOY | planner | in_progress | 提交并部署新 executor、adapter 与 recorder bundle，保持 account-a HALTED | 当前生产 commit `8a77a50`，部署时间 2026-08-09 21:59:07 UTC；等待 reviewer PASS 与新 commit-bound bundle |
| C-PREFLIGHT | live-trade-verifier | pending | deployed adapter preflight、目标归零和非目标组合审计快照 | 等待 C-DEPLOY；现场读取当前 Redis instrument 原始字节、当前 book 和新鲜 exchange snapshot |
| C-LIVE | live-trade-verifier | pending | 单次 `0.07 SOLUSDT LIMIT + IOC` 往返、精确平仓、最终 HALT | 等待签名 gate 与 dry-run |
| C-REVIEW | reviewer/evidence-auditor | pending | 最终 P0/P1、交易所证据、组合基线和损益复核 | 等待 C-LIVE |

## 2026-08-09 Meta-Review 有效任务

下表保留 Phase A 冻结时点的任务状态。当前受限 canary 授权以本文件顶部
“Canary 收敛任务”和现行恢复计划为准。

| ID | Owner | Status | Deliverable | Evidence / Next Gate |
|---|---|---|---|---|
| A0 | planner | done | 从 `7368641` 创建干净 worktree `codex/account-stall-phase-a` | `/Users/balen/projects/trader-bot-account-stall-phase-a` 初始 status 为空 |
| A1 | planner | done | 冻结 169 项 directory-collapsed source status、373 项展开 status、tracked patch、domain hashes 与四组 test inventory | `docs/evidence/2026-08-09-account-stall-phase-a-source-freeze.md`；`docs/evidence/inventory/` |
| A2 | scope-integration-auditor | done | in-scope dependency closure、混合 hunk、导入顺序 | `docs/evidence/2026-08-09-account-stall-scope-integration-audit.md`；A6 使用符号级最小 runtime closure |
| A3 | runtime-diagnostician | done | 当前代码 stall red-capable seam 与假设排序 | `docs/evidence/2026-08-09-account-stall-runtime-diagnosis.md` |
| A4 | runtime-resource-contract-auditor | done | Node/manifest optionality matrix 与 Phase B interface | `docs/evidence/2026-08-09-account-stall-runtime-resource-contract-audit.md` |
| A5 | historical-evidence-auditor | done | 历史/offload lineage、Redis 强度、mirror drift、test inventory 审核 | `docs/evidence/2026-08-09-account-stall-history-and-evidence-audit.md` |
| A5-T | evidence-auditor | in_progress | 固化完整 replay package 与部署脚本测试根策略 | Agent worktree `/Users/balen/projects/trader-bot-account-stall-a3-evidence`；完整 `hk-deploy-20260803.sh` 已明确排除出本地执行；等待依赖制品 hash、继承环境、raw output、exit code、JUnit、warning/skip 工具 |
| A6 | planner/integrator | in_progress | 导入最小 runtime dependency closure，实现并运行 fault-injection loop | `627c6ff` cleanup retry authority；`bbe35fa` actor message-bus seam；`bca0720` runtime lifecycle；Phase A runtime `30 passed`；等待 actor/node 与 strategy closure manifest |
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
