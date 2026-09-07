# GOAL-1 · 后端读模型与角色 (G1)

> 一句话目标：给控制面补齐 app/面板共用的只读读模型（镜像持仓/对账/绩效/溯源），战报与 bridge 报表补真实数据——**零迁移、零记账写路径**，hedge 保护单归属零错判。（v1.1：契约按 codex review 修订并冻结；D12 撤销 mobile_operator 角色）

## 0. 你是谁 / 边界
- 你负责：`services/control-plane/`、`services/report/`、`packages/contracts/v1/`（新增 schema + 登记）、`tests/control-plane/`、`services/report/tests/`、**`bridge/apps/api/app/services/report_snapshot.py`（仅 M2g/B-08 接线）**。
- **不碰**：`bridge/apps/dashboard`、bridge/apps/api 其余部分、`db/migrations`（本次零迁移）、alert-personal 仓库、任何记账表写路径（contracts/README.md §4 全局禁止事项）。
- 你是上游：接缝已在 `contracts/backend-api.md` 冻结，**照它实现，逐字段一致**；想改接缝先 `block`，禁止私改。G2 靠它写 fixtures 并行开工。
- 分支：trader-bot 内开 `goal/backend-v1`（基于 `arch/execution-state-v1`）。

## 1. 技术栈
Python（仓库既有 `.venv-arch`，禁止重建；新依赖走 block 仲裁）；pytest；FastAPI 路由风格与 `read_api.py` 既有约定一致（新端点允许拆子模块 router 挂载，设计 R4）。

## 2. 交付物
- `/v1/mirror/positions`、`/v1/reconcile`、`/v1/outcomes` 三个新端点（带 `_envelope()`）
- `/v1/positions|orders|trades` 补 `account_id`；`/v1/positions` 补 `protection` 真值
- M1e：六层溯源端点 + incidents 只读列表 + 命令状态查询
- M2g：bridge report_snapshot 收集器接线控制面读端点
- `packages/contracts/v1/` 新 schema 文件 + 发布产物登记
- 战报新 KPI（profit factor / 平均持仓时长）+ 依赖三态页首渲染

## 3. 接口契约(provides)
`contracts/backend-api.md`（冻结版）——响应形状、hedge 归属规则（含 algo_orders）、bot 单正则、KPI 公式、写路径契约全部以它为准。

## 4. 任务清单(taskList.json → modules.backend)
| id | 交付 | 验收要点 |
|---|---|---|
| B-01 | 测试入口固化 + 契约核对 | 实跑两套 pytest 入口（report 套现有 1 个 collection error 要查明），结果更新进 contracts/backend-api.md 附录 |
| B-02 | M1a account_id + protection 真值 | 设计§4.1a/d；T1-3/T1-8；`.venv-arch/bin/python -m pytest tests/control-plane -q` 全绿 |
| B-03 | M1b mirror/positions + reconcile | 设计§4.1b/c；T1-1/T1-2(hedge 双向,事故回归)/T1-4/T1-5/T1-6 |
| B-04 | M1c outcomes | 设计§4.1e；T1-7（R 分桶边界、水位过期 stale） |
| B-05 | M1e 溯源/incidents/命令状态 | 设计§4.1g、契约§6；T1-11 |
| B-06 | M2f 战报增强 | 设计§5.1-4；T2-9/T2-10；`.venv-arch/bin/python -m pytest services/report/tests -q` |
| B-07 | schema 落 packages/contracts/v1 + 登记（SCHEMAS/manifest/snapshot/examples） | e2a37f6 教训 + review 缺口 #7 |
| B-08 | M2g bridge 报表收集器接线 | 设计§5.1-5；T2-12；依赖 B-03/B-04 |

每个任务按 CLAUDE.md 用 codex-dispatch 委派实现，或派 upgrade-crew 班底子代理（backend-executor 实现 / reviewer 审查 / tester 验收，见 `docs/agent-team/upgrade-crew-workflow-protocol.md` 两窗口部署模型）；六段式任务书从设计文档对应小节摘取，**禁止事项必须附 contracts/README.md §4**；验收按证据式流程（diff 落地、实跑测试、产出规模）。

## 5. 协作协议
```bash
python3 scripts/task.py claim backend B-02
python3 scripts/task.py report backend --status in_progress --progress 0.3 --note "<进展>"
python3 scripts/task.py done backend B-02
```
B-02/B-03 done 时在 note 里注明"G2 可联调"，G0 会广播。

## 6. DoD
- 三端点与补字段和契约逐字段一致；既有面板未升级也零回归（字段只增）
- T1-1..T1-11 全部落地且全绿
- 全部 diff 过记账红线检查（无记账表写入、无新迁移）
- `/v1/mirror/positions` 与 `v3_query.py positions` 同时刻输出一致性抽查记录在 PR 描述

## 7. 验证命令
```bash
.venv-arch/bin/python -m pytest tests/control-plane services/report/tests -q
```

## 8. 依赖与顺序
无上游（契约 v1.1 已冻结）。B-01 最先；**并行规则（G0 裁决 08-30）**：B-06 随时可并行（零冲突，KPI 公式以契约§4 为准）；B-03/B-04/B-05 与 B-02 并行的硬性前提是新端点走独立 router 子模块文件、read_api.py 只加挂载行；**merge 优先级 B-02 第一**，其余在其合并后 rebase 再合；B-08 等 B-03/B-04 合并。被 G2 等待：**B-02 → F-01（M0 硬依赖，review #4）**、B-03 → F-03 联调 + F-08、B-04 → F-05、B-05 → F-06。
