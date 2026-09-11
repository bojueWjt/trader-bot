# 契约：execution-interface（G2 market provides）

> 状态：**v1 已冻结（2026-09-11 OR-01）**。来源：合并稿 C.1 / C.2 行情流 / D.4，选型报告 Q3 / Q4。改签名先 `block`，由 G0 仲裁并写 changeLog。
> **优先级**：§5「OR-01 定稿修订」+ `docs/adr/report-G2-contract-revision-M01.md` 的 R1–R10 为规范性增补，与 §1–§4 的 v0 表冲突时**以 §5 与该报告为准**。

## 1. 行情湖分区（`data/lake/market/`）

```
bronze/<venue>/<market>/<data_type>/<interval>/<instrument>/<yyyy-mm>.zip   # Binance Vision 原包，带 sha256
silver/<venue>/<market>/<data_type>/<interval>/instrument=<X>/date=<yyyy-mm-dd>/part.parquet
_manifest/<partition_id>.json   # expected_rows, actual_rows, missing, duplicates, schema_hash, quarantine_n, rule_version, source_uri, source_sha256
```
`venue=binance`，`market=um`（USDT 永续），`data_type ∈ {klines, markPriceKlines, indexPriceKlines, premiumIndexKlines, fundingRate, metrics}`，`interval ∈ {1m, 15m, 8h, 5m}`。写入用临时文件 + 原子改名，幂等。

分区体检（合并稿 C.2 行情流）产出 validity mask 列：`ohlc_valid, spike_flag, gap_flag`；尖刺只标不删。品种生命周期与精度表 `silver/binance/um/instrument_rules/instrument=<X>/rules.parquet`：`effective_from, effective_to, tick_size, step_size, min_notional, multiplier, funding_interval_hours, status`。

## 2. 三时钟 as-of 库
```python
# quant_lab.market.asof
def asof_join(
    left: pl.DataFrame, right: pl.DataFrame, *,
    left_on: str = "t_dec", right_on: str = "available_at",
    by: list[str] | None = None,                # 如 ["instrument_id"]
    strategy: Literal["strict_lt", "le_with_sequence"] = "strict_lt",
    tolerance: timedelta | None = None,         # 超过 → 右侧列 null 且 reason=MARK_STALE
    sequence_cols: tuple[str, str] | None = None,
) -> pl.DataFrame
def last_closed_bar(bars: pl.DataFrame, *, at: datetime, instrument_id: str, interval: str) -> pl.DataFrame | None  # 只取 close_time <= at
def mark_price_at(at: datetime, instrument_id: str, *, max_staleness_s: int = 120) -> tuple[Decimal | None, str | None]  # (价格, reason_code)
```
性质测试：等号语义、晚到数据、同秒歧义、跨周期未收盘 bar 不可见。

## 3. 执行合同（合并稿 D.4）

### ExecutionRequest
| 字段 | 类型 | 说明 |
|---|---|---|
| `episode_id` / `graph_version` | string | 来自 G1 |
| `decision_snapshot_hash` | string | 决策图闭包哈希 |
| `t_dec` | timestamp | |
| `order_plan` | struct | 冻结的计划：`instrument_id, side, entries[{price_lo, price_hi, kind ∈ {limit, market_ref, ladder}}], stop, tps[{level, fraction}], size, expiry` |
| `policy_version` | string | 执行政策（展开、分批、超时） |
| `risk_budget` | decimal > 0 | 入场前固定，移动 SL 不重置 |
| `cost_scenario` | enum{base, stress} | 手续费/滑点档 |
| `path_scenario` | enum{primary, adverse, favorable} | 同 bar OHLC 路径情景 |
| `market_manifest` | string | 行情数据版本 |
| `execution_contract_version` | string | |
| `seed` | int | |

### ExecutionResult
| 字段 | 类型 |
|---|---|
| `canonical_events` | list[struct{seq, ts, kind ∈ {submitted, accepted, rejected, working, partial_fill, filled, cancelled, expired, stop_triggered, tp_triggered, funding, closed}, price, qty, fee, reason}] |
| `fill_status` | enum{none, partial, filled} |
| `filled_qty` / `fees` / `funding` / `slippage` | decimal |
| `net_pnl` / `net_R` | **decimal?（可空，见 §5 裁定 B3）**：`censor_reason` 非空 → 两者均 null；否则 `fill_status=none` → `net_R=0`；其余 `net_R = net_pnl / risk_budget`。skip 不在 G2 出现（G3 侧记 0） |
| `censor_reason` | string?（`LABEL_RIGHT_CENSORED` 等；删失不记 0） |
| `coverage_mask` | struct{mark_ok, funding_ok, rules_ok} |
| `trace_hash` | string（同输入同合同重放一致） |

```python
# quant_lab.market.execution
def simulate(req: ExecutionRequest, *, kernel: Literal["A", "B"] = "A") -> ExecutionResult
def simulate_batch(reqs: Iterable[ExecutionRequest], **kw) -> pl.DataFrame
```

### 不变量（每次 simulate 后断言）
数量守恒、手续费/资金费率账务守恒、reduce-only 不增仓、GTD/IOC 终态唯一、累计成交 ≤ 订单量、触 TP 前无 fill 则仓位为零、`net_R` 在 `censor_reason` 非空时为 null。

### 终态映射（A15 / B9，规范性）

`reconstructed_outcome.kind` 由 `ExecutionResult` 按下列顺序唯一确定，首条命中即取值：

| # | 条件 | kind |
|---|---|---|
| 1 | `censor_reason == LABEL_RIGHT_CENSORED` | `right_censored` |
| 2 | `censor_reason` 为其余五种证据缺失码 | `unevaluable`（**禁映 `right_censored`**）|
| 3 | 未成交且事件含 `rejected` | `rejected`（**禁并入 `unfilled_expired`**）|
| 4 | 未成交且订单以 `expired` **或** `cancelled` 终止（TTL 到期 / IOC·FOK 立即撤销 / 撤单）| `unfilled_expired`（§5.12 B10）|
| — | **以上均不命中** | **`raise ContractError`，回报实际事件集合；禁止兜底贴值（§5.12 B10 第 1 条 (b)）** |
| 5 | 已 `closed` 且出场成交含 `leg=sl` | `stopped` |
| 6 | 已 `closed` 且出场成交全为 `leg=tp` | `tp_hit` |
| 7 | 其余已 `closed` 且有成交 | `filled_closed`（v0 不可达，留给政策平仓腿 C06）|

诊断列 `exit_legs` 给出出场实际参与的腿集合（`("sl",)` / `("sl","tp")` / `("tp",)` / `()`），使混合出场可分而无须扩枚举。

## 4. 最小 episode 期望集（`tests/market/fixtures/episodes/*.json`）
≥10 个手工构造：跳空、同分钟两价格流异步、mark/last 分离触发、funding 结算边界、部分成交、同 bar 双触发、未成交到期、reduce-only、区间入场、多档止盈。每个带独立期望 `ExecutionResult`，两内核候选都要过。

---

