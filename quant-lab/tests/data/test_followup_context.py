"""Synthetic conversation, targeting and mutation coverage; no network or real messages."""
import json
from datetime import timedelta

import polars as pl
import pytest

from quant_lab.data import cx_batch as cx, followup
from quant_lab.data.llm import RecordedClient, record_key
from test_followup import (
    CHANNEL, OTHER, T, atom, envelope, episode, event, follow_checks, instruction,
    message, mutant, restored, stop, terminal, v2_open,
)
from quant_lab.data.lake import Layout


def opening(source, root, days, symbol, side="long", text=None):
    when = T - timedelta(days=days)
    checks = json.loads(v2_open(symbol=symbol))
    checks["action"]["side"] = side
    raw = text if text is not None else f"仿写 {symbol} 开仓"
    return event(source, root, when, json.dumps(checks)), message(source, root, when, raw)


def prompt_for(opens, text, previous=(), *, episodes=(), terminals=(), reply=None):
    events = [pair[0] for pair in opens] + [event("follow", 999, T, follow_checks("reduce"))]
    messages = [pair[1] for pair in opens] + list(previous) + [message("follow", 999, T, text, reply=reply)]
    prompts = followup.plan_prompts_from_tables(messages, events, list(episodes), list(terminals), graph_version="synthetic-context")
    assert len(prompts) == 1
    return prompts[0]


def validate(prompt, *items):
    spec = followup.candidates_from_user(prompt["user"])
    return followup.validate_response(envelope(*items), prompt["text"], candidate_root_ids=spec["root_ids"],
                                      candidate_symbols=spec["symbols"], target_context=spec)


def pending(action, text, **kwargs):
    return instruction(None, action, text, symbol=None, uncertain=True, **kwargs)


def assert_target(result, target, uncertain=False):
    rows = result["instructions"]
    assert len(rows) == 1
    assert rows[0]["target_message_id"] == target
    assert rows[0]["uncertain"] is uncertain


def conversation_case():
    posts = [message(f"chat-{i}", i, T - timedelta(minutes=20-i), "仿写闲聊" + "闲" * 330) for i in range(1, 11)]
    posts[3]["message_type"] = "service"
    # A visible edit replaces text without counting as a second conversation post.
    edit = {**posts[4], "source_version_id": "edit-5", "version_no": 2, "available_at": T - timedelta(minutes=1), "text": "仿写编辑闲聊"}
    banned = [message("other", 50, T - timedelta(minutes=1), "异频道以太", channel=OTHER),
              message("future", 51, T + timedelta(minutes=1), "未来"),
              message("delayed", 52, T - timedelta(minutes=1), "迟到", available=T + timedelta(minutes=1)),
              message("equal", 53, T, "同刻"), message("own-old", 999, T - timedelta(minutes=1), "自身旧版本")]
    moment = {"channel_id": CHANNEL, "event_time": T, "message_date": T, "available_at": T, "message_id": 999}
    return posts + [edit] + banned, moment


def assert_conversation():
    posts, moment = conversation_case()
    rows = followup.visible_conversation(posts, moment)
    assert [row["message_id"] for row in rows] == list(range(3, 11))
    assert [row["minutes_before"] for row in rows] == list(range(17, 9, -1))
    assert rows[1]["text"].startswith("仿写闲聊")  # service/no-number chatter is retained
    assert rows[2]["text"] == "仿写编辑闲聊"
    assert max(len(row["text"]) for row in rows) == 300
    assert all(set(row) == {"message_id", "minutes_before", "text"} for row in rows)
    return rows


def test_conversation_eight_posts_three_hundred_chars_order_asof_and_chatter():
    assert_conversation()
    posts, moment = conversation_case()
    moment["available_at"] = None
    assert followup.visible_conversation(posts, moment) == []
    # An edited followup still sees only posts from before its original publication.
    moment.update(available_at=T + timedelta(minutes=5), event_time=T + timedelta(minutes=5))
    rows = followup.visible_conversation(posts, moment)
    assert 51 not in [row["message_id"] for row in rows]


