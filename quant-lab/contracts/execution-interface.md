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
| 4 | 未成交且事件含 `expired` | `unfilled_expired` |
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
