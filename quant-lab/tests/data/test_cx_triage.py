"""v8 F3 cx.triage.v1 side pass: selection, as-of context, deterministic checks and the silver sidecar.

All messages, prices and ids are made up. The lake is built from the frozen schemas (message_version,
canonical_plan, extracted_event, episode + the §4 columns plan_link_kind/dup_of, plan_link) without running the pipeline.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import sys

import polars as pl
import pytest

from quant_lab.data import cx_batch as cx, cx_triage as tr, extract, lifecycle, normalize, validate
from quant_lab.data.lake import Layout, stable_id
from quant_lab.data.llm import record_key

CH = -1009999000001
T0 = datetime(2024, 7, 1, 12, 0, tzinfo=UTC)
GV = "fake-v8a"
EPISODE_EXTRA = {"plan_link_kind": pl.String, "dup_of": pl.String}
LINK_SCHEMA = {"channel_id": pl.Int64, "message_id": pl.Int64, "source_version_id": pl.String, "branch_index": pl.Int32,
               "episode_id": pl.String, "kept_episode_id": pl.String, "plan_link_kind": pl.String, "gap_s": pl.Float64}


def at(minutes=0.0, seconds=0.0):
    return T0 + timedelta(minutes=minutes, seconds=seconds)


class Lake:
    def __init__(self, tmp_path):
        self.layout = Layout.flat(tmp_path / "lake").ensure()
        self.messages, self.plans, self.episodes, self.links, self.events = [], [], [], [], []

    def msg(self, mid, when, text, *, grade="V", reply=None, edit_delay_s=None, channel=CH, kinds=(), svid=None, visible=None):
        last_edit = when + timedelta(seconds=edit_delay_s) if edit_delay_s is not None else None
        self.messages.append(dict(source_id=dict(peer_id=channel, message_id=mid), channel_id=channel, channel_name="仿写频道",
                                  message_type="message", source_version_id=svid or f"m{mid}", version_no=1, sequence=mid, content_hash=f"h{mid}",
                                  text=text, media_kinds=list(kinds), media_hashes=[], reply_to_message_id=reply, message_date=when,
                                  last_edit_at=last_edit, time_grade=grade, event_time=when, available_at=when if visible is None else visible, ingested_at=when,
                                  reason_codes=[], batch_id="tg-fake"))
        return f"m{mid}"

    def plan(self, mid, branch=0, *, symbol="BTC", side="long", entry=("limit", "60000", "60000"), entries=None, stop=None,
             tps=(), kind="entry_proposal", checks=None, channel=CH, mode="price"):
        lo, hi = (Decimal(entry[1]), Decimal(entry[2])) if entry else (None, None)
        row = dict(plan_id=f"p{mid}-{branch}", branch_index=branch, extract_id=f"x{mid}-{branch}", source_version_id=f"m{mid}",
                   channel_id=channel, message_id=mid, extractor_name="llm", kind=kind, symbol_raw=symbol, instrument_id=f"{symbol}USDT",
                   side=side, entry=dict(kind=entry[0], lo=lo, hi=hi) if entry else None,
                   entries=[Decimal(x) for x in entries] if entries else [], stop=Decimal(stop) if stop else None,
                   tps=[dict(level=Decimal(t), fraction=None, kind="price") for t in tps], entry_mode=mode,
                   checks=json.dumps(checks or {"schema_version": 2, "time_ref": "now"}, ensure_ascii=False), reason_codes=[],
                   available_at=at(), event_time=at())
        self.plans.append(row)
        return row

    def episode(self, plan, *, link_kind="root", dup_of=None, orphan=False, **extra):
        mid, branch = plan["message_id"], plan["branch_index"]
        self.episodes.append(dict(episode_id=f"e{mid}-{branch}", graph_version=GV, channel_id=plan["channel_id"], root_plan_id=plan["plan_id"],
                                  root_source_version_id=plan["source_version_id"], root_message_id=mid, left_truncated=orphan,
                                  plan_link_kind=link_kind, dup_of=dup_of, reason_codes=[], **extra))
        return f"e{mid}-{branch}"

    def root(self, mid, when, text, **kw):
        grade, edit = kw.pop("grade", "V"), kw.pop("edit_delay_s", None)
        link_kind, dup_of, extra = kw.pop("link_kind", "root"), kw.pop("dup_of", None), kw.pop("episode", {})
        self.msg(mid, when, text, grade=grade, edit_delay_s=edit, reply=kw.pop("reply", None), visible=kw.pop("visible", None))
        return self.episode(self.plan(mid, **kw), link_kind=link_kind, dup_of=dup_of, **extra)

    def event(self, mid, branch, action):
        """An extracted_event row carrying the v2 action (with its text spans), as extract writes it."""
        self.events.append(dict(extract_id=f"x{mid}-{branch}", source_version_id=f"m{mid}", channel_id=CH, message_id=mid, version_no=1,
                                branch_index=branch, checks=json.dumps({"schema_version": 2, "time_ref": "now", "action": action}, ensure_ascii=False),
                                kind="entry_proposal", symbol_raw=action.get("symbol_raw"), spans=action.get("spans"), reason_codes=[],
                                event_time=at(), available_at=at(), ingested_at=at()))

    def link(self, mid, branch, episode, kept, kind, gap_s):
        self.links.append(dict(channel_id=CH, message_id=mid, source_version_id=f"m{mid}", branch_index=branch, episode_id=episode,
                               kept_episode_id=kept, plan_link_kind=kind, gap_s=gap_s))

    def write(self):
        pl.DataFrame(self.messages, schema=normalize.MESSAGE_VERSION_SCHEMA).write_parquet(self.layout.message_version)
        pl.DataFrame(self.plans, schema=validate.CANONICAL_PLAN_SCHEMA).write_parquet(self.layout.canonical_plan)
        pl.DataFrame(self.episodes, schema={**lifecycle.EPISODE_SCHEMA, **EPISODE_EXTRA}).write_parquet(self.layout.episode(GV))
        if self.events:
            pl.DataFrame(self.events, schema=extract.EXTRACTED_EVENT_SCHEMA).write_parquet(self.layout.extracted_event)
        if self.links:
            pl.DataFrame(self.links, schema=LINK_SCHEMA).write_parquet(self.layout.gold_dir / f"plan_link__{GV}.parquet")
        return self.layout


def prompts(layout):
    rows, meta, _, counts = tr.plan_prompts(layout, GV)
    return {r["source_version_id"]: r for r in rows}, meta, counts


def user(row):
    return json.loads(row["user"])


def answer(branch_index=0, **changes):
    base = dict(branch_index=branch_index, verdict="new_entry", reason="other", venue_hint="unspecified", relation="none",
                relation_target_message_id=None, evidence_quote="BTC 60000 多")
    return {**base, **changes}


def item(*branches):
    return dict(schema_version=tr.SCHEMA_NAME, branches=list(branches))


def recording(path, items, *, version=tr.IMPORT_VERSION):
    path.write_text(json.dumps(dict(version=version, model="fake", items=items), ensure_ascii=False))
    return path


def sidecar(tmp_path, layout, items):
    rec = recording(tmp_path / "triage.json", items)
    report = tr.build_sidecar(layout, GV, rec, tmp_path / "nostop_triage.parquet", ingested_at=T0)
    frame = pl.read_parquet(tmp_path / "nostop_triage.parquet")
    return {(r["source_version_id"], r["branch_index"]): r for r in frame.to_dicts()}, report


CONTEXT = {"branch_indexes": [0], "candidate_message_ids": [7]}
TEXT = "仿写 BTC 60000 多，看反弹"


# ---------------------------------------------------------------- 1, 2: deterministic answer checks
def test_quote_outside_the_text_makes_the_branch_invalid_uncertain():
    good = tr.validate_response(item(answer()), TEXT, CONTEXT)["response"]["checked"][0]
    assert (good["status"], good["verdict"], good["evidence_span"]) == ("ok", "new_entry", [3, 14])
    for quote in ("BTC 60000 多空", "", "   ", None):
        bad = tr.validate_response(item(answer(evidence_quote=quote)), TEXT, CONTEXT)["response"]["checked"][0]
        assert (bad["status"], bad["verdict"], bad["model_verdict"]) == ("invalid", "uncertain", "new_entry")
        assert bad["invalid_reasons"] and bad["evidence_quote"] is None


def test_relation_target_must_be_a_candidate_and_enums_must_be_legal():
    def check(**changes):
        return tr.validate_response(item(answer(**changes)), TEXT, CONTEXT)["response"]["checked"][0]
    assert check(relation="restates", relation_target_message_id=7)["status"] == "ok"
    assert check(relation="restates", relation_target_message_id=8)["invalid_reasons"] == ["relation_target_not_candidate"]
    assert check(relation="amends", relation_target_message_id=None)["invalid_reasons"] == ["relation_target_not_candidate"]
    assert check(relation="restates", relation_target_message_id=True)["status"] == "invalid"
    assert check(relation="none", relation_target_message_id=7)["invalid_reasons"] == ["relation_target_without_relation"]
    assert check(verdict="maybe")["invalid_reasons"] == ["verdict_not_in_enum"]
    assert check(reason="spam")["invalid_reasons"] == ["reason_not_in_enum"]
    assert check(venue_hint="margin")["invalid_reasons"] == ["venue_hint_not_in_enum"]
    assert check(relation="copies", relation_target_message_id=7)["invalid_reasons"] == ["relation_not_in_enum"]
    invalid = check(relation="restates", relation_target_message_id=8)
    assert (invalid["relation"], invalid["relation_target_message_id"], invalid["verdict"]) == ("none", None, "uncertain")


def test_envelope_unanswered_duplicate_and_unknown_branches():
    assert tr.validate_response(dict(branches=[]), TEXT, CONTEXT)["abstain"]["note"] == "invalid_triage_envelope"
    assert tr.validate_response(item(answer()), TEXT, None)["abstain"]["note"] == "missing_triage_context"
    two = {"branch_indexes": [0, 1], "candidate_message_ids": []}
    checked = tr.validate_response(item(answer(0), answer(5)), TEXT, two)["response"]
    assert [c["invalid_reasons"] for c in checked["checked"]] == [[], ["branch_not_answered"]]
    assert checked["rejected"] == [dict(branch_index=5, reason="not_a_selected_branch")]
    twice = tr.validate_response(item(answer(0), answer(0, verdict="not_entry")), TEXT, CONTEXT)["response"]["checked"][0]
    assert (twice["status"], twice["invalid_reasons"]) == ("invalid", ["branch_answered_twice"])


def test_a_validated_response_revalidates_to_itself():
    first = tr.validate_response(item(answer(), answer(1, evidence_quote="不在原文")), TEXT, {"branch_indexes": [0, 1], "candidate_message_ids": []})
    again = tr.validate_response(json.loads(json.dumps(first["response"])), TEXT, {"branch_indexes": [0, 1], "candidate_message_ids": []})
    assert again == first


# ---------------------------------------------------------------- 3: as-of context
def test_context_is_cut_strictly_before_the_root_and_candidates_show_their_own_fields(tmp_path):
    lake = Lake(tmp_path)
    for i in range(5):
        lake.msg(100 + i, at(-50 + i), f"仿写闲聊{i} " + "长" * 500)
    # A candidate whose stop arrives after the root: the graph merged it (dec_stop), the prompt shows the stop as then unknown.
    lake.root(1, at(-30), "仿写 BTC 61000 多 先看看", entry=("limit", "61000", "61000"), episode=dict(dec_stop=Decimal("60500")))
    lake.msg(9, at(10), "仿写 止损 60500", reply=1)
    lake.root(2, at(-73 * 60), "仿写 BTC 50000 多 止损 49000", stop="49000")                    # outside 72 h
    lake.root(3, at(-71 * 60), "仿写 ETH 3000 空 止损 3100", symbol="ETH", side="short", entry=("zone", "2990", "3010"), stop="3100")
    lake.root(4, at(-10), "仿写 SOL 150 多", symbol="SOL", entry=("limit", "150", "150"))
    lake.root(10, at(0), "仿写 BTC 60000 多，看反弹")
    lake.msg(10, at(-1), "仿写 本条消息的旧版本", svid="m10-old")                                # the root's own message is not context
    lake.root(11, at(0), "仿写 BTC 60100 多 同一时刻")                                           # same t_vis: not visible
    lake.root(12, at(0, 1), "仿写 BTC 60200 多 晚一秒")                                          # 1 s later
    lake.msg(13, at(0, 1), "仿写 晚一秒的闲聊")
    rows, _, _ = prompts(lake.write())
    doc = user(rows["m10"])
    assert doc["text"] == "仿写 BTC 60000 多，看反弹" and doc["schema_version"] == tr.SCHEMA_NAME and doc["reply_text"] is None
    blob = json.dumps(doc, ensure_ascii=False)
    for later in ("晚一秒", "同一时刻", "60500", "旧版本"):
        assert later not in blob
    assert [p["message_id"] for p in doc["previous"]] == [104, 1, 4]
    assert all(len(p["text"]) <= tr.PREVIOUS_CHARS for p in doc["previous"]) and doc["previous"][0]["minutes_before"] == 46
    cands = doc["candidates"]
    assert [c["message_id"] for c in cands] == [1, 4, 3]  # same coin first, then newest; 73 h old and same-time roots are out
    assert cands[0] == dict(message_id=1, symbol="BTC", side="long", entry_summary="limit 61000", stop=None, minutes_before=30)
    assert cands[2]["entry_summary"] == "zone 2990-3010" and cands[2]["stop"] == "3100" and cands[2]["minutes_before"] == 71 * 60
    assert doc["branches"] == [dict(branch_index=0, symbol="BTC", side="long", entry_summary="limit 60000", tps=[])]


def test_reply_text_follows_the_original_time_rule(tmp_path):
    lake = Lake(tmp_path)
    lake.msg(1, at(-5), "仿写 父消息：准备看多比特币")
    lake.root(2, at(0), "仿写 BTC 60000 多", reply=1)
    lake.msg(3, at(-4), "仿写 H1 父消息", grade="H1")
    lake.root(4, at(1), "仿写 BTC 60300 多", reply=3)
    lake.root(5, at(2), "仿写 BTC 60400 多", reply=1, grade="H1")
    rows, _, _ = prompts(lake.write())
    assert user(rows["m2"])["reply_text"] == "仿写 父消息：准备看多比特币"
    assert user(rows["m4"])["reply_text"] is None and user(rows["m5"])["reply_text"] is None


# ---------------------------------------------------------------- 4: branches are independent
def test_two_branches_of_one_message_get_independent_results(tmp_path):
    lake = Lake(tmp_path)
    text = "仿写 BTC 60000 多\n仿写 BTC 58000 再补一单"
    lake.msg(1, at(0), text)
    lake.episode(lake.plan(1, 0))
    lake.episode(lake.plan(1, 1, entry=("limit", "58000", "58000")))
    rows, _, _ = prompts(lake.write())
    row = rows["m1"]
    assert row["branch_indexes"] == [0, 1] and len(rows) == 1
    got = item(answer(0, evidence_quote="BTC 60000 多"), answer(1, verdict="not_entry", reason="position_update", evidence_quote="不在原文"))
    side, _ = sidecar(tmp_path, lake.layout, {row["key"]: {"response": got}})
    first, second = side[("m1", 0)], side[("m1", 1)]
    assert (first["status"], first["verdict"], first["exclusion_code"]) == ("ok", "new_entry", None)
    assert (second["status"], second["verdict"], second["exclusion_code"]) == ("invalid", "uncertain", "TRIAGE_INVALID")
    assert second["model_verdict"] == "not_entry" and first["prompt_key"] == second["prompt_key"] == row["key"]


# ---------------------------------------------------------------- 5-8: deterministic rules
def test_contract_discouraged_wording_is_flagged_and_the_verdict_stays_the_models(tmp_path):
    rule = tr.deterministic_rule("仿写 合约先等等，现货布局 BTC 60000")
    assert (rule["code"], rule["verdict"], rule["reason"]) == ("CONTRACT_DISCOURAGED", "not_entry", "advised_not_to_follow")
    for text in ("仿写 合约先别做", "仿写 先别开合约", "仿写 这单不建议跟", "仿写 不许合约", "仿写 别跟", "仿写 不要跟这单"):
        assert tr.deterministic_rule(text)["code"] == "CONTRACT_DISCOURAGED", text
    lake = Lake(tmp_path)
    text = "仿写 合约先等等，现货布局 BTC 60000 多"
    lake.root(1, at(0), text)
    rows, _, counts = prompts(lake.write())
    assert counts["rule_CONTRACT_DISCOURAGED"] == 1
    side, _ = sidecar(tmp_path, lake.layout, {rows["m1"]["key"]: {"response": item(answer(evidence_quote="BTC 60000 多"))}})
    row = side[("m1", 0)]
    # B's lifecycle applies the rule (not_entry, CONTRACT_DISCOURAGED); the sidecar keeps the validated answer and the span.
    assert (row["verdict"], row["reason"], row["exclusion_code"], row["status"]) == ("new_entry", "other", None, "ok")
    assert (row["rule_code"], text[row["rule_start"]:row["rule_end"]]) == ("CONTRACT_DISCOURAGED", "合约先等")


def test_spot_only_wording_keeps_the_entry_as_spot(tmp_path):
    for text in ("仿写 BTC 60000 只做现货", "仿写 不做合约，只拿现货 BTC 60000", "仿写 坚持现货 BTC 60000"):
        rule = tr.deterministic_rule(text)
        assert (rule["code"], rule["verdict"], rule["venue_hint"]) == ("SPOT_ONLY", None, "spot"), text
    lake = Lake(tmp_path)
    lake.root(1, at(0), "仿写 不做合约，只拿现货 BTC 60000 多")
    rows, _, _ = prompts(lake.write())
    side, _ = sidecar(tmp_path, lake.layout, {rows["m1"]["key"]: {"response": item(answer(evidence_quote="BTC 60000 多"))}})
    row = side[("m1", 0)]
    assert (row["verdict"], row["venue_hint"], row["rule_code"], row["exclusion_code"]) == ("new_entry", "unspecified", "SPOT_ONLY", None)
    assert (row["rule_start"], row["rule_end"]) == (3, 7)  # 「不做合约」: B sets venue spot from it


def test_ordinary_follow_wording_is_not_discouragement():
    for text in ("仿写 别跟大饼走反了 ETH 3000 多", "仿写 不要跟在后面追，BTC 60000 挂单", "仿写 别跟风", "仿写 不要跟着情绪走"):
        assert tr.deterministic_rule(text) is None, text


def test_cash_watch_area_post_is_not_entry(tmp_path):
    text = "仿写 ETH 关注区域 2900-2950，看是否企稳……失效 2850……"
    rule = tr.deterministic_rule(text)
    assert (rule["code"], rule["verdict"], rule["reason"]) == ("CASH_WATCH_POST", "not_entry", "watchlist")
    assert tr.deterministic_rule("仿写 ETH 关注区域 2900-2950") is None
    lake = Lake(tmp_path)
    lake.root(1, at(0), text, symbol="ETH", entry=("zone", "2900", "2950"))
    rows, _, _ = prompts(lake.write())
    side, _ = sidecar(tmp_path, lake.layout, {})
    row = side[("m1", 0)]
    assert (row["status"], row["verdict"], row["exclusion_code"], row["rule_code"]) == ("missing", None, "TRIAGE_MISSING", "CASH_WATCH_POST")


def test_rules_read_the_branch_paragraph_when_the_message_names_several_coins():
    text = "仿写 BTC 60000 多\n仿写 SUI 3.2 多，不建议跟"
    btc = dict(op="open", symbol_raw="BTC", spans=[dict(field="entry.price", start=3, end=8, source="text")])
    sui = dict(op="open", symbol_raw="SUI", spans=[dict(field="entry.price", start=text.index("3.2"), end=text.index("3.2") + 3, source="text")])
    assert tr.deterministic_rule(text, btc, [sui]) is None
    assert tr.deterministic_rule(text, sui, [btc])["code"] == "CONTRACT_DISCOURAGED"


# ---------------------------------------------------------------- 9: key stability
def test_key_is_stable_and_follows_candidate_order(tmp_path):
    lake = Lake(tmp_path)
    lake.root(1, at(-20), "仿写 BTC 61000 多")
    lake.root(2, at(-10), "仿写 ETH 3000 空", symbol="ETH", side="short", entry=("limit", "3000", "3000"))
    lake.root(3, at(0), "仿写 BTC 60000 多")
    layout = lake.write()
    first, _, _ = prompts(layout)
    again, _, _ = prompts(layout)
    assert first["m3"]["key"] == again["m3"]["key"]
    assert first["m3"]["key"] == record_key(first["m3"]["system"], first["m3"]["user"], tr.SCHEMA_NAME)
    doc = user(first["m3"])
    kw = dict(channel_name="仿写频道", message_date=doc["message_date"], reply_text=None, previous=doc["previous"], branches=doc["branches"])
    same = record_key(*tr.build_prompt(doc["text"], candidates=doc["candidates"], **kw), tr.SCHEMA_NAME)
    swapped = record_key(*tr.build_prompt(doc["text"], candidates=doc["candidates"][::-1], **kw), tr.SCHEMA_NAME)
    assert same == first["m3"]["key"] and swapped != same and len(doc["candidates"]) == 2


# ---------------------------------------------------------------- 10: sidecar identity
def test_a_changed_sidecar_changes_its_signature_and_the_graph_input_hash_part(tmp_path):
    lake = Lake(tmp_path)
    lake.root(1, at(0), "仿写 BTC 60000 多")
    rows, _, _ = prompts(lake.write())
    key = rows["m1"]["key"]
    sidecar(tmp_path, lake.layout, {key: {"response": item(answer())}})
    path = tmp_path / "nostop_triage.parquet"
    one = tr.sidecar_signature(path)
    assert one["triage_schema"] == tr.SCHEMA_NAME and one["recording_version"] == tr.IMPORT_VERSION and one["rows"] == 1
    sidecar(tmp_path, lake.layout, {key: {"response": item(answer(verdict="not_entry", reason="result_post"))}})
    two = tr.sidecar_signature(path)
    assert one["sha256"] != two["sha256"] and one["recording_sha256"] != two["recording_sha256"]
    assert stable_id("gin", one) != stable_id("gin", two)
    sidecar(tmp_path, lake.layout, {key: {"response": item(answer(verdict="not_entry", reason="result_post"))}})
    assert tr.sidecar_signature(path) == two  # same inputs, same build time: same file


# ---------------------------------------------------------------- 11: selection
def test_selection_covers_dup_of_and_promoted_roots_and_leaves_out_short_companions(tmp_path):
    lake = Lake(tmp_path)
    kept = lake.root(1, at(0), "仿写 BTC 60000 多 止损 59000", stop="59000")                        # own stop: not selected
    lake.root(2, at(1), "仿写 BTC 60000 多 再说一遍", link_kind="restatement", dup_of=kept)          # dup_of, stopless: selected
    lake.root(3, at(0, 30), "仿写 做多比特币了", link_kind="companion", dup_of=kept)                 # companion <=120 s: out
    long_kept = lake.root(4, at(5), "仿写 ETH 3000 空 止损 3100", symbol="ETH", side="short", stop="3100",
                          entry=("limit", "3000", "3000"), grade="H1", edit_delay_s=1801)
    lake.root(5, at(5, 20), "仿写 做空以太了", symbol="ETH", side="short", link_kind="companion", dup_of=long_kept)   # kept is a long edit: in
    edit_ok = lake.root(6, at(8), "仿写 SOL 150 多 止损 140", symbol="SOL", stop="140", entry=("limit", "150", "150"), grade="H1", edit_delay_s=1800)
    lake.root(7, at(8, 20), "仿写 做多 SOL 了", symbol="SOL", link_kind="image_text_pair", dup_of=edit_ok)   # kept edit 1800 s: out
    lake.root(8, at(9), "仿写 做多 SOL", symbol="SOL", link_kind="body_after_title", dup_of=edit_ok)        # gap 300 s per plan_link: in
    lake.link(8, 0, "e8-0", edit_ok, "body_after_title", 300.0)
    promoted = {"schema_version": 2, "time_ref": "conditional", "time_ref_promoted": {"from": "conditional", "rule": "setup_card", "scope": "main"}}
    lake.root(9, at(12), "仿写 交易策略：入场 3100 止损 3050", symbol="ETH", entry=("limit", "3100", "3100"), stop="3050", checks=promoted)
    close = {"schema_version": 2, "time_ref": "now", "action": {"stop": {"kind": "condition", "price": {"value": "57000"}, "condition": "4小时收盘跌破57000"}}}
    lake.root(10, at(13), "仿写 BTC 60000 多 4小时收盘跌破57000止损", checks=close)                 # close stop is an own stop: out
    lake.root(11, at(14), "仿写 BTC 现价多", entry=None, mode="market_ref", link_kind="repost")       # repost, stopless: in
    lake.msg(12, at(15), "仿写 BTC 继续拿着")
    lake.episode(lake.plan(12, kind="stop_move", entry=None), orphan=True)                          # orphan root: out
    lake.root(13, at(16), "仿写 BTC 58000 多", link_kind="late_stop", dup_of=kept)                    # late_stop, stopless: in
    lake.root(14, at(0, 40), "仿写 交易策略：入场 60000 止损 59000", stop="59000", checks=promoted,
              link_kind="companion", dup_of=kept)                                                      # promoted companion: in
    rows, meta, counts = prompts(lake.write())
    assert sorted(rows) == ["m11", "m13", "m14", "m2", "m5", "m8", "m9"]
    assert counts["excluded_companion"] == 2 and counts["orphan_roots"] == 1 and counts["entry_roots"] == 13
    assert counts["selected_promoted"] == 2 and counts["selected_no_own_stop"] == 5 and counts["messages"] == 7
    sel = {k: m["branches"][0]["selected_as"] for k, m in meta.items()}
    assert sel[rows["m9"]["key"]] == ["promoted"] and sel[rows["m2"]["key"]] == ["no_own_stop"]
    assert user(rows["m11"])["branches"][0]["entry_summary"] == "market"
    assert tr.own_stop(next(p for p in lake.plans if p["message_id"] == 11)) is None  # the market root
    assert tr.own_stop(next(p for p in lake.plans if p["message_id"] == 10)) == ("close", Decimal("57000"))


# ---------------------------------------------------------------- sidecar statuses, recording and CLI
def test_sidecar_counts_missing_abstain_and_uncertain_separately(tmp_path):
    lake = Lake(tmp_path)
    for i, text in enumerate(("仿写 BTC 60000 多 一", "仿写 BTC 60000 多 二", "仿写 BTC 60000 多 三", "仿写 BTC 60000 多 四", "仿写 BTC 60000 多 五")):
        lake.root(i + 1, at(i * 100), text)
    rows, _, _ = prompts(lake.write())
    items = {rows["m1"]["key"]: {"response": item(answer(verdict="uncertain"))},
             rows["m2"]["key"]: {"abstain": {"reason_code": "INTENT_AMBIGUOUS", "note": cx.TRANSPORT_FAILED}},
             rows["m3"]["key"]: {"abstain": {"reason_code": "INTENT_AMBIGUOUS", "note": "invalid_triage_envelope"}},
             rows["m4"]["key"]: {"response": item(answer(verdict="not_entry", reason="result_post"))}}
    side, report = sidecar(tmp_path, lake.layout, items)
    codes = {svid: side[(svid, 0)]["exclusion_code"] for svid in ("m1", "m2", "m3", "m4", "m5")}
    assert codes == dict(m1="TRIAGE_UNCERTAIN", m2="TRIAGE_MISSING", m3="TRIAGE_INVALID", m4="TRIAGE_NOT_ENTRY", m5="TRIAGE_MISSING")
    assert side[("m5", 0)]["status_note"] == "no_recording" and side[("m2", 0)]["verdict"] is None
    assert report["status_missing"] == 2 and report["missing_share"] == 0.4 and report["rows"] == 5
    frame = pl.read_parquet(tmp_path / "nostop_triage.parquet")
    assert dict(frame.schema) == tr._sidecar_schema()
    assert frame["triage_schema"].unique().to_list() == [tr.SCHEMA_NAME]
    with pytest.raises(ValueError, match="invalid triage recording version"):
        tr.build_sidecar(lake.layout, GV, recording(tmp_path / "bad.json", {}, version="cx-batch-v2"), tmp_path / "x.parquet")
    with pytest.raises(ValueError, match="sidecar_duplicate_key"):
        tr.check_sidecar(pl.concat([frame, frame.head(1)]))
    # The rule codes are B's to apply: the sidecar's exclusion_code never carries one.
    with pytest.raises(ValueError, match="sidecar_bad_exclusion_code"):
        tr.check_sidecar(frame.with_columns(pl.lit("CONTRACT_DISCOURAGED").alias("exclusion_code")))
    with pytest.raises(ValueError, match="sidecar_bad_rule_code"):
        tr.check_sidecar(frame.with_columns(pl.lit("NOT_A_RULE").alias("rule_code")))


@pytest.fixture
def fake_triage_codex(tmp_path, monkeypatch):
    script, capture = tmp_path / "fake-triage-codex", tmp_path / "triage-calls.jsonl"
    script.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
data = json.loads(sys.stdin.read())
with open(os.environ["TRIAGE_CAPTURE"], "a") as stream:
    stream.write(json.dumps({{"schema": schema, "instructions": data["instructions"], "messages": data["messages"]}}) + "\\n")
items = []
for message in data["messages"]:
    branches = []
    for b in message["branches"]:
        target = message["candidates"][0]["message_id"] if message["candidates"] else None
        # Every answer names message 999 as well: it is never a candidate, so the context check must reject it.
        branches.append({{"branch_index": b["branch_index"], "verdict": "new_entry", "reason": "other", "venue_hint": "perp",
                          "relation": "restates" if target else "none", "relation_target_message_id": target,
                          "evidence_quote": message["text"][:6]}})
        branches.append({{"branch_index": 99, "verdict": "not_entry", "reason": "other", "venue_hint": "perp",
                          "relation": "amends", "relation_target_message_id": 999, "evidence_quote": "x"}})
    items.append({{"key": message["key"], "schema_version": "cx.triage.v1", "branches": branches}})
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps({{"items": items}}))
''')
    script.chmod(0o700)
    monkeypatch.setenv("TRIAGE_CAPTURE", str(capture))
    return script, capture


def test_cli_export_cx_batch_run_import_and_build(tmp_path, fake_triage_codex, capsys):
    script, capture = fake_triage_codex
    lake = Lake(tmp_path)
    lake.root(1, at(-10), "仿写 BTC 61000 多")
    lake.root(2, at(0), "仿写 BTC 60000 多 再来")
    lake.write()
    out = tmp_path / "l3.jsonl"
    assert tr.main(["export", "--build-dir", str(tmp_path / "lake"), "--graph-version", GV, "--output", str(out)]) == 0
    exported = list(cx.read_jsonl(out))
    assert json.loads(capsys.readouterr().out)["messages"] == 2 == len(exported)
    assert json.loads(out.with_suffix(".stats.json").read_text())["unique_keys"] == 2
    report = cx.run_batches(out, tmp_path / "run", executable=str(script), batch_size=5, backoff=0)
    assert report["abstained"] == 0
    call = [json.loads(line) for line in capture.read_text().splitlines()][0]
    assert call["schema"] == json.loads(json.dumps(tr.output_schema())) == json.loads((tmp_path / "run" / "schema.json").read_text())
    assert call["instructions"] == tr.RULES
    assert all("system" not in m and "schema_version" not in m and m["branches"] for m in call["messages"])
    responses = {r["key"]: r["response"] for r in cx.read_jsonl(tmp_path / "run" / "responses.jsonl")}
    by_svid = {r["source_version_id"]: responses[r["key"]] for r in exported}
    assert by_svid["m2"]["checked"][0]["relation_target_message_id"] == 1 and by_svid["m2"]["checked"][0]["status"] == "ok"
    assert by_svid["m1"]["checked"][0]["relation"] == "none"
    assert all(r["rejected"] == [dict(branch_index=99, reason="not_a_selected_branch")] for r in responses.values())
    assert cx.revalidate_raw(out, tmp_path / "run", tmp_path / "again")["missing"] == 0
    rec = tmp_path / "triage-v1.json"
    assert cx.main(["import", "--responses", str(tmp_path / "run" / "responses.jsonl"), "--output", str(rec), "--schema", tr.SCHEMA_NAME]) == 0
    assert json.loads(rec.read_text())["version"] == tr.IMPORT_VERSION
    capsys.readouterr()
    side = tmp_path / "nostop_triage-v1.parquet"
    assert tr.main(["build", "--build-dir", str(tmp_path / "lake"), "--graph-version", GV, "--recording", str(rec), "--output", str(side)]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["rows"] == 2 and built["status_ok"] == 2 and built["sidecar"]["sha256"] == tr.sidecar_signature(side)["sha256"]
    rows = {r["source_version_id"]: r for r in pl.read_parquet(side).to_dicts()}
    assert rows["m2"]["relation"] == "restates" and rows["m2"]["venue_hint"] == "perp" and rows["m2"]["exclusion_code"] is None


def test_cx_batch_dispatches_answers_to_this_module():
    good = item(answer())
    stored = cx.quote_response(good, TEXT, candidates=CONTEXT, expected_schema=tr.SCHEMA_NAME)
    assert stored["response"]["checked"][0]["status"] == "ok"
    assert cx.quote_response(good, TEXT, expected_schema="cx.numfill.v1")["abstain"]["note"] == "response_schema_mismatch"
    assert cx.quote_response(good, TEXT, expected_schema="cx.actions.v2")["abstain"]["note"] == "response_schema_mismatch"
    assert cx.side_pass(tr.SCHEMA_NAME) is tr


# ---------------------------------------------------------------- review fixes: t_vis cut, paragraph scope, missing rows, sha
def test_context_is_cut_by_t_vis_not_by_message_date(tmp_path):
    lake = Lake(tmp_path)
    # The root was first seen 30 minutes after it was posted: t_vis(root) = at(30).
    lake.root(10, at(0), "仿写 BTC 60000 多", visible=at(30))
    # Posted earlier, visible later (a late first_seen and an H1 on a later clock): never in the prompt.
    lake.msg(1, at(-10), "仿写 早发晚见的闲聊", visible=at(31))
    lake.msg(2, at(-15), "仿写 编辑后才可见的旧帖", grade="H1", edit_delay_s=7200, visible=at(45))
    lake.root(3, at(-20), "仿写 BTC 59000 多 早发晚见", entry=("limit", "59000", "59000"), visible=at(40))
    # Posted after the root's message_date, visible before its t_vis: in the prompt.
    lake.msg(4, at(5), "仿写 晚发早见的闲聊", visible=at(6))
    lake.root(5, at(10), "仿写 BTC 61000 多 晚发早见", entry=("limit", "61000", "61000"), visible=at(12))
    rows, _, _ = prompts(lake.write())
    doc = user(rows["m10"])
    blob = json.dumps(doc, ensure_ascii=False)
    for hidden in ("早发晚见", "编辑后才可见"):
        assert hidden not in blob
    assert [(p["message_id"], p["minutes_before"]) for p in doc["previous"]] == [(4, 24), (5, 18)]
    assert [(c["message_id"], c["minutes_before"]) for c in doc["candidates"]] == [(5, 18)]


def test_reply_parent_with_the_roots_own_clock_is_not_context(tmp_path):
    lake = Lake(tmp_path)
    lake.msg(1, at(0), "仿写 同一时刻的父消息")
    lake.root(2, at(0), "仿写 BTC 60000 多", reply=1)
    lake.msg(3, at(0, -1), "仿写 早一秒的父消息")
    lake.root(4, at(0, 30), "仿写 BTC 60100 多", reply=3)
    rows, _, _ = prompts(lake.write())
    assert user(rows["m2"])["reply_text"] is None
    assert user(rows["m4"])["reply_text"] == "仿写 早一秒的父消息"


# Two coins, so the rule reads paragraphs. The discouragement sits only on the third line, which has no coin of
# its own: BTC's first leg is cut from it by the sibling leg's anchor (61000), not by another coin mention.
MULTI = "仿写 SUI 3.2 多\n仿写 BTC 60000 多\n61000 再补一单，不建议跟"
MULTI_LEGS = (("SUI", "3.2"), ("BTC", "60000"), ("BTC", "61000"))


def _leg_action(index):
    symbol, price = MULTI_LEGS[index]
    start = MULTI.index(price)
    return dict(op="open", time_ref="now", symbol_raw=symbol, branch_index=index,
                spans=[dict(field="entry.price", start=start, end=start + len(price), source="text")])


@pytest.mark.parametrize("source", ["extracted_event", "canonical_plan"])
def test_rule_audit_reads_each_branchs_own_paragraph_through_the_pipeline(tmp_path, source):
    lake = Lake(tmp_path)
    lake.msg(1, at(0), MULTI)
    for branch, (symbol, price) in enumerate(MULTI_LEGS):
        action = _leg_action(branch)
        checks = {"schema_version": 2, "time_ref": "now", "action": action} if source == "canonical_plan" else None
        lake.episode(lake.plan(1, branch, symbol=symbol, entry=("limit", price, price), checks=checks))
        if source == "extracted_event":
            lake.event(1, branch, action)
    rows, _, counts = prompts(lake.write())
    assert rows["m1"]["branch_indexes"] == [0, 1, 2] and counts["rule_CONTRACT_DISCOURAGED"] == 1 and counts["rule_none"] == 2
    got = item(answer(0, evidence_quote="SUI 3.2 多"), answer(1, evidence_quote="BTC 60000 多"), answer(2, evidence_quote="61000 再补一单"))
    side, report = sidecar(tmp_path, lake.layout, {rows["m1"]["key"]: {"response": got}})
    sui, btc, leg = (side[("m1", i)] for i in range(3))
    assert sui["rule_code"] is None and btc["rule_code"] is None and (btc["rule_start"], btc["rule_end"]) == (None, None)
    assert leg["rule_code"] == "CONTRACT_DISCOURAGED" and MULTI[leg["rule_start"]:leg["rule_end"]] == "不建议跟"
    assert [r["verdict"] for r in (sui, btc, leg)] == ["new_entry"] * 3 and [r["exclusion_code"] for r in (sui, btc, leg)] == [None] * 3
    assert report["rule_CONTRACT_DISCOURAGED_model_new_entry"] == 1


def test_a_null_branch_index_reads_its_plan_link_row(tmp_path):
    lake = Lake(tmp_path)
    kept = lake.root(1, at(0), "仿写 SOL 150 多 止损 140", symbol="SOL", stop="140", entry=("limit", "150", "150"))
    lake.root(2, at(5), "仿写 做多 SOL", symbol="SOL", branch=None, link_kind="body_after_title", dup_of=kept)
    lake.link(2, None, "e2-None", kept, "body_after_title", 300.0)  # 300 s apart per plan_link: not a companion
    rows, _, counts = prompts(lake.write())
    assert sorted(rows) == ["m2"] and rows["m2"]["branch_indexes"] == [0] and "excluded_companion" not in counts


def test_a_selected_root_without_text_or_clock_still_gets_a_missing_row(tmp_path):
    lake = Lake(tmp_path)
    lake.msg(1, at(0), "", kinds=("photo",))                     # an image-only root: no text, no prompt
    lake.episode(lake.plan(1))
    lake.root(2, at(1), "仿写 BTC 60000 多")
    rows, meta, unprompted, counts = tr.plan_prompts(lake.write(), GV)
    assert [r["source_version_id"] for r in rows] == ["m2"] and counts["selected_no_own_stop"] == 2
    assert counts["skipped_no_clock_or_text"] == 1 and [(b["source_version_id"], b["branch_index"]) for b in unprompted] == [("m1", 0)]
    side, report = sidecar(tmp_path, lake.layout, {rows[0]["key"]: {"response": item(answer())}})
    row = side[("m1", 0)]
    assert (row["status"], row["status_note"], row["exclusion_code"], row["verdict"], row["prompt_key"]) == \
        ("missing", "no_text_or_clock", "TRIAGE_MISSING", None, None)
    assert (row["message_id"], row["source_plan_id"], row["available_at"]) == (1, "p1-0", at(0))
    assert report["rows"] == 2 == counts["selected_no_own_stop"] and report["status_missing"] == 1 and report["missing_share"] == 0.5


def test_rebuilding_the_same_sidecar_gives_the_same_sha_without_an_explicit_clock(tmp_path, capsys):
    lake = Lake(tmp_path)
    lake.root(1, at(0), "仿写 BTC 60000 多")
    lake.root(2, at(3), "仿写 BTC 60100 多", visible=at(7))
    rows, _, _ = prompts(lake.write())
    rec = recording(tmp_path / "triage.json", {rows["m1"]["key"]: {"response": item(answer())}})
    first = tr.build_sidecar(lake.layout, GV, rec, tmp_path / "a.parquet")
    second = tr.build_sidecar(lake.layout, GV, rec, tmp_path / "b.parquet")
    assert first["sidecar"]["sha256"] == second["sidecar"]["sha256"] and first["ingested_at"] == at(7).isoformat()
    assert pl.read_parquet(tmp_path / "a.parquet")["ingested_at"].unique().to_list() == [at(7)]
    stamp = "2026-10-05T00:00:00+00:00"
    assert tr.main(["build", "--build-dir", str(tmp_path / "lake"), "--graph-version", GV, "--recording", str(rec),
                    "--output", str(tmp_path / "c.parquet"), "--ingested-at", stamp]) == 0
    assert json.loads(capsys.readouterr().out)["ingested_at"] == stamp
    assert pl.read_parquet(tmp_path / "c.parquet")["ingested_at"].unique().to_list() == [datetime(2026, 10, 5, tzinfo=UTC)]
    with pytest.raises(SystemExit):
        tr.main(["build", "--build-dir", str(tmp_path / "lake"), "--graph-version", GV, "--recording", str(rec),
                 "--output", str(tmp_path / "d.parquet"), "--ingested-at", "2026-10-05T00:00:00"])


def test_a_candidate_with_a_close_clause_stop_shows_it_as_a_close_stop(tmp_path):
    lake = Lake(tmp_path)
    close = {"schema_version": 2, "time_ref": "now", "stop_rule": {"rule": "close_from_clause", "base": "57000"}}
    lake.root(1, at(-10), "仿写 BTC 60000 多 4小时收盘跌破57000止损", stop="57000", checks=close)
    lake.root(2, at(0), "仿写 BTC 60500 多")
    rows, _, _ = prompts(lake.write())
    assert tr.own_stop(lake.plans[0]) == ("close", Decimal("57000"))
    assert [c["stop"] for c in user(rows["m2"])["candidates"]] == ["close 57000"]