@pytest.mark.parametrize("before,after", [
    ("rows[-MAX_CONVERSATION:]", "rows"),
    ("text[:MAX_MESSAGE_CHARS]", "text"),
    ("if published is None or published >= posted or not _clocks_visible(message, cutoff):", "if published is None:"),
    ('message.get("channel_id") != moment["channel_id"] or root is None or root == moment.get("message_id")', "root is None"),
    ('latest[root] = message', 'latest[root] = previous or message'),
    ('rows = sorted(latest.values(), key=', 'rows = sorted(latest.values(), reverse=True, key='),
], ids=["eight", "chars", "asof", "channel-and-own", "latest-edit", "chronology"])
def test_conversation_mutations(before, after):
    assert_conversation()
    fn = mutant(followup.visible_conversation, before, after)
    with pytest.raises(AssertionError):
        restored("visible_conversation", fn, assert_conversation)


@pytest.mark.parametrize("text", ["余下的止损推保本", "走一部分再推保本"])
def test_continuation_uses_previous_named_message_without_guessing_fraction(text):
    previous = [message("teacher", 90, T - timedelta(minutes=3), "仿写以太这笔继续观察，先前止损180减仓25%"),
                message("chat", 91, T - timedelta(minutes=2), "大家耐心，等通知")]
    prompt = prompt_for([opening("btc", 1, 1, "BTC"), opening("eth", 2, 2, "ETH")], text, previous)
    kept = validate(prompt, pending("move_stop", text, stop_obj=stop(to_entry=True, quote="保本")))
    assert_target(kept, 2)
    if "一部分" in text:
        reduced = validate(prompt, pending("reduce", "走一部分"))
        assert_target(reduced, 2)
        assert reduced["instructions"][0]["fraction_pct"] is None
    # Digits present only in earlier messages remain unusable.
    assert validate(prompt, pending("reduce", text, fraction=atom(25, "25%")))["instructions"] == []
    assert validate(prompt, pending("move_stop", text, stop_obj=stop((180, "180"), quote="180")))["instructions"] == []


def expansion_case():
    opens = [opening(f"base-{i}", i, i / 100, "SOL") for i in range(1, 14)]
    opens += [opening("eth-old", 100, 40, "以太"), opening("eth-older", 101, 50, "eth"),
              opening("bnb-old", 102, 60, "bnb"), opening("btc-outside", 103, 60 + 1/86400, "大饼"),
              opening("doge-closed", 104, 30, "DOGE"), opening("xrp-past", 105, 30, "XRP")]
    past = json.loads(opens[-1][0]["checks"])
    past["time_ref"] = past["action"]["time_ref"] = "past"
    opens[-1][0]["checks"] = json.dumps(past)
    previous = [message("teacher", 200, T - timedelta(minutes=1), "仿写 BNB 这笔记得管理")]
    return prompt_for(opens, "仿写以太提醒：多数人可止盈，大饼、DOGE、XRP也留意", previous,
                      episodes=[episode("closed", 104)], terminals=[terminal("closed", T - timedelta(days=1))])


def assert_expansion():
    prompt = expansion_case()
    user = json.loads(prompt["user"])
    assert user["candidate_root_ids"] == list(range(1, 13)) + [100, 102]
    eth, bnb = user["context"][-2:]
    assert eth["symbol"] == "ETH" and bnb["symbol"] == "BNB"
    assert eth["side"] == "long" and eth["entry"] == ["100"] and eth["stop"] == "90" and eth["tps"] == ["110"]
    assert eth["open_time"] == (T - timedelta(days=40)).isoformat()
    assert eth["has_visible_terminal"] is False
    return prompt


def test_expansion_recovers_old_named_opens_current_and_conversation_sixty_day_boundary():
    assert_expansion()
    for name in ("以太", "eth", "ETH", "大饼", "饼"):
        symbol = "ETH" if name in ("以太", "eth", "ETH") else "BTC"
        prompt = prompt_for([opening("old", 1, 40, symbol)], f"仿写{name}这笔止盈")
        assert_target(validate(prompt, pending("close_all", f"仿写{name}这笔止盈")), 1)


