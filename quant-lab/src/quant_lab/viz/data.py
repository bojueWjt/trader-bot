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
from quant_lab.data.graph import resolve_alias, stale_episodes
from quant_lab.data.lake import Layout
from quant_lab.data.market_lake import LakeMarket
from quant_lab.market.contract import FILL_KINDS, resolve_policy
from quant_lab.market.execution import BATCH_SCHEMA, load_market_from_lake, simulate_batch
from quant_lab.market.l0_replay import attach_management, load_followup_actions, prepare_episode_request
from quant_lab.market.partition_check import rules_path
from quant_lab.market.vision import LakePaths, partition_id, symbol_of

VARIANTS = {"": "5天", "-be1": "保本", "-w14": "14天", "-w14-be1": "14天+保本", "-live": "让点",
            "-be1-live": "让点+保本", "-follow": "跟指令", "-live-follow": "让点+跟指令",
            "-w60": "60天", "-w60-be1": "60天+保本"}
#: v8 runbook §8 第 7 步的目录（l0-v8<suffix>）。主口径放第一位（频道页默认口径）。无连字符的后缀是变体图
#: （<ch>-v8e/-v8w/-v8nw，与主图同一个数据根），只按这里列出的精确名发现，不把任意 l0-v8xxx 目录当口径。
TAG_VARIANTS = {
    "v8": {"-w60lf-ns300": "主口径（60天·让点·跟指令·无止损300U）", "-w1-ns300": "无止损1天档", "-5d-ns300": "无止损5天档",
           "-w14-ns300": "无止损14天档", "e": "编辑敏感性 v8e", "w": "宽口径 v8w", "nw": "不等待止损 v8nw"},
}
#: 不带连字符、但按前缀发现的目录（都用主图）：runbook §8 第 7 步 C 批次 `l0-v8cmp*`（与 v7 对照，B=100）。
TAG_BARE_PREFIXES = {"v8": ("cmp",)}
INTERVALS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
UTC = dt.timezone.utc
SIZING_RISK, SIZING_NOSTOP = "risk", "nostop"
#: L0 trades 的 v8 列（G2 产生或从 G1 透传）；旧报告没有这些列时为 None。
V8_TRADE_FIELDS = ("sizing_basis", "nostop_notional_U", "legs_n", "legs_gt3", "mae_U", "mae_pct_notional", "entry_notional_U",
                   "mtm_U_at_censor", "second_pass_decision", "nostop_kind", "venue_hint", "triage_verdict", "plan_link_kind",
                   "family_id", "stop_rule", "time_ref_promoted", "promotion_scope", "signal_age_s", "edit_delay_s",
                   "edit_may_contain_outcome")
#: G1 v8 episode 列（方案 §4），详情页按「同一计划合并」展示；图里没有的列不出现。
V8_EPISODE_FIELDS = ("plan_group_id", "family_id", "dup_of", "plan_link_kind", "repost_of", "amend_of", "reentry_of",
                     "reentry_parent_stop", "stop_rule", "stop_base", "stop_source_version_id", "nostop_kind", "venue_hint",
                     "triage_verdict", "triage_reason", "promotion_scope", "signal_age_s", "edit_delay_s", "signal_anchor",
                     "edit_original_unavailable", "edit_may_contain_outcome", "entry_legs_n", "time_ref_promoted")
NOSTOP_NOTE = "无止损仅内核 A：按固定名义定量（每腿 k×B）；R 只是金额等价（net_U / B），不与有止损单的 R 混算"


# Everything the kernel reports about the trade except its identity stamps.
RESULT_FIELDS = tuple(key for key in BATCH_SCHEMA if key not in ("trace_hash", "kernel_version", "canonical_events"))


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


def sizing_of(row: dict) -> str:
    return row.get("sizing_basis") or SIZING_RISK      # v8 之前的报告没有该列，全部按风险定量


def net_U(row: dict) -> Decimal | None:
    """Money result: net_R × risk_budget (for a stopless row net_R is net_U / B). None when the budget is unknown."""
    budget = row.get("risk_budget")
    return None if budget is None or row.get("net_R") is None else Decimal(str(row["net_R"])) * Decimal(str(budget))


def _clustered(samples: list[dict], value) -> dict:
    """Per-trade point estimates; 95% interval over UTC t_dec day means (mean ± 1.96·sd/√days)."""
    days = defaultdict(list)
    curve, total = [], Decimal(0)
    for row in samples:
        number = value(row)
        days[row["t_dec"].date()].append(float(number))
        total += number
        curve.append({"episode_id": row["episode_id"], "t_dec": row["t_dec"], "cumulative": float(total)})
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
    return {"n": len(samples), "n_days": len(days),
            "win_rate": sum(value(r) > 0 for r in samples) / len(samples) if samples else None,
            "mean": float(total / len(samples)) if samples else None, "ci95": interval, "sum": float(total),
            "conclusion": conclusion, "curve": curve}


