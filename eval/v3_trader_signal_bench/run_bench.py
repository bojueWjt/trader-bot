#!/usr/bin/env python3
"""Offline v3-trader signal bench. Does not place orders."""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset.json"
DEFAULT_ENV = Path("/srv/hermes/profiles/trader/.env")
DEFAULT_BASE = "https://api.balenw.cloud/v1"

SYSTEM = """你是 trader-v3 的决策器，只根据 v3-trader 铁律判断频道消息该做什么。
禁止下单、禁止编造交易所查询结果。只输出一个 JSON 对象。

铁律：
1. 可执行开仓：有品种+方向，尽量有止损。有止损则 notional_mode=auto_with_sl（不要自己编仓位金额）。无止损才 small_fixed_no_sl。
2. 明确入场区间（如 2295-2315、7.36-7.42万）必须 entry_type=zone，填 price_min/price_max，禁止压成单点 limit。
3. 入场带“附近/左右/大约/约”则 entry_offset=true，价格仍填信号原值，不要自己平移。
4. “略破/小幅跌破/小幅涨破”且无精确止损数：基准位沿突破方向外扩 0.3%，止损再放宽 0.1%。多单止损再下、空单止损再上。
5. 万= x10000（7.36万=73600）。止盈点位若省“万”但入场是万档，按万档补齐。
6. 美股/股票代码映射为 <TICKER>USDT：MU=MUUSDT，谷歌=GOOGLUSDT，SNDK=SNDKUSDT，SPCX=SPCXUSDT。原油CL=CLUSDT，黄金=XAUUSDT。
7. 多个入场价（建仓+补仓）action=open_batch，batch_entries 填每一档，不要合成一笔。
8. “锁定X%利润/减仓X%/止盈一半”=partial，partial_pct 为仓位百分比。
9. 只调止损用 set_sl；只平仓用 close。
10. 复盘/战绩/已实现盈亏回顾且没有新的带点位指令 -> skip/review。
11. 社群噪音、活动、穿过价格提醒、连通性测试 -> skip/noise 或 skip/non_actionable。
12. 等信号、还没到、观察 -> skip/wait。
13. 缺品种或缺关键点位、无法映射合约 -> skip/missing_params，禁止猜。
14. 消息明确“不要玩/不要做” -> skip/non_actionable。
15. 纯分析、现货定投感想、没有合约执行点 -> skip/analysis。

输出 JSON schema：
{
  "action": "open|open_batch|close|partial|set_sl|skip",
  "skip_reason": "noise|review|analysis|wait|missing_params|non_actionable"|null,
  "symbol": "BTCUSDT"|null,
  "side": "long|short"|null,
  "entry_type": "market|limit|zone"|null,
  "price": number|null,
  "price_min": number|null,
  "price_max": number|null,
  "sl": number|null,
  "tps": [number],
  "entry_offset": boolean,
  "batch_entries": [number],
  "partial_pct": number|null,
  "notional_mode": "auto_with_sl|small_fixed_no_sl|none",
  "rationale": "不超过40字"
}
只输出 JSON。"""

CONFIGS = [
    {"name": "gpt-5.6-sol", "model": "gpt-5.6-sol", "reasoning_effort": None},
    {"name": "spark-medium", "model": "gpt-5.3-codex-spark", "reasoning_effort": "medium"},
    {"name": "spark-high", "model": "gpt-5.3-codex-spark", "reasoning_effort": "high"},
    {"name": "spark-xhigh", "model": "gpt-5.3-codex-spark", "reasoning_effort": "xhigh"},
]

SYMBOL_ALIASES = {
    "BTCUSDT": {"BTCUSDT", "BTC", "BITCOIN", "比特币", "大饼"},
    "ETHUSDT": {"ETHUSDT", "ETH", "以太", "以太坊", "姨太"},
    "SOLUSDT": {"SOLUSDT", "SOL"},
    "SNDKUSDT": {"SNDKUSDT", "SNDK", "闪迪"},
    "GOOGLUSDT": {"GOOGLUSDT", "GOOGL", "GOOG", "谷歌", "GOOGLE"},
    "MUUSDT": {"MUUSDT", "MU", "美光"},
    "SPCXUSDT": {"SPCXUSDT", "SPCX"},
    "ZROUSDT": {"ZROUSDT", "ZRO"},
    "FILUSDT": {"FILUSDT", "FIL"},
    "TIAUSDT": {"TIAUSDT", "TIA"},
    "ETHFIUSDT": {"ETHFIUSDT", "ETHFI"},
    "JTOUSDT": {"JTOUSDT", "JTO"},
    "CLUSDT": {"CLUSDT", "CL", "美油", "原油"},
    "XAUUSDT": {"XAUUSDT", "XAU", "GOLD", "黄金"},
}


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def call_model(base: str, key: str, cfg: dict, user: str, timeout: int) -> dict:
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": 1200,
    }
    if cfg.get("reasoning_effort"):
        body["reasoning_effort"] = cfg["reasoning_effort"]
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode())
        sec = time.perf_counter() - t0
        msg = ((raw.get("choices") or [{}])[0].get("message") or {})
        text = msg.get("content") or ""
        usage = raw.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return {
            "ok": True,
            "sec": round(sec, 3),
            "text": text,
            "pred": extract_json(text),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "reasoning_tokens": details.get("reasoning_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
        }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "sec": round(time.perf_counter() - t0, 3),
            "http": exc.code,
            "error": exc.read()[:400].decode("utf-8", "replace"),
            "pred": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "sec": round(time.perf_counter() - t0, 3),
            "error": f"{type(exc).__name__}: {exc}",
            "pred": None,
        }


