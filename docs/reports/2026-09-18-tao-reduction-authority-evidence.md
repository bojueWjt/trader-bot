# 2026-09-18 TAO 减仓授权 scope 证据（Codex 验收 FAIL 后）

> 历史调查记录：下文“当前 patch / 未实现”的表述对应 B 方案实施前。最终修复与验证见 [修复交付记录](2026-09-18-close-reduction-fix.md)。生产证据和历史缺口仍有效。

- 工作树：`/Users/balen/projects/trader-bot/.worktrees/tao-reduction-authority-20260918`
- 分支：`codex/tao-reduction-authority-20260918`（HEAD `bc0d22a`，跟踪 `origin/codex/order-lifecycle-mobile-fix-20260917`）
- 生产：只读。本报告不构成已部署。
- 结论先行：**当前本地 patch 用全账户同向 mirror 算 `--percent`（9.890×30%=2.967），不满足「百分比作用于本授权可管理仓」。现有 HTTP 接口不暴露 robot-owned / channel-owned。节点 `fraction` 路径也不是 `owned×fraction`。该 intent 当前 stash 无 TAO 行，不能把「robot_owned<0.092」写成已查实拒因。**

## 1. 当前 `v3_trade.py` 实际 diff

`git diff -- hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py`（worktree 相对 `bc0d22a`）。

必要 hunks：

1. `--percent` 改为读 `exchange_state_mirror`（`_venue_reduce_position`），不再用 `_position_for` → `positions_projection`。
2. `Decimal` `ROUND_DOWN` 到 `quantity_step`。
3. stale / 缺仓 / 缺 step 拒绝。
4. **`--quantity` 在能取到 mirror step 时也会被静默向下量化（0.0924→0.092）。这违反「绝对 quantity 保持旧语义」，验收应打回，未再扩大修改。**

完整 diff 见本 worktree 未提交改动；stat：`v3_trade.py` +128/-部分，连同测试与 SKILL 共 5 文件 +631/-37。

测试证据（本地 `.venv-arch`）：

```text
cd /Users/balen/projects/trader-bot/.worktrees/tao-reduction-authority-20260918
/Users/balen/projects/trader-bot/.venv-arch/bin/python -m pytest \
  tests/test_v3_trade_routing.py \
  tests/execution/manage/test_intent_execution_planner_manage.py \
  -q
# 103 passed, 4 subtests passed in 5.61s  exit 0
```

红灯复现（修 CLI 前）：`partial --percent 30` 对冲突 fixture 打出 `0.0924`（投影 0.308）而不是 mirror `2.967`。

`tests/execution/manage/test_hedge_reduce_only_submission.py` 新增节点授权用例：**本机无 `nautilus_trader`，未跑。**

## 2. 节点授权核心函数（bc0d22a 源，与任务指定修复分支同 commit）

文件：`services/nautilus-node/strategy/intent_execution_strategy.py`

### 2.1 `_robot_owned_position_quantity` L2736–2788

输入：

| 参数 | 来源 |
|---|---|
| `instrument_id` | 本笔 `OrderPlan.instrument_id` |
| `position_side` | 由订单 side 推出：SELL→LONG，BUY→SHORT（`_plan_for_submission` L9383） |
| `account_quantity` | `assessment.quantity`（reconciled **venue 整簿**，`_build_reconciled_state`） |
| stash 行 | 进程内 `self._entry_protection_stash`，落盘 `NODE_STATE_DIR/protection_stash.json`（默认 `/state/protection_stash.json`；jp-24 主机映射 `/srv/trader-v3/node-state/{a,b,c,d}/protection_stash.json`） |

算法：遍历 **全部** stash；只过滤 `instrument_id` 与 entry_side→LONG/SHORT。**不按 `authorized_by_id` / channel 过滤。** owned 取 `batch_fills`→`owned_quantity`，否则 `protected_quantity`，否则同 intent 的机器人 `filled_qty`。最后 `min(sum(owned), venue_qty)`。

`protected_quantity is None` 且无 batch_fills 时走 fill 回退，**不是把整簿当 owned**。

### 2.2 channel vs user（`_plan_for_submission` L9359–9461）

- user：`limit = assessment.quantity`（venue 整簿）L9421–9422
- **否则（channel）**：`limit = Decimal(_robot_owned_position_quantity(...))` L9423–9424
- 若 order_plan 有 `quantity`：`limit = min(limit, approved_qty)` L9425–9429
- elif 有 `fraction`：`limit = min(limit, assessment.quantity * fraction)` L9430–9434  
  这里的 `assessment.quantity` 仍是 **venue 整簿**，不是 owned×fraction。

最后 `if quantity > limit: OrderDenied("reduction_quantity_exceeds_authority", plan.quantity)` L9449–9450。  
**detail 是 plan.quantity，不是 limit。** 本 intent 的 `0.092` 只能证明计划数量，不能证明授权上限。

inbox 记录来自 `_intent_execution_inbox.get_by_client_order_id`；`raw = record.intent_payload["order_plan"]`（控制面批准时写入的 order_plan，含 authorization/quantity）。

### 2.3 channel scope 解析

`_stash_protection_authorization` L11768–11776：从 stash 的 TP/SL authorization 或 `entry_tags` 还原 `{authorized_by_type,id,source_message_id,parent_intent_id}`。  
该函数用于 **无 inbox 记录的保护单路径**（L9438–9448 要 stash 授权匹配）。  
**有 inbox 的 operator partial_close 不算 per-channel owned**：channel 分支直接 sum 该品种该向全部 robot stash。

