# watcher-app-crew 任务看板

> 四个平台共用的唯一任务表。状态：pending / in_progress / review / testing / done / blocked。设计真相：`docs/plans/2026-09-11-watcher-to-app-migration.md`；协议：`docs/agent-team/watcher-app-crew-workflow-protocol.md`。
> 集成分支：trader-bot `integ/watcher-app-crew`（基线 `67b401a`，worktree `.worktrees/wac-integ`）；alert-personal `integ/watcher-app-crew`（基线 `0f7d26d`，worktree `.worktrees/wac-integ`）。

## 里程碑

| 里程碑 | 模块 | 状态 |
|--------|------|------|
| M0 契约冻结 | 路由真源 YAML、backend-api.md 新节 | in_progress |
| M1 watcher 地基 | W-0 | pending |
| M2 控制面快照 | C-0 | pending |
| M3 平板基础设施 | T-0 | done（代码合入 91f6c8e；真机与键盘避让待 Tester） |
| M4 网关 | C-1 | pending |
| M5 对照与发行准备 | O-0（非生产部分 + Release 清单） | pending |
| M6 app 接入 | A-0 | pending |
| M7 信号 Tab | A-1 | pending |
| M8 交易配置 | A-2 | pending |
| M9 价格提醒 | A-3 | pending |
| M10 存量页平板改造 | A-T | pending |
| M11 生产切换 | O-0 生产部分（需用户授权） | pending |

## 任务

| ID | 标签 | 仓库 | 文件范围 | 验收（计划原文出处） | 验证命令 | 依赖 | 负责 | 状态 | 备注 |
|----|------|------|---------|---------------------|---------|------|------|------|------|
| wac-001 | [Executor] 契约冻结：路由真源与 backend-api.md 网关/快照节 | trader-bot | `contracts/watcher-gateway-routes.yaml`、`contracts/backend-api.md` | §2.1–§2.3、§3、附录 E | YAML 解析与自检 | — | watcher-app-crew-architect（Opus 子代理） | review | bd27b32；WGW-1.0，63 条路由；A-1..A-7 Planner 暂定接受，交 Reviewer 复核 |
| wac-002 | [Reviewer] 契约冻结 | trader-bot | — | 协议红线 5 | — | wac-001 | watcher-app-crew-reviewer（Opus 子代理） | in_progress | 2026-09-26 派发 |
| wac-003 | [Executor] T-0 平板基础设施 | alert-personal | `apps/attention-android/src/ui/`、`src/navigation/RootTabs.tsx`、单列页外层包装（Setup/Pairing/Request/Settings/TradingSetup/AntdPrototype）、新增测试 | §7A.1（除账户/仓位/信号/配置页）、§7A.3 T7A-1/2/3/5 | typecheck:android、lint:android、test:android | —（与契约无关，提前并行） | watcher-app-crew-app-executor（Codex gpt-6-sol medium） | done | c9fc751 → 集成 91f6c8e |
| wac-004 | [Reviewer] T-0 平板基础设施 | alert-personal | — | 协议红线 3、7、8 | 同上 | wac-003 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴，5 🟡；报告 `docs/agent-team/reviews/wac-003.md` |
| wac-005 | [Executor] T-0 审查建议修补：守卫补漏、档位切换保持当前 Tab、宽屏安全区断言、测试卫生 | alert-personal | `apps/attention-android/__tests__/**`，必要时 `src/ui/useLayoutClass.ts` | wac-003 审查 🟡1/2/4/5 与 nit | typecheck/lint/test:android | wac-004 | watcher-app-crew-app-executor（Codex gpt-6-sol medium） | in_progress | 🟡3 键盘避让转 Tester 真机项 |
| wac-006 | [Reviewer] T-0 审查建议修补 | alert-personal | — | 协议红线 7、8 | 同上 | wac-005 | watcher-app-crew-reviewer（Opus 子代理） | pending | |
