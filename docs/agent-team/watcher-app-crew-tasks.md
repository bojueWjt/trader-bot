# watcher-app-crew 任务看板

> 四个平台共用的唯一任务表。状态：pending / in_progress / review / testing / done / blocked。设计真相：`docs/plans/2026-09-11-watcher-to-app-migration.md`；协议：`docs/agent-team/watcher-app-crew-workflow-protocol.md`。
> 集成分支：trader-bot `integ/watcher-app-crew`（基线 `67b401a`，worktree `.worktrees/wac-integ`）；alert-personal `integ/watcher-app-crew`（基线 `0f7d26d`，worktree `.worktrees/wac-integ`）。

## 里程碑

| 里程碑 | 模块 | 状态 |
|--------|------|------|
| M0 契约冻结 | 路由真源 YAML、backend-api.md 新节 | pending |
| M1 watcher 地基 | W-0 | pending |
| M2 控制面快照 | C-0 | pending |
| M3 平板基础设施 | T-0 | pending |
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
| wac-001 | [Executor] 契约冻结：路由真源与 backend-api.md 网关/快照节 | trader-bot | `contracts/watcher-gateway-routes.yaml`、`contracts/backend-api.md` | §2.1–§2.3、§3、附录 E | YAML 解析与自检 | — | watcher-app-crew-architect | pending | |
| wac-002 | [Reviewer] 契约冻结 | trader-bot | — | 协议红线 5 | — | wac-001 | watcher-app-crew-reviewer | pending | |
