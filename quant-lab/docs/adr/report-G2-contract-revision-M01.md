# G2 → G0：execution-interface 签名定稿修订意见（M-01）

> 状态：G2 提交，等 G0 在 OR-01 裁定后改 `contracts/execution-interface.md`。G1（行情校验层 Telegram-5）与 G3（feature_snapshot / EventEvaluator）可按本文签名写桩；G0 若否决某条，以契约为准，G2 同步改实现。
> 依据：合并稿 C.1 / C.2 / D.4，选型报告 Q3 / Q4。

## 0. 结论先行（G1/G3 写桩只需看这一节）

| # | 契约位置 | 现状 | 修订 | 影响 |
|---|---|---|---|---|
| R1 | §2 `mark_price_at` | 无数据源参数，纯函数无法实现 | 首位加 `marks: pl.DataFrame`（silver markPriceKlines 1m 视图）；返回值**保持** `(price, reason)` 二元组；另加 `mark_bar_at(...) -> MarkAt` 返回富记录（含 `close_time`、`staleness_s`） | G1 校验层：δ 计算用 `mark_bar_at` 拿 m 与其 close_time |
| R2 | §2 `asof_join` | `sequence_cols` 语义与输出列未定 | 见 §2.1：左连接保留全部左行；输出加 `asof_reason` 列（null / `MARK_STALE` / `NO_PRIOR`）；右表 `(by, right_on, seq)` 非唯一 → 抛 `AsOfKeyDuplicate`；右列冲突加后缀 `_r` | G3 特征对齐用 `last_closed_bar`，不直接用 `asof_join` |
| R3 | §2 `last_closed_bar` | 无延迟假设 | 加 `latency: timedelta = 0`（H0：`available_at = close_time + latency`，写进 manifest `available_at_basis`）；`close_time` 定义为 **bar 区间右端点**（`open_time + interval`），不是 Binance 的 `open_time + interval − 1ms` | G3 契约 §3 "取 `close_time <= t_dec` 最后一根" 语义不变 |
| R4 | §1 silver bar 列 | 未列 | 固定列名与 dtype（§1.1），OHLC 用 `Float64`，Decimal 只在执行合同层 | G1/G3 桩直接照 §1.1 造合成 bars |
| R5 | §3 `order_plan` | `size`、`stop`、`expiry` 类型未定 | §3.1：`sizing ∈ {fixed_qty, risk_budget}`；`stop.trigger = mark`；`tps` 按 last 撮合；`expiry = {entry_ttl_s, max_holding_s}`；每 entry 带 `tif ∈ {GTC, GTD, IOC}` | G1 规范化输出 order_plan 时按此填 |
| R6 | §3 ExecutionRequest | 缺观察终点 | 加 `horizon_end`（右删失边界）与 `t_start`（= t_dec + 政策延迟，默认由 policy 决定） | G3 配对时用 `horizon_end` 判删失 |
| R7 | §3 canonical_events | 缺归因字段 | struct 加 `order_id, leg ∈ {entry, sl, tp, close}, trigger_basis ∈ {mark, last, funding, expiry, none}, bar_open_time, path_step ∈ {O,H,L,C,none}`；`kind` 加 `amended` | 候选 B 对拍的解释码按 `leg × trigger_basis` 归类 |
| R8 | §3 ExecutionResult | 缺辅助指标与内核版本 | 加 `kernel ∈ {A,B}, kernel_version, entry_avg_price, exit_avg_price, position_open_at, position_close_at, mae_R, mfe_R, gross_pnl`；`coverage_mask` 加 `bars_ok, liquidation_unmodeled` | G3 "尾损/未闭合率" 用 `mae_R`、`position_close_at is null` |
| R9 | §3 `simulate_batch` | 输出列未定 | §3.3：标量列平铺 + `canonical_events` list[struct] 列；配对键 `(episode_id, graph_version, policy_version, cost_scenario, path_scenario, kernel)`；**不含 candidate 概念**，take/skip 由 G3 在自己侧应用 | G3 EventEvaluator 的 `execution` 入参按此列名 |
| R10 | §3 `net_R` | 两句冲突（"skip/未成交=0" 与 "censor 非空时 null"） | 优先级：`censor_reason` 非空 → `net_R=null, net_pnl=null`；否则未成交（`fill_status=none`）→ `net_R=0`；skip 不在 G2 出现（G3 侧记 0） | 不变量"censor 非空 ⇒ net_R null"保留 |
| R11 | 看板 M-01/D-01/R-01 verify | `pytest -q --co \| grep 'collected [1-9]'` 永远不匹配（pytest 9 的 -q 输出是 `N tests collected`） | 改为 `grep -E '([1-9][0-9]* tests? collected\|collected [1-9])'` 或去掉 `-q` | 三个窗口 verify 同病 |

