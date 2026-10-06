"""cx.symfill.v1 side pass: fill a missing coin from the reply parent / previous messages. Invented messages only."""
from datetime import timedelta
import hashlib
import json

import polars as pl
import pytest

from quant_lab.data import api, cx_batch as cx, cx_symfill as sf, cx_v2, extract, lifecycle, linker, validate
from quant_lab.data.lake import Layout
from quant_lab.data.llm import record_key
from quant_lab.data.market_stub import InstrumentRegistry, InstrumentRule, SyntheticMarks, fixture_registry, instrument_id_for
from test_cx_batch import mutant
from v8_lake import CHANNEL, T0, Msg, mv_frame, number, open_action, stop_action

#: Synthetic registry: the fixture coins plus two invented listings the tests trade.
REGISTRY = InstrumentRegistry(fixture_registry().rules + [
    InstrumentRule("ROBO", instrument_id_for("ROBO"), T0 - timedelta(days=90), None, 0.00001, 1),
    InstrumentRule("BNB", instrument_id_for("BNB"), T0 - timedelta(days=900), None, 0.01, 0.01)], version="registry-symfill-test")
MARKS = {"ROBO": 0.0405, "BNB": 628.0, "BTC": 60100, "ETH": 3185, "SOL": 151.0}
ALERT = "仿写 ROBOUSDT.P 穿过 0.04076"
ROOT = "仿写 这个想玩的小小仓，止损在0.038"


def svid(mid, channel=CHANNEL):
    return f"sv{channel % 10}-{mid}"


def stopless(symbol=None, side="long", stop="0.038"):
    return open_action(symbol=symbol, side=side, stop=stop)


def reply_lake():
    return [Msg(1, ALERT, 0), Msg(2, ROOT, 120, [stopless()], reply=1)]


def lake(tmp_path, messages):
    """Flat lake + main v2 prompts and recording, as cx_batch export / import would leave them."""
    layout = Layout.flat(tmp_path / "lake").ensure()
    mv = mv_frame(messages)
    mv.write_parquet(layout.message_version)
    prompts = tmp_path / "prompts.jsonl"
    cx.export_prompts(layout, prompts)
    by_svid = {svid(m.mid, m.channel): m for m in messages}
    items = {row["key"]: dict(response=dict(schema_version=2, actions=list(by_svid[row["source_version_id"]].actions)))
             for row in cx.read_jsonl(prompts)}
    recording = tmp_path / "recorded-main.json"
    recording.write_text(cx.dumps(dict(version="cx-batch-v2", items=items)))
    return layout, prompts, recording


def export(tmp_path, layout, prompts, recording, name="l4.jsonl"):
    stats = sf.export(prompts, recording, layout.message_version, tmp_path / name, registry=REGISTRY)
    return stats, list(cx.read_jsonl(tmp_path / name))


def branch(index, symbol, source, message_id, quote):
    return dict(branch_index=index, symbol=symbol, source=source, source_message_id=message_id, evidence_quote=quote)


def record(tmp_path, rows, answers, name="symfill.json"):
    """answers: {source_version_id: [branch answers]} -> model responses checked by cx_batch, imported with the schema."""
    responses = tmp_path / (name + ".responses.jsonl")
    out = []
    for row in rows:
        item = dict(schema_version=sf.SCHEMA_NAME, branches=answers.get(row["source_version_id"], []))
        out.append(dict(key=row["key"], **cx.quote_response(item, row["text"], candidates=sf.contexts_from_user(row["user"]),
                                                            expected_schema=row["schema_name"])))
    responses.write_text("".join(cx.dumps(r) + "\n" for r in out))
    path = tmp_path / name
    assert cx.main(["import", "--responses", str(responses), "--output", str(path), "--schema", sf.SCHEMA_NAME]) == 0
    return path


def build(layout, recording, symfill=None):
    kw = dict(symfill_fixture=symfill, registry=REGISTRY) if symfill is not None else {}
    summary = extract.run(layout, llm_fixture=recording, ingested_at=T0, **kw)
    ex = pl.read_parquet(layout.extracted_event)
    return summary, ex.filter(pl.col("extractor").struct.field("name") == "llm")


def llm(ex, mid, index=0):
    frame = ex.filter((pl.col("message_id") == mid) & (pl.col("branch_index") == index))
    assert frame.height == 1, (mid, index, frame.height)
    row = frame.row(0, named=True)
    return row, json.loads(row["checks"])


