# GOAL-2 · 面板与移动端 (G2)

> 一句话目标：修掉面板三处真断裂，把四主题升级做进 dashboard（逐页迁 Headless UI），在 alert-personal app 做出与 jp-bot 面板口径一致的 Trading 区——**写操作全幂等强确认，数据龄诚实展示**。

## 0. 你是谁 / 边界
- 你负责：`bridge/apps/dashboard/`（trader-bot 仓库）+ `~/projects/working/alert-personal/apps/attention-android/`（独立仓库）。
- **不碰**：services/*、packages/*、db/*、bridge/apps/api（唯一例外：M0 里 pair-lock 改调既有 `/api/risk/pair-locks`，只改前端调用方）；app 现有告警 5 屏行为零改动（D2）。
- 你是下游：数据形状以 `contracts/backend-api.md`（冻结版）为准，**先用 fixtures 开工，不等后端合并**；联调时只许改 client 层，组件层数据形状不动。接缝异议走 `block`。
- 分支：trader-bot 内 `goal/frontend-v1`；alert-personal 内 `goal/trading-v1`。
- **口径一致铁律（D10）**：app 与面板同一数字必须同源同算法（同端点、同三态规则、同数据龄阈值：绿<60s/黄<300s/红≥300s 或 stale）。

## 1. 技术栈
- 面板：React 19 + Vite + TS（现状）；新增 `@headlessui/react` + Tailwind CSS（迁移策略见设计§2.1：逐页替换、preflight 控制引入范围、先打样一个 Dialog）；vitest + Playwright。
- app：RN 0.82 + react-navigation（现状，Node ≥20）；jest + tsc；不新增重量级状态库。组件库用 **`@ant-design/react-native` 5.4.3**（D11）：需同时引入 react-native-gesture-handler + reanimated（原生依赖，要重编译）；**M3a 第一步先打样** List/Modal/Slider/Stepper/Toast 在 RN 0.82 新架构下可用且 `build:android` 通过，失败即 block 报 G0 走降级方案（antd token + 自绘），不许硬顶。

## 2. 交付物
- 面板：M0 断裂修复、M2a UI 基座、M2b 多账号+真相层+reconcile 页、M2c 保护单真值+告警条、M2d 绩效区+History 换源、M2e 审计链抽屉+舰队健康+incidents 表
- app：M3a Trading 配置/导航/AccountsScreen、M3b PositionsScreen+详情只读、M3c 写操作（改SL/改TP/百分比平仓+确认流+幂等 ref+`mobile-` 前缀 request_id）

## 3. 接口契约(consumes)
`contracts/backend-api.md`。fixtures 按其 schema 手工构造（含 hedge 双向、SL 在 algo_orders、stale 账户、dry_run 响应含 would_reject 各形态）。

## 4. 任务清单(taskList.json → modules.frontend-app)
| id | 交付 | 验收要点 |
|---|---|---|
| F-01 | M0 断裂修复 v1.1（命令信封/两段式 operator/pair-lock/Reports 形状） | 设计§3；T0-1..T0-8；**依赖 backend B-02 合并**（review #4）；e2e mock 按契约§5 校验请求 |
| F-02 | M2a Headless UI+Tailwind 基座打样 | 设计§2.1；build+test 绿；存量页样式无污染（R7） |
| F-03 | M2b 多账号+真相层+reconcile 页 | 设计§5.1-1；T2-1/T2-2/T2-7；fixtures 先行 |
| F-04 | M2c 保护单+无保护告警 | 设计§5.1-2；T2-3/T2-8；三态仅按 mirror 在场性（无 policy 逻辑，review #18） |
| F-05 | M2d 绩效+History 换源 | 设计§5.1-3；T2-6；修 pnlPct 恒 0 |
| F-06 | M2e 审计链+舰队健康+incidents | 设计§5.1-4；T2-4/T2-5；halt_reason/心跳>120s 红 |
| F-07 | e2e 全量 + a11y | T2-11 + 既有 Playwright 全绿 |
| F-08 | M3a app 配置/导航/Accounts | 设计§6.1；T3-6/T3-7；tradingApi 配置与 attention 配置隔离 |
| F-09 | M3b Positions+详情只读 | T3-5；hedge 两行、mirror stale 黄条 |
| F-10 | M3c 写操作：dry_run 确认流+绝对数量换算+幂等+终态轮询 | T3-1..T3-4；**必须 adversarial review**；告警 5 屏回归全绿 |

每任务按 CLAUDE.md 用 codex-dispatch 委派（app 任务 `--dir ~/projects/working/alert-personal`），或派 upgrade-crew 班底子代理（frontend-executor 实现 / reviewer 审查 / tester 验收，见 `docs/agent-team/upgrade-crew-workflow-protocol.md` 两窗口部署模型）；六段式任务书摘设计文档对应小节，附 contracts/README.md §4 禁止事项。

## 5. 协作协议
```bash
python3 scripts/task.py claim frontend-app F-01
python3 scripts/task.py report frontend-app --status in_progress --progress 0.5 --note "<进展>"
python3 scripts/task.py done frontend-app F-01
```
依赖（v1.1）：**F-01 硬依赖 B-02**；F-03/04 联调等 B-02/03；F-05 等 B-04；F-06 等 B-05(M1e)；F-08 等 B-03(M1b)。等待期间做 fixtures 与其余任务，**不干等**。

## 6. DoD
- T0/T2/T3 全部测试用例落地且全绿；e2e+a11y 绿；app typecheck/lint/test/build 全绿
- 告警功能零回归；四账户卡与战报数字一致抽查（联调后）
- 写操作走 dry_run→确认→提交两段式，确认框回显齐全、幂等 client_ref 复用、`warnings`/`would_reject` 全文展示、终态经 operator status 轮询
- 全部 diff 过记账红线检查（前端不该有任何 DB/迁移改动）

## 7. 验证命令
```bash
npm --prefix bridge/apps/dashboard test && npm --prefix bridge/apps/dashboard run test:e2e
npm --prefix /Users/balen/projects/working/alert-personal run typecheck:android && npm --prefix /Users/balen/projects/working/alert-personal run test:android
```

## 8. 依赖与顺序
上游：contracts/backend-api.md v1.1（已冻结）。建议顺序：（等 B-02）F-01 → F-02 → (F-03..F-06 并行) → F-07；app 线（等 B-03）F-08 → F-09 → F-10 可与面板线并行；等待期先做 fixtures/组件层。被 G0 等待：全部 done 后进入 §7A.3 端到端账实核对。
