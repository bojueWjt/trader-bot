# 接口契约总纲 (Contracts) — panel-and-app-upgrade-v1

> 本目录是本次升级各窗口的「接缝真相」，由 GOAL-0（协调窗口）维护，其余窗口只读，发现不一致就在 `taskList.json` 提 blocker。
> **注意区分**：`packages/contracts/v1/` 是系统运行时契约（发布产物）；本目录只做窗口间接缝冻结，交付时后端窗口把新 schema 正式落到 `packages/contracts/v1/`（任务 B-07）。

## 0. 项目一句话

面板与移动端升级 v1：修复面板三处真断裂，为控制面补齐只读读模型（镜像持仓/对账/绩效），面板四主题升级并迁移 Headless UI，alert-personal app 新增 Trading 区（多账号状态 + 改SL/TP + 百分比平仓）。质量底线：**记账红线零触碰**（设计文档 §7A，D9），新鲜度诚实，写操作全幂等。

**设计/验收/测试标准唯一来源**：`docs/plans/2026-08-30-panel-and-app-upgrade-v1.md`。GOAL 文件与本目录只做索引与接缝，不复述设计；冲突以设计文档为准，改设计文档需 GOAL-0 仲裁。

## 1. 仓库布局

```
trader-bot/                      # 主仓库（G0/G1 全部工作 + G2 的面板部分）
├─ taskList.json                 # 唯一看板（scripts/task.py 读写）
├─ scripts/task.py
├─ contracts/                    # 本目录（G0 维护）
│   ├─ README.md
│   └─ backend-api.md           # 后端新端点接缝（冻结版）
├─ goals/GOAL-{0,1,2}-*.md
├─ services/control-plane/       # G1
├─ services/report/              # G1
├─ bridge/apps/dashboard/        # G2
└─ packages/contracts/v1/        # G1 交付时正式落盘

~/projects/working/alert-personal/   # G2 的 app 部分（独立仓库，看板仍用本仓库的 taskList.json）
└─ apps/attention-android/
```

## 2. 看板协议

**铁律：**
1. 只用 `python3 scripts/task.py` 改看板，不手改 JSON（原子锁防多窗口竞争写）。
2. 只动自己模块的 subtree `modules.<你的模块>`；`contracts`/`integration` 只有 G0 能写。
3. 每完成一个任务或状态变化立即上报；被卡 → `block`，对方就绪 → `unblock`。
4. **契约冻结后要改接缝：先 `block` 提出，等 G0 改契约并广播，禁止私自改接口。**

```bash
python3 scripts/task.py show
python3 scripts/task.py claim <module> <task-id>
python3 scripts/task.py report <module> --status in_progress --progress 0.4 --note "..."
python3 scripts/task.py done <module> <task-id>
python3 scripts/task.py block <module> --on "契约: ..."
```

## 3. 环境仲裁（防互相拆台）

1. **Python**：统一用仓库既有 `.venv-arch`；**每个 worktree 内建符号链接 `ln -s <主仓>/.venv-arch .venv-arch`**（入口测试按树根解析解释器）（`.venv-arch/bin/python -m pytest ...`，已验证可收集 tests/control-plane 448 个用例）。**任何窗口不得重建/升级该 venv**；需要新依赖 → block 报 G0 仲裁。
2. **Node**：dashboard 需 Node ≥22.12（engines 已钉）；alert-personal app 需 Node ≥20。各自 `npm ci`，不跨项目共享 node_modules。
3. dashboard dev server 用 `bridge/apps/dashboard` 自己的 vite 配置，端口冲突时 G2 自行换端口并写进看板 note，不动他人进程。
4. **git 纪律**：G1/G2 在 trader-bot 内各开自己的分支（`goal/backend-v1`、`goal/frontend-v1`），基于当前 `arch/execution-state-v1`；不碰对方分支；alert-personal 内 G2 开 `goal/trading-v1`。API 网络异常退避重试 3 次再放弃，长任务收口前先把状态写进看板。

## 4. 全局禁止事项（写进每个派发任务）

- **记账红线（D9）**：禁止任何对 trade_outcomes / orders_projection / positions_projection / execution_events / exchange_state_mirror 等记账表的写路径新增；禁止新增 DB 迁移（本次升级零迁移）。
- 禁止改动告警链路（attention 相关服务与 app 现有 5 屏行为）。
- 禁止扩展 bridge/apps/api 的内存态 audit/release-gates。
- `/v1` 既有响应字段只增不改不删。

## 5. 模块接缝表

| 模块 | provides | consumes | 契约文件 |
|---|---|---|---|
| backend (G1) | /v1/mirror/positions、/v1/reconcile、/v1/outcomes、M1e 溯源/incidents、account_id+protection+nodes 补字段、packages/contracts/v1 schema、M2g 报表收集器接线 | contracts/backend-api.md | contracts/backend-api.md |
| frontend-app (G2) | 升级后面板（M0+M2a-e）、app Trading 区（M3a-c） | contracts/backend-api.md（冻结后即可用 fixtures 开工，不等后端合并） | contracts/backend-api.md |
| orchestrator (G0) | contracts/、codex review 裁决、INTEGRATION_REPORT.md | 全部 | — |

依赖顺序：契约冻结 → G1/G2 全并行（G2 用契约 fixtures 写前端与 app，联调等 G1 对应任务 done）→ G0 集成收尾（端到端账实核对，设计 §7A.3）。