def graph(layout, ex_all):
    """validate -> linker -> lifecycle on this build (synthetic marks), as v8_lake.build does."""
    mv = pl.read_parquet(layout.message_version)
    anchors = {instrument_id_for(k): [(T0 - timedelta(days=30), v)] for k, v in MARKS.items()}
    cp, *_ = validate.validate_frame(ex_all, mv, registry=REGISTRY, marks=SyntheticMarks(anchors), ingested_at=T0)
    cb, jd, _ = linker.build_candidates(cp, mv, ex_all, plan_source="llm", ingested_at=T0)
    episodes, *_ = lifecycle.build_graph(cp, mv, cb, jd, None, graph_version="symfill-test", plan_source="llm",
                                         extracted_event=ex_all, ingested_at=T0)
    return cp, episodes


# ---------------------------------------------------------------- fills
def test_reply_parent_alert_fills_the_symbol(tmp_path):
    layout, prompts, recording = lake(tmp_path, reply_lake())
    stats, rows = export(tmp_path, layout, prompts, recording)
    assert stats["messages"] == 1 and stats["branches"] == 1 and stats["with_reply"] == 1 and stats["main_key_mismatch"] == 0
    user = json.loads(rows[0]["user"])
    assert rows[0]["system"] == sf.RULES and rows[0]["source_key"] == user["source_key"] and rows[0]["branch_indexes"] == [0]
    assert user["reply"] == dict(message_id=1, text=ALERT)
    assert user["previous"] == [dict(message_id=1, minutes_before=2, text=ALERT)]
    assert user["branches"] == [dict(branch_index=0, symbol_raw=None, side="long", entry="market", stop="0.038")]
    fixture = record(tmp_path, rows, {svid(2): [branch(0, "ROBO", "reply", 1, "ROBOUSDT.P 穿过")]})
    summary, ex = build(layout, recording, fixture)
    assert summary["llm"]["symfill_applied"] == 1 and summary["llm"]["symfill_missing"] == summary["llm"]["symfill_abstain"] == 0
    assert summary["silver_marks"]["symfill:reply"] == 1 and summary["symfill_registry_version"] == REGISTRY.version
    row, checks = llm(ex, 2)
    assert row["symbol_raw"] == "ROBO" and checks["action"]["symbol_raw"] == "ROBO"
    response_hash = hashlib.sha256(json.dumps(json.loads(fixture.read_text())["items"][rows[0]["key"]]["response"], sort_keys=True,
                                              ensure_ascii=False).encode()).hexdigest()
    assert {k: checks["symfill"][k] for k in ("source", "source_message_id", "recording_version", "response_hash")} == dict(
        source="reply", source_message_id=1, recording_version=sf.IMPORT_VERSION, response_hash=response_hash)
    assert checks["symfill"]["original_symbol_raw"] is None and checks["symfill"]["source_version_id"] == svid(1)
    assert checks["dependencies"] == [dict(ref=svid(1), purpose="all", available_at=T0.isoformat())]
    assert ":symfill:" + summary["symfill_fixture_sha256"] in row["extractor"]["version"]
    # The filled root is an ordinary root with an instrument; without the fixture it has none.
    cp, episodes = graph(layout, pl.read_parquet(layout.extracted_event))
    plan = cp.filter((pl.col("message_id") == 2) & (pl.col("extractor_name") == "llm")).row(0, named=True)
    assert plan["instrument_id"] == instrument_id_for("ROBO") and plan["side"] == "long"
    assert episodes.filter(pl.col("root_message_id") == 2)["instrument_id"].to_list() == [instrument_id_for("ROBO")]
    _, ex_plain = build(layout, recording)
    assert llm(ex_plain, 2)[0]["symbol_raw"] is None
    cp_plain, _ = graph(layout, pl.read_parquet(layout.extracted_event))
    assert cp_plain.filter((pl.col("message_id") == 2) & (pl.col("extractor_name") == "llm"))["instrument_id"].to_list() == [None]