## 5. OR-01 定稿修订（G0 裁定，2026-09-11，规范性）

G2 在 M-01 提交 `docs/adr/report-G2-contract-revision-M01.md`（R1–R11）。G0 裁定如下。

### 5.1 全数采纳：R1–R9（并入契约，该报告为规范性附件）

`mark_price_at`/`mark_bar_at` 加 `marks: pl.DataFrame` 首参（R1，§8 第 4 项闭合：隐式全局状态已消除）；`asof_join` 的 `asof_reason` 列 / `AsOfKeyDuplicate` / 后缀 `_r`（R2）；`last_closed_bar(latency=0)` 与 `close_time` = 区间右端点、原始值另存 `close_time_raw`（R3）；silver bar 固定列与 dtype（R4，§1.1）；`order_plan` 结构（R5，§3.1）；`t_start` / `horizon_end` / `position_mode` / `fee_rates`（R6，§3.2）；`canonical_events` 归因字段与确定性 `seq` 排序（R7，§3.3）；`ExecutionResult` 新增标量与 `coverage_mask`（R8）；`simulate_batch` 输出列与配对键（R9）。**G1 与 G3 按该报告 §0 表写桩即可，不必等 G0 再改本文件。**

### 5.2 裁定 B1：`instrument_id` = `BTCUSDT-PERP.BINANCE-UM`（G2 §4.1 问题 1）

采纳 G2 提案，依据是 `nautilus_trader 1.227.0` 实测：旧格式 `BINANCE-PERP:BTCUSDT` 在 `InstrumentId.from_str()` 直接 `ValueError`（缺 `.` 分隔符），新格式解析为 `symbol=BTCUSDT-PERP, venue=BINANCE-UM`。M-08 的 A/B 对拍不能在被对拍的接缝上插有损转换。`research-schema.md` §9.2 已同步改。互转 helper 只允许出现在读 Binance Vision 原始文件名的边界。

### 5.3 裁定 B2：H0 `latency` 默认 0，但真实数据声明必须做敏感性（G2 §4.1 问题 2）

默认 `latency=0`、参数化、`manifest.available_at_basis` 记录，采纳。**附加必修**：`latency=0` 等于假设 bar 在 `close_time` 整点即可知，对真实行情是**乐观**假设。P2 任何基于真实数据的 θ 声明，必须附 `latency=1s` 的敏感性重跑与 Δθ；Δθ 大于 θ 的标准误则该声明降级为"描述"。P0/P1 合成数据不受此约束。

### 5.4 裁定 B3：`net_R` 可空（§8 第 3 项闭合，采纳 R10）

优先级：`censor_reason` 非空 → `net_R = net_pnl = null`；否则 `fill_status = none` → `net_R = 0`；skip 由 G3 侧记 0，G2 不产生 skip。§3 表已改为 `decimal?`，§3 不变量保留。`feature-snapshot.md` §4 的"候选 NaN → skip(0)"仅指 G3 侧规则求值，不覆盖本条。

### 5.5 裁定 B4：`fee_rates` 数值进 policy 文档，但 `policy_version` 必须内容寻址（G2 §4.1 问题 3）

采纳"契约只定 enum、数值进 `policy_version` 文档"，**但补一条必修**：若 `policy_version` 只是自由字符串，改了费率数值而版本串不变时 `trace_hash` **不会变**——同一个 `trace_hash` 会对应两个不同的经济结果，静默破坏可复算性。因此：

1. `ExecutionRequest` 增 `policy_hash: string`（policy 文档规范化内容的 sha256）；
2. `policy_hash` 进 `trace_hash` 的输入；
3. policy 文档任何数值改动必须同时改 `policy_version` 串，CI/测试断言 `(policy_version, policy_hash)` 一一对应。

### 5.6 裁定 B5：`leg` 枚举含 `funding`（G2 报告内部不一致，G0 消歧）

R7 表写 `leg ∈ {entry, sl, tp, close}`，同报告 §3.3 写 `leg ∈ {entry, sl, tp, close, funding}`。**以含 `funding` 的五值为准**——资金费事件必须能归因到腿，否则账务守恒不变量无法按腿核对。

### 5.7 裁定 B6：`simulate_batch` 不含 baseline/candidate 概念（跨契约冲突，改的是 feature-snapshot）

G2 的 R9 明确"输出不含 candidate 概念"，而 `feature-snapshot.md` §4 原文写 `execution: G2 simulate_batch 输出，含 baseline 与 candidate 同机会`。**判 G2 对**：执行内核不应知道研究侧的对照设计，否则内核要为每种研究口径改一次，A/B 对拍与 `trace_hash` 都会被污染。G3 改为**发两组 request**（差异体现在 `policy_version`/规则，两组共享 `episode_id, graph_version`），拿回两个结果集后按 `episode_id` 自行配对成 baseline/candidate。`feature-snapshot.md` §7 已同步改。

### 5.8 待 G2 回执（下轮 verify 点）

`policy_hash` 进 `ExecutionRequest` 与 `trace_hash`；`leg` 五值；`instrument_id` 新格式落到 `vision.py` 与 `contract.py`；`build_request(...)` 按 `research-schema.md` §9.4 落到 `quant_lab.market.contract`。G0 下轮实跑核对签名。

### 5.9 裁定 B5：`Expiry.entry_ttl_s` 可空，缺失由 policy 解析（2026-09-11 OR-02 R3，规范性）

**触发**：G0 OR-04 骨架首次以 `build_request` 消费真实 G1 `gold/episode` 行，26 个有 order_plan 的 episode 里 **25 个** `expiry.entry_ttl_s = null`，`Expiry.entry_ttl_s: int = Field(gt=0)` 直接抛 `ValidationError`，端到端在第一个接缝断裂。此前无人发现，因为 G3 用 `synthetic.fake_execution` 自造执行结果、从未调用过 `build_request`。

**裁定**：原文未给入场有效期时，G1 **不得**猜值（与 review-G1-P1 S06 一致：`gold` 不得冒充作者事实）。改由 G2 承接：

1. `Expiry.entry_ttl_s: int | None = Field(default=None, gt=0)`（与 `max_holding_s` 同构）。
2. `ExecutionPolicy` 增 `entry_ttl_s: int`（默认值，建议 24×3600），作为 policy 内容的一部分进 `content_hash` ⇒ 改默认 TTL 必然改 `policy_hash`，可复算不被静默破坏。
3. `build_request` 解析：`ttl = plan.expiry.entry_ttl_s if not None else policy.entry_ttl_s`，解析结果必须写进 `ExecutionRequest` 的显式字段（不得只在 `horizon_end` 里隐式体现），并进 `REQUEST_ID_COLS` ⇒ 进 `trace_hash`。
4. `ExecutionResult` 的 `entry_ttl_source ∈ {plan, policy}` 作为诊断列输出，损耗归因时可区分"作者给了 TTL" 与 "policy 兜底"。

**属主**：G2（M-06/M-09）。**验收**：`build_request` 对 `entry_ttl_s=None` 的真实 G1 行成功构造；两条 policy 只差 `entry_ttl_s` 时 `policy_hash` 与 `trace_hash` 均不同；`entry_ttl_source` 覆盖两种取值各 ≥1 例。

