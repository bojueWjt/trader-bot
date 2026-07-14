# 平仓归属加固方案（2026-07-14 事故修复，子账户方案不在本期范围）

背景：见仓库根 `INCIDENT-2026-07-14.md`。Gauls 频道的复盘教学帖被 cron session 误读为操作指令，平掉了归属舒琴频道的 BTC 多单。三层根因：①复盘帖被当成指令；②算法止损成交从不回写频道上下文；③close 无任何归属校验。本方案覆盖除"子账户物理隔离"外的全部修复项。

## 线上事实（2026-07-14 核实）

- 控制面：hk `/srv/trader-v3/services/control-plane/api/read_api.py`（2113 行单文件，uvicorn 127.0.0.1:8080，热补丁演化，14 个 .bak；部署基线 git 6bf052a2 + 手工补丁，`cancel_order` 里已有 "adversarial review P1-1" 加固痕迹）。
- 写路径：`POST /v1/operator/orders`（read_api.py:1792）。close_position/partial_close 校验仅有：`require_reader → risk_admin`、symbol 格式、reason 非空。**无归属、无数量、无 message-type 校验**。
- 客户端：`/srv/hermes/profiles/trader/skills/trading/v3-trader/scripts/v3_trade.py`，close 只传 action/symbol/reason（+side/account）。open 已强制 `client_ref`（幂等）。
- 归属数据链（已存在，未参与运行时决策）：`trade_intents.hermes_decision_id → hermes_decisions → raw_messages.channel_id`；open 的 `client_ref`（`tg-<消息id>` / `verbal-*`）派生 `idempotency_key`（sha256，64hex，具体派生函数需在实现时核对 read_api.py）。
- feeder：`/srv/trader-v3/scripts/hermes_signal_feeder.py`，PROMPT_TEMPLATE 注入 `channel_name (channel_id)` 与 channel_ctx 尾部；channel_ctx 是 session 自觉回写的 markdown（根因②）。
- 系统单号格式：`B<32hex><2位序号>`，32hex 即 intent uuid（cancel_order 已用它做所有权证明）。
- 双账户 account-a/b 已存在（双节点双 key），默认 account-a。hedge mode 开启：同币种多空可并存，但**同向仓位跨频道在币安层面合并为同一仓位对象**——归属只存在于系统记账层。
- SKILL.md 线上版与仓库 hermes-profile 镜像仅差一处 stance 枚举说明；事故报告选项 A（软防规则）**尚未执行**。
- DB：postgres（127.0.0.1:5432/trader），表含 trade_intents / hermes_decisions / raw_messages / execution_events / positions_projection / orders_projection / exchange_state_mirror / operator_commands 等。
- 事故报告中的孤儿/危险挂单清单截至报告时未清理（用户要求亲自核对后再撤）。

## 工作块

### W0 — 孤儿单核对与清理（运维，人工确认闸，立即）

1. 重跑 `v3_query reconcile` + `orders`，生成**当前时点**的孤儿/超量/过期挂单清单（事故表可能已过时）。
2. 呈给用户逐类确认；确认后用 `v3_trade cancel`（仅系统格式单号可撤）逐张撤。
3. 名义 4.4 万 U 的 `aos_coin_` 手工限价买单是外部单，系统撤不了：请用户确认是否本人所挂，非本人则在币安 app 亲自撤。
4. 不写代码，不自动化；清单与撤单结果记入 ops 记录。

### W1 — 软防：SKILL.md + feeder 模板硬规则（立即，改文件即生效）

SKILL.md 铁律新增三条（同步提交仓库镜像 `hermes-profile/skills/trading/v3-trader/SKILL.md`）：

1. **复盘/总结类消息零动作**：消息主体是已实现盈亏回顾、经验教训、策略复盘（无新的带点位的操作指令）→ 一律不产生任何交易动作，回复"🔕 复盘帖，不操作"，只记上下文。
2. **动仓前先验归属**：任何 close/partial/set-sl/set-tps 前必须确认目标仓位归属频道（positions 输出带归属后一跳可得，W3 前用 `v3_query intents --symbol` + `intent <id>` 反查）；归属非本频道 → 不动，回复中写明"该仓归属 XX 频道"。
3. **close/partial 必须带 --entry-ref**（对应入场时的 `tg-<消息id>` / `verbal-*` ref）；查不到入场 ref → 不平，转人工确认。

