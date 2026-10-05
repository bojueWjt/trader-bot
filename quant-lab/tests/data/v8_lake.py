"""v8 gold test lake: invented messages + recorded v2 actions through the real extract → validate → linker → lifecycle path.

Only fabricated text and numbers; nothing here comes from a real channel."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import polars as pl

from quant_lab.data import cx_batch as cx, extract, linker, lifecycle, normalize, validate
from quant_lab.data.lake import Layout
from quant_lab.data.llm import RecordedClient
from quant_lab.data.market_stub import SyntheticMarks, fixture_registry, instrument_id_for

T0 = datetime(2025, 3, 3, 8, tzinfo=UTC)
CHANNEL = -1002000000001
OTHER = -1002000000002
MARKS = {"BTC": 62050, "ETH": 3185, "SOL": 216.0}


def number(value, quote=None):
    return dict(value=str(value), quote=str(value) if quote is None else quote)


def open_action(symbol="BTC", side="short", *, entry=None, lo=None, hi=None, stop=None, tps=(), time_ref="now", stop_condition=None):
    if entry is None and lo is None:
        e = dict(kind="market_ref", price=None, lo=None, hi=None, levels=[])
    elif lo is not None:
        e = dict(kind="zone", price=None, lo=number(lo), hi=number(hi), levels=[])
    elif isinstance(entry, str) and entry.startswith("cmp:"):
        e = dict(kind="market_ref", price=number(entry[4:]), lo=None, hi=None, levels=[])
    else:
        e = dict(kind="limit", price=number(entry), lo=None, hi=None, levels=[])
    if stop_condition is not None:
        s = dict(kind="condition", price=None if stop is None else number(stop), condition=stop_condition)
    else:
        s = None if stop is None else dict(kind="price", price=number(stop), condition=None)
    return dict(op="open", time_ref=time_ref, symbol_raw=symbol, side=side, entry=e, stop=s,
                tps=[dict(kind="price", value=number(t)) for t in tps], field_issues=[])


def stop_action(stop, symbol=None, side=None):
    return dict(op="stop_move", time_ref="now", symbol_raw=symbol, side=side, entry=None,
                stop=dict(kind="price", price=number(stop), condition=None), tps=[], field_issues=[])


@dataclass
class Msg:
    mid: int
    text: str
    dt_s: float = 0
    actions: list = field(default_factory=list)
    reply: int | None = None
    author: str = "teacher"
    channel: int = CHANNEL
    media: bool = False
    grade: str = "H0"
    edit_delay_s: int | None = None  # H1 under --edit-visible-at-post
    edit_unknown: bool = False  # H1 whose edit time is missing or unusable (normalize writes edit_delay_unknown)


def mv_frame(messages: list[Msg]) -> pl.DataFrame:
    rows = []
    for i, m in enumerate(messages):
        at = T0 + timedelta(seconds=m.dt_s)
        ta = {"freeze_delay_s": 60, "clock": "message_date+freeze_delay"}
        event_time = at
        if m.grade == "H1":
            ta = {"freeze_delay_s": 60, "clock": "message_date+freeze_delay:edit_at_post", "edit_visible_at_post": True,
                  "edit_original_unavailable": True, "edit_delay_s": None if m.edit_unknown else (m.edit_delay_s or 0)}
            if m.edit_unknown:
                ta["edit_delay_unknown"] = True
        rows.append(dict(source_id=dict(peer_id=m.channel, message_id=m.mid), channel_id=m.channel, channel_name="仿写频道",
                         source_version_id=f"sv{m.channel % 10}-{m.mid}", text=m.text, message_type="message", message_date=at,
                         event_time=event_time, available_at=at, ingested_at=at, version_no=1, sequence=None,
                         reply_to_message_id=m.reply, media_kinds=["photo"] if m.media else [], media_hashes=[],
                         reason_codes=["EDIT_ORIGINAL_UNAVAILABLE"] if m.grade == "H1" else [], batch_id="tg-v8", content_hash=f"h{i}",
                         time_grade=m.grade, author_id=m.author, temporal_assumptions=json.dumps(ta)))
    return pl.DataFrame(rows, schema=normalize.MESSAGE_VERSION_SCHEMA)


def build(tmp_path, messages: list[Msg], *, marks=None, gv="v8-test", triage=None, **kw):
    layout = Layout.flat(tmp_path / "lake").ensure()
    mv = mv_frame(messages)
    mv.write_parquet(layout.message_version)
    prompts = tmp_path / "prompts.jsonl"
    cx.export_prompts(layout, prompts)
    by_svid = {f"sv{m.channel % 10}-{m.mid}": m for m in messages}
    recordings = {}
    for row in cx.read_jsonl(prompts):
        m = by_svid[row["source_version_id"]]
        recordings[row["key"]] = dict(response=dict(schema_version=2, actions=list(m.actions)))
    client = RecordedClient(recordings, version="cx-batch-v2")
    ex, _, _, _ = extract.extract_frame(mv, None, client=client, ingested_at=T0)
    ex.write_parquet(layout.extracted_event)
    anchors = {instrument_id_for(k): [(T0 - timedelta(days=30), v)] for k, v in (marks or MARKS).items()}
    cp, *_ = validate.validate_frame(ex, mv, registry=fixture_registry(), marks=SyntheticMarks(anchors), ingested_at=T0)
    cp.write_parquet(layout.canonical_plan)
    cb, jd, _ = linker.build_candidates(cp, mv, ex, plan_source="llm", ingested_at=T0)
    cb.write_parquet(layout.silver_dir / "candidate_edges.parquet")
    jd.write_parquet(layout.silver_dir / "adjudications.parquet")
    episodes, events, _, _, summary = lifecycle.build_graph(cp, mv, cb, jd, None, graph_version=gv, plan_source="llm", extracted_event=ex,
                                                            ingested_at=T0, triage=triage, **kw)
    return dict(layout=layout, mv=mv, ex=ex, cp=cp, cb=cb, jd=jd, episodes=episodes, events=events, summary=summary)


def episode(result, mid, channel=CHANNEL):
    frame = result["episodes"].filter((pl.col("root_message_id") == mid) & (pl.col("channel_id") == channel))
    assert frame.height == 1, (mid, frame.height)
    return frame.row(0, named=True)


def llm_row(result, mid, channel=CHANNEL):
    """The single LLM canonical plan of one message (the rule parser also writes rows)."""
    frame = result["cp"].filter((pl.col("message_id") == mid) & (pl.col("channel_id") == channel) & (pl.col("extractor_name") == "llm"))
    assert frame.height == 1, (mid, frame.height)
    return frame.row(0, named=True)


#: D's frozen sidecar columns (cx_triage._sidecar_schema in the D group), copied here because B reads the sidecar by
#: schema and never imports cx_triage.
D_SIDECAR_SCHEMA = {
    "source_version_id": pl.String, "branch_index": pl.Int32, "channel_id": pl.Int64, "message_id": pl.Int64,
    "source_plan_id": pl.String, "source_episode_id": pl.String, "selected_as": pl.List(pl.String),
    "verdict": pl.String, "reason": pl.String, "venue_hint": pl.String, "relation": pl.String,
    "relation_target_message_id": pl.Int64, "evidence_quote": pl.String, "evidence_start": pl.Int64, "evidence_end": pl.Int64,
    "status": pl.String, "status_note": pl.String, "invalid_reasons": pl.List(pl.String), "model_verdict": pl.String,
    "rule_code": pl.String, "rule_start": pl.Int64, "rule_end": pl.Int64, "exclusion_code": pl.String,
    "prompt_key": pl.String, "response_hash": pl.String, "recording_version": pl.String, "recording_sha256": pl.String,
    "triage_schema": pl.String, "triage_rules_version": pl.String, "source_graph_version": pl.String,
    "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
}


def d_sidecar_row(svid, status, verdict=None, **extra):
    """One row as D writes it: ok rows carry the model verdict; invalid rows verdict=uncertain; missing rows verdict=None
    (D cx_triage._empty_row); exclusion_code follows D's exclusion_code(status, verdict)."""
    code = ("TRIAGE_MISSING" if status == "missing" else "TRIAGE_INVALID" if status == "invalid"
            else {"new_entry": None, "not_entry": "TRIAGE_NOT_ENTRY"}.get(verdict, "TRIAGE_UNCERTAIN"))
    row = {k: None for k in D_SIDECAR_SCHEMA}
    row.update(source_version_id=svid, branch_index=0, channel_id=CHANNEL, selected_as=["no_stop"], verdict=verdict,
               venue_hint="unspecified", relation="none", status=status, status_note=None if status == "ok" else "no_recording",
               invalid_reasons=[], exclusion_code=code, prompt_key="k", recording_version="triage-rec-v1", triage_schema="cx.triage.v1",
               available_at=T0, ingested_at=T0)
    row.update(extra)
    return row


def d_sidecar(rows):
    return pl.DataFrame(rows, schema=D_SIDECAR_SCHEMA)


def triage_frame(rows):
    """Frozen F3 sidecar schema (svid, branch_index) + verdict columns."""
    base = {"source_version_id": pl.String, "branch_index": pl.Int32, "verdict": pl.String, "reason": pl.String, "venue_hint": pl.String,
            "relation": pl.String, "relation_target_message_id": pl.Int64, "evidence_quote": pl.String, "prompt_key": pl.String,
            "recording_version": pl.String, "triage_schema": pl.String}
    full = [{**{k: None for k in base}, "prompt_key": "k", "recording_version": "triage-rec-v1", "triage_schema": "cx.triage.v1", "branch_index": 0, **r} for r in rows]
    return pl.DataFrame(full, schema=base)