### 5.10 裁定 B8：`order_plan` 的 `entries[].fraction` / `tps[].fraction` 可空，缺失由 policy 分配（2026-09-11 OR-02 R5，规范性）

> **标号勘误**：§5.9 的标题误写为「裁定 B5」（B5 已用于 §5.6），其规范标号应为 **B7**。引用时一律**以节号为准**（§5.9 = entry_ttl；§5.10 = fraction）。

**触发**：G0 OR-04 以 `build_request` 消费真实 G1 `gold/episode` 行，26 个有 `order_plan` 的 episode 中 **25 个** `entries[].fraction` / `tps[].fraction` 为 null（G1 按 review-G1-P1 S06「不得均分猜值」主动留空），而 G2 `Entry.fraction: Decimal = Decimal(1)` / `Tp.fraction: Decimal`（必填）+ 校验 `sum(entries.fraction) == 1`、`sum(tps.fraction) <= 1` 直接抛 `ValidationError`/`ContractError`，端到端在同一个接缝上第二次断裂。

**裁定**：与 §5.9 **同型**处置——原文未给分配比例时 G1 **不得**猜值（`gold` 不得冒充作者事实），由 G2 用 policy 兜底并把解析结果显式化、可复算。

1. **契约类型**：`order_plan.entries[].fraction` 与 `order_plan.tps[].fraction` 改为 **`decimal?`（可空，默认 None）**。§3 表的 `tps[{level, fraction}]` 与 `report-G2-contract-revision-M01.md` R5 的 `order_plan` 结构按本条修订。`Entry.fraction` 的现有默认值 `Decimal(1)` **必须去掉**（默认 1 会让"作者未给"与"作者明写全仓"不可区分，直接污染 `fraction_source` 的诊断价值）。

2. **`ExecutionPolicy` 增分配规则**（进 `content_hash` ⇒ 改规则必然改 `policy_hash` 与 `trace_hash`）：
   - `entry_fraction_rule: Literal["equal"] = "equal"` —— 单腿 ⇒ `[1]`；n 腿 ⇒ 每腿 `quantize(1/n, 12)`，**余量并入末腿**使和恰为 `1`。
   - `tp_fraction_rule: Literal["equal"] = "equal"`、`tp_total_fraction: Decimal = Decimal(1)` —— m 个 TP ⇒ 每档 `quantize(tp_total_fraction/m, 12)`，**余量并入末档**使和恰为 `tp_total_fraction`（≤1）。
   - 量化标度固定 `12`（与 §9/CR-01 的 `Decimal(38,12)` 全链一致），舍入 `ROUND_DOWN` 后补余量，**禁止**用二进制浮点做等分。

3. **全有或全无**：同一列表内 `fraction` 必须要么全部给出、要么全部为 null。**部分给出 ⇒ `ContractError`**，不得对缺失项做 policy 兜底后再与作者值混合求和——混合会产生"作者给了 0.6，policy 补 0.5"这类和不为 1 且无人负责的分配。

4. **显式化进 `trace_hash`**：`build_request` 解析后必须把结果写进 `ExecutionRequest` 的显式字段 `entry_fractions: tuple[decimal, ...]` 与 `tp_fractions: tuple[decimal, ...]`（不得只在内部展开），并加入 `REQUEST_ID_COLS` ⇒ 进 `trace_hash`。

5. **诊断列**：`ExecutionResult` 增 `fraction_source ∈ {plan, policy}`（与 `entry_ttl_source` 同性质，进 `RESULT_SCALAR_COLS`）。entries 与 tps 若来源不同，取值规则：两者都来自 plan ⇒ `plan`，否则 `policy`（保守，宁可标 policy）。

6. **G1 义务**：保持留空，**不得**均分猜值；`gold` 的 `fraction` 列类型按 §9.10 A8 为 `Decimal(38,12)`（可空），不得为 `Float64`——`1/3` 的浮点等分会让 `sum != 1` 在 G2 侧随机报错。

**属主**：G2（M-06/M-09）主改，G1（D-07）只改 dtype。**验收**：(a) `build_request` 对 25 行 `fraction=null` 的真实 G1 行全部成功构造；(b) 两条只差 `tp_total_fraction` 的 policy 产出不同 `policy_hash` 与 `trace_hash`；(c) `fraction_source` 两种取值各 ≥1 例；(d) 3 腿等分用例 `sum(entry_fractions) == Decimal(1)` 精确成立；(e) 部分给出的用例抛 `ContractError`。

### 5.11 裁定 B9：终态映射入契约 + 枚举不可达的正当处置 + 显式字段的一般规则（G0 OR-02 R22，规范性）

G2 按 §9.10.11 A15 做完映射并提交 `docs/adr/report-G2-outcome-kind-mapping.md`，其 §5 段落经 G0 审阅后**已并入本文件 §3「终态映射」**，为规范性内容。另裁两条：

#### 1. `filled_closed` 在 v0 不可达 —— 准，且**禁止为凑满枚举而改判**

v0 无政策平仓腿（ADR C06 留 P2），任何完整平仓都会先命中 `stopped` 或 `tp_hit`，第 7 条规则不可达。

**裁定**：枚举值在某个内核版本下不可达是合法状态。覆盖测试的正确写法是**断言其不可达并注明原因**（`filled_closed` 的原因为"v0 无 `leg=close` 政策平仓腿"），C06 落地后转为正例。**严禁**把 `tp_hit` 或 `stopped` 改判成 `filled_closed` 来让七值"都被覆盖到"——那会把"止盈平仓"与"政策平仓"两种不同事实合并，正是 A15 要堵的标签串味；测试覆盖率不是把标签改错的理由。

同理适用于其余枚举值：任何"为了让覆盖测试好看"而放宽或改判标签的行为，一律视为放宽性变更，须先 `block` 给 G0（§9.10.12 A16 第 4 条）。

#### 2. 显式字段的一般规则（采纳 G2 对 S17 教训的推广，优于 G0 原表述）

凡"**显式字段 + 兜底规则**"型裁定（现有：§5.9 `entry_ttl_s`、§5.10 `entry_fractions`/`tp_fractions`；今后同型一律适用），显式字段在语义上是**解析结果的记录**，不是独立输入。因此必须同时满足：

1. **逐值相等**：显式值与 `resolve_*(plan, policy)` 的解析结果逐值相等，不等即 `ContractError`；
2. **同域校验**：显式值必须通过与被解析值**同一套**域校验（精度/标度/范围）。

第 2 条是 S17 暴露的第二个口子：`.5000000000001` 能进请求、批表截断到标度 12、而 `trace_hash` 用的是未截断的原值 —— 输出无法忠实复原参与哈希的输入，可复算性在"看起来都对"的情况下静默破裂。

**根因归属**：这是 G0 的 B8 裁定引入的新接缝（要求显式字段进 `trace_hash` 却未规定其域校验），由 G2 在四审中发现并堵上。记为判例 3。