def test_previous_message_fills(tmp_path):
    msgs = [Msg(9, "仿写 早盘随便聊聊 SOL", -7 * 3600), Msg(10, "仿写 bnb做个空 635止损", 0, [open_action("BNB", "short", stop=635)]),
            Msg(11, "仿写 625-630是好机会", 60, [open_action(symbol=None, side="short", lo=625, hi=630)])]
    layout, prompts, recording = lake(tmp_path, msgs)
    stats, rows = export(tmp_path, layout, prompts, recording)
    # The BNB open names its coin and is not selected; the 7 h old message is outside the 6 h window.
    assert stats["messages"] == 1 and [r["source_version_id"] for r in rows] == [svid(11)]
    user = json.loads(rows[0]["user"])
    assert user["reply"] is None and user["previous"] == [dict(message_id=10, minutes_before=1, text="仿写 bnb做个空 635止损")]
    assert user["branches"] == [dict(branch_index=0, symbol_raw=None, side="short", entry="zone 625-630", stop=None)]
    fixture = record(tmp_path, rows, {svid(11): [branch(0, "BNB", "previous", 10, "bnb做个空")]})
    summary, ex = build(layout, recording, fixture)
    row, checks = llm(ex, 11)
    assert row["symbol_raw"] == "BNB" and checks["symfill"]["source"] == "previous" and checks["symfill"]["source_message_id"] == 10
    assert summary["silver_marks"]["symfill:previous"] == 1
    assert llm(ex, 10)[0]["symbol_raw"] == "BNB" and "symfill" not in llm(ex, 10)[1]


def test_a_registered_alias_in_the_quote_counts_and_case_is_ignored():
    context = dict(branch_indexes=[0, 1], reply=None, previous=[dict(message_id=5, text="仿写 大饼 6万3 多"), dict(message_id=6, text="仿写 robo/usdt 起飞")])
    item = dict(schema_version=sf.SCHEMA_NAME, branches=[branch(0, "BTC", "previous", 5, "大饼 6万3"), branch(1, "ROBO", "previous", 6, "robo/usdt")])
    checked = sf.validate_response(item, "仿写 再来一单", context)["response"]["checked"]
    assert [(c["status"], c["symbol"], c["evidence_span"]) for c in checked] == [("ok", "BTC", [3, 9]), ("ok", "ROBO", [3, 12])]
    # A coin merely containing the letters, or a word around them, is not the symbol.
    assert not sf.symbol_in_quote("OP", "止损 STOP 在 0.5") and not sf.symbol_in_quote("ROBO", "ROBOTICS 板块")


def test_dependency_is_the_parent_version_visible_before_the_root(tmp_path):
    layout, prompts, recording = lake(tmp_path, reply_lake())
    mv = pl.read_parquet(layout.message_version)
    later = mv.filter(pl.col("source_version_id") == svid(1)).with_columns(
        pl.lit(svid(1) + "-v2").alias("source_version_id"), pl.lit(2, dtype=pl.Int32).alias("version_no"),
        (pl.col("available_at") + pl.duration(seconds=300)).alias("available_at"))
    pl.concat([mv, later]).write_parquet(layout.message_version)  # same text, a second version visible after the root
    _, rows = export(tmp_path, layout, prompts, recording)
    fixture = record(tmp_path, rows, {svid(2): [branch(0, "ROBO", "reply", 1, "ROBOUSDT.P")]})
    _, ex = build(layout, recording, fixture)
    checks = llm(ex, 2)[1]
    assert checks["symfill"]["source_version_id"] == svid(1) and checks["dependencies"][0]["available_at"] == T0.isoformat()


def test_numfill_and_symfill_together_use_the_pre_numfill_branch_summary(tmp_path):
    from quant_lab.data import cx_numfill
    refused = [dict(field="entry", reason="原文省略单位，不能猜"), dict(field="stop", reason="6万2 需要换算，不可擅自补成数字")]
    root = dict(op="open", time_ref="now", symbol_raw=None, side="long", entry=None, stop=None, tps=[], field_issues=refused)
    msgs = [Msg(1, "仿写 BTCUSDT.P 穿过 63000", 0), Msg(2, "仿写 这个6万3附近多，跌破6万2止损", 120, [root], reply=1)]
    layout, prompts, recording = lake(tmp_path, msgs)
    cx_numfill.export(prompts, recording, tmp_path / "l2.jsonl")
    numfill_rows = list(cx.read_jsonl(tmp_path / "l2.jsonl"))
    fills = [dict(branch_index=0, field="entry.price", value=dict(value="63000", quote="6万3")),
             dict(branch_index=0, field="stop.price", value=dict(value="62000", quote="6万2"))]
    answers = [dict(key=r["key"], **cx.quote_response(dict(schema_version=cx_numfill.SCHEMA_NAME, fills=fills), r["text"],
                                                       candidates=cx_numfill.contexts_from_user(r["user"]), expected_schema=r["schema_name"]))
               for r in numfill_rows]
    (tmp_path / "l2-responses.jsonl").write_text("".join(cx.dumps(a) + "\n" for a in answers))
    cx.import_responses(tmp_path / "l2-responses.jsonl", tmp_path / "numfill.json", schema=cx_numfill.SCHEMA_NAME)
    _, rows = export(tmp_path, layout, prompts, recording)
    assert json.loads(rows[0]["user"])["branches"][0]["entry"] == "none"  # export never applies numfill
    fixture = record(tmp_path, rows, {svid(2): [branch(0, "BTC", "reply", 1, "BTCUSDT.P")]})
    summary = extract.run(layout, llm_fixture=recording, numfill_fixture=tmp_path / "numfill.json", symfill_fixture=fixture,
                          registry=REGISTRY, ingested_at=T0)
    assert summary["llm"]["numfill_applied"] == 1 and summary["llm"]["symfill_applied"] == 1
    ex = pl.read_parquet(layout.extracted_event).filter(pl.col("extractor").struct.field("name") == "llm")
    row, checks = llm(ex, 2)
    assert row["symbol_raw"] == "BTC" and row["entry"]["lo"] == 63000 and checks["numfill"]["fields"] == ["entry.price", "stop.price"]
    assert ":numfill:" in row["extractor"]["version"] and ":symfill:" in row["extractor"]["version"]