@pytest.mark.parametrize("before,after", [
    ('for text in mention_texts:', 'for text in mention_texts[:1]:'),
    ('days=60', 'days=59'),
    ('days=60', 'days=61'),
    ('latest = matching[0]', 'latest = matching[-1]'),
    ('chosen.append(latest)', 'pass'),
], ids=["conversation-mentions", "sixty-inclusive", "sixty-outside", "latest-symbol", "append"])
def test_expansion_mutations(before, after):
    assert_expansion()
    with pytest.raises(AssertionError):
        restored("visible_opens", mutant(followup.visible_opens, before, after), assert_expansion)


def assert_cap():
    opens = [opening(f"base-{i}", i, i/100, "SOL") for i in range(1, 13)]
    names = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ"]
    opens += [opening(name, 100+i, 40+i, name) for i, name in enumerate(names)]
    prompt = prompt_for(opens, "仿写 " + "、".join(names) + " 各自止盈")
    assert prompt["candidate_root_ids"] == list(range(1, 13)) + list(range(100, 108))


def test_candidate_cap_twenty_and_mutation():
    assert_cap()
    with pytest.raises(AssertionError):
        restored("visible_opens", mutant(followup.visible_opens, "if len(chosen) >= MAX_CANDIDATES:", "if False:"), assert_cap)


def assert_side_from_open():
    opens = [opening("short", 1, 2, "小火箭", None, "仿写小火箭开空，入场100止损110"),
             opening("long", 2, 1, "小火箭", None, "仿写小火箭开多，入场100止损90")]
    prompt = prompt_for(opens, "仿写小火箭这笔空单先止盈一半")
    assert [row["side"] for row in json.loads(prompt["user"])["context"]] == ["long", "short"]
    result = validate(prompt, pending("reduce", prompt["text"], fraction=atom(50, "一半")))
    assert_target(result, 1)
    assert result["instructions"][0]["fraction_pct"]["value"] == "50"


def test_missing_side_uses_exact_open_text_and_never_later_or_multisymbol_text():
    assert_side_from_open()
    opens = [opening("a", 1, 2, "小火箭", None, "仿写小火箭开仓"),
             opening("b", 2, 1, "小火箭", None, "仿写小火箭与ETH开空")]
    prompt = prompt_for(opens, "仿写小火箭这笔空单止盈", [message("chat", 90, T - timedelta(minutes=1), "仿写小火箭开空")])
    assert all(row["side"] is None for row in json.loads(prompt["user"])["context"])
    assert_target(validate(prompt, pending("close_all", prompt["text"])), None, True)


def test_side_fill_mutation():
    assert_side_from_open()
    with pytest.raises(AssertionError):
        restored("_context_row", mutant(followup._context_row, "side = _text_side(source_text)", "side = None"), assert_side_from_open)


@pytest.mark.parametrize("name,before,after", [
    ("_mentioned_symbols", "canonical_symbol(name)", "name.upper()"),
    ("_context_row", 'canonical_symbol(action.get("symbol_raw"))', 'action.get("symbol_raw")'),
], ids=["alias-normalization", "candidate-normalization"])
def test_symbol_normalization_mutations(name, before, after):
    assert_expansion()
    with pytest.raises(AssertionError):
        restored(name, mutant(getattr(followup, name), before, after), assert_expansion)


def test_expansion_uses_only_truncated_eight_message_context():
    opens = [opening("old", 1, 40, "ETH")]
    previous = [message("early", 100, T - timedelta(minutes=20), "仿写以太")]
    previous += [message(f"chat-{i}", 101+i, T - timedelta(minutes=10-i), "仿写闲聊" + "闲" * 300 + "以太") for i in range(8)]
    prompt = prompt_for(opens, "仿写这笔止盈", previous)
    assert prompt["candidate_root_ids"] == []
    assert len(json.loads(prompt["user"])["conversation"]) == 8


