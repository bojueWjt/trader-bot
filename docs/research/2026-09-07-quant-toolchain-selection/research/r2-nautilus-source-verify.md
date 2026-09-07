# R2-1 · NautilusTrader 1.227.0 源码核验（本机，2026-09-07）

来源：PyPI sdist `nautilus_trader-1.227.0.tar.gz`（sha256 fbb9c119…，与生产 uv.lock 一致），解包于 scratchpad。全部为一手源码，可信度高。

## 架构事实
- 1.227.0 的 Python 包 `nautilus_trader/backtest/` 里**没有** `exchange.pyx` / `matching_engine.pyx`；撮合在 Rust：`crates/execution/src/matching_engine/engine.rs`（撮合引擎）、`crates/execution/src/matching_core/mod.rs`（价格核）、`crates/backtest/src/exchange.rs`（SimulatedExchange）。Python `BacktestEngine`（engine.pyx）通过 pyo3 持有 SimulatedExchange。
- `crates/backtest/src/exchange.rs:588` 有 `pub fn send(&mut self, command: TradingCommand)` —— Rust 层存在"直接向交易所发交易命令"的入口；Python 层的常规路径仍是 Strategy → ExecEngine → BacktestExecClient → exchange.send。**结论：可用"计划回放壳策略"承接外部订单计划，无需 hack。**

## 三个待证点的裁定

| 待证点 | 源码证据 | 裁定 |
|---|---|---|
| ① `TriggerType::MARK_PRICE` 在回测是否按 mark price 触发 | `exchange.rs` 公开方法只有 `process_order_book_delta/deltas/depth10`、`process_quote_tick`、`process_trade_tick`、`process_bar`、`process_instrument_status/close`、`process_modules` —— **没有 `process_mark_price`**；`crates/backtest/src` 里 `MarkPriceUpdate` 只出现在 config 数据类型枚举与 data_client 的 subscribe 桩，撮合不消费。`engine.rs:3531-3543` 触发价选择：`LastPrice => last`、`LastOrBidAsk => last or bid/ask`、**其余（含 MarkPrice、IndexPrice）=> bid/ask**。止损触发判定 `matching_core/mod.rs:594 is_touch_triggered`：买 `ask <= trigger`、卖 `bid >= trigger`。 | **不生效**。mark price 数据可作为 Data 喂给策略，但止损/止盈触发一律按 bid/ask（bar 驱动时即 last）。要建模 mark 触发只能在自研层或用 mark 1m K 线作为"驱动 bar"再喂一次。 |
| ② 回测账户是否结算资金费率 | `grep -ri funding crates/backtest/src crates/execution/src/matching_engine` **零命中**。 | **不结算**。funding 必须外挂：用 `monthly/fundingRate` 归档按结算时间对持仓名义值后算，与 Nautilus 的 commissions 分列。 |
| ③ bar 驱动下 post-only / IOC 语义 | `engine.rs:1412 process_bar` → `process_trade_ticks_from_bar`（1496）：把 bar 拆成 O→H→L→C 四个 TradeTick 依序撮合；`bar_adaptive_high_low_ordering` 为真时若 low 更接近 open 则 L 先于 H（1541-1542）；每个 tick 后 `set_last_raw`，并经 L1 book 更新 `set_bid_raw/set_ask_raw`（`process_trade_tick` 1695-1741）→ **bid = ask = last**。post-only 拒单条件 `engine.rs:2985`：`is_post_only && is_limit_matched(side, px)` 即限价穿过 bid/ask 就拒（消息 "would have been a TAKER"）。IOC/FOK：`engine.rs:3038` 提交时若不能立即成交则取消；`4346` IOC 部分成交后剩余量取消。open tick 段 `fill_at_market=true`（跳空按市价成交），H/L/C 段 `fill_at_market=false`（按触发价成交）。 | **可用但语义是 tick 化 OHLC**：post-only 只在限价与 last 穿越时拒，不模拟真实盘口排队；IOC 余量取消已建模；限价成交条件是"价格触及即成交"（`is_limit_matched`），比审稿要求的"穿越才成交"更乐观，需用 `FillModel.prob_fill_on_limit<1` 或自研层做保守界。 |

## 其他已确认
- FillModel（`nautilus_trader/backtest/models/fill.pyx`）：`prob_fill_on_limit`、`prob_slippage`；matching core 有 `fill_limit_inside_spread` 开关。
- `bar_execution` / `bar_adaptive_high_low_ordering` 为 BacktestVenueConfig 字段（`crates/backtest/src/config.rs:298-300`）。
- 若同时提供 bid 与 ask 两种 bar（`process_quote_ticks_from_bar` 1609），bid/ask 分离，post-only 语义才接近真实；但 Binance 归档只有成交 K 线与 bookTicker（2023-05 起日包），前者做不到 bid/ask bar。

## 含义
1. Nautilus 作主引擎成立：订单状态机、tick/step/min-notional、IOC、reduce-only、OrderList、GTD 到期、同 bar 自适应排序全部一手确认。
2. 三件事必须由自研差分层补：mark price 触发、funding 结算、同 bar 乐观/悲观双界（Nautilus 只有自适应一种）。
3. "回测与生产同版本"是真实优势：撮合语义与 1.227.0 生产节点共用同一 Rust 引擎代码路径的订单 FSM。
