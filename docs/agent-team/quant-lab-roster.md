# quant-lab 花名册

| 角色 | 窗口 | 定义 | 模型 | 市场参考（融合来源） |
|---|---|---|---|---|
| quant-lab-orchestrator | G0 | `.claude/agents/quant-lab-orchestrator.md` | opus | specialized/specialized-model-qa.md（独立审计、复现与 delta 报告纪律） |
| quant-lab-data-executor | G1 | `.claude/agents/quant-lab-data-executor.md` | opus | engineering/engineering-data-engineer.md（Bronze/Silver/Gold、幂等、契约、血缘） |
| quant-lab-kernel-executor | G2 | `.claude/agents/quant-lab-kernel-executor.md` | opus | engineering/engineering-backend-architect.md（状态机、幂等、超时与重试语义） |
| quant-lab-research-executor | G3 | `.claude/agents/quant-lab-research-executor.md` | opus | academic/academic-statistician.md（设计先于数据、多重比较、区间而非 p 值） |
| Codex 架构师 / 审查者 | 各窗口自派；G0 派集成 | `docs/agent-team/quant-lab-codex-briefs.md` | gpt-6-astra medium | — |

启动顺序：G0（OR-01 定契约）与 G1/G2（各自第一个任务提交签名）同时开；契约 frozen 后 G3 开工。三份 ADR 是各窗口第二个任务，ADR 未到不写核心模块。

产物：`quant-lab/goals/GOAL-0..3`、`quant-lab/taskList.json`（38 任务）、`quant-lab/contracts/`（4 份）、协议 `docs/agent-team/quant-lab-workflow-protocol.md`、任务书 `docs/agent-team/quant-lab-codex-briefs.md`、存档 `quant-lab/.agent-team/team-selection.json`。