# ---------------------------------------------------------------- deterministic validation
@pytest.mark.parametrize("answer,reason", [
    (branch(0, "ROBO", "reply", 1, "ROBOUSDT.P 穿过 0.05"), "evidence_quote_not_in_source"),   # quote not present
    (branch(0, "ROBO", "reply", 1, "穿过 0.04076"), "symbol_not_in_quote"),                    # symbol not in quote
    (branch(0, "ROBO", "previous", 3, "ROBOUSDT.P 再次"), "source_not_visible"),               # posted after the root
    (branch(0, "ROBO", "reply", 3, "ROBOUSDT.P 穿过"), "source_not_visible"),                  # not the reply parent
    (branch(0, "ROBO", "text", 1, "ROBOUSDT.P 穿过"), "source_message_id_not_null"),
    (branch(0, "ROBO", "none", None, "ROBOUSDT.P 穿过"), "symbol_without_source"),
])
def test_invalid_answers_leave_the_branch_as_it_was(tmp_path, answer, reason):
    msgs = reply_lake() + [Msg(3, "仿写 ROBOUSDT.P 再次穿过 0.0412", 600)]
    layout, prompts, recording = lake(tmp_path, msgs)
    _, rows = export(tmp_path, layout, prompts, recording)
    assert [r["source_version_id"] for r in rows] == [svid(2)]
    user = json.loads(rows[0]["user"])
    assert [p["message_id"] for p in user["previous"]] == [1]  # message 3 is visible only after the root
    checked = sf.validate_response(dict(schema_version=sf.SCHEMA_NAME, branches=[answer]), rows[0]["text"], sf.contexts_from_user(rows[0]["user"]))
    assert checked["response"]["checked"][0]["status"] == "invalid" and reason in checked["response"]["checked"][0]["invalid_reasons"]
    fixture = record(tmp_path, rows, {svid(2): [answer]})
    summary, ex = build(layout, recording, fixture)
    assert summary["llm"]["symfill_abstain"] == 1 and summary["llm"]["symfill_applied"] == 0
    row, checks = llm(ex, 2)
    assert row["symbol_raw"] is None and "symfill" not in checks and "dependencies" not in checks
    assert not any(k.startswith("symfill:") for k in summary["silver_marks"])


@pytest.mark.parametrize("parent", [Msg(1, ALERT, 300), Msg(1, ALERT, 0, grade="H1")], ids=["visible_after_root", "not_original_time"])
def test_a_reply_parent_not_visible_before_the_root_is_not_context(tmp_path, parent):
    # A parent whose clock is after the root's, or one that is no F7 context (H1), cannot be cited as the reply.
    layout, prompts, recording = lake(tmp_path, [parent, Msg(2, ROOT, 120, [stopless()], reply=1)])
    _, rows = export(tmp_path, layout, prompts, recording)
    user = json.loads(rows[0]["user"])
    assert user["reply"] is None and [p["message_id"] for p in user["previous"]] == ([] if parent.dt_s > 120 else [1])
    fixture = record(tmp_path, rows, {svid(2): [branch(0, "ROBO", "reply", 1, "ROBOUSDT.P 穿过")]})
    summary, ex = build(layout, recording, fixture)
    assert summary["llm"]["symfill_abstain"] == 1 and llm(ex, 2)[0]["symbol_raw"] is None


