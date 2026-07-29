# 订单管理状态机（冻结版 v1）— OM0-01

> 机器可读真相源：[`packages/contracts/v1/order_state.v1.json`](../../packages/contracts/v1/order_state.v1.json)
> 一致性校验：`tests/order_management/test_state_machines_v1.py`
> 对应 PLAN：`docs/order-management/PLAN.md` §5

本文件是状态机契约的人类可读镜像。**真相以 `order_state.v1.json` 为准**；二者由测试 `test_descriptor_matches_doc_states` 保证不漂移。所有 reducer（OM2）、执行作业生命周期（OM4）、命令编排（OM6）都必须以该 descriptor 为唯一来源。**未在转换表中列出、且不属于该域 `any_state_to` 中断目标的转换都是非法的，reducer 必须记录 anomaly**（PLAN §5.2）。

## 0. 与现有代码的对账（freeze + extend，不是平地造）

| 域 | 现状 | 本次冻结后的差异 | 由谁补齐 |
|---|---|---|---|
| order | `orders_projection.status` 是自由文本 `text`，默认 `submitted`；无转换校验 | 引入 14 态规范状态集 + 显式转换表 + 事件→态映射 | reducer 落在 OM2-02 |
| position | `positions_projection.status` 自由文本，默认 `open` | 引入 opening/open/reducing/closed/external/reconciliation_required | OM2-03 |
| intent | DB enum `trade_intent_status = {draft,approved,rejected,cancelled,expired}` | 规范生命周期从 `approved` 起，新增 dispatched/node_accepted/executing/completed/denied/failed/needs_review | **DB enum 扩展在 OM0-04** |
| command | `operator_commands` 状态 `{pending,acknowledged,partial,failed,completed}`；node ack `{pending,acked,failed}` | 规范新增 accepted/running/verifying/timed_out，并强制 cancel_all/close_all 在 `verifying` 通过交易所核验后才 `completed` | OM6-01 |
| 模式闸 | `risk_state` 模式 `{ACTIVE,REDUCING,HALTED}`；governor 用 `OPENING_ACTIONS` 拦截 | 形式化 permission_matrix，明确 HALTED/REDUCING 下减仓通道常开 | 写入契约，OM6-04 校验 |

> `draft` 是 Decision Gateway 的审批前态，**刻意不纳入**本生命周期（本生命周期从 `approved` 起）。

## 1. Intent 生命周期（PLAN §5.1）

```text
approved → dispatched → node_accepted → executing → completed
   │            │             │             │
   └────────────┴─────────────┴─────────────┴──→ {rejected | denied | failed | expired | cancelled | needs_review}
needs_review → dispatched (operator_resume) | cancelled (operator_cancel)
```

- 终态必须由**执行结果或显式失败**决定，不能仅因 cursor 推进。
- `needs_review` 是非终态隔离态，需人工/operator 处理。

## 2. Order 生命周期（PLAN §5.2）

```text
planned → pending_submit → submitted → accepted → working → partially_filled → filled
   │            │              │           │          │            │
 denied       failed        rejected    {cancelled/expired/pending_cancel→cancelled}
任意非终态 → lost（本地无法解释，触发对账；不代表交易所无此单）
```

事件→态映射（供 reducer 直接查表）：

| Nautilus 事件 | 目标态 | 说明 |
|---|---|---|
| OrderSubmitted | submitted | |
| OrderAccepted | accepted | |
| OrderFilled | derive | `leaves_qty<=0`(或 `filled_qty>=quantity`)→`filled`，否则 `partially_filled` |
| OrderCanceled | cancelled | |
| OrderRejected | rejected | |
| OrderExpired | expired | |
| OrderUpdated | self | replace/move-stop 确认，宏观态不变 |
| OrderPendingUpdate | self | 修改在途，宏观态不变 |
| OrderPendingCancel | pending_cancel | |

- **`lost`**：非终态，`unknown` 类，必须排期 reconciliation。它表示本地投影无法从事件流解释订单状态（事件缺口/乱序/丢 ack），**绝不**断言交易所没有这张单。

## 3. Position 生命周期（PLAN §5.3）

```text
opening → open ⇄ reducing → closed
            └───────────────→ closed (PositionClosed 直接全平)
external → open (adopt) | closed (close)
任意非终态 → reconciliation_required（检测到与交易所现实漂移）
```

- `PositionChanged` 的目标态由 reducer 依数量变化推导（向零减小→`reducing`，否则 `open`）。
- 仓位身份 = OM0-02 解析出的规范 `(account_id, instrument 规范键, position_id)`。

## 4. Command 生命周期（PLAN §5.4）

```text
requested → accepted → running → verifying → completed
                          │          │
                       {partial|failed|timed_out}   verifying→{partial|timed_out}
```

- **`cancel_all` / `close_all` 只有在 `verifying` 阶段交易所最终核验通过**（无残留工作单 / 持仓 flat）才能进入 `completed`。这是铁律级约束，`verifying` 态把它变成强制环节。
- 与现有 `operator_commands` 对账映射：`pending→requested`、`acknowledged→running`、`completed/partial/failed` 同名；新增 `accepted/verifying/timed_out` 由 OM6-01 落地。

## 5. HALTED / REDUCING 动作权限矩阵（PLAN §3 铁律 #4）

HALTED/REDUCING 阻止**新增风险**，但**减仓通道（cancel / partial_close / close / close_all）永远开放**。

| 动作 \ 模式 | ACTIVE | REDUCING | HALTED |
|---|:---:|:---:|:---:|
| open_position / add_position | ✅ | ⛔ | ⛔ |
| partial_close / close_position | ✅ | ✅ | ✅ |
| move_stop_loss / move_stop_to_entry / replace_take_profits | ✅ | ✅ | ✅ |
| cancel / cancel_all / close_all | ✅ | ✅ | ✅ |

该矩阵对账 `risk_state.VALID_MODES` 与 `governor.py` 的 `OPENING_ACTIONS` 闸，并由 OM6-04 在执行路径上强制。

## 6. 变更规则

- 本 descriptor 属于 `contracts-v1`，但**不是消息实例 schema**，因此不进入 `tests/contracts` 的 `.snapshot.json` 向后兼容快照门（那只覆盖 4 个消息 schema）。它的稳定性由 `tests/order_management/test_state_machines_v1.py` 的结构一致性 + 代码对账测试保证。
- 任何状态/转换增删都要：①改 `order_state.v1.json`；②同步本文件；③`test_state_machines_v1.py` 全绿；④在受影响里程碑任务的 evidence 里记命令与 commit SHA（铁律 #10）。