def assert_population():
    prompt = prompt_for([opening("eth", 1, 40, "ETH"), opening("older", 2, 50, "ETH")], "仿写以太提醒：80%的人要止盈")
    kept = validate(prompt, pending("close_all", prompt["text"]))
    assert_target(kept, 1)
    assert kept["instructions"][0]["fraction_pct"] is None
    guessed = validate(prompt, pending("reduce", prompt["text"], fraction=atom(80, "80%")))
    assert guessed["instructions"] == []


def test_population_percentage_is_not_position_fraction_and_mutation():
    assert_population()
    with pytest.raises(AssertionError):
        restored("_prove_fraction", mutant(followup._prove_fraction,
                 'if re.match(r"\\s*的?\\s*(?:人|用户|学员|交易者)", text[_span["end"]:]):', 'if False:'), assert_population)


def test_missing_time_or_direction_conflict_never_selects_recent():
    # Mixed sides with no stated side need the single latest open, so a missing time must not pick one.
    prompt = prompt_for([opening("a", 1, 2, "ETH", "long"), opening("b", 2, 1, "ETH", "short")], "仿写以太止盈")
    spec = followup.candidates_from_user(prompt["user"])
    for invalid in (None, "", "2024-07-31T00:00:00"):
        spec["context"][0]["open_time"] = invalid
        result = followup.validate_response(envelope(pending("close_all", prompt["text"])), prompt["text"],
                                            candidate_root_ids=spec["root_ids"], target_context=spec)
        assert_target(result, None, True)
    conflict = prompt_for([opening("a", 1, 2, "ETH", "long")], "仿写以太空单止盈")
    assert_target(validate(conflict, pending("close_all", conflict["text"])), None, True)


def assert_resolution(kind):
    opens = [opening("first", 1, 2, "ETH", "short"), opening("second", 2, 1, "ETH", "long")]
    previous = []
    if kind == "named":
        opens = [opening("btc", 1, 1, "BTC"), opening("eth", 2, 2, "ETH")]
        text, expected = "仿写以太这笔止盈", 2
    elif kind == "side":
        text, expected = "仿写eth空单止盈", 1
    elif kind == "latest":
        text, expected = "仿写以太止盈", 2
    elif kind == "same-side":
        # One-way netting: both ETH shorts are one position on the exchange, so both are targeted.
        opens = [opening("first", 1, 2, "ETH", "short"), opening("second", 2, 1, "ETH", "short")]
        text, expected = "仿写以太空单止盈", [2, 1]
    elif kind == "tie":
        opens = [opening("first", 1, 1, "ETH"), opening("second", 2, 1, "ETH")]
        text, expected = "仿写以太止盈", [1, 2]
    elif kind == "unknown":
        opens = [opening("first", 1, 2, "ETH", text="仿写开仓"), opening("second", 2, 1, "ETH", text="仿写开仓")]
        text, expected = "仿写这笔止盈", None
    elif kind == "multi-context":
        previous = [message("named", 90, T - timedelta(minutes=1), "仿写以太、大饼一起观察")]
        text, expected = "仿写这笔止盈", None
    else:
        raise AssertionError(kind)
    prompt = prompt_for(opens, text, previous)
    if isinstance(expected, list):
        result = validate(prompt, pending("close_all", text))
        assert sorted(row["target_message_id"] for row in result["instructions"]) == sorted(expected)
        assert all(row["uncertain"] is False for row in result["instructions"])
        return
    # A proposed candidate id is still insufficient evidence when scope is ambiguous.
    draft = pending("close_all", text) if expected is not None else instruction(1, "close_all", text, symbol="ETH")
    assert_target(validate(prompt, draft), expected, expected is None)


@pytest.mark.parametrize("kind", ["named", "side", "latest", "same-side", "tie", "unknown", "multi-context"])
def test_named_direction_latest_and_ambiguity_counterexamples(kind):
    assert_resolution(kind)


