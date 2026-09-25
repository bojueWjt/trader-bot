"""真实行情湖的重放前准备：交易规则表 + 分区体检（L0 首跑 461/461 因 RULE_HISTORY_MISSING 与未体检全部删失）。

1. 保存一份币安 U 本位 exchangeInfo 公开快照（只读 GET，不需要任何凭据）。
2. 规则表：生命周期取自已下载分区的 manifest（rules_from_manifests，只证明「自最早分区起在交易」）；
   tick_size / step_size / min_notional 取自该快照，**假定自最早分区起未变**——这是假设，写进 source 字段，
   不是历史证据。快照时刻之前若币安改过精度，体检会把不在网格上的价格报出来。
3. 对这些品种的全部已下载分区跑 partition_check（K 线与资金费率），把 check_status 写回 manifest。

用法：python scripts/prepare_market_lake.py --symbols BTCUSDT,ETHUSDT,BCHUSDT,SOLUSDT [--lake <dir>] [--exchange-info <已存快照>]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from pathlib import Path

from quant_lab.market.partition_check import check_partition, rules_from_manifests, write_rules
from quant_lab.market.vision import LakePaths, instrument_id

EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"


def load_exchange_info(path: Path | None, save_to: Path) -> tuple[dict, str]:
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8")), path.name
    import httpx
    resp = httpx.get(EXCHANGE_INFO_URL, timeout=30)
    resp.raise_for_status()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H%MZ")
    save_to.mkdir(parents=True, exist_ok=True)
    out = save_to / f"exchangeInfo-{stamp}.json"
    out.write_text(resp.text, encoding="utf-8")
    return resp.json(), out.name


def filters_for(info: dict, symbol: str) -> dict[str, str | None]:
    s = next((x for x in info.get("symbols", []) if x.get("symbol") == symbol and x.get("contractType") == "PERPETUAL"), None)
    if s is None:
        raise SystemExit(f"exchangeInfo 快照里没有永续合约 {symbol}")
    f = {x["filterType"]: x for x in s.get("filters", [])}
    return {"tick_size": f.get("PRICE_FILTER", {}).get("tickSize"), "step_size": f.get("LOT_SIZE", {}).get("stepSize"),
            "min_notional": f.get("MIN_NOTIONAL", {}).get("notional")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="prepare_market_lake")
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--lake")
    ap.add_argument("--exchange-info", type=Path, help="已保存的 exchangeInfo 快照；不给则在线取一份并存到 reports/")
    a = ap.parse_args(argv)
    lake = LakePaths(Path(a.lake)) if a.lake else LakePaths.default()
    info, snap = load_exchange_info(a.exchange_info, lake.root.parent.parent / "reports")
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    for sym in symbols:
        inst = instrument_id(sym)
        fl = filters_for(info, sym)
        rules = rules_from_manifests(lake, inst, **fl)
        if rules.height == 0:
            print(f"{sym}: 湖里没有该品种的分区，跳过"); continue
        rules = rules.with_columns(source=__import__("polars").lit(
            f"manifests-inferred; tick/step/minNotional from {snap}, ASSUMED constant since effective_from"))
        write_rules(lake, rules)
        print(f"{sym}: rules effective_from={rules['effective_from'][0]} tick={fl['tick_size']} step={fl['step_size']} (假设自最早分区起不变)")
    statuses: Counter = Counter()
    for mp in sorted((lake.root / "_manifest").glob("*.json")):
        if mp.name.startswith("._"):
            continue
        m = json.loads(mp.read_text())
        sym = m.get("instrument_id", "").split("-")[0]
        if sym not in symbols or not m.get("actual_rows"):
            continue
        rep = check_partition(lake, data_type=m["data_type"], interval=m["interval"], symbol=sym, period=m["period"])
        statuses[(m["data_type"], rep.status)] += 1
        if rep.status not in ("ok",):
            print(f"  {m['partition_id']}: {rep.status} missing={rep.missing} reasons={dict(list(rep.reason_counts.items())[:4])}")
    for (dtype, st), n in sorted(statuses.items()):
        print(f"{dtype:>16} {st:>12} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
