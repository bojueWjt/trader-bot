# 最终验收报告（INTERIM — gate=testnet_core_proven，未达 testnet_only 全量）

> 分支 `work/integration-acceptance-v3`。当前为**核心验收链路真机打通**阶段：真实图文回放 + testnet 真实下单已亲验，但全量 testnet（C-08 十四场景）/混沌(C-07)/面板对账(C-06)/kill-switch 演练(C-09) 与 A↔B order_plan 对齐未完成。
> **release gate = `blocked`（不可 live）**：核心执行链已验证，但默认上限 `testnet_only` 的全量验收未过，且 live 需 operator 签名。

## 0. 真机验收里程碑（hk，2026-06-20）

两大头部验收项已在真机亲验：

- **C-04/C-05 真实多模态 Hermes 回放**：80 条真实 Telegram 语料(text+图)→ ingress → PostgreSQL → **真 Hermes `gemini-3.1-pro`** → 决策。**75/75 成功**（8 并发，102s）。判定合理：analysis/noise→ignore 36、new_signal→open_position 14（从图里读出 instrument/side/stop）、update→needs_review 19（空快照下 fail-safe）。提交 `3b3cd52`。
- **C-08 核心 testnet 真实下单**：intent → control-plane → node 轮询(1s) → strategy → **Binance USDT-M testnet 真实成交**（BTCUSDT 0.0020 @ 63474.35，venue_order_id 15670069285，OrderFilled→PositionOpened）→ execution_events 回流 control-plane。提交 `be928f9`。

为打通执行链，修了一串 **B host-verify 缺陷**（B 从未在真机跑过）：`node.build()` 缺失(崩溃循环)；node↔control-plane seam 6 个端点 A 侧从未实现（我补齐：intents-ack/execution-events/heartbeat/commands/account）；`subscribe_data` 在 1.227.0 拒绝无 client 自定义数据(改走 msgbus)；`ClientOrderId` 传 str；lifecycle 硬编码 HALTED 无 RESUME 通路。提交 `dcd6d90`/`571e4c7`/`be928f9`。

**剩余缺口（findings，risk-critical 未仓促改）**：① A↔B **order_plan 契约不一致**（A gateway `{side:long,entry{type}}` vs B planner `{side:buy|sell,type,quantity}`+需仓位定量）—— 不对齐则真 gateway 决策无法在 node 执行；② **command poller 未接进 node**（RESUME/kill-switch 到不了节点，卡 C-09）；③ **snapshot 投影未从 node 执行事件派生**（事件已入 `execution_events`，但 AccountState 载荷空、OrderFilled 稀疏，需 node ExecutionProjectionActor 富化 + 派生逻辑，卡 C-06）。

## 1. 已完成并验证（C-00 + A 的 8 个 P0）

合并 A+B：0 冲突（`268c84c`）。随后修复并用真实 pg 验证 A 的 8 个 P0：

| commit | 修复 | 验证 |
|---|---|---|
| d772e87 | 契约 `order_plan` 闭合空→开放（执行可携带订单） | contracts 5/5 |
| 090fa32 | 风控 governor **fail-OPEN→fail-CLOSED**（缺 risk_state 不再当 ACTIVE） | risk 31/31（含 2 新测试） |
| 5c60e1a | gateway 强制决策 freshness/valid_until | +1 测试 |
| 51292fd | worker 确定性挡非法动作组合（update→open、close 无 target） | +5 测试 |
| 81c0431 | instrument 暴露闸读真实 positions（原读恒零字段、永不触发） | risk 31/31 |
| be5e2f9 | `hermes_decisions` UNIQUE(raw_message_id) 防重复决策（migration 0004，up/down 可逆） | schema 测试 |
| c797d0a | ingress 写端点鉴权（无匿名注入） | ingress-auth 5/5 |
| c797d0a+6436cdc | token fail-closed、删公开默认 admin token、canonical 角色 | bridge 222/222 |

**全量回归绿**：A python 面 **105**（92 原 + 13 新）+ bridge **222** + no-semantic-regex **0 违规** = 327 测试通过。