def test_netting_skips_finished_opens_and_mixed_sides_keep_latest():
    opens = [opening("first", 1, 3, "ETH", "short"), opening("second", 2, 2, "ETH", "short"), opening("third", 3, 1, "ETH", "long")]
    # Mixed sides and no stated side: still only the single latest open.
    assert_target(validate(prompt_for(opens, "仿写以太止盈"), pending("close_all", "仿写以太止盈")), 3, False)
    # Stated side: every live open of that side.
    result = validate(prompt_for(opens, "仿写以太空单走了"), pending("close_all", "仿写以太空单走了"))
    assert sorted(row["target_message_id"] for row in result["instructions"]) == [1, 2]


@pytest.mark.parametrize("name,before,after,kind", [
    ("_resolve_targets", "elif len(named) == 1:", "elif False:", "named"),
    ("_resolve_targets", "if side is not None:", "if False:", "side"),
    ("_resolve_targets", "elif len(sides) == 1 and live:", "elif False:", "same-side"),
    ("_resolve_targets", "if recent is not None:", "if False:", "latest"),
    ("_resolve_targets", "live = [root for root in matched_roots", "live = [root for root in matched_roots[:1]", "tie"),
    ("_resolve_targets", "selected = []\n    if bulk:", 'selected = [context[0]["root_message_id"]] if context else []\n    if bulk:', "unknown"),
], ids=["named", "side", "same-side-ambiguous", "latest", "time-tie", "no-guess"])
def test_targeting_mutations(name, before, after, kind):
    body = lambda: assert_resolution(kind)
    body()
    with pytest.raises(AssertionError):
        restored(name, mutant(getattr(followup, name), before, after), body)


def assert_bulk(side=False):
    opens = [opening("a", 1, 3, "BTC"), opening("b", 2, 2, "ETH"), opening("c", 3, 1, "BNB", "short")]
    text = "仿写所有多单推保本" if side else "仿写手里的都走一半"
    prompt = prompt_for(opens, text)
    draft = pending("move_stop", text, stop_obj=stop(to_entry=True, quote="保本")) if side else pending("reduce", text, fraction=atom(50, "一半"))
    result = validate(prompt, draft, draft)  # no repeated fanout from duplicate model items
    expected = [2, 1] if side else [3, 2, 1]
    assert [row["target_message_id"] for row in result["instructions"]] == expected
    assert all(row["uncertain"] is False for row in result["instructions"])
    assert [row["target_symbol"] for row in result["instructions"]] == (["ETH", "BTC"] if side else ["BNB", "ETH", "BTC"])


def test_collective_scope_fans_out_to_matching_candidates_and_undefined_scope_stays_uncertain():
    assert_bulk()
    assert_bulk(True)
    prompt = prompt_for([opening("a", 1, 2, "BTC"), opening("b", 2, 1, "ETH")], "仿写之前那几笔走一半")
    assert_target(validate(prompt, pending("reduce", prompt["text"], fraction=atom(50, "一半"))), None, True)


@pytest.mark.parametrize("before,after,side", [
    ('if bulk:', 'if False:', False),
    ('and (side is None or row.get("side") == side)', '', True),
    ('selected = list(dict.fromkeys(row["root_message_id"] for row in matched))', 'selected = [matched[0]["root_message_id"]]', False),
], ids=["scope", "direction-filter", "fanout"])
def test_collective_mutations(before, after, side):
    body = lambda: assert_bulk(side)
    body()
    with pytest.raises(AssertionError):
        restored("_resolve_targets", mutant(followup._resolve_targets, before, after), body)


def assert_terminal_ambiguity():
    prompt = prompt_for([opening("a", 1, 2, "ETH")], "仿写以太止盈",
                        episodes=[episode("closed", 1), episode("open", 1)], terminals=[terminal("closed", T - timedelta(days=1))])
    assert json.loads(prompt["user"])["context"][0]["has_visible_terminal"] is True
    assert_target(validate(prompt, pending("close_all", prompt["text"])), None, True)


