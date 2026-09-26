"""Only fabricated messages and fake Codex; executable mutants accompany assertions."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import inspect
import json
import sys
import textwrap
import polars as pl
import pytest
from quant_lab.data import api, cx_batch as cx, extract, lifecycle, linker, normalize, plan_source
from quant_lab.data.lake import Layout
from quant_lab.data.llm import Abstention, RecordedClient, SCHEMA_NAME_EXTRACT, build_extract_prompt, record_key, validate_evidence

T0 = datetime(2024, 7, 1, tzinfo=UTC)
CHANNEL, OTHER = -1000000000701, -1000000000702
TEXT = "仿写 BTC 做多，入场 100，止损 90，止盈 110、120。另记观察位 88。"


def mutant(function, before, after):
    source = textwrap.dedent(inspect.getsource(function))
    assert source.count(before) == 1
    namespace = dict(function.__globals__)
    exec(compile(source.replace(before, after), "<cx-mutant>", "exec"), namespace)
    return namespace[function.__name__]


def prompt(i=0):
    system, user = build_extract_prompt(TEXT, channel_name="仿写频道", message_date=(T0 + timedelta(minutes=i)).isoformat())
    return dict(key=record_key(system, user, SCHEMA_NAME_EXTRACT), system=system, user=user, schema_name=SCHEMA_NAME_EXTRACT,
                source_version_id=f"s{i}", channel_id=CHANNEL, text=TEXT, has_image=False, previous_text=None)


def item():
    return dict(key="key", kind="entry_proposal", symbol_raw="BTC", side="long", entry=dict(lo=100, hi=100, kind="limit"),
                entries=[], stop=90, tps=[dict(level=110), dict(level=120)], reason_codes=[], op="open", refers_to_previous=False,
                quotes=[dict(field="entry.lo", quote="入场 100"), dict(field="entry.hi", quote="入场 100"),
                        dict(field="stop", quote="止损 90"), dict(field="tps", quote="110、120"), dict(field="tps", quote="110、120")])


def prompts(path, n=5):
    rows = [prompt(i) for i in range(n)]
    path.write_text("".join(cx.dumps(r) + "\n" for r in rows))
    return rows


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    script, capture = tmp_path / "fake-codex", tmp_path / "calls.jsonl"
    script.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys, time
args = sys.argv[1:]
assert args[:5] == ["exec", "-m", "gpt-6-astra", "-s", "read-only"]
assert "--skip-git-repo-check" in args and "--ephemeral" in args and args[-1] == "-"
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
assert "actions" in schema["properties"]["items"]["items"]["properties"]
assert "spans" not in schema["properties"]["items"]["items"]["properties"]
data = json.loads(sys.stdin.read())
with open(os.environ["CX_CAPTURE"], "a") as stream:
    stream.write(json.dumps({{"keys": [r["key"] for r in data["messages"]], "at": time.time()}}) + "\\n")
mode = os.environ.get("CX_MODE", "ok")
if mode == "slow": time.sleep(0.25)
print("tokens used\\n1,234", file=sys.stderr)
if mode == "fail": sys.exit(3)
if mode == "timeout": time.sleep(2)
items = []
for message in data["messages"]:
    result = {item()!r}
    result["key"] = message["key"]
    if mode == "bad_quote": result["quotes"][2]["quote"] = "不存在 90"
    if message["schema_name"] == "cx.actions.v2":
        atom = lambda value, quote: {{"value": str(value), "quote": quote}}
        action = {{"op": "open", "time_ref": "now", "symbol_raw": "BTC", "side": "long",
                   "entry": {{"kind": "limit", "price": atom(100, "入场 100"), "lo": None, "hi": None, "levels": []}},
                   "stop": {{"kind": "price", "price": atom(90, "止损 90"), "condition": None}},
                   "tps": [{{"kind": "price", "value": atom(v, str(v))}} for v in (110, 120)], "field_issues": []}}
        result = {{"key": message["key"], "schema_version": 2, "actions": [action]}}
    items.append(result)
if mode == "missing": items = items[:-1]
if mode == "duplicate" and len(items) > 1: items[-1] = items[0]
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps({{"items": items}}))
''')
    script.chmod(0o700)
    monkeypatch.setenv("CX_CAPTURE", str(capture))
    return script, capture


def calls(path):
    return list(cx.read_jsonl(path)) if path.exists() else []


def test_batches_resume_journal_and_mutants(tmp_path, fake_codex):
    script, capture = fake_codex
    path, output = tmp_path / "prompts", tmp_path / "run"
    prompts(path)
    report = cx.run_batches(path, output, executable=str(script), batch_size=2)
    assert sorted(len(c["keys"]) for c in calls(capture)) == [1, 2, 2]
    assert report["calls"] == 3 and report["tokens_used"] == 3702
    assert len(list((output / "raw").glob("*.json"))) == 6
    (output / "responses.jsonl").unlink()  # crash after journal commit
    def invariant(runner):
        before = len(calls(capture))
        result = runner(path, output, executable=str(script), batch_size=2)
        assert result["calls"] == 0 and len(calls(capture)) == before
        assert len(list(cx.read_jsonl(output / "responses.jsonl"))) == 5
    invariant(cx.run_batches)
    with pytest.raises(AssertionError):
        invariant(mutant(cx.run_batches, 'if k not in completed]', 'if True]'))
    broken = mutant(cx.length_batches, 'len(batch) >= batch_size or size + length > max_chars', 'False')
    with pytest.raises(AssertionError):
        assert len(list(broken(list(cx.read_jsonl(path)), 2))) == 3


@pytest.mark.parametrize("mode", ["fail", "missing", "duplicate", "timeout"])
def test_retries_and_mutant(tmp_path, fake_codex, monkeypatch, mode):
    script, _ = fake_codex
    monkeypatch.setenv("CX_MODE", mode)
    path = tmp_path / "prompts"
    prompts(path, 2)
    def invariant(directory):
        result = cx.run_batches(path, directory, executable=str(script), batch_size=2, timeout=0.15 if mode == "timeout" else 5)
        assert result["calls"] == 3 and result["abstained"] == 2
        assert all("abstain" in r for r in cx.read_jsonl(directory / "responses.jsonl"))
    invariant(tmp_path / "ok")
    monkeypatch.setattr(cx, "_run_batch", mutant(cx._run_batch, 'range(retries + 1)', 'range(1)'))
    with pytest.raises(AssertionError):
        invariant(tmp_path / "mutant")


def test_concurrency_and_mutant(tmp_path, fake_codex, monkeypatch):
    script, capture = fake_codex
    monkeypatch.setenv("CX_MODE", "slow")
    path = tmp_path / "prompts"
    prompts(path, 3)
    def invariant(runner, directory):
        capture.unlink(missing_ok=True)
        runner(path, directory, executable=str(script), batch_size=1, concurrency=3)
        times = sorted(float(c["at"]) for c in calls(capture))
        assert times[-1] - times[0] < 0.4
    invariant(cx.run_batches, tmp_path / "ok")
    with pytest.raises(AssertionError):
        invariant(mutant(cx.run_batches, 'ThreadPoolExecutor(max_workers=concurrency)', 'ThreadPoolExecutor(max_workers=1)'), tmp_path / "mutant")


def test_quotes_exact_spans_all_tps_and_mutant():
    def invariant(convert):
        result = convert(item(), TEXT)["response"]
        assert validate_evidence(result, TEXT) == []
        assert [TEXT[s["start"]:s["end"]] for s in result["spans"]] == ["100", "100", "90", "110", "120"]
        assert result["op"] == "open" and result["refers_to_previous"] is False
    invariant(cx.quote_response)
    broken = mutant(cx.quote_response, 'a, b = start + match.start(), start + match.end()', 'a, b = match.start(), match.end()')
    with pytest.raises((AssertionError, KeyError)):
        invariant(broken)


@pytest.mark.parametrize("change", ["missing_quote", "missing_number", "partial_number", "schema", "previous", "missing_tp"])
def test_quote_fail_closed_and_mutants(change):
    value = item()
    if change == "missing_quote": value["quotes"][2]["quote"] = "从未出现 90"
    elif change == "missing_number": value["quotes"][2]["quote"] = "入场 100"
    elif change == "partial_number":
        value["entry"]["lo"] = value["entry"]["hi"] = 10
        value["quotes"][0]["quote"] = value["quotes"][1]["quote"] = "10"
    elif change == "schema": value["side"] = "both"
    elif change == "previous": value["refers_to_previous"] = True
    else: value["quotes"].pop()
    def invariant(convert):
        assert "abstain" in convert(value, TEXT)
    invariant(cx.quote_response)
    broken = mutant(cx.quote_response, 'return abstain("evidence_rejected:invalid_schema_or_quote")', 'return {"response": raw}')
    with pytest.raises(AssertionError):
        invariant(broken)


def test_import_replay_revalidation_and_mutants(tmp_path, fake_codex, monkeypatch):
    script, _ = fake_codex
    path, fixture = tmp_path / "prompts", tmp_path / "fixture.json"
    rows = prompts(path, 1)
    cx.run_batches(path, tmp_path / "run", executable=str(script))
    args = {k: rows[0][k] for k in ("system", "user", "schema_name")}
    def invariant(importer):
        importer(tmp_path / "run" / "responses.jsonl", fixture)
        client = RecordedClient.from_file(fixture)
        assert client.has(**args)
        assert client.complete_json(**args).payload["stop"] == "90"
        assert extract.llm_extract(TEXT, client=client, channel_name="仿写频道", message_date=T0.isoformat())[0].checks["llm_evidence_valid"]
    invariant(cx.import_responses)
    with pytest.raises(AssertionError):
        invariant(mutant(cx.import_responses, 'key = row["key"]', 'key = "incorrect"'))
    monkeypatch.setenv("CX_MODE", "bad_quote")
    cx.run_batches(path, tmp_path / "bad", executable=str(script))
    cx.import_responses(tmp_path / "bad" / "responses.jsonl", fixture)
    assert isinstance(RecordedClient.from_file(fixture).complete_json(**args), Abstention)
    rec = cx.quote_response(item(), TEXT)
    rec["response"]["stop"] = 999
    client = RecordedClient({rows[0]["key"]: rec})
    def evidence_invariant():
        pr, ab, _ = extract.llm_extract(TEXT, client=client, channel_name="仿写频道", message_date=T0.isoformat())
        assert pr is None and ab is not None
    evidence_invariant()
    monkeypatch.setattr(extract, "validate_evidence", lambda *_: [])
    with pytest.raises(AssertionError):
        evidence_invariant()


def synthetic_lake(tmp_path):
    layout = Layout.flat(tmp_path / "lake").ensure()
    rows = []
    for i, (channel, text, kind, mid, reply, version, minute) in enumerate([
        (CHANNEL, TEXT, "message", 1, None, 1, 0),
        (CHANNEL, "仿写 BTC 已全部平仓", "message", 2, 1, 1, 2),
        (CHANNEL, "仿写 BTC 入场 101 止损 91 做多", "message", 1, None, 2, 5),
        (CHANNEL, "仿写加入通知", "service", 3, None, 1, 6),
        (OTHER, "   ", "message", 1, None, 1, 0),
        (OTHER, "仿写今天只讨论行情", "message", 2, None, 1, 1),
        (OTHER, TEXT, "message", 3, None, 1, 2),
    ]):
        at = T0 + timedelta(minutes=minute)
        rows.append(dict(source_id=dict(peer_id=channel, message_id=mid), channel_id=channel, channel_name=f"仿写频道{channel}",
                         source_version_id=f"s{i}", text=text, message_type=kind, message_date=at, event_time=at, available_at=at,
                         ingested_at=at, version_no=version, sequence=i, reply_to_message_id=reply,
                         media_kinds=["photo"] if i == 1 else [], media_hashes=[], reason_codes=[],
                         batch_id="tg-synthetic", content_hash=f"h{i}", time_grade="H0"))
    mv = pl.DataFrame(rows, schema=normalize.MESSAGE_VERSION_SCHEMA)
    mv.write_parquet(layout.message_version)
    return layout, mv


def test_export_versions_strata_context_and_mutants(tmp_path, monkeypatch):
    layout, _ = synthetic_lake(tmp_path)
    path = tmp_path / "prompts"
    result = cx.export_prompts(layout, path)
    assert result == dict(input=7, skipped_service=1, skipped_empty=1, eligible=5, exported=5, unique_keys=5, seed=0)
    rows = list(cx.read_jsonl(path))
    assert {"s0", "s2"}.issubset({r["source_version_id"] for r in rows})
    assert next(r for r in rows if r["source_version_id"] == "s1")["has_image"]
    def context_invariant(exporter):
        exporter(layout, path)
        row = next(r for r in cx.read_jsonl(path) if r["source_version_id"] == "s1")
        assert row["previous_text"] == TEXT
    context_invariant(cx.export_prompts)
    with pytest.raises(AssertionError):
        context_invariant(mutant(cx.export_prompts, 'p["available_at"] <= cutoff', 'True'))
    def sample_invariant():
        cx.export_prompts(layout, path, sample=2, seed=7)
        assert {r["channel_id"] for r in cx.read_jsonl(path)} == {CHANNEL, OTHER}
    sample_invariant()
    monkeypatch.setattr(cx, "stratified", lambda rows, n, seed, key: rows[:n])
    with pytest.raises(AssertionError):
        sample_invariant()


def canonical_pair(tmp_path):
    from quant_lab.data import validate
    layout, mv = synthetic_lake(tmp_path)
    ex, *_ = extract.extract_frame(mv, None, ingested_at=T0)
    ex.write_parquet(layout.extracted_event)
    validate.run(layout, synthetic=True, ingested_at=T0)
    cp = pl.read_parquet(layout.canonical_plan)
    llm_rows = []
    for row in cp.to_dicts():
        if row["source_version_id"] not in ("s0", "s1"): continue
        r = deepcopy(row)
        r.update(plan_id="llm-" + r["plan_id"], extract_id="llm-" + r["extract_id"], extractor_name="llm")
        r["checks"] = json.dumps({**json.loads(r["checks"]), "llm_evidence_valid": True})
        if r["source_version_id"] == "s0": r["stop"] = Decimal(88)
        llm_rows.append(r)
    llm_ex = []
    for row in ex.to_dicts():
        if row["source_version_id"] not in ("s0", "s1"):
            continue
        row["extract_id"] = "llm-" + row["extract_id"]
        row["extractor"] = dict(name="llm", version="test", model="fake")
        row["checks"] = '{"llm_evidence_valid":true}'
        if row["source_version_id"] == "s0":
            row["stop"] = Decimal(88)
        llm_ex.append(row)
    ex = pl.concat([ex, pl.DataFrame(llm_ex, schema=ex.schema)])
    ex.write_parquet(layout.extracted_event)
    return layout, mv, ex, pl.concat([cp, pl.DataFrame(llm_rows, schema=cp.schema)])


def graph(cp, mv, ex, mode=None):
    kwargs = {} if mode is None else dict(plan_source=mode)
    edges, decisions, _ = linker.build_candidates(cp, mv, ex, ingested_at=T0, **kwargs)
    return lifecycle.build_graph(cp, mv, edges, decisions, None, graph_version="test", ingested_at=T0,
                                 observation_end=T0 + timedelta(days=1), **kwargs)


def test_plan_sources_linking_disagreements_default_and_mutants(tmp_path, monkeypatch):
    _, mv, ex, cp = canonical_pair(tmp_path)
    default, parser = graph(cp, mv, ex), graph(cp, mv, ex, "parser")
    assert default[0].equals(parser[0]) and default[1].equals(parser[1])
    def invariant():
        for mode, stop, source in [("parser", 90, "parser"), ("llm", 88, "llm"), ("reconciled", 88, "llm")]:
            episodes, events, _, _, report = graph(cp, mv, ex, mode)
            ep = episodes.filter(pl.col("root_source_version_id") == "s0").row(0, named=True)
            assert ep["order_plan"]["stop"]["price"] == stop
            assert ep["n_events"] >= 2
            assert report["episode_sources"][ep["episode_id"]]["root"] == source
            assert report["n_disagreements"] == 1 and ep["episode_id"] in report["disagreement_episode_ids"]
            assert ("s6" in set(episodes["root_source_version_id"])) == (mode != "llm")
    invariant()
    monkeypatch.setattr(plan_source, "select_plans", mutant(plan_source.select_plans, 'if plan_source == "parser":', 'if True:'))
    with pytest.raises(AssertionError):
        invariant()


def test_reconciled_abstention_and_nonopening_override_mutants(tmp_path, monkeypatch):
    _, _, _, cp = canonical_pair(tmp_path)
    rows = cp.to_dicts()
    for row in rows:
        if row["extractor_name"] == "llm" and row["source_version_id"] == "s0":
            row.update(kind="undecidable", checks="{}")
    changed = pl.DataFrame(rows, schema=cp.schema)
    def invariant():
        root = plan_source.select_plans(changed, "reconciled").filter(pl.col("source_version_id") == "s0").row(0, named=True)
        assert root["extractor_name"] == "parser"
    invariant()
    with monkeypatch.context() as patch:
        patch.setattr(plan_source, "llm_eligible", lambda r: r["extractor_name"] == "llm")
        with pytest.raises(AssertionError): invariant()
    for row in rows:
        if row["extractor_name"] == "llm" and row["source_version_id"] == "s0":
            row.update(kind="analysis", checks='{"llm_evidence_valid":true}')
    changed = pl.DataFrame(rows, schema=cp.schema)
    def nonopening_invariant():
        selected = plan_source.select_plans(changed, "reconciled")
        assert selected.filter((pl.col("source_version_id") == "s0") & (pl.col("kind") == "entry_proposal")).height == 0
    nonopening_invariant()
    monkeypatch.setattr(plan_source, "select_plans", mutant(plan_source.select_plans, 'and r["source_version_id"] not in approved)', 'and True)'))
    with pytest.raises(AssertionError): nonopening_invariant()


def test_disagreement_fields_and_mutant(tmp_path):
    _, _, _, cp = canonical_pair(tmp_path)
    for field in ("side", "stop", "entry", "entries"):
        rows = cp.to_dicts()
        parser = next(r for r in rows if r["source_version_id"] == "s0" and r["extractor_name"] == "parser")
        for row in rows:
            if row["source_version_id"] == "s0" and row["extractor_name"] == "llm":
                row["stop"] = parser["stop"]
                row[field] = dict(side="short", stop=Decimal(88), entry=dict(lo=Decimal(101), hi=Decimal(101), kind="limit"), entries=[Decimal(101)])[field]
        assert plan_source.disagreements(pl.DataFrame(rows, schema=cp.schema)) == {"s0": [field]}
    broken = mutant(plan_source.disagreements, 'if values[0] != values[1]:', 'if False:')
    with pytest.raises(AssertionError): assert broken(cp) == {"s0": ["stop"]}


def test_review_snapshot_strata_and_mutant(tmp_path, monkeypatch):
    layout, mv, ex, cp = canonical_pair(tmp_path)
    cx.save_build_review(layout, "v1", cp, mv, graph(cp, mv, ex, "reconciled")[0], "reconciled")
    mv.with_columns(pl.lit("后续构建内容").alias("text")).write_parquet(layout.message_version)
    path = tmp_path / "review.jsonl"
    def invariant():
        result = cx.review_sample(layout, "v1", path, sample=4, seed=2)
        rows = list(cx.read_jsonl(path))
        assert result["exported"] == 4
        assert {(r["channel_id"], r["is_open"]) for r in rows} == {(CHANNEL, True), (CHANNEL, False), (OTHER, True), (OTHER, False)}
        assert all("后续构建内容" != r["text"] and "parser_results" in r and "llm_results" in r for r in rows)
    invariant()
    monkeypatch.setattr(cx, "stratified", lambda rows, n, seed, key: rows[:n])
    with pytest.raises(AssertionError): invariant()


def test_api_cli_mode_forwarding_and_mutant(tmp_path, monkeypatch):
    seen = []
    def fake_build(*args, **kwargs):
        seen.append(kwargs["plan_source"])
        return {}
    monkeypatch.setattr(api, "build", fake_build)
    def invariant(main):
        seen.clear()
        for mode in ("parser", "llm", "reconciled"):
            main(["--build", "--fixture", str(tmp_path), "--out", str(tmp_path / "out"), "--plan-source", mode])
        main(["--build", "--fixture", str(tmp_path), "--out", str(tmp_path / "out")])
        assert seen == ["parser", "llm", "reconciled", "parser"]
    invariant(api.main)
    with pytest.raises(AssertionError): invariant(mutant(api.main, 'plan_source=a.plan_source', 'plan_source="parser"'))


def test_export_fake_run_import_api_build_and_nonopening_mutant(tmp_path, fake_codex, monkeypatch):
    """The real offline API, including canonicalization dropping non-signal LLM rows."""
    script, _ = fake_codex
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("result.json").write_text(json.dumps({"id": 701, "type": "public_channel", "name": "仿写频道",
        "messages": [{"id": 1, "type": "message", "date": T0.isoformat(), "date_unixtime": str(int(T0.timestamp())), "text": TEXT}]}))
    layout = Layout.flat(tmp_path / "build")
    api.build(source, layout, graph_version="baseline", ingested_at=T0)
    path, fixture = tmp_path / "prompts", tmp_path / "fixture.json"
    cx.export_prompts(layout, path)
    cx.run_batches(path, tmp_path / "run", executable=str(script))
    cx.import_responses(tmp_path / "run" / "responses.jsonl", fixture)
    result = api.build(source, layout, graph_version="llm", llm_fixture=fixture, plan_source="llm", ingested_at=T0)
    assert result["extract"]["llm"]["rows"] == 1
    assert result["lifecycle"]["n_episodes"] == 1
    assert set(r["root"] for r in result["lifecycle"]["episode_sources"].values()) == {"llm"}
    review = tmp_path / "review"
    cx.review_sample(layout, "llm", review, sample=10)
    row = next(cx.read_jsonl(review))
    assert row["llm_results"][0]["checks"]["op"] == "open"
    assert row["parser_results"] and row["selected_results"]
    assert (layout.gold_dir / "plan_source__llm.json").exists()
    # Same numeric data, now a retrospective analysis. No new episode allowed.
    doc = json.loads(fixture.read_text())
    for rec in doc["items"].values():
        rec["response"]["actions"][0].update(op="analysis", time_ref="past")
    cx.write_json(fixture, doc)
    def invariant(version):
        report = api.build(source, layout, graph_version=version, llm_fixture=fixture,
                           plan_source="reconciled", ingested_at=T0)
        assert report["lifecycle"]["n_episodes"] == 0
    invariant("nonopening")
    monkeypatch.setattr(plan_source, "action_overridden", lambda *_: False)
    with pytest.raises(AssertionError):
        invariant("mutant-nonopening")


def test_source_hash_modes_and_mutant(tmp_path, monkeypatch):
    layout, mv, ex, cp = canonical_pair(tmp_path)
    cp.write_parquet(layout.canonical_plan)
    edges, decisions, _ = linker.build_candidates(cp, mv, ex, ingested_at=T0)
    edges.write_parquet(layout.silver_dir / "candidate_edges.parquet")
    decisions.write_parquet(layout.silver_dir / "adjudications.parquet")
    def invariant(hash_of):
        hashes = [hash_of(layout, ingested_at=T0, plan_source=mode) for mode in ("parser", "llm", "reconciled")]
        assert len(set(hashes)) == 3
        assert hashes[0] == hash_of(layout, ingested_at=T0)
    invariant(lifecycle.input_hash_of)
    broken = mutant(lifecycle.input_hash_of, 'if kw.get("plan_source", "parser") != "parser":', 'if False:')
    with pytest.raises(AssertionError):
        invariant(broken)


def test_market_reference_compact_chinese_decimal_and_mutant():
    text = "仿写BTC做多现价0.123456789012，止损0.120000000001，目标0.14、0.15"
    value = item()
    value.update(entry=dict(lo=Decimal("0.123456789012"), hi=Decimal("0.123456789012"), kind="market_ref"),
                 stop=Decimal("0.120000000001"), tps=[dict(level=Decimal("0.14")), dict(level=Decimal("0.15"))],
                 quotes=[dict(field="entry.lo", quote="现价0.123456789012"), dict(field="entry.hi", quote="现价0.123456789012"),
                         dict(field="stop", quote="止损0.120000000001"), dict(field="tps", quote="目标0.14、0.15"),
                         dict(field="tps", quote="目标0.14、0.15")])
    response = cx.quote_response(value, text)["response"]
    assert response["entry"]["lo"] == Decimal("0.123456789012")
    assert validate_evidence(response, text) == []
    system, user = build_extract_prompt(text, channel_name="仿写", message_date=None)
    key = record_key(system, user, SCHEMA_NAME_EXTRACT)
    parsed, _, _ = extract.llm_extract(text, client=RecordedClient({key: dict(response=response)}), channel_name="仿写", message_date=None)
    assert parsed.entry_mode == "market_ref"
    root = dict(instrument_id="BTC-USDT", side="long", entry=parsed.entry, entries=[], entry_mode=parsed.entry_mode)
    def invariant(order_plan):
        plan = order_plan(root, parsed.stop, parsed.tps, None)
        assert plan["entries"][0]["kind"] == "market_ref"
        assert plan["entries"][0]["price_lo"] == Decimal("0.123456789012")
        assert len(plan["tps"]) == 2
    invariant(lifecycle._order_plan)
    broken = mutant(lifecycle._order_plan, 'elif e["kind"] == "market_ref":', 'elif False:')
    with pytest.raises(AssertionError):
        invariant(broken)


def test_token_accounting_and_mutant(tmp_path, fake_codex, monkeypatch):
    script, _ = fake_codex
    path = tmp_path / "prompts"
    prompts(path, 1)
    def invariant(directory):
        report = cx.run_batches(path, directory, executable=str(script))
        assert report["tokens_used"] == 1234 and report["unknown_token_calls"] == 0
        assert report["total_calls"] == 1 and report["total_call_elapsed_s"] > 0
    invariant(tmp_path / "ok")
    monkeypatch.setattr(cx, "_tokens", lambda log: None)
    with pytest.raises(AssertionError):
        invariant(tmp_path / "mutant")


def test_partial_response_recovery_and_mutant(tmp_path, fake_codex):
    script, capture = fake_codex
    path, output = tmp_path / "prompts", tmp_path / "run"
    prompts(path, 2)
    cx.run_batches(path, output, executable=str(script))
    def invariant(run):
        (output / "responses.jsonl").write_text('{"key":')
        before = len(calls(capture))
        run(path, output, executable=str(script))
        assert len(calls(capture)) == before
        assert len(list(cx.read_jsonl(output / "responses.jsonl"))) == 2
    invariant(cx.run_batches)
    broken = mutant(cx.run_batches, 'if not journal_paths and response_path.exists():', 'if response_path.exists():')
    with pytest.raises(json.JSONDecodeError):
        invariant(broken)


def test_review_excludes_missing_recordings_and_mutant(tmp_path, monkeypatch):
    layout, mv, ex, cp = canonical_pair(tmp_path)
    cx.save_build_review(layout, "sample", cp, mv, graph(cp, mv, ex, "llm")[0], "llm")
    path = tmp_path / "review"
    def invariant(review):
        report = review(layout, "sample", path, sample=20)
        assert report["exported"] == 2
        assert {r["source_version_id"] for r in cx.read_jsonl(path)} == {"s0", "s1"}
    invariant(cx.review_sample)
    broken = mutant(cx.review_sample, 'rows = [r for r in rows if r["selected_results"]]', 'rows = rows')
    with pytest.raises(AssertionError):
        invariant(broken)


def test_zone_range_and_signed_token_mutant(monkeypatch):
    text = "仿写BTC多 入场100-105 止损90 止盈110、120"
    value = item()
    value["entry"].update(hi=105, kind="zone")
    value["quotes"][0]["quote"] = value["quotes"][1]["quote"] = "入场100-105"
    value["quotes"][2]["quote"] = "止损90"
    assert cx.quote_response(value, text)["response"]["entry"]["hi"] == 105
    # A positive price cannot cite the digits inside a negative literal.
    text = TEXT.replace("止损 90", "止损 -90")
    value = item()
    value["quotes"][2]["quote"] = "90"
    def invariant():
        assert "abstain" in cx.quote_response(value, text)
    invariant()
    import re
    monkeypatch.setattr(cx, "NUMBER", re.compile(cx.NUMBER.pattern.replace(r"[+\-]?", "")))
    with pytest.raises(AssertionError):
        invariant()


def test_restart_reasks_transport_failures_and_backoff(tmp_path, fake_codex, monkeypatch):
    script, capture = fake_codex
    path, output = tmp_path / "prompts", tmp_path / "run"
    prompts(path, 2)
    monkeypatch.setenv("CX_MODE", "fail")
    cx.run_batches(path, output, executable=str(script), batch_size=2, retries=0)
    assert all(cx._transport_failed(r) for r in cx.read_jsonl(output / "responses.jsonl"))
    failed_journal = next((output / "completed").glob("*.json"))
    monkeypatch.setenv("CX_MODE", "ok")
    def invariant(runner):
        report = runner(path, output, executable=str(script), batch_size=2)
        assert report["calls"] == 1 and report["abstained"] == 0
        # Make the stale failure sort last: a restart must still keep the answer.
        failed_journal.rename(output / "completed" / "zzzz.json")
        again = runner(path, output, executable=str(script), batch_size=2)
        assert again["calls"] == 0 and again["abstained"] == 0
    invariant(cx.run_batches)
    with pytest.raises(AssertionError):
        (output / "completed" / "zzzz.json").rename(failed_journal)
        for extra in set((output / "completed").glob("*.json")) - {failed_journal}:
            extra.unlink()
        invariant(mutant(cx.run_batches, 'if k not in completed or _transport_failed(completed[k])]', 'if k not in completed]'))
    slept = []
    monkeypatch.setattr(cx.time, "sleep", slept.append)
    monkeypatch.setenv("CX_MODE", "fail")
    cx.run_batches(path, tmp_path / "backoff", executable=str(script), batch_size=2, retries=2, backoff=10)
    assert slept == [10, 20]
