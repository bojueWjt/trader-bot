# Zone Ladder 区间下单方案 v1（order_plan v1.1）

> 状态：计划定稿，待拆解实施 · 2026-07-02
> 决策人：Balen · 起草：Claude Code
> 依据：hk v3 库 49 条真实信号 × Binance 1m K线的区间刺入测算（见 §7）

## 1. 目标

把 zone 信号（`entry_price_min`/`entry_price_max`）从现行单档执行（2026-07-02 hotfix 后：近沿边界单张限价 + 成交后自动挂 SL/TP 保护，做空挂 `price_min`）升级为确定性的三档下单计划：追入窗口 + 区间内梯度挂单 + 机械失效规则。`{}`/v1.0 计划继续走现行单档路径，v1.1 是加法升级，迁移期两条路径并存。计划由 A 侧（决策网关）展开成含绝对价格的 `order_plan`，节点只做机械执行，**节点侧不得包含任何 zone 语义**（防止再次出现 A↔B 翻译层丢字段一类的 bug）。

## 2. 已定参数（用户拍板，实施时不得擅改）

| 参数 | 值 | 说明 |
|---|---|---|
| 追入窗口 | **0.35%** | 信号到达时现价距区间近沿 ≤0.35%（含已在区间内）→ 近沿档立即吃单 |
| T1 近沿档 | **55%** 权重，深度 0% | 大头放近沿；追入窗口命中时本档转为立即成交，否则挂近沿等待 |
| T2 中点档 | **30%** 权重，深度 50% | 区间中点 |
| T3 深位档 | **15%** 权重，深度 85% | 留最后 15% 当底部插针缓冲，不挂远沿 |

深度定义：long 以 `price_max` 为近沿（0%）、`price_min` 为远沿（100%）；short 镜像。权重为**风险权重**（见 §4 sizing），不是名义金额权重。

已知取舍（有意为之，写明留档）：17 样本测算显示深档成交率仅比近沿低 12pp 且价格/止损显著更优，前置权重是**参与优先**的选择——信号生效时尽量把仓位拿在手上，以浅档均价换参与度。§6 的打点数据积累到位后重新校准权重。

## 3. 执行方案

### 3.1 展开逻辑（网关侧纯函数）

```
expand_zone_to_plan(intent, market_snapshot) -> OrderPlan
```

以 long 为例（short 全部镜像）：

1. `premium = (p0 - zone_near) / zone_near`，p0 为决策时刻标记价/最新价。
2. **追入分支**：`premium ≤ 0.35%` → T1 以 marketable limit 立即下单，限价上限 `zone_near × 1.0035`（封住滑点，不用裸市价）；T2/T3 照常挂。
3. **常规分支**：`premium > 0.35%` → T1 挂近沿（post-only），T2 挂 50% 深度，T3 挂 85% 深度。
4. **Stale 分支**：
   - p0 已在区间内（0% < 深度 ≤ 100%）：T1 立即吃单（此时 premium ≤ 0 天然过窗口）；T2/T3 只挂在**现价以下**的既定深度档位，档位已在现价之上的取消该档、权重并入相邻更深档。
   - p0 已穿透区间（深度 > 100%）：不自动执行，转 `needs_review`（信号已失效或破位）。
5. **太窄保护**：区间高度 < 0.15%（≈两倍手续费+滑点）→ 塌缩为单张限价挂区间中点。
6. 产出的 `order_plan` 只含**绝对价格**（每档 price、SL、TP1、失效触发价），节点零区间语义。

### 3.2 机械失效规则（硬规则，不留裁量）

| 规则 | 触发 | 动作 |
|---|---|---|
| TP1 先到 | 任意档成交前 TP1 被触及 | 撤掉全部未成交档（信号已兑现，之后的回踩是另一个语境） |
| 击穿失效 | 15m 收盘价越过 `far_edge − 0.2 × zone_height` | 撤未成交档；已成交部分持有原 SL 不动 |
| TTL | `min(信号 valid_until, 48h)` 到期 | 撤未成交档 |
| Kill/HALT | 节点 HALTED 或 kill switch | 遵循现有 A-08 状态机语义，不新增规则 |

### 3.3 订单细节

- 挂单档 post-only（吃 maker 费率）；追入档 IOC/marketable limit 带价格上限。
- client order id 复用线上既有编码 `B{intent_uuid_hex}{seq:02d}`（35 字符，Binance clientOrderId 上限 36；`{intent_id}:{tranche_id}` 明文拼接 44+ 字符发不出去，弃用）。seq 段分配：**01–09 入场档**（t1=01, t2=02, t3=03）、**11–19 止损**（11=主 SL）、**21–29 止盈**（21=TP1…）。可反解 intent_id + 语义段，天然幂等，节点重启/重连后按此对账。tranche_id 仅存在于 order_plan 与打点事件，不进交易所。
- 双账户对冲模式：每个账户的 intent 各自独立展开三档，互不感知。

