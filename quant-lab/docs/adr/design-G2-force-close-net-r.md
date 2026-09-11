# `force_close_net_R` 设计（G0 B19 裁定落地规格）

> 状态：**设计定稿，待十一审回执后实现**（十一审 `task-mtwhxe7y-rc756b` 正在做审前/审后 SHA256 比对，此刻改 `src/` 会毁其证据同一性）。
> 上位裁定：B18（P1 主口径定 B、A 作强制敏感性）、**B19（采纳选项一：唯一实现，禁止跨窗口重实现）**、A24 跨窗口补充（必须 import、属主登记列跨窗口调用方、消费方以哨兵证明"是调用不是自算"）。

## 1. 这个函数为什么是这个形状

G0 B19 的决定性理由不是成本，是语义：

> **B 口径的"观察终点强制平仓"是一个研究估值约定，不是一件发生过的事。** 仓位在世界上从未平掉，我们只是按 `horizon_end` 的 mark 给余仓记了个价。把它实现成内核的平仓腿，等于**把一个记账约定伪装成一个发生过的动作**。

这直接决定三件事：**不提前 C06**；函数**只读** `ExecutionResult` 不改它；产出是**研究层派生量**，与执行记录并列而不覆盖它。执行记录必须继续如实说"我们没看到它平仓"。

## 2. 签名与不变量

```python
def force_close_net_R(
    res: ExecutionResult,
    *,
    mark: Decimal,                 # horizon_end 处最后一根**已闭合** mark bar 的收盘价
    mark_at: dt.datetime,          # 该 bar 的 open_time（证据出处，不是 horizon_end）
    mark_source: str,              # manifest_id / 显式点，供追溯
    policy: ExecutionPolicy,
    side: Side,
    multiplier: Decimal,
) -> ForceCloseValuation | None      # 无余仓 → None（不是 0：0 会与"平了但不赚不亏"混淆）
```

**硬约束（B19 三条，逐条对应断言）**

| 约束 | 实现方式 | 回归 |
|---|---|---|
| 不得产生 `canonical_events` | 函数不接受也不返回事件；`res` 是 frozen 模型，无写路径 | 断言调用前后 `res.canonical_events` 对象同一且长度不变 |
| 不得改 `fill_status` / `outcome_kind` / `censor_reason` | 同上；返回独立数据类 | 断言三者调用前后逐字相等，且 `outcome_kind(res)` 仍为 `right_censored` |
| 不得覆写 `net_R` | 结果落 `net_R_forced`，与 `net_R` 并存 | 断言 `res.net_R is None`（删失样本）而 `val.net_R_forced is not None` |

返回体带齐溯源，使任何 θ 声明都能自证口径（B18 §2「任何 θ 声明必须写明所用 estimand」）：

```python
@dataclass(frozen=True)
class ForceCloseValuation:
    net_R_forced: Decimal
    estimand: Literal["forced_close"]      # 固定值，不是可选项——写死才能防止口径漂移
    mark: Decimal
    mark_at: dt.datetime
    mark_source: str
    residual_qty: Decimal                  # 诊断：被估值的余仓
    close_fee: Decimal                     # 诊断：余仓平仓费
```

## 3. 五项记账（G0 原样记进契约的那句：任一项在第二份实现里写错都会安静地改变 θ）

```
residual      = res.filled_qty − Σ qty(出场成交事件)          # 出场腿 ∈ {sl, tp, close}，kind ∈ {filled, partial_fill}
sign          = +1 if side == "long" else −1
unrealized    = (mark − res.entry_avg_price) × residual × sign × multiplier
close_fee     = mark × residual × policy.cost(scenario).taker_fee × multiplier
net_forced    = res.gross_pnl + unrealized − res.fees − close_fee + res.funding
net_R_forced  = quantize_ratio(net_forced / risk_budget)
```

1. **已实现部分** = `res.gross_pnl`（多档 TP 可能已部分平仓，这部分是真发生的）。
2. **余仓方向与数量** = `residual` × `sign`，由事件推出而非猜测。
3. **`horizon_end` 处最后一根已闭合 mark** —— 由调用方按 as-of 语义取得并连同 `mark_at` 传入；**函数不自己找行情**，因此不可能取到未来价，取价责任与证据一并显式化。
4. **余仓 taker 费** = `close_fee`，不计入等于给强平口径一个免费的出场，会系统性抬高 θ。
5. **累计费与资金费** = `res.fees` / `res.funding`，取删失时点的累计值（删失发生在 `horizon_end`，故已覆盖整个观察窗）。

## 4. 单一来源与跨窗口（A24 补充）

- **属主**：G2，函数落 `quant_lab.market.contract`（与 `derived_t_start` 等同一处），进 `__all__`。
- **调用点登记**：`ALLOWED_CALLERS["force_close_net_R"]` 必须列出**跨窗口**调用方（G3 的调用位置），不只列 market 内部。
- **消费方义务**：G3 以**哨兵行为**证明"是调用不是自算"——patch 掉本函数使其返回哨兵值，断言 G3 的 θ 随之改变。仅断言"调用发生了"不合格（S39 的形态）。
- **禁令**：树级 lint 增一条，禁止在本函数之外出现 `(mark − *.entry_avg_price)` 形态的余仓估值表达式。

## 5. 不做什么

- **不实现 C06**。政策平仓腿是 P2，且按 B19 它与本函数是两件不同的事：C06 是内核真的平了仓（产生事件、`filled_closed`），本函数是研究层给未平的仓记价（不产生事件、仍是 `right_censored`）。**两者将来并存，不互相取代。**
- **不改 `right_censored` 的可达性**。G0 R47 已自我更正：两条路径（`kernel_a.py:618` 的 `hold_end`、`:621/:644` 的 `horizon`）在 B 下**仍然全部可达**，B 改变的只是研究层不再丢弃这些 episode。两条路径的可达性**分别断言**，不合并。
- **不替 G3 选 estimand**。函数只提供 B 口径的值；A 口径是"不调用本函数"，无需额外实现。

## 6. 实现后必须自证的突变（A24 §12.1 + A26）

| 注入 | 期望 |
|---|---|
| 去掉 `close_fee` 项 | 回归 RED（θ 会被系统性抬高） |
| `sign` 写反 | RED |
| `mark` 改用 `horizon_end` 之后的 bar（取未来价） | RED——由调用方侧的 as-of 断言捕获 |
| 漏计 `res.funding` | RED |
| 漏计 `res.gross_pnl`（只算余仓浮盈） | RED |
| 函数被绕过、调用方自算 | 调用点登记 RED + 树级禁令 RED |
| 函数写回 `res.net_R` | 不变量断言 RED |

若某条注入后仍绿，按 **A26** 先做叠加突变——破坏该项所守护的性质本身，看回归是否变红——再判它是冗余防御还是真正无法失败的检查。
