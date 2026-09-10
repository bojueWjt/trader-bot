# quant-lab 协作协议

> 团队所有成员必读。形态：**三窗口流水线 + G0 协调 + Codex 设计/审查**。看板与契约在 `quant-lab/`，路线依据是合并稿 `docs/plans/2026-09-11-quant-scale-up-merged-plan.md`。

## 团队编制

| 角色 | 定义文件 | 模型 | 职责一句话 |
|---|---|---|---|
| G0 协调者 | `.claude/agents/quant-lab-orchestrator.md` | Claude `opus` | 契约、20 分钟评审、闸门跟踪、集成与报告 |
| G1 数据执行者 | `.claude/agents/quant-lab-data-executor.md` | Claude `opus` | Telegram → episode 六级产线、标注与分级放行 |
| G2 内核执行者 | `.claude/agents/quant-lab-kernel-executor.md` | Claude `opus` | 行情湖、三时钟 as-of、执行内核 A/B |
| G3 研究执行者 | `.claude/agents/quant-lab-research-executor.md` | Claude `opus` | AST/算子契约、评估器、walk-forward、max-t、分档 |
| Codex 架构师 | codex-dispatch 任务（`docs/agent-team/quant-lab-codex-briefs.md` §1） | `gpt-6-astra` medium（high 可试） | 三份引擎 ADR：episode 引擎、执行内核、表达式与统计引擎 |
| Codex 审查者 | codex-dispatch 任务（§3 / §4） | `gpt-6-astra` medium | 每窗口 P1/P2 review；集成 adversarial review |

用户拍板：Claude 侧一律 opus，不分层。

## 阶段与门

| 阶段 | 内容 | 门 |
|---|---|---|
| P0 地基 | 三窗口 venv；G1/G2 提交签名，G0 冻结契约（OR-01）；三份 ADR 落盘 | ADR 行数与关键词 verify 通过；tests 可 collect |
| P1 主链路（合成数据） | D-03..D-09 / M-03..M-09 / R-03..R-09；每窗口 Codex P1 review | 所有 verify 由 G0 前台实跑通过；review 必修闭合；OR-04 合成端到端 θ/账本/损耗表非空 |
| P2 硬化（真实数据） | 用户闸门批准后 1a 导出、真实分区、FPR/功效、A/B 定型；OR-05 adversarial review | 必修闭合；INTEGRATION_REPORT；仍 `research_only` |

## 窗口启动指令

```
G0：/loop 20m 按 quant-lab/goals/GOAL-0-orchestrator.md 执行一轮评审
G1：按 quant-lab/goals/GOAL-1-data.md 执行；先 D-01、D-02（派 Codex ADR），ADR 到手后按顺序做；API 错误退避重试 3 次；收口前把状态写进看板
G2：按 quant-lab/goals/GOAL-2-market-kernel.md 执行；先 M-01、M-02；其余同上
G3：按 quant-lab/goals/GOAL-3-research-engine.md 执行；先 R-01（写桩）、R-02；其余同上
```
先开 G0 与 G1/G2 定签名，G0 冻结契约后 G3 开工（G3 的 R-01 桩可提前按草案写）。

## 看板铁律
1. 只用 `python scripts/task.py` 改看板；只动自己的 `modules.<name>`；`contracts`/`integration` 只有 G0 可写。
2. 完成即上报；被卡 `block`；对方就绪自己 `unblock`。
3. **自报 done 不算完成**，G0 实跑 verify 且产出非空才算。
4. 契约冻结后改接缝先 `block`，等 G0 改契约；禁止私改。

## Codex 参与协议
1. **引擎设计先于实现**：D-02 / M-02 / R-02 是各窗口第二个任务，ADR 未到不得开写核心模块（归一、下载器、AST lint 这类无争议的基础可先做）。
2. **派发方式**：codex-dispatch 脚本，`--model gpt-6-astra --effort medium`，任务书六段式（目标/范围/约束/验收/验证/输出），只读仓库 + 只写指定输出文件；WATCH_CMD 放 Monitor 工具；result.sh 取结果后核产出规模（行数、关键词、文件存在）。禁止 `--resume-last`；STALLED 即 cancel 重派。
3. **ADR 与契约冲突**：窗口不得按 ADR 私改接缝，先 `block` 给 G0 仲裁；G0 可要求 Codex 修订 ADR。
4. **review 必修项**：窗口闭合后在看板 note 写"review-G<N>-P<k> 必修 S01..Sx 已闭合：<证据>"，G0 核对后才置里程碑。
5. **失败升级**：同一任务 verify 失败 2 次出 gap 分析；3 次强制中断求助用户。

## 环境仲裁
`.python-version` 3.12；`requirements/*.txt` 钉版本；每窗口自己的 `.venv-gN`，绝不共用或重建别人的；`uv.toml` 镜像与重试；长驻服务 nohup + 固定端口（Label Studio 8081）；研究湖 `data/` 只有 G1/G2 各写自己的分区，G3 只读。

## 铁律（AGENTS.md）
RESUME/开闸只凭用户明示；不碰、不撤销、不针对用户手工单告警；研究层零生产凭据；不改生产；不 import `services/*`。研究通过不等于任何上线授权。

## 文档护栏
修改本目录任何协议或 agent 定义前，先备份到 `docs/agent-team-backups/<时间戳>/`。