def test_visible_terminal_disagreement_stays_uncertain_and_mutation():
    assert_terminal_ambiguity()
    with pytest.raises(AssertionError):
        restored("_resolve_targets", mutant(followup._resolve_targets,
                 'if any(row.get("has_visible_terminal", False) for row in matched if row["root_message_id"] in selected):',
                 'if False:'), assert_terminal_ambiguity)


def assert_runner_context():
    prompt = prompt_for([opening("a", 1, 2, "ETH", "short"), opening("b", 2, 1, "ETH")], "仿写以太空单止盈")
    spec = followup.candidates_from_user(prompt["user"])
    result = cx.quote_response(envelope(pending("close_all", prompt["text"])), prompt["text"], candidates=spec, expected_schema=followup.SCHEMA_NAME)
    assert_target(result["response"], 1)
    outsider = cx.quote_response(envelope(instruction(777, "close_all", prompt["text"], symbol="ETH")), prompt["text"], candidates=spec)
    assert outsider["response"]["instructions"] == []
    assert outsider["response"]["stats"]["reject_reasons"][0]["reason"] == "target_not_in_candidates"


def test_runner_passes_context_and_rejects_candidate_outside_id_and_mutation():
    assert_runner_context()
    with pytest.raises(AssertionError):
        restored_runner = mutant(cx.quote_response,
            'target_context=candidates if isinstance(candidates, dict) and "context" in candidates else None', 'target_context=None')
        original = cx.quote_response
        cx.quote_response = restored_runner
        try:
            assert_runner_context()
        finally:
            cx.quote_response = original


NEW_RULE_SENTENCES = (
    "规则版本：followup-v2；schema 仍为 followup-v1。",
    "conversation 是同频道发布前最近 8 条消息，远到近，每条最多 300 字；只用于指向，不作数字证据。",
    "context 是 21 天内最近 12 条开仓，加上当前 text 或 conversation 点名币种在 60 天内最近一条开仓，总数最多 20。",
    "币种与别名统一按归一 symbol 匹配，例如以太/eth=ETH、大饼/饼=BTC。当前指令的点名优先于前文；当前未点名时只用最近一条明确点名的前文，不跨多个币种猜接续关系。",
    "明确点名后，该币种只有一条候选，或原文方向一致的候选只有一条，可以指向它，uncertain=false。",
    "明确写了方向却有两条同名同向候选时仍 uncertain=true；side=null 不能当作方向吻合。",
    "未写方向时，同名多候选取开仓时间唯一最近的一条，uncertain=false；时间缺失或最近时间并列则仍 uncertain=true。",
    "「所有多单保本」「手里的都走一半」等范围明确的笼统指令可以输出多条，各指向范围内候选；范围无法界定时 target_message_id=null、uncertain=true。",
    "has_visible_terminal=true 的候选存在终态分歧，不作确定指向；无点名、无明确全体范围且无可见候选回复目标时仍 uncertain=true。",
    "百分数修饰人群（例如「80%的人要止盈」）不是仓位比例，不写 fraction_pct。",
)


@pytest.mark.parametrize("sentence", NEW_RULE_SENTENCES)
def test_new_prompt_rules_and_removal_mutations(sentence):
    def contract(rules):
        assert sentence in rules
    contract(followup.RULES)
    assert followup.RULES.count(sentence) == 1
    with pytest.raises(AssertionError):
        contract(followup.RULES.replace(sentence, "", 1))


def test_rule_version_is_in_record_key_and_old_recording_misses(tmp_path):
    prompt = prompt_for([opening("a", 1, 2, "ETH")], "仿写以太止盈")
    user = json.loads(prompt["user"])
    assert followup.RULE_VERSION == "followup-v2"
    assert user["rule_version"] == followup.RULE_VERSION
    assert user["schema_version"] == "followup-v1"
    # Hold all data/schema constant: changing the rule marker alone invalidates replay.
    old_user = {**user, "rule_version": "followup-v1"}
    old_system = prompt["system"].replace("规则版本：followup-v2", "规则版本：followup-v1")
    old_key = record_key(old_system, followup._dumps_user(old_user), followup.SCHEMA_NAME)
    assert old_key != prompt["key"]
    recorded = tmp_path / "old.json"
    recorded.write_text(json.dumps({"version": "followup-v1", "items": {old_key: {"response": envelope(pending("close_all", prompt["text"]))}}}))
    client = RecordedClient.from_file(recorded)
    with pytest.raises(KeyError):
        client.complete_json(system=prompt["system"], user=prompt["user"], schema_name=followup.SCHEMA_NAME)


