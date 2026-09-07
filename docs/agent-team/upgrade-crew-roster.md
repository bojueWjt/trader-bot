# upgrade-crew 花名册

- 组队日期：2026-08-30；形态：流水线（pipeline）；平台：Claude Code + Grok CLI
- 目标：执行 panel-and-app-upgrade-v1（设计 `docs/plans/2026-08-30-panel-and-app-upgrade-v1.md`，模块 M0-M3）
- 挑选存档：`.agent-team/team-selection.json`（下次重组基于它）

| 角色 | 来源市场 agent | 定制要点 | Claude | Grok |
|------|---------------|---------|--------|------|
| Planner | project-management/project-manager-senior.md（Senior Project Manager） | 保留"逐字引用规格、反范围蔓延、验收可测"方法论；任务系统替换为本项目 taskList.json 看板；注入两层规划与失败升级纪律 | opus | grok-4.6 xhigh |
| Backend Executor | engineering/engineering-backend-architect.md（Backend Architect） | 保留 API 契约治理/幂等/可观测性专长；注入记账红线、零迁移、契约逐字段一致、hedge 事故回归要求 | sonnet | grok-4.6 high |
| Frontend Executor | engineering/engineering-frontend-developer.md（Frontend Developer） | 保留 React/性能/a11y 专长；注入 Headless UI 迁移策略、antd RN 打样门、口径一致铁律、告警零回归 | sonnet | grok-4.6 high |
| Reviewer | engineering/engineering-code-reviewer.md（Code Reviewer） | 保留导师式分级评审（🔴/🟡/💭）；红线清单替换为本项目六条（记账/契约/幂等/hedge/越界/测试） | sonnet | grok-4.6 xhigh |
| Tester | testing/testing-test-automation-engineer.md（Test Automation Engineer） | 保留反 flaky/证据式/产物可调试信条；用例表替换为设计文档 T0-T3 具名用例与里程碑门 | sonnet | grok-4.6 high |

## 模型分层说明

- Grok 硬纪律（用户拍板 2026-08-30）：所有角色一律 `grok-4.6`，reasoning_effort 仅 high/xhigh。
- Claude 侧偏离记录：Executor 档按方法论默认应为 haiku，本团队因实盘交易系统风险（记账红线、控制面权限）提为 sonnet，已在提案阶段向用户展示并经挑选页确认。

## 裁剪理由

- **Architect**：裁。设计已冻结为文档（含 API 契约、验收、测试标准），无再设计需求；接缝变更走 G0 仲裁。
- **Documenter**：裁。设计/契约/GOAL/协议文档已交付，代码注释按仓库风格由 Executor 随任务自带，不设免审直通角色。
- **tasks.md**：不生成。项目已有 taskList.json + scripts/task.py 原子锁看板，避免双任务系统分叉。

## 启用方式（两个窗口，见协议"两窗口部署模型"）

开工前置：G0 已冻结 `contracts/backend-api.md`（等 codex 设计 review 裁决完成）。

- **窗口 1（后端，G1）**启动指令：
  `以 upgrade-crew-planner 身份按 goals/GOAL-1-backend.md 执行，遵守 docs/agent-team/upgrade-crew-workflow-protocol.md；只动 modules.backend 子树，从 B-01 开始展开二元组，派 upgrade-crew-backend-executor / reviewer / tester 干活。`
- **窗口 2（前端+app，G2）**启动指令：
  `以 upgrade-crew-planner 身份按 goals/GOAL-2-frontend-app.md 执行，遵守 docs/agent-team/upgrade-crew-workflow-protocol.md；只动 modules.frontend-app 子树，从 F-01 开始（契约 fixtures 先行），派 upgrade-crew-frontend-executor / reviewer / tester 干活。`
- **Grok CLI**：主会话身份用 `--agent` 直接指定（agent 定义接管整个会话的 system prompt/工具），两个窗口分别在项目目录执行：
  ```bash
  grok --agent upgrade-crew-planner   # 窗口 1 和窗口 2 都用 planner 身份起
  ```
  起来后第一条消息发对应窗口的启动指令（上面两条，去掉"以 upgrade-crew-planner 身份"前缀——身份已由 --agent 给定）。`/config-agents` 可确认 upgrade-crew-* 五个定义已被项目 `.grok/agents/` 发现；派班底用 spawn_subagent（subagent_type 传角色名）。等价写法：`GROK_AGENT=upgrade-crew-planner grok` 或 `grok --agent .grok/agents/upgrade-crew-planner.md`。
- **G0 协调窗口**：不属于执行窗口，由主会话承担，挂 `/loop 20m 按 goals/GOAL-0-orchestrator.md 执行一轮评审`。

## F-10 已接受残余风险登记（2026-08-30 八轮 adversarial 终局,G0 入档）

1. live POST 响应丢失后同 `client_ref` 重试——幂等设计本意,服务端去重+replay:true 兜底
2. terminal 后 mirror 刷新失败静默,页面可能短暂显示旧仓位——下次提交前会强制重拉重校验
3. mirror GET 与正式 POST 间 TOCTOU 窗口——服务端风控承担最终门禁
4. 轮询总时长可能超过配置值(单请求独立 12s 超时)
5. legacy 动态键守卫在原生无 secureKeys 导出时不生效——判 N/A:三键存储代码从未进入任何构建产物,该数据在真实设备上不存在
6. 空字符串 legacy 值漏检——同上 N/A
7. 测试代码一处解构风格 nit,无运行时影响

审计线索: 8 轮 adversarial review job id 见看板评审历史;测试量 53→115。