feeder PROMPT_TEMPLATE 增加频道纪律声明："你的频道身份是 {channel_id}，只允许管理入场归属为本频道的仓位；平仓/减仓命令必须带 --channel {channel_id} 与 --entry-ref"。

部署：SKILL.md 改完即时生效（per-session 加载）；feeder 改模板需重启（cursor 续跑安全，有先例）。

### W2 — 服务端归属闸门（核心，确定性代码，1-2 天）

**目标不变量：任何管理类动作必须 trace 到一条本频道（或 operator）的入场 intent，服务端强制。**

v3_trade.py：

- close/partial/set-sl/set-tps/cancel 新增 `--channel <id>`（必填）与 `--entry-ref <ref>`（close/partial 必填）；payload 加 `channel` / `entry_ref` 字段。
- 错误回显对 LLM 友好（错误信息里写"该仓归属频道 X（入场 ref Y）"，session 可直接转告用户）。

read_api.py `operator_order`：

- 管理类 action（close_position/partial_close/move_stop_loss/replace_take_profits/cancel_order）：`channel` 必填。
- close_position/partial_close：`entry_ref` 必填 → 解析入场 intent（idempotency_key 反查或 `tg-` 前缀直查 raw_messages）→ 校验：
  - intent 存在且 action=open_position，否则 400；
  - intent.instrument_id 与请求 symbol 一致，否则 400；
  - intent 归属频道（hermes_decisions→raw_messages.channel_id；verbal ref 归 operator）== 请求 channel，否则 **403**，detail 写明实际归属；
  - partial_close 数量 ≤ 该 entry 的未平数量（若运行时可算；T0.5 允许先跳过数量校验，W3 落地后补强）。
- `channel == "operator"`（用户口头指令）：跳过归属拒绝，但 response 强制附 attribution（平的是谁的仓），供 session 回告用户。
- move_stop_loss/replace_take_profits/cancel_order：channel 必填 + 归属校验（cancel 已有 intent 所有权证明，补频道比对）。
- 兼容性开关：`OPERATOR_ATTRIBUTION_ENFORCE=1` 环境变量控制强制/仅告警（灰度一天再强制）。
- 无 ref 的历史仓位 / 外部手动仓位：close 时查不到入场 intent → 归 operator 频道专属（只有 operator 能平），错误信息引导。

部署：live 热补丁（.bak-20260714-attribution 惯例）+ uvicorn 重启（秒级，session 调用失败自动重试幂等安全）；同一 patch 镜像提交回 git（work/hermes-data-v3 分支 services/control-plane/api/ 对应文件 + hermes-profile 脚本镜像）。回滚 = 恢复 .bak + 重启。

### W3 — 读路径归属：positions 输出带归属（W2 后，2-3 天）

- `/v1/positions`（或 snapshot 组装处）每个仓位附 `attribution: [{channel, entry_ref, intent_id8, filled_qty, opened_at}]`：从 execution_events 的入场 fill + trade_intents 现算（P0-4c episode 算法的运行时简化版：per (account,instrument,position_side) 净额切分）。
- `v3_query positions` 透传展示。W1 的"动仓前验归属"从多跳反查变一跳。
- 同向多频道并存时列出多条 attribution 及各自数量——这正是事故场景，必须显式可见。

### W4 — 算法单成交回写 + channel_ctx 事实区（与 W3 并行）

- 新增（或扩展 order_lifecycle_monitor.py）一个 fill watcher：消费 execution_events + exchange_state_mirror diff，检测**算法 SL/TP 成交、外部/手动平仓**（仓位减少但无对应系统 close intent）→ 定位归属频道（W3 的归属算法）→ append 到该频道 channel_ctx：`[系统事实 <UTC时间>] BTC 多 0.039 已被算法止损平出 @61xxx（入场 ref tg-5019）`。
- channel_ctx 文件增加机器生成的 `## 系统持仓事实（自动维护，勿手改）` 小节：feeder 注入前从 DB 现算刷新该节（当前 episode、剩余数量、保护单状态）；session 手写部分保留但仅作定性参考。
- 这是根因②的根治：状态由交易事件驱动，不再依赖 session 自觉。