def statistics_for(rows: list[dict]) -> dict:
    """R statistics over risk-sized rows only; stopless rows are a separate money (U) block; the total is money only.

    v8 F1/§11-7: a stopless trade's R is a money equivalent, never mixed into mean R, win rate or the R curve.
    """
    ordered = sorted((r for r in rows if evaluable(r)), key=lambda r: (r["t_dec"], r["episode_id"]))
    risk = [r for r in ordered if sizing_of(r) == SIZING_RISK]
    nostop = [r for r in ordered if sizing_of(r) == SIZING_NOSTOP]
    stats = _clustered(risk, lambda r: Decimal(str(r["net_R"])))
    result = {"n": stats["n"], "n_days": stats["n_days"], "win_rate": stats["win_rate"], "mean_R": stats["mean"],
              "ci95": stats["ci95"], "sum_R": stats["sum"], "conclusion": stats["conclusion"],
              "cumulative_R": [{"episode_id": p["episode_id"], "t_dec": p["t_dec"], "cumulative_R": p["cumulative"]}
                               for p in stats["curve"]]}
    if not any(sizing_of(r) == SIZING_NOSTOP for r in rows):
        return result          # 旧报告（全部按风险定量）：输出与 v8 之前相同
    # 没有 risk_budget 的行不能换成金额：整块为空，而不是当成 0（无法比较 ≠ 相等）。
    unknown = any(net_U(r) is None for r in ordered)
    money = None if unknown else _clustered(nostop, net_U)
    result["blocks"] = {
        "nostop": ({"n": len(nostop), "status": "risk_budget 缺失"} if money is None else
                   {"n": money["n"], "n_days": money["n_days"], "win_rate": money["win_rate"], "mean_U": money["mean"],
                    "ci95": money["ci95"], "sum_U": money["sum"], "conclusion": money["conclusion"]}),
        "total": {"sum_net_U": None if unknown else float(sum((net_U(r) for r in ordered), Decimal(0))),
                  "note": "合计只有金额：不出合计均值与胜率"},
        "note": NOSTOP_NOTE,
    }
    return result


DUP_MEMBER_COLUMNS = ("episode_id", "root_message_id", "root_source_version_id", "t_dec", "plan_link_kind", "dup_of")


def scan_episodes(layout: Layout, graph_version: str) -> pl.LazyFrame | None:
    """Lazy description episode table of one immutable graph, stale episodes removed; None when not published.

    Only for the dup_of lookups below: a filtered scan instead of loading the whole graph per request."""
    path = layout.episode(graph_version)
    if not path.is_file():
        return None
    frame = pl.scan_parquet(path)
    stale = stale_episodes(layout, graph_version)
    return frame.filter(~pl.col("episode_id").is_in(sorted(stale))) if stale else frame


def dup_counts(layout: Layout, graph_version: str, channel_id: int) -> dict[str, int] | None:
    """kept episode → how many episodes this graph merged into it; None when the graph has no dup_of (pre-v8)."""
    frame = scan_episodes(layout, graph_version)
    if frame is None or "dup_of" not in frame.collect_schema().names():
        return None
    merged = frame.filter((pl.col("channel_id") == channel_id) & pl.col("dup_of").is_not_null() & (pl.col("dup_of") != ""))
    return dict(merged.group_by("dup_of").agg(pl.len()).collect().iter_rows())


def dup_members_of(layout: Layout, graph_version: str, channel_id: int, episode_id: str) -> list[dict]:
    """The episodes merged into one kept episode of this graph (filtered scan; [] for pre-v8 graphs)."""
    frame = scan_episodes(layout, graph_version)
    if frame is None:
        return []
    names = frame.collect_schema().names()
    if "dup_of" not in names:
        return []
    members = frame.filter((pl.col("channel_id") == channel_id) & (pl.col("dup_of") == episode_id)).select(
        [c for c in DUP_MEMBER_COLUMNS if c in names]).collect()
    return dup_members(members).get(episode_id, [])


def variant_graph_names(channel: dict, variant: str) -> list[str]:
    """Graph names a variant-graph report (v8e/v8w/v8nw) may use: graph_aliases as {variant: name | [names]},
    or a list matched by its '-<variant>' suffix ("<ch>-v8e" → v8e)."""
    aliases = channel.get("graph_aliases") or []
    if isinstance(aliases, dict):
        value = aliases.get(variant, [])
        return [value] if isinstance(value, str) else list(value)
    return [name for name in aliases if name.endswith(f"-{variant}")]


