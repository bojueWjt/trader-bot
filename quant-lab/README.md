# quant-lab

频道种子→量化算法的研究 app。**与生产完全分离**：不 import `services/*`，不持有任何生产凭据（数据库、operator token、交易所、节点、Redis、生产 Telegram session）。

路线依据：`docs/plans/2026-09-11-quant-scale-up-merged-plan.md`（合并稿，S01–S14 已闭合）与 `docs/research/2026-09-07-quant-toolchain-selection/`。

## 多窗口工作方式

- 看板：`taskList.json`，只用 `python scripts/task.py` 读写。
- 契约：`contracts/`，只有 GOAL-0 可写。
- 窗口：`goals/GOAL-0..3`。启动指令见各 GOAL 文件第 1 节。
- Codex：引擎设计 ADR 与里程碑 review 由各窗口用 codex-dispatch 派发，任务书模板在 `docs/agent-team/quant-lab-codex-briefs.md`。

## 环境

```bash
cd quant-lab
uv venv .venv-g1 --python 3.12 && uv pip install --python .venv-g1/bin/python -r requirements/g1.txt   # G1
uv venv .venv-g2 --python 3.12 && uv pip install --python .venv-g2/bin/python -r requirements/g2.txt   # G2
uv venv .venv-g3 --python 3.12 && uv pip install --python .venv-g3/bin/python -r requirements/g3.txt   # G3
```
每窗口 `PYTHONPATH=src` 或 `uv pip install -e .`（pyproject 由 D-01/M-01/R-01 各自补齐，包名 `quant_lab`，共用同一 pyproject，改动走看板通知）。
