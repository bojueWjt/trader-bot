"""quant_lab.market.execution —— 执行接口（契约 §3：simulate / simulate_batch；M-07 replay CLI；M-09 DataFrame 输出）。

simulate 只做分发与结果边界验证，不重做 A/B 触发；simulate_batch 一 request 一行，列 = 配对键 + 结果标量 + canonical_events。
行情来源：显式 MarketView（夹具/合成）或按 market_manifest 从行情湖装载（`load_market_from_lake`，M-09/P2）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal

import polars as pl

from quant_lab.market.contract import (
    DF_DECIMAL, PAIR_KEY, REQUEST_ID_COLS, RESULT_SCALAR_COLS, ContractError, grid_points_between, EpisodeFixture, ExecutionRequest, ExecutionResult,
    MarketView, check_invariants, diff_result, load_fixtures,
)

MarketResolver = Callable[[ExecutionRequest], MarketView]
DEC = pl.Decimal(*DF_DECIMAL)      # S23：从契约常量派生，不再硬编码（改 DF_DECIMAL 必须改 schema）
EVENT_STRUCT = pl.Struct({
    "seq": pl.Int64, "ts": pl.Datetime("us", "UTC"), "kind": pl.Utf8, "order_id": pl.Utf8, "leg": pl.Utf8, "trigger_basis": pl.Utf8,
    "price": DEC, "qty": DEC, "fee": DEC, "reason": pl.Utf8, "bar_open_time": pl.Datetime("us", "UTC"), "path_step": pl.Utf8, "cash_delta": DEC,
})
BATCH_SCHEMA: dict[str, pl.DataType] = {
    "episode_id": pl.Utf8, "graph_version": pl.Utf8, "decision_snapshot_hash": pl.Utf8, "t_dec": pl.Datetime("us", "UTC"),
    "policy_version": pl.Utf8, "policy_hash": pl.Utf8, "cost_scenario": pl.Utf8, "path_scenario": pl.Utf8, "market_manifest": pl.Utf8,
    "execution_contract_version": pl.Utf8, "seed": pl.Int64, "entry_ttl_s": pl.Int64,
    "entry_fractions": pl.List(DEC), "tp_fractions": pl.List(DEC),
    "kernel": pl.Utf8, "kernel_version": pl.Utf8, "trace_hash": pl.Utf8, "fill_status": pl.Utf8,
    "filled_qty": DEC, "fees": DEC, "funding": DEC, "slippage": DEC, "gross_pnl": DEC, "net_pnl": DEC, "net_R": DEC,
    "censor_reason": pl.Utf8, "entry_avg_price": DEC, "exit_avg_price": DEC,
    "position_open_at": pl.Datetime("us", "UTC"), "position_close_at": pl.Datetime("us", "UTC"), "mae_R": DEC, "mfe_R": DEC,
    "entry_ttl_source": pl.Utf8, "fraction_source": pl.Utf8,
    "outcome_kind": pl.Utf8, "exit_legs": pl.List(pl.Utf8), "horizon_source": pl.Utf8,
    "mark_ok": pl.Boolean, "funding_ok": pl.Boolean, "rules_ok": pl.Boolean, "bars_ok": pl.Boolean, "liquidation_unmodeled": pl.Boolean,
    "risk_budget": DEC, "canonical_events": pl.List(EVENT_STRUCT),
}


def simulate(req: ExecutionRequest, *, kernel: Literal["A", "B"] = "A", market: MarketView | None = None,
             resolver: MarketResolver | None = None) -> ExecutionResult:
    """契约 §3：simulate(req, kernel="A"|"B")。行情由 market 或 resolver(req) 提供。"""
    if market is None:
        if resolver is None:
            raise ContractError("simulate 需要 market 或 resolver（按 market_manifest 装载行情）")
        market = resolver(req)
    if market.manifest_id != req.market_manifest:
        raise ContractError(f"market_manifest 不符：request={req.market_manifest} market={market.manifest_id}")
    if kernel == "A":
        from quant_lab.market.kernel_a import simulate_a
        res = simulate_a(req, market)
    elif kernel == "B":
        from quant_lab.market.nautilus_adapter import simulate_b
        res = simulate_b(req, market)
    else:
        raise ContractError(f"未知 kernel {kernel}")
    check_invariants(req, res, multiplier=market.rules.multiplier)      # S08：公共接缝同样带 multiplier
    return res


def _dec(x):
    return None if x is None else Decimal(x)


def result_row(req: ExecutionRequest, res: ExecutionResult) -> dict:
    row = {k: getattr(req, k) for k in REQUEST_ID_COLS}
    row["entry_fractions"] = list(req.entry_fractions)
    row["tp_fractions"] = list(req.tp_fractions)
    row.update({k: getattr(res, k) for k in RESULT_SCALAR_COLS})
    row["exit_legs"] = list(res.exit_legs)
    row.update(res.coverage_mask.model_dump())
    row["risk_budget"] = req.risk_budget
    row["canonical_events"] = [e.model_dump() for e in res.canonical_events]
    return row


def check_policy_hash_consistency(df: pl.DataFrame) -> None:
    """§5.18 B16(3) 补强：同一 policy_version 必须恰好映射一个 policy_hash。

    配对面带 policy_hash 之后，"同一版本串两套内容"不再被重复检测拦住，因此需要这条批量级断言——
    合并数据集时它当场炸，而不是让两臂悄悄配上不同经济假设。S31：错误信息必须列出**冲突的哈希取值**，
    否则排查时还要自己去翻数据（"引用会随被引用物损坏，抄录不会"同理适用于错误信息）。
    """
    if not df.height or "policy_hash" not in df.columns:
        return
    conflict = (df.group_by("policy_version").agg(pl.col("policy_hash").unique().alias("hashes"))
                  .filter(pl.col("hashes").list.len() > 1))
    if conflict.height:
        detail = "; ".join(f"{r['policy_version']} → {sorted(r['hashes'])}" for r in conflict.to_dicts())
        raise ContractError(f"同一 policy_version 对应多个 policy_hash（版本串与内容不一一对应）：{detail}")


def simulate_batch(reqs: Iterable[ExecutionRequest], *, kernel: Literal["A", "B"] = "A", markets: dict[str, MarketView] | None = None,
                   resolver: MarketResolver | None = None, strict: bool = True) -> pl.DataFrame:
    """一 request 一行；配对键 (episode_id, graph_version, policy_version, cost_scenario, path_scenario, kernel) 必须唯一。
    strict=False 时内核异常写入 error 列而不中断（研究批量），不变量失败仍中断。"""
    rows, errors = [], []
    for req in reqs:
        mk = markets.get(req.market_manifest) if markets else None
        try:
            res = simulate(req, kernel=kernel, market=mk, resolver=resolver)
        except AssertionError:
            raise
        except Exception as e:  # noqa: BLE001
            if strict:
                raise
            errors.append({"episode_id": req.episode_id, "error": f"{type(e).__name__}: {e}"})
            continue
        rows.append(result_row(req, res))
    df = pl.DataFrame(rows, schema=BATCH_SCHEMA) if rows else pl.DataFrame(schema=BATCH_SCHEMA)
    if df.height and df.select(list(PAIR_KEY)).is_duplicated().any():
        raise ContractError("simulate_batch 配对键重复：同键多行不得静默取最后一行")
    check_policy_hash_consistency(df)
    if errors:
        df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias("error"))
        schema = dict(df.schema)
        err_df = pl.DataFrame({c: [e.get(c) for e in errors] if c in ("episode_id", "error") else [None] * len(errors) for c in schema},
                              schema=schema)
        df = pl.concat([df, err_df], how="vertical")
    return df


# ---------------------------------------------------------------------------
# 从行情湖装载（P2 真实路径；M-09 提供最小实现）
# ---------------------------------------------------------------------------
def load_market_from_lake(req: ExecutionRequest, *, lake_root: str | Path = "data/lake/market", symbol: str | None = None,
                          window_before_s: int = 0, snapshot_id: str | None = None) -> MarketView:
    """按 request 的 instrument 与 [t_dec, horizon_end] 从 silver 装 1m last/mark bars + funding + rules（S05/S12）。
    覆盖证据：每个涉及分区的 manifest 必须存在且 check_status ∈ {ok, gap}（体检过）；quarantine severity=error 的 bar 与
    ohlc_valid=false 的 bar 视为不可用（bars_complete=False）；网格首尾完整性按期望 bar 数核对；
    manifest_refs（partition_id, source_sha256, schema_hash, available_at_basis, check_rule_version）进入 manifest_hash。"""
    import json as _json

    if snapshot_id is not None:
        raise ContractError("unsupported: versioned lake snapshot resolution (S12; G1/G0)")

    from quant_lab.market.contract import Bar, FundingRow, Rules
    from quant_lab.market.partition_check import load_rules, rule_at
    from quant_lab.market.vision import INTERVAL_SECONDS, LakePaths, partition_id, symbol_of

    lake = LakePaths(Path(lake_root))
    inst = req.order_plan.instrument_id
    sym = symbol or symbol_of(inst)
    # S27：启动时刻只有一个来源 —— req.resolved_t_start(policy)。此前 loader 自己用 (t_start or t_dec) 又推了一遍，
    # 省略 t_start 时漏掉 policy.latency_s，与 contract/kernel 分叉（同 S24 的"同一规则多处实现"形态）。
    from quant_lab.market.contract import resolve_policy as _rp
    a = req.resolved_t_start(_rp(req.policy_version)) - dt.timedelta(seconds=window_before_s)
    b = req.horizon_end
    refs: list[dict] = []
    problems: list[str] = ["unsupported: S12 versioned lake snapshot resolution; active silver only",
                           "unsupported: S02 U03 real settlement time; archive calc_time only"]
    quality_ok = [True]

    def manifest_ok(data_type: str, interval: str, period: str) -> bool:
        mp = lake.manifest(partition_id(data_type, interval, sym, period))
        if not mp.exists():
            problems.append(f"manifest missing {data_type} {period}")
            return False
        m = _json.loads(mp.read_text())
        refs.append({k: m.get(k) for k in ("partition_id", "source_sha256", "schema_hash", "available_at_basis", "check_rule_version", "check_status", "rule_version")})
        if m.get("check_status") not in ("ok", "gap"):
            problems.append(f"{m.get('partition_id')} check_status={m.get('check_status')}")
            return False
        return True

    def bars(data_type: str) -> tuple[list[Bar], bool]:
        sdir = lake.silver_dir(data_type, "1m", sym)
        out, complete = [], True
        d = a.date()
        months = set()
        while d <= b.date():
            months.add(d.strftime("%Y-%m"))
            p = sdir / f"date={d.isoformat()}" / "part.parquet"
            if p.exists():
                df = pl.read_parquet(p).filter((pl.col("open_time") >= a) & (pl.col("open_time") < b))
                if "gap_flag" not in df.columns or df["gap_flag"].is_null().any():
                    complete = False
                    quality_ok[0] = False
                    problems.append(f"{data_type} {d} gap_flag 缺列/null（质量未知）")
                elif df["gap_flag"].any():
                    complete = False
                # S05：缺列 / null（未知）/ false 一律质量失败——polars 的 .any() 会跳过 null，必须显式查 is_null
                if ("ohlc_valid" not in df.columns or df["ohlc_valid"].is_null().any()
                        or (~df["ohlc_valid"].fill_null(False)).any()):
                    complete = False
                    quality_ok[0] = False
                    problems.append(f"{data_type} {d} ohlc_valid 缺列/null/false（质量未知或失败）")
                # S14：逐行源版本必须等于所选 manifest 的 source_sha256；缺列/null/不符 → 不可验证，拒收（fail closed）
                mp = lake.manifest(partition_id(data_type, "1m", sym, d.strftime("%Y-%m")))
                want = _json.loads(mp.read_text()).get("source_sha256") if mp.exists() else None
                if df.height and (want is None or "source_sha256" not in df.columns or df["source_sha256"].is_null().any()
                                  or (df["source_sha256"] != want).any()):
                    complete = False
                    quality_ok[0] = False
                    problems.append(f"{data_type} {d} 行 source_sha256 缺列/null/与 manifest 不符（不可验证来源）")
                for r in df.select(["open_time", "open", "high", "low", "close", "volume"]).iter_rows():
                    out.append(Bar(open_time=r[0], o=Decimal(str(r[1])), h=Decimal(str(r[2])), l=Decimal(str(r[3])), c=Decimal(str(r[4])), volume=Decimal(str(r[5]))))
            else:
                complete = False
                problems.append(f"{data_type} {d} 无分区文件")
            d += dt.timedelta(days=1)
        for mo in sorted(months):
            if not manifest_ok(data_type, "1m", mo):
                complete = False
                quality_ok[0] = False
        # 网格首尾完整性：期望 bar 数 = [a, b) 内的**网格点个数**（S05：尾缺无后续行承载 gap_flag）。
        # 族B 漏网处：此前按 (b-a)/interval 取整，t_dec 带亚秒时窗口不与网格对齐，计数会错一根（S28 同族）。
        expected = grid_points_between(a, min(b, dt.datetime.now(dt.UTC)), INTERVAL_SECONDS["1m"])
        if len(out) != expected:
            complete = False
            problems.append(f"{data_type} 期望 {expected} 根，实际 {len(out)}")
        return out, complete

    last, ok1 = bars("klines")
    mark, ok2 = bars("markPriceKlines")
    fdir = lake.silver_dir("fundingRate", "8h", sym)
    frows: list[FundingRow] = []
    d = a.date()
    fmonths = set()
    while d <= b.date():
        fmonths.add(d.strftime("%Y-%m"))
        p = fdir / f"date={d.isoformat()}" / "part.parquet"
        if p.exists():
            for r in pl.read_parquet(p).select(["calc_time", "funding_rate", "funding_interval_hours"]).iter_rows():
                if a <= r[0] <= b:
                    frows.append(FundingRow(calc_time=r[0], rate=Decimal(str(r[1])), interval_hours=int(r[2])))
        d += dt.timedelta(days=1)
    funding_ok = all(manifest_ok("fundingRate", "8h", mo) for mo in sorted(fmonths))
    # S02：窗口内每个 8h 网格结算时刻必须有行（fundingRate 文件缺失/为空 → 缺证据，不能当 0 资金费）
    g = a.replace(minute=0, second=0, microsecond=0)
    g = g.replace(hour=(g.hour // 8) * 8)
    have = {f.calc_time for f in frows}
    while g <= b:
        if g >= a and not any(abs((h - g).total_seconds()) <= 60 for h in have):
            funding_ok = False
            problems.append(f"fundingRate 缺 {g.isoformat()} 结算行")
        g += dt.timedelta(hours=8)
    rules_df = load_rules(lake, inst)
    rr = rule_at(rules_df, inst, a)
    rr_end = rule_at(rules_df, inst, b) if rules_df is not None else None
    # S05：状态非 TRADING、tick/step/min_notional 未知、窗口内规则版本切换或区间重叠 → 未知规则，不得伪装已知
    overlap = False
    if rules_df is not None and rules_df.height > 1:
        rs = rules_df.filter(pl.col("instrument_id") == inst).sort("effective_from")
        for i in range(1, rs.height):
            prev_to = rs["effective_to"][i - 1]
            if prev_to is None or prev_to > rs["effective_from"][i]:
                overlap = True
    # G2-SC-01（G2 自查，同族第六例；G0 §5.16 B14）：multiplier 此前不在校验清单里，未知会被 `or "1"` 静默当成 1——
    # 而 S08 之后它缩放全部经济量（PnL/费用/资金费/滑点/预留），"未知当 1" 会让一笔算错的结果看起来完全正常。
    rules_known = (bool(rr) and bool(rr.get("tick_size")) and bool(rr.get("step_size")) and rr.get("min_notional") is not None
                   and rr.get("multiplier") is not None
                   and rr.get("status") == "TRADING" and rr_end is not None and rr_end.get("effective_from") == rr.get("effective_from") and not overlap)
    if rr and not rules_known:
        problems.append("规则未知：缺 tick/step/min_notional、status≠TRADING、区间重叠或窗口内版本切换（未支持）")
    rules = Rules(tick_size=Decimal(rr["tick_size"]), step_size=Decimal(rr["step_size"]),
                  min_notional=Decimal(rr["min_notional"]), multiplier=Decimal(rr["multiplier"]),
                  effective_from=rr["effective_from"], effective_to=rr["effective_to"]) if rules_known else Rules()
    return MarketView(manifest_id=req.market_manifest, bars_last=last, bars_mark=mark, funding=frows,
                      funding_schedule_complete=funding_ok, rules=rules, rules_known=rules_known, bars_complete=ok1 and ok2,
                      bars_quality_ok=quality_ok[0], manifest_refs=refs, quality_notes=problems)


# ---------------------------------------------------------------------------
# replay CLI（M-07 verify）：对夹具集回放并与独立期望比对
# ---------------------------------------------------------------------------
def replay(fixtures: list[EpisodeFixture], *, kernel: Literal["A", "B"] = "A", repeat: int = 2) -> dict:
    out = {"kernel": kernel, "passed": 0, "failed": 0, "results": []}
    for f in fixtures:
        if kernel not in f.kernels:
            continue
        rec = {"id": f.id, "ok": False, "diff": [], "trace_hash": None, "replay_consistent": None}
        try:
            hashes = []
            res = None
            for _ in range(max(1, repeat)):
                res = simulate(f.request, kernel=kernel, market=f.market)
                hashes.append(res.trace_hash)
            rec["trace_hash"] = hashes[0]
            rec["replay_consistent"] = len(set(hashes)) == 1
            rec["diff"] = diff_result(f.expected, res)
            rec["ok"] = not rec["diff"] and rec["replay_consistent"]
        except Exception as e:  # noqa: BLE001
            rec["diff"] = [f"EXC {type(e).__name__}: {e}"]
        out["passed" if rec["ok"] else "failed"] += 1
        out["results"].append(rec)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="quant_lab.market.execution")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("replay", help="回放夹具集并与独立期望比对")
    s.add_argument("--fixtures", default="tests/market/fixtures/episodes"); s.add_argument("--kernel", default="A", choices=["A", "B"])
    s.add_argument("--json", action="store_true"); s.add_argument("--repeat", type=int, default=2)
    a = p.parse_args(argv)
    if a.cmd == "replay":
        rep = replay(load_fixtures(a.fixtures), kernel=a.kernel, repeat=a.repeat)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
        else:
            for r in rep["results"]:
                print(f"{r['id']:<6} {'ok ' if r['ok'] else 'FAIL'} replay={r['replay_consistent']} {r['diff'][0][:160] if r['diff'] else ''}")
            print(f"kernel={rep['kernel']} passed={rep['passed']} failed={rep['failed']}")
        return 0 if rep["failed"] == 0 and rep["passed"] > 0 else 1     # S11：空集不算通过
    return 2


if __name__ == "__main__":
    sys.exit(main())
