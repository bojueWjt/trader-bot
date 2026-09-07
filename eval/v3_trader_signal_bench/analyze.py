#!/usr/bin/env python3
"""Score original gold vs a relaxed gold that accepts offline-eval-reasonable alternatives."""
from __future__ import annotations

import json
from pathlib import Path

from run_bench import score_item, summarize, norm_sym

HERE = Path(__file__).resolve().parent


def relaxed_gold(gold: dict, item_id: str) -> dict:
    g = dict(gold)
    extra = list(g.get("also_accept_actions") or [])
    if item_id == "S03":
        extra.append("open_batch")
    if item_id == "S16":
        extra.extend(["partial", "set_sl"])
        g["symbol_accept"] = ["ETHUSDT", "ONDOUSDT", "BTCUSDT"]
    if item_id == "S19":
        extra.append("skip")
    if item_id == "S23":
        extra.append("skip")
    g["also_accept_actions"] = extra
    return g


def score_relaxed(gold: dict, pred: dict | None, item_id: str) -> dict:
    g = relaxed_gold(gold, item_id)
    out = score_item(g, pred)
    if item_id == "S16" and pred:
        action = str(pred.get("action") or "")
        psym = norm_sym(pred.get("symbol"))
        if action in {"set_sl", "partial"} and psym in {"ETHUSDT", "ONDOUSDT", "BTCUSDT"}:
            out["action_ok"] = True
            out["symbol_ok"] = True
            out["strict_ok"] = True
    if item_id == "S19" and pred and str(pred.get("action") or "") == "skip":
        out["action_ok"] = True
        out["strict_ok"] = True
    if item_id == "S03" and pred and str(pred.get("action") or "") in {"open", "open_batch"}:
        if norm_sym(pred.get("symbol")) == "BTCUSDT":
            out["action_ok"] = True
            out["symbol_ok"] = True
            out["strict_ok"] = bool(out["side_ok"] is not False)
    return out


def main() -> None:
    ds = json.loads((HERE / "dataset.json").read_text())
    golds = {i["id"]: i["gold"] for i in ds["items"]}
    data = json.loads((HERE / "results.json").read_text())
    print("original")
    print(json.dumps(data["summary"]["configs"], ensure_ascii=False, indent=2))
    print("\nrelaxed (S03 open_batch, S16 ETH/ONDO/BTC manage, S19 skip CL/BZ)")
    relaxed_summary = {}
    for name, rows in data["results"].items():
        scored = []
        for r in rows:
            s = score_relaxed(golds[r["id"]], r.get("pred"), r["id"])
            scored.append({**r, "score": s, "ok": r.get("ok")})
        relaxed_summary[name] = summarize(scored)
        misses = [
            f"{r['id']}:{r.get('pred_action')}/{r.get('pred_symbol')}"
            for r, row in zip(rows, scored)
            if not row["score"]["action_ok"]
        ]
        print(name, relaxed_summary[name], "miss", misses)
    (HERE / "summary_relaxed.json").write_text(
        json.dumps({"original": data["summary"]["configs"], "relaxed": relaxed_summary}, indent=2)
        + "\n"
    )


if __name__ == "__main__":
    main()
