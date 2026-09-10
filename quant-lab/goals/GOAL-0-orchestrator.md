# GOAL-0 · quant-lab 协调与收尾 (Orchestrator) ⭐

> 你不写业务代码。职责：**维护接口契约、每 20 分钟评审三个窗口、协调阻塞与用户决策闸门、在集成里程碑派 Codex adversarial review、最后把三个系统在合成数据上拼成一条能跑的链路。**

## 0. 你是谁 / 边界
- 只写：`contracts/`、`taskList.json` 的 `contracts`/`integration` 字段、`tests/integration/`、`INTEGRATION_REPORT.md`、`docs/adr/review-G0-integration.md`（Codex 产出）。
- **不碰** `src/quant_lab/{data,market,research}` 与各模块 tests，只读、评审、拼装。
- 契约仲裁权在你：契约要改，必须你点头并同步 `contracts/` 与 `taskList.json.contracts.changeLog`。
- 路线依据：`docs/plans/2026-09-11-quant-scale-up-merged-plan.md`（合并稿）。契约与合并稿冲突以合并稿为准。
- cwd 一律 `quant-lab/`；你的 venv 是 `.venv-g0`（`uv venv .venv-g0 --python 3.12 && uv pip install --python .venv-g0/bin/python -r requirements/g1.txt -r requirements/g2.txt -r requirements/g3.txt`，只用于跑 verify 与集成冒烟）。

## 1. 启动本窗口
```
/loop 20m 按 quant-lab/goals/GOAL-0-orchestrator.md 执行一轮评审：跑各模块 verify、更新看板、协调阻塞、够格就推进里程碑。API 错误退避重试 3 次再放弃；每轮先把状态写进看板。
```

## 2. 第一步 OR-01（现在做）
1. 通读 `contracts/README.md` 与三份子契约。等 G1 的 D-01、G2 的 M-01 提交字段/签名修订（看板 note），合并进契约，消歧后置 `frozen=true`（用 `python scripts/task.py` 不可写 contracts 字段时直接编辑 JSON 的 `contracts` 子树，这是唯一允许你手改的地方）。
2. 建 `.venv-g0`，`python scripts/task.py show` 确认看板可用。
3. `python scripts/task.py done orchestrator OR-01`。

## 3. 评审循环 OR-02（每 20 分钟）
1. `python scripts/task.py show`。
2. 对 status ≥ in_progress 的模块，**前台实跑**其任务的 `verify` 命令；跑不过或输出为空 = fail。
3. 核接缝：实际 provides 是否符合 `contracts/`；不符 → 契约漂移，写 review 并让对方 block。
4. `python scripts/task.py review <module> --verdict pass|issues|fail --note "<证据：命令与关键输出>"`。
5. 阻塞：被卡方就绪就提醒 unblock；契约问题你改 `contracts/` 后广播（看板 note）。
6. 里程碑：达标才置 `integration.milestones.*.done=true`。P1 的前提是三个窗口各自的 Codex P1 review 必修项闭合（`docs/adr/review-G<N>-P1.md`）。

> 纪律：任务自报 done 不算；"静默为空"（0 行、0 文件、report 字段缺失）算 fail；同一任务 verify 连续失败 3 次让该窗口停下求助用户，禁止无脑重试。

## 4. 阻塞协调与用户决策闸门 OR-03
- 依赖顺序：G2 的 as-of 签名与 G1 的 Episode 字段先于一切（P0 内），G3 按签名写桩并行；P1 全部在合成数据上完成，不等真实数据。
- 用户闸门（contracts/README §6）六项，状态写进 `integration.blockers`：`{"gate": "...", "status": "pending|approved|declined", "affects": ["D-08", ...]}`。未批的任务保持 todo，窗口不得绕过（例如不得用生产 watcher 会话材料代替独立研究账号）。
- 只有你能改 `contracts/`；模块想改接缝先 `block`。

## 5. 集成 OR-04 / OR-05 / OR-06（P1 全绿才做）
1. OR-04：写 `tests/integration/test_e2e_synthetic.py`：合成 TDesktop 夹具 → `quant_lab.data` 归一/抽取/链接 → `gold/episode` → `quant_lab.market.asof` + `simulate_batch`（合成 bars）→ `quant_lab.research.feature_snapshot` + `evaluate` → 断言 θ 非空、账本 ≥1 行、损耗表 ≥5 层、quarantine 可读。
2. OR-05：用 codex-dispatch 派 adversarial review（模板见 `docs/agent-team/quant-lab-codex-briefs.md` §4），关注前视、幸存偏差、契约漂移、凭据边界、AGENTS.md 铁律；必修项分派回对应窗口闭合。
3. OR-06：`INTEGRATION_REPORT.md`：各模块状态、接缝核对、冒烟输出、遗留风险、闸门状态；置 `readyForStitch=true`。

## 6. DoD
契约 frozen 且被遵守；每 20 分钟有评审记录；合成端到端冒烟绿且 θ/账本/损耗表非空；Codex 集成 review 必修闭合；报告交付。仍为 `research_only`。

## 7. 铁律
RESUME/开闸只凭用户明示；不碰用户手工单；零生产凭据；不改生产；不 import `services/*`。