### 5.12 裁定 B10：修补 A15 规格的两处自身漏洞（G0 OR-02 R23，规范性）

G2 五审（`review-G2-P1.md` §五审判定表）判 S01–S17 **open 0**，两条新 open **S18/S19 均是 G0 规格或其落实的缺陷，不是 G2 的工程问题**。逐条裁定，实现随后由 G2 落地。

#### 1. S18：`unfilled_expired` 的兜底伪装 —— G0 规格漏洞，必修

**事实**：`contract.py:547` 为 `return "rejected" if "rejected" in kinds else "unfilled_expired"`。零成交的 IOC 订单事件为 `submitted/accepted/cancelled/closed`，**不含 `expired`**，却被 else 分支贴上 `unfilled_expired`。§3 映射表第 4 条写的是"未成交且事件含 `expired`"，该状态**不命中任何一条规则**，实现用兜底把它伪装成到期。

**裁定 (a)**：§3 第 4 条**扩为**"未成交且订单以 `expired` 或 `cancelled` 终止（TTL 到期、TIF=IOC/FOK 的立即撤销）→ `unfilled_expired`"。合并的正当性：两者的经济事实同一（挂出去了、从未成交、订单已终结、无仓位无盈亏），差别只是终止机制，而机制在 `canonical_events` 里完整保留。这与 `right_censored`/`unevaluable`（世界的事实 vs 我们的事实）、`rejected`/`unfilled_expired`（从未挂出 vs 挂出未成）不同——后两组是**不同的现实**，必须分开；本组是**同一现实的两种机制**。

**裁定 (b)（一般规则，比 (a) 重要）**：**禁止用兜底分支把函数补成全函数**。`outcome_kind` 及今后任何标签映射，未命中任何规则时必须 `raise ContractError` 并回报实际事件集合，**不得**返回某个"看起来合理"的值。理由与 §9.8 A6、§9.10.11 A15 一脉：静默贴一个似是而非的标签，比抛错难发现得多——S18 正是靠对抗审查才暴露，而它已经在生产标签了。

#### 2. S19：显式 `entry_ttl_s` 可绕过 policy —— B9 第 1 条未被对称落实，必修

**事实**：`contract.py:391–393` 仅在 `plan_ttl is not None` 时比对。作者 TTL 为 `null` 时，调用方可在**同一 `policy_hash`** 下传入任意 `entry_ttl_s`（实测 86400 与 1 分别得到 `filled/net5/tp_hit` 与 `none/net0/unfilled_expired`），政策兜底形同虚设。

**裁定**：§5.11 B9 第 1 条"显式值与 `resolve_*(plan, policy)` 的解析结果逐值相等"是**对称要求**，两条分支都要查：计划给值时比计划，计划为 `null` 时比 **policy 解析值**。单边比对不构成合规。`entry_ttl_s`、`entry_fractions`、`tp_fractions` 一并适用，并各加一条"计划 null + 显式值篡改 → `ContractError`"的反例测试。

#### 3. M-03 联网 verify 由 G0 独立实跑解除

五审将 M-03 记为 `insufficient` 且明示非阻断，理由是禁网且**拒绝用 MockTransport 冒充联网实跑**——该拒绝是正确行为，记为判例 4。G0 已于 R23 独立实跑该 verify：`actual_rows=44640`、`distinct_keys=44640`、`check_status=ok`、`checksum_source=vision_CHECKSUM`、`source_uri` 为真实 Binance Vision 月度包，rc=0。该项**解除**，不再计入 M-10 的未决项。

**一般规则**：`insufficient` 默认视同 `fail`（§9.9 A7 不变）。**唯一例外**：当造成 `insufficient` 的全部条目 (a) 被报告显式标为非阻断、(b) 因审查环境的结构性限制（禁网、无凭据、缺硬件）而不可验、(c) 审查方未以任何形式伪造该验证，则 **G0 可通过亲自实跑这些 verify 予以解除**，并在看板记录实际输出。窗口自行实跑不算解除。若仍有任何阻断项，一律 `fail`，不得解除。

### 5.13 裁定 B11：`horizon_end` 定为「可自由选择但必须记账」+ 死常量审查规则（G0 OR-02 R25，规范性）

G2 在等裁窗口内按 S17/S19 的共同根因做同族穷举，自查出 **S20**（`t_start` 显式值从不对账，同 `policy_hash` 下平移 45s 可把 `filled/net_R=1/tp_hit` 翻成 `none/net_R=0/unfilled_expired`，判例 3 第 4 例）与 **S21**（`CENSOR_PRIORITY` 是只定义不使用的死常量，实现为 first-wins；候选 B 更严重，bars 分支无条件覆盖，把 `RULE_HISTORY_MISSING` 覆写成 `BAR_GAP`），均已修并有回归。G0 认可两条修复方向，并就其留裁的 `horizon_end` 裁定如下。

#### 1. `horizon_end` 不强制为推导值，但必须记账

**与 S17/S19/S20 的本质区别**：`entry_ttl_s` / `fractions` / `t_start` 都是**对作者意图与政策的机械解析**——存在唯一正确答案，任何偏离都是篡改。`horizon_end` 不同：观察多久才判右删失，是**正当的研究设计选择**，不同研究问题可以合理地取不同窗口。强行绑死公式会把一个真实的设计自由度伪装成不存在。

**但它不能是隐形旋钮**。裁定四条：

1. **默认路径必须是推导值且受同款对账**：调用方不传时，`horizon_end = t_dec + entry_ttl + (max_holding 或 policy.max_horizon_s)`；该默认值与显式值的关系按 §5.11 B9 处理（默认路径被篡改即 `ContractError`）。
2. **显式值合法**，但必须进 `trace_hash`。G0 已核实 `request_canonical` 吃 `req.model_dump()` 全量、`horizon_end` 在内，**该性质自此为规范要求**，不得在后续优化中被移出。
3. **增诊断列 `horizon_source ∈ {policy, caller}`**（与 `entry_ttl_source` / `fraction_source` 同族）。
4. **记账的牙齿（属主 G3）**：`horizon_source == "caller"` 时，`horizon_end` 必须进入研究尝试账本的配置身份（`config_id` 输入）。**理由**：不受约束的观察窗是典型的分叉路径自由度——换个窗口重跑直到结果好看，若不计入尝试账本就是免费的多重比较。计入后，试多个窗口会消耗尝试预算并出现在账本里，`max-t` 与分档随之正确惩罚。这不是限制研究自由，而是让行使这一自由**可见且有代价**，正是账本与 max-t 机制存在的意义。

#### 2. 死常量审查规则（采纳 G2 对 S21 的归纳，列入常规审查清单）

**"定义了却不使用"的常量/规则比缺失更危险，因为它让阅读者以为规则已实现。** `CENSOR_PRIORITY` 写在契约里、导出在 `__all__` 里、却没有任何调用点，主因实际取决于代码检查顺序——契约与行为分叉，而静态阅读契约或源码都看不出来。

