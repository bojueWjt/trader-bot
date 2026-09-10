# 契约：execution-interface（G2 market provides）

> 状态：v0 草案，G0 在 OR-01 定稿。来源：合并稿 C.1 / C.2 行情流 / D.4，选型报告 Q3 / Q4。

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
| `net_pnl` / `net_R` | decimal（`net_R = net_pnl / risk_budget`，skip 或未成交 = 0） |
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