## 4. Sizing（风险归一）

每档名义 = `(risk_budget × tranche_risk_weight) / |tranche_price − sl_price|`，即每档亏到 SL 的美元损失 = 总风险预算 × 该档权重。深档止损距离短、同风险下名义更大，这是刻意保留的特性。全部名义受 governor `max_notional`、`max_leverage` 封顶，超限按比例缩减全部档位（不改相对权重）。

**Sizing 归属：网关。** 入场价、SL、risk_budget、governor 上限在展开时刻全部已知，`expand_zone_to_plan` 直接产出每档**绝对 quantity**（按合约 step size 量化），节点收到的计划零计算、纯执行——这才配得上"节点零区间语义"。`risk_weight` 保留在 schema 里仅作审计/回放用，节点不得据其计算任何数量。

## 5. order_plan v1.1 schema 草案

```jsonc
"order_plan": {
  "plan_version": "1.1",
  "policy": "zone-ladder-v1.0",          // 展开函数版本，回放/审计用
  "mode": "zone_ladder",                  // zone_ladder | single_limit | market
  "tranches": [
    { "tranche_id": "t1_near", "seq": 1, "price": 106200.0, "quantity": "0.014",
      "risk_weight": 0.55,                // 审计字段，节点不据此计算
      "style": "chase",                   // chase = 立即 marketable limit
      "limit_cap": 106571.7 },
    { "tranche_id": "t2_mid",  "seq": 2, "price": 105600.0, "quantity": "0.007", "risk_weight": 0.30, "style": "rest" },
    { "tranche_id": "t3_deep", "seq": 3, "price": 105180.0, "quantity": "0.004", "risk_weight": 0.15, "style": "rest" }
  ],
  "stop_loss": 104000.0,
  "take_profits": [108000.0, 109500.0, 111000.0],  // 成交后节点挂 reduce-only TP（沿用 07-02 hotfix 的 post-fill 保护路径，seq 21+）
  "invalidation": {
    "cancel_on_price_touch": 108000.0,    // = TP1，触及即撤未成交档
    "cancel_on_close_beyond": 104960.0,   // = far − 0.2h，15m 收盘确认
    "close_bar": "15m",
    "expires_at": "2026-07-04T12:00:00Z"
  }
}
```

兼容性约束：

- 形式合同（`packages/contracts/v1/approved_trade_intent.v1.json`）目前把 `order_plan` 锁死为 `{"additionalProperties": false, "properties": {}}`——**已与线上流量脱节**：07-02 hotfix 后 wire order_plan 实际携带 `type/side/quantity/price/price_min/price_max/time_in_force/stop_loss/take_profits/leverage`。单元 1 除新增 v1.1 外，**必须先把这些既成事实字段收编为 v1.0 合法字段**，否则 snapshot 检测会判现网流量非法。
- v1.1 为**加法变更**，`{}` 与 v1.0 字段集必须继续合法（节点解释为现行单档行为），过 `.snapshot.json` 向后不兼容检测。版本表达（单元 1 实施定）：信封 `schema_version` 保持 `const "1.0"` 不动，ladder 语义由 `order_plan.plan_version: "1.1"` 承载——动信封版本会波及全部消费方，加法变更不值得。
- 合同属 A/B 共享接口，**动 schema 前需与窗口 B/C 对齐**；examples 目录同步补 valid/invalid 样例。
- **seam 直通守卫**：control-plane `_execution_order_plan()` 的 B 格式直通分支现判 `type+quantity+buy/sell`，v1.1 计划（`mode/tranches` 结构）不满足会被误当 A 格式重新合成、字段全毁——与本方案要防的翻译层丢字段是同一类事故。必须加 `plan_version >= 1.1 → 原样直通` 守卫，进单元 2 验收。

## 6. 打点与再校准（方案的一部分，不是可选项）

每档全生命周期写 execution events：`tranche_placed / tranche_filled / tranche_cancelled`，携带：成交深度、追入溢价、撤单原因（tp1 / breach / ttl / stale_skip）、下单到成交时长。仪表盘出一列每档状态。

**再校准触发条件：累计 ≥50 条新鲜 zone 信号后**，重跑刺入统计 + 档位成交回放，重新评审三档权重与深度（尤其"前置 vs 压深"的取舍）、0.35% 窗口、`−0.2h` 失效缓冲。统计脚本随实施收编进 `scripts/analysis/zone_penetration_stats.py`（现版本在本机 scratchpad，K线缓存可复用）。

