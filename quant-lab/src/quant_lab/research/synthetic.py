"""合成夹具生成器 —— 按 G1（research-schema §2/§9）与 G2（execution-interface §5 + report-G2 §1.1/§3.3）签名造假数据。

P1 全部在合成数据上完成：fake_bars（silver bar 列）、fake_episodes（gold/episode 决策图视图，含 t_dec 实列、
order_plan、decision_snapshot_hash）、fake_execution（simulate_batch 输出列，net_R 可空）。
instrument_id 一律 `BTCUSDT-PERP.BINANCE-UM` 形式（裁定 A1/B1）。零生产凭据，纯 numpy 随机。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal

import numpy as np
import polars as pl

UTC_US = pl.Datetime("us", "UTC")
DEC = pl.Decimal(38, 12)
#: G2 simulate_batch 输出 schema 的本地镜像（report-G2 §3.3 + R9 + §5.9 B5）；test_g2_parity 断言与 quant_lab.market.execution.BATCH_SCHEMA 逐列一致
EVENT_STRUCT = pl.Struct({
    "seq": pl.Int64, "ts": UTC_US, "kind": pl.Utf8, "order_id": pl.Utf8, "leg": pl.Utf8, "trigger_basis": pl.Utf8,
    "price": DEC, "qty": DEC, "fee": DEC, "reason": pl.Utf8, "bar_open_time": UTC_US, "path_step": pl.Utf8, "cash_delta": DEC,
})
BATCH_SCHEMA: dict[str, pl.DataType] = {
    "episode_id": pl.Utf8,
    "graph_version": pl.Utf8,
    "decision_snapshot_hash": pl.Utf8,
    "t_dec": UTC_US,
    "policy_version": pl.Utf8,
    "policy_hash": pl.Utf8,
    "cost_scenario": pl.Utf8,
    "path_scenario": pl.Utf8,
    "market_manifest": pl.Utf8,
    "execution_contract_version": pl.Utf8,
    "seed": pl.Int64,
    "entry_ttl_s": pl.Int64,
    "entry_fractions": pl.List(DEC),
    "tp_fractions": pl.List(DEC),
    "kernel": pl.Utf8,
    "kernel_version": pl.Utf8,
    "trace_hash": pl.Utf8,
    "fill_status": pl.Utf8,
    "filled_qty": DEC,
    "fees": DEC,
    "funding": DEC,
    "slippage": DEC,
    "gross_pnl": DEC,
    "net_pnl": DEC,
    "net_R": DEC,
    "censor_reason": pl.Utf8,
    "entry_avg_price": DEC,
    "exit_avg_price": DEC,
    "position_open_at": UTC_US,
    "position_close_at": UTC_US,
    "mae_R": DEC,
    "mfe_R": DEC,
    "entry_ttl_source": pl.Utf8,
    "fraction_source": pl.Utf8,
    "outcome_kind": pl.Utf8,
    "exit_legs": pl.List(pl.Utf8),
    "horizon_source": pl.Utf8,          # §5.13 B11 诊断列：观察窗来自 policy 推导还是调用方自选（caller 时进 G3 config_id）
    "mark_ok": pl.Boolean,
    "funding_ok": pl.Boolean,
    "rules_ok": pl.Boolean,
    "bars_ok": pl.Boolean,
    "liquidation_unmodeled": pl.Boolean,
    "risk_budget": DEC,
    "canonical_events": pl.List(EVENT_STRUCT),
}
DEFAULT_INSTRUMENTS = ("BTCUSDT-PERP.BINANCE-UM", "ETHUSDT-PERP.BINANCE-UM", "SOLUSDT-PERP.BINANCE-UM")
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "8h": 28800}
T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
CENSOR_REASONS = ("LABEL_RIGHT_CENSORED", "MARK_STALE", "BAR_GAP")


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- bars
def fake_bars(
    instrument_ids: tuple[str, ...] | list[str] = DEFAULT_INSTRUMENTS,
    *, start: dt.datetime = T0, n_bars: int = 2000, interval: str = "15m", seed: int = 0,
    gap_frac: float = 0.0, vol: float = 0.002,
) -> pl.DataFrame:
    """silver bar 表（report-G2 §1.1）。close_time = open_time + interval（右端点）；available_at = close_time（H0, latency=0）。
    gap_frac>0 时随机丢弃 bar（墙钟缺 bar，不插值）。"""
    bad = [i for i in instrument_ids if not isinstance(i, str) or not i]
    if bad:
        raise ValueError(f"fake_bars: instrument_id 须为非空字符串，得到 {bad}（决策视图不得含 null instrument，research-schema §9.7）")
    rng = np.random.default_rng(seed)
    step = dt.timedelta(seconds=INTERVAL_SECONDS[interval])
    frames = []
    for k, inst in enumerate(instrument_ids):
        base = 100.0 * (k + 1)
        rets = rng.normal(0, vol, n_bars)
        close = base * np.exp(np.cumsum(rets))
        open_ = np.concatenate([[base], close[:-1]])
        hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, n_bars)))
        lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, n_bars)))
        opens = [start + i * step for i in range(n_bars)]
        closes = [o + step for o in opens]
        volu = rng.gamma(2.0, 50.0, n_bars)
        df = pl.DataFrame({
            "instrument_id": [inst] * n_bars, "interval": [interval] * n_bars,
            "open_time": opens, "close_time": closes,
            "open": open_, "high": hi, "low": lo, "close": close,
            "volume": volu, "quote_volume": volu * close, "taker_buy_volume": volu * 0.5,
            "taker_buy_quote_volume": volu * close * 0.5, "trades": rng.integers(10, 500, n_bars),
        }, schema_overrides={"open_time": UTC_US, "close_time": UTC_US, "trades": pl.Int64})
        if gap_frac > 0:
            keep = rng.random(n_bars) >= gap_frac
            df = df.filter(pl.Series(keep))
        frames.append(df)
    out = pl.concat(frames)
    return out.with_columns(
        pl.col("close_time").alias("event_time"),
        pl.col("close_time").alias("available_at"),
        pl.lit(start).cast(UTC_US).alias("ingested_at"),
        pl.lit(True).alias("ohlc_valid"), pl.lit(False).alias("spike_flag"), pl.lit(False).alias("gap_flag"),
        pl.lit(0.0).alias("spike_score"), pl.lit("synthetic").alias("source_sha256"), pl.lit("g3-synth-v0").alias("rule_version"),
    ).sort(["instrument_id", "open_time"])


# ---------------------------------------------------------------- episodes
def _q12(x: float) -> Decimal:
    return Decimal(f"{x:.12f}")


ORDER_PLAN_DTYPE = pl.Struct({
    "instrument_id": pl.Utf8, "side": pl.Utf8,
    "entries": pl.List(pl.Struct({"kind": pl.Utf8, "price_lo": DEC, "price_hi": DEC, "fraction": DEC, "tif": pl.Utf8, "post_only": pl.Boolean})),
    "stop": pl.Struct({"price": DEC, "trigger": pl.Utf8}),
    "tps": pl.List(pl.Struct({"level": DEC, "fraction": DEC})),
    "sizing": pl.Struct({"mode": pl.Utf8, "qty": DEC}),
    "expiry": pl.Struct({"entry_ttl_s": pl.Int64, "max_holding_s": pl.Int64}),
    "reduce_only_exit": pl.Boolean,
})


def _order_plan(inst: str, side: str, entry: float, stop: float, tps: list[float], entry_ttl_s: int | None = 3600, fractions_given: bool = True) -> dict:
    """report-G2 §3.1 结构；数值列 Decimal(38,12)（research-schema §9.10.1 A8）；entry_ttl_s 可空（§5.9）；
    fraction 可空且全有或全无（§5.10：原文未给分配比例时 G1 留 null，由 G2 policy 兜底）。"""
    n = len(tps)
    tp_fr = [Decimal(1) / n for _ in tps]
    if fractions_given and n:
        tp_fr = [f.quantize(Decimal("1e-12"), rounding="ROUND_DOWN") for f in tp_fr]
        tp_fr[-1] = Decimal(1) - sum(tp_fr[:-1])          # 余量并入末档，和恰为 1
    return {
        "instrument_id": inst, "side": side,
        "entries": [{"kind": "limit", "price_lo": _q12(entry), "price_hi": _q12(entry), "fraction": Decimal(1) if fractions_given else None, "tif": "GTD", "post_only": False}],
        "stop": {"price": _q12(stop), "trigger": "mark"},
        "tps": [{"level": _q12(tp), "fraction": (tp_fr[i] if fractions_given else None)} for i, tp in enumerate(tps)],
        "sizing": {"mode": "risk_budget", "qty": None},
        "expiry": {"entry_ttl_s": entry_ttl_s, "max_holding_s": 86400 * 3},
        "reduce_only_exit": True,
    }


def fake_episodes(
    n: int = 200, *, instrument_ids: tuple[str, ...] | list[str] = DEFAULT_INSTRUMENTS,
    start: dt.datetime = T0, span_days: int = 400, seed: int = 0, n_channels: int = 5,
    graph_version: str = "gv-synth-0001", censor_frac: float = 0.05, cluster_window_h: int = 24,
    bars: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """gold/episode 决策图视图子集（research-schema §2 + §9.1 增列）。

    - `t_dec` 是实列（裁定 A2），= available_at + processing_delay_s；`decision_eligible_at` 只作诊断列。
    - `cluster_id`：同品种、t_dec 相距 < cluster_window_h 的 episode 连通成一簇（E.1 经济簇的合成近似）。
    - `order_plan` 结构按 report-G2 §3.1；`decision_snapshot_hash` = sha256(决策闭包内容)。
    - 若给 bars，入场价取 t_dec 前最后一根闭合 bar 的 close（等号语义由 features 层负责，这里只造数）。
    """
    rng = np.random.default_rng(seed)
    secs = rng.integers(0, span_days * 86400, n)
    secs.sort()
    insts = [instrument_ids[i] for i in rng.integers(0, len(instrument_ids), n)]
    sides = ["long" if x < 0.5 else "short" for x in rng.random(n)]
    chans = [f"ch{c:02d}" for c in rng.integers(0, n_channels, n)]
    avail = [start + dt.timedelta(seconds=int(s)) for s in secs]
    delay = rng.integers(0, 120, n)
    t_dec = [a + dt.timedelta(seconds=int(d)) for a, d in zip(avail, delay)]
    # 簇：同品种按 t_dec 顺序，相邻间隔 < 窗口 则同簇
    cluster = [""] * n
    last_by_inst: dict[str, tuple[dt.datetime, int]] = {}
    cid = 0
    for i in sorted(range(n), key=lambda k: t_dec[k]):
        prev = last_by_inst.get(insts[i])
        if prev and (t_dec[i] - prev[0]) < dt.timedelta(hours=cluster_window_h):
            cluster[i] = f"c{prev[1]:05d}"
            last_by_inst[insts[i]] = (t_dec[i], prev[1])
        else:
            cid += 1
            cluster[i] = f"c{cid:05d}"
            last_by_inst[insts[i]] = (t_dec[i], cid)
    price_lookup = None
    if bars is not None:
        price_lookup = bars.select("instrument_id", "close_time", "close")
    rows = []
    for i in range(n):
        inst, side = insts[i], sides[i]
        ref = 100.0 * (instrument_ids.index(inst) + 1)
        if price_lookup is not None:
            v = price_lookup.filter((pl.col("instrument_id") == inst) & (pl.col("close_time") <= pl.lit(t_dec[i]).cast(UTC_US)))
            if v.height:
                ref = float(v.sort("close_time")["close"][-1])
        risk = ref * 0.01
        stop = ref - risk if side == "long" else ref + risk
        tps = [ref + risk * m if side == "long" else ref - risk * m for m in (1.0, 2.0)]
        plan = _order_plan(inst, side, ref, stop, tps, entry_ttl_s=None if rng.random() < 0.3 else 3600,
                           fractions_given=rng.random() >= 0.3)   # 约 30% 原文无 TTL；约 30% 原文无分配比例（fraction 全 null）
        eid = f"ep-{seed:02d}-{i:05d}"
        censored = bool(rng.random() < censor_frac)
        rows.append({
            "episode_id": eid, "graph_version": graph_version,
            "predecessor_ids": [], "successor_ids": [],
            "channel_id": chans[i], "trader_id": f"tr-{chans[i]}", "instrument_id": inst, "side": side,
            "entry_branch_id": f"{eid}-b0", "duplicate_group_id": f"dg-{eid}", "cluster_id": cluster[i],
            "author_plan_state": "active", "author_claim_state": "unknown",
            "entry_observed": True, "exit_observed": not censored, "left_truncated": False, "right_censored": censored,
            "censor_at": (t_dec[i] + dt.timedelta(days=3)) if censored else None,
            "censor_reason": "LABEL_RIGHT_CENSORED" if censored else None,
            "time_grade_min": "V", "decision_eligible_at": avail[i],
            "t_dec": t_dec[i], "processing_delay_s": int(delay[i]),
            "order_plan": plan,
            "decision_snapshot_hash": _sha({"episode_id": eid, "plan": plan, "t_dec": t_dec[i]}),
            "temporal_assumptions": json.dumps({"basis": "synthetic", "processing_delay_s": int(delay[i])}),
            "is_tombstone": False, "tombstoned_at": None, "migration_reason": None,
            # research-schema §9.10.1 A8 第 5 条：struct 固定六键、Boolean 不可空，与 G1 gold 同构（约 3% entry_decision=False）
            "eligibility_by_estimand": {"description": True, "entry_decision": bool(rng.random() >= 0.03), "execution": True,
                                        "original_entry": True, "price_check": True, "outcome": not censored},
            "label_status": "auto_accepted_under_audit", "derivation_hash": _sha([eid, graph_version]),
            "event_time": avail[i], "available_at": avail[i], "ingested_at": avail[i] + dt.timedelta(seconds=1),
        })
    df = pl.DataFrame(rows, schema_overrides={
        "t_dec": UTC_US, "decision_eligible_at": UTC_US, "censor_at": UTC_US, "tombstoned_at": UTC_US,
        "event_time": UTC_US, "available_at": UTC_US, "ingested_at": UTC_US,
        "predecessor_ids": pl.List(pl.Utf8), "successor_ids": pl.List(pl.Utf8), "migration_reason": pl.Utf8,
        "processing_delay_s": pl.Int64,
        "eligibility_by_estimand": pl.Struct({k: pl.Boolean for k in ("description", "entry_decision", "execution", "original_entry", "price_check", "outcome")}),
        "order_plan": ORDER_PLAN_DTYPE,
    })
    return df.sort("t_dec")


# ---------------------------------------------------------------- execution
def _fractions(plan, key: str, total: Decimal = Decimal(1)) -> list[Decimal]:
    """§5.10 显式化分配：计划给定用计划值；缺失按 policy 'equal' 等分（余量并入末腿，标度 12）。"""
    legs = (plan or {}).get(key) or []
    if not legs:
        return []
    vals = [l.get("fraction") for l in legs]
    if all(v is not None for v in vals):
        if any(not isinstance(v, Decimal) or not v.is_finite() or v <= 0 for v in vals):
            raise ValueError(f"{key} fraction 须为正有限 Decimal")
        total_given = sum(vals)
        if (key == "entries" and total_given != 1) or (key == "tps" and total_given > 1):
            raise ValueError(f"{key} fraction 总量不合法")
        return list(vals)
    if any(v is not None for v in vals):
        raise ValueError(f"{key} fraction 必须全有或全无")
    n = len(legs)
    eq = (total / n).quantize(Decimal("1e-12"), rounding="ROUND_DOWN")
    out = [eq] * (n - 1) + [total - eq * (n - 1)]
    return out
def _policy_identity(policy_version: str) -> tuple[str, Decimal]:
    """已登记的 G2 政策使用真实内容身份；未登记名称仅为本合成桩的等分政策。"""
    from quant_lab.market.contract import POLICIES, resolve_policy
    if policy_version in POLICIES:
        policy = resolve_policy(policy_version)
        return policy.content_hash, policy.tp_total_fraction
    return _sha(["policy", policy_version]), Decimal(1)


def _fraction_source(plan) -> str:
    source = "plan"
    for key in ("entries", "tps"):
        _fractions(plan, key)
        legs = (plan or {}).get(key) or []
        if legs and all(leg.get("fraction") is None for leg in legs):
            source = "policy"
    return source


def fake_execution(
    episodes: pl.DataFrame, *, policy_version: str = "policy-synth-base", seed: int = 0,
    none_frac: float = 0.10, signal: np.ndarray | None = None, noise_sd: float = 1.0,
    cost_scenario: str = "base", path_scenario: str = "primary", market_manifest: str = "mm-synth-0001",
    kernel: str = "A", horizon_days: int = 3, residuals: np.ndarray | None = None, caller_horizon: bool = False,
) -> pl.DataFrame:
    """simulate_batch 输出（report-G2 §3.3 + R9 配对键），一行一 episode。

    net_R 判定表（feature-snapshot §7.4）：episode.right_censored → censor_reason 非空、net_R/net_pnl = null；
    fill_status=none → net_R=0；其余 net_R = signal_i + residual_i（单位 R）。skip 不在此产生（G3 侧记 0）。
    `signal` / `residuals` 供空模型与功效实验注入（长度 = episodes.height）。
    `caller_horizon=True` 表示观察窗由调用方自选（§5.13 B11：`horizon_source="caller"`，G3 侧须把 horizon_end 计入尝试身份）。
    """
    policy_hash, tp_total = _policy_identity(policy_version)
    rng = np.random.default_rng(seed)
    n = episodes.height
    res = residuals if residuals is not None else rng.normal(0.0, noise_sd, n)
    sig = signal if signal is not None else np.zeros(n)
    fill_none = rng.random(n) < none_frac
    cens = episodes["right_censored"].to_list()
    t_dec = episodes["t_dec"].to_list()
    plans = episodes["order_plan"].to_list() if "order_plan" in episodes.columns else [None] * n
    risk_budget = Decimal("100")
    rows = []
    for i in range(n):
        censored = bool(cens[i])
        if censored:
            status, net_r, reason = "filled", None, CENSOR_REASONS[i % len(CENSOR_REASONS)]
        elif fill_none[i]:
            status, net_r, reason = "none", Decimal("0"), None
        else:
            status, net_r, reason = "filled", Decimal(f"{float(sig[i] + res[i]):.12f}"), None
        net_pnl = None if net_r is None else (net_r * risk_budget)
        fee = Decimal("0") if status == "none" else Decimal("0.05")
        opened = None if status == "none" else t_dec[i] + dt.timedelta(minutes=15)
        closed = None if (status == "none" or censored) else t_dec[i] + dt.timedelta(hours=int(rng.integers(1, horizon_days * 24)))
        plan_ttl = (plans[i] or {}).get("expiry", {}).get("entry_ttl_s") if isinstance(plans[i], dict) else None
        events = [] if status == "none" else [
            {"seq": 0, "ts": t_dec[i], "kind": "submitted", "order_id": f"o-{i}", "leg": "entry", "trigger_basis": "none",
             "price": None, "qty": None, "fee": None, "reason": None, "bar_open_time": None, "path_step": "none", "cash_delta": None},
            {"seq": 1, "ts": opened, "kind": "filled", "order_id": f"o-{i}", "leg": "entry", "trigger_basis": "last",
             "price": Decimal("100"), "qty": Decimal("1"), "fee": fee, "reason": None, "bar_open_time": opened, "path_step": "C", "cash_delta": -fee},
        ]
        outcome = "filled_closed"
        if status == "none":
            outcome = "unfilled_expired"
        elif reason == "LABEL_RIGHT_CENSORED":
            outcome = "right_censored"
        elif reason is not None:
            outcome = "unevaluable"
        # exit_legs（§9.10.11 C）：出场成交实际参与的 leg 集合，排序去重；未开仓/未闭合为空
        exit_legs = [] if (status == "none" or closed is None) else (["sl"] if (float(net_r) if net_r is not None else 0.0) < 0 else ["tp"])
        horizon_source = "caller" if caller_horizon else "policy"      # §5.13 B11 诊断列
        rows.append({
            "episode_id": episodes["episode_id"][i], "graph_version": episodes["graph_version"][i],
            "decision_snapshot_hash": episodes["decision_snapshot_hash"][i], "t_dec": t_dec[i],
            "policy_version": policy_version, "policy_hash": policy_hash, "cost_scenario": cost_scenario, "path_scenario": path_scenario,
            "market_manifest": market_manifest, "execution_contract_version": "g2-exec-v0", "seed": seed,
            "entry_ttl_s": int(plan_ttl) if plan_ttl is not None else 86400,
            "kernel": kernel, "kernel_version": "synthetic-0", "trace_hash": _sha([episodes["episode_id"][i], policy_version, seed, kernel]),
            "fill_status": status, "filled_qty": Decimal("0") if status == "none" else Decimal("1"),
            "fees": fee, "funding": Decimal("0"), "slippage": Decimal("0"),
            "gross_pnl": net_pnl, "net_pnl": net_pnl, "net_R": net_r, "censor_reason": reason,
            "entry_avg_price": None if status == "none" else Decimal("100"), "exit_avg_price": None if closed is None else Decimal("101"),
            "position_open_at": opened, "position_close_at": closed,
            "mae_R": None if status == "none" else Decimal("-0.5"), "mfe_R": None if status == "none" else Decimal("0.8"),
            "entry_fractions": _fractions(plans[i], "entries"), "tp_fractions": _fractions(plans[i], "tps", tp_total),
            "entry_ttl_source": "plan" if plan_ttl is not None else "policy",
            "fraction_source": _fraction_source(plans[i]),
            "outcome_kind": outcome, "exit_legs": exit_legs, "horizon_source": horizon_source,
            "mark_ok": True, "funding_ok": True, "rules_ok": True, "bars_ok": True, "liquidation_unmodeled": True,
            "risk_budget": risk_budget, "canonical_events": events,
        })
    return pl.DataFrame(rows, schema=BATCH_SCHEMA)


EXECUTION_PAIR_KEYS = ("episode_id", "graph_version", "policy_version", "cost_scenario", "path_scenario", "kernel")

__all__ = ["BATCH_SCHEMA", "DEC", "ORDER_PLAN_DTYPE", "DEFAULT_INSTRUMENTS", "EVENT_STRUCT", "EXECUTION_PAIR_KEYS", "T0", "UTC_US", "fake_bars", "fake_episodes", "fake_execution"]