**自此列入每轮评审清单**：契约中规定了排序/优先级/阈值的具名常量，必须有可追溯的调用点与一条断言其生效的测试；只导出不调用即视为未实现，判该项 `open`。G0 在评审中按此核查。

### 5.14 裁定 B12：拆分「安全上限」与「研究标准观察窗」（G0 OR-02 R26，规范性）

G2 指出 B11 的推导路径在实践中不可用：`max_holding_s` 缺失时回落到 `policy.max_horizon_s = 14 天`。G0 实测证实且比其陈述更强——真实 `gold/episode` **23/23 行** `max_holding_s` 为 `null`，推导窗口 **100%** 落到 14 天，故 `horizon_source=caller` 会是主路径而非例外。

#### 1. 先更正一处推论（影响 G2 与 G3 的规模估计）

G2 由此推断"第 ④ 条的记账会大幅加重尝试预算与 max-t 惩罚"。**该推论不成立**：把一个字段纳入 `config_id` 本身不消耗预算，它只在**取值不同**时把尝试分开。若整个研究程序统一使用同一个观察窗，所有尝试该字段同值，重复仍被正确识别为 `duplicate`，预算与惩罚强度不变。它只在有人**真的去变动窗口**时咬人——而那正是它该咬的时候。G3 无须按"主路径加重"重估预算。

#### 2. 真正的缺陷是「安全上限」与「研究默认」被合成了一个字段

14 天是**安全上限**（防止无界观察、界定数据需求），不是任何人想要的**研究观察窗**。用上限充当默认，等于没有默认：每个调用方都得自己编一个窗口，于是同一研究程序内不同批次的窗口各不相同——**每一次都被记账，却互相不可比**。记账解决的是"免费的多重比较"，解决不了"没有共同标尺"。

**裁定**：`ExecutionPolicy` 拆为两个字段，均进 `content_hash`：

| 字段 | 含义 | 用途 |
|---|---|---|
| `max_horizon_s` | **安全上限**（保留 14 天） | 任何 `horizon_end` 不得超过 `t_start + max_horizon_s`，超出即 `ContractError` |
| `research_horizon_s` | **研究标准观察窗**（新增，必填） | 计划未给 `max_holding_s` 时的推导取值 |

推导式改为 `horizon_end = t_dec + latency + entry_ttl + (max_holding_s 或 policy.research_horizon_s)`，并受 `max_horizon_s` 封顶。如此 `horizon_source=policy` 回到主路径，`caller` 恢复为"有人刻意研究另一个窗口"的真实例外，诊断列重获区分力。

#### 3. `research_horizon_s` 的取值不由 G0 拍脑袋

**依据必须是标签成熟度，不是圆整数字。** 窗口太短则右删失率高、θ 建立在少数已成熟样本上（幸存偏差的一种）；太长则标签在研究期内不成熟、且与信号的实际持有意图脱节。

**要求（G2 + G3 共同提案，G0 裁定后入契约）**：在合成数据上给出**右删失率随观察窗变化的曲线**（至少覆盖 4h / 12h / 1d / 3d / 7d），连同各档的 `n_evaluated` 与 `n_censored_excluded`，据此提议取值并说明理由。在提案到达前，实现可暂以 `research_horizon_s = max_horizon_s` 保持现行行为，但**必须标为待定**，不得视为已定稿。

**闸门关联**：该取值直接决定任何 θ 声明所依赖的删失率，属 `G-STAT-CLAIM` 的实质内容。P1 合成阶段可自行选定并记录；**P2 真实数据的 θ 声明须在该闸门批准时一并确认本取值**。

#### 4. G1 不得为此猜 `max_holding_s`

G2 提议"让 G1 在 `max_holding_s` 缺失时保持 null"——**准，且这本就是现行铁律**（`research-schema` 原文：不填零、不猜值）。作者没说持有上限就是没说，由 policy 提供研究默认是正确的归属：**那是研究者的选择，不是作者的意图**，二者不可混淆。

#### 5. 排期认可

G2 决定把 B11/B12 的落地排在第六轮终审落盘之后、不在审查读树时改，G0 认可。依据是四审已因并发改树产生噪音（该报告自记"审查期间其他会话更新了契约与测试"），让终审探针撞上半改的树会拿到"因错误原因而 fail"的终裁，白烧一轮。

### 5.15 裁定 B13：`research_horizon_s` 暂定 3 天（占位），并须在真实括号分布上重算（G0 OR-02 R29，规范性）

G2 按 §5.14 B12 交出删失率曲线（`docs/adr/report-G2-research-horizon-proposal.md`）：**4h 78.7% / 12h 48.9% / 1d 25.5% / 3d 4.3% / 7d 0%**，提议 3 天。

#### 1. 取值逻辑认可

- **不取 4h / 12h**：删失样本被 G3 排除出分母，4h 档排除 78.7% 后，θ 实际只描述"四小时内走完的信号"——那是**另一个 estimand**，偏向高波动快速兑现的子总体，属幸存偏差。该论证成立。
- **不取 7d**：3d→7d 只多 2 个样本，却增加 `t1` 重叠，purge/embargo 削掉更多训练样本，是用统计效率换一个已经很低的删失率。该权衡成立。

#### 2. 但 G0 实测发现曲线的基础与真实计划不符，且偏差方向已知

G2 自陈曲线用的是**自编的 1.5% / 3% 括号**，并正确指出"真实计划的括号宽度决定曲线形状"。G0 在当前真实夹具（`fixture-v1@0c51102b`，65 条有完整括号）上实测：

| 量 | G2 曲线假设 | 真实夹具实测 |
|---|---|---|
| TP 距离 | 3% | 中位 **5.83%**，最大 **18.92%**，最小 3.14% |
| SL 距离 | 1.5% | 中位 **1.70%** |

**真实止盈距离是假设值的约两倍，尾部达六倍。** 括号越宽，触及越慢，**同一观察窗下的删失率只会更高**——曲线整体右移。因此 **3 天很可能低估了所需窗口**，偏差方向明确，不是随机噪声。

#### 3. 裁定

1. `research_horizon_s = 3 天` **暂定生效**，状态为**占位**，须在契约与报告中标明。
2. **P1 结束前**由 G2 + G3 在**真实夹具的括号分布**（而非自编括号）上重算曲线，并按 §5.14 B12 附各档 `n_evaluated` 与 `n_censored_excluded`；若重算显示 3d 删失率显著高于 4.3%，按新曲线提新值。
3. 改值是 policy 内容变更，走 §5.5 B4 版本登记迁移。
4. P2 真实数据的 θ 声明须随 `G-STAT-CLAIM` 闸门确认最终取值；在此之前该值不得用于任何对外声明。

#### 4. 删失率不是越低越好（采纳 G2 给 G3 的提醒，列为选窗原则）

删失率与 `t1` 重叠、purge/embargo 损耗、单位时间独立机会数**此消彼长**。选窗口必须同时看同一批数据下的 `n_evaluated` **与折内有效簇数**——单看删失率会一路选到最长窗口，把统计效率赔光。G3 重算时须同时给出两者。