## 3. 该 intent 的 stash / robot_owned：**当前盘没有 TAO 行**

intent `25da8016-b5bd-47a0-9c93-27578afb9d1a` 库内授权（已查，不重复巡检）：

- channel `-1002189417451`，`quantity=0.0924`，无 fraction
- attribution `resolution=intent`，`owner_channel=-1002189417451`，`channel_match=true`
- entry_ref `tg-sig-c1002189417451-m6894`

只读 stash（上海 2026-09-18 21:13）：

| 项 | 值 |
|---|---|
| 路径 | `/srv/trader-v3/node-state/c/protection_stash.json` |
| mtime | **2026-09-18 20:28:02 +0800**（拒单 11:14 之后被改写） |
| key 数 | 8 |
| `25da8016…` | **无** |
| `2adb3d10…`（9/13 TAO 开仓） | **无** |
| `instrument_id` 含 TAO | **`TAO_STASH_ROWS []`** |
| 现有 8 行 | DOGE/PUMP/XAU/BNB/BTC/XRP/PAXG/ASTER，channel 均为 `-1002189417451`；若干 `protected_quantity` 为 null |

**缺口：** 没有 11:14 时点的 stash 快照，无法重算当时 `_robot_owned_position_quantity(TAOUSDT, LONG, venue)`。  
**不得**再把「若 robot_owned<0.092」写成已核实拒因。只能说：按现码，detail=`plan.quantity`；limit 当时未落审计。

## 4. 现有 HTTP 是否暴露 owned qty：**没有**

| 接口 | 返回仓位字段 | robot/channel owned |
|---|---|---|
| `GET /v1/mirror/positions` `v1_mirror.py` L98–108 | symbol, position_side, quantity, prices, leverage, quantity_step, min_quantity, protection | **无** |
| `GET /v1/positions` `read_api.py` L5311–5332 | venue 整簿 + projection 注释：position_id, quantity/size, signal_id/intent_id, protection | **无 owned** |
| `GET /api/system/snapshot` | `positions`=positions_projection；`exchange_state`=mirror 原样 payload | **无 owned** |
| `GET /v1/query/{resource}` | channels/orders/intents/fills/outcomes… | **无 owned resource** |
| `GET /v1/nodes/{id}/exchange-state` | 单账户 mirror 行 | 节点对账用，**无 owned** |

`load_robot_owned_balance`（`ownership_ledger.py` L75–101）只给控制面 **RESUME/平仓锚点**（`_robot_owned_symbol_footprint` `read_api.py` L2335），按机器人 `client_order_id ~ ^B[0-9a-f]{32}[0-9]{2}$` 成交合计。**不是 GET 字段，也不是 per-channel。**

## 5. 现有 `fraction` 能否当「按 scope 百分比」：**不能直接复用**

Planner `_resolve_exit_quantity` L526–532：

```python
if has_fraction:
    return Decimal(str(position.quantity)) * fraction
```

`position.quantity` 是 planner 的 **仓位快照（整簿）**，不是 robot_owned。  
节点随后用 `min(robot_owned, venue*fraction)` **卡上限**，订单数量仍可能先按整簿×fraction 编出来，再在 `_plan_for_submission` 被拒。

这与「CLI 先算 2.967 再等节点拒绝」是同一错误形状。

真要满足 Codex：**qty = ROUND_DOWN(scope_owned × percent/100, step)**，且 **qty ≤ 同向 venue**。scope_owned 对 channel 必须是可管理仓（至少 robot-owned；若还要 per-channel，现码 `_robot_owned_position_quantity` 也没按 channel 切）。

## 6. NEEDS_CODEX（不擅自加接口、不扩本地补丁）

**不部署。** 本地 patch 另外两项也要在裁决里处理：

1. `--percent` 用全账户 mirror → 2.967（验收 FAIL）。
2. `--quantity` 有 step 时静默 0.0924→0.092（破坏绝对数量旧语义）。

候选（需你点头，均不重放 `25da8016`）：

**A. 最小只读 owned-qty（推荐先定语义再动）**  
在现有 `GET /v1/mirror/positions` 或 `GET /v1/positions` **加字段**（这是契约变更）：  
`robot_owned_quantity` + `venue_quantity` + `scope`。  
实现源二选一并写进契约：节点 stash 实时 owned，或 CP `load_robot_owned_balance`（机器人成交账，非 stash）。  
Channel CLI：`percent` 只乘 **scope owned**，mirror 只做 `min(..., venue)`。缺 owned → fail closed。

**B. 改节点 fraction 真正按 scope 定量**  
inbox 的 channel+`fraction`：计划数量 = `ROUND_DOWN(robot_owned * fraction, step)`，不再 `position.quantity * fraction`。  
CLI 对 channel `--percent` 只传 `fraction=0.3`、不传绝对 qty。  
仍缺 per-channel 切片；且 fraction 目前若与 quantity 同时存在会被 planner 拒绝。

**C. 不新增 API、CLI 对 channel `--percent` fail closed**  
直到 A 或 B 落地。避免再提交 2.967。user `--percent` 才可用整簿（与节点 user limit=venue 一致）。

交易节点：A 若只读 stash 需节点或 state 文件查询通道；B **必须换节点版**。仅改 Hermes CLI **不够**。

回滚范围若误部署：只回 Hermes `v3_trade.py` + `SKILL.md`。本次未部署。