### W5 — 保护单 reaper + TTL 清理修复（风险最高，干跑闸，1 周内）

- 仓位减少事件后：重算该 (account,symbol,position_side) 应挂的保护单数量；超量/无主的**系统格式**挂单（B 前缀）自动撤。外部/手动单绝不碰。
- 修复 48h TTL 过期挂单清理器（事故：过期 29h 未清）。
- **分级放行**：先 dry-run 模式只记日志 + 每日汇总 3 天，人工核对无误杀后再打开自动撤。开关环境变量控制。

### W6 — 日报投递回执（独立小活，随时插队）

- gateway 投递成功后落 telegram message_id 回执（journal + jobs.json `last_delivery_message_id`），消除"发没发靠猜"。

## 顺序与依赖

W0、W1 立即（W0 等用户确认后撤单）；W2 次日上（先 warn-only 灰度一天再 enforce）；W3 → W4 → W5 一周内串行（W3 的归属算法是 W4/W5 的依赖）；W6 插队。

## 不做什么（本期边界）

- 子账户/多账户按频道路由：用户明确本期不做。
- clientOrderId 频道编码：要动节点容器内执行路径（intent_execution_strategy 等 .fixed 热补丁文件），风险收益比本期不合算，W3 的 DB 侧归属已覆盖读路径需求，推后。
- user data stream（listenKey WebSocket）：45s REST 镜像 + W4 watcher 已覆盖检测需求，实时性收益本期不必须，推后。
- 控制面单文件 read_api.py 的架构重构：只加闸门，不重构。

## 已知风险与开放问题（请对抗审查重点打）

1. entry_ref 强制是否卡死正常流：无 ref 历史仓位、外部开仓、多笔入场（-e1/-e2 后缀）同仓位选哪个 ref、set-tps 重整时 ref 传哪笔。
2. idempotency_key 能否从 client_ref 反查（派生函数是否含其他字段）；tg- 前缀直查 raw_messages 的 channel 是否总能命中。
3. operator bypass 是否成为新逃逸口（session 谎报 --channel operator？prompt 注入的频道身份是否足够可靠）。
4. uvicorn 重启窗口对在途 intent / feeder batch 的影响。
5. feeder 重启的 cursor/幂等安全性（模板改动生效路径）。
6. W5 自动撤单误杀：episode 算法边界（净额切分在部分成交/多档 TP 下的正确性）、与节点自身保护单管理的竞态。
7. W4 写 channel_ctx 与 feeder append_channel_context 的并发写竞态。
8. 灰度开关 OPERATOR_ATTRIBUTION_ENFORCE 的默认值与观测指标（warn 日志落哪、怎么统计误拒率）。

---

# 修订 v2（2026-07-14，对抗审查后）

对抗审查报告：`docs/plans/2026-07-14-attribution-review.md`（10 P0 / 11 P1 / 2 P2）。全部 P0 采纳；W2-W5 按下列决策改版分阶段实施。W1 已于 2026-07-14 14:36 UTC 部署（SKILL.md 铁律 11/12 + feeder 模板纪律 7/8，feeder 已重启续跑）。

## 八项设计决策

