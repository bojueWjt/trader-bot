"""quant_lab.market.nautilus_adapter —— 候选 B spike：Nautilus 1.227.0 + SimulationModule（ADR-G2 §9；M-08）。

结构：
- `MarkFundingModule(SimulationModule)`：持有 mark 流与 funding 结算表。`pre_process(TradeTick)` 在撮合前
  (1) 以 q(t−) 结算 calc_time ≤ tick.ts 的 funding（`exchange.adjust_account`），(2) 用 ts ≤ tick.ts 的 mark 判 SL 触发，
  触发即通过策略壳释放 reduce-only market（在本 tick 后的命令排空/iterate 中按本 tick 价成交）。mark-only 时刻的触发
  在下一 last tick 到来时释放（与 A 的"只释放不成交"一致）。
- `PlanShell(Strategy)`：无信号策略壳，t_start 提交入场限价/市价单，entry fill 后建 reduce-only TP 限价单，事件全部记录。
- 事件由 B 自己归约成 canonical（不调用 kernel_a 的任何 helper）。

已知不能覆盖（§9.3）：真实盘口队列、原子 OCO、滑点模型（B 用 book 价）、跳空时限价改善（B 按限价成交）。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from quant_lab.market.contract import (
    Bar, CanonicalEvent, CoverageMask, ExecutionPolicy, ExecutionRequest, ExecutionResult, MarketView, PricePoint, P2_UNSUPPORTED,
    entry_expiry_at, floor_step, quantize_money, quantize_ratio, request_canonical, resolve_policy, trace_hash,
)

KERNEL_VERSION = "kernel-b-nautilus-1.227.0-spike-v0.3"
ZERO = Decimal(0)


def kernel_build_id() -> str:
    import hashlib
    from pathlib import Path
    h = hashlib.sha256()
    for name in ("nautilus_adapter.py", "contract.py"):
        h.update((Path(__file__).parent / name).read_bytes())
    try:
        import nautilus_trader
        nv = nautilus_trader.__version__
        meta = Path(nautilus_trader.__file__).parent.parent / f"nautilus_trader-{nv}.dist-info" / "RECORD"
        if meta.exists():
            h.update(meta.read_bytes())          # 已安装制品清单摘要（wheel 内容身份）
    except Exception:  # noqa: BLE001
        nv = "?"
    return f"{KERNEL_VERSION}+nautilus{nv}+{h.hexdigest()[:12]}"
NS = 1_000_000_000
BIG = Decimal("1000000000")


def _ns(t: dt.datetime) -> int:
    return int(t.timestamp() * NS) if t.microsecond == 0 else int(t.replace(microsecond=0).timestamp()) * NS + t.microsecond * 1000


def _dt(ns: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ns // NS, dt.UTC) + dt.timedelta(microseconds=(ns % NS) // 1000)


def expand_bar_b(b: Bar, scenario: str, side: str) -> list[PricePoint]:
    """B 自己的路径合成（与 A 独立实现，同一定义：primary 等距 H 先；adverse/favorable 按方向）。"""
    o, h, l, c = b.o, b.h, b.l, b.c
    if scenario == "primary":
        seq = "OLHC" if abs(o - l) < abs(h - o) else "OHLC"
    elif scenario == "adverse":
        seq = "OLHC" if side == "long" else "OHLC"
    else:
        seq = "OHLC" if side == "long" else "OLHC"
    px = {"O": o, "H": h, "L": l, "C": c}
    iv = dt.timedelta(seconds=b.interval_s)
    offs = [dt.timedelta(0), iv / 3, iv * 2 / 3, iv - dt.timedelta(microseconds=1)]
    return [PricePoint(ts=b.open_time + offs[i], price=px[s], capacity=None, bar_open_time=b.open_time, path_step=s) for i, s in enumerate(seq)]


class KernelBUnavailable(RuntimeError):
    pass


def _replay_ledger(events: list[dict], sign: int, mult: Decimal = Decimal(1)) -> dict:
    pos = entry_qty = entry_cost = exit_qty = exit_value = realized = fees = funding = ZERO
    open_at = close_at = None
    closed = False
    for e in sorted(events, key=lambda x: x["ts"]):
        if e["fee"] is not None:
            fees += e["fee"]
        if e["kind"] in ("filled", "partial_fill"):
            if e["leg"] == "entry":
                pos += sign * e["qty"]; entry_qty += e["qty"]; entry_cost += e["price"] * e["qty"]
                open_at = open_at or e["ts"]
            else:
                avg = entry_cost / entry_qty
                realized += (e["price"] - avg) * e["qty"] * sign * mult
                pos -= sign * e["qty"]; exit_qty += e["qty"]; exit_value += e["price"] * e["qty"]
        elif e["kind"] == "funding":
            funding += e["cash_delta"]
        elif e["kind"] == "closed":
            closed = True
            close_at = e["ts"] if entry_qty > 0 else None
    return dict(pos=pos, entry_qty=entry_qty, entry_cost=entry_cost, exit_qty=exit_qty, exit_value=exit_value, realized=realized,
                fees=fees, funding=funding, open_at=open_at, close_at=close_at, closed=closed)


def simulate_b(req: ExecutionRequest, market: MarketView, policy: ExecutionPolicy | None = None) -> ExecutionResult:
    try:
        return _simulate_b(req, market, policy or resolve_policy(req.policy_version))
    except ImportError as e:  # pragma: no cover
        raise KernelBUnavailable(str(e)) from e


def _simulate_b(req: ExecutionRequest, market: MarketView, policy: ExecutionPolicy) -> ExecutionResult:
    if policy.content_hash != req.policy_hash:
        from quant_lab.market.contract import ContractError
        raise ContractError("request.policy_hash 与当前政策内容不符，拒绝执行（S12）")
    from nautilus_trader.backtest.config import SimulationModuleConfig
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.backtest.modules import SimulationModule
    from nautilus_trader.common.config import LoggingConfig
    from nautilus_trader.model.currencies import BTC, USDT
    from nautilus_trader.model.data import TradeTick
    from nautilus_trader.model.enums import AccountType, AggressorSide, OmsType, OrderSide, TimeInForce
    from nautilus_trader.model.events import (
        OrderAccepted, OrderCanceled, OrderDenied, OrderExpired, OrderFilled, OrderRejected, OrderSubmitted, OrderUpdated,
    )
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
    from nautilus_trader.model.instruments import CryptoPerpetual
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy, StrategyConfig

    plan = req.order_plan
    sign = plan.sign
    cost = policy.cost(req.cost_scenario)
    rules = market.rules
    t_start = req.resolved_t_start(policy)
    if len(req.entry_fractions) != len(plan.entries) or len(req.tp_fractions) != len(plan.tps):
        from quant_lab.market.contract import ContractError
        raise ContractError("request.entry_fractions/tp_fractions 与 order_plan 长度不符")
    end = req.horizon_end
    price_prec = max(0, -rules.tick_size.normalize().as_tuple().exponent) if rules.tick_size > 0 else 8
    size_prec = max(0, -rules.step_size.normalize().as_tuple().exponent) if rules.step_size > 0 else 8
    venue = Venue("SIM")
    iid = InstrumentId(Symbol("PLAN-PERP"), venue)
    inst = CryptoPerpetual(
        instrument_id=iid, raw_symbol=Symbol("PLAN"), base_currency=BTC, quote_currency=USDT, settlement_currency=USDT,
        is_inverse=False, price_precision=price_prec, size_precision=size_prec,
        price_increment=Price(rules.tick_size if rules.tick_size > 0 else Decimal("0.00000001"), price_prec),
        size_increment=Quantity(rules.step_size if rules.step_size > 0 else Decimal("0.00000001"), size_prec),
        min_notional=Money(rules.min_notional, USDT) if rules.min_notional > 0 else None,
        maker_fee=cost.maker_fee, taker_fee=cost.taker_fee, ts_event=0, ts_init=0,
    )

    # ------------------------------------------------------------ 行情
    lasts = list(market.last) + [p for b in market.bars_last if b.open_time >= t_start for p in expand_bar_b(b, req.path_scenario, plan.side)]
    # S03：mark bars 同样只展开 open_time >= t_start；启动前只保留已闭合的 mark（显式点或早于 t_start 的 bar 的 C 点）
    marks = sorted(list(market.mark)
                   + [p for b in market.bars_mark if b.open_time >= t_start for p in expand_bar_b(b, req.path_scenario, plan.side)]
                   + [p for b in market.bars_mark if b.open_time < t_start for p in expand_bar_b(b, req.path_scenario, plan.side) if p.path_step == "C" and p.ts <= t_start],
                   key=lambda p: p.ts)
    mult = rules.multiplier
    if market.funding and len({f.calc_time for f in market.funding}) != len({(f.calc_time, f.rate) for f in market.funding}):
        from quant_lab.market.contract import ContractError
        raise ContractError("funding 同 calc_time 冲突费率：同快照重复键拒收（S02）")
    lasts = sorted([p for p in lasts if t_start <= p.ts <= end], key=lambda p: p.ts)
    ticks = []
    for i, p in enumerate(lasts):
        size = p.capacity if p.capacity is not None else BIG
        if size <= 0:
            size = Decimal(rules.step_size if rules.step_size > 0 else "0.00000001")   # 零容量：最小步（B 不能表达 0 容量，记 B_LIQUIDITY_MODEL）
        ticks.append(TradeTick(iid, Price(p.price, price_prec), Quantity(size, size_prec), AggressorSide.NO_AGGRESSOR, TradeId(f"t{i}"), _ns(p.ts), _ns(p.ts)))
    point_by_ns = {_ns(p.ts): p for p in lasts}

    # ------------------------------------------------------------ 共享状态（B 自己的账本）
    st = {
        "events": [], "pos": ZERO, "entry_qty": ZERO, "entry_cost": ZERO, "exit_qty": ZERO, "exit_value": ZERO, "realized": ZERO,
        "fees": ZERO, "funding": ZERO, "slippage": ZERO, "open_at": None, "close_at": None, "closed": False, "censor": None,
        "cov": {"mark_ok": True, "funding_ok": True, "rules_ok": True, "bars_ok": True}, "min_p": ZERO, "max_p": ZERO,
        "sl_triggered": False, "sl_qty": ZERO, "tag_of": {}, "tp_orders": {}, "entry_orders": {}, "plan_qty": ZERO,
        "funding_applied": 0, "mark": None, "mark_ts": None, "exit_latch": False, "diag": [],
    }

    def emit(ts, kind, oid, leg, tb="none", price=None, qty=None, fee=None, reason=None, bar=None, step="none", cash_delta=None):
        st["events"].append(dict(ts=ts, kind=kind, order_id=oid, leg=leg, trigger_basis=tb, price=price, qty=qty, fee=fee, reason=reason,
                                 bar_open_time=bar, path_step=step, cash_delta=cash_delta))

    def bar_step(ts_ns):
        p = point_by_ns.get(ts_ns)
        return (p.bar_open_time, p.path_step) if p else (None, "none")

    # ------------------------------------------------------------ 策略壳
    class ShellConfig(StrategyConfig, frozen=True):
        pass

    class PlanShell(Strategy):
        def __init__(self):
            super().__init__(ShellConfig(strategy_id="PLAN-SHELL-001"))
            self.module = None

        def on_start(self):
            self.subscribe_trade_ticks(iid)
            self._submit_entries()

        def _submit_entries(self):
            ts = t_start
            total = self._sizing_total()
            st["plan_qty"] = total
            legs = []
            for e, frac in zip(plan.entries, req.entry_fractions):
                prices = [None] if e.kind == "market_ref" else self._ladder(e)
                leg_total = total * frac
                per = floor_step(leg_total / len(prices), rules.step_size)
                qtys = [per] * len(prices)
                rem = floor_step(leg_total, rules.step_size) - per * len(prices)
                i = 0
                while rules.step_size > 0 and rem >= rules.step_size:
                    qtys[i % len(prices)] += rules.step_size
                    rem -= rules.step_size
                    i += 1
                legs += [(e, p, q) for p, q in zip(prices, qtys)]
            # filter（ADAPTER_FILTER：Nautilus 原生只在 Price/Quantity 精度与 min_notional 上拒绝；其余由适配层补做）
            reject = None
            reserve = ZERO
            for e, p, q in legs:
                ref = p if p is not None else (e.price_lo + e.price_hi) / 2
                if p is not None and rules.tick_size > 0 and (p / rules.tick_size) % 1 != 0:
                    reject = reject or "PRICE_FILTER"
                if q <= 0 or (rules.step_size > 0 and (q / rules.step_size) % 1 != 0):
                    reject = reject or "LOT_SIZE"
                if ref * q * rules.multiplier < rules.min_notional:
                    reject = reject or "MIN_NOTIONAL"
                reserve += ref * q * rules.multiplier / policy.leverage
            if reject is None and reserve > policy.wallet:
                reject = "MARGIN"
            deadline = entry_expiry_at(t_start, req.entry_ttl_s)
            side = OrderSide.BUY if sign > 0 else OrderSide.SELL
            for i, (e, p, q) in enumerate(legs):
                tag = f"entry-{i}"
                if reject:
                    emit(ts, "submitted", tag, "entry", price=p, qty=q)
                    emit(ts, "rejected", tag, "entry", reason=reject)
                    continue
                if p is None:
                    o = self.order_factory.market(iid, side, Quantity(q, size_prec), tags=[tag])
                else:
                    tif = TimeInForce.IOC if e.tif == "IOC" else (TimeInForce.GTD if deadline <= end else TimeInForce.GTC)
                    o = self.order_factory.limit(iid, side, Quantity(q, size_prec), Price(p, price_prec), time_in_force=tif,
                                                 expire_time=deadline if tif == TimeInForce.GTD else None, post_only=e.post_only, tags=[tag])
                st["tag_of"][o.client_order_id] = tag
                st["entry_orders"][tag] = o
                emit(ts, "submitted", tag, "entry", price=p, qty=q)
                self.submit_order(o)
            if reject:
                emit(ts, "closed", "bracket-0", "close", reason="no_fill")
                st["closed"] = True

        def _sizing_total(self):
            s = plan.sizing
            if s.mode == "fixed_qty":
                return floor_step(s.qty, rules.step_size)
            ref = sum(((e.price_lo + e.price_hi) / 2) * f for e, f in zip(plan.entries, req.entry_fractions))
            dist = abs(ref - plan.stop.price) * rules.multiplier
            return floor_step(req.risk_budget / dist, rules.step_size) if dist > 0 else ZERO

        def _ladder(self, e):
            n = policy.ladder_steps
            if e.kind != "ladder" or n <= 1 or e.price_lo == e.price_hi:
                return [(e.price_lo + e.price_hi) / 2 if e.kind == "ladder" else e.price_lo]
            stepp = (e.price_hi - e.price_lo) / (n - 1)
            pts = [e.price_hi - stepp * i for i in range(n)] if sign > 0 else [e.price_lo + stepp * i for i in range(n)]
            return pts

        # ---- 事件
        def on_event(self, event):
            tag = st["tag_of"].get(getattr(event, "client_order_id", None))
            if tag is None:
                return
            ts = _dt(event.ts_event)
            leg = tag.split("-")[0]
            exec_sl = tag.endswith("#exec")
            if isinstance(event, OrderSubmitted):
                return   # on_start 阶段策略未 RUNNING，OrderSubmitted 会被丢弃；submitted 统一在提交处由壳自己记录
            elif isinstance(event, OrderAccepted):
                if exec_sl:
                    return
                emit(ts, "accepted", tag, leg)
                o = self.cache.order(event.client_order_id)
                if o.has_price and o.time_in_force != TimeInForce.IOC:
                    emit(ts, "working", tag, leg)
            elif isinstance(event, (OrderRejected, OrderDenied)):
                # 释放的 SL market 被原生 reduce-only 拒绝（仓位已被同刻 TP 平掉）：记在 sl-0 上，解释码 B_COMMAND_LATENCY
                emit(ts, "rejected", tag.replace("#exec", ""), "sl" if exec_sl else leg, reason=getattr(event, "reason", None))
            elif isinstance(event, OrderCanceled):
                o = self.cache.order(event.client_order_id)
                reason = st.get("cancel_reason", {}).get(tag)
                emit(ts, "cancelled", tag, leg, qty=(o.leaves_qty.as_decimal() if reason == "ioc_remainder" else None), reason=reason or "cancelled")
            elif isinstance(event, OrderExpired):
                o = self.cache.order(event.client_order_id)
                emit(ts, "expired", tag, leg, tb="expiry", qty=o.leaves_qty.as_decimal(), reason="entry_ttl")
                if st["pos"] == 0 and st["entry_qty"] == 0 and not any(x.is_open for x in st["entry_orders"].values()):
                    emit(ts, "closed", "bracket-0", "close", reason="no_fill")
                    st["closed"] = True
            elif isinstance(event, OrderUpdated):
                emit(ts, "amended", tag, leg, price=(event.price.as_decimal() if event.price is not None else None), qty=event.quantity.as_decimal())
            elif isinstance(event, OrderFilled):
                self._on_fill(event, tag, leg if not exec_sl else "sl", ts)

        def _on_fill(self, ev, tag, leg, ts):
            px = ev.last_px.as_decimal()
            q = ev.last_qty.as_decimal()
            fee = quantize_money((ev.commission.as_decimal() if ev.commission is not None else ZERO) * mult)   # S08：原生费按名义额，乘 multiplier
            o = self.cache.order(ev.client_order_id)
            kind = "filled" if o.leaves_qty.as_decimal() == 0 else "partial_fill"
            bar, step = bar_step(ev.ts_event)
            oid = tag.replace("#exec", "")
            if leg == "tp":
                emit(ts, "tp_triggered", oid, "tp", tb="last", price=px, qty=q + o.leaves_qty.as_decimal(), bar=bar, step=step)
            emit(ts, kind, oid, leg, tb="last", price=px, qty=q, fee=fee, bar=bar, step=step)
            st["fees"] += fee
            if leg == "entry":
                st["pos"] += sign * q
                st["entry_qty"] += q
                st["entry_cost"] += px * q
                if st["open_at"] is None:
                    st["open_at"] = ts
                self._protect(ts)
            else:
                avg = st["entry_cost"] / st["entry_qty"]
                st["realized"] += (px - avg) * q * sign * mult
                st["pos"] -= sign * q
                st["exit_qty"] += q
                st["exit_value"] += px * q
                if leg == "tp" and not st["exit_latch"]:
                    st["exit_latch"] = True
                    for t_, eo in st["entry_orders"].items():
                        if eo.is_open:
                            st.setdefault("cancel_reason", {})[t_] = "exit_latch"
                            self.cancel_order(eo)
                if st["pos"] == 0:
                    self._finish(ts)
                elif leg == "tp":
                    st["sl_qty"] = abs(st["pos"])
                    emit(ts, "amended", "sl-0", "sl", price=plan.stop.price, qty=st["sl_qty"])

        def _protect(self, ts):
            absq = abs(st["pos"])
            exit_side = OrderSide.SELL if sign > 0 else OrderSide.BUY
            if st["sl_qty"] == 0 and not st["sl_triggered"]:
                emit(ts, "submitted", "sl-0", "sl", price=plan.stop.price, qty=absq)
                emit(ts, "accepted", "sl-0", "sl")
                emit(ts, "working", "sl-0", "sl")
            elif not st["sl_triggered"]:
                emit(ts, "amended", "sl-0", "sl", price=plan.stop.price, qty=absq)
            st["sl_qty"] = absq
            targets = [floor_step(st["entry_qty"] * f, rules.step_size) for f in req.tp_fractions]
            if plan.tps and sum(req.tp_fractions) == 1 and st["entry_qty"] - sum(targets) > 0:
                targets[-1] += st["entry_qty"] - sum(targets)      # S04：余量给末档
            for i, (tp, target) in enumerate(zip(plan.tps, targets)):
                tag = f"tp-{i}"
                o = st["tp_orders"].get(tag)
                if o is None:
                    if target <= 0:
                        continue
                    o = self.order_factory.limit(iid, exit_side, Quantity(target, size_prec), Price(tp.level, price_prec), reduce_only=True, tags=[tag])
                    st["tag_of"][o.client_order_id] = tag
                    st["tp_orders"][tag] = o
                    emit(ts, "submitted", tag, "tp", price=tp.level, qty=target)
                    self.submit_order(o)
                elif o.is_open and o.quantity.as_decimal() != target:
                    self.modify_order(o, quantity=Quantity(target, size_prec))
            # 入场后保护重检（用已到达的 mark）
            if self.module is not None:
                self.module.check_stop(ts, after_entry=True)

        def release_stop(self, ts):
            """mark 触发后释放 reduce-only market（在本 tick 之后的命令排空中成交）。"""
            for tag, o in st["tp_orders"].items():
                if o.is_open:
                    st.setdefault("cancel_reason", {})[tag] = "sl_triggered"
                    self.cancel_order(o)
            for tag, o in st["entry_orders"].items():
                if o.is_open:
                    st.setdefault("cancel_reason", {})[tag] = "exit_latch"
                    self.cancel_order(o)
            st["exit_latch"] = True
            side = OrderSide.SELL if sign > 0 else OrderSide.BUY
            o = self.order_factory.market(iid, side, Quantity(abs(st["pos"]), size_prec), reduce_only=True, tags=["sl-0#exec"])
            st["tag_of"][o.client_order_id] = "sl-0#exec"
            self.submit_order(o)

        def _finish(self, ts):
            if st["closed"]:
                return
            for tag, o in st["tp_orders"].items():
                if o.is_open:
                    st.setdefault("cancel_reason", {})[tag] = "position_closed"
                    self.cancel_order(o)
            if not st["sl_triggered"]:
                emit(ts, "cancelled", "sl-0", "sl", reason="position_closed")
            for tag, o in st["entry_orders"].items():
                if o.is_open:
                    st.setdefault("cancel_reason", {})[tag] = "position_closed"
                    self.cancel_order(o)
            emit(ts, "closed", "bracket-0", "close")
            st["closed"] = True
            st["close_at"] = ts

    # ------------------------------------------------------------ SimulationModule：mark 触发 + funding
    class MarkFundingModule(SimulationModule):
        def __init__(self, shell):
            super().__init__(SimulationModuleConfig(component_id="MARK-FUNDING-MODULE"))
            self.shell = shell
            self.marks = marks
            self.funding_rows = sorted(market.funding, key=lambda f: f.calc_time)
            self.fi = 0
            self.mi = 0
            self.settled_at = {}

        def pre_process(self, data):
            if not isinstance(data, TradeTick) or st["closed"] or st["censor"]:
                return
            ts = _dt(data.ts_event)
            # 推进 mark 游标到 ts
            while self.mi < len(self.marks) and self.marks[self.mi].ts <= ts:
                st["mark"], st["mark_ts"] = self.marks[self.mi].price, self.marks[self.mi].ts
                self.mi += 1
            # funding（q(t−)）
            while self.fi < len(self.funding_rows) and self.funding_rows[self.fi].calc_time <= ts:
                row = self.funding_rows[self.fi]
                self.fi += 1
                if row.calc_time < t_start or row.calc_time in self.settled_at:
                    continue
                self.settled_at[row.calc_time] = True
                mk = [m for m in self.marks if m.ts <= row.calc_time and m.path_step in ("none", "C")]
                if not mk or (row.calc_time - mk[-1].ts).total_seconds() > policy.mark_max_staleness_s:
                    st["censor"], st["cov"]["mark_ok"], st["censor_idx"] = "MARK_STALE", False, len(st["events"])
                    st["censor_at"] = row.calc_time
                    return
                cash = quantize_money(-st["pos"] * mult * mk[-1].price * row.rate)
                emit(row.calc_time, "funding", f"funding-{st['funding_applied']}", "funding", tb="funding", price=mk[-1].price, qty=st["pos"],
                     reason=f"rate={row.rate}", cash_delta=cash)
                st["funding_applied"] += 1
                st["funding"] += cash
                if cash != 0:
                    self.exchange.adjust_account(Money(cash, USDT))
            if not market.funding_schedule_complete and st["pos"] != 0:
                st["censor"], st["cov"]["funding_ok"], st["censor_idx"] = "FUNDING_SCHEDULE_GAP", False, len(st["events"])
                st["censor_at"] = ts
                return
            # mark 新鲜度
            if st["pos"] != 0 and (st["mark_ts"] is None or (ts - st["mark_ts"]).total_seconds() > policy.mark_max_staleness_s):
                st["censor"], st["cov"]["mark_ok"], st["censor_idx"] = "MARK_STALE", False, len(st["events"])
                st["censor_at"] = ts
                return
            self.check_stop(ts)

        def flush(self, until):
            """引擎结束后：仍持仓且 calc_time ≤ until 的未结算行按 q(t−) 入账（S02：funding 定时器独立于成交量）。"""
            while self.fi < len(self.funding_rows) and self.funding_rows[self.fi].calc_time <= until and st["pos"] != 0 and not st["censor"]:
                row = self.funding_rows[self.fi]
                self.fi += 1
                if row.calc_time < t_start or row.calc_time in self.settled_at or row.calc_time < (st["open_at"] or t_start):
                    continue
                self.settled_at[row.calc_time] = True
                mk = [m for m in self.marks if m.ts <= row.calc_time and m.path_step in ("none", "C")]
                if not mk or (row.calc_time - mk[-1].ts).total_seconds() > policy.mark_max_staleness_s:
                    st["censor"], st["cov"]["mark_ok"], st["censor_idx"] = "MARK_STALE", False, len(st["events"])
                    st["censor_at"] = row.calc_time
                    return
                cash = quantize_money(-st["pos"] * mult * mk[-1].price * row.rate)
                emit(row.calc_time, "funding", f"funding-{st['funding_applied']}", "funding", tb="funding", price=mk[-1].price, qty=st["pos"],
                     reason=f"rate={row.rate}", cash_delta=cash)
                st["funding_applied"] += 1
                st["funding"] += cash

        def check_stop(self, ts, after_entry=False):
            if st["pos"] == 0 or st["sl_triggered"] or st["mark"] is None:
                return
            hit = st["mark"] <= plan.stop.price if sign > 0 else st["mark"] >= plan.stop.price
            if not hit:
                return
            st["sl_triggered"] = True
            trig_ts = ts if after_entry else max(st["mark_ts"], t_start)
            bar, step = bar_step(_ns(trig_ts))
            emit(trig_ts if trig_ts <= ts else ts, "stop_triggered", "sl-0", "sl", tb="mark", price=st["mark"], qty=abs(st["pos"]), bar=bar, step=step)
            self.shell.release_stop(ts)

        def process(self, ts_now):
            pass

        def log_diagnostics(self, logger):
            pass

        def reset(self):
            pass

    # ------------------------------------------------------------ 引擎
    def _censor(reason: str, cov_key: str) -> None:
        """S21：按契约 CENSOR_PRIORITY 选主因。此前 bars 分支是**无条件覆盖**，
        规则未知 + bars 不完整时把 RULE_HISTORY_MISSING 覆写成 BAR_GAP（比 A 更严重的违约）。"""
        from quant_lab.market.contract import CENSOR_PRIORITY as _P
        if st["censor"] is None or _P.index(reason) < _P.index(st["censor"]):
            st["censor"] = reason
        st["cov"][cov_key] = False

    if not market.rules_known:
        _censor("RULE_HISTORY_MISSING", "rules_ok")
    elif (rules.effective_from and t_start < rules.effective_from) or (rules.effective_to and t_start >= rules.effective_to):
        _censor("SYMBOL_TIME_INVALID", "rules_ok")
    if not market.bars_complete:
        _censor("BAR_GAP", "bars_ok")

    if st["censor"] is None:
        engine = BacktestEngine(BacktestEngineConfig(trader_id="QL-B-001", logging=LoggingConfig(bypass_logging=True)))
        shell = PlanShell()
        module = MarkFundingModule(shell)
        shell.module = module
        engine.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                         starting_balances=[Money(policy.wallet, USDT)], base_currency=None, default_leverage=policy.leverage,
                         modules=[module], support_gtd_orders=True, use_reduce_only=True, bar_execution=False, trade_execution=True,
                         liquidity_consumption=True, use_market_order_acks=True)
        engine.add_instrument(inst)
        if ticks:
            engine.add_data(ticks)
        engine.add_strategy(shell)
        try:
            engine.run(start=t_start, end=end)
        finally:
            engine.dispose()
        module.flush(end)
        # 删失：模块无法中止引擎撮合，事后截断删失点之后的事件（COVERAGE 差异按解释码记录）
        if st.get("censor_idx") is not None:
            st["events"] = st["events"][: st["censor_idx"]]
            st["closed"], st["close_at"] = False, None
        # S04：max_holding —— 首次开仓 + hold 之后的事件截断，持仓 → 右删失（adapter 事后处理，与 A 的时刻插入等价）
        if plan.expiry.max_holding_s is not None:
            opens = [e["ts"] for e in st["events"] if e["kind"] in ("filled", "partial_fill") and e["leg"] == "entry"]
            if opens:
                hold_end = min(opens) + dt.timedelta(seconds=plan.expiry.max_holding_s)
                kept = [e for e in st["events"] if e["ts"] < hold_end]
                if len(kept) != len(st["events"]):
                    st["events"] = kept
                    st["closed"], st["close_at"] = False, None
                    if st["censor"] is None:
                        st["censor"] = "LABEL_RIGHT_CENSORED"
        # 账本从事件重放（B 自己的归约，不用增量状态）
        ledger = _replay_ledger(st["events"], sign, mult)
        st.update(ledger)
        # 观察终点
        if st["censor"] is None and not st["closed"]:
            if st["pos"] != 0:
                st["censor"] = "LABEL_RIGHT_CENSORED"
            else:
                for tag, o in st["entry_orders"].items():
                    if o.is_open:
                        emit(end, "expired", tag, "entry", tb="expiry", qty=o.leaves_qty.as_decimal(), reason="horizon_end")
                if st["entry_qty"] == 0:
                    emit(end, "closed", "bracket-0", "close", reason="no_fill")
                    st["closed"] = True
        # 暴露：每个 last 点处（事件后）realized + unrealized(最新 mark)
        if st["entry_qty"] > 0:
            evs = sorted(st["events"], key=lambda e: e["ts"])
            cutoff = end - dt.timedelta(microseconds=1)
            if st["close_at"] is not None:
                cutoff = min(cutoff, st["close_at"])
            if st.get("censor_at") is not None:
                cutoff = min(cutoff, st["censor_at"] - dt.timedelta(microseconds=1))
            if plan.expiry.max_holding_s is not None and st["open_at"] is not None:
                cutoff = min(cutoff, st["open_at"] + dt.timedelta(seconds=plan.expiry.max_holding_s) - dt.timedelta(microseconds=1))
            eval_pts = sorted({m.ts for m in marks if t_start <= m.ts <= cutoff} | {p.ts for p in lasts if p.ts <= cutoff})
            for pts in eval_pts:
                p = PricePoint(ts=pts, price=ZERO)
                mk = [m for m in marks if m.ts <= p.ts]
                if not mk:
                    continue
                realized_now, pos_now, entry_cost_now, entry_qty_now = ZERO, ZERO, ZERO, ZERO
                for e in evs:
                    if e["ts"] > p.ts:
                        break
                    if e["kind"] in ("filled", "partial_fill"):
                        if e["leg"] == "entry":
                            pos_now += sign * e["qty"]
                            entry_cost_now += e["price"] * e["qty"]
                            entry_qty_now += e["qty"]
                        else:
                            avg_now = entry_cost_now / entry_qty_now
                            realized_now += (e["price"] - avg_now) * e["qty"] * sign * mult
                            pos_now -= sign * e["qty"]
                if entry_qty_now == 0:
                    continue
                avg_now = entry_cost_now / entry_qty_now
                pnl = realized_now + (mk[-1].price - avg_now) * pos_now * mult
                st["min_p"], st["max_p"] = min(st["min_p"], pnl), max(st["max_p"], pnl)

    # ------------------------------------------------------------ 归约
    order_rank = {"funding": 0, "expired": 1, "stop_triggered": 2, "cancelled": 3, "tp_triggered": 4, "partial_fill": 5, "filled": 5,
                  "submitted": 6, "accepted": 7, "working": 8, "amended": 9, "rejected": 7, "closed": 10}
    # closed 必须排在同刻所有订单事件之后（原生撤单/拒绝事件晚于壳的 closed 记录；I17 closed 当刻兄弟腿须终态）
    evs = sorted(enumerate(st["events"]), key=lambda ie: (ie[1]["ts"], 1 if ie[1]["kind"] == "closed" else 0, ie[0]))
    events = [CanonicalEvent(seq=i, **e) for i, (_, e) in enumerate(evs)]
    b = req.risk_budget
    gross = quantize_money(st["realized"])
    censored = st["censor"] is not None
    net = None if censored else quantize_money(gross - st["fees"] + st["funding"])
    fill_status = "none" if st["entry_qty"] == 0 else ("filled" if st["entry_qty"] >= st["plan_qty"] else "partial")
    rc = request_canonical(req, policy, t_start=t_start, market_manifest_hash=market.manifest_hash)
    return ExecutionResult(
        canonical_events=events, fill_status=fill_status, filled_qty=st["entry_qty"], fees=quantize_money(st["fees"]),
        funding=quantize_money(st["funding"]), slippage=quantize_money(st["slippage"]), gross_pnl=gross, net_pnl=net,
        net_R=None if net is None else quantize_ratio(net / b), censor_reason=st["censor"],
        coverage_mask=CoverageMask(**st["cov"], liquidation_unmodeled=True),
        trace_hash=trace_hash(kernel="B", kernel_version=kernel_build_id(), req_canonical=rc, events=events), kernel="B", kernel_version=kernel_build_id(),
        entry_avg_price=None if st["entry_qty"] == 0 else quantize_ratio(st["entry_cost"] / st["entry_qty"]),
        exit_avg_price=None if st["exit_qty"] == 0 else quantize_ratio(st["exit_value"] / st["exit_qty"]),
        position_open_at=st["open_at"], position_close_at=st["close_at"],
        mae_R=quantize_ratio(min(ZERO, st["min_p"]) / b), mfe_R=quantize_ratio(max(ZERO, st["max_p"]) / b),
        entry_ttl_source=req.entry_ttl_source, fraction_source=req.fraction_source, horizon_source=req.horizon_source,
    )



# ---------------------------------------------------------------------------
# A/B 比较（ADR §10）：逐 episode 差异 + 解释码 + 代码量/耗时
# ---------------------------------------------------------------------------
EXPLANATION = {
    "MATCH": "业务序列与标量精确一致且都过 gold",
    "B_COMMAND_LATENCY": "模块在 pre_process 释放/撤单的命令要等本 tick 撮合后才排空：同刻 resting TP 先于 SL market 成交，或 mark-only 时刻的撤单/ack 推迟到下一 tick（引擎时钟只随数据推进）",
    "SAME_TS_PRIORITY": "同刻事件序列化顺序不同（IOC 撤余量/保护腿建立/多腿 submit-accept 交错），标量一致",
    "GTD_BOUNDARY": "Nautilus 在到期时刻先撮合该 tick 再过期：A 定义 GTD 等号不成交",
    "GAP_PRICE": "Nautilus 限价按限价成交，无跳空改善：A 取 max(限价,last)",
    "B_LIQUIDITY_MODEL": "B 无滑点模型（按 book 价成交）/零容量不可表达",
    "COVERAGE": "删失后引擎仍撮合，adapter 事后截断",
    "UNEXPLAINED": "未知差异，阻断定型",
}
# 人工判定（S10）：每条绑定 (解释码, 冻结的完整差异键集合, 允许不同的标量集合)。
# 判定条件：B 结果非空且过不变量；全部差异键（event[i].field / events length / 标量名）与冻结集合**完全相等**；
# 冻结集合之外的标量必须相等。任何 EXC、缺失结果、集合不等 → UNEXPLAINED；b 缺失 → NOT_RUN。谓词随内核版本冻结。
def load_manual_codes() -> dict:
    """冻结的人工解释（ab_explanations.json）：每例 code + 完整差异键集合 + 允许不同的标量；绑定 KERNEL_VERSION，不匹配即全部失效。"""
    import json
    from pathlib import Path
    p = Path(__file__).with_name("ab_explanations.json")
    if not p.exists():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    if d.get("kernel_b_version") != KERNEL_VERSION:
        return {}
    return {k: (v["code"], set(v["diff_keys"]), set(v["allowed_scalar_diffs"]), v.get("frozen_b_scalars", {})) for k, v in d["episodes"].items()}


MANUAL_CODES = load_manual_codes()
_SCALAR_KEYS = ("net_R", "fees", "funding", "slippage", "filled_qty", "censor_reason", "fill_status", "mae_R", "mfe_R",
                "entry_avg_price", "exit_avg_price", "gross_pnl", "net_pnl", "position_open_at", "position_close_at")


def diff_keys(diff: list[str]) -> set[str]:
    return {d.split(":")[0].strip() for d in diff}


def classify(fid: str, diff: list[str], a, b) -> str:
    """EXC / 缺结果优先；人工码要求差异键集合与冻结集合完全相等，且冻结集合外标量全部相等；否则 UNEXPLAINED。"""
    if b is None or a is None:
        return "NOT_RUN"
    if any(d.startswith("EXC") for d in diff):
        return "UNEXPLAINED"
    if not diff:
        return "MATCH"
    if fid not in MANUAL_CODES or a is None:
        return "UNEXPLAINED"
    code, frozen_keys, allowed_scalars, frozen_vals = MANUAL_CODES[fid]
    keys = diff_keys(diff)
    if keys != frozen_keys:
        return "UNEXPLAINED"
    for k in _SCALAR_KEYS:
        if k in allowed_scalars:
            # 允许与 A 不同，但必须等于冻结时登记的 B 值（S10：−999 之类的意外值不得被洗成已解释）
            if str(getattr(b, k)) != frozen_vals.get(k):
                return "UNEXPLAINED"
            continue
        if getattr(a, k) != getattr(b, k):
            return "UNEXPLAINED"
    return code


def ab_report(fixtures_dir: str = "tests/market/fixtures/episodes", out: str | None = None, reps: int = 5) -> dict:
    import statistics
    import subprocess
    import time
    from pathlib import Path

    from quant_lab.market.contract import check_invariants, diff_result, load_fixtures
    from quant_lab.market.kernel_a import KERNEL_VERSION as KA, simulate_a

    fx = load_fixtures(fixtures_dir)
    rows, codes = [], {}
    ta, tb = [], []
    for f in fx:
        ra = simulate_a(f.request, f.market)
        da = diff_result(f.expected, ra, ignore_reason=False, all_diffs=True)
        try:
            rb = simulate_b(f.request, f.market)
            check_invariants(f.request, rb, multiplier=f.market.rules.multiplier)
            db = diff_result(f.expected, rb, ignore_reason=False, all_diffs=True)
        except Exception as e:  # noqa: BLE001
            rb, db = None, [f"EXC {type(e).__name__}: {e}"]
        code = classify(f.id, db, ra, rb)
        if da:
            code = "A_GOLD_FAIL"          # S11：A 金标失败即报告失败，不被 B 解释码掩盖
        codes[code] = codes.get(code, 0) + 1
        delta = "—"
        if rb is not None:
            def d_(k):
                x, y = getattr(ra, k), getattr(rb, k)
                return "=" if x == y else f"{x}→{y}"
            delta = f"qty {d_('filled_qty')} / fees {d_('fees')} / funding {d_('funding')} / net_R {d_('net_R')} / censor {d_('censor_reason')}"
        rows.append({"id": f.id, "scenario": f.request.path_scenario, "cost": f.request.cost_scenario, "policy": f.request.policy_version,
                     "a_gold": "pass" if not da else "FAIL", "b_gold": "pass" if not db else "FAIL",
                     "a_hash": ra.trace_hash[:12], "b_hash": (rb.trace_hash[:12] if rb else "—"),
                     "first_diff": (("; ".join(d[:70] for d in db)).replace("|", "\\|") if db else "—"), "delta": delta, "code": code,
                     "sev": "—" if code == "MATCH" else ("阻断" if code == "UNEXPLAINED" else ("合同差异" if code in ("GTD_BOUNDARY", "GAP_PRICE", "B_LIQUIDITY_MODEL", "B_COMMAND_LATENCY") else "序列化"))})
    benchmark_ok = bool(rows) and not any(codes.get(code, 0) for code in ("NOT_RUN", "UNEXPLAINED", "A_GOLD_FAIL"))
    if reps < 1:
        raise ValueError("reps must be >= 1")
    if not benchmark_ok:
        ta, tb = [0.0], [0.0]
    for _ in range(reps if benchmark_ok else 0):
        t0 = time.perf_counter(); [simulate_a(f.request, f.market) for f in fx]; ta.append(time.perf_counter() - t0)
        t0 = time.perf_counter(); [simulate_b(f.request, f.market) for f in fx]; tb.append(time.perf_counter() - t0)
    root = Path(__file__).resolve().parents[3]
    wc = lambda p: sum(1 for _ in open(root / p, encoding="utf-8"))  # noqa: E731
    size = {"A kernel_a.py": wc("src/quant_lab/market/kernel_a.py"), "B nautilus_adapter.py": wc("src/quant_lab/market/nautilus_adapter.py"),
            "共享 contract.py": wc("src/quant_lab/market/contract.py"), "A tests": wc("tests/market/test_kernel_a.py"),
            "B tests": wc("tests/market/test_nautilus_adapter.py") if (root / "tests/market/test_nautilus_adapter.py").exists() else 0}
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=root).stdout.strip()
    except Exception:  # noqa: BLE001
        commit = "?"
    n_match = codes.get("MATCH", 0)
    lines = [
        "# report-G2-kernel-AB：候选 A（自研参考实现）vs 候选 B（Nautilus 1.227.0 + SimulationModule）P1 对拍报告",
        "",
        f"> 生成：`{'.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report'}`；commit {commit}；A={KA}，B={KERNEL_VERSION}；"
        f"夹具 {len(fx)} 例（tests/market/fixtures/episodes）。**P1 只报告差异与解释码，不定型**（ADR-G2 §10：定型留到 P2）。",
        "",
        "## 1. 结论先行",
        "",
        f"- 候选 A：{sum(r['a_gold']=='pass' for r in rows)}/{len(fx)} 过独立期望 + 不变量；trace_hash 重放一致。",
        f"- 候选 B：{n_match}/{len(fx)} 与 gold 精确一致（MATCH）；UNEXPLAINED = {codes.get('UNEXPLAINED', 0)}，NOT_RUN = {codes.get('NOT_RUN', 0)}，A_GOLD_FAIL = {codes.get('A_GOLD_FAIL', 0)}"
        f"（{'满足' if benchmark_ok else '不满足'} ADR §10「无法解释差异率必须为 0」）。",
        "- B 的四类合同差异（GTD 等号、跳空限价改善、滑点模型、同刻命令延迟）均为 Nautilus 撮合语义，adapter 不改 vendor；"
        "其中 B_COMMAND_LATENCY 已由 U02 spike 证实：SimulationModule.pre_process 内提交的命令要到本 tick 撮合之后的排空阶段才执行，"
        "同刻 resting TP 会先于 mark 触发的 SL market 成交（E10；E17 首个差异是无滑点模型，其次同样是该竞合），mark-only 时刻的撤单要等下一 tick（E02）。",
        "- 解释码分布：" + ", ".join(f"{k}={v}" for k, v in sorted(codes.items())) + "。",
        "",
        "## 2. 逐 episode 差异（解释码见 §4）",
        "",
        "| episode | scenario | cost / policy | A gold | B gold | A hash | B hash | 首个事件 diff | 数量/费用/funding/净R/删失 差值 | 解释码 | 严重度 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['id']} | {r['scenario']} | {r['cost']} / {r['policy']} | {r['a_gold']} | {r['b_gold']} | `{r['a_hash']}` | `{r['b_hash']}` | {r['first_diff']} | {r['delta']} | {r['code']} | {r['sev']} |")
    lines += [
        "",
        "## 3. 比较表（ADR §10 模板）",
        f"性能测量：{'run' if benchmark_ok else 'not_run（正确性门禁失败，下面 0 为未测占位）'}。",
        "",
        "| 维度 | 候选 A | 候选 B | 依据 |",
        "|---|---|---|---|",
        f"| 一次性代码量（行） | kernel_a.py {size['A kernel_a.py']} + tests {size['A tests']} | nautilus_adapter.py {size['B nautilus_adapter.py']} + tests {size['B tests']}（另依赖 nautilus_trader 139 MB wheel） | wc -l，同一 commit，共享 contract.py {size['共享 contract.py']} 行不计入 |",
        f"| 可验证性 | gold {sum(r['a_gold']=='pass' for r in rows)}/{len(fx)}；结果层不变量 + 事件层（终态后 fill/兄弟腿终态/结算键）+ I10 账户守恒；40 随机计划 | gold {n_match}/{len(fx)}；每例 B 结果在本报告生成时强制 check_invariants（失败即 EXC→UNEXPLAINED）；余额刷新仅验 adapter 自计 funding 与 gold 一致，未读引擎账户余额（not_run） | test_kernel_a.py / test_nautilus_adapter.py / test_review_p1.py |",
        f"| 性能（{len(fx)} 例全量，{reps} 次） | 中位 {statistics.median(ta)*1000:.0f} ms，p95 {sorted(ta)[int(0.95*(reps-1))]*1000:.0f} ms | 中位 {statistics.median(tb)*1000:.0f} ms，p95 {sorted(tb)[int(0.95*(reps-1))]*1000:.0f} ms（每例冷启动一个 BacktestEngine） | 同机同进程顺序执行；B 长跑优化未做（U08） |",
        "| 维护面 | 自研队列/账务/组合单/路径全部自担；无上游 | 上游 1.227.0 钉死；升级需重核 mark 路由/GTD/reduce-only；模块 API 稳定但命令排空时序是隐式契约 | ADR §9 |",
        "| 永续语义 | mark SL：native；funding：native；路径：native | mark SL：adapter（SimulationModule）；funding：adapter（adjust_account，引擎余额核对 not_run）；路径：adapter 合成 TradeTick | §5 |",
        "| 逐 episode 差异 | — | 见 §2 | — |",
        "| 最终决策 | 待 P2 | 待 P2 | 不用主观分数抵消正确性失败 |",
        "",
        "## 4. 解释码定义",
        "",
    ] + [f"- **{k}**：{v}" for k, v in EXPLANATION.items()] + [
        "",
        "## 5. 候选 B 审计记录（ADR §9.3；native / adapter / not_run）",
        "",
        "| 范围 | 执行层 | 观察 | 判定 |",
        "|---|---|---|---|",
        "| 订单状态 | Strategy.on_event（OrderAccepted/Filled/Canceled/Expired/Rejected/Updated） | on_start 阶段 OrderSubmitted 被丢弃（策略未 RUNNING），submitted 由壳记录；market 单需 use_market_order_acks=True 才有 accepted | native + adapter 补 submitted/working/tp_triggered |",
        "| PRICE_FILTER / LOT_SIZE / MIN_NOTIONAL / MARGIN | adapter 提交前校验（E14a/b/c 与 A 相同拒因） | 原生只在 Price/Quantity 构造精度与 min_notional 上拒绝；MARGIN 由原生 RiskEngine 另有一层未触发 | ADAPTER_FILTER |",
        "| reduce-only | 原生 use_reduce_only=True | SL market 在仓位已被 TP 平掉后被原生拒绝「would have increased position」（E17） | native |",
        "| OrderList | 未用；TP 为独立 reduce-only 限价，SL 为模块条件单 | 非原子 OCO | ORDERLIST_NONATOMIC（设计内） |",
        "| GTD / IOC | 原生 support_gtd_orders + TimeInForce | GTD 到期与同刻 tick 撮合顺序：先撮合后过期（E09）；IOC 余量原生取消（E08） | native，GTD_BOUNDARY 差异 |",
        "| 价格精度 | Price/Quantity 精度由 tick/step 推导 | 夹具 tick=1 全过；真实 0.1/0.001 未跑 | not_run（P2） |",
        "| 账户 / funding | exchange.adjust_account(Money(cash, USDT))，pre_process 内以 q(t−) 结算；无后续 tick 的行由 flush 只记 adapter 账本（无 adjust_account） | E05/E06/E07 的 adapter 自计 funding 与 gold 一致；**引擎账户余额未读取核对（not_run）** | adapter |",
        "| mark 路由 | 模块自持 mark 流（不喂引擎，避免 PriceType::Mark panic） | mark-only 时刻只能在下一 last tick 释放（E02） | adapter，B_COMMAND_LATENCY |",
        "| 容量 / 部分成交 | liquidity_consumption=True + TradeTick.size=capacity | E08 IOC 部分成交与 gold 一致；**零容量不可表达（替换为最小步，B 会多成交）；bars 模式未采用 policy.participation（容量无限）** | native 近似，B_LIQUIDITY_MODEL |",
        "| 暴露 mae/mfe | adapter 事后按 mark 点 + last 点重放 | 含 mark-only 时刻（E02 的 −1.2 MAE 已覆盖） | adapter |",
        "",
        "## 6. 待核项进展（ADR §15）",
        "",
        "- U01：本机 Cython SimulatedExchange 直接调用 modules（process_trade_tick → module.pre_process → matching_engine.process_trade_tick），SimulationModule 登记可达；mark-only 调度需自持流，已证实。",
        "- U02：同刻 funding→余额→mark 触发→命令释放→last 撮合：pre_process 内 adjust_account 已调用（返回 void，引擎账户余额未读取核对，not_run）；命令释放落后一个撮合阶段（E10/E17）——**B 在同刻 SL/TP 竞合上不满足合同**，不修改 vendor 无法闭合，按 ADR §9.1 输出 B_COMMAND_LATENCY 并判该情景不通过。",
        "- U04：Money(USDT) 8 位精度与 Decimal(38,12) 合同：夹具金额均在 8 位内；更细金额未测（not_run）。",
        "- U06：公开 liquidity_consumption 可复现确定性容量（E08、E18 部分）；零容量不可表达。",
        "- U08：性能见 §3；账户隔离采用每 request 一个引擎，长跑优化未做。",
        "",
        "## 7. 未决 / 下一步",
        "",
        "- P2 定型前需：真实分区（BTCUSDT 2024-01）上 A/B 各跑 ≥20 个合成 order_plan 的差异分布；B 的 GTD_BOUNDARY / GAP_PRICE 是否可通过公开配置（FillModel、bar_adaptive）收敛；同刻竞合改用 QuoteTick 双价路径是否改变结论。",
        "- 本报告不声称两候选定型；A 为当前参考实现出主数字。",
        "",
    ]
    if out is not None:
        Path(root / out).write_text("\n".join(lines), encoding="utf-8")
    return {"rows": rows, "codes": codes, "unsupported": P2_UNSUPPORTED, "median_ms": {"A": statistics.median(ta) * 1000, "B": statistics.median(tb) * 1000}}


def main(argv=None) -> int:
    import argparse
    import json
    p = argparse.ArgumentParser(prog="quant_lab.market.nautilus_adapter")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("report"); s.add_argument("--fixtures", default="tests/market/fixtures/episodes"); s.add_argument("--out", default=None, help="显式指定才写 Markdown；默认仅输出摘要"); s.add_argument("--reps", type=int, default=5)
    a = p.parse_args(argv)
    r = ab_report(a.fixtures, a.out, a.reps)
    print(json.dumps({"codes": r["codes"], "median_ms": r["median_ms"], "unsupported": r["unsupported"]}, ensure_ascii=False))
    bad = r["codes"].get("UNEXPLAINED", 0) + r["codes"].get("A_GOLD_FAIL", 0) + r["codes"].get("NOT_RUN", 0)
    return 0 if bad == 0 and r["rows"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())


__all__ = ["simulate_b", "KERNEL_VERSION", "KernelBUnavailable", "expand_bar_b", "ab_report", "classify", "MANUAL_CODES", "EXPLANATION"]