## 1. §1 行情湖分区补充

### 1.1 silver bar 表列（klines / markPriceKlines / indexPriceKlines / premiumIndexKlines 共用）

| 列 | dtype | 说明 |
|---|---|---|
| `instrument_id` | `Utf8` | `BTCUSDT-PERP.BINANCE-UM` 形式（venue/market 进 id，避免与现货撞名） |
| `interval` | `Utf8` | `1m / 15m / 8h / 5m` |
| `open_time` | `Datetime("us","UTC")` | Binance 归档毫秒统一到微秒 |
| `close_time` | `Datetime("us","UTC")` | **= open_time + interval（右端点）**；原始 `close_time_raw`（`+interval−1ms`）保留 |
| `open/high/low/close` | `Float64` | 价格；精度以 `instrument_rules.tick_size` 为准，执行层再量化到 Decimal |
| `volume, quote_volume, taker_buy_volume, taker_buy_quote_volume` | `Float64` | markPrice/indexPrice/premiumIndex 包这些列为 0，统一保留列名 |
| `trades` | `Int64` | 同上，非 klines 包为 0 |
| `event_time` | `Datetime` | = `close_time`（合并稿 C.1：bar 覆盖区间终点） |
| `available_at` | `Datetime` | H0：`close_time + latency`（默认 latency=0，manifest 记 `available_at_basis="H0_close_plus_0s"`）；**不是实际已知**，晚导入不倒推 |
| `ingested_at` | `Datetime` | 入湖时刻 |
| `ohlc_valid, spike_flag, gap_flag` | `Boolean` | 分区体检产出（M-04）；bronze→silver 初写全为 `ohlc_valid` 实算、`spike_flag/gap_flag` 由体检回填 |
| `spike_score` | `Float64` | 尖刺分数（只标不删） |
| `source_sha256, rule_version` | `Utf8` | 血缘 |

fundingRate 表：`instrument_id, calc_time(us,UTC), funding_interval_hours: Int64, funding_rate: Float64, event_time=calc_time, available_at=calc_time, ingested_at`。metrics 表：`instrument_id, create_time(us,UTC), sum_open_interest, sum_open_interest_value, count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio, sum_taker_long_short_vol_ratio`（字符串时间 → 毫秒向下取整，规则写死进 manifest）。

### 1.2 manifest 补充字段
`partition_id, data_type, interval, instrument_id, period(yyyy-mm 或 yyyy-mm-dd), source_uri, source_sha256, checksum_source ∈ {vision_CHECKSUM, computed}, downloaded_at, parser_version, zip_member, expected_rows, actual_rows, missing, duplicates, schema_hash, quarantine_n, rule_version, available_at_basis, status ∈ {ok, gap, quarantined}`。

## 2. §2 三时钟 as-of 库