def test_contextual_replay_recovers_target_and_revalidates_numeric_quotes(tmp_path):
    opened = opening("old", 1, 40, "eth")
    text = "仿写余下推保本"
    teacher = message("teacher", 90, T - timedelta(minutes=2), "仿写以太这笔继续，先前止损180减仓25%")
    prompt = prompt_for([opened], text, [teacher])
    layout = Layout.flat(tmp_path / "lake").ensure()
    gv = "synthetic-context"
    pl.DataFrame([opened[1], teacher, message("follow", 999, T, text)]).write_parquet(layout.message_version)
    pl.DataFrame([opened[0], event("follow", 999, T, follow_checks("reduce"))]).write_parquet(layout.extracted_event)
    pl.DataFrame([episode("e1", 1)]).write_parquet(layout.episode(gv))
    pl.DataFrame(schema={"episode_id": pl.String}).write_parquet(layout.episode_event(gv))
    fixture = tmp_path / "new.json"
    response = envelope(pending("move_stop", text, stop_obj=stop(to_entry=True, quote="保本")),
                        pending("reduce", text, fraction=atom(25, "25%")))
    fixture.write_text(json.dumps({"version": "followup-v1", "items": {prompt["key"]: {"response": response}}}))
    report = followup.build_actions(layout, gv, fixture)
    assert report["matched"] == 1 and report["instructions"] == 1 and report["rejected"] == 1
    row = pl.read_parquet(layout.silver_dir / "followup_action.parquet").row(0, named=True)
    assert row["target_message_id"] == 1 and row["episode_id"] == "e1" and row["uncertain"] is False
    assert row["to_entry"] is True and row["stop_price"] is None and row["rule_version"] == "followup-v2"


def assert_numeric_source_is_current():
    prompt = prompt_for([opening("old", 1, 40, "ETH")], "仿写走一部分，余下推保本",
                        [message("teacher", 90, T - timedelta(minutes=1), "仿写以太先前减仓25%，止损180")])
    assert validate(prompt, pending("reduce", "走一部分", fraction=atom(25, "25%")))["instructions"] == []
    assert validate(prompt, pending("move_stop", "余下推保本", stop_obj=stop((180, "180"), quote="180")))["instructions"] == []


def test_context_numbers_cannot_become_current_evidence_mutation():
    assert_numeric_source_is_current()
    fn = mutant(followup.validate_response, '_clean_instruction(raw, text, roots, prices, symbols)',
                '_clean_instruction(raw, text + "".join(row["text"] for row in (target_context or {}).get("conversation", [])), roots, prices, symbols)')
    with pytest.raises(AssertionError):
        restored("validate_response", fn, assert_numeric_source_is_current)


def test_candidate_membership_with_rich_context_mutation():
    assert_runner_context()
    fn = mutant(followup._clean_instruction, 'if target is not None and target not in candidate_root_ids:', 'if False:')
    with pytest.raises(AssertionError):
        restored("_clean_instruction", fn, assert_runner_context)


def assert_undefined_scope():
    prompt = prompt_for([opening("a", 1, 2, "BTC"), opening("b", 2, 1, "ETH")], "仿写之前那几笔走一半")
    assert_target(validate(prompt, pending("reduce", prompt["text"], fraction=atom(50, "一半"))), None, True)


def test_undefined_collective_scope_mutation():
    assert_undefined_scope()
    with pytest.raises(AssertionError):
        restored("_target_scope", mutant(followup._target_scope, 'bool(UNDEFINED_SCOPE_RE.search(scope))', 'False'), assert_undefined_scope)