#### 5. 同族第六、七例（记录，验证 §5.12 B10 与 R27 归纳的普适性）

- **S22**：`entry_fractions=[]` 等**合法假值**被 `or` 兜底静默替换。G2 归纳精准：S17/S19/S20 是"显式值绕过推导"，S22 是"显式值被静默修补"，**同一枚硬币的两面**，根因都是没把显式字段当作必须逐值对账的记录。
- **`partition_check` 的 `or 0`**：空分区路径上未知的 `expected_rows` 被填成 0，于是 `missing = 0` —— **等于宣称"一根 bar 都不缺"**。这是 `or` 兜底最危险的形态：把"不知道"伪装成"没问题"。已改为未知即 `None`。
- **S23**：`DF_DECIMAL` 是死常量（`execution.py` 硬编码 `pl.Decimal(38,12)`，改契约常量不改 schema），另有阈值/排序常量有调用点但缺"断言其生效"的测试。按 §5.13 B11 第 2 节记 `open`，G2 正在补。

### 5.16 裁定 B14：证据完备性判定必须覆盖它所解锁的每个经济量参数（G0 OR-02 R30，规范性）

G2 按 R27 的 `or` 兜底通则做全模块自查，报 **S24**。G0 实地核实属实：

- `execution.py:234` 的 `rules_known` 校验了 `tick_size` / `step_size` / `min_notional` / `status==TRADING` / 生效区间一致与不重叠，**唯独没有 `multiplier`**；
- `execution.py:239` 为 `multiplier=Decimal(rr["multiplier"] or "1")`，**未知乘数静默当作 1**。

S08 之后 `multiplier` 缩放**全部**经济量（PnL、费用、资金费、滑点、保证金预留）。

#### 1. 严重度排序（本族内首次区分，具规范意义）

G2 的对比精准，予以采纳为判定准则：**S22 让一个非法请求通过，S24 让一笔算错的经济结果看起来完全正常。** 后者更重——非法请求迟早在某处现形，而一个"合理但错误"的数字与正确答案在下游不可区分，会一路进入 θ、损耗表与最终声明。

**准则**：同族缺陷定级时，"产出看似合理的错误数值"一律高于"放过一个非法输入"。

#### 2. 一般规则

**凡"证据完备性"判定（`rules_known` / `bars_complete` / `funding_schedule_complete` 等），必须覆盖它所解锁的每一个参与经济计算的参数。** 只要某字段缺失会改变经济结果，它的缺失就必须使该判定为假，而不是被默认值补上。判定项与其解锁的参数集**必须逐一对应**，新增参数时同步扩充判定，并加一条"该字段缺失 → 判定为假"的断言测试。

#### 3. 关于"今日不可达"

S24 当前不可达（两个规则生产者都硬写 `"1"`），但 `instrument_rules.multiplier` 是可空列，接入第三个数据源即触发。G2 的观察印证了 R27 归纳：**`or` 写法在非空常见值上永远看起来是对的**——它在现有两个生产者上恒正确，要等新数据源才咬人，而那时它已经在产出经济结果了。

**裁定**：**不可达不是不修的理由**。修复成本是一行判定加一个断言；依赖"生产者永远不会变"是把正确性寄托在契约之外的巧合上。与 §5.11 B9 对 `filled_closed` 的处理并不矛盾——那里不可达的是**合法枚举值**（断言不可达并注明原因即可），这里不可达的是**缺陷**（必须修）。

#### 4. 排期与自查认可

G2 将修复排在终审落盘之后（不在审查读树时改），理由是四审已因并发改树产生噪音、而 S24 今日不可达，G0 认可。其在终审任务书中要求审查方独立做同族穷举且不采信自查结论——若审查方独立发现 S24，即构成对本规则有效性的一次交叉验证。

### 5.17 裁定 B15：B12 推导式未按契约落实 + 亚秒绕过（G0 OR-02 R32，规范性）

G2 七审（`review-G2-P1.md` §七审判定表）：S01–S23 **closed 15 / partial-P2 8 / open 0**，两条新 open **均指向 G0 裁定的落实或规格本身**。

#### 0. 先记一处编号冲突（务必消歧）

**"S24" 被两件不同的事占用**：G2 自查的 `multiplier` 未纳入 `rules_known`（见 §5.16 B14），与七审的 B12 推导式未落实（本节）。二者无关。**自此约定：审查方新增编号与窗口自查编号不得共用序列**，窗口自查项一律加前缀（如 `G2-SC-01`），历史两条按"§5.16 B14 的 S24"与"§5.17 B15 的 S24"引用。

#### 1. B12 推导式漏了 `entry_ttl` 项（必修）

契约 §5.14 B12 写的是 `horizon_end = t_dec + latency + entry_ttl + (max_holding_s 或 research_horizon_s)`；实现（`contract.py:263/418/471`）**两处都只用 `research_horizon_s`，丢掉了 `entry_ttl` 项**。

**该项不是冗余**：`entry_ttl` 是入场单的存活时长，若成交发生在 TTL 末刻，仍应有完整的 `research_horizon_s` 用于观察持仓。丢掉它等于把"最坏情况下的成交延迟"从观察窗里扣掉。

审查方给的反例是决定性的：合成 policy（ttl 60 / research 50 / max 200）下契约值 110s、实现值 50s，**同一份行情里 50s 得 `right_censored`/`net=None`，110s 得 `tp_hit`/`net=5`——标签直接翻转**。且现有两条 horizon 测试**把漏了 TTL 的式子写成了期望**，因此不能作为合规证据（同 §9.10.13 A17：门被写成现状就不再是门）。

#### 2. `research_horizon_s` 宣告必填却可省略并静默补 14 天（必修）

§5.14 B12 将其定为必填，模型却允许省略并回落到 `max_horizon_s`。这是**同族又一例**：声明为必填，实现给默认值，于是"没配置"与"配置成了安全上限"不可分。**裁定**：必填即必填，缺失抛 `ContractError`；若确需占位，**显式登记一个占位政策版本**并在 `content_hash` 中体现，不得由模型默默补值。

#### 3. S25：亚秒绕过 —— G0 规格漏洞（必修）

`policy` 未声明的 `+1µs` 偏移被接受；`policy` 与 `caller` 路径超出安全上限 `+1µs` 至 `+999999µs` 均放行。根因是 §5.11 B9 的逐值相等与 §5.14 B12 的封顶都在**秒粒度**上比较，亚秒余量被 int 截断吃掉。

**裁定**：`t_start` / `horizon_end` / TTL 的一致性对账与安全封顶，**一律在完整时间戳分辨率上做精确比较，禁止任何截断或取整**。策略字段以秒为单位不代表比较可以降到秒——**降精度比较等于给出一个每次都小于一秒的免费额度**，而这一族的全部教训就是"免费的、不留痕的自由度终将被用掉"。

#### 4. 归属说明

本节两条均非 G2 工程疏漏：第 1、2 条是 G0 裁定文本已写明而实现未落实（G2 须修），第 3 条是 G0 规格未规定比较分辨率（G0 补规）。七审能在 S01–S23 全部收口后仍挖出这两条，且给出标签翻转的量化反例，质量值得记录。