def test_a_symbol_the_registry_cannot_resolve_is_not_filled(tmp_path):
    msgs = [Msg(1, "仿写 FAKEUSDT.P 穿过 0.04076", 0), Msg(2, ROOT, 120, [stopless()], reply=1)]
    layout, prompts, recording = lake(tmp_path, msgs)
    _, rows = export(tmp_path, layout, prompts, recording)
    fixture = record(tmp_path, rows, {svid(2): [branch(0, "FAKE", "reply", 1, "FAKEUSDT.P")]})
    assert json.loads(fixture.read_text())["items"][rows[0]["key"]]["response"]["checked"][0]["status"] == "ok"
    summary, ex = build(layout, recording, fixture)
    assert summary["llm"]["symfill_abstain"] == 1 and llm(ex, 2)[0]["symbol_raw"] is None


def test_model_abstain_and_missing_recording_are_counted(tmp_path):
    layout, prompts, recording = lake(tmp_path, reply_lake())
    _, rows = export(tmp_path, layout, prompts, recording)
    fixture = record(tmp_path, rows, {svid(2): [branch(0, None, "none", None, "")]})
    summary, ex = build(layout, recording, fixture)
    assert summary["llm"]["symfill_abstain"] == 1 and llm(ex, 2)[0]["symbol_raw"] is None
    doc = json.loads(fixture.read_text())
    doc["items"] = {}
    fixture.write_text(json.dumps(doc))
    summary, _ = build(layout, recording, fixture)
    assert summary["llm"]["symfill_missing"] == 1
    with pytest.raises(ValueError, match="symfill_needs_registry"):
        extract.run(layout, llm_fixture=recording, symfill_fixture=fixture, ingested_at=T0)
    with pytest.raises(ValueError, match="invalid symfill fixture"):
        sf.load_fixture(recording)


# ---------------------------------------------------------------- never overwrite, only selected branches
def test_multi_branch_message_fills_only_the_selected_branch(tmp_path):
    text = "仿写 BTC 60000 多 止损 59000；这个也来个小仓，止损在0.038；之前那单止损移到0.039"
    actions = [open_action("BTC", "long", entry=60000, stop=59000), stopless(), stop_action("0.039"),
               open_action(symbol=None, side="long", stop=59000, time_ref="past")]
    msgs = [Msg(1, ALERT, 0), Msg(2, text, 120, actions, reply=1)]
    layout, prompts, recording = lake(tmp_path, msgs)
    stats, rows = export(tmp_path, layout, prompts, recording)
    # Only the symbol-less now-open is asked for: not the BTC open, the stop_move or the past (entry_claimed) open.
    assert rows[0]["branch_indexes"] == [1] and stats["branches"] == 1
    fixture = record(tmp_path, rows, {svid(2): [branch(1, "ROBO", "reply", 1, "ROBOUSDT.P"), branch(0, "ETH", "reply", 1, "ROBOUSDT.P"),
                                                branch(2, "ROBO", "reply", 1, "ROBOUSDT.P")]})
    response = json.loads(fixture.read_text())["items"][rows[0]["key"]]["response"]
    assert [r["reason"] for r in response["rejected"]] == ["not_a_selected_branch", "not_a_selected_branch"]
    summary, ex = build(layout, recording, fixture)
    assert summary["llm"]["symfill_applied"] == 1
    assert [llm(ex, 2, i)[0]["symbol_raw"] for i in range(4)] == ["BTC", "ROBO", None, None]
    assert ["symfill" in llm(ex, 2, i)[1] for i in range(4)] == [False, True, False, False]


