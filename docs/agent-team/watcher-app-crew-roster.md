# watcher-app-crew 花名册

- 组队日期：2026-09-26；形态：流水线（pipeline）；平台：Claude Code / OpenCode / Codex / Grok
- 目标：执行 `docs/plans/2026-09-11-watcher-to-app-migration.md` v0.6（watcher 站点功能迁入 balen-bot app，含控制面网关、统一密钥无感、快照替代副本、信号 Tab、交易配置、价格提醒记录、Android 平板适配）
- 挑选存档：`.agent-team/watcher-app-crew/team-selection.json`（提案：同目录 `team-proposal.json`）

| 角色 | 定义名 | 来源市场 agent | Claude | Codex | OpenCode | Grok |
|------|--------|---------------|--------|-------|----------|------|
| Planner（编排者） | `watcher-app-crew-planner` | project-management/project-manager-senior.md（Senior Project Manager） | opus | gpt-6-astra (xhigh) | balenw/glm-5.3 | grok-4.6 (xhigh) |
| Contract Architect（契约冻结） | `watcher-app-crew-architect` | engineering/engineering-api-platform-engineer.md（API Platform Engineer） | opus | gpt-6-astra (xhigh) | balenw/glm-5.3 | grok-4.6 (xhigh) |
| Backend Executor（watcher + 控制面） | `watcher-app-crew-backend-executor` | engineering/engineering-backend-architect.md（Backend Architect） | sonnet | gpt-6-sol (high) | balenw/glm-5.3 | grok-4.6 (high) |
| App Executor（balen-bot RN） | `watcher-app-crew-app-executor` | engineering/engineering-mobile-app-builder.md（Mobile App Builder） | sonnet | gpt-6-sol (medium) | balenw/glm-5.3 | grok-4.6 (high) |
| Security Reviewer（代码与信任边界门） | `watcher-app-crew-reviewer` | engineering/engineering-code-reviewer.md（Code Reviewer） | sonnet | gpt-6-astra (high) | balenw/glm-5.3 | grok-4.6 (xhigh) |
| Tester（证据式验收门） | `watcher-app-crew-tester` | testing/testing-test-automation-engineer.md（Test Automation Engineer） | sonnet | gpt-6-sol (medium) | balenw/glm-5.3 | grok-4.6 (high) |
| Release Steward（生产闸门，可选） | `watcher-app-crew-release-steward` | engineering/engineering-sre.md（SRE (Site Reliability Engineer)） | sonnet | gpt-6-astra (high) | balenw/glm-5.3 | grok-4.6 (xhigh) |

## 定制要点

- **Planner**（Senior Project Manager）：保留"逐字引用规格、反范围蔓延、验收可测"；任务系统换成本团队看板；注入计划 §9 派发顺序、生产授权闸门与失败升级。
- **Architect**（API Platform Engineer）：保留契约优先、向后兼容、统一错误体与幂等；产物限定为路由真源 YAML 与 backend-api.md 新节，kickoff 后退场。
- **Backend Executor**（Backend Architect）：保留超时、重试、幂等、纵深防御；注入三身份鉴权、async 网关与准入、快照 fail-closed、记账红线。
- **App Executor**（Mobile App Builder）：保留平台规范与性能意识；注入 RN 技术栈、统一密钥无感、三档布局与 Xiaomi Pad 9 Pro Max 真机要求。
- **Reviewer**（Code Reviewer）：保留导师式分级评审；红线替换为本计划八条（生产与记账、凭据、鉴权边界、网关隔离、契约、一致性、测试、越界）。
- **Tester**（Test Automation Engineer）：保留反 flaky 与证据式；用例换成计划具名用例，加入隔离两段实测、三路对照与真机验收。
- **Release Steward**（SRE）：保留渐进上线与数据驱动；限定为清单、脚本与待授权清单，无生产执行权。

## 模型分层说明

- Codex 代号按 2026-09-26 换代核对：脑力 `gpt-6-astra` xhigh；审查与生产闸门 `gpt-6-astra` high；执行与测试 `gpt-6-sol`。
- 偏离记录：方法论默认执行档为 Claude `haiku` / Codex `gpt-6-luna`。本团队因实盘交易系统与鉴权边界风险，执行者提为 Claude `sonnet` / Codex `gpt-6-sol`，后端执行者推理强度 high，已在提案中说明并经挑选页确认。
- OpenCode：本机只有 `balenw` 网关，全部角色 `balenw/glm-5.3`。
- Grok 硬纪律：全部 `grok-4.6`，推理强度仅 high / xhigh。

## 裁剪理由

- **Documenter**：裁。计划、契约与 review 已成文，代码注释由执行者随任务自带。
- 质量门写权限：Reviewer 与 Tester 需要写审查报告、测试报告和看板，所以四个平台都给了文件写权限（方法论默认只读）；"只出报告不改代码"由 prompt 红线约束。
- 替补席（性能测试、真实性核查、无障碍、移动发布、UI 设计、UX 架构、技术写作）未入选，需要时基于挑选存档重组。

## 启用方式

- **Claude Code**：定义已在 `.claude/agents/`，主会话直接派 `watcher-app-crew-planner`，或以 Planner 身份开工。
- **OpenCode**：在项目目录启动，Tab 切到 `watcher-app-crew-planner`（primary），其余角色用 `@watcher-app-crew-<角色>` 调用。
- **Codex**：`.codex/config.toml` 已开 `multi_agent`；以 `watcher-app-crew-planner` 身份开工，按看板顺序 spawn 其余角色。
- **Grok**：`grok --agent watcher-app-crew-planner`，`/config-agents` 确认七个定义可见；只有主会话能 spawn_subagent。

开工第一条指令（任一平台，对 Planner 说）：

> 按 `docs/plans/2026-09-11-watcher-to-app-migration.md` v0.6 与 `docs/agent-team/watcher-app-crew-workflow-protocol.md` 开工。先建两个仓库的 `integ/watcher-app-crew` 集成分支，派 watcher-app-crew-architect 冻结契约（路由真源 YAML 与 backend-api.md 新节），契约 PASS 后并行派 W-0、C-0、T-0。任何生产动作只列清单，等我授权。
