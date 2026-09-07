# upgrade-crew 协作协议

> 本文档是团队所有成员的必读协议。任何成员行动前先确认自己所处的 Phase 和职责边界。
> 设计真相：`docs/plans/2026-08-30-panel-and-app-upgrade-v1.md`；接缝真相：`contracts/backend-api.md`；看板真相：`taskList.json`（只用 `python3 scripts/task.py` 读写）。三者冲突时按 设计文档 > 契约 > 看板 顺序请 G0 仲裁。

## 团队编制

| 角色 | 定义文件（Claude / Grok） | 模型（Claude / Grok） | 职责一句话 |
|------|---------|------|-----------|
| Planner | `.claude/agents/upgrade-crew-planner.md` / `.grok/agents/…` | opus / grok-4.6 xhigh | 拆任务、派发、失败回流、里程碑验收 |
| Backend Executor | `…upgrade-crew-backend-executor.md` | sonnet / grok-4.6 high | worktree 内实现 M1a-d/M2f |
| Frontend Executor | `…upgrade-crew-frontend-executor.md` | sonnet / grok-4.6 high | worktree 内实现 M0/M2a-e/M3a-c |
| Reviewer | `…upgrade-crew-reviewer.md` | sonnet / grok-4.6 xhigh | 红线审查，PASS 才 merge |
| Tester | `…upgrade-crew-tester.md` | sonnet / grok-4.6 high | T0-T3 证据式验收 |

编制裁剪记录：Architect 裁掉（设计已冻结成文档）；Documenter 裁掉（设计/契约/GOAL 文档已交付，代码注释由 Executor 随任务自带）。

## 两窗口部署模型（本团队怎么开）

本团队**只开两个执行窗口**（对应 goal 脚手架的 G1/G2），5 个角色是两个窗口共用的班底，不是 5 个窗口：

| 窗口 | 遵循 | 主会话身份 | 派发的子代理 | 只动的看板子树 |
|------|------|-----------|-------------|---------------|
| 窗口 1（后端） | `goals/GOAL-1-backend.md` | upgrade-crew-planner（以 G1 范围行事） | backend-executor、reviewer、tester | `modules.backend`（B-01..B-07） |
| 窗口 2（前端+app） | `goals/GOAL-2-frontend-app.md` | upgrade-crew-planner（以 G2 范围行事） | frontend-executor、reviewer、tester | `modules.frontend-app`（F-01..F-10） |

- 每个窗口的主会话即该窗口的 Planner 实例：只读自己的 GOAL 文件、只领自己子树的任务、只派自己线的 Executor；reviewer/tester 两窗口共用定义但各审各线的 worktree。
- 窗口之间**不直接通信**，一切经 taskList.json（block/unblock/note）；契约仲裁与里程碑推进归 G0（协调窗口，挂 `/loop 20m` 评审）。
- Grok 下同理：两个 `grok` 会话分别以 G1/G2 范围开工，spawn_subagent 派各自班底（深度 1 限制天然符合本模型）。

## 流水线协议

### 任务标签与二元组

每个实现单元生成二元组，标题带标签：
- `[Executor] <任务描述>` — 实现（worktree 隔离）
- `[Reviewer] <同名>` — blocked by Executor 任务

### 看板（替代通用 tasks.md）

本项目已有 GOAL 脚手架看板，团队**统一用它**，不另建任务表：
- 读写：`python3 scripts/task.py show / claim / report / done / block`
- 模块子树：后端任务在 `modules.backend`（B-01..B-07），前端+app 在 `modules.frontend-app`（F-01..F-10）
- 只动自己模块的 subtree；`contracts`/`integration` 字段只有 G0 能写

### Phase 流转

1. **规划**：Planner 按 GOAL-1/GOAL-2 依赖表展开，一次只展开一个模块的二元组。
2. **实现**：Executor `git worktree add .worktrees/task-<ID> -b auto/task-<ID>`，只在 worktree 内改动，只 stage 自己改的文件，`auto:` 前缀提交。绝不直接碰 main。app 任务在 alert-personal 仓库内同法（分支 `goal/trading-v1` 之上）。
3. **审查**：Reviewer 进 worktree → 实跑测试与类型检查 → 对照红线清单。
   - PASS：`git merge --no-ff` 进 main，删除 worktree 和分支，看板 done。
   - FAIL：`git worktree remove --force` 销毁，看板 block，🔴 报告回 Planner。
4. **模块验收**：模块二元组全部完成后 Planner 派 Tester 跑对应验收清单，出 PASS/FAIL 报告。
5. **收尾**：Tester PASS 后 Planner squash：`git reset --soft HEAD~<N> && git commit -m "feat(<模块>): completed"`；更新看板 progress 并通知 G0。

### 审查红线（任一不满足即 FAIL）

1. **记账红线**：diff 新增任何对 trade_outcomes / orders_projection / positions_projection / execution_events / exchange_state_mirror 的写入，或新增 DB 迁移（本次升级零迁移）。
2. 实现与 `contracts/backend-api.md` 字段不一致；既有 `/v1` 响应字段被改名/删除（只增不改不删）。
3. 写操作无幂等 ref 或重试不复用同 ref；app 侧 request_id 无 `mobile-` 前缀。
4. hedge 保护单归属未按 exit side 拆分或无双向持仓测试。
5. 越界改文件（对照 GOAL 文件"不碰"清单）；告警链路行为被改动。
6. 测试未全绿 / 类型检查报错 / 残留调试代码。

### 验证命令（全员统一，实跑不转述）

```bash
# 后端
.venv-arch/bin/python -m pytest tests/control-plane services/report/tests -q
# 面板
npm --prefix bridge/apps/dashboard test && npm --prefix bridge/apps/dashboard run test:e2e
# app（alert-personal 根）
npm --prefix /Users/balen/projects/working/alert-personal run typecheck:android && npm --prefix /Users/balen/projects/working/alert-personal run test:android
```

### 失败与升级

- FAIL → Planner 造新二元组修复。
- **同一任务失败 2 次**：Reviewer/Tester 出 Gap Analysis，Planner 决定接受偏差/再修/问用户。
- **失败 3 次：强制中断，求助人工。禁止无脑重试。**

## 跨引擎说明

- **Claude Code**：各角色定义在 `.claude/agents/`，Planner 用 Agent 工具派发其余角色；看板一律走 `scripts/task.py`（不用 TaskCreate 等会话级任务工具，保证跨引擎一致）。
- **Grok CLI**：角色定义在 `.grok/agents/` + `.grok/roles/`（模型硬纪律：一律 grok-4.6，effort 仅 high/xhigh）。主会话身份用 `grok --agent upgrade-crew-planner` 指定（agent 定义接管整个会话；等价 `GROK_AGENT=upgrade-crew-planner grok`）。**Grok subagent 嵌套深度为 1**：主会话即 Planner，用 spawn_subagent 派发其余角色（subagent_type 传角色名，Executor 传 isolation: "worktree"），子代理之间不互派。

## 文档护栏

修改本目录任何协议/agent 定义前，先备份到 `docs/agent-team-backups/<时间戳>/`。