### 2.1 `asof_join`
```python
def asof_join(
    left: pl.DataFrame, right: pl.DataFrame, *,
    left_on: str = "t_dec", right_on: str = "available_at",
    by: list[str] | None = None,
    strategy: Literal["strict_lt", "le_with_sequence"] = "strict_lt",
    tolerance: timedelta | None = None,
    sequence_cols: tuple[str, str] | None = None,   # (left_seq, right_seq)，仅 le_with_sequence 用
    suffix: str = "_r",
) -> pl.DataFrame
```
- 语义：`strict_lt` 取 `right_on < left_on` 的最后一行；`le_with_sequence` 取 `right_on < left_on` 或 `(right_on == left_on and right_seq < left_seq)` 的最后一行——**等号只在有顺序证据时可用**（合并稿 C.1）。
- 输出：左连接，全部左行保留；右列同名加 `suffix`；新增 `asof_reason: Utf8`：匹配 → null；无更早行 → `NO_PRIOR`；`tolerance` 超限 → `MARK_STALE`（右侧列置 null，不前填）。
- 前置校验：右表 `(by…, right_on[, right_seq])` 必须唯一，否则抛 `AsOfKeyDuplicate`（Q3：右表先去重，不靠未文档化顺序）。
- 时区：两侧时间列必须为 `Datetime(_, "UTC")`，否则抛 `TimeUnitInvalid`（原因码 `TIME_UNIT_INVALID`）。

### 2.2 `last_closed_bar`
```python
def last_closed_bar(bars: pl.DataFrame, *, at: datetime, instrument_id: str, interval: str,
                    latency: timedelta = timedelta(0)) -> pl.DataFrame | None
```
只取 `close_time + latency <= at` 的最后一根；`at` 必须 tz-aware UTC；返回 1 行 DataFrame 或 None。**等号成立**（bar 在 close_time 整点即视为闭合，对应 H0；latency>0 是模型假设并进 manifest）。跨周期：15m bars 里 `at=10:07` 不可见 `10:00–10:15` 那根。

### 2.3 `mark_price_at` / `mark_bar_at`
```python
class MarkAt(NamedTuple):
    price: Decimal | None; reason: str | None; close_time: datetime | None; staleness_s: float | None
def mark_bar_at(marks: pl.DataFrame, at: datetime, instrument_id: str, *, max_staleness_s: int = 120) -> MarkAt
def mark_price_at(marks: pl.DataFrame, at: datetime, instrument_id: str, *, max_staleness_s: int = 120) -> tuple[Decimal | None, str | None]
```
- m = `last_closed_bar(marks, at=at, interval="1m")` 的 `close`；`staleness_s = at − close_time`；`> max_staleness_s` 或无 bar → `(None, "MARK_STALE")`（合并稿 C.3：MARK_STALE 覆盖"过旧/不存在"）。
- 绝不取包含 `at` 但未闭合的 bar。
- 价格以 `Decimal(str(close))` 量化到 `tick_size`（规则表缺 → 不量化并带 `RULE_HISTORY_MISSING` 作第二原因，见 M-04）。

## 3. §3 执行合同

### 3.1 `order_plan` 结构（冻结的计划）
```
order_plan:
  instrument_id: str
  side: long | short
  entries: [{kind: limit | market_ref | ladder, price_lo: Decimal, price_hi: Decimal, fraction: Decimal, tif: GTC|GTD|IOC, post_only: bool=false}]
  stop:   {price: Decimal, trigger: mark}            # SL 只按 mark 触发（Binance algo 默认 MARK_PRICE）
  tps:    [{level: Decimal, fraction: Decimal}]       # 按 last 撮合的 reduce-only 限价；fraction 和 ≤ 1
  sizing: {mode: fixed_qty | risk_budget, qty: Decimal | null}   # risk_budget 模式：qty = risk_budget / |entry_ref − stop.price|，向下量化到 step_size
  expiry: {entry_ttl_s: int, max_holding_s: int | null}          # 入场未成交到期；最大持仓时间（null = 到 horizon_end）
  reduce_only_exit: true                                         # 全部出场腿 reduce-only
```
`ladder` 在 `[price_lo, price_hi]` 内按 `policy_version` 定义的档数均匀展开；`market_ref` 表示"以 t_start 首个可用 last 成交"（滑点按 `cost_scenario`）。

