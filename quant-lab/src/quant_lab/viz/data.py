"""Explicit-layout reads and single-episode L0 replay for the dashboard."""
from __future__ import annotations

from collections import defaultdict
import datetime as dt
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import tempfile
import threading

import polars as pl

from quant_lab.data.api import load_episodes, load_episode_events_description, load_message_texts
from quant_lab.data.graph import resolve_alias
from quant_lab.data.lake import Layout
from quant_lab.data.market_lake import LakeMarket
from quant_lab.market.contract import FILL_KINDS, resolve_policy
from quant_lab.market.execution import load_market_from_lake, simulate_batch
from quant_lab.market.l0_replay import attach_management, load_followup_actions, prepare_episode_request
from quant_lab.market.vision import LakePaths, symbol_of

VARIANTS = {"": "5天", "-be1": "保本", "-w14": "14天", "-live": "让点",
            "-be1-live": "让点+保本", "-follow": "跟指令", "-live-follow": "让点+跟指令",
            "-w60": "60天", "-w60-be1": "60天+保本"}
INTERVALS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
UTC = dt.timezone.utc


def json_value(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def dumps(value) -> str:
    return json.dumps(value, default=json_value, ensure_ascii=False, allow_nan=False)


def evaluable(row: dict) -> bool:
    return (row.get("fill_status") in {"filled", "partial"}
            and row.get("censor_reason") in (None, "") and row.get("net_R") is not None
            and all(row.get(key) is True for key in ("mark_ok", "funding_ok", "rules_ok", "bars_ok")))


def statistics_for(rows: list[dict]) -> dict:
    samples = sorted((r for r in rows if evaluable(r)), key=lambda r: (r["t_dec"], r["episode_id"]))
    days = defaultdict(list)
    curve, total = [], Decimal(0)
    for row in samples:
        value = Decimal(str(row["net_R"]))
        days[row["t_dec"].date()].append(float(value))
        total += value
        curve.append({"episode_id": row["episode_id"], "t_dec": row["t_dec"], "cumulative_R": float(total)})
    daily = [statistics.mean(values) for values in days.values()]
    interval = None
    if len(daily) >= 2:
        center = statistics.mean(daily)
        margin = 1.96 * statistics.stdev(daily) / math.sqrt(len(daily))
        interval = [center - margin, center + margin]
    conclusion = "≈0"
    if interval is not None:
        if interval[0] > 0:
            conclusion = "正期望"
        elif interval[1] < 0:
            conclusion = "负期望"
    return {"n": len(samples), "n_days": len(days), "win_rate": sum(float(r["net_R"]) > 0 for r in samples) / len(samples) if samples else None,
            "mean_R": float(total / len(samples)) if samples else None, "ci95": interval,
            "sum_R": float(total), "conclusion": conclusion, "cumulative_R": curve}


def mixed_timeline(messages: list[dict], events: list[dict]) -> list[dict]:
    items = [{**row, "type": "message"} for row in messages]
    items.extend({**row, "type": "backtest", "time": row["ts"]} for row in events)
    # Unknown clocks remain visible at the end; canonical seq orders equal-time fills.
    return sorted(items, key=lambda row: (row.get("time") or dt.datetime.max.replace(tzinfo=UTC),
                  0 if row["type"] == "message" else 1,
                  str(row.get("source_version_id", "")), row.get("seq", 0)))


def automatic_interval(start: dt.datetime, end: dt.datetime) -> str:
    span = (end - start).total_seconds()
    for name, seconds in INTERVALS.items():
        if span / seconds <= 1600:
            return name
    return "4h"


def read_bars(lake: Path, instrument: str, start: dt.datetime, end: dt.datetime, interval: str) -> dict:
    """Aggregate only available, valid 1m bars; never fill missing minutes."""
    seconds = INTERVALS[interval]
    result = {}
    for kind in ("klines", "markPriceKlines"):
        directory = LakePaths(lake).silver_dir(kind, "1m", symbol_of(instrument))
        frames = []
        day = start.date()
        while day <= end.date():
            path = directory / f"date={day.isoformat()}" / "part.parquet"
            if path.is_file():
                frame = pl.read_parquet(path).filter((pl.col("open_time") >= start) & (pl.col("open_time") <= end))
                if "ohlc_valid" in frame.columns:
                    frame = frame.filter(pl.col("ohlc_valid").fill_null(False))
                frames.append(frame.select("open_time", "open", "high", "low", "close"))
            day += dt.timedelta(days=1)
        bars = []
        if frames:
            frame = pl.concat(frames).sort("open_time").unique("open_time", keep="first", maintain_order=True)
            grouped = frame.group_by_dynamic("open_time", every=f"{seconds}s").agg(
                pl.col("open").first(), pl.col("high").max(), pl.col("low").min(), pl.col("close").last(), pl.len().alias("minutes"))
            for row in grouped.iter_rows(named=True):
                prices = [float(row[key]) for key in ("open", "high", "low", "close")]
                if all(math.isfinite(price) for price in prices):
                    bars.append({"time": int(row["open_time"].timestamp()), **dict(zip(("open", "high", "low", "close"), prices)),
                                 "minutes": row["minutes"], "partial": row["minutes"] < seconds // 60})
        result[kind] = bars
    return {"interval": interval, "start": start, "end": end, "series": result}


class Dashboard:
    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path).expanduser().resolve()
        config_bytes = self.config_path.read_bytes()
        self.config = json.loads(config_bytes)
        self.config_id = hashlib.sha256(str(self.config_path).encode() + config_bytes).hexdigest()
        base = self.config_path.parent

        def path(value):
            candidate = Path(value).expanduser()
            return (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

        self.reports = path(self.config["reports"])
        self.market_lake = path(self.config["market_lake"]) if self.config.get("market_lake") else None
        self.tag = self.config.get("tag", "v7")
        if not SAFE_KEY.fullmatch(self.tag):
            raise ValueError("invalid tag")
        self.channels = {}
        for channel in self.config["channels"]:
            key = channel["key"]
            if not SAFE_KEY.fullmatch(key) or key in self.channels:
                raise ValueError("invalid or duplicate channel key")
            self.channels[key] = {**channel, "layout": Layout.from_root(path(channel["data_root"]))}
        self.lock = threading.RLock()

    def channel(self, key: str) -> dict:
        if key not in self.channels:
            raise LookupError("未知频道")
        return self.channels[key]

    def variants(self) -> dict[str, dict]:
        suffixes = dict(VARIANTS)
        prefix = f"l0-{self.tag}"
        if self.reports.is_dir():
            for directory in self.reports.iterdir():
                if directory.is_dir() and directory.name.startswith(prefix):
                    suffix = directory.name[len(prefix):]
                    if suffix.startswith("-") and SAFE_KEY.fullmatch(suffix):
                        suffixes.setdefault(suffix, suffix)
        variants = {}
        for suffix, name in suffixes.items():
            key = "base" if suffix == "" else suffix[1:]
            if suffix and key == "base":
                key = "suffix:-base"  # Preserve an unknown -base without shadowing the default.
            variants[key] = {"suffix": suffix, "name": name}
        return variants

    def report(self, channel: str, variant: str) -> tuple[dict | None, list[dict]]:
        self.channel(channel)
        variants = self.variants()
        if variant not in variants:
            raise LookupError("未知口径")
        suffix = variants[variant]["suffix"]
        directory = self.reports / f"l0-{self.tag}{suffix}" / channel
        summary_path, trades_path = directory / "summary.json", directory / "trades.parquet"
        try:
            before = [(p.stat().st_mtime_ns, p.stat().st_size) for p in (summary_path, trades_path)]
            summary = json.loads(summary_path.read_text())
            rows = pl.read_parquet(trades_path).to_dicts()
            after = [(p.stat().st_mtime_ns, p.stat().st_size) for p in (summary_path, trades_path)]
            # L0 publishes parquet before summary. Refuse mixed generations while running.
            if before != after or before[0][0] < before[1][0] or summary["overall"]["n_trades"] != len(rows):
                return None, []
            if summary["channel"] != self.channel(channel)["channel_id"]:
                return None, []
            for row in rows:
                if row["graph_version"] != summary["graph_version"] or row["policy_hash"] != summary["policy_hash"]:
                    return None, []
            return summary, rows
        except (OSError, ValueError, KeyError, TypeError, pl.exceptions.PolarsError):
            return None, []

    def overview(self) -> dict:
        variants = self.variants()
        channels = []
        for key, channel in self.channels.items():
            stats = {}
            for variant in variants:
                summary, rows = self.report(key, variant)
                stats[variant] = {"status": "已出", **statistics_for(rows)} if summary is not None else {"status": "未出"}
            channels.append({"key": key, "name": channel["name"], "statistics": stats})
        return {"tag": self.tag, "variants": variants, "channels": channels}

    def trades(self, key: str) -> dict:
        channel = self.channel(key)
        collected, statuses = {}, {}
        variants = self.variants()
        for variant in variants:
            summary, rows = self.report(key, variant)
            statuses[variant] = "已出" if summary is not None else "未出"
            for row in rows:
                eid = row["episode_id"]
                if eid not in collected:
                    collected[eid] = {field: row.get(field) for field in ("episode_id", "t_dec", "instrument", "side", "entries", "stop", "targets")}
                    collected[eid]["variants"] = {}
                collected[eid]["variants"][variant] = {field: row.get(field) for field in (
                    "net_R", "fill_status", "outcome_kind", "n_teacher_actions_executed", "censor_reason", "trace_hash")}
        return {"key": key, "name": channel["name"], "variants": variants, "statuses": statuses,
                "trades": sorted(collected.values(), key=lambda r: (r["t_dec"], r["episode_id"])),
                "teacher_episode_ids": self.teacher_episode_ids(channel)}

    def teacher_episode_ids(self, channel: dict) -> list[str]:
        paths = {channel["layout"].silver_dir / "followup_action.parquet"}
        for variant in self.variants():
            summary, _ = self.report(channel["key"], variant)
            if summary and summary.get("follow_teacher", {}).get("path"):
                paths.add(Path(summary["follow_teacher"]["path"]).expanduser())
        ids = set()
        layout = channel["layout"]
        gv = resolve_alias(layout, channel["graph_version"])
        roots = defaultdict(list)
        if layout.episode(gv).is_file():
            episodes = load_episodes(gv, decision_graph=False, layout=layout)
            for episode in episodes.filter(pl.col("channel_id") == channel["channel_id"]).iter_rows(named=True):
                roots[episode["root_message_id"]].append(episode["episode_id"])
        for path in paths:
            if path.is_file():
                for row in load_followup_actions(path):
                    if row.get("channel_id", row.get("channel")) != channel["channel_id"]:
                        continue
                    if row.get("episode_id"):
                        ids.add(row["episode_id"])
                    ids.update(roots.get(row.get("target_message_id"), []))
        return sorted(ids)

    def context(self, channel_key: str, variant: str, episode_id: str) -> tuple:
        channel = self.channel(channel_key)
        # Validate against discovered IDs before any graph/lake access or cache name construction.
        summary, rows = self.report(channel_key, variant)
        matching = [row for row in rows if row["episode_id"] == episode_id]
        if len(matching) != 1:
            raise LookupError("未知单笔或该口径未出")
        trade = matching[0]
        layout = channel["layout"]
        gv = resolve_alias(layout, channel["graph_version"])
        if gv != summary["graph_version"]:
            raise ValueError("配置图版本与回测报告不一致")
        episodes = load_episodes(gv, layout=layout).filter(
            (pl.col("channel_id") == channel["channel_id"]) & (pl.col("episode_id") == episode_id)).to_dicts()
        if len(episodes) != 1:
            raise ValueError("回测单笔不在当前已发布决策图")
        episode = episodes[0]
        policy = resolve_policy(summary["policy_version"])
        if policy.content_hash != summary["policy_hash"]:
            raise ValueError("策略哈希与当前登记不一致")
        lake = Path(summary.get("market_lake") or self.market_lake or "").expanduser().resolve()
        if not summary.get("market_lake") and self.market_lake is None:
            raise ValueError("未配置行情湖")
        text = load_message_texts([episode["root_source_version_id"]], layout=layout).get(episode["root_source_version_id"])
        req, audit, why, _, _ = prepare_episode_request(episode, marks=LakeMarket(lake), lake=lake,
            policy=policy, risk_budget=Decimal(summary["risk_budget"]), text=text)
        if req is None:
            raise ValueError(f"无法复算：{why}")
        follow_path = Path(summary.get("follow_teacher", {}).get("path") or layout.silver_dir / "followup_action.parquet").expanduser()
        follow_rows = load_followup_actions(follow_path) if follow_path.is_file() else []
        if policy.follow_teacher:
            follow_rows = load_followup_actions(follow_path)  # Fail closed if recorded input disappeared.
            attached, _ = attach_management([req], follow_rows, policy=policy, channel=channel["channel_id"], graph_version=gv)
            req = attached[0]
        return channel, summary, trade, episode, req, audit, lake, follow_path, follow_rows

    def fingerprint(self, channel: dict, req, lake: Path, follow_path: Path) -> str:
        layout = channel["layout"]
        paths = {layout.message_version, layout.extracted_event, layout.episode(req.graph_version),
                 layout.episode_event(req.graph_version), follow_path}
        paths.update((layout.gold_dir / "_manifest").glob("*.json"))
        paths.update(Path(__file__).parents[1].joinpath("market").glob("*.py"))
        paths.update(Path(__file__).parent.glob("*.py"))
        # Cache revalidation includes mutable market partitions, manifests and rules.
        paths.update(lake.rglob("*.json"))
        for kind, interval in (("klines", "1m"), ("markPriceKlines", "1m"), ("fundingRate", "8h")):
            paths.update(LakePaths(lake).silver_dir(kind, interval, symbol_of(req.order_plan.instrument_id)).rglob("*.parquet"))
        paths.update(lake.rglob("*rules*.parquet"))
        stamps = []
        for path in sorted(paths):
            if path.is_file():
                stat = path.stat()
                stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
        return hashlib.sha256(dumps(stamps).encode()).hexdigest()

    def replay_one(self, key: str, variant: str, trade: dict, channel: dict, req, lake: Path, follow_path: Path) -> dict:
        identity = dumps([variant, key, req.episode_id, trade["trace_hash"]])
        filename = hashlib.sha256(identity.encode()).hexdigest() + ".json"
        cache = self.reports / "_dash_cache" / filename
        fingerprint = self.fingerprint(channel, req, lake, follow_path)
        with self.lock:
            try:
                doc = json.loads(cache.read_text())
                if doc["identity"] == identity and doc["fingerprint"] == fingerprint:
                    return doc["result"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
            result = simulate_batch([req], kernel="A", resolver=lambda request: load_market_from_lake(request, lake_root=lake)).to_dicts()[0]
            # A write during replay invalidates the result; do not persist mixed input generations.
            if fingerprint != self.fingerprint(channel, req, lake, follow_path):
                raise ValueError("复算输入正在变化，请稍后刷新")
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", dir=cache.parent, suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(dumps({"identity": identity, "fingerprint": fingerprint, "result": result}))
                temporary.replace(cache)
            except OSError:
                # Read-only reports mounts still permit uncached replay.
                if "temporary" in locals():
                    temporary.unlink(missing_ok=True)
            return result

    def message_timeline(self, channel: dict, episode: dict, req, events: list[dict], follow_rows: list[dict]) -> list[dict]:
        layout = channel["layout"]
        linked = load_episode_events_description(req.graph_version, layout=layout).filter(pl.col("episode_id") == req.episode_id).to_dicts()
        linked_sources = {row["source_version_id"] for row in linked} | {episode["root_source_version_id"]}
        extracted = pl.read_parquet(layout.extracted_event).to_dicts() if layout.extracted_event.is_file() else []
        by_source = defaultdict(list)
        for row in extracted:
            by_source[row["source_version_id"]].append(row)
        messages = pl.read_parquet(layout.message_version).filter(pl.col("channel_id") == channel["channel_id"]).to_dicts()
        from quant_lab.data.extract import SYMBOL_ALIASES, canonical_symbol
        symbol = symbol_of(req.order_plan.instrument_id).removesuffix("USDT")
        aliases = [alias for alias, code in SYMBOL_ALIASES.items() if code == symbol]
        mentions = re.compile(r"(?<![A-Za-z0-9])" + re.escape(symbol) + r"(?:USDT)?(?![A-Za-z0-9])", re.I)

        def same_symbol(message):
            parsing = by_source[message["source_version_id"]]
            if any(row.get("instrument_id") == req.order_plan.instrument_id or canonical_symbol(row.get("symbol_raw")) == symbol for row in parsing):
                return True
            text = message.get("text") or ""
            if mentions.search(text):
                return True
            return any(alias in text for alias in aliases if not alias.isascii())

        context_sources = {message["source_version_id"] for message in messages if same_symbol(message)}
        selected_follow = defaultdict(list)
        policy = resolve_policy(req.policy_version)
        for row in follow_rows:
            if row.get("channel_id", row.get("channel")) != channel["channel_id"]:
                continue
            clock = row.get("available_at")
            in_window = clock is not None and req.t_dec <= clock <= req.horizon_end
            target = canonical_symbol(row.get("target_symbol"))
            related = (row.get("episode_id") == req.episode_id or row.get("target_message_id") == episode["root_message_id"]
                       or (row.get("episode_id") is None and in_window
                           and (target == symbol or row.get("source_version_id") in context_sources)))
            if not related:
                continue
            reason = None
            if row.get("graph_version", req.graph_version) != req.graph_version:
                reason = "graph_version_mismatch"
            elif row.get("episode_ambiguity") not in (None, ""):
                reason = "episode_ambiguity"
            elif row.get("uncertain") is not False:
                reason = "uncertain"
            elif not policy.follow_teacher:
                reason = "policy_disabled"
            else:
                _, report = attach_management([req], [row], policy=policy, channel=channel["channel_id"], graph_version=req.graph_version)
                reason = next(iter(report["discard_reason_counts"]), None)
            execution = []
            for event in events:
                if event["kind"] == "management":
                    payload = json.loads(event["reason"])
                    if payload["source_message_id"] == row["message_id"] and payload["kind"] == row["action"]:
                        execution.append(payload["status"])
            selected_follow[row.get("source_version_id")].append({**row, "adopted": reason is None,
                "adoption": "采用" if reason is None else "未采用", "discard_reason": reason, "execution_statuses": execution})
        output = []
        for message in messages:
            source = message["source_version_id"]
            clock = message.get("available_at") or message.get("event_time") or message.get("message_date")
            in_window = clock is not None and req.t_dec <= clock <= req.horizon_end
            parsing = by_source[source]
            text = message.get("text")
            mention = source in context_sources
            if source not in linked_sources and source not in selected_follow and not (in_window and mention):
                continue
            output.append({"time": clock, "event_time": message.get("event_time"), "message_date": message.get("message_date"),
                           "available_at": message.get("available_at"), "source_version_id": source,
                           "message_id": (message.get("source_id") or {}).get("message_id"), "version_no": message.get("version_no"),
                           "reply_to": message.get("reply_to_message_id"), "text": text,
                           "role": "开仓" if source == episode["root_source_version_id"] else "后续指令" if source in selected_follow else "关联消息",
                           "parsed": parsing, "instructions": selected_follow[source],
                           "graph_events": [row for row in linked if row["source_version_id"] == source]})
        known = {row["source_version_id"] for row in output}
        for source, instructions in selected_follow.items():
            if source not in known:
                output.append({"time": instructions[0].get("available_at"), "source_version_id": source or "",
                               "text": None, "role": "后续指令（原文版本缺失）", "parsed": [], "instructions": instructions})
        return output

    def detail(self, key: str, variant: str, episode_id: str, interval: str | None = None) -> dict:
        if interval is not None and interval not in INTERVALS:
            raise LookupError("未知K线周期")
        channel, summary, trade, episode, req, audit, lake, follow_path, follow_rows = self.context(key, variant, episode_id)
        result = self.replay_one(key, variant, trade, channel, req, lake, follow_path)
        consistent = result["trace_hash"] == trade["trace_hash"]
        events = result["canonical_events"] if consistent else []
        # Cache dates are strings; normalize before sorting and deriving segmented price lines.
        for event in events:
            if isinstance(event["ts"], str):
                event["ts"] = dt.datetime.fromisoformat(event["ts"])
            qty = event.get("qty")
            filled_qty = result["filled_qty"]
            event["fraction_of_filled"] = float(Decimal(str(qty)) / Decimal(str(filled_qty))) if qty is not None and Decimal(str(filled_qty)) > 0 and event["kind"] in FILL_KINDS else None
        start, end = req.t_dec - dt.timedelta(days=1), req.horizon_end + dt.timedelta(days=1)
        interval = interval or automatic_interval(start, end)
        messages = self.message_timeline(channel, episode, req, events, follow_rows)
        plan = req.order_plan.model_dump()
        stop = plan.get("stop")
        moves = [event for event in events if event["kind"] == "amended" and event["leg"] == "sl" and event.get("price") is not None]
        segments, at, price = [], req.t_dec, stop["price"] if stop else None
        for move in moves:
            if price is not None and move["ts"] > at:
                segments.append({"start": at, "end": move["ts"], "price": price})
            at, price = move["ts"], move["price"]
        if price is not None:
            segments.append({"start": at, "end": req.horizon_end, "price": price})
        return {"channel": {"key": key, "name": channel["name"]}, "variant": variant, "episode_id": episode_id,
                "trade": trade, "plan": plan, "live_audit": audit, "horizon_end": req.horizon_end,
                "consistency": {"ok": consistent, "label": "复算一致" if consistent else "复算不一致",
                                "expected": trade["trace_hash"], "actual": result["trace_hash"]},
                "events": events, "timeline": mixed_timeline(messages, events), "stop_segments": segments,
                "bars": read_bars(lake, req.order_plan.instrument_id, start, end, interval)}