## 2. 评审产物
- `docs/acceptance/WINDOW_A_REVIEW.md`（A 的 11 个 P0；上面 8 个已修，剩 3 个见 §4）。
- `docs/acceptance/WINDOW_B_REVIEW.md`（B：逻辑扎实但「未在真机验证」；安全回路/命令通路未接进运行进程、执行测试全 skip、大量 host-verify TODO）。
- `MERGE_REPORT.md` / `HANDOFF_REVIEW.md` / `ACCEPTANCE_PLAN.md` / `docs/runbooks/CUTOVER_ROLLBACK.md`。

## 3. Release gate 判定 = `blocked`
未跑真实回放(C-05)/testnet(C-08)/混沌(C-07)/面板对账(C-06)/kill-switch 演练(C-09)；B 执行层未在真机验证。G1–G10、S1–S11 均未取得 C 亲验的验收证据（A 自报不算）。`release-gate.json` 为机器记分板。

## 4. 剩余工作 + 阻塞原因

**A（剩 3）—— 需运行时/部署裁决，非孤立代码修复：**
- 服务端仍跑 legacy bridge 栈；安全 control-plane（已含 §2.2 envelope + DB 审计 + 5 角色）是独立未挂载 app。要「换栈/退役 legacy」是**部署接线决策**（哪个 service 服务什么、dashboard 指向何处），需真实运行环境敲定（牵动 222 bridge 测试与 bridge↔PG 接线）。
- 账户级 `open_risk_fraction` 总风险闸：需 **positions/projection 写入**（Nautilus 节点事件→需主机）。
- 危险操作落库审计：与上面换栈绑定（legacy router 退役与否）。

**B（全部执行层）：** host-verify TODO、安全回路接进运行进程、BacktestEngine harness、跑 skip 的执行测试 —— **均需 Nautilus 真机**。

**C-02…C-12：** 迁移、真实图文回放、testnet、面板对账、混沌、kill-switch 演练、切换 —— **均需基础设施**。

## 5. 唯一阻塞（operator 提供，取自 balen Mac mini）
1. **Linux + Docker + Python 3.12 主机**（跑 Nautilus 1.227.0；与 hk 生产隔离端口/项目或专用 VPS）。
2. **Binance USDT-M Futures testnet API keys**。
3. **真实 Hermes `HERMES_API_URL / HERMES_API_KEY / HERMES_MODEL`**（HK gateway）。
4. **S3/MinIO 对象存储**（media bytes）。

## 6. 拿到基础设施后的即时恢复计划（按序）
1. 在主机起 v3 栈（PG/Redis/control-plane/ingress/hermes-worker/2× Nautilus node HALTED+testnet/dashboard）；先验 B 的 host-verify TODO（projection 订阅、order factory、RiskEngine 字段、命令 adapter）→ 修 B 的 P0（安全回路接进 run_node、close-all 等待、denial 上抛、Redis DB 隔离、容器上限）。
2. C-02 迁移旧 SQLite→PG（`legacy_*`，幂等，row/hash 对账）。
3. C-03 用真实 corpus（扩到 500 条、补 HYPE 事故）人工标 gold。
4. C-04 真实回放 harness（watcher→PG→真 Hermes→gateway→Nautilus sandbox→projection→dashboard，禁 importer 旁路）。
5. C-05 三干净+两重复回放对 gold；C-06 面板逐层对账；C-07 混沌；C-08 testnet 14 场景；C-09 kill-switch 演练。
6. C-11 切换/回滚演练（`docs/runbooks/CUTOVER_ROLLBACK.md`）。
7. 更新本报告 + `release-gate.json`；默认上限 `testnet_only`；`live_small` 需 operator 签名。

## 7. 残余风险
- B 执行层「绿 CI = 规划数学对」≠「执行安全」，真机前不可信。
- A 换栈前，served 面板/审计仍是 legacy 行为（gate-blocked 下不对外）。
- 真实 Hermes 多模态判定质量需 C-05 三次回放稳定性裁决。

## 8. Operator 签名
**未签**。gate 保持 `blocked`。升 `testnet_only`/`live_small` 需本报告 §5 基础设施到位 + 全部 P0/验收通过 + operator 留痕。