def test_a_resolvable_symbol_is_never_overwritten():
    text = "仿写 BTC 60000 多 止损 59000"
    version = mv_frame([Msg(1, ALERT, 0), Msg(2, text, 120, reply=1)]).to_dicts()
    results, _ = cx_v2.parse_actions(dict(schema_version=2, actions=[open_action("BTC", "long", entry=60000, stop=59000)]), text)
    by_message = {(CHANNEL, v["source_id"]["message_id"]): [v] for v in version}
    pools = sf.pools(version)
    # Targets naming the resolvable branch (as a mismatched export might) and an answer that would replace it.
    targets = [dict(branch_index=0, symbol_raw="BTC", side="long", entry="limit 60000", stop="59000")]
    kw = dict(by_message=by_message, message_pools=pools, channel_name="仿写频道", message_date=T0.isoformat(), source_key="k", registry=REGISTRY)
    reply, previous, _ = sf.context_for(version[1], by_message, pools)
    system, user = sf.build_prompt(text, channel_name="仿写频道", message_date=T0.isoformat(), source_key="k", reply=reply, previous=previous, branches=targets)
    answer = cx.quote_response(dict(schema_version=sf.SCHEMA_NAME, branches=[branch(0, "ROBO", "reply", 1, "ROBOUSDT.P")]), text,
                               candidates=sf.contexts_from_user(user), expected_schema=sf.SCHEMA_NAME)
    fixture = dict(version=sf.IMPORT_VERSION, items={record_key(system, user, sf.SCHEMA_NAME): answer}, sha256="x")
    assert sf.apply(results, targets, version[1], fixture=fixture, **kw) == "abstain"
    assert results[0].symbol_raw == "BTC" and "symfill" not in results[0].checks
    broken = mutant(sf.apply, " or not needs_symbol(result, at, registry)", "")
    results, _ = cx_v2.parse_actions(dict(schema_version=2, actions=[open_action("BTC", "long", entry=60000, stop=59000)]), text)
    assert broken(results, targets, version[1], fixture=fixture, **kw) == "applied" and results[0].symbol_raw == "ROBO"


# ---------------------------------------------------------------- keys
def test_prompt_key_is_stable_and_follows_the_context(tmp_path):
    branches = [dict(branch_index=0, symbol_raw=None, side="long", entry="market", stop="0.038")]
    base = dict(text=ROOT, channel_name="仿写频道", message_date=T0.isoformat(), source_key="k1", reply=dict(message_id=1, text=ALERT),
                previous=[dict(message_id=1, minutes_before=2, text=ALERT)], branches=branches)
    key = record_key(*sf.build_prompt(**base), sf.SCHEMA_NAME)
    assert key == record_key(*sf.build_prompt(**dict(base)), sf.SCHEMA_NAME)
    for change in (dict(source_key="k2"), dict(reply=None), dict(previous=[]), dict(text=ROOT + "。"),
                   dict(previous=[dict(message_id=1, minutes_before=3, text=ALERT)]), dict(branches=[dict(branches[0], stop="0.039")])):
        assert record_key(*sf.build_prompt(**dict(base, **change)), sf.SCHEMA_NAME) != key, change
    # Through export: the same lake gives the same key; an edited previous message gives another, and a build with
    # the old recording counts it as missing instead of applying an answer given for other context.
    layout, prompts, recording = lake(tmp_path / "a", [Msg(5, "仿写 SOLUSDT.P 拉升", -60)] + reply_lake())
    _, rows = export(tmp_path, layout, prompts, recording)
    _, again = export(tmp_path, layout, prompts, recording, name="again.jsonl")
    assert [r["key"] for r in rows] == [r["key"] for r in again]
    fixture = record(tmp_path, rows, {svid(2): [branch(0, "ROBO", "reply", 1, "ROBOUSDT.P")]})
    mv = pl.read_parquet(layout.message_version)
    mv.with_columns(pl.when(pl.col("source_version_id") == svid(5)).then(pl.lit("仿写 SOLUSDT.P 回落")).otherwise(pl.col("text")).alias("text")).write_parquet(layout.message_version)
    _, changed = export(tmp_path, layout, prompts, recording, name="changed.jsonl")
    assert changed[0]["key"] != rows[0]["key"] and changed[0]["source_key"] == rows[0]["source_key"]
    summary, ex = build(layout, recording, fixture)
    assert summary["llm"]["symfill_missing"] == 1 and llm(ex, 2)[0]["symbol_raw"] is None


def test_export_refuses_old_rule_prompts_and_skips_mismatched_layouts(tmp_path):
    layout, prompts, recording = lake(tmp_path, reply_lake())
    row = next(r for r in cx.read_jsonl(prompts) if r["source_version_id"] == svid(2))
    legacy = tmp_path / "v7.jsonl"
    legacy.write_text(cx.dumps({k: v for k, v in row.items() if k != "prompt_context_rule"}) + "\n")
    with pytest.raises(ValueError, match="prompts_parent_rule_mismatch"):
        sf.export(legacy, recording, layout.message_version, tmp_path / "x.jsonl", registry=REGISTRY)
    # Another layout (the reply parent is not original-time there) gives the root another main key: skipped, counted.
    mv = pl.read_parquet(layout.message_version)
    other = tmp_path / "other.parquet"
    mv.with_columns(pl.when(pl.col("source_version_id") == svid(1)).then(pl.lit("H1")).otherwise(pl.col("time_grade")).alias("time_grade")).write_parquet(other)
    stats = sf.export(prompts, recording, other, tmp_path / "x.jsonl", registry=REGISTRY)
    assert stats["main_key_mismatch"] == 1 and stats["messages"] == 0


