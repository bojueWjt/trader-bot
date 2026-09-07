# GOAL-0 · 协调与收尾 (Orchestrator / Reviewer) ⭐

> 你不写业务代码。职责：**维护接口契约、每 20 分钟评审 G1/G2、协调阻塞、裁决 codex review 发现、最后做端到端账实核对并出集成报告。**

## 0. 你是谁 / 边界
- 只写：`contracts/`、`taskList.json` 的 `contracts`/`integration` 字段、端到端冒烟脚本、`INTEGRATION_REPORT.md`。
- **不碰** G1/G2 业务代码 — 只读、评审、拼装。契约仲裁权在你。
- 设计真相：`docs/plans/2026-08-30-panel-and-app-upgrade-v1.md`（改设计也归你仲裁）。

## 1. 启动本窗口
```
/loop 20m 按 goals/GOAL-0-orchestrator.md 执行一轮评审:跑各模块 verify、更新看板、协调阻塞、够格就推进里程碑
```

## 2. 第一步 OR-01（现在做）
1. 收取 codex 设计 review 结果（已派发，job json 路径见派发记录；用 codex-dispatch 的 result.sh 取）。**阻断项**逐条裁决：改设计文档/改 contracts/backend-api.md，或标记不成立并记理由。
2. 阻断项清零后：`contracts/backend-api.md` 状态改为 frozen，`taskList.json.contracts.frozen=true` 并在 changeLog 记一条。
3. 广播开工：G1/G2 窗口可启动。
4. `python3 scripts/task.py done orchestrator OR-01`

## 3. 评审循环 OR-02（每 20 分钟一轮）
1. `python3 scripts/task.py show`。
2. 对 status≥in_progress 的模块**实跑**其任务 verify 命令（先前台跑一次对照真实输出；"静默为空"= fail）。
3. 查接缝：G1 实际响应形状 vs `contracts/backend-api.md`；G2 fixtures vs 同文件。漂移 → 记 blocker。
4. `python3 scripts/task.py review <module> --verdict pass|issues|fail --note "<证据>"`。
5. 协调阻塞；达标才置 `integration.milestones.*.done=true`（gate 定义在 taskList.json）。

## 4. 协调阻塞 OR-03
- 依赖顺序：契约冻结 → G1 全线 ∥ G2（fixtures 先行）→ 联调（F-03/04 等 B-02/03；F-05 等 B-04；F-10 联调等 B-05）→ 集成。
- G2 不许干等后端：契约冻结即用 fixtures 开工。
- 模块要改接缝必须 block，你改 contracts/ 后广播；每模块任务 done 后按 CLAUDE.md 跑 `/codex:review --background`，B-05 与 F-10 必须 adversarial review（权限/幂等/hedge 归属/竞态/记账红线）。

## 5. 集成收尾 OR-04（里程碑全绿才做）
1. 端到端账实核对（设计 §7A.3）：测试环境 改SL→改TP→50%平仓→100%平仓，`v3_query.py` / 面板 / app 三端数字一致；幂等重发 3 次只入一笔。
2. 记账红线终检：全部 diff 无记账表写入新增、无新迁移。
3. 出 `INTEGRATION_REPORT.md`：模块状态、接缝核对、演练输出、遗留风险。
4. `integration.readyForStitch=true`，通知所有窗口。部署到 jp-24 时遵守设计 §9.1（动 Caddy 必跑 resume_race.py）。

## 6. DoD
codex review 阻断项全部裁决；契约冻结且被遵守；每 20 分钟有评审记录；§7A.3 演练全绿；报告交付。

> 纪律（历史教训）：API 错误退避重试 3 次再放弃；长任务收口先写看板再继续；任务自报 done 不算数，以实跑 verify + 产出规模为准。
