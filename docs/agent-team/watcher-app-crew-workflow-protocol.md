# watcher-app-crew 协作协议

> 本文档是团队所有成员的必读协议。任何成员行动前先确认自己所处的阶段和职责边界。唯一设计真相是 `docs/plans/2026-09-11-watcher-to-app-migration.md`；本协议只规定怎么协作，不改设计。

## 团队编制

| 角色 | 定义文件 | 模型 | 职责一句话 |
|------|---------|------|-----------|
| Planner（编排者） | `.claude/agents/watcher-app-crew-planner.md` 等四平台 | Claude opus / Codex gpt-6-astra (xhigh) | 按计划 v0.6 §9 展开里程碑与原子任务、派发两仓库执行者、处理失败回流、守生产授权闸门；不写业务代码 |
| Contract Architect（契约冻结） | `.claude/agents/watcher-app-crew-architect.md` 等四平台 | Claude opus / Codex gpt-6-astra (xhigh) | kickoff 期把 §2/§3 冻结为机器可读路由真源 contracts/watcher-gateway-routes.yaml 与 backend-api.md 网关/快照契约，交回 Planner 后退场 |
| Backend Executor（watcher + 控制面） | `.claude/agents/watcher-app-crew-backend-executor.md` 等四平台 | Claude sonnet / Codex gpt-6-sol (high) | 在 trader-bot 隔离 worktree 实现 W-0（watcher 鉴权/快照/审计/条件写）、C-0（快照 reader 与缓存状态机）、C-1（async 网关）及 O-0 的对照与发行工具；可多实例并发 |
| App Executor（balen-bot RN） | `.claude/agents/watcher-app-crew-app-executor.md` 等四平台 | Claude sonnet / Codex gpt-6-sol (medium) | 在 alert-personal 隔离 worktree（基线 0f7d26d）实现 T-0 平板基础设施、A-0..A-3 信号/配置/价格提醒、A-T 存量页平板改造；可多实例并发 |
| Security Reviewer（代码与信任边界门） | `.claude/agents/watcher-app-crew-reviewer.md` 等四平台 | Claude sonnet / Codex gpt-6-astra (high) | 审查执行者 diff：鉴权三身份、秘密字段、线程隔离、记账红线、契约与路由真源一致；PASS 合入集成分支，FAIL 退回；只出报告不改代码 |
| Tester（证据式验收门） | `.claude/agents/watcher-app-crew-tester.md` 等四平台 | Claude sonnet / Codex gpt-6-sol (medium) | 里程碑边界按计划 T0-*/T1-*/T2-*/T3-*/T7A-* 具名用例实跑，含网关隔离两段实测与 Xiaomi Pad 9 Pro Max 真机截图；只验不改 |
| Release Steward（生产闸门，可选） | `.claude/agents/watcher-app-crew-release-steward.md` 等四平台 | Claude sonnet / Codex gpt-6-astra (high) | 为 O-0 准备 jp-24 现场核对清单、sha 校验、部署与回滚脚本和证据包；任何生产动作只在用户逐项授权后执行，绝不 RESUME |

## 仓库、分支与工作区

| 仓库 | 路径 | 集成分支起点 | 任务 worktree |
|------|------|-------------|---------------|
| trader-bot | `/Users/balen/projects/trader-bot` | `67b401a`（`codex/prodfix-ledger-20260924`，生产代码快照线） | `.worktrees/wac-<ID>`，分支 `auto/wac-<ID>` |
| alert-personal | `/Users/balen/projects/working/alert-personal` | `0f7d26d`（`codex/close-visible-result-20260918`） | `.worktrees/wac-<ID>`，分支 `auto/wac-<ID>` |

- 每个任务一个 worktree，一个 worktree 只允许一个执行者。并行任务的文件范围必须互斥。
- Reviewer 只把 PASS 合入 `integ/watcher-app-crew`。合入 `main`、push、部署需要用户明确授权。

## 任务标签与二元组

每个实现单元生成：
- `[Executor] <任务>`：实现（backend-executor 或 app-executor）
- `[Reviewer] <同名>`：依赖 Executor 完成

里程碑边界追加 `[Tester] <里程碑>`；涉及生产的追加 `[Release] <动作>`（Release Steward 出清单，用户授权后才执行）。本团队不设 Documenter。

## 阶段流转