# ---------------------------------------------------------------- no fixture: byte-identical
def test_no_fixture_build_never_touches_symfill(tmp_path, monkeypatch):
    def hashes(tmp):
        layout, prompts, recording = lake(tmp, reply_lake() + [Msg(3, "仿写 625-630是好机会", 600, [open_action(symbol=None, side="short", lo=625, hi=630)])])
        summary, _ = build(layout, recording)
        summary.pop("paths")
        ex = pl.read_parquet(layout.extracted_event)
        cp, episodes = graph(layout, ex)
        frames = [hashlib.sha256(f.write_ipc(None).getvalue()).hexdigest() for f in (ex, cp, episodes)]
        return frames, layout.extracted_event.read_bytes(), json.dumps(summary, sort_keys=True, default=str), ex
    plain = hashes(tmp_path / "plain")
    assert not any("symfill" in k for k in json.loads(plain[2]))
    assert not any("symfill" in k for k in json.loads(plain[2])["llm"])
    assert not any(":symfill:" in v["version"] for v in plain[3]["extractor"].to_list())
    assert not any("symfill" in json.loads(c) for c in plain[3]["checks"].to_list())

    def boom(*args, **kwargs):
        raise AssertionError("symfill code ran without a fixture")
    for name in ("targets_for", "apply", "pools", "load_fixture", "version_tag", "context_for", "needs_symbol"):
        monkeypatch.setattr(sf, name, boom)
    guarded = hashes(tmp_path / "guarded")
    assert guarded[0] == plain[0] and guarded[1] == plain[1] and guarded[2] == plain[2]


def test_cli_passes_symfill_fixture(tmp_path, monkeypatch):
    captured = {}

    def fake_build(fixture, layout, **kwargs):
        captured.update(kwargs)
        return {}
    monkeypatch.setattr(api, "build", fake_build)
    path = tmp_path / "symfill.json"
    assert api.main(["--build", "--fixture", str(tmp_path), "--out", str(tmp_path / "out"), "--symfill-fixture", str(path)]) == 0
    assert captured["symfill_fixture"] == str(path)


def test_build_scopes_include_symfill_content_and_pass_the_validate_registry(tmp_path, monkeypatch):
    from quant_lab.data import dedup, normalize
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path / "data"))
    layout = Layout.flat(tmp_path / "lake").ensure()
    seen = []

    def fake_extract(work, **kwargs):
        seen.append((work, kwargs))
        return {}
    for module in (normalize, dedup, validate, linker, lifecycle):
        monkeypatch.setattr(module, "run", lambda *args, **kwargs: {})
    monkeypatch.setattr(extract, "run", fake_extract)
    path = tmp_path / "symfill.json"
    path.write_text(json.dumps(dict(version=sf.IMPORT_VERSION, items={})))
    api.build(tmp_path, layout, graph_version="none")
    api.build(tmp_path, layout, graph_version="one", symfill_fixture=path)
    api.build(tmp_path, layout, graph_version="same", symfill_fixture=path)
    path.write_text(json.dumps(dict(version=sf.IMPORT_VERSION, items={"k": {"abstain": {"reason_code": "INTENT_AMBIGUOUS", "note": "x"}}})))
    api.build(tmp_path, layout, graph_version="two", symfill_fixture=path)
    scopes = [work.silver_dir for work, _ in seen]
    assert scopes[0] != scopes[1] == scopes[2] != scopes[3]
    assert "symfill_fixture" not in seen[0][1] and "registry" not in seen[0][1]
    assert seen[1][1]["symfill_fixture"] == path and seen[1][1]["registry"].version == fixture_registry().version


def test_symfill_rows_run_through_cx_batch_with_their_own_rules_schema_and_context(tmp_path, monkeypatch):
    import sys
    layout, prompts, recording = lake(tmp_path, reply_lake())
    _, rows = export(tmp_path, layout, prompts, recording)
    script, capture = tmp_path / "fake-symfill-codex", tmp_path / "calls.jsonl"
    script.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