def norm_sym(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip().upper().replace(".P", "").replace("-", "")
    if raw.endswith("USDT.P"):
        raw = raw[:-6] + "USDT"
    if not raw.endswith("USDT") and re.fullmatch(r"[A-Z0-9]{2,12}", raw):
        raw = raw + "USDT"
    for canon, aliases in SYMBOL_ALIASES.items():
        compact = {a.upper().replace("USDT", "") for a in aliases}
        key = raw.replace("USDT", "")
        if raw == canon or raw in {a.upper() for a in aliases} or key in compact:
            return canon
    return raw


def close_num(pred, accepts, rel=0.012) -> bool:
    if pred is None or not accepts:
        return False
    try:
        p = float(pred)
    except (TypeError, ValueError):
        return False
    for a in accepts:
        try:
            g = float(a)
        except (TypeError, ValueError):
            continue
        if g == 0:
            if abs(p) < 1e-9:
                return True
            continue
        if abs(p - g) / abs(g) <= rel:
            return True
        # 万-unit mismatch: 747 vs 74700
        if abs(p * 10000 - g) / abs(g) <= rel or abs(p / 10000 - g) / abs(g) <= rel:
            return True
    return False


def score_item(gold: dict, pred: dict | None) -> dict:
    out = {
        "action_ok": False,
        "symbol_ok": None,
        "side_ok": None,
        "entry_ok": None,
        "offset_ok": None,
        "sl_ok": None,
        "strict_ok": False,
    }
    if not pred:
        return out
    action = str(pred.get("action") or "").strip()
    accept = set([gold["action"]] + list(gold.get("also_accept_actions") or []))
    out["action_ok"] = action in accept
    if gold["action"] == "skip":
        reason = str(pred.get("skip_reason") or "").strip()
        skip_ok = action == "skip" and (
            not gold.get("skip_accept") or reason in gold["skip_accept"] or reason == gold.get("skip_reason")
        )
        out["action_ok"] = skip_ok or (action == "skip")
        out["strict_ok"] = skip_ok
        return out
    gsym = gold.get("symbol")
    psym = norm_sym(pred.get("symbol"))
    out["symbol_ok"] = (psym == gsym) if gsym else None
    gside = gold.get("side")
    pside = (pred.get("side") or None)
    if pside:
        pside = str(pside).lower()
    out["side_ok"] = (pside == gside) if gside else None
    if gold.get("entry_type"):
        et = str(pred.get("entry_type") or "").lower()
        if gold["entry_type"] == "zone":
            out["entry_ok"] = et == "zone"
        elif gold["action"] == "open_batch":
            out["entry_ok"] = et in {"limit", "market", "zone"} or bool(pred.get("batch_entries"))
        else:
            out["entry_ok"] = et == gold["entry_type"] or (
                gold["entry_type"] == "market" and et in {"market", "limit"}
            )
    if "entry_offset" in gold:
        out["offset_ok"] = bool(pred.get("entry_offset")) is bool(gold["entry_offset"])
    if gold.get("sl_accept"):
        out["sl_ok"] = close_num(pred.get("sl"), gold["sl_accept"])
    needed = [out["action_ok"], out["symbol_ok"] is not False]
    if out["side_ok"] is not None:
        needed.append(out["side_ok"])
    if gold.get("entry_type") == "zone":
        needed.append(out["entry_ok"] is True)
    out["strict_ok"] = all(needed)
    return out


def user_prompt(item: dict) -> str:
    return (
        f"频道: {item['channel_name']} ({item['channel_id']})\n"
        f"消息ID: {item['source_message_id']}\n"
        f"时间: {item['received_at']}\n"
        f"正文:\n{item['text']}\n"
    )


def pack_row(item: dict, raw: dict) -> dict:
    pred = raw.get("pred")
    return {
        "id": item["id"],
        "channel": item["channel_name"],
        "gold_action": item["gold"]["action"],
        "pred_action": (pred or {}).get("action") if pred else None,
        "pred_symbol": (pred or {}).get("symbol") if pred else None,
        "pred_skip": (pred or {}).get("skip_reason") if pred else None,
        "rationale": (pred or {}).get("rationale") if pred else None,
        "score": score_item(item["gold"], pred),
        "ok": raw.get("ok"),
        "sec": raw.get("sec"),
        "usage": raw.get("usage"),
        "error": raw.get("error") or raw.get("http"),
        "pred": pred,
    }


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    ok = sum(1 for r in rows if r["score"]["action_ok"])
    strict = sum(1 for r in rows if r["score"]["strict_ok"])
    secs = [r["sec"] for r in rows if r.get("sec") is not None]
    fails = sum(1 for r in rows if not r.get("ok"))
    return {
        "n": n,
        "action_acc": round(ok / n, 4) if n else 0,
        "strict_acc": round(strict / n, 4) if n else 0,
        "action_correct": ok,
        "strict_correct": strict,
        "http_fail": fails,
        "latency_s": {
            "p50": round(sorted(secs)[len(secs) // 2], 3) if secs else None,
            "mean": round(sum(secs) / len(secs), 3) if secs else None,
            "max": round(max(secs), 3) if secs else None,
            "total": round(sum(secs), 3) if secs else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--out", default=str(HERE / "results.json"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--retry-failed",
        default="",
        help="existing results.json: retry only HTTP/parse failures, merge in place",
    )
    parser.add_argument("--retry-attempts", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    args = parser.parse_args()

    data = json.loads(Path(DATASET).read_text())
    items = data["items"]
    if args.limit:
        items = items[: args.limit]
    env = load_env(Path(args.env_file))
    key = env.get("CLIPROXYAPI_API_KEY") or env.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("missing API key in env file")

    by_id = {item["id"]: item for item in items}
    cfg_by_name = {cfg["name"]: cfg for cfg in CONFIGS}

    def run_one(cfg, item):
        raw = call_model(args.base_url, key, cfg, user_prompt(item), args.timeout)
        return cfg["name"], pack_row(item, raw)

    t0 = time.perf_counter()
    if args.retry_failed:
        existing = json.loads(Path(args.retry_failed).read_text())
        results = existing.get("results") or {}
        pending = []
        for name, rows in results.items():
            cfg = cfg_by_name.get(name)
            if not cfg:
                continue
            for i, row in enumerate(rows):
                if row.get("ok") and row.get("pred"):
                    continue
                item = by_id.get(row["id"])
                if item:
                    pending.append((name, cfg, i, item))
        print(f"retry {len(pending)} failed calls", flush=True)
        for name, cfg, idx, item in pending:
            row = None
            for attempt in range(1, args.retry_attempts + 1):
                _, row = run_one(cfg, item)
                print(
                    f"retry {name} {item['id']} try={attempt} ok={row['ok']} "
                    f"action_ok={row['score']['action_ok']} {row['sec']}s",
                    flush=True,
                )
                if row.get("ok") and row.get("pred"):
                    break
                time.sleep(args.retry_sleep)
            if row is not None:
                row["retried"] = True
                results[name][idx] = row
            time.sleep(args.retry_sleep)
        out_path = args.out if args.out != str(HERE / "results.json") else args.retry_failed
    else:
        jobs = []
        for cfg in CONFIGS:
            for item in items:
                jobs.append((cfg, item))
        results = {cfg["name"]: [] for cfg in CONFIGS}
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futs = [pool.submit(run_one, cfg, item) for cfg, item in jobs]
            done = 0
            for fut in as_completed(futs):
                name, row = fut.result()
                results[name].append(row)
                done += 1
                print(
                    f"{done}/{len(jobs)} {name} {row['id']} "
                    f"action_ok={row['score']['action_ok']} {row['sec']}s",
                    flush=True,
                )
        out_path = args.out

    wall = round(time.perf_counter() - t0, 3)
    for name in results:
        results[name].sort(key=lambda r: r["id"])

    summary = {
        "wall_s": wall,
        "n_items": len(items),
        "configs": {name: summarize(rows) for name, rows in results.items()},
        "note": "gpt-5.6 is not routed on api.balenw.cloud/jp-api; used gpt-5.6-sol. No live orders were sent.",
    }
    out = {"summary": summary, "results": results}
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("wrote", out_path)


if __name__ == "__main__":
    main()