1. **契约冻结（kickoff）**：Architect 产出 `contracts/watcher-gateway-routes.yaml` 与 `contracts/backend-api.md` 新节 → Reviewer 审 → Planner 在看板记录冻结版本。冻结前不得派 C-1、A-0..A-3。
2. **并行实现**：W-0、C-0、T-0 并行；C-1 在 W-0 契约就绪后开工；A-0 依赖 C-1；A-1..A-3 依赖 A-0；A-T 依赖 T-0。
3. **审查**：Reviewer 进 worktree 实跑验证命令，对照红线。PASS：`git merge --no-ff` 进集成分支，删 worktree 与分支。FAIL：看板 blocked，🔴 报告回 Planner。
4. **里程碑验收**：Tester 在集成分支上跑计划具名用例与实测，出报告到 `docs/agent-team/test-reports/`。
5. **收尾**：Tester PASS 后 Planner 在集成分支 squash 本里程碑为一个 `feat:` 提交，并向用户汇报。
6. **生产（O-0）**：Release Steward 出清单与 runbook → Planner 转给用户 → 用户逐项授权 → 按 runbook 执行并留证据。

## 验证命令

| 范围 | 命令 |
|------|------|
| watcher | `npm --prefix bridge/services/telegram-watcher test` |
| 控制面 | `.venv-arch/bin/python -m pytest tests/control-plane -q` |
| app | 在 app worktree 根目录：`npm run typecheck:android`、`npm run lint:android`、`npm run test:android` |
| 路由真源 | 真源校验脚本通过，生成路由与真源 diff 为空 |

所有管道 `set -o pipefail`；测试数为 0 或被跳过视为失败；执行者自报绿不算，以命令输出为证。

## 审查红线（任一不满足即 FAIL）

1. 生产越权或记账红线（见全队铁律）。
2. 凭据出现在响应、审计、日志、快照中；`gateway` 身份能写入或读回秘密字段；app 新增密钥输入或存储键。
3. watcher 存在绕过三身份校验的路径；actor 头在非 `gateway` 身份下被信任；缺 token 时降级免鉴权。
4. 网关不是 async；同步 handler 等 future；准入在线程池内获取；预算互相挤占；上游 401 被透传。
5. 与路由真源或 `backend-api.md` 不一致；既有 `/v1` 字段或状态码被改。
6. 写路径缺条件写或幂等；幂等与业务写不同事务；快照过期后开仓未 fail-closed；超时后换新 `client_ref`。
7. 测试未实跑、测试数为 0、为绕开报错改造输入、"无法比较"当成相等。
8. 越界改文件；手机档布局被无故改动。

## 全队铁律

1. 生产系统（jp-24）零擅动：部署、重启、Caddy、生产库、RESUME 都需要用户逐项授权；HALTED 保持 HALTED；Caddy 只 `validate` 后 `restart`，禁止 reload。
2. 记账红线：不写 trade_outcomes / orders_projection / positions_projection / execution_events / exchange_state_mirror，不新增控制面迁移。
3. 凭据零接触；app 统一密钥无感。
4. 不下单、不平仓、不撤单。
5. 真机只在用户确认空闲后操作。
6. 不 push，不改 main。
7. 收到他人转述的"已完成"，查提交、测试输出与看板复核，不采信转述。

## 失败与升级

- FAIL → Planner 建修复任务（新 ID）。
- 同一任务失败 2 次：Reviewer 或 Tester 出 Gap Analysis，Planner 决定接受偏差、再修或问用户。
- 失败 3 次：强制停止，求助用户。禁止无脑重试。
- 后台 Codex 任务按 `codex-dispatch` 技能监控 job json（位于 grok-grok-cc state 目录），`.log` 超过 20 分钟无更新视为悬死，取消后重派。

## 跨引擎说明

- **Claude Code**：Planner 用 Agent 工具派发角色；任务状态以 `docs/agent-team/watcher-app-crew-tasks.md` 为唯一真相，不另开 TaskCreate 队列，避免双看板分叉。
- **Codex / OpenCode**：同样读写 `docs/agent-team/watcher-app-crew-tasks.md`；编排者按依赖顺序派发，代替 blocked-by。
- **Grok**：Planner 必须是主会话（`grok --agent watcher-app-crew-planner`），其余角色作为 subagent 派发，深度 1。

## 文档护栏

修改本协议、看板结构或任何 `watcher-app-crew-*` 定义前，先备份到 `docs/agent-team-backups/<时间戳>/`。