### 5.18 裁定 B16：`research_horizon_s` 定为 5 天 + 占位期版本登记边界（G0 OR-02 R33，规范性）

#### 1. 曲线 v2 证实 B13 的预测，`research_horizon_s` 改为 **5 天**

G2 用**真实 gold 括号形状**（67 条）重算（`report-G2-research-horizon-proposal.md` §6）：

| 观察窗 | 4h | 12h | 1d | 2d | 3d | 5d | 7d |
|---|---|---|---|---|---|---|---|
| v2（真实括号） | 75.7% | 49.5% | 38.2% | 22.3% | **15.3%** | **6.0%** | 3.4% |
| v1（自编括号） | 78.7% | 48.9% | 25.5% | — | 4.3% | — | 0.0% |

**3d 的删失率从 4.3% 升到 15.3%，3.6 倍**——§5.15 B13 基于真实括号宽度做出的"3 天很可能低估"判断，方向与量级均被证实。

**采纳 5 天**，理由链与否定 4h/12h 时同构：3d 的 15.3% 被排除的恰是走得慢、括号宽的那批，θ 会偏向快速兑现样本（同一种幸存偏差，程度轻些）；5d 降到 6.0%，与 v1 中判定可接受的 4.3% 同量级；7d 只再降到 3.4%，多花两天换 42 个样本，`t1` 重叠与 purge 代价不划算。

**G2 找到的真正解释比中位数更有力，记录在案**：67 条里 **40 条是三档止盈梯**，且分数留空即等分，**要完全平仓必须打满三档**；v1 的单档假设让仓位一次走完。按档数拆：1 档在 3d 为 12%，2 档 29%，3 档 17%。另真实多空 57:10 而 v1 用 50:50，在上行月会低估删失。

#### 2. 由此浮出一个比窗口长度更根本的问题（须 G2 + G3 联合提案）

三档梯占六成，而**打满三档才算平仓**，意味着当前删失率曲线的形状主要由"梯子走完需要多久"决定，而不是由信号本身的兑现速度决定。随之而来的是一个 estimand 选择：

**部分出场的 episode（已吃到 TP1/TP2、TP3 未到）目前整条被判右删失、`net_R=null`、被 G3 排除出分母。** 这丢掉了已经实现的那部分信息，且丢弃是系统性的——偏向丢掉走得慢的、括号宽的、梯子长的。

**要求**：在 P1 结束前由 G2 + G3 联合评估并提案，至少覆盖两种口径的对比：
1. **现行**：未完全平仓即删失（保守，但系统性丢弃部分信息，且删失率受梯子档数支配）；
2. **观察终点强制平仓**（以 `horizon_end` 的 mark 价结算余仓）：每条 episode 都可评估、删失率趋零，代价是引入一个估值约定，且 θ 的含义变为"持有至观察终点"的收益。

**G0 不预设结论**——两者是不同的 estimand，各有代价。但必须**明示选择并说明理由**，不能因为"现行实现恰好是这样"而默认。提案须附两种口径下的 `n_evaluated`、有效簇数与 θ 的差异。该选择属 `G-STAT-CLAIM` 实质内容。

#### 3. 占位期版本登记边界（答 G2 的 B4 问题）——**当前不满足，须先补 `PAIR_KEY`**

G2 问：占位期的数值变更可否只迁移登记表、最终值定稿时再一次性 bump `policy_version`？

**B4 真正要防的是"同一版本串对应两套数值"导致不可复算**。若每一处**身份与配对面**都携带 `policy_hash`（内容寻址），版本串就只是人类标签，占位期不 bump 不破坏可复算性。G0 实测当前状况：

| 面 | `policy_version` | `policy_hash` |
|---|---|---|
| `REQUEST_ID_COLS`（→ `trace_hash`） | 有 | **有** |
| G3 尝试账本 | 有 | **有** |
| **`PAIR_KEY`（两臂配对）** | 有 | **无** |

**`PAIR_KEY` 缺 `policy_hash` 是实质缺陷**，不只是记账问题：两套不同数值若共用一个版本串，配对时会被当作同一条臂，**baseline 与 candidate 可能来自不同经济假设而无人察觉**——这比"报告里分不清"严重得多。

**裁定**：
1. **先把 `policy_hash` 加入 `PAIR_KEY`**（无论是否采纳占位期豁免，这都该修）。
2. 补齐后，**占位期豁免成立**：`research_horizon_s` 这类已在契约中明确标注为"占位、待重算"的数值，变更时可只迁移登记表而不 bump 版本串，**条件是**登记表中显式标注占位状态与待定依据。
3. **最终值定稿时必须 bump 一次**（`base-v1` → `base-v2`），并在 changeLog 记录"占位期内经历的全部中间取值"，使历史结果可追溯到具体内容哈希。
4. 非占位数值（费率、滑点模型等）一律按 B4 原文每次 bump，不适用本豁免。

G2 此前"迁移登记表未 bump"的做法，在补齐 `PAIR_KEY` 后**追认**；在此之前该状态属违规，但因所有已产出结果的 `trace_hash` 均含 `policy_hash`，**无实际不可复算后果**。

### 5.19 裁定 B17：B16(3) 的副作用修补 + 突变测试作为门禁验收方式（G0 OR-02 R35，规范性）

#### 1. G0 的 B16(3) 裁定单独执行会拿一道门换另一道门 —— 采纳 G2 的补强

G2 指出：`PAIR_KEY` 在 `execution.py:101` 的唯一用途是 `simulate_batch` 的**重复检测**。把 `policy_hash` 加进键后，"同一版本串、两套内容"的两行会从**重复**变成**两条合法不同的行**——下游配对不再混淆（G0 要的），但**本侧少了一道门**（G0 没想到的）。

**这正是本项目反复出现的形态在 G0 裁定上的又一次现身**：修一处、在别处悄悄放宽，而放宽的那处不会变红。

**裁定**：采纳 G2 提出的补强，作为 B16(3) 的必要组成部分（非可选）：
1. `PAIR_KEY` 加入 `policy_hash`（B16(3) 原文）；
2. **另加批量级断言**：同一 `simulate_batch` 输出内，每个 `policy_version` 必须**恰好映射一个 `policy_hash`**，否则 `ContractError` 并列出冲突取值。合并数据集时"一个版本串两套数值"**当场抛错，而不是悄悄配对**，且不依赖下游是否记得读哈希；
3. 配一条**注入式回归**：构造同版本串双哈希的批量输出，断言必须抛错。

**另记**：G2 在改之前先核实了 G3 侧 `_ARM_KEYS` 已含 `policy_hash`（`evaluator.py:168`），确认这是**单侧修复**而非双侧——其自陈动机是"以为两边都要改、结果只改一边正是 S24 的形态"。G0 复核属实，且 `_CTX_KEYS` 正确地不含 `policy_hash`（两臂本就该按 policy 区分）。

#### 2. 突变测试确立为门禁的验收方式（采纳 G2 的自证方法，补 §9.10.13 A17）