1. **可信来源（对 P0-1/P1-2）**：open_position 新增可选 `source_channel`；服务端从 client_ref 确定性解析 `tg-sig-c<频道>-m<消息id>` 交叉校验，二者冲突 → 400；通过后把真实频道写入 operator raw_messages.channel_id（source 仍为 operator）。历史 intent 不做自动回填：归属解析优先用 intent 落库时的 channel_id，缺失时按 client_ref 解析；解析不出 → legacy/ambiguous，enforce 模式下转人工。禁止按 reason 文本回填。
2. **operator 分权（对 P0-2）**：本阶段接受"channel 自报"残余风险（结构上已阻断事故型失误：跨频道平仓需同时谎报 channel 并引用他频道 entry_ref）。缓解：bypass 使用 shadow 计数 + channel=operator 的管理动作响应强制回显被操作仓位归属。真正分权（feeder 注入 per-channel token / 独立 operator token）标记为 W2.5，依赖 gateway 改造，单独评估。
3. **lot 台账（对 P0-3/P0-8，W3 重构）**：事件化 lot ledger：`entry_intent_id, channel_id, account_id, symbol, position_side, opened_qty, closed_qty, reserved_qty, remaining_qty, attribution_status`；fill 按唯一 event id 消费；保护单 fill 回原 lot（client_order_id 内嵌 intent uuid）；外部变化进 `unattributed_qty`；守恒式 `sum(lot.remaining)+unattributed = exchange book` 定期对账。
4. **entry-scope 管理动作（对 P0-3/P0-4/P0-5）**：频道 close 由服务端改写为 quantity 明确的 reduce-only partial（quantity = lot remaining）；set-sl/set-tps 携带 target entry，保护单带 generation。W3 之前的近似（phase 2）：remaining ≈ 该 intent 入场成交 − 其自有保护单成交，clamp 到 book；shadow 期先验证该近似的准确率，不准不 enforce。account/symbol/position_side 一律从 entry intent 派生并断言一致（phase 1 即上）。
5. **预留协议（对 P0-6）**：reserved_qty + 节点执行前重验列 W3+；phase 1-2 不实现，shadow 记录"审批时数量 vs 镜像数量"偏差率来量化该竞态的真实频率。
6. **全写幂等（对 P0-7/P1-1）**：管理动作强制 client_ref；幂等键 v2 = `sha256("operator-v2|account|action|symbol|position_side|client_ref")`；open 保留旧公式（兼容历史 ref 的 replay 语义）。phase 1 即上。
7. **保护单同步收尾（对 P0-9/P0-10/P1-5）**：close/partial fill 确认后由 order_lifecycle_monitor 扩展逻辑撤该 scope 残余系统保护单；镜像读取必须同时覆盖 open_orders 与 algo_orders；`REAPER_MODE=off|observe|enforce` 默认 off，DB advisory lock 单实例，候选连续两轮稳定才动手，observe 候选持久化。W5 只处理异常漂移。
8. **灰度与回滚（对 P1-11）**：独立开关：provenance（默认开，纯增量）；`ATTRIBUTION_MODE=off|shadow|enforce`（默认 shadow）；entry-scoped close（默认 off）；REAPER_MODE（默认 off）。shadow 指标落 `/srv/trader-v3/logs/attribution-shadow.jsonl`（请求数、解析成功/legacy/跨频道候选/bypass/数量偏差）。回滚 = 开关归位 + .bak 恢复；CLI/SKILL 新字段全部向后兼容。

## 分阶段

- **Phase 1（立即，Codex 实现）**：read_api provenance 写入 + 管理动作幂等 v2 + shadow 归属校验与指标 + account/symbol/side 断言 + 响应 attribution 块；v3_trade 增加 `--channel`/`--entry-ref` 可选参数透传；测试（FastAPI TestClient + 假 DB）。部署后 shadow 观察 ≥1 天。
- **Phase 2（shadow 数据核验后）**：SKILL 铁律 13（强制 --entry-ref）+ feeder 参数指令 + ATTRIBUTION_MODE=enforce + 频道 close→entry-scoped partial（近似 remaining）。
- **Phase 3（W3-W5 重构版）**：lot ledger + 保护单 generation + 同步收尾 + reaper（off→observe→enforce）+ channel_ctx 机器事实 DB 化（P1-6/7：flock+原子替换，事实区独立注入）。
- **W6** 与 **运维事件→日报输入**（2026-07-14 日报 review 发现：日报无法感知事故标注）随 Phase 3 排期。

## P2 采纳说明

- P2-1（float 精度）：lot ledger 全链路 Decimal 字符串，phase 3 落地。
- P2-2（intent 前缀多匹配）：v3_query 多匹配时列候选并拒绝，随 phase 1 顺手修。
