# G2 → G0：ExecutionResult 终态 → `reconstructed_outcome.kind` 七值映射（A15 落地规格）

> 状态：**G0 已裁定**（契约 v1.5 §9.10.11 A15：五值作废，七值为准；A 准、B 准、C 采纳保守提案并增 `exit_legs` 诊断列）。本文是落地规格 + 供 G0 粘贴进 `execution-interface.md` §3 的映射表原文。
> 代码落地排在 Codex 写任务 task-mtwc80bl-dwasc4 之后（避免与其并发改 `src/quant_lab/market/` 撞写）。

## 1. 判定顺序（首条命中即取值）

| # | ExecutionResult 条件 | kind |
|---|---|---|
| 1 | `censor_reason == "LABEL_RIGHT_CENSORED"` | `right_censored` |
| 2 | `censor_reason ∈ {MARK_STALE, BAR_GAP, FUNDING_SCHEDULE_GAP, RULE_HISTORY_MISSING, SYMBOL_TIME_INVALID}` | `unevaluable` |
| 3 | `fill_status == "none"` 且事件含 `rejected` | `rejected` |
| 4 | `fill_status == "none"` 且事件含 `expired` | `unfilled_expired` |
| 5 | 有 `closed`，且出场成交的 `leg` 含 `sl` | `stopped` |
| 6 | 有 `closed`，且出场成交的 `leg` 非空且全为 `tp` | `tp_hit` |
| 7 | 有 `closed` 且 `filled_qty > 0`（其余） | `filled_closed` |

- 第 1/2 条优先于成交形态：**先回答"这条样本能不能评"，再回答"它怎么结束"**。删失原因分两类是 A15 的核心——`right_censored` 是"数据齐全、标签未成熟"（关于世界），`unevaluable` 是"看世界所需的数据缺了"（关于我们）。
- 第 3 条先于第 4 条：`rejected` 是"根本没挂出去"（计划质量/账户约束），`unfilled_expired` 是"挂了但市场没来"（策略信息）。拒因细分不扩枚举，读 `canonical_events` 里 `rejected.reason`（PRICE_FILTER / LOT_SIZE / MIN_NOTIONAL / MARGIN / POST_ONLY_CROSS）。
- 第 5 条先于第 6 条：止损参与即 `stopped`（保守，标签偏向不利结局）。混合出场不丢信息——见 `exit_legs`。
- 任何结果必落且只落一条；`outcome_kind` 对同一 `ExecutionResult` 是纯函数，不读行情。

## 2. `exit_legs` 诊断列

出场实际参与的 `leg` 集合，去重后按固定顺序（`sl` < `tp` < `close`）排序的元组，只统计 `kind ∈ {filled, partial_fill}` 且 `leg ∈ {sl, tp, close}` 的事件：

| 场景 | `outcome_kind` | `exit_legs` |
|---|---|---|
| 纯止损平仓（E01/E02/E04b） | `stopped` | `("sl",)` |
| 先部分 TP、余仓止损（E12） | `stopped` | `("sl","tp")` |
| 多档 TP 全部平掉 | `tp_hit` | `("tp",)` |
| 未成交 / 被拒 / 删失 | 相应值 | `()` |

这样 `{sl}` 与 `{sl,tp}` 可分，混合出场不必再扩枚举（G0 裁定 C）。

## 3. v0 的可达性说明（A15 要求"不可达值在报告里说明原因"）

当前 v0 执行内核**没有政策平仓腿**（`leg="close"` 的成交，ADR-G2 C06 的 `max_holding` 强制平仓留 P2），出场只可能来自 SL 或 TP，因此：

- **可达 6 值**：`right_censored`、`unevaluable`、`rejected`、`unfilled_expired`、`stopped`、`tp_hit`。
- **暂不可达 1 值**：`filled_closed`。它是第 7 条兜底，专留给未来的政策平仓腿；v0 任何完整平仓都会先命中 `stopped` 或 `tp_hit`。覆盖测试对该值断言"当前夹具集不可达"，并在 C06 落地后转为正例，**不得**为了凑满七值把 `tp_hit` 改判成 `filled_closed`。

## 4. 落地清单（Codex 写任务完成后执行）

1. `quant_lab.market.contract.outcome_kind(res) -> Literal[七值]` 与 `exit_legs(res) -> tuple[str, ...]`；`ExecutionResult` 增 `exit_legs` 字段，进 `RESULT_SCALAR_COLS`。
2. `simulate_batch` 增 `outcome_kind`（Utf8）与 `exit_legs`（List(Utf8)）两列。
3. `tests/market/test_outcome_kind.py`：22 夹具 + review 反例逐一断言恰命中一条规则；六个可达值各 ≥1 例；`filled_closed` 断言不可达并注明理由；`stopped` 的 `("sl",)` 与 `("sl","tp")` 各 1 例；删失两类不得互串（尤其 `MARK_STALE` 不得映 `right_censored`）。
4. 本文 §1/§2 表由 G0 粘进 `execution-interface.md` §3（`contracts/` G2 只读）。

## 5. 供 G0 粘贴的 §3 段落