A17 要求门禁被反例钉死，但未规定**如何证明一条断言确实在起作用**。G2 自发做了更强的事：**把原缺陷注入回实现，确认对应测试立刻变红，还原后全绿**。

**裁定：此法为门禁的标准验收方式。** 凡新增或修改验收断言（含 review 类 verify、`verify_report_text` 一类门函数、契约不变量测试），须给出**突变自证**：注入该断言意图防止的原始缺陷 → 断言必须失败；还原 → 必须通过。看板 note 记录注入内容与两次结果。

**配套要求（G2 已自觉遵守，写成规范）**：断言必须写**契约公式本身**（如 `== entry_ttl_s + research_horizon_s`），**不得**写成 `== derived_window_s(...)` 这类对被测实现的同义反复——后者会随实现一起漂移，实现改错时断言跟着改错，正是 A17 要防的"门被写成现状"。

#### 3. B15 三条 G0 独立核验闭合

| 项 | 核验 |
|---|---|
| B15(1) 推导式含 `entry_ttl` | `derived_window_s`（`contract.py:232`）= `entry_ttl_s + (max_holding 或 research_horizon_s)`，与契约逐字一致；**单一来源**，`build_request` 与请求校验共用 |
| B15(2) `research_horizon_s` 必填 | 省略即 `ValidationError` |
| B15(3) 亚秒精确比较 | `horizon` / `ttl` / `t_start` 路径上已无 `int()` 截断 |
| §5.16 B14（G2-SC-01） | `execution.py:248` `rr.get("multiplier") is not None` 已纳入 `rules_known` |

### 5.20 裁定 B18：更正 S31 定性 + funding 网格例外 + 部分出场 estimand（G0 OR-02 R46，规范性）

#### 1. 更正 G0 在 R45 对 S31 的定性（G0 记错，G2 校正属实）

R45 记"**门本身在真实输入上不生效**"。G0 复核十审原文第 111 行，逐字为：

> 真实代码当前并没有 sanitized 替换；这里是标准门禁突变证据，**不混称为当前生产放行漏洞**。

**更正**：S31 是**验证缺陷**，不是生产缺陷。准确表述为：该测试**分辨不出送进门的那张表是否就是 `simulate_batch` 的真实输出**（P10D 把输入换成同高度、hash 全 sanitized 的表后测试仍 GREEN）。而 B17(3) 要求的"删门→回归必须红"**已达成**（P10M 真删门：旧 B16 GREEN、**新 B17 RED**）。

**S31 维持 open、维持阻断、维持由 G2 按 A24 自行注入证明**——定性更正不改变处置。但定性必须准确：**错误定性会让后来者在不存在的地方找问题**，这与写错结论同样有害。G2 坚持校正是对的。

#### 2. funding 网格例外 —— 准，且确立"例外要有证据"的一般规则

G2 以逐行核对真实归档（`note-G2-archive-timestamp-grid.md`，非抽样）给出实测：

| 流 | 行数 | 离网行 | 离网率 |
|---|---|---|---|
| `klines` 1m | 44640 | **0** | 0% |
| `markPriceKlines` 1m | 44640 | **0** | 0% |
| `fundingRate` 8h | 93 | **15** | **16.1%**（偏移仅 +1/+2/+3 ms） |

**裁定**：`check_bars` 采用严格网格规则（真实 bar 类零离网，纯增益）；**`fundingRate` 不适用严格网格**，其现有 60 秒容差正确且比实测抖动大四个数量级，**A24 的树级 lint 不得把 funding 的时长除法一并禁掉并"归一"**——那会逼出一个把 16% 真实结算行判脏的"修复"。

**一般规则（采纳 G2 的表述）**：**例外要有证据，不是有豁免。** 任何对统一规则的例外，必须附**真实数据上的实测**，证明统一规则会误判真实行；仅凭"这里不一样"的论证不成立。反过来，**统一规则也须先验证不误伤真实数据再推行**——本例中 bar 类零离网正是严格规则可推行的证据。

#### 3. 部分出场 estimand：P1 主口径定为 B（观察终点强制平仓），A 作强制敏感性

G2 交付 `report-G2-partial-exit-estimand.md`。**核心发现（G0 复核报告原文确认）**：

- **机制是定义层面的**：止损**全平**、止盈**分档**，因此"未结清"本身就是"未被止损"的证据，**"仍在持仓"与"正在盈利"高度相关**。
- **后果**：口径 A（未完全平仓即删失、剔出分母）**系统性丢掉赢家**，θ_A 向下偏；7d 档被丢弃的 **54 条全部盈利**。
- **最要紧的一条**：**1d 档上 estimand 的选择直接翻转显著性**——A 给 `|t|=3.03`（看着显著），B 给 `|t|=1.45`（不显著），而 **A 的 3.03 测的正是丢掉赢家造成的偏倚量本身**。

**裁定**：
1. **P1 主口径 = B**（`horizon_end` 的 mark 结算余仓）。理由：A 的偏倚是**定义导致的、方向固定的、且会制造虚假显著**；B 引入的是一个**明示的估值约定**（余仓按 mark 计价、不可成交、不含滑点），代价对称、可声明。
2. **A 作为强制敏感性**：每份 θ 报告必须同时给出两种口径的结果与差异，不得只报其一。
3. **任何 θ 声明必须写明所用 estimand**——本例已证明**读数可以差到结论反号**，不写明等同于未声明。
4. **最终口径属 `G-STAT-CLAIM` 实质内容**，P2 真实数据声明时随闸门确认；P1 合成阶段按本裁定执行。

#### 4. `right_censored` 在 B 下的可达性：两条路径分别交代

采纳 B 后 `right_censored` 的**观察窗路径**不可达，但 **`max_holding` 路径仍可达**（要 C06 政策平仓腿落地才关）。按 §5.11 B9：**不可达的合法枚举值须断言其不可达并注明原因，禁止改判**。要求覆盖测试**分两条断言**：观察窗路径"在 B 口径下不可达（原因：余仓在 horizon 强制结算）"、`max_holding` 路径"可达，C06 前保留"。**两条路径不得合并成一句"不可达"**——那会在 C06 落地时无人察觉地变成错的。

#### 5. `G-STAT-CLAIM` 增列最小有效簇数约束（准 G2 所请）

§5.18 B16 仅依**删失率**把 `research_horizon_s` 定为 5d。G2 指出删失率与**有效簇数反向**（窗口越长 → 删失越低，但 `t1` 重叠越多 → 折内有效簇数越少 → 功效越低）。**仅优化删失率会一路选到最长窗口而把统计效率赔光**（§5.18 B16 第 4 节已述原则，此处给出约束形式）。

**裁定**：`G-STAT-CLAIM` 闸门须**同时**约束两项——删失率上限**与**折内最小有效簇数下限；`research_horizon_s` 的最终取值须同时满足。具体数值由 G2 + G3 在重算曲线时联合提议（附两项随窗口变化的对照表），G0 裁定后入契约。**5d 在最小簇数约束确立前维持占位状态。**
