"""手工最小 episode 期望夹具生成器（ADR-G2 §8 E01–E18 规格；M-06）。

独立期望：每个夹具的事件表与标量由 derivation 里的手算式给出，本脚本只消除 submitted/accepted/working 三连等样板，
**不 import kernel_a / nautilus_adapter / execution**，不读取任何引擎输出。
运行：.venv-g2/bin/python tests/market/fixtures/build_episodes.py  →  写 tests/market/fixtures/episodes/E*.json
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from quant_lab.market.contract import resolve_policy   # 只取政策内容哈希（schema 层），不触及任何内核

OUT = Path(__file__).parent / "episodes"
T = dt.datetime(2024, 1, 1, 1, 0, tzinfo=dt.UTC)          # 非 funding 用例：01:00，窗口内无结算点
TF = dt.datetime(2024, 1, 1, 7, 59, tzinfo=dt.UTC)         # funding 用例：07:59，08:00 结算
INST = "BTCUSDT-PERP.BINANCE-UM"
ALL_OK = {"mark_ok": True, "funding_ok": True, "rules_ok": True, "bars_ok": True, "liquidation_unmodeled": True}


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def at(base: dt.datetime, s: float = 0) -> str:
    return iso(base + dt.timedelta(seconds=s))


class Events:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, ts, kind, order_id, leg, tb="none", price=None, qty=None, fee=None, reason=None, bar=None, step="none", cash_delta=None):
        self.items.append({"seq": len(self.items), "ts": ts, "kind": kind, "order_id": order_id, "leg": leg, "trigger_basis": tb,
                           "price": None if price is None else str(price), "qty": None if qty is None else str(qty),
                           "fee": None if fee is None else str(fee), "reason": reason, "bar_open_time": bar, "path_step": step,
                           "cash_delta": None if cash_delta is None else str(cash_delta)})
        return self

    def submit(self, ts, oid, leg, price, qty, working=True):
        self.add(ts, "submitted", oid, leg, price=price, qty=qty).add(ts, "accepted", oid, leg)
        if working:
            self.add(ts, "working", oid, leg)
        return self

    def protect(self, ts, sl_price, sl_qty, tps: list[tuple]):
        """首次 entry fill 后：sl-0 与各 tp-i 的三连。tps=[(price, qty), ...]，qty=0 的档不提交。"""
        self.submit(ts, "sl-0", "sl", sl_price, sl_qty)
        for i, (p, q) in enumerate(tps):
            if q and str(q) != "0":
                self.submit(ts, f"tp-{i}", "tp", p, q)
        return self

    def fill(self, ts, oid, leg, price, qty, fee="0", kind="filled", bar=None, step="none"):
        return self.add(ts, kind, oid, leg, tb="last", price=price, qty=qty, fee=fee, bar=bar, step=step)

    def closed(self, ts, reason=None):
        return self.add(ts, "closed", "bracket-0", "close", reason=reason)


def plan(side="long", entries=None, stop="95", tps=(("105", "1"),), sizing=("risk_budget", None), ttl=3600, max_holding=None):
    return {
        "instrument_id": INST, "side": side,
        "entries": entries or [{"kind": "limit", "price_lo": "100", "price_hi": "100", "fraction": "1", "tif": "GTC", "post_only": False}],
        "stop": {"price": stop, "trigger": "mark"},
        "tps": [{"level": l, "fraction": f} for l, f in tps],
        "sizing": {"mode": sizing[0], "qty": sizing[1]},
        "expiry": {"entry_ttl_s": ttl, "max_holding_s": max_holding}, "reduce_only_exit": True,
    }


def request(eid, t_dec, horizon_s=3600, *, plan_=None, budget="5", policy="fixture-zero-v1", path="primary", cost="base"):
    return {
        "episode_id": eid, "graph_version": "gv-fixture", "decision_snapshot_hash": f"dsh-{eid}", "t_dec": iso(t_dec),
        "order_plan": plan_ or plan(), "policy_version": policy, "policy_hash": resolve_policy(policy).content_hash, "risk_budget": budget, "cost_scenario": cost, "path_scenario": path,
        "market_manifest": f"fixture-{eid}", "execution_contract_version": "g2-exec-v0", "seed": 0, "t_start": None,
        "horizon_end": at(t_dec, horizon_s), "position_mode": "one_way", "horizon_source": "caller",
    }


def pt(ts, price, cap=None):
    return {"ts": ts, "price": str(price), "capacity": None if cap is None else str(cap), "bar_open_time": None, "path_step": "none"}


def market(eid, last=(), mark=(), funding=(), bars_last=(), bars_mark=(), rules=None, schedule_complete=True):
    return {"manifest_id": f"fixture-{eid}", "last": list(last), "mark": list(mark), "bars_last": list(bars_last), "bars_mark": list(bars_mark),
            "funding": [{"calc_time": c, "rate": r, "interval_hours": h} for c, r, h in funding],
            "funding_schedule_complete": schedule_complete,
            "rules": rules or {"tick_size": "1", "step_size": "1", "min_notional": "0", "multiplier": "1"}, "rules_known": True, "bars_complete": True}


def expected(ev: Events, *, fill_status, filled_qty, fees="0", funding="0", slippage="0", gross=None, net=None, R=None, censor=None,
             coverage=None, entry_avg=None, exit_avg=None, open_at=None, close_at=None, mae="0", mfe="0"):
    return {"canonical_events": ev.items, "fill_status": fill_status, "filled_qty": str(filled_qty), "fees": fees, "funding": funding,
            "slippage": slippage, "gross_pnl": gross, "net_pnl": net, "net_R": R, "censor_reason": censor, "coverage_mask": coverage or ALL_OK,
            "entry_avg_price": entry_avg, "exit_avg_price": exit_avg, "position_open_at": open_at, "position_close_at": close_at,
            "mae_R": mae, "mfe_R": mfe}


def fixture(eid, title, derivation, req, mkt, exp, kernels=("A", "B")):
    return {"id": eid, "title": title, "derivation": derivation, "request": req, "market": mkt, "expected": exp, "kernels": list(kernels)}


def entry_and_protect(ev: Events, ts, price="100", qty="1", sl="95", tps=(("105", "1"),), oid="entry-0", bar=None, step="none"):
    ev.fill(ts, oid, "entry", price, qty, bar=bar, step=step)
    ev.protect(ts, sl, qty, list(tps))
    return ev


F: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# E01 跳空 SL
# ---------------------------------------------------------------------------
ev = Events().submit(at(T), "entry-0", "entry", "100", "1")
entry_and_protect(ev, at(T))
ev.add(at(T, 60), "stop_triggered", "sl-0", "sl", tb="mark", price="94", qty="1")
ev.add(at(T, 60), "cancelled", "tp-0", "tp", reason="sl_triggered")
ev.fill(at(T, 60), "sl-0", "sl", "90", "1").closed(at(T, 60))
F["E01"] = fixture("E01", "跳空 SL：mark 94 触发，last 90 成交",
    "qty=floor(5/|100-95|)=1。T 入场 100。T+60 mark=94≤95 触发 SL（trigger_basis=mark），同 ts 取消 TP；last=90 成交（跳空，reduce-only market）。"
    "gross=(90-100)×1=-10；fees=funding=0；net=-10；R=-10/5=-2。mae：T+60 P8 时 realized=-10 → -2；mfe=0。",
    request("E01", T), market("E01", last=[pt(at(T), 100), pt(at(T, 60), 90)], mark=[pt(at(T), 100), pt(at(T, 60), 94)]),
    expected(ev, fill_status="filled", filled_qty=1, gross="-10", net="-10", R="-2", entry_avg="100", exit_avg="90", open_at=at(T), close_at=at(T, 60), mae="-2"))

# E02 双流异步
ev = Events().submit(at(T), "entry-0", "entry", "100", "1")
entry_and_protect(ev, at(T))
ev.add(at(T, 20), "stop_triggered", "sl-0", "sl", tb="mark", price="94", qty="1")
ev.add(at(T, 20), "cancelled", "tp-0", "tp", reason="sl_triggered")
ev.fill(at(T, 21), "sl-0", "sl", "98", "1").closed(at(T, 21))
F["E02"] = fixture("E02", "双流异步：mark 先触发，last 后到成交",
    "T 入场 100。T+20 只有 mark=94 → 触发 SL，无 last 不能成交（禁止回用 T 的 last=100）。T+21 last=98 → 成交 98。gross=-2，R=-0.4。"
    "mae：T+20 unrealized=(94-100)×1=-6 → -1.2；T+21 realized -2；min=-6 → mae_R=-1.2。",
    request("E02", T), market("E02", last=[pt(at(T), 100), pt(at(T, 21), 98)], mark=[pt(at(T), 100), pt(at(T, 20), 94)]),
    expected(ev, fill_status="filled", filled_qty=1, gross="-2", net="-2", R="-0.4", entry_avg="100", exit_avg="98", open_at=at(T), close_at=at(T, 21), mae="-1.2"))

# E03 last 跌但 mark 未触
ev = Events().submit(at(T), "entry-0", "entry", "100", "1")
entry_and_protect(ev, at(T))
ev.add(at(T, 60), "tp_triggered", "tp-0", "tp", tb="last", price="105", qty="1")
ev.fill(at(T, 60), "tp-0", "tp", "105", "1")
ev.add(at(T, 60), "cancelled", "sl-0", "sl", reason="position_closed").closed(at(T, 60))
F["E03"] = fixture("E03", "last 跌破 95 但 mark 未触：无 SL，TP 105 平仓",
    "T 入场。T+30 last=94（≤95）但 mark=100 → SL 只看 mark，不触发；TP 未触及。T+60 last=105 → tp_triggered，成交 105，取消 SL。gross=5，R=1。"
    "mae：T+30 P=(mark100-100)=0（用 mark 不用 last）→ 0；mfe：T+60 realized 5 → 1。",
    request("E03", T), market("E03", last=[pt(at(T), 100), pt(at(T, 30), 94), pt(at(T, 60), 105)], mark=[pt(at(T), 100), pt(at(T, 30), 100), pt(at(T, 60), 100)]),
    expected(ev, fill_status="filled", filled_qty=1, gross="5", net="5", R="1", entry_avg="100", exit_avg="105", open_at=at(T), close_at=at(T, 60), mfe="1"))

# ---------------------------------------------------------------------------
# E04 同 bar 双触（bars 模式）
# ---------------------------------------------------------------------------
def bar(open_time, o, h, l, c):
    return {"open_time": open_time, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "volume": "0", "interval_s": 60}


BARS = [bar(at(T), 100, 100, 100, 100), bar(at(T, 60), 100, 110, 90, 100)]
B1, B2 = at(T), at(T, 60)
# 四点合成时间：open_time + 0 / 20 / 40 / 60s-1us
PO, P1, P2, P3 = 0, 20, 40, 59.999999

ev = Events().submit(at(T), "entry-0", "entry", "100", "1")
entry_and_protect(ev, at(T), bar=B1, step="O")
ev.add(at(T, 60 + P1), "tp_triggered", "tp-0", "tp", tb="last", price="110", qty="1", bar=B2, step="H")
ev.fill(at(T, 60 + P1), "tp-0", "tp", "110", "1", bar=B2, step="H")
ev.add(at(T, 60 + P1), "cancelled", "sl-0", "sl", reason="position_closed").closed(at(T, 60 + P1))
F["E04a"] = fixture("E04a", "同 bar 双触 long primary：|O-L|=|H-O| 等距 → H 先，TP 按价点 110 成交",
    "bar1 全 100：入场在 O 点（T）。bar2 O100/H110/L90/C100：primary 等距取 O→H→L→C，H 点（T+80）last=110≥105 → tp_triggered，成交价 max(105,110)=110（跳空改善，不是插值 105）。"
    "gross=10，R=2；mfe=2。",
    request("E04a", T, path="primary"), market("E04a", bars_last=BARS, bars_mark=BARS),
    expected(ev, fill_status="filled", filled_qty=1, gross="10", net="10", R="2", entry_avg="100", exit_avg="110", open_at=at(T), close_at=at(T, 60 + P1), mfe="2"))

ev = Events().submit(at(T), "entry-0", "entry", "100", "1")
entry_and_protect(ev, at(T), bar=B1, step="O")
ev.add(at(T, 60 + P1), "stop_triggered", "sl-0", "sl", tb="mark", price="90", qty="1", bar=B2, step="L")
ev.add(at(T, 60 + P1), "cancelled", "tp-0", "tp", reason="sl_triggered")
ev.fill(at(T, 60 + P1), "sl-0", "sl", "90", "1", bar=B2, step="L").closed(at(T, 60 + P1))
F["E04b"] = fixture("E04b", "同 bar 双触 long adverse：O→L→H→C，L 点 mark 90 触发 SL 成交 90",
    "同 E04a 行情，adverse(long)=O→L→H→C。L 点（T+80）mark=90≤95 触发，last=90 成交。gross=-10，R=-2；mae=-2。",
    request("E04b", T, path="adverse"), market("E04b", bars_last=BARS, bars_mark=BARS),
    expected(ev, fill_status="filled", filled_qty=1, gross="-10", net="-10", R="-2", entry_avg="100", exit_avg="90", open_at=at(T), close_at=at(T, 60 + P1), mae="-2"))

SHORT = plan(side="short", stop="105", tps=(("95", "1"),))
ev = Events().submit(at(T), "entry-0", "entry", "100", "1")
ev.fill(at(T), "entry-0", "entry", "100", "1", bar=B1, step="O")
ev.protect(at(T), "105", "1", [("95", "1")])
ev.add(at(T, 60 + P1), "stop_triggered", "sl-0", "sl", tb="mark", price="110", qty="1", bar=B2, step="H")
ev.add(at(T, 60 + P1), "cancelled", "tp-0", "tp", reason="sl_triggered")
ev.fill(at(T, 60 + P1), "sl-0", "sl", "110", "1", bar=B2, step="H").closed(at(T, 60 + P1))
F["E04c"] = fixture("E04c", "同 bar 双触 short adverse：O→H→L→C，H 点 mark 110 触发 SL",
    "short 镜像：SL=105，TP=95。adverse(short)=O→H→L→C，H 点 mark=110≥105 触发，last=110 成交。gross=(100-110)×1=-10，R=-2；mae=-2。",
    request("E04c", T, path="adverse", plan_=SHORT), market("E04c", bars_last=BARS, bars_mark=BARS),
    expected(ev, fill_status="filled", filled_qty=1, gross="-10", net="-10", R="-2", entry_avg="100", exit_avg="110", open_at=at(T), close_at=at(T, 60 + P1), mae="-2"))

# ---------------------------------------------------------------------------
# E05–E07 funding
# ---------------------------------------------------------------------------
S8 = at(TF, 60)          # 08:00
ev = Events().submit(at(TF), "entry-0", "entry", "100", "1")
entry_and_protect(ev, at(TF))
ev.add(S8, "funding", "funding-0", "funding", tb="funding", price="100", qty="1", reason="rate=0.001", cash_delta="-0.1")
ev.add(S8, "tp_triggered", "tp-0", "tp", tb="last", price="105", qty="1")
ev.fill(S8, "tp-0", "tp", "105", "1")
ev.add(S8, "cancelled", "sl-0", "sl", reason="position_closed").closed(S8)
F["E05"] = fixture("E05", "funding 与平仓同刻：先以 q(t−)=1 结算，再 TP 平仓",
    "07:59 入场 100。08:00 结算：settlement_mark=最后闭合 mark=100（07:59），rate=0.001 → cash_delta=-1×100×0.001=-0.1（long 支付）。同刻 last=105 平仓。"
    "gross=5，funding=-0.1，net=4.9，R=0.98；mfe=1（gross 不含 funding）。",
    request("E05", TF), market("E05", last=[pt(at(TF), 100), pt(S8, 105)], mark=[pt(at(TF), 100)], funding=[(S8, "0.001", 8)]),
    expected(ev, fill_status="filled", filled_qty=1, funding="-0.1", gross="5", net="4.9", R="0.98", entry_avg="100", exit_avg="105", open_at=at(TF), close_at=S8, mfe="1"))

ev = Events().submit(at(TF), "entry-0", "entry", "100", "1")
ev.add(S8, "funding", "funding-0", "funding", tb="funding", price="100", qty="0", reason="rate=0.001", cash_delta="0")
entry_and_protect(ev, S8)
ev.add(at(TF, 120), "tp_triggered", "tp-0", "tp", tb="last", price="105", qty="1")
ev.fill(at(TF, 120), "tp-0", "tp", "105", "1")
ev.add(at(TF, 120), "cancelled", "sl-0", "sl", reason="position_closed").closed(at(TF, 120))
F["E06"] = fixture("E06", "funding 与开仓同刻：结算前仓位 0，本次费为 0",
    "07:59 挂单，08:00 结算时 q(t−)=0 → cash_delta=0（事件保留，qty=0）；同刻 last=100 入场。08:01 TP 105。gross=net=5，R=1；不得按开仓后 qty 收 0.1。",
    request("E06", TF), market("E06", last=[pt(S8, 100), pt(at(TF, 120), 105)], mark=[pt(at(TF), 100), pt(S8, 100), pt(at(TF, 120), 100)], funding=[(S8, "0.001", 8)]),
    expected(ev, fill_status="filled", filled_qty=1, gross="5", net="5", R="1", entry_avg="100", exit_avg="105", open_at=S8, close_at=at(TF, 120), mfe="1"))

S12 = at(TF, 60 + 4 * 3600)     # 12:00
S12b = at(TF, 120 + 4 * 3600)   # 12:01
SHORT99 = plan(side="short", stop="105", tps=(("99", "1"),), sizing=("fixed_qty", "1"))
ev = Events().submit(at(TF), "entry-0", "entry", "100", "1")
ev.fill(at(TF), "entry-0", "entry", "100", "1")
ev.protect(at(TF), "105", "1", [("99", "1")])
ev.add(S8, "funding", "funding-0", "funding", tb="funding", price="100", qty="-1", reason="rate=0.001", cash_delta="0.1")
ev.add(S12, "funding", "funding-1", "funding", tb="funding", price="100", qty="-1", reason="rate=-0.002", cash_delta="-0.2")
ev.add(S12b, "tp_triggered", "tp-0", "tp", tb="last", price="99", qty="1")
ev.fill(S12b, "tp-0", "tp", "99", "1")
ev.add(S12b, "cancelled", "sl-0", "sl", reason="position_closed").closed(S12b)
F["E07"] = fixture("E07", "可变周期 + 空仓收付：08:00(8h) 收 0.1，12:00(4h) 付 0.2",
    "short 1@100。08:00 rate=0.001：cash=-(-1)×100×0.001=+0.1；12:00 rate=-0.002（周期切 4h，按结算表不按固定 8h）：cash=-(-1)×100×(-0.002)=-0.2。"
    "12:01 last=99 TP 平仓：gross=(100-99)×1=1；funding=-0.1；net=0.9；R=0.18（budget 5）；mfe=0.2。第二次结算不能漏、不能乘 4/8。",
    request("E07", TF, horizon_s=6 * 3600, plan_=SHORT99),
    market("E07", last=[pt(at(TF), 100), pt(S12b, 99)], mark=[pt(at(TF), 100), pt(S8, 100), pt(S12, 100), pt(S12b, 100)],
           funding=[(S8, "0.001", 8), (S12, "-0.002", 4)]),
    expected(ev, fill_status="filled", filled_qty=1, funding="-0.1", gross="1", net="0.9", R="0.18", entry_avg="100", exit_avg="99", open_at=at(TF), close_at=S12b, mfe="0.2"))

# ---------------------------------------------------------------------------
# E08 部分成交 IOC
# ---------------------------------------------------------------------------
IOC3 = plan(entries=[{"kind": "limit", "price_lo": "100", "price_hi": "100", "fraction": "1", "tif": "IOC", "post_only": False}], sizing=("fixed_qty", "3"))
ev = Events().submit(at(T), "entry-0", "entry", "100", "3", working=False)
ev.fill(at(T), "entry-0", "entry", "100", "1", kind="partial_fill")
ev.add(at(T), "cancelled", "entry-0", "entry", qty="2", reason="ioc_remainder")
ev.protect(at(T), "95", "1", [("105", "1")])
ev.add(at(T, 60), "tp_triggered", "tp-0", "tp", tb="last", price="105", qty="1")
ev.fill(at(T, 60), "tp-0", "tp", "105", "1")
ev.add(at(T, 60), "cancelled", "sl-0", "sl", reason="position_closed").closed(at(T, 60))
F["E08"] = fixture("E08", "部分成交 IOC：容量 1 成交 1，余量 2 立即撤销",
    "IOC qty=3，T 点 last=100 容量 1 → partial_fill 1，余 2 cancelled（同点不可再消耗容量）。保护腿 qty=1。T+60 TP 105 平 1。"
    "filled_qty=1，fill_status=partial，gross=5，R=5/10=0.5；mfe=0.5。",
    request("E08", T, plan_=IOC3, budget="10"), market("E08", last=[pt(at(T), 100, cap=1), pt(at(T, 60), 105, cap=10)], mark=[pt(at(T), 100), pt(at(T, 60), 100)]),
    expected(ev, fill_status="partial", filled_qty=1, gross="5", net="5", R="0.5", entry_avg="100", exit_avg="105", open_at=at(T), close_at=at(T, 60), mfe="0.5"))

# E09 未成交到期（GTD 等号不成交）
GTD90 = plan(entries=[{"kind": "limit", "price_lo": "90", "price_hi": "90", "fraction": "1", "tif": "GTD", "post_only": False}], stop="85", tps=(("95", "1"),), ttl=60)
ev = Events().submit(at(T), "entry-0", "entry", "90", "1")
ev.add(at(T, 60), "expired", "entry-0", "entry", tb="expiry", qty="1", reason="entry_ttl").closed(at(T, 60), reason="no_fill")
F["E09"] = fixture("E09", "未成交到期：GTD 恰在 T+60 到期，同刻 last=90 不成交",
    "buy limit 90，last=100 不成交。entry_ttl=60 → T+60 到期先于同刻撮合（GTD 等号不成交），expired 且无 fill。fill_status=none，filled_qty=0，net=R=0，entry_avg=null。",
    request("E09", T, plan_=GTD90), market("E09", last=[pt(at(T), 100), pt(at(T, 60), 90)], mark=[pt(at(T), 100), pt(at(T, 60), 100)]),
    expected(ev, fill_status="none", filled_qty=0, gross="0", net="0", R="0"))

# E10 reduce-only 竞合：mark 触 SL 与 last 触 TP 同刻 → SL 优先
ev = Events().submit(at(T), "entry-0", "entry", "100", "1")
entry_and_protect(ev, at(T))
ev.add(at(T, 60), "stop_triggered", "sl-0", "sl", tb="mark", price="94", qty="1")
ev.add(at(T, 60), "cancelled", "tp-0", "tp", reason="sl_triggered")
ev.fill(at(T, 60), "sl-0", "sl", "105", "1").closed(at(T, 60))
F["E10"] = fixture("E10", "reduce-only 竞合：同刻 mark 94 / last 105，SL 优先只卖 1",
    "P4 mark SL 先于 P5 last TP：stop_triggered@94，TP 取消；SL market 在同点 last=105 成交 1。净仓 0，无反向仓。gross=5，R=1；mfe=1。",
    request("E10", T), market("E10", last=[pt(at(T), 100), pt(at(T, 60), 105)], mark=[pt(at(T), 100), pt(at(T, 60), 94)]),
    expected(ev, fill_status="filled", filled_qty=1, gross="5", net="5", R="1", entry_avg="100", exit_avg="105", open_at=at(T), close_at=at(T, 60), mfe="1"))

# E11 区间入场梯
LADDER = plan(entries=[{"kind": "ladder", "price_lo": "98", "price_hi": "100", "fraction": "1", "tif": "GTC", "post_only": False}], sizing=("fixed_qty", "2"))
ev = Events().submit(at(T), "entry-0", "entry", "100", "1").submit(at(T), "entry-1", "entry", "98", "1")
entry_and_protect(ev, at(T))
ev.fill(at(T, 30), "entry-1", "entry", "98", "1")
ev.add(at(T, 30), "amended", "sl-0", "sl", price="95", qty="2").add(at(T, 30), "amended", "tp-0", "tp", price="105", qty="2")
ev.add(at(T, 60), "tp_triggered", "tp-0", "tp", tb="last", price="105", qty="2")
ev.fill(at(T, 60), "tp-0", "tp", "105", "2")
ev.add(at(T, 60), "cancelled", "sl-0", "sl", reason="position_closed").closed(at(T, 60))
F["E11"] = fixture("E11", "区间入场梯 [98,100] 两档各 1，均价 99，TP 105 平 2",
    "ladder_steps=2 → entry-0@100、entry-1@98（long 从高到低）。T last=100 成交 entry-0；保护腿 qty=1。T+30 last=98 成交 entry-1 → SL/TP amended qty=2。"
    "T+60 TP 105 平 2。entry_avg=99，gross=(105-99)×2=12，budget=10 → R=1.2；mfe：T+30 P=(100-99)×2=2→0.2，T+60 realized 12→1.2。",
    request("E11", T, plan_=LADDER, budget="10"), market("E11", last=[pt(at(T), 100), pt(at(T, 30), 98), pt(at(T, 60), 105)], mark=[pt(at(T), 100), pt(at(T, 30), 100), pt(at(T, 60), 100)]),
    expected(ev, fill_status="filled", filled_qty=2, gross="12", net="12", R="1.2", entry_avg="99", exit_avg="105", open_at=at(T), close_at=at(T, 60), mfe="1.2"))

# E12 多档止盈
MULTI = plan(tps=(("105", "0.5"), ("110", "0.25")), sizing=("fixed_qty", "4"))
ev = Events().submit(at(T), "entry-0", "entry", "100", "4")
ev.fill(at(T), "entry-0", "entry", "100", "4")
ev.protect(at(T), "95", "4", [("105", "2"), ("110", "1")])
ev.add(at(T, 60), "tp_triggered", "tp-0", "tp", tb="last", price="105", qty="2")
ev.fill(at(T, 60), "tp-0", "tp", "105", "2").add(at(T, 60), "amended", "sl-0", "sl", price="95", qty="2")
ev.add(at(T, 120), "tp_triggered", "tp-1", "tp", tb="last", price="110", qty="1")
ev.fill(at(T, 120), "tp-1", "tp", "110", "1").add(at(T, 120), "amended", "sl-0", "sl", price="95", qty="1")
ev.add(at(T, 180), "stop_triggered", "sl-0", "sl", tb="mark", price="94", qty="1")
ev.fill(at(T, 180), "sl-0", "sl", "94", "1").closed(at(T, 180))
F["E12"] = fixture("E12", "多档止盈：105×0.5、110×0.25，余 1 止损 94，SL 数量 4→2→1",
    "qty=4。tp-0=floor(4×0.5)=2，tp-1=floor(4×0.25)=1，余 1 只由 SL 覆盖。T+60 平 2@105 → SL amended 2；T+120 平 1@110 → SL amended 1；T+180 mark/last 94 止损 1。"
    "gross=10+10-6=14；exit_avg=(210+110+94)/4=103.5；budget 20 → R=0.7；mfe：T+120 realized 20 → 1；mae 0。",
    request("E12", T, plan_=MULTI, budget="20"),
    market("E12", last=[pt(at(T), 100), pt(at(T, 60), 105), pt(at(T, 120), 110), pt(at(T, 180), 94)],
           mark=[pt(at(T), 100), pt(at(T, 60), 100), pt(at(T, 120), 100), pt(at(T, 180), 94)]),
    expected(ev, fill_status="filled", filled_qty=4, gross="14", net="14", R="0.7", entry_avg="100", exit_avg="103.5", open_at=at(T), close_at=at(T, 180), mfe="1"))

# ---------------------------------------------------------------------------
# E14 filter / 资金不足 → rejected
# ---------------------------------------------------------------------------
def rejected_fixture(eid, title, deriv, plan_, policy="fixture-zero-v1", reason="PRICE_FILTER", qty="1", price="100.5", rules=None):
    ev = Events().add(at(T), "submitted", "entry-0", "entry", price=price, qty=qty)
    ev.add(at(T), "rejected", "entry-0", "entry", reason=reason).closed(at(T), reason="no_fill")
    return fixture(eid, title, deriv, request(eid, T, plan_=plan_, policy=policy),
                   market(eid, last=[pt(at(T), 100), pt(at(T, 60), 105)], mark=[pt(at(T), 100), pt(at(T, 60), 100)], rules=rules),
                   expected(ev, fill_status="none", filled_qty=0, gross="0", net="0", R="0"))


F["E14a"] = rejected_fixture("E14a", "PRICE_FILTER：限价 100.5 不在 tick=1 网格 → rejected",
    "用户明确价格不合 tick 不偷偷调整：submitted→rejected(PRICE_FILTER)，无 fill；fill_status=none，net=R=0，closed(no_fill)。",
    plan(entries=[{"kind": "limit", "price_lo": "100.5", "price_hi": "100.5", "fraction": "1", "tif": "GTC", "post_only": False}], sizing=("fixed_qty", "1")))
F["E14b"] = rejected_fixture("E14b", "MIN_NOTIONAL：qty1×100=100 < 101 → rejected",
    "min_notional=101，名义额 100 不足：rejected(MIN_NOTIONAL)。合法边界对照：min_notional=100 时须接受（见 test_kernel_a）。",
    plan(sizing=("fixed_qty", "1")), reason="MIN_NOTIONAL", price="100", rules={"tick_size": "1", "step_size": "1", "min_notional": "101", "multiplier": "1"})
F["E14c"] = rejected_fixture("E14c", "MARGIN：钱包 99 < 名义额 100 / 杠杆 1 → rejected",
    "policy fixture-wallet99-v1 钱包 99，入场预留 100/1=100 > 99：rejected(MARGIN)，不自动加杠杆。",
    plan(sizing=("fixed_qty", "1")), policy="fixture-wallet99-v1", reason="MARGIN", price="100")

# ---------------------------------------------------------------------------
# E15 右删失 / MARK_STALE；E16 funding 缺口
# ---------------------------------------------------------------------------
ev = Events().submit(at(T), "entry-0", "entry", "100", "1")
entry_and_protect(ev, at(T))
F["E15a"] = fixture("E15a", "右删失：窗口结束仍有仓 → LABEL_RIGHT_CENSORED",
    "T 入场，T+60 行情 100 无出场，horizon_end=T+120 仍持仓 → censor=LABEL_RIGHT_CENSORED；net_pnl/net_R=null；gross（已实现）=0；保留入场事件；无 closed。",
    request("E15a", T, horizon_s=120), market("E15a", last=[pt(at(T), 100), pt(at(T, 60), 100)], mark=[pt(at(T), 100), pt(at(T, 60), 100)]),
    expected(ev, fill_status="filled", filled_qty=1, gross="0", censor="LABEL_RIGHT_CENSORED", entry_avg="100", open_at=at(T)))

ev = Events().submit(at(T), "entry-0", "entry", "100", "1")
entry_and_protect(ev, at(T))
F["E15b"] = fixture("E15b", "MARK_STALE：持仓期间 mark 年龄 121s → 删失",
    "T 入场（mark T=100）。T+121 处理 last 时 mark 年龄 121>120 → censor=MARK_STALE，mark_ok=false，停止撮合（T+121 的 last=105 不触发 TP）。net=null。",
    request("E15b", T), market("E15b", last=[pt(at(T), 100), pt(at(T, 121), 105)], mark=[pt(at(T), 100)]),
    expected(ev, fill_status="filled", filled_qty=1, gross="0", censor="MARK_STALE", entry_avg="100", open_at=at(T), coverage={**ALL_OK, "mark_ok": False}))

ev = Events().submit(at(TF), "entry-0", "entry", "100", "1")
entry_and_protect(ev, at(TF))
F["E16"] = fixture("E16", "funding 缺口：持仓跨 08:00 但结算行缺失 → FUNDING_SCHEDULE_GAP",
    "07:59 入场，08:00 应有结算（8h 网格）但 funding 表无行且 schedule 不完整 → censor=FUNDING_SCHEDULE_GAP，funding_ok=false；后续 08:30 last=105 不再处理。net=null，不能补零得完整 R。",
    request("E16", TF), market("E16", last=[pt(at(TF), 100), pt(at(TF, 31 * 60), 105)], mark=[pt(at(TF), 100), pt(at(TF, 31 * 60), 100)], schedule_complete=False),
    expected(ev, fill_status="filled", filled_qty=1, gross="0", censor="FUNDING_SCHEDULE_GAP", entry_avg="100", open_at=at(TF), coverage={**ALL_OK, "funding_ok": False}))

# E17 费用与滑点（fixture-tick-v1：taker 0.05%，market 滑点 1 tick）
MKT = plan(entries=[{"kind": "market_ref", "price_lo": "100", "price_hi": "100", "fraction": "1", "tif": "GTC", "post_only": False}], tps=(("110", "1"),))
ev = Events().add(at(T), "submitted", "entry-0", "entry", qty="1").add(at(T), "accepted", "entry-0", "entry")
ev.fill(at(T), "entry-0", "entry", "101", "1", fee="0.0505")
ev.protect(at(T), "95", "1", [("110", "1")])
ev.add(at(T, 60), "stop_triggered", "sl-0", "sl", tb="mark", price="94", qty="1")
ev.add(at(T, 60), "cancelled", "tp-0", "tp", reason="sl_triggered")
ev.fill(at(T, 60), "sl-0", "sl", "109", "1", fee="0.0545").closed(at(T, 60))
F["E17"] = fixture("E17", "费用与滑点：market 买 100→101、SL market 卖 110→109，taker 0.05%",
    "market_ref 入场参考 last=100，滑点 1 tick 买加 → 101，fee=101×0.0005=0.0505。T+60 mark=94 触发 SL，last 参考 110 卖减 → 109，fee=0.0545。"
    "gross=109-101=8；fees=0.105；slippage=1+1=2（已含在成交价里，不再从 net 扣）；net=8-0.105=7.895；R=1.579。mae：T 时 P=(100-101)=-1→-0.2；mfe：1.6。",
    request("E17", T, plan_=MKT, policy="fixture-tick-v1"), market("E17", last=[pt(at(T), 100), pt(at(T, 60), 110)], mark=[pt(at(T), 100), pt(at(T, 60), 94)]),
    expected(ev, fill_status="filled", filled_qty=1, fees="0.105", slippage="2", gross="8", net="7.895", R="1.579", entry_avg="101", exit_avg="109",
             open_at=at(T), close_at=at(T, 60), mae="-0.2", mfe="1.6"))

# E18 入场即越 SL
ev = Events().submit(at(T), "entry-0", "entry", "100", "1")
entry_and_protect(ev, at(T))
ev.add(at(T), "stop_triggered", "sl-0", "sl", tb="mark", price="94", qty="1")
ev.add(at(T), "cancelled", "tp-0", "tp", reason="sl_triggered")
ev.fill(at(T), "sl-0", "sl", "100", "1").closed(at(T))
F["E18"] = fixture("E18", "入场即越 SL：mark 94 已低于 95，入场后同点保护重检立即触发并用剩余容量平仓",
    "T 点 mark=94（无仓位时不触发），last=100 容量 2 → 入场 1@100；保护腿建立后 P7 重检 mark 94≤95 → 同 ts stop_triggered，取消 TP，同点剩余容量 1 平 1@100。gross=net=R=0；不得等下一 bar。",
    request("E18", T), market("E18", last=[pt(at(T), 100, cap=2), pt(at(T, 60), 100)], mark=[pt(at(T), 94), pt(at(T, 60), 94)]),
    expected(ev, fill_status="filled", filled_qty=1, gross="0", net="0", R="0", entry_avg="100", exit_avg="100", open_at=at(T), close_at=at(T)))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for eid, fx in F.items():
        (OUT / f"{eid}.json").write_text(json.dumps(fx, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(F)} fixtures to {OUT}")


if __name__ == "__main__":
    main()
