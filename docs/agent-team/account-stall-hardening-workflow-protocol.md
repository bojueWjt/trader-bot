# Account Stall Hardening 协作协议

> 本文档是 `account-stall-hardening` 团队的强制协议。任何 agent 行动前必须确认当前 Phase、任务所有者、生产权限和验收门。

## 目标

完成以下改造并交付可复跑证据：

1. 不可变节点镜像、release manifest、A/B image digest 一致性门。
2. 稳定 Redis UUID namespace、单写者 fencing、namespace registry、janitor 和容量边界。
3. `node-control-api`、`node-event-ingest`、`operator-query-api` 的资源隔离与 PostgreSQL 专属连接池。
4. `NodeControlPlaneSession` 独立 heartbeat、command、ACK、intent 和 event lanes。
5. actor tick watchdog、四维健康状态、持久 `node_incidents` 和恢复后 `AWAITING_RESUME`。
6. 故障注入、真实 Redis/Nautilus 重启恢复、一次受限真实小额交易闭环。

## 团队纪律

- `docs/agent-team/tasks.md` 是任务状态单一真相源。
- 每次只展开一个里程碑；同一里程碑内的独立 Executor 任务可以并行。
- Executor 使用 `.worktrees/account-stall-<ID>` 和 `codex/account-stall-<ID>` 分支。
- Planner 负责集成。Reviewer、Tester 和 Evidence Auditor 保持只读。
- 任何 agent 都不能覆盖或回滚用户已有改动。
- 生产环境默认只读。生产写操作必须经过本文的 Production Gate。
- 真实交易权限仅授予 `account-stall-hardening-live-trade-verifier`。
- 失败关闭贯穿全部阶段：状态不确定时保持 HALTED，禁止增加风险。
- 凭据只能从既有 secret/env/credential helper 读取，禁止打印、复制、写入仓库或测试产物。

## 任务流水线

每个实现任务生成四个连续阶段：

1. `[Executor]`：在独立 worktree 实现并提交。
2. `[Reviewer]`：审查 diff、测试、状态机和回滚面，输出 PASS/FAIL。
3. `[Integrator]`：Planner 在 Reviewer PASS 后集成，不采用 Reviewer 直接合并。
4. `[Documenter]`：在独立 docs worktree 更新 ADR、runbook 和验证说明。

里程碑边界增加两个质量门：

1. `[Chaos Tester]`：执行隔离环境故障注入和真实依赖恢复测试。
2. `[Evidence Auditor]`：核对 commit、digest、配置哈希、测试输出和四层证据。

## Phase 流转

### Phase 0：基线冻结

- 记录当前分支、HEAD、工作区已有改动、线上 image/mount/file hash、A/B version、Redis 容量和节点状态。
- 建立证据目录，所有读操作输出脱敏。
- 任何基线差异写入任务表，Planner 决定归属，团队不得擅自清理。

### Phase 1：架构冻结

- Architect 输出 ADR、模块边界、状态机、接口契约和迁移顺序。
- Reviewer 审查 ADR 对现有行为、回滚、账户隔离和幂等的影响。
- ADR PASS 后 Executor 才能开始跨模块实现。

### Phase 2：实现与单元验证

- Node Runtime、Control Plane、Redis/Release Executor 在独立 worktree 工作。
- 每个提交聚焦一个原子任务，包含对应测试和失败路径。
- 代码遵循仓库 `AGENTS.md`、既有模块边界和本协议。

### Phase 3：集成与故障注入

- 集成环境使用真实 PostgreSQL、Redis 和 pinned Nautilus 容器。
- 覆盖控制面延迟、ACK 阻塞、连接池耗尽、Redis persistence 压力、actor 冻结、进程重启和重复回放。
- 任何重复 intent、重复订单、账户串线、恢复后自动 ACTIVE 都构成 FAIL。

### Phase 4：不可变发布

- 构建一次镜像，按 digest 部署。
- release manifest 至少包含 git SHA、image digest、config hash、dependency lock hash 和 schema version。
- account-a canary 保持 HALTED，完成启动对账和只读核验。
- account-b 只在 account-a canary PASS 后更新。
- 版本偏差超过发布窗口或出现新 incident 时执行已验证回滚。

### Phase 5：真实小额交易

- Live Trade Verifier 读取 `account-stall-hardening-live-trade-policy.md`。
- 全部前置门通过后自动执行，不请求额外用户输入。
- 完成真实挂单撤单和一个微型 market round trip。
- 平仓和四层证据闭环完成后，Evidence Auditor 才能签署最终 PASS。

## Production Gate

Planner 必须确认以下项目全部 PASS：

- Reviewer 对目标 commit PASS。
- Chaos Tester 对当前 release digest PASS。
- Evidence Auditor 已核对 A/B、manifest、配置和数据库迁移。
- 回滚命令、备份和恢复验证均已准备。
- 节点处于 HALTED，actor tick、heartbeat、projection 和 reconciliation 状态可观测。
- Redis 活跃 namespace 已登记，清理计划不包含活跃 namespace。
- PostgreSQL migration 支持向前恢复；破坏性 down migration 不进入自动路径。

## 审查红线

以下任一项出现即 FAIL：

- actor 线程执行网络 I/O 或等待未受控 future。
- heartbeat 排在 ACK backlog 后。
- 队列、重试、缓存、stream 或 namespace 无容量上限。
- 同一账户允许两个活跃写进程且缺少 fencing。
- 进程 liveness 未包含 actor tick age。
- 依赖恢复直接切回 ACTIVE。
- 发布物料包含 Python 业务代码 bind-mount。
- A/B digest、config hash 或 schema version 不一致。
- 测试只验证派生配置，没有核对真实 Redis key 和重启恢复。
- 生产凭据进入日志、命令行、报告或 git。
- 真实交易超过策略额度，或异常状态下继续重试增加风险。

## 失败升级

- 第一次失败：Executor 根据完整 finding 新建修复任务。
- 第二次失败：Reviewer 输出 Gap Analysis，Architect 复核边界。
- 第三次失败：Planner 标记 `blocked`，停止生产发布和真实交易。
- 任何生产异常立即打开 incident，保持 HALTED，并执行回滚或精确减仓。

## Codex 协作方式

- Planner 使用 multi-agent 工具派发角色，并在 `docs/agent-team/tasks.md` 更新状态。
- 独立读取和测试任务尽量并行派发。
- Agent 间交接通过任务表的 Evidence、Commit、Findings 和 Next Owner 字段完成。
- Planner 每次派发时附当前里程碑、允许修改路径、禁止修改路径和验收命令。

## 完成定义

- 所有任务状态为 `done`，无开放 P0/P1 finding。
- 真实 Redis/Nautilus 恢复测试从 skip 转为可复跑 PASS。
- A/B 运行同一 digest 和 config schema。
- 故障注入报告 PASS。
- 真实小额交易完成开仓、投影、reduce-only 平仓和账户归零。
- 最终报告包含四层证据、实际费用/滑点、incident 状态和回滚证据。