data = json.loads(sys.stdin.read())
with open(os.environ["CX_CAPTURE"], "a") as stream:
    stream.write(json.dumps({{"instructions": data["instructions"], "messages": data["messages"],
                             "schema": json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())}}) + "\\n")
items = [{{"key": m["key"], "schema_version": "cx.symfill.v1",
           "branches": [{{"branch_index": b["branch_index"], "symbol": "ROBO", "source": "reply",
                          "source_message_id": m["reply"]["message_id"], "evidence_quote": "ROBOUSDT.P"}} for b in m["branches"]]
                       + [{{"branch_index": 7, "symbol": "ETH", "source": "text", "source_message_id": None, "evidence_quote": "x"}}]}}
         for m in data["messages"]]
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps({{"items": items}}))
''')
    script.chmod(0o700)
    monkeypatch.setenv("CX_CAPTURE", str(capture))
    run = tmp_path / "run"
    report = cx.run_batches(tmp_path / "l4.jsonl", run, executable=str(script), batch_size=5, backoff=0)
    assert report["abstained"] == 0
    call = json.loads(capture.read_text().splitlines()[0])
    assert call["instructions"] == sf.RULES and call["schema"] == json.loads(json.dumps(sf.output_schema()))
    assert all("system" not in m and "schema_version" not in m and m["branches"] and m["reply"] for m in call["messages"])
    response = next(cx.read_jsonl(run / "responses.jsonl"))["response"]
    assert [c["status"] for c in response["checked"]] == ["ok"] and response["rejected"] == [dict(branch_index=7, reason="not_a_selected_branch")]
    assert cx.revalidate_raw(tmp_path / "l4.jsonl", run, tmp_path / "again")["missing"] == 0
    out = tmp_path / "symfill.json"
    cx.import_responses(run / "responses.jsonl", out)
    assert sf.load_fixture(out)["version"] == sf.IMPORT_VERSION
    summary, ex = build(layout, recording, out)
    assert summary["llm"]["symfill_applied"] == 1 and llm(ex, 2)[0]["symbol_raw"] == "ROBO"


def test_cli_finds_the_one_scoped_bronze(tmp_path, capsys):
    layout, prompts, recording = lake(tmp_path, reply_lake())
    root = tmp_path / "root"
    scoped = Layout.from_root(root).gold_dir.parent / "_build" / "abc123" / "bronze"
    scoped.mkdir(parents=True)
    (scoped / "message_version.parquet").write_bytes(layout.message_version.read_bytes())
    argv = ["export", "--prompts", str(prompts), "--recording", str(recording), "--lake-root", str(root),
            "--synthetic-registry", "--output", str(tmp_path / "l4.jsonl")]
    assert sf.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["message_version"] == str(scoped / "message_version.parquet")
    (scoped.parent.parent / "def456" / "bronze").mkdir(parents=True)
    (scoped.parent.parent / "def456" / "bronze" / "message_version.parquet").write_bytes(b"")
    with pytest.raises(SystemExit):
        sf.main(argv)


def test_cli_export_import_and_extract_round_trip(tmp_path, capsys):
    msgs = [Msg(1, "仿写 SOLUSDT.P 穿过 150", 0), Msg(2, "仿写 进场，防守140", 60, [open_action(symbol=None, side="long", stop=140)], reply=1)]
    layout, prompts, recording = lake(tmp_path, msgs)
    out = tmp_path / "l4.jsonl"
    assert sf.main(["export", "--prompts", str(prompts), "--recording", str(recording), "--build-dir", str(tmp_path / "lake"),
                    "--synthetic-registry", "--output", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["registry_version"] == fixture_registry().version
    rows = list(cx.read_jsonl(out))
    fixture = record(tmp_path, rows, {svid(2): [branch(0, "SOL", "reply", 1, "SOLUSDT.P 穿过 150")]})
    capsys.readouterr()
    assert extract.main(["--out", str(tmp_path / "lake"), "--llm-fixture", str(recording), "--symfill-fixture", str(fixture)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["llm"]["symfill_applied"] == 1
    ex = pl.read_parquet(layout.extracted_event).filter(pl.col("extractor").struct.field("name") == "llm")
    assert llm(ex, 2)[0]["symbol_raw"] == "SOL"
    with pytest.raises(SystemExit):
        sf.main(["export", "--prompts", str(prompts), "--recording", str(recording), "--build-dir", str(tmp_path / "nowhere"),
                 "--synthetic-registry", "--output", str(out)])