> **终态映射（A15）**：`reconstructed_outcome.kind` 由 `ExecutionResult` 按下列顺序唯一确定：①`censor_reason=LABEL_RIGHT_CENSORED` → `right_censored`；②`censor_reason` 为其余五种证据缺失码 → `unevaluable`（**禁映 `right_censored`**）；③未成交且有 `rejected` → `rejected`（**禁并入 `unfilled_expired`**）；④未成交且有 `expired` → `unfilled_expired`；⑤已 `closed` 且出场含 `sl` → `stopped`；⑥已 `closed` 且出场全为 `tp` → `tp_hit`；⑦其余已 `closed` 且有成交 → `filled_closed`（v0 不可达，留给政策平仓腿）。诊断列 `exit_legs` 给出出场实际参与的腿集合（`("sl",)` / `("sl","tp")` / `("tp",)` / `()`），使混合出场可分而不扩枚举。

## 6. 落地后发现的规范表空洞（block 给 G0，2026-09-11）

**事实**：`fill_status=none` 且事件既无 `rejected` 也无 `expired` 的结果**可达**，而契约 §3 终态映射的七条规则**一条都不命中**。

复现（IOC 入场价永不可成交，首个撮合机会即撤余量；窗口结束时已无存活 entry 单，故不产生 `expired`）：

```
events: ['submitted', 'accepted', 'cancelled', 'closed']
fill_status=none  censor=None  net_R=0
规则命中: ①False ②False ③False ④False ⑤⑥⑦False
当前实现兜底给出: unfilled_expired
```

**为什么必须裁定而不是留兜底**：`outcome_kind` 是 G1 `reconstructed_outcome.kind` 的唯一来源，规范表不满射时实现只能猜，而"猜"正是 A15 与 B9 要堵的标签串味——今天兜底成 `unfilled_expired`，明天换个实现者可能兜底成 `filled_closed` 或抛错，同一批样本的损耗表就会不可比。

**G2 建议（倾向 A）**：

- **A. 把规则 ④ 放宽为「未成交且非 `rejected`」**（覆盖 `expired` 与 `cancelled` 两种存活后未成交）。经济事实与 `expired` 同类：单子确实挂进了市场、最终没成交；差别只是终止原因（TTL 到期 vs IOC 语义 vs 保证金撤单），按 G0 对 `rejected.reason` 的同一理由——**细分读 `canonical_events`，不扩枚举**。
- B. 增第八值 `unfilled_cancelled`。不推荐：它与 `unfilled_expired` 在研究口径上不可区分（都进"未成交"分母），徒增枚举。

裁定前实现维持 A 的行为，并以 `test_outcome_kind.py::test_no_fill_without_reject_or_expire_is_pending_ruling` 显式钉住——若 G0 裁定不同，该断言会立刻失败并强制同步，不会静默漂移。

## 7. G0 裁定后的落地（契约 v1.8 §5.12 B10）+ 自查新发现 S20/S21 — 2026-09-11

| ID | 来源 | 处置 | 回归 |
|---|---|---|---|
| S18 | 五审 open / 我 block | 规则④按 B10 取**枚举** `expired\|cancelled`；未命中任何规则 **raise ContractError 并回报实际事件集合**，删除兜底分支 | `test_s18_b10_enumerated_rule4_and_no_fallback_labelling`（零成交 IOC 命中④；剔掉 cancelled 与剔掉出场腿两种未命中场景均抛错） |
| S19 | 五审 open | 显式 `entry_ttl_s` 改为**对称对账**：计划给值比计划，计划 null 比 policy 解析值 | `test_s19_explicit_ttl_cannot_bypass_policy_fallback` |
| **S20** | **本轮自查（A 族穷举）** | `t_start` 契约语义是"policy 决定"的解析记录，此前显式值从不对账：同一 `policy_hash` 下平移 45s 即把 `filled/net_R=1/tp_hit` 翻成 `none/net_R=0/unfilled_expired`。现要求等于 `t_dec + policy.latency_s`，要平移开始时刻须改 `policy.latency_s`（进 `content_hash`） | `test_request_validation`（两个伪造偏移均拒） |
| **S21** | **本轮自查（B 族穷举）** | `CENSOR_PRIORITY` 此前是**只定义不使用的死常量**，主因取决于代码检查顺序；候选 B 更严重——bars 分支**无条件覆盖**，规则未知 + bars 不完整时把 `RULE_HISTORY_MISSING` 覆写成 `BAR_GAP`。两内核改为按契约优先级选主因 | `test_s21_primary_censor_follows_contract_priority_not_check_order`（A/B 同取 RULE_HISTORY_MISSING，两个 coverage 位都落下） |

**自查方法与剩余面**：A 族（显式字段 + 兜底规则）穷举 `ExecutionRequest` 全部可空/有默认字段——`entry_ttl_s`✓、`entry_fractions`/`tp_fractions`✓、`t_start`→S20✓；`horizon_end` 是调用方自主选择的观察窗（非 policy 推导），保持自由输入但已进 `trace_hash`，如 G0 认为它也应由 `max_holding`/数据末端推导，请裁定。B 族（枚举/映射是否满射且被真正使用）逐个核对 `OUTCOME_KINDS`（B10 后满射且未命中即抛）、`CENSOR_REASONS`/`EVIDENCE_CENSORS`（pydantic 校验，两类不互串有测试）、`CENSOR_PRIORITY`→S21✓、`fill_status`、解释码集合（严格集合相等）。