## 7. 数据依据摘要（2026-07-02 测算）

- 样本：hk v3 `hermes_decisions` 49 条带价信号（join `raw_messages` 取原始消息时间，跨 2 月+6 月）；有效 17 条新鲜 zone 信号 + 6 条 limit。
- **双峰分布**：41%（7/17）不回踩、TP1 先到（仅 1 笔差 0.03% 的真擦边，其余距区间 1%–6.7%）；一旦回踩（10/17），中位刺入 ~130%，8/10 打穿整个区间。
- 挂单深度 0%→100% 成交率 59%→47%；0% 与 25% 深度成交集合完全相同。
- 0.35% 追入窗口按 17 样本回放约命中 3–5 笔，能捞回 7 笔 runaway 中的 1 笔；其余 6 笔在该约束下数学上不可参与（最近逼近 1.05%–6.7%）。
- 中位区间高度 1.18% price / 1.42 ATR(1h)。
- **样本警告**：n=17、BTC 占大头、同日多信号相关、2 月样本来自 corpus 回放。结构可信，参数是起点。

## 8. 实施拆分（Codex 派发单元）

状态标记（2026-07-02）：单元 1 ✅ 完成验收（合同扩字段 + v1.1 ladder schema + examples，contracts 测试 7/7 绿，待跨窗口签字）；单元 2 ✅ 完成验收（`zone_ladder.py` 纯函数 + 22 测试绿，含舒琴 BTC/SOL 真信号回归、合同校验、决定论）；单元 5 ✅ 完成验收（`scripts/analysis/zone_penetration_stats.py` + 15 确定性测试 + 再校准 runbook，纯核心离线可测，live 复现 §7 待对 hk 库单跑）。**单元 3（节点执行器，跨窗口 B/C worktree + 依赖 OM）与网关接线均阻塞：需用户定 zone 下注规模（风险比例×权益 vs 固定 notional）+ 协调跨窗口。** 单元 4（打点+面板）依赖单元 3 先发事件。

| # | 单元 | 范围 | 验收 |
|---|---|---|---|
| 1 | 合同 v1.1 | `packages/contracts/v1/`：先收编 v1.0 既成事实字段（见 §5 兼容性约束第一条），再加 v1.1 ladder schema + valid/invalid examples + snapshot 检测通过 | `{}`、v1.0 字段集、v1.1 三者皆合法；现网真实 wire 样本作为 valid example 过校验；跨窗口评审签字 |
| 2 | 网关展开函数 | `services/control-plane/decision_gateway/`：`expand_zone_to_plan` 纯函数（产出含绝对 quantity，见 §4）+ 配置化参数 + seam v1.1 直通守卫 | 单测覆盖 §3 全部分支（追入/常规/stale×2/太窄/short 镜像）；17 样本信号作为固定 fixture 的展开回归；`_execution_order_plan` 对 v1.1 计划原样直通有测试 |
| 3 | 节点执行器 | 节点侧（窗口 B/C worktree）：多档下单、三条失效规则、幂等对账、HALTED 交互 | 双档部分成交、重启恢复、TP1/击穿/TTL 撤单各有测试；testnet 全流程一遍 |
| 4 | 打点+仪表盘 | execution events 新事件类型 + 面板每档状态列 | 回放一条信号能在面板看到三档生命周期 |
| 5 | 统计脚本收编 | `scripts/analysis/zone_penetration_stats.py` + 再校准 runbook | 从 v3 库全量重跑可复现 §7 数字 |

依赖与顺序：1 → 2/3 并行 → 4/5。单元 3 依赖 OM worktree 的订单管理能力（work/order-management-v3，70/73 done）。

## 9. 风险与未决

- **合同变更跨窗口**：单元 1 未对齐前，2/3 不得合入主干。
- **前置权重与数据的张力**：已在 §2 留档，§6 校准点强制复审。
- **Phase 2（反转金K确认模式）**：捕获其余 6/7 runaway 的唯一健全路径——区间上方回调不触区时，低周期反转结构进场、止损放确认结构下方（而非远处区间外）。需要低周期行情流 + armed/triggered 状态机 + 先行统计"回调不触区案例的反转结构出现率"。**明确不进本期范围**，等 ladder 打点数据积累后单独立项。**适用范围（Balen 07-02 定）：反转金K确认模式用于指定明确入场点的单子**（信号给出具体入场价/limit 类），zone 信号本期一律走 ladder，不做金K确认。
- 当前线上为 Hermes 大脑模式（v3 auto 停用）：展开函数设计为纯函数，operator 通路（Hermes 下 zone 指令）与恢复后的 auto 通路共用同一模块。