def dup_members(episodes: pl.DataFrame | None) -> dict[str, list[dict]]:
    """kept episode → the episodes G1 merged into it (v8 dup_of); empty for graphs built before v8."""
    if episodes is None or "dup_of" not in episodes.columns:
        return {}
    columns = [c for c in DUP_MEMBER_COLUMNS if c in episodes.columns]
    out = defaultdict(list)
    for row in episodes.filter(pl.col("dup_of").is_not_null() & (pl.col("dup_of") != "")).select(columns).iter_rows(named=True):
        out[row["dup_of"]].append(row)
    # 按决策时刻排序，时刻未知（伴随帖并入后不单独决策）的置末。
    return {key: sorted(rows, key=lambda r: (r.get("t_dec") is None, r.get("t_dec") or dt.datetime.min.replace(tzinfo=UTC),
                                             r["episode_id"])) for key, rows in out.items()}


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
        builtin = TAG_VARIANTS.get(self.tag, VARIANTS)
        bare = TAG_BARE_PREFIXES.get(self.tag, ())
        suffixes = dict(builtin)
        prefix = f"l0-{self.tag}"
        if self.reports.is_dir():
            for directory in sorted(self.reports.iterdir()):
                if directory.is_dir() and directory.name.startswith(prefix):
                    suffix = directory.name[len(prefix):]
                    if (suffix.startswith("-") or suffix.startswith(bare)) and SAFE_KEY.fullmatch(suffix):
                        suffixes.setdefault(suffix, suffix)
        variants = {}
        for suffix, name in suffixes.items():
            # 变体图：内置表里不带连字符的目录（l0-v8e/-v8w/-v8nw），报告必须在该变体自己的图上。
            graph = "variant" if suffix and not suffix.startswith("-") and suffix in builtin else "main"
            if suffix and not suffix.startswith("-"):
                key = f"{self.tag}{suffix}"      # l0-v8e → 口径 v8e；l0-v8cmp-x → v8cmp-x
            else:
                key = "base" if suffix == "" else suffix[1:]
            if suffix and key == "base":
                key = "suffix:-base"  # Preserve an unknown -base without shadowing the default.
            variants[key] = {"suffix": suffix, "name": name, "graph": graph}
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
            # v8 summaries count only risk-sized rows in overall; stopless rows are in blocks.nostop.
            expected = summary["overall"]["n_trades"] + (summary["blocks"]["nostop"]["n_trades"] if "blocks" in summary else 0)
            if before != after or before[0][0] < before[1][0] or expected != len(rows):
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
        counts_by_graph = {}
        for variant in variants:
            summary, rows = self.report(key, variant)
            statuses[variant] = "已出" if summary is not None else "未出"
            # v8 列只在该报告有这些列时写入（v7 频道页不带一串空字段）。
            columns = set(rows[0]) if rows else set()
            v8_fields = [field for field in V8_TRADE_FIELDS if field in columns]
            counts = None
            if summary is not None:
                # 合并数按该口径报告自己的图（v8e/v8w/v8nw 的合并与主图不同）。
                gv = summary["graph_version"]
                if gv not in counts_by_graph:
                    counts_by_graph[gv] = dup_counts(channel["layout"], gv, channel["channel_id"])
                counts = counts_by_graph[gv]
            for row in rows:
                eid = row["episode_id"]
                if eid not in collected:
                    collected[eid] = {field: row.get(field) for field in ("episode_id", "t_dec", "instrument", "side", "entries", "stop", "targets")}
                    collected[eid]["variants"] = {}
                record = {field: row.get(field) for field in (
                    "net_R", "fill_status", "outcome_kind", "n_teacher_actions_executed", "censor_reason", "trace_hash")}
                record.update({field: row.get(field) for field in v8_fields})
                if "sizing_basis" in columns:
                    record["sizing_basis"] = sizing_of(row)
                    record["net_U"] = net_U(row)
                if counts is not None:
                    record["n_dup_members"] = counts.get(eid, 0)
                collected[eid]["variants"][variant] = record
        episodes = self.graph_episodes(channel)
        return {"key": key, "name": channel["name"], "variants": variants, "statuses": statuses,
                "trades": sorted(collected.values(), key=lambda r: (r["t_dec"], r["episode_id"])),
                "teacher_episode_ids": self.teacher_episode_ids(channel, episodes), "nostop_note": NOSTOP_NOTE}

    def graph_episodes(self, channel: dict) -> pl.DataFrame | None:
        """The configured graph's full (description) episode table for this channel; None when not published."""
        layout = channel["layout"]
        gv = resolve_alias(layout, channel["graph_version"])
        if not layout.episode(gv).is_file():
            return None
        return load_episodes(gv, decision_graph=False, layout=layout).filter(pl.col("channel_id") == channel["channel_id"])

    def teacher_episode_ids(self, channel: dict, episodes: pl.DataFrame | None = None) -> list[str]:
        paths = {channel["layout"].silver_dir / "followup_action.parquet"}
        for variant in self.variants():
            summary, _ = self.report(channel["key"], variant)
            if summary and summary.get("follow_teacher", {}).get("path"):
                paths.add(Path(summary["follow_teacher"]["path"]).expanduser())
        ids = set()
        roots = defaultdict(list)
        if episodes is None:
            episodes = self.graph_episodes(channel)
        if episodes is not None:
            for episode in episodes.iter_rows(named=True):
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
        # v8 变体图（v8e/v8w/v8nw）与主图在同一个数据根里：变体口径只认 graph_aliases 里它自己的图，
        # 其余口径（主口径、持有期档、S/C 批次）只认频道配置的主图，放错目录的变体报告不会被当成主口径。
        if self.variants()[variant]["graph"] == "variant":
            names = variant_graph_names(channel, variant)
        else:
            names = [channel["graph_version"]]
        allowed = {resolve_alias(layout, name) for name in names}
        gv = summary["graph_version"]
        if gv not in allowed:
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
        # 同回放（l0_replay.replay）：止损可能在另一条消息里（补充止损），按 stop_source_version_id 另读原文传给 live v4。
        stop_source = episode.get("stop_source_version_id")
        texts = load_message_texts([episode["root_source_version_id"], stop_source], layout=layout)
        text = texts.get(episode["root_source_version_id"])
        req, audit, why, _, _ = prepare_episode_request(episode, marks=LakeMarket(lake), lake=lake,
            policy=policy, risk_budget=Decimal(summary["risk_budget"]), text=text, stop_text=texts.get(stop_source))
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
        # Cache revalidation covers this request's market inputs only: the daily partitions and monthly
        # manifests the loader reads (window padded on both sides) and the instrument rules. Walking the
        # whole lake (every symbol and day on the external disk) made one detail view take many minutes.
        lake_paths, symbol = LakePaths(lake), symbol_of(req.order_plan.instrument_id)
        first, last = (req.t_dec - dt.timedelta(days=2)).date(), (req.horizon_end + dt.timedelta(days=1)).date()
        days = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
        months = sorted({day.strftime("%Y-%m") for day in days})
        for kind, interval in (("klines", "1m"), ("markPriceKlines", "1m"), ("fundingRate", "8h")):
            folder = lake_paths.silver_dir(kind, interval, symbol)
            paths.update(folder / f"date={day.isoformat()}" / "part.parquet" for day in days)
            paths.update(lake_paths.manifest(partition_id(kind, interval, symbol, month)) for month in months)
        paths.add(rules_path(lake_paths, req.order_plan.instrument_id))
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
        same_trace = result["trace_hash"] == trade["trace_hash"]
        # A newer kernel stamps a different trace hash even when nothing about the trade changed (v0.5→v0.6
        # added management actions, byte-identical when none apply). Accept that only if every result field
        # matches and the kernel version is what differs; anything else stays a visible mismatch.
        same_result = (json.loads(dumps({k: trade.get(k) for k in RESULT_FIELDS}))
                       == json.loads(dumps({k: result.get(k) for k in RESULT_FIELDS})))
        consistent = same_trace or (same_result and result.get("kernel_version") != trade.get("kernel_version"))
        label = "复算一致" if same_trace else "结果一致（内核版本不同）" if consistent else "复算不一致"
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
        sizing = {field: trade.get(field) for field in V8_TRADE_FIELDS if field in trade}
        sizing.update({"sizing_basis": sizing_of(trade), "net_U": net_U(trade)})
        if sizing["sizing_basis"] == SIZING_NOSTOP:
            sizing["note"] = NOSTOP_NOTE
        return {"channel": {"key": key, "name": channel["name"]}, "variant": variant, "episode_id": episode_id,
                "trade": trade, "plan": plan, "live_audit": audit, "horizon_end": req.horizon_end,
                "sizing": sizing,
                "plan_link": {field: episode[field] for field in V8_EPISODE_FIELDS if field in episode},
                "dup_members": dup_members_of(channel["layout"], req.graph_version, channel["channel_id"], episode_id),
                "consistency": {"ok": consistent, "label": label,
                                "expected": trade["trace_hash"], "actual": result["trace_hash"],
                                "expected_kernel": trade.get("kernel_version"), "actual_kernel": result.get("kernel_version")},
                "events": events, "timeline": mixed_timeline(messages, events), "stop_segments": segments,
                "bars": read_bars(lake, req.order_plan.instrument_id, start, end, interval)}
