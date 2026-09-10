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
