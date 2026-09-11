"""quant_lab.market.kernel_a —— 候选 A：自研永续参考实现 v0.2（ADR-G2 §4–§7、§11；M-07；review-G2-P1 S01–S05/S07/S08/S13 闭合）。

order_plan → 规范执行事件。单 episode 隔离账户、one_way、USDT 线性合约（multiplier 参与全部金额）。
判定顺序（每个时刻，ADR §5.1）：
  P0 规则/覆盖（每时刻查生命周期）→ P1 funding（以 q(t−)，结算 mark = calc_time 前最后**已闭合** mark）→ P2 到期/持仓上限
  → P3 摄入 mark/last、mark 新鲜度（有仓位或即将入场都要求 mark 可用）→ P4 mark 触发 SL → P5 last 触发 TP
  → P6 撮合（SL market → TP（当前 last 必须满足限价）→ entry 按价格/时间优先，post-only 穿价拒绝，钱包重检）
  → P7 入场后保护重检（一次）→ P8 暴露与不变量。
分钟内部启动：bars 只展开 open_time >= t_start 的 bar；启动前最新已闭合 mark 作初始游标（不回填未来值）。
不 import nautilus_adapter；不与候选 B 共享撮合/触发/funding helper。
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

_SRC_FILES = ("kernel_a.py", "contract.py")   # S12：构建身份含共享 contract（归约/不变量）

from quant_lab.market.contract import (
    Bar, CanonicalEvent, CENSOR_PRIORITY, ContractError, CoverageMask, first_grid_point, ExecutionInvariantError, ExecutionPolicy, ExecutionRequest,
    ExecutionResult, MarketView, PricePoint, check_invariants, floor_step, quantize_money, quantize_ratio,
    entry_expiry_at, grid_points_between, request_canonical, resolve_policy, trace_hash,
)

KERNEL_VERSION = "kernel-a-v0.2"
ZERO = Decimal(0)
US = dt.timedelta(microseconds=1)


def kernel_build_id() -> str:
    """内核构建身份 = 版本串 + 本文件源码摘要（S12：代码改动必须改 trace_hash）。"""
    h = hashlib.sha256()
    for name in _SRC_FILES:
        h.update((Path(__file__).parent / name).read_bytes())
    return f"{KERNEL_VERSION}+{h.hexdigest()[:12]}"


@dataclass
class Order:
    id: str
    leg: str                      # entry | sl | tp
    side: str                     # buy | sell
    kind: str                     # limit | market | stop_market
    price: Decimal | None
    qty: Decimal
    seq: int = 0                  # accepted 顺序（价格相同时先到先得）
    tif: str = "GTC"
    post_only: bool = False
    po_checked: bool = False
    filled: Decimal = ZERO
    status: str = "working"       # working | filled | cancelled | expired | rejected
    triggered: bool = False       # sl：mark 已触发；tp：last 曾触及（只作事件，不作可成交条件）
    ioc_pending: bool = False
    tp_index: int = -1
    deadline: dt.datetime | None = None

    @property
    def leaves(self) -> Decimal:
        return self.qty - self.filled

    @property
    def live(self) -> bool:
        return self.status == "working"


@dataclass
class Moment:
    ts: dt.datetime
    marks: list[PricePoint] = field(default_factory=list)
    lasts: list[PricePoint] = field(default_factory=list)
    funding: list = field(default_factory=list)
    expiry: bool = False
    horizon: bool = False
    hold_end: bool = False
    bar_gap: bool = False


class KernelA:
    def __init__(self, req: ExecutionRequest, market: MarketView, policy: ExecutionPolicy | None = None):
        self.req = req
        self.plan = req.order_plan
        self.market = market
        self.policy = policy or resolve_policy(req.policy_version)
        if policy is None and self.policy.content_hash != req.policy_hash:
            raise ContractError("request.policy_hash 与当前政策内容不符，拒绝执行（S12）")
        self.cost = self.policy.cost(req.cost_scenario)
        self.rules = market.rules
        self.mult = self.rules.multiplier
        self.sign = self.plan.sign
        self.t_start = req.resolved_t_start(self.policy)
        if len(req.entry_fractions) != len(self.plan.entries) or len(req.tp_fractions) != len(self.plan.tps):
            raise ContractError("request.entry_fractions/tp_fractions 与 order_plan 长度不符（请求须重新构造而非 model_copy）")
        self.events: list[CanonicalEvent] = []
        self.orders: dict[str, Order] = {}
        self.seq_counter = 0
        self.pos = ZERO
        self.entry_qty = ZERO
        self.entry_cost = ZERO
        self.exit_qty = ZERO
        self.exit_value = ZERO
        self.realized = ZERO          # 已乘 multiplier
        self.fees = ZERO
        self.funding_total = ZERO
        self.slippage = ZERO
        self.cash = self.policy.wallet   # AccountLedger：cash = wallet − fees + funding + realized
        self.mark: Decimal | None = None
        self.mark_ts: dt.datetime | None = None
        self.exit_latch = False
        self.censor: str | None = None
        self.cov = {"mark_ok": True, "funding_ok": True, "rules_ok": True, "bars_ok": True}
        self.min_p = ZERO
        self.max_p = ZERO
        self.open_at: dt.datetime | None = None
        self.close_at: dt.datetime | None = None
        self.closed = False
        self.n_funding = 0
        self.settled_keys: dict[dt.datetime, Decimal] = {}
        self.plan_qty = ZERO
        self.hold_end: dt.datetime | None = None
        self.all_marks: list[PricePoint] = []

    # ------------------------------------------------------------------ 事件
    def emit(self, ts, kind, oid, leg, tb="none", price=None, qty=None, fee=None, reason=None, bar=None, step="none", cash_delta=None):
        self.events.append(CanonicalEvent(seq=len(self.events), ts=ts, kind=kind, order_id=oid, leg=leg, trigger_basis=tb,
                                          price=price, qty=qty, fee=fee, reason=reason, bar_open_time=bar, path_step=step,
                                          cash_delta=cash_delta))

    def censor_now(self, reason: str, cov_key: str | None = None) -> None:
        """S21：同刻多重证据缺失时按契约 CENSOR_PRIORITY 选主因，而非代码检查顺序的先到先得。

        （此前 CENSOR_PRIORITY 是只定义不使用的死常量，主因取决于检查顺序，任何重排都会静默违约。）
        """
        if self.censor is None or CENSOR_PRIORITY.index(reason) < CENSOR_PRIORITY.index(self.censor):
            self.censor = reason
        if cov_key:
            self.cov[cov_key] = False

    # ------------------------------------------------------------------ 价格/数量/金额
    def q_tick(self, px: Decimal, side: str) -> Decimal:
        t = self.rules.tick_size
        if t <= 0:
            return px
        return (px / t).to_integral_value(rounding=ROUND_DOWN if side == "buy" else ROUND_UP) * t

    def on_tick(self, px: Decimal) -> bool:
        return self.rules.tick_size <= 0 or (px / self.rules.tick_size) % 1 == 0

    def on_step(self, q: Decimal) -> bool:
        return self.rules.step_size <= 0 or (q / self.rules.step_size) % 1 == 0

    def fee_for(self, px: Decimal, qty: Decimal, taker: bool) -> Decimal:
        rate = self.cost.taker_fee if taker else self.cost.maker_fee
        return quantize_money(px * qty * self.mult * rate)

    def market_px(self, ref: Decimal, side: str) -> Decimal:
        adj = self.cost.slippage_ticks * self.rules.tick_size + ref * self.cost.slippage_bps / Decimal(10000)
        px = ref + adj if side == "buy" else ref - adj
        return self.q_tick(px, "sell" if side == "buy" else "buy")

    def reserved_margin(self) -> Decimal:
        if self.entry_qty == 0 or self.pos == 0:
            return ZERO
        return abs(self.pos) * (self.entry_cost / self.entry_qty) * self.mult / self.policy.leverage

    def equity(self) -> Decimal:
        """equity = cash + 按最新 mark 的未实现盈亏（S07：额度用 mark 刷新后的 equity，不用成本）。"""
        if self.pos == 0 or self.entry_qty == 0 or self.mark is None:
            return self.cash
        avg = self.entry_cost / self.entry_qty
        return self.cash + (self.mark - avg) * self.pos * self.mult

    def affordable_qty(self, px: Decimal, want: Decimal) -> Decimal:
        """入场可承担量：名义额/杠杆 + taker 费用缓冲 ≤ equity − 已用保证金（不自动加杠杆）。"""
        unit = px * self.mult / self.policy.leverage + px * self.mult * self.cost.taker_fee
        if unit <= 0:
            return want
        avail = self.equity() - self.reserved_margin()
        return max(ZERO, floor_step(min(want, avail / unit), self.rules.step_size))

    # ------------------------------------------------------------------ 时间线
    @staticmethod
    def expand_bar(b: Bar, scenario: str, side: str, participation: Decimal | None, step: Decimal) -> list[PricePoint]:
        o, h, l, c = b.o, b.h, b.l, b.c
        if scenario == "primary":
            order = ["O", "L", "H", "C"] if abs(o - l) < abs(h - o) else ["O", "H", "L", "C"]
        elif scenario == "adverse":
            order = ["O", "L", "H", "C"] if side == "long" else ["O", "H", "L", "C"]
        else:
            order = ["O", "H", "L", "C"] if side == "long" else ["O", "L", "H", "C"]
        px = {"O": o, "H": h, "L": l, "C": c}
        iv = dt.timedelta(seconds=b.interval_s)
        offs = [dt.timedelta(0), iv / 3, iv * 2 / 3, iv - US]
        cap = None if participation is None else floor_step(participation * b.volume / 4, step)
        return [PricePoint(ts=b.open_time + offs[i], price=px[s], capacity=cap, bar_open_time=b.open_time, path_step=s)
                for i, s in enumerate(order)]

    def _expanded(self, bars: list[Bar], participation) -> list[PricePoint]:
        # S03：t_start 落在 bar 内部 → 从下一完整 bar 的 O 开始，不消费该分钟已发生的极值
        return [p for b in bars if b.open_time >= self.t_start
                for p in self.expand_bar(b, self.req.path_scenario, self.plan.side, participation, self.rules.step_size)]

    def timeline(self) -> list[Moment]:
        m = self.market
        end = self.req.horizon_end
        lasts = [p for p in m.last if self.t_start <= p.ts <= end] + [p for p in self._expanded(m.bars_last, self.policy.participation) if p.ts <= end]
        marks = [p for p in m.mark if self.t_start <= p.ts <= end] + [p for p in self._expanded(m.bars_mark, None) if p.ts <= end]
        moments: dict[dt.datetime, Moment] = {}

        def at(ts) -> Moment:
            return moments.setdefault(ts, Moment(ts=ts))

        for p in sorted(marks, key=lambda x: x.ts):
            at(p.ts).marks.append(p)
        for p in sorted(lasts, key=lambda x: x.ts):
            at(p.ts).lasts.append(p)
        for f in m.funding:
            if self.t_start <= f.calc_time <= end:
                at(f.calc_time).funding.append(f)
        deadline = entry_expiry_at(self.t_start, self.req.entry_ttl_s)
        if deadline <= end:
            at(deadline).expiry = True
        # S05：bars 网格首个缺口 → 逐时 BAR_GAP（保留此前事件）；缺口不可定位（bars_complete=False 且无 bars）→ 起点删失
        gaps = [g for g in (self._first_bar_gap(m.bars_last, end), self._first_bar_gap(m.bars_mark, end)) if g is not None]
        if gaps:
            at(min(gaps)).bar_gap = True          # S15：两流取最早缺口
        at(end).horizon = True
        at(self.t_start)
        self.moments = moments
        return [moments[k] for k in sorted(moments)]

    def _first_bar_gap(self, bars: list[Bar], end: dt.datetime) -> dt.datetime | None:
        """bars 网格在 [t_start, end) 内的首个缺口时刻：逐点匹配身份，离网行不计覆盖。"""
        if not bars:
            return None
        interval_s = bars[0].interval_s
        iv = dt.timedelta(seconds=interval_s)
        opens = sorted(b.open_time for b in bars if b.open_time >= self.t_start and b.open_time < end)
        # S43：游标只由精确匹配的网格点推进；离网行不能填补缺口或移动网格。
        expected = first_grid_point(self.t_start, interval_s)
        if not opens:
            return None                    # 保留显式 points / 窗口外 bars 的既有语义
        for opened in opens:
            if opened != expected:
                if expected < end:
                    return expected
                return opened              # 网格已齐但多出离网行，仍不能报 bars_ok
            expected += iv
        if grid_points_between(expected, end, interval_s) > 0:
            return expected
        return None

    def add_hold_end_moment(self) -> None:
        """首次开仓后把 open_at + max_holding 作为时刻插入（S04：观察终点先于同刻 funding）。"""
        if self.hold_end is not None and self.hold_end <= self.req.horizon_end and self.hold_end not in self.moments:
            self.moments[self.hold_end] = Moment(ts=self.hold_end, hold_end=True)
            self.moment_list = [self.moments[k] for k in sorted(self.moments)]
        elif self.hold_end is not None and self.hold_end in self.moments:
            self.moments[self.hold_end].hold_end = True

    # ------------------------------------------------------------------ 入场提交
    def sizing_total(self) -> Decimal:
        s = self.plan.sizing
        if s.mode == "fixed_qty":
            return floor_step(s.qty, self.rules.step_size)
        ref = sum(self.leg_ref_price(e) * f for e, f in zip(self.plan.entries, self.req.entry_fractions))
        dist = abs(ref - self.plan.stop.price) * self.mult
        return floor_step(self.req.risk_budget / dist, self.rules.step_size) if dist > 0 else ZERO

    def leg_ref_price(self, e) -> Decimal:
        return (e.price_lo + e.price_hi) / 2

    def ladder_prices(self, e) -> list[Decimal]:
        n = self.policy.ladder_steps
        side = "buy" if self.sign > 0 else "sell"
        if e.kind != "ladder":
            return [e.price_lo]
        if n <= 1 or e.price_lo == e.price_hi:
            return [self.q_tick((e.price_lo + e.price_hi) / 2, side)]
        stepp = (e.price_hi - e.price_lo) / (n - 1)
        pts = [e.price_hi - stepp * i for i in range(n)] if self.sign > 0 else [e.price_lo + stepp * i for i in range(n)]
        return [self.q_tick(p, side) for p in pts]

    def submit_entries(self, ts: dt.datetime) -> None:
        total = self.sizing_total()
        legs: list[tuple] = []
        for e, frac in zip(self.plan.entries, self.req.entry_fractions):
            prices = [None] if e.kind == "market_ref" else self.ladder_prices(e)
            leg_total = floor_step(total * frac, self.rules.step_size)
            per = floor_step(leg_total / len(prices), self.rules.step_size)
            qtys = [per] * len(prices)
            rem = leg_total - per * len(prices)
            i = 0
            while self.rules.step_size > 0 and rem >= self.rules.step_size:
                qtys[i % len(prices)] += self.rules.step_size
                rem -= self.rules.step_size
                i += 1
            legs += [(e, p, q) for p, q in zip(prices, qtys)]
        self.plan_qty = sum(q for _, _, q in legs)          # 有效计划量 = 分配总量（S04）
        side = "buy" if self.sign > 0 else "sell"
        r = self.rules
        reject = None
        reserve = ZERO
        for e, p, q in legs:
            ref = p if p is not None else self.leg_ref_price(e)
            if p is not None and (not self.on_tick(p) or p < r.min_price or (r.max_price is not None and p > r.max_price)):
                reject = reject or "PRICE_FILTER"
            if q <= 0 or not self.on_step(q) or q < r.min_qty or (r.max_qty is not None and q > r.max_qty):
                reject = reject or "LOT_SIZE"
            if ref * q * self.mult < r.min_notional:
                reject = reject or "MIN_NOTIONAL"
            reserve += ref * q * self.mult / self.policy.leverage + ref * q * self.mult * self.cost.taker_fee   # 含手续费预留
        if reject is None and reserve > self.cash:
            reject = "MARGIN"
        deadline = entry_expiry_at(self.t_start, self.req.entry_ttl_s)
        for i, (e, p, q) in enumerate(legs):
            oid = f"entry-{i}"
            self.emit(ts, "submitted", oid, "entry", price=p, qty=q)
            if reject:
                self.emit(ts, "rejected", oid, "entry", reason=reject)
                self.orders[oid] = Order(oid, "entry", side, "limit" if p is not None else "market", p, q, status="rejected")
                continue
            self.emit(ts, "accepted", oid, "entry")
            self.seq_counter += 1
            o = Order(oid, "entry", side, "limit" if p is not None else "market", p, q, seq=self.seq_counter, tif=e.tif,
                      post_only=e.post_only, ioc_pending=(e.tif == "IOC"), deadline=deadline)
            if p is not None and e.tif != "IOC":
                self.emit(ts, "working", oid, "entry")
            self.orders[oid] = o
        if reject:
            self.emit(ts, "closed", "bracket-0", "close", reason="no_fill")
            self.closed = True

    # ------------------------------------------------------------------ 保护腿
    def tp_targets(self) -> list[Decimal]:
        """按累计入场量分配各档目标量；fractions 和为 1 时舍入余量给末档（S04，ADR §4.2）。"""
        tps = self.plan.tps
        fracs = self.req.tp_fractions
        targets = [floor_step(self.entry_qty * f, self.rules.step_size) for f in fracs]
        if tps and sum(fracs) == 1:
            rem = self.entry_qty - sum(targets)
            if rem > 0:
                targets[-1] += rem
        return targets

    def protect(self, ts: dt.datetime) -> None:
        absq = abs(self.pos)
        exit_side = "sell" if self.sign > 0 else "buy"
        sl = self.orders.get("sl-0")
        if sl is None:
            self.seq_counter += 1
            sl = Order("sl-0", "sl", exit_side, "stop_market", self.plan.stop.price, absq, seq=self.seq_counter)
            self.orders["sl-0"] = sl
            self.emit(ts, "submitted", "sl-0", "sl", price=sl.price, qty=absq)
            self.emit(ts, "accepted", "sl-0", "sl")
            self.emit(ts, "working", "sl-0", "sl")
        elif sl.live and sl.qty != absq + sl.filled:
            sl.qty = absq + sl.filled
            self.emit(ts, "amended", "sl-0", "sl", price=sl.price, qty=sl.qty)
        for i, (tp, target) in enumerate(zip(self.plan.tps, self.tp_targets())):
            oid = f"tp-{i}"
            o = self.orders.get(oid)
            if o is None:
                if target <= 0:
                    continue
                self.seq_counter += 1
                o = Order(oid, "tp", exit_side, "limit", tp.level, target, seq=self.seq_counter, tp_index=i)
                self.orders[oid] = o
                self.emit(ts, "submitted", oid, "tp", price=tp.level, qty=target)
                self.emit(ts, "accepted", oid, "tp")
                self.emit(ts, "working", oid, "tp")
            elif o.live and o.qty != target:
                o.qty = target
                self.emit(ts, "amended", oid, "tp", price=o.price, qty=target)

    def cancel(self, ts, o: Order, reason: str, with_qty: bool = False) -> None:
        if not o.live:
            return
        o.status = "cancelled"
        self.emit(ts, "cancelled", o.id, o.leg, qty=o.leaves if with_qty else None, reason=reason)

    def live_orders(self, leg: str) -> list[Order]:
        out = [o for o in self.orders.values() if o.leg == leg and o.live]
        if leg == "entry":   # 价格优先（买高先/卖低先，market 最先）、accepted_seq 次之（S07）
            def key(o: Order):
                if o.price is None:
                    return (0, ZERO, o.seq)
                return (1, -o.price if o.side == "buy" else o.price, o.seq)
            out.sort(key=key)
        return out

    def set_exit_latch(self, ts) -> None:
        if not self.exit_latch:
            self.exit_latch = True
            for o in self.live_orders("entry"):
                self.cancel(ts, o, "exit_latch")

    # ------------------------------------------------------------------ 账务
    def _fill_event(self, ts, o: Order, leg: str, px: Decimal, q: Decimal, fee: Decimal, bar, step) -> None:
        if not o.live:
            raise ExecutionInvariantError(f"终态订单 {o.id} 不得再成交")
        o.filled += q
        if o.leaves < 0:
            raise ExecutionInvariantError(f"订单 {o.id} 累计成交超过订单量")
        kind = "filled" if o.leaves == 0 else "partial_fill"
        if kind == "filled":
            o.status = "filled"
        self.emit(ts, kind, o.id, leg, tb="last", price=px, qty=q, fee=fee, bar=bar, step=step)
        self.fees += fee
        self.cash -= fee

    def apply_entry_fill(self, ts, o: Order, px: Decimal, q: Decimal, taker: bool, bar, step) -> None:
        self._fill_event(ts, o, "entry", px, q, self.fee_for(px, q, taker), bar, step)
        self.pos += self.sign * q
        self.entry_qty += q
        self.entry_cost += px * q
        if self.open_at is None:
            self.open_at = ts
            if self.plan.expiry.max_holding_s is not None:
                self.hold_end = ts + dt.timedelta(seconds=self.plan.expiry.max_holding_s)
                self.add_hold_end_moment()

    def apply_exit_fill(self, ts, o: Order, px: Decimal, q: Decimal, taker: bool, bar, step) -> None:
        self._fill_event(ts, o, o.leg, px, q, self.fee_for(px, q, taker), bar, step)
        avg = self.entry_cost / self.entry_qty
        gross = (px - avg) * q * self.sign * self.mult      # S08：multiplier 进 PnL
        self.realized += gross
        self.cash += gross
        self.pos -= self.sign * q
        self.exit_qty += q
        self.exit_value += px * q

    def finish_if_flat(self, ts) -> None:
        if self.pos == 0 and self.entry_qty > 0 and not self.closed:
            for o in self.live_orders("tp"):
                self.cancel(ts, o, "position_closed")
            sl = self.orders.get("sl-0")
            if sl and sl.live:
                self.cancel(ts, sl, "position_closed")
            for o in self.live_orders("entry"):
                self.cancel(ts, o, "position_closed")
            if any(o.live for o in self.orders.values()):
                raise ExecutionInvariantError("closed 前仍有存活订单")
            self.emit(ts, "closed", "bracket-0", "close")
            self.closed = True
            self.close_at = ts

    # ------------------------------------------------------------------ 触发
    def stop_hit(self) -> bool:
        if self.mark is None or self.pos == 0:
            return False
        s = self.plan.stop.price
        return self.mark <= s if self.sign > 0 else self.mark >= s

    def trigger_stop(self, ts, bar=None, step="none") -> None:
        sl = self.orders.get("sl-0")
        if sl is None or not sl.live or sl.triggered:
            return
        sl.triggered = True
        self.emit(ts, "stop_triggered", "sl-0", "sl", tb="mark", price=self.mark, qty=abs(self.pos), bar=bar, step=step)
        self.set_exit_latch(ts)
        for o in self.live_orders("tp"):
            self.cancel(ts, o, "sl_triggered")

    def tp_executable(self, o: Order, last: Decimal) -> bool:
        return last >= o.price if self.sign > 0 else last <= o.price

    # ------------------------------------------------------------------ 撮合
    def _exit_sl(self, ts, sl: Order, last: Decimal, take, bar, step) -> None:
        q = take(min(sl.leaves, abs(self.pos)))
        if q > 0:
            px = self.market_px(last, sl.side)
            self.slippage += abs(px - last) * q * self.mult
            self.apply_exit_fill(ts, sl, px, q, True, bar, step)
            self.finish_if_flat(ts)

    def match_point(self, ts, p: PricePoint) -> None:
        cap = p.capacity
        last = p.price
        bar, step = p.bar_open_time, p.path_step

        def take(q: Decimal) -> Decimal:
            nonlocal cap
            q = floor_step(q, self.rules.step_size)          # S13：成交量必须落在 step 网格
            if cap is None:
                return q
            q = floor_step(min(q, cap), self.rules.step_size)
            cap -= q
            return q

        sl = self.orders.get("sl-0")
        if sl and sl.live and sl.triggered and self.pos != 0:
            self._exit_sl(ts, sl, last, take, bar, step)
            if self.closed:
                return
        # TP：当前 last 必须满足限价（S01：历史触及不是永久可成交条件）
        for o in sorted(self.live_orders("tp"), key=lambda x: x.tp_index):
            if self.pos == 0 or not self.tp_executable(o, last):
                continue
            q = take(min(o.leaves, abs(self.pos)))
            if q <= 0:
                continue
            px = max(o.price, last) if self.sign > 0 else min(o.price, last)
            self.apply_exit_fill(ts, o, px, q, False, bar, step)
            self.set_exit_latch(ts)
            if self.pos == 0:
                self.finish_if_flat(ts)
                return
            self.protect(ts)
        if self.exit_latch:
            return
        # entry：价格/时间优先；post-only 穿价拒绝；钱包重检（S07）
        for o in self.live_orders("entry"):
            if o.kind == "limit":
                ok = last <= o.price if o.side == "buy" else last >= o.price
                if o.post_only and not o.po_checked:
                    o.po_checked = True      # 首个撮合机会：穿价即拒，不按 maker 成交
                    if ok:
                        o.status = "rejected"
                        self.emit(ts, "rejected", o.id, "entry", reason="POST_ONLY_CROSS")
                        continue
                if not ok:
                    continue
                px = min(o.price, last) if o.side == "buy" else max(o.price, last)
                taker = False
            else:
                px = self.market_px(last, o.side)
                taker = True
            q = take(min(o.leaves, self.affordable_qty(px, o.leaves)))
            filled_now = False
            if q > 0:
                if taker:
                    self.slippage += abs(px - last) * q * self.mult
                self.apply_entry_fill(ts, o, px, q, taker, bar, step)
                filled_now = True
            if o.live and o.leaves > 0 and self.affordable_qty(px, o.leaves) <= 0:
                self.cancel(ts, o, "MARGIN", with_qty=True)
            if o.ioc_pending:
                o.ioc_pending = False
                if o.live and o.leaves > 0:
                    self.cancel(ts, o, "ioc_remainder", with_qty=True)
            if filled_now:
                self.protect(ts)
                if not self.market.funding_schedule_complete:   # S02：无完整结算证据的持仓不能继续得到标签
                    self.censor_now("FUNDING_SCHEDULE_GAP", "funding_ok")
                    return
        for o in self.live_orders("entry"):
            if o.ioc_pending:
                o.ioc_pending = False
                self.cancel(ts, o, "ioc_remainder", with_qty=True)
        # P7：入场后保护重检（一次）
        if self.pos != 0 and self.stop_hit():
            self.trigger_stop(ts, bar, step)
            sl = self.orders.get("sl-0")
            if sl and sl.live and self.pos != 0:
                self._exit_sl(ts, sl, last, take, bar, step)

    # ------------------------------------------------------------------ funding
    def closed_mark_at(self, ts: dt.datetime) -> PricePoint | None:
        """calc_time 前最后**已闭合**的 mark：显式点按 ts ≤ calc_time；bar 展开点只取 C 点（S02）。"""
        cands = [p for p in self.all_marks if p.ts <= ts and p.path_step in ("none", "C")]
        return cands[-1] if cands else None

    def settle_funding(self, ts, row) -> None:
        if ts in self.settled_keys:
            if self.settled_keys[ts] != row.rate:
                raise ContractError(f"funding 同 calc_time={ts} 冲突费率 {self.settled_keys[ts]} vs {row.rate}：同快照重复键拒收（S02）")
            return
        mp = self.closed_mark_at(ts)
        if mp is None or (ts - mp.ts).total_seconds() > self.policy.mark_max_staleness_s:
            self.censor_now("MARK_STALE", "mark_ok")
            return
        cash = quantize_money(-self.pos * self.mult * mp.price * row.rate)
        self.emit(ts, "funding", f"funding-{self.n_funding}", "funding", tb="funding", price=mp.price, qty=self.pos,
                  reason=f"rate={row.rate}", cash_delta=cash)
        self.n_funding += 1
        self.funding_total += cash
        self.cash += cash
        self.settled_keys[ts] = row.rate      # 成功入账后记键

    # ------------------------------------------------------------------ 主循环
    def run(self) -> ExecutionResult:
        self.all_marks = sorted(list(self.market.mark) + [p for b in self.market.bars_mark for p in
                                self.expand_bar(b, self.req.path_scenario, self.plan.side, None, self.rules.step_size)], key=lambda p: p.ts)
        r = self.rules
        if not self.market.rules_known or r.tick_size <= 0 or r.step_size <= 0:
            self.censor_now("RULE_HISTORY_MISSING", "rules_ok")
        elif (r.effective_from and self.t_start < r.effective_from) or (r.effective_to and self.t_start >= r.effective_to):
            self.censor_now("SYMBOL_TIME_INVALID", "rules_ok")
        if not self.market.bars_quality_ok:
            self.censor_now("BAR_GAP", "bars_ok")     # S15：来源/体检/OHLC 质量失败不是时间洞，起点即删失
        elif not self.market.bars_complete and (self._first_bar_gap(self.market.bars_last, self.req.horizon_end) is None
                                                and self._first_bar_gap(self.market.bars_mark, self.req.horizon_end) is None):
            self.censor_now("BAR_GAP", "bars_ok")     # 缺口不可定位（缺文件）→ 起点删失
        elif not self.market.bars_complete:
            self.cov["bars_ok"] = False
        seed = self.closed_mark_at(self.t_start)          # 启动前最新已闭合 mark 作初始游标
        if seed is not None:
            self.mark, self.mark_ts = seed.price, seed.ts
        if self.censor is None:
            self.moment_list = self.timeline()
            i = 0
            while i < len(self.moment_list):
                mo = self.moment_list[i]
                i += 1
                ts = mo.ts
                if r.effective_to and ts >= r.effective_to:          # P0 每时刻生命周期（S05）
                    self.censor_now("SYMBOL_TIME_INVALID", "rules_ok")
                    break
                if mo.bar_gap:                                        # S05：逐时 bars 覆盖
                    self.censor_now("BAR_GAP", "bars_ok")
                    break
                if (mo.hold_end or (self.hold_end is not None and ts >= self.hold_end)) and self.pos != 0:
                    self.censor_now("LABEL_RIGHT_CENSORED")           # S04：持仓上限先于同刻 funding/撮合
                    break
                if mo.horizon and self.pos != 0:
                    self.censor_now("LABEL_RIGHT_CENSORED")           # 观察终点先于同刻 funding
                    break
                if ts == self.t_start and not self.orders:
                    self.submit_entries(ts)
                if self.closed:
                    break
                for row in mo.funding:
                    self.settle_funding(ts, row)
                    if self.censor:
                        break
                if self.censor:
                    break
                if mo.expiry:
                    for o in self.live_orders("entry"):
                        if o.deadline is not None and o.deadline <= ts:
                            o.status = "expired"
                            self.emit(ts, "expired", o.id, "entry", tb="expiry", qty=o.leaves, reason="entry_ttl")
                    if self.pos == 0 and self.entry_qty == 0 and not self.live_orders("entry"):
                        self.emit(ts, "closed", "bracket-0", "close", reason="no_fill")
                        self.closed = True
                        break
                if mo.horizon:
                    if self.pos != 0:
                        self.censor_now("LABEL_RIGHT_CENSORED")
                    else:
                        for o in self.live_orders("entry"):
                            o.status = "expired"
                            self.emit(ts, "expired", o.id, "entry", tb="expiry", qty=o.leaves, reason="horizon_end")
                        if not self.closed and self.entry_qty == 0:
                            self.emit(ts, "closed", "bracket-0", "close", reason="no_fill")
                            self.closed = True
                    break
                for p in mo.marks:
                    self.mark, self.mark_ts = p.price, p.ts
                if mo.lasts and (self.pos != 0 or self.live_orders("entry")):   # S05：入场前也要 mark 可用
                    if self.mark_ts is None or (ts - self.mark_ts).total_seconds() > self.policy.mark_max_staleness_s:
                        self.censor_now("MARK_STALE", "mark_ok")
                        break
                if mo.marks and self.stop_hit():
                    p0 = mo.marks[-1]
                    self.trigger_stop(ts, p0.bar_open_time, p0.path_step)
                for p in mo.lasts:
                    if self.pos != 0:
                        for o in sorted(self.live_orders("tp"), key=lambda x: x.tp_index):
                            if not o.triggered and self.tp_executable(o, p.price):
                                o.triggered = True
                                self.emit(ts, "tp_triggered", o.id, "tp", tb="last", price=p.price, qty=o.leaves, bar=p.bar_open_time, step=p.path_step)
                    self.match_point(ts, p)
                    if self.moment_list[i - 1] is not mo or len(self.moment_list) != len(self.moments):
                        pass
                    if self.closed or self.censor:
                        break
                if self.hold_end is not None and self.pos != 0:
                    # 同刻开仓且 hold_end 刚插入：重新定位游标到 hold_end 之前
                    self.moment_list = [self.moments[k] for k in sorted(self.moments)]
                    i = next(k for k, m in enumerate(self.moment_list) if m.ts > ts)
                if self.entry_qty > 0 and self.mark is not None:
                    avg = self.entry_cost / self.entry_qty
                    pnl = self.realized + (self.mark - avg) * self.pos * self.mult
                    self.min_p, self.max_p = min(self.min_p, pnl), max(self.max_p, pnl)
                if self.closed or self.censor:
                    break
        return self.result()

    def result(self) -> ExecutionResult:
        b = self.req.risk_budget
        gross = quantize_money(self.realized)
        censored = self.censor is not None
        net = None if censored else quantize_money(gross - self.fees + self.funding_total)
        fill_status = "none" if self.entry_qty == 0 else ("filled" if self.entry_qty >= self.plan_qty else "partial")
        rc = request_canonical(self.req, self.policy, t_start=self.t_start, market_manifest_hash=self.market.manifest_hash)
        kv = kernel_build_id()
        res = ExecutionResult(
            canonical_events=self.events, fill_status=fill_status, filled_qty=self.entry_qty,
            fees=quantize_money(self.fees), funding=quantize_money(self.funding_total), slippage=quantize_money(self.slippage),
            gross_pnl=gross, net_pnl=net, net_R=None if net is None else quantize_ratio(net / b),
            censor_reason=self.censor, coverage_mask=CoverageMask(**self.cov, liquidation_unmodeled=True),
            trace_hash=trace_hash(kernel="A", kernel_version=kv, req_canonical=rc, events=self.events),
            kernel="A", kernel_version=kv,
            entry_avg_price=None if self.entry_qty == 0 else quantize_ratio(self.entry_cost / self.entry_qty),
            exit_avg_price=None if self.exit_qty == 0 else quantize_ratio(self.exit_value / self.exit_qty),
            position_open_at=self.open_at, position_close_at=self.close_at,
            mae_R=quantize_ratio(min(ZERO, self.min_p) / b), mfe_R=quantize_ratio(max(ZERO, self.max_p) / b),
            entry_ttl_source=self.req.entry_ttl_source, fraction_source=self.req.fraction_source, horizon_source=self.req.horizon_source,
        )
        check_invariants(self.req, res, multiplier=self.mult)
        if quantize_money(self.cash) != quantize_money(self.policy.wallet + self.realized - self.fees + self.funding_total):
            raise ExecutionInvariantError("I10 账户守恒失败：cash != wallet + realized − fees + funding")
        return res


def simulate_a(req: ExecutionRequest, market: MarketView, policy: ExecutionPolicy | None = None) -> ExecutionResult:
    return KernelA(req, market, policy).run()


__all__ = ["KernelA", "simulate_a", "KERNEL_VERSION", "kernel_build_id"]