### 3.2 ExecutionRequest 新增 / 明确
| 字段 | 类型 | 说明 |
|---|---|---|
| `t_start` | timestamp | 内核开始处理的时刻，默认 `t_dec + policy.latency`（policy 决定） |
| `horizon_end` | timestamp | 观察终点；`max_holding` 或数据末端更早者；到点仓位未闭 → `censor_reason=LABEL_RIGHT_CENSORED` |
| `position_mode` | enum{one_way} | v0 只支持单向；`isolated` 保证金、**不建模强平**（`coverage_mask.liquidation_unmodeled=true`） |
| `fee_rates` | 由 `cost_scenario` 展开 | base: maker 0.02% / taker 0.05%，滑点 0；stress: taker 0.05% 全部按 taker、滑点 1 tick + 0.02%；具体数值进 `policy_version` 文档 |

### 3.3 ExecutionResult 新增 / 明确
- `canonical_events[]` struct：`seq: Int64, ts: Datetime, kind, order_id: Utf8, leg ∈ {entry, sl, tp, close, funding}, trigger_basis ∈ {mark, last, funding, expiry, none}, price: Decimal|null, qty: Decimal|null, fee: Decimal|null, reason: Utf8|null, bar_open_time: Datetime|null, path_step ∈ {O,H,L,C,none}`。`kind` 集合加 `amended`。`seq` 从 0 单调，同 ts 内按 `(kind 优先级, order_id)` 确定性排序，优先级 = funding < stop_triggered < tp_triggered < fill 类 < cancel/expire < closed。
- 标量新增：`kernel: A|B, kernel_version: Utf8, entry_avg_price, exit_avg_price: Decimal|null, position_open_at, position_close_at: Datetime|null, gross_pnl: Decimal|null, mae_R, mfe_R: Decimal|null`（按 mark 逐 bar 计算的最大不利/有利偏移，除以 risk_budget）。
- `coverage_mask`：`{mark_ok, funding_ok, rules_ok, bars_ok, liquidation_unmodeled}`。
- `censor_reason` 允许值：`LABEL_RIGHT_CENSORED, MARK_STALE, BAR_GAP, FUNDING_SCHEDULE_GAP, RULE_HISTORY_MISSING, SYMBOL_TIME_INVALID`。
- `net_R` 优先级见 R10。
- `trace_hash = sha256(canonical_json(execution_contract_version, kernel, kernel_version, request_canonical, canonical_events))`，Decimal 序列化为字符串、时间为 ISO-8601 UTC 微秒；同输入同合同重放一致；**不含** `ingested_at` 等非决定性字段。

`simulate_batch(reqs, *, kernel="A") -> pl.DataFrame`：一行一 request；列 = ExecutionRequest 配对键（`episode_id, graph_version, decision_snapshot_hash, t_dec, policy_version, cost_scenario, path_scenario, market_manifest, execution_contract_version, seed`）+ ExecutionResult 全部标量 + `canonical_events: List(Struct)`。Decimal 列在 DataFrame 中为 `Decimal(38, 12)`（G3 需要 float 时自行 cast）。

### 3.4 不变量（补一条）
现有七条保留；补：`trigger_basis=mark` 的事件只允许 `kind ∈ {stop_triggered}`，`tp_triggered` 只允许 `trigger_basis=last`（防止 mark/last 混用）。

## 4. 未决（G0 裁定）
1. `instrument_id` 命名：`BTCUSDT-PERP.BINANCE-UM`（G2 建议）还是与 G1 品种映射表一致的裸 `BTCUSDT`？G2 实现两者互转 helper，契约选一个。
2. `available_at` H0 默认 latency 取 0 还是 1s？G2 实现参数化，默认 0。
3. `fee_rates` 数值是否进契约，还是只进 `policy_version` 文档？G2 建议进 policy 文档，契约只定 enum。
