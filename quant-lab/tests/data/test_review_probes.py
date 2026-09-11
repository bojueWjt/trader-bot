"""Independent r2 §9 / r3 §10 probes, with explicit acceptance assertions.

Only fixture identities are translated through ANCHORS after the 1..72 remapping.
The historical scripts print observations; these tests supply the missing verdicts.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
from decimal import Decimal
import json
import pathlib
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import polars as pl
import pytest

from quant_lab.data import api, audit, extract, graph, lake, lifecycle, linker, llm, normalize, sources, validate

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIX = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample"
ANCH = json.loads((FIX / "ANCHORS.json").read_text())
T = datetime(2024, 6, 10, 9, 1, tzinfo=UTC)
BUILD = datetime(2026, 9, 11, tzinfo=UTC)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    layout = lake.Layout.from_root(tmp_path_factory.mktemp("review-probes"))
    api.build(FIX, layout, graph_version="fixture-v1", llm_fixture=FIX.parent / "llm_recorded/extract_v1.json", ocr_fixture=FIX.parent / "llm_recorded/ocr_v1.json", ingested_at=BUILD)
    return layout


@pytest.fixture(scope="module")
def probes(built):
    results = {}
    failures = []
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(ROOT)
        mp.setenv("QUANT_LAB_DATA_ROOT", str(built.bronze_dir.parents[2]))
        # Layout.from_root path is lake/telegram/bronze; resolve from the fixture itself.
        mp.setattr(api, "_layout", lambda: built)
        with patch.object(lake.Layout, "from_root", return_value=built):
            for round_no in (2, 3):
                report = (ROOT / f"docs/adr/review-G1-P1-r{round_no}.md").read_text()
                source = report.split(f"\n# R{round_no}_PROBE_BEGIN\n", 1)[1].split(f"\n# R{round_no}_PROBE_END", 1)[0]
                source = source.replace("(pl.col('message_id')==103)", f"(pl.col('message_id')=={ANCH['anchors']['A_eth_1']})")
                source = source.replace("m.message_id==120", f"m.message_id=={ANCH['anchors']['CE1_edited_sl']}")
                source = source.replace("(pl.col('root_message_id')==140)", f"(pl.col('root_message_id')=={ANCH['anchors']['CE3_timeout']})")
                env = {}

                def emit(tag, value):
                    results[f"r{round_no}:{tag}"] = json.loads(json.dumps(value, default=str))
                    print(f"r{round_no}:{tag}", json.dumps(value, default=str))

                for node in ast.parse(source).body:
                    try:
                        exec(compile(ast.Module(body=[node], type_ignores=[]), f"review-r{round_no}", "exec"), env)
                        if isinstance(node, ast.FunctionDef) and node.name == "emit":
                            env["emit"] = emit
                    except LookupError:
                        if "X11-public-empty-files" not in ast.get_source_segment(source, node):
                            raise
                        emit("X11-public-empty-files", "LookupError")
                    except Exception as error:
                        failures.append((round_no, node.lineno, type(error).__name__, str(error)))
    assert not failures, failures
    return results


@pytest.mark.parametrize("tag", ["R01", "X01-correction", "X01-original"])
def test_r01_x01_public_all_columns(probes, tag):
    assert probes[f"r2:{tag}"]["diff"] == []


def test_r02_h1_no_retroactive_entry(probes):
    r = probes["r2:R02"]
    assert not r["at_1021_visible"]
    assert r["available_at"].startswith("2024-04-02 10:25")
    for row in r["h1"]:
        assert row["t_dec"] is None and row["order_plan"] is None
        assert not row["eligibility_by_estimand"]["entry_decision"]


def test_r03_full_album_closure(probes):
    assert probes["r2:R03a"][0]["edge_available_at"].startswith("2024-06-11 09:01")
    r = probes["r2:R03b"]
    assert r["after"] > r["late_media"] > r["before"]
    assert "r2-late-media" in r["deps"]


def test_r04_x04_unknown_market_evidence(probes):
    for key in ("future", "missing_available"):
        assert probes["r2:R04"][key][0] is None
    assert probes["r2:R04"]["cal_no_samples"][0] is None
    assert probes["r2:X04-null"][0] is None


def test_r05_identity_and_branch_guards(probes):
    assert probes["r2:R05a"]["actions"] == ["unresolved"]
    assert all(r["is_canonical"] for r in probes["r2:R05b"])
    assert not any(r["method"] == "same_source" for r in probes["r2:R05c"])


def test_r06_t02_no_price_or_fraction_guessing(probes):
    for tag in ("R06a", "R06c"):
        assert Decimal(str(probes[f"r2:{tag}"]["entry"]["lo"])) == 6  # r4 §8 V01
        assert Decimal(str(probes[f"r2:{tag}"]["stop"])) == 5  # r4 §8 V01
        assert Decimal(str(probes[f"r2:{tag}"]["tps"][0]["level"])) == 7  # r4 §8 V01
    assert probes["r2:R06b"]["entry"] is None and probes["r2:R06d"] is None
    assert float(probes["r2:R06e"][0]["fraction"]) == .25
    assert not probes["r3:T02-same-version"]["snapshot_equal"]
    assert probes["r3:T02-future"]["snapshot_equal"] and probes["r3:T02-future"]["plan_equal"]


def test_r07_x07_quarantine_inheritance(probes):
    assert probes["r2:R07"]["mixed"] == ["UNIT_SCALE_CONFLICT", "fatal"]
    assert not json.loads(probes["r2:R07"]["missing_media"][0]["eligibility_by_estimand"])["execution"]
    for reason in ("RAW_HASH_MISMATCH", "SCHEMA_DRIFT", "KEY_DUPLICATE_OR_ORDER", "MEDIA_MISSING"):
        assert not probes[f"r2:X07-{reason}"]["execution"]
    assert probes["r2:X07-direction"][0][1] == "fatal"


def test_r08_x08_schema_and_source_clocks(probes):
    r = probes["r2:R08"]
    assert r["asserted_event_time"] and r["alias"] and r["codes"] == 29
    assert r["source_clock_mismatches"] == 0 and probes["r2:X08-all-clock"] == []
    assert all(probes["r2:X08-basis"])
    assert probes["r3:PUBLIC-eligibility"] == [{k: 0 for k in lifecycle.ELIG_KEYS}]


def test_r09_observation_horizon_and_delete(probes):
    r = probes["r2:R09"]
    assert r["deadline_root"][0]["author_plan_state"] == "active"
    assert r["future_expire"] == 0 and r["future_roots"] == 0
    assert r["delete_parse"] == "delete_notice" and r["delete_allowed"]


def test_r10_c3_hash_all_used_inputs(probes):
    assert probes["r2:R10a"] == {"both_clocks_changed_rows": 2, "edit_only_changed_rows": 2  # r4 §6/§7: edit evidence creates a version.
}
    assert not probes["r2:R10b"]["input_hash_equal"]
    r = probes["r3:S10-MV"]
    assert not r["input_hash_equal"] and not r["snapshot_equal"] and r["t_dec_after"] > r["t_dec_before"]


def test_r11_x11_t01_manifest_and_stale(probes):
    for tag in ("R11-missing", "R11-empty_files", "R11-no_files", "X11-public-missing", "X11-public-empty-files"):
        assert probes[f"r2:{tag}"] == "LookupError"
    cases = [value for tag, value in probes.items() if tag.startswith("r3:T01-")]
    assert len(cases) >= 19 and all(case == {"error": "LookupError"} for case in cases)
    assert probes["r3:X11-stale-resolved"]["episode_rows"] == probes["r3:X11-stale-resolved"]["event_rows"] == 0


def test_r12_physical_denominators(probes, built):
    assert probes["r2:R12a"]["conserved_rows"]
    for layer in (4, 5, 6):
        assert probes[f"r2:R12-L{layer}"]["unaccounted"] == 0
    mv = pl.read_parquet(built.message_version)
    dg = pl.read_parquet(built.duplicate_group)
    canonical = set(dg.filter(pl.col("is_canonical"))["source_version_id"])
    batch = mv["batch_id"][0]
    mapped = set(pl.read_parquet(built.mapping(batch, 3))["input_ref"])
    assert set(mv["source_version_id"]) & canonical <= mapped


def test_r13_x13_c3_recorded_protocol(probes):
    assert probes["r2:R13-adj"]["action"] == "unresolved"
    assert probes["r3:S13-filtered-prompt"]["sent_context"] == []
    assert probes["r3:S13-filtered-prompt"]["result"]["action"] == "unresolved"
    assert probes["r3:R13-adj-21-exact"]["result"]["action"] == "unresolved"
    assert "TEXT_IMAGE_CONFLICT" in probes["r2:X13-conflict"]
    assert probes["r2:X13-budget"]["sent_chars"] < 8000
    assert probes["r2:X13-budget"]["result"] == "unresolved"
    assert probes["r2:R13-numeric"]


def test_r14_c3_no_partial_label_pass(probes):
    for key in ("empty", "partial"):
        assert probes["r2:R14"][key]["status"] == "insufficient"
    for tag in ("partial_null", "partial_present", "mixed_null"):
        assert probes[f"r3:R14-{tag}"].get("verdict") != "pass"
    assert probes["r3:R14-good"]["result"] == "pass"
    for tag in ("n201", "attempt0", "hash_empty", "reviewer_empty"):
        assert probes[f"r3:R14-{tag}"]["result"] != "pass"


def test_r15_fixture_spec(probes):
    r = probes["r2:R15"]
    assert r["unique_sources"] == 216 and r["mv"] >= 246
    assert set(r["grades"]) == {"V", "H0", "H1", "H2", "U"}
    assert {2024, 2025, 2026} <= set(r["years"])
    grouped = {}
    edits = {}
    for m in sources.read_all(FIX):
        grouped.setdefault(m.channel_id, set()).add(m.message_id)
        if m.edited_unixtime is not None:
            edits.setdefault(m.channel_id, {}).setdefault(m.message_id, set()).add(m.edited_unixtime)
    assert len(grouped) == 3 and all(ids == set(range(1, 73)) for ids in grouped.values())
    assert all(len(ids) >= 8 and sum(len(times) >= 2 for times in ids.values()) >= 2 for ids in edits.values())
    assert json.loads((FIX / "SPEC.json").read_text())["seed"] == 20260911


def test_r16_media_path_guard(probes):
    for tag, reason in (("/r2-sentinel", "absolute_path"), ("../r2-sentinel", "parent_escape"), ("photos/r2-link", "symlink_escape")):
        assert probes[f"r2:R16-{tag}"] == [{"reason": reason, "sha256": None, "exists": False}]


def test_t03_u01_size_and_bbox_matrix(probes):
    assert probes["r3:T03-valid"]["kind"] == "undecidable"  # r4 §6: entry has a box but stop has none.
    for case in ("negative", "short", "nan", "inf", "reverse", "outside"):
        assert probes[f"r3:T03-{case}"]["kind"] == "undecidable" and probes[f"r3:T03-{case}"]["reasons"]  # r4 §7
        assert probes[f"r3:T03-{case}"]["numbers"] == [] and probes[f"r3:T03-{case}"]["bbox_count"] == 0
    for tag, value in probes.items():
        if tag.startswith("r3:U01-size-"):
            assert value["status"] == "unreadable" and value["numbers"] == []
    for item in probes["r3:T03-fixture"]:
        assert item["actual"] == item["recorded"] and item["actual"][0] >= 64 and item["actual"][1] >= 32 and item["all_valid"]


def _pipeline(tmp_path, texts):
    """Hand-authored message stream; no gold expectations derive from the implementation."""
    from quant_lab.data import dedup
    template = next(m for m in sources.read_all(FIX) if m.channel_id == ANCH["peers"]["A"] and m.message_id == ANCH["anchors"]["A_eth_1"])
    messages = []
    for index, item in enumerate(texts):
        text, reply, at, author = item
        messages.append(dataclasses.replace(template, channel_id=1, from_id=author, message_id=index + 1, text=text, text_entities=[], reply_to_message_id=reply,
                                            date_unixtime=int(at.timestamp()), edited_unixtime=None, first_seen_at=at, snapshot_at=at, raw_index=index,
                                            sequence_evidence=index, media=[], grouped_id=None, raw_hash="test-stream", cohort_id="synthetic"))
    layout = lake.Layout.flat(tmp_path)
    mv, _, _, _ = normalize.normalize_messages(messages, layout, ingested_at=BUILD)
    dg, *_ = dedup.dedup_frame(mv, ingested_at=BUILD)
    ex, *_ = extract.extract_frame(mv, dg, ingested_at=BUILD)
    from quant_lab.data.market_stub import fixture_registry, SyntheticMarks
    marks = SyntheticMarks({"BTCUSDT-PERP.BINANCE-UM": [(T - timedelta(days=10), 60000)], "ETHUSDT-PERP.BINANCE-UM": [(T - timedelta(days=10), 3400)]})
    cp, *_ = validate.validate_frame(ex, mv, registry=fixture_registry(), marks=marks, ingested_at=BUILD)
    cb, jd, _ = linker.build_candidates(cp, mv, ex, ingested_at=BUILD)
    ep, ev, qs, ledger, _ = lifecycle.build_graph(cp, mv, cb, jd, dg, graph_version="independent", ingested_at=BUILD)
    return mv, ex, cp, cb, jd, ep, ev, qs


@pytest.mark.parametrize("broken", ["missing", "clock", "cycle"])
def test_s03_white_grey_black_dependency_rejection(broken):
    nodes = {"root": {"available_at": T, "dependencies": ["field"]}, "field": {"available_at": T, "dependencies": ["media"]}, "media": {"available_at": T + timedelta(days=1)}}
    assert graph.dependency_closure("root", nodes)[0] == T + timedelta(days=1)
    if broken == "missing":
        del nodes["media"]
    elif broken == "clock":
        nodes["media"]["available_at"] = None
    else:
        nodes["media"]["dependencies"] = ["root"]
    assert graph.dependency_closure("root", nodes)[0] is None


def test_s03_purpose_specific_market_and_field_closure(tmp_path):
    mv, _, cp, *_ = _pipeline(tmp_path, [("BTC 做多 入场 60000 止损 59000", None, T, "a")])
    plan = cp.row(0, named=True)
    versions = {r["source_version_id"]: r for r in mv.iter_rows(named=True)}
    checks = json.loads(plan["checks"])
    checks["dependencies"].append({"ref": "late-calibration", "purpose": "price_check", "available_at": (T + timedelta(days=1)).isoformat()})
    plan["checks"] = json.dumps(checks)
    assert graph.plan_dependencies(plan, versions, {})[0] == T
    assert graph.plan_dependencies(plan, versions, {}, purpose="price_check")[0] == T + timedelta(days=1)
    checks["dependencies"].append({"ref": "unknown-field", "purpose": "all", "available_at": None})
    plan["checks"] = json.dumps(checks)
    assert graph.plan_dependencies(plan, versions, {})[0] is None


@pytest.mark.parametrize("text", ["BTC 多 60000；ETH 空 3400", "BTC 多 60000；独立单 BTC 多 58000", "BTC 做多 入场 60000 止损 59000；ETH 做空 入场 3400 止损 3500", "BTC 做多 入场 60000 止损 59000；独立单 BTC 做多 入场 58000 止损 57000"])
def test_s05_independent_branches_become_distinct_roots(tmp_path, text):
    _, ex, cp, _, _, ep, _, _ = _pipeline(tmp_path, [(text, None, T, "a")])
    assert ex["branch_index"].to_list() == [0, 1]
    assert cp["plan_id"].n_unique() == ep.height == 2
    assert ep["entry_branch_id"].n_unique() == 2
    titan = extract.parse_branches("BTC 做多 首次入场价 60000 第二次入场价 59000 止损 58000")
    assert len(titan) == 1 and titan[0].entries == [60000, 59000]


def test_s05_quote_unique_and_ambiguous(tmp_path):
    first = "BTC 做多 入场 60000 止损 59000"
    for two in (False, True):
        messages = [(first, None, T, "a")]
        if two:
            messages.append((first + " 独立新单", None, T + timedelta(minutes=1), "a"))
        messages.append((f'“{first}” 止损上移到 59500', None, T + timedelta(hours=1), "a"))
        _, _, _, cb, jd, _, _, _ = _pipeline(tmp_path / str(two), messages)
        quote = cb.filter(pl.col("method") == "quote")
        assert quote.height == (2 if two else 1)
        assert quote["strength"].to_list() == (["weak", "weak"] if two else ["strong"])
        assert jd["action"][-1] == ("unresolved" if two else "link")


def test_s05_author_plan_reference_namespace(tmp_path):
    texts = [("计划 A BTC 做多 入场 60000 止损 59000", None, T, "alice"),
             ("计划 A BTC 做多 入场 58000 止损 57000", None, T + timedelta(minutes=1), "bob"),
             ("计划 A BTC 止损上移到 59500", None, T + timedelta(hours=1), "alice")]
    _, _, cp, cb, jd, _, _, _ = _pipeline(tmp_path, texts)
    refs = cb.filter(pl.col("method") == "plan_ref")
    assert refs.height == 1 and refs["selected"][0]
    assert refs["to_plan_id"][0] == cp.filter(pl.col("message_id") == 1)["plan_id"][0]


def test_s09_delete_correction_reopen(tmp_path):
    texts = [("BTC 做多 入场 60000 止损 59000", None, T, "a"),
             ("BTC 止损写错，更正止损 58500", 1, T + timedelta(hours=1), "a"),
             ("BTC 已删除上一条", 1, T + timedelta(hours=2), "a"),
             ("BTC 全部平仓", 1, T + timedelta(hours=3), "a"),
             ("BTC 做多 入场 58000 止损 57000", 1, T + timedelta(hours=4), "a")]
    mv, _, cp, cb, jd, ep, ev, _ = _pipeline(tmp_path, texts)
    root = ep.filter(pl.col("root_message_id") == 1).row(0, named=True)
    successor = ep.filter(pl.col("root_message_id") == 5).row(0, named=True)
    assert root["replay_required"] and not any(root["eligibility_by_estimand"].values())
    correction = ev.filter(pl.col("kind") == "correction").row(0, named=True)
    assert correction["supersedes_event_id"]
    assert ev.filter(pl.col("event_id") == correction["supersedes_event_id"])["superseded"][0]
    assert root["deleted_after_observation_any"]
    assert not any(v is True for v in mv["deleted_after_observation"])
    assert successor["predecessor_ids"] == [root["episode_id"]] and successor["migration_reason"] == "reopen"
    # Full event history cannot mutate the original entry snapshot.
    original_cp = cp.filter(pl.col("message_id") == 1)
    before = lifecycle.build_graph(original_cp, mv, cb.head(0), jd.head(0), None, graph_version="independent", ingested_at=BUILD)[0].row(0, named=True)
    assert before["decision_snapshot_hash"] == root["decision_snapshot_hash"]


def test_s09_unlocatable_correction_is_unresolved(tmp_path):
    texts = [("BTC 做多 入场 60000", None, T, "a"), ("BTC 止损写错，更正止损 59000", 1, T + timedelta(hours=1), "a")]
    *_, ep, ev, qs = _pipeline(tmp_path, texts)
    correction = ev.filter(pl.col("kind") == "correction").row(0, named=True)
    assert json.loads(correction["payload"])["unresolved"]
    assert "LIFECYCLE_INVALID" in ep["reason_codes"][0]


@pytest.mark.parametrize("field, image", [("entry.lo", "BTC 做多 入场 101-102 止损 90 止盈 110"), ("entry.hi", "BTC 做多 入场 100-103 止损 90 止盈 110"), ("stop", "BTC 做多 入场 100-102 止损 900 止盈 110"), ("tps[0].level", "BTC 做多 入场 100-102 止损 90 止盈 1100")])
def test_s13_fieldwise_ocr_conflicts(field, image):
    text = "BTC 做多 入场 100-102 止损 90 止盈 110"
    result = extract.apply_ocr(extract.parse_message(text), text, ["image"], llm.RecordedOcr({"image": {"size": [256, 80], "text": image, "numbers": []}}))
    assert "TEXT_IMAGE_CONFLICT" in result.reason_codes
    assert any(c["field"] == field and not c["match"] for c in result.checks["text_image"])
    assert result.bboxes == []


def _audit_artifact(path, batch="b"):
    layout = lake.Layout.flat(path)
    layout.ensure()
    ids = [f"{prefix}-{i}" for prefix in ("first", "second", "third", "sample") for i in range(200)]
    if batch == "small":
        ids = ["small-0", "small-1"]
    lake.write_parquet_atomic(pl.DataFrame({"episode_id": ids, "t_dec": [T] * len(ids), "batch_id": [batch] * len(ids)}), layout.episode("reviewed"))
    lake.write_parquet_atomic(pl.DataFrame({"event_id": ["e"]}), layout.episode_event("reviewed"))
    graph.publish_manifest(layout, "reviewed", input_hash="a" * 64, rule_versions={"lifecycle": lifecycle.RULE_VERSION}, assumptions={}, counts={"episodes": len(ids), "events": 1, "decision_roots": len(ids)}, built_at=BUILD)
    return layout


def _sample(prefix, n=200, errors=0):
    ids = tuple(f"{prefix}-{i}" for i in range(n))
    labels = tuple({"sample_id": identifier, "severity": "general" if i < errors else "ok", "reviewer_id": "independent", "annotation": {"instrument_id": "BTCUSDT-PERP.BINANCE-UM", "side": "long", "entry": "60000", "stop": "59000", "kind": "entry_proposal"}} for i, identifier in enumerate(ids))
    return dict(sample_ids=ids, sample_ids_hash=audit.sample_identity(list(ids)), labels=labels, errors_general=errors)


def test_s14_formal_history_and_full_audit(tmp_path):
    # r4 §6/§7/V03: saved artifact, nonempty labels, failed first review, evidenced retest.
    layout = _audit_artifact(tmp_path / "large")
    args = dict(n_sampled=200, errors_fatal=0, reviewer_ids=("independent",), producer_ids=("producer",), batch_id="b", layout=layout, graph_version="reviewed", input_hash="a" * 64)
    first = audit.acceptance_decision(**args, **_sample("first", errors=4))
    repair = layout.gold_dir / "_audit" / "repair.json"
    repair.write_text('{"fix": "corrected field attribution", "test": "regression passed"}')
    assert audit.acceptance_decision(**args, **_sample("second")).result == "insufficient"
    assert audit.acceptance_decision(**args, **_sample("second"), audit_plan_version="renamed", repair_evidence_refs=(str(repair),)).result == "insufficient"
    second = audit.acceptance_decision(**args, **_sample("second"), repair_evidence_refs=(str(repair),))
    third = audit.acceptance_decision(**args, **_sample("third"), audit_plan_version="renamed", repair_evidence_refs=(str(repair),))
    assert (first.result, second.result, third.result) == ("fail", "pass", "insufficient")
    assert (first.attempt, second.attempt, third.attempt) == (1, 2, 3)
    records = audit.read_acceptance_records(layout)
    assert records.height == audit.read_signoffs(layout).height == 2
    assert records["repair_evidence_hash"][1] and records["previous_record_hash"][1] == records["record_hash"][0]
    assert all(len(ids) == 200 for ids in records["sample_ids"])
    small = _audit_artifact(tmp_path / "small", batch="small")
    args.update(layout=small, batch_id="small", n_sampled=2, batch_size=2)
    assert audit.acceptance_decision(**args, **_sample("small", n=2)).result == "full_audit_required"
    assert audit.acceptance_decision(**args, **_sample("small", n=2), full_audit=True).result == "pass"
    assert audit.acceptance_decision(**args, **_sample("another", n=2), full_audit=True, repair_evidence_refs=(str(repair),)).result == "insufficient"


def test_s15_source_matrix_and_cross_attributes():
    from collections import Counter
    spec = json.loads((FIX / "SPEC.json").read_text())["channels"]
    expected = {"proposal": 14, "management": 16, "claim": 8, "lifecycle": 6, "noise": 12, "album": 8, "image": 4, "copy": 4}
    assert set(spec) == {"A", "B", "C"}
    for key, categories in spec.items():
        assert Counter(categories.values()) == expected
        raw = [m for m in sources.read_all(FIX) if m.channel_id == ANCH["peers"][key]]
        assert len({m.message_id for m in raw if m.reply_to_message_id is not None}) >= 12
        assert len({m.message_id for m in raw if '“' in m.text}) >= 4
        assert len({m.message_id for m in raw if '计划 P' in m.text}) >= 4


def test_s13_fixture_pixels_and_unreadable_pair():
    import struct
    import zlib
    records = json.loads((FIX.parent / "llm_recorded/ocr_v1.json").read_text())["items"]
    by_hash = {hashlib.sha256(p.read_bytes()).hexdigest(): p for p in FIX.rglob("*.png")}
    readable = 0
    for digest, record in records.items():
        if record.get("unreadable"):
            continue
        readable += 1
        image = by_hash[digest].read_bytes()
        width, height = struct.unpack(">II", image[16:24])
        pos, compressed = 8, b""
        while pos < len(image):
            length = struct.unpack(">I", image[pos:pos+4])[0]
            if image[pos+4:pos+8] == b"IDAT":
                compressed += image[pos+8:pos+8+length]
            pos += length + 12
        pixels = zlib.decompress(compressed)
        stride = width * 3 + 1
        # Independent occupancy check: every recorded character box contains several ink columns/rows.
        for number in record["numbers"]:
            x0, y0, x1, y1 = number["bbox"]
            ink_rows = [y for y in range(y0, y1) if any(pixels[y*stride+1+x*3] == 0 for x in range(x0, x1))]
            ink_cols = [x for x in range(x0, x1) if any(pixels[y*stride+1+x*3] == 0 for y in range(y0, y1))]
            assert len(ink_rows) >= 10 and len(ink_cols) >= 6
        provider = llm.RecordedOcr({digest: record})
        assert extract.apply_ocr(extract.parse_message(""), "", [digest], provider).kind == "entry_proposal"
        refused = extract.apply_ocr(extract.parse_message(""), "", [digest], llm.RecordedOcr({digest: {"unreadable": True}}))
        assert "OCR_UNREADABLE" in refused.reason_codes and refused.kind == "undecidable"
    pairs = [(h, r) for h, r in records.items() if r.get("paired_with")]
    assert len(pairs) == 1 and pairs[0][0] != pairs[0][1]["paired_with"]
    assert pairs[0][1]["unreadable"] and pairs[0][1]["paired_with"] in records
    assert readable == 3


def test_decimal_physical_types_and_q12(built):
    from decimal import Decimal
    for path in (built.extracted_event, built.canonical_plan):
        frame = pl.read_parquet(path)
        assert frame.schema["stop"] == pl.Decimal(38, 12)
        assert frame.schema["entry"].to_schema()["lo"] == pl.Decimal(38, 12)
        assert frame.schema["tps"].inner.to_schema()["level"] == pl.Decimal(38, 12)
    assert lake.stable_id(Decimal("6")) == lake.stable_id(Decimal("6.000000000000"))


def test_s03_sequence_evidence_and_unknown_equal_time(tmp_path):
    template = next(sources.read_all(FIX))
    base = dataclasses.replace(template, media=[], first_seen_at=None, edited_unixtime=None, date_unixtime=int(T.timestamp()), message_type="message", raw_uri="example/result.json", raw_hash="sequence-test")
    rows = [dataclasses.replace(base, message_id=i + 1, raw_index=i, text=f"message {i}") for i in range(2)]
    mv, *_ = normalize.normalize_messages(rows, lake.Layout.flat(tmp_path / "ordered"))
    assert set(mv["sequence"]) == {1, 2}
    assert all(json.loads(a)["sequence_source"] == "message_id_monotonic_within_export" for a in mv["temporal_assumptions"])
    reversed_rows = [dataclasses.replace(rows[0], raw_index=1), dataclasses.replace(rows[1], raw_index=0)]
    unknown, *_ = normalize.normalize_messages(reversed_rows, lake.Layout.flat(tmp_path / "unknown"))
    assert unknown["sequence"].null_count() == 2
    assert not graph.decision_visible(T, T)


def test_s09_reopen_is_persisted_without_staling_predecessor(tmp_path):
    from quant_lab.data import dedup
    texts = [("BTC 做多 入场 60000 止损 59000", None, T, "a"), ("BTC 全部平仓", 1, T + timedelta(hours=1), "a"), ("BTC 做多 入场 58000 止损 57000", 1, T + timedelta(hours=2), "a")]
    mv, ex, cp, cb, jd, ep, ev, _ = _pipeline(tmp_path, texts)
    layout = lake.Layout.flat(tmp_path)
    dg, *_ = dedup.dedup_frame(mv)
    for frame, path in ((mv, layout.message_version), (ex, layout.extracted_event), (cp, layout.canonical_plan), (cb, layout.silver_dir / "candidate_edges.parquet"), (jd, layout.silver_dir / "adjudications.parquet"), (dg, layout.duplicate_group)):
        lake.write_parquet_atomic(frame, path)
    lifecycle.run(layout, graph_version="reopened", ingested_at=BUILD)
    migrations = graph.read_migrations(layout)
    assert migrations.height == 1 and migrations["migration_reason"][0] == "reopen"
    assert not graph.stale_episodes(layout, "reopened")
    lifecycle.run(layout, graph_version="reopened", ingested_at=BUILD)
    assert graph.read_migrations(layout).height == 1


def test_s03_missing_dependency_blocks_root_and_records_reason(tmp_path):
    mv, _, cp, cb, jd, _, _, _ = _pipeline(tmp_path, [("BTC 做多 入场 60000 止损 59000", 999, T, "a")])
    ep, _, qs, *_ = lifecycle.build_graph(cp, mv, cb, jd, None, graph_version="missing", ingested_at=BUILD)
    assert ep["t_dec"][0] is None and ep["order_plan"][0] is None
    assert "DEPENDENCY_NOT_AVAILABLE" in ep["reason_codes"][0]
    assert any(q["reason_code"] == "DEPENDENCY_NOT_AVAILABLE" for q in qs)


@pytest.fixture(scope="module")
def r4_probes(tmp_path_factory):
    """r4 §9 verbatim statements; rejected reads are successful negative observations."""
    import os
    root = tmp_path_factory.mktemp("r4")
    report = (ROOT / "docs/adr/review-G1-P1-r4.md").read_text()
    source = report.split("# R4_PROBE_BEGIN\n", 1)[1].split("# R4_PROBE_END", 1)[0]
    results = {}
    env = {}
    def capture(tag, value):
        results.setdefault(tag, []).append(value)
        print(tag, json.dumps(value, default=str))
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(ROOT)
        mp.setenv("QUANT_LAB_DATA_ROOT", str(root))
        for node in ast.parse(source).body:
            try:
                exec(compile(ast.Module(body=[node], type_ignores=[]), "review-r4", "exec"), env)
                if isinstance(node, ast.FunctionDef) and node.name == "emit":
                    env["emit"] = capture
            except LookupError:
                assert "T01" in ast.get_source_segment(source, node)
                capture("T01", "LookupError")
    return results


@pytest.mark.parametrize("item", ["V01", "V02", "V03", "V04", "T03/S13", "S03", "S10", "S12", "T01"])
def test_r4_original_counterexamples(r4_probes, item):
    values = r4_probes[item]
    if item == "V01":
        assert values[0][1:] == ["60000.123456789123", "Decimal"]
    elif item == "V02":
        assert len(values[0]) == 2
        assert len({r["cluster_id"] for r in values[0]}) == 1
    elif item == "V03":
        assert all(v[1] != "pass" for v in values)  # r4 §6: no physical sample IDs.
    elif item == "V04":
        row = next(r for r in values[0] if r["root_message_id"] == 3)
        assert row["predecessor_ids"] == [] and row["migration_reason"] is None
    elif item == "T03/S13":
        assert all(v["kind"] == "undecidable" and v["reasons"] and not v["entry"] and not v["stop"] for v in values)
    elif item == "S03":
        assert values[0][0] == T + timedelta(days=1)
        assert any("media:" in r and "image-hash" in r for r in values[0][1])
    elif item == "S10":
        assert not values[0]["same_id"] and values[0]["rows"] == 2
    elif item == "S12":
        assert values[0]["missing"] == 0
    elif item == "T01":
        assert values == ["LookupError"]


def _decision(frame):
    with patch.object(api, 'verify_manifest'), patch.object(api, 'resolve_alias', return_value='independent'), patch.object(api, 'stale_episodes', return_value=set()), patch.object(api.pl, 'read_parquet', return_value=frame):
        return api.load_episodes('independent')


def test_v01_noninteger_roundtrip_and_diagnostic_hash(tmp_path):
    text = 'BTC 做多 入场 60000.123456789123 止损 59000.123456789123 止盈 61000.123456789123 仓位 12%'
    mv, ex, cp, cb, jd, ep, *_ = _pipeline(tmp_path / 'input', [(text, None, T, 'a')])
    for name, frame in [('ex', ex), ('cp', cp), ('ep', ep)]:
        path = tmp_path / f'{name}.parquet'
        lake.write_parquet_atomic(frame, path)
        reread = pl.read_parquet(path)
        assert frame.equals(reread)
        row = reread.row(0, named=True)
        if name == 'ep':
            assert row['order_plan']['entries'][0]['price_lo'] == Decimal('60000.123456789123')
            assert row['order_plan']['stop']['price'] == Decimal('59000.123456789123')
            assert row['order_plan']['tps'][0]['level'] == Decimal('61000.123456789123')
        else:
            assert row['entry']['lo'] == Decimal('60000.123456789123')
            assert row['stop'] == Decimal('59000.123456789123')
            assert row['size_hint']['fraction'] == Decimal('0.12')
    changed = cp.with_columns(pl.lit(123.45).alias('delta_lo'))
    assert lifecycle._sig(cp, 'plan_id') == lifecycle._sig(changed, 'plan_id')
    can, *_ = validate.canonicalize_row(ex.row(0, named=True) | {'time_grade': 'V', 'entry': {'lo': Decimal('1.123456789123'), 'hi': Decimal('3.123456789123'), 'kind': 'zone'}, 'entries': []}, registry=__import__('quant_lab.data.market_stub', fromlist=['fixture_registry']).fixture_registry())
    assert can['entry_ref'] == Decimal('2.123456789123')


def test_v02_default_copy_family_and_future_invariance(tmp_path):
    text = 'BTC 做多 入场 60000 止损 59000'
    rows = [(text, None, T, 'a')]
    before = _decision(_pipeline(tmp_path / 'one', rows)[5])
    rows.append((text, None, T + timedelta(minutes=1), 'a'))
    after = _decision(_pipeline(tmp_path / 'two', rows)[5])
    assert before.height == 1 and after.height == 2
    assert after['cluster_id'].n_unique() == 1
    assert before.to_dicts() == after.filter(pl.col('root_message_id') == 1).to_dicts()
    assert after.filter(pl.col('root_message_id') == 2)['audit_stratum'][0] == 'repost_same_channel'
    rows.append((text, None, T + timedelta(minutes=2), 'a'))
    future = _decision(_pipeline(tmp_path / 'three', rows)[5])
    assert after.sort('root_message_id').to_dicts() == future.filter(pl.col('root_message_id') <= 2).sort('root_message_id').to_dicts()


@pytest.mark.parametrize('symbol,author,expected', [('BTC', 'a', True), ('ETH', 'a', False), ('BTC', 'b', False), ('ETH', 'b', False)])
def test_v04_reopen_guards(tmp_path, symbol, author, expected):
    text = f'{symbol} 做多 入场 60000 止损 59000'
    rows = [('BTC 做多 入场 60000 止损 59000', None, T, 'a'), ('BTC 全部平仓', 1, T + timedelta(hours=1), 'a'), (text, 1, T + timedelta(hours=2), author)]
    ep = _pipeline(tmp_path, rows)[5]
    old = ep.filter(pl.col('root_message_id') == 1).row(0, named=True)
    new = ep.filter(pl.col('root_message_id') == 3).row(0, named=True)
    assert len(ep) == 2
    if expected:
        assert new['predecessor_ids'] == [old['episode_id']] and new['migration_reason'] == 'reopen'
    else:
        assert new['predecessor_ids'] == [] and new['migration_reason'] is None


@pytest.mark.parametrize('text', ['撤回上一条断言', '上一条作废'])
@pytest.mark.parametrize('reply', [1, None])
def test_s09_assertion_withdrawal(tmp_path, text, reply):
    assert extract.parse_message(text).kind == 'correction'
    rows = [('BTC 做多 入场 60000 止损 59000', None, T, 'a'), (text, reply, T + timedelta(hours=1), 'a')]
    mv, ex, cp, cb, jd, ep, ev, qs = _pipeline(tmp_path, rows)
    assert ep.height == 1 and ep['replay_required'][0]
    assert not any(ep['eligibility_by_estimand'][0].values())
    assert ev.filter(pl.col('event_seq') == 0)['superseded'][0]
    correction = ev.filter(pl.col('kind') == 'correction').row(0, named=True)
    assert correction['supersedes_event_id'] and json.loads(correction['payload'])['replay_required']


def test_s09_missing_assertion_is_unresolved(tmp_path):
    *_, jd, ep, ev, qs = _pipeline(tmp_path, [('撤回上一条断言', None, T, 'a')])
    assert jd['action'][0] == 'unresolved'
    assert 'LIFECYCLE_INVALID' in jd['reason_codes'][0]


@pytest.mark.parametrize('field,image', [('symbol_raw', 'ETH 做多 入场 60000 止损 59000'), ('side', 'BTC 做空 入场 60000 止损 59000'), ('size_hint.fraction', 'BTC 做多 入场 60000 止损 59000 仓位 20%'), ('expires_after_s', 'BTC 做多 入场 60000 止损 59000 2小时有效')])
def test_s13_identity_size_and_expiry_comparison(field, image):
    text = 'BTC 做多 入场 60000 止损 59000 仓位 10% 1小时有效'
    result = extract.apply_ocr(extract.parse_message(text), text, ['h'], llm.RecordedOcr({'h': {'size': [256, 80], 'text': image, 'numbers': []}}))
    assert 'TEXT_IMAGE_CONFLICT' in result.reason_codes
    assert any(c['field'] == field and not c['match'] for c in result.checks['text_image'])


def test_s12_all_six_physical_input_sets_and_cumulative_exits(built):
    mv = pl.read_parquet(built.message_version)
    ex = pl.read_parquet(built.extracted_event)
    cp = pl.read_parquet(built.canonical_plan)
    batch = mv['batch_id'][0]
    expected = {2: set(mv['source_version_id']), 3: set(mv['source_version_id']), 4: set(ex['extract_id']), 5: set(cp['plan_id']), 6: set(cp['plan_id'])}
    maps = {layer: pl.read_parquet(built.mapping(batch, layer)) for layer in range(1, 7)}
    assert maps[1]['input_ref'].n_unique() == len(list(sources.read_all(FIX)))
    assert set(maps[1]['output_ref'].drop_nulls()) == set(mv['source_version_id'])
    for layer, ids in expected.items():
        assert ids == set(maps[layer]['input_ref']), layer
    losses = pl.read_parquet(built.loss(batch))
    cumulative = {}
    for layer in range(1, 7):
        mapping = maps[layer]
        exited = mapping.filter(pl.col('status').is_in(['review', 'quarantine', 'dup_ref']) | (pl.col('relation') == 'excluded'))
        for row in exited.iter_rows(named=True):
            cumulative.setdefault(row['stratum'], set()).add(row['input_ref'])
        for row in losses.filter(pl.col('layer') == layer).iter_rows(named=True):
            assert row['cum_excluded_ids'] == len(cumulative.get(row['stratum'], set()))
            assert row['input_n'] == row['n_ok'] + row['n_review'] + row['n_quarantine'] + row['n_dup_ref']


def test_s03_recursive_parent_album_edge_and_root(tmp_path):
    rows = [('BTC 做多 入场 60000 止损 59000', None, T, 'a'), ('BTC 止损调整到 59500', 1, T + timedelta(minutes=1), 'a')]
    mv, ex, cp, *_ = _pipeline(tmp_path, rows)
    parent = mv.filter(pl.col('source_id').struct.field('message_id') == 1).row(0, named=True)
    parent['grouped_id'] = 77
    media = dict(parent, source_version_id='late-parent-image', source_id={'peer_id': parent['channel_id'], 'message_id': 99}, media_hashes=['late-hash'], available_at=T + timedelta(days=1))
    late = pl.concat([mv.filter(pl.col('source_version_id') != parent['source_version_id']), pl.DataFrame([parent, media], schema=mv.schema)])
    cb, jd, _ = linker.build_candidates(cp, late, ex, ingested_at=BUILD)
    selected = cb.filter(pl.col('selected')).row(0, named=True)
    assert selected['edge_available_at'] == T + timedelta(days=1)
    assert any('media:' in ref and 'late-hash' in ref for ref in selected['dependency_refs'])
    ep, ev, *_ = lifecycle.build_graph(cp, late, cb, jd, None, graph_version='late', ingested_at=BUILD, observation_end=T + timedelta(days=2))
    assert ep.height == 1 and ep['t_dec'][0] == T + timedelta(days=1, seconds=1)
    child = ev.filter(pl.col('kind') == 'stop_move').row(0, named=True)
    assert child['edge_available_at'] == T + timedelta(days=1)
    assert any('media:' in ref and 'late-hash' in ref for ref in child['dependency_refs'])


def test_s10_edit_only_version_evidence_and_loss(tmp_path):
    original = next(sources.read_all(FIX))
    original = dataclasses.replace(original, media=[], first_seen_at=T, edited_unixtime=int((T-timedelta(minutes=1)).timestamp()), text='BTC 做多 入场 60000 止损 59000', message_type='message', unknown_keys=[], edit_time_problem=None, time_unit_problem=None)
    later = dataclasses.replace(original, edited_unixtime=original.edited_unixtime + 1)
    mv, qs, ledgers, _ = normalize.normalize_messages([original, later], lake.Layout.flat(tmp_path), ingested_at=BUILD)
    assert mv.height == mv['source_version_id'].n_unique() == 2
    assert set(mv['version_evidence']) == {'edit_date'}
    assert mv['event_time'].n_unique() == 2
    assert sum(len(ledger.status) for ledger in ledgers.values()) == 2
    assert sum(len(ledger.mappings) for ledger in ledgers.values()) == 2


@pytest.mark.parametrize('count', ['episodes', 'events', 'decision_roots'])
@pytest.mark.parametrize('reader', [api.load_episodes, api.load_episode_events])
def test_t01_real_parquet_counts_are_verified(built, count, reader):
    manifest = graph.read_manifest(built, 'fixture-v1')
    wrong = {**manifest, 'counts': {**manifest['counts'], count: manifest['counts'][count] + 1}}
    with patch.object(api, '_layout', return_value=built), patch.object(graph, 'read_manifest', return_value=wrong):
        with pytest.raises(LookupError, match='counts mismatch'):
            reader('fixture-v1')


def test_a12_repost_diagnostics_not_run(built):
    batch = pl.read_parquet(built.message_version)['batch_id'][0]
    report = audit.repost_audit_report(built, batch)
    assert report['unconfirmed_repost_candidates'] > 0
    assert report['status'] == 'not_run' and report['confirmation_rate'] is None and report['n_reviewed'] == 0


@pytest.mark.parametrize('bad', ['empty', 'hash', 'labels', 'counts', 'artifact', 'foreign_samples', 'partial_annotation'])
def test_v03_formal_evidence_rejection(tmp_path, bad):
    layout = _audit_artifact(tmp_path)
    args = dict(n_sampled=200, errors_fatal=0, reviewer_ids=('independent',), producer_ids=('producer',), batch_id='b', layout=layout, graph_version='reviewed', input_hash='a' * 64, **_sample('sample'))
    if bad == 'empty':
        args['sample_ids'] = ()
    elif bad == 'hash':
        args['sample_ids_hash'] = 'b' * 64
    elif bad == 'labels':
        args['labels'] = args['labels'][:-1]
    elif bad == 'counts':
        args['errors_general'] = 1
    elif bad == 'foreign_samples':
        args.update(_sample('foreign'))
    elif bad == 'partial_annotation':
        args['labels'] = tuple({**r, 'annotation': {'kind': 'entry_proposal'}} for r in args['labels'])
    else:
        args['input_hash'] = 'b' * 64
    assert audit.acceptance_decision(**args).result == 'insufficient'
    assert audit.read_acceptance_records(layout).height == audit.read_signoffs(layout).height == 0


@pytest.mark.parametrize('field,token', [('qty', '数量'), ('notional', '名义金额')])
def test_s13_quantity_notional_are_compared(field, token):
    text = f'BTC 做多 入场 60000 止损 59000 {token} 1.123456789123'
    image = f'BTC 做多 入场 60000 止损 59000 {token} 2.123456789123'
    result = extract.apply_ocr(extract.parse_message(text), text, ['h'], llm.RecordedOcr({'h': {'size': [256, 80], 'text': image, 'numbers': []}}))
    assert result.size_hint[field] == Decimal('1.123456789123')
    assert any(c['field'] == f'size_hint.{field}' and not c['match'] for c in result.checks['text_image'])
    assert 'TEXT_IMAGE_CONFLICT' in result.reason_codes


def test_s13_same_numbers_without_boxes_never_match():
    text = 'BTC 做多 入场 60000 止损 59000'
    result = extract.apply_ocr(extract.parse_message(text), text, ['h'], llm.RecordedOcr({'h': {'size': [256, 80], 'text': text, 'numbers': [{'value': 60000}, {'value': 59000}]}}))
    numbers = [c for c in result.checks['text_image'] if c['field'] in ('entry.lo', 'entry.hi', 'stop')]
    assert len(numbers) == 3 and all(not c['match'] and not c['evidence_valid'] for c in numbers)
    assert 'OCR_UNREADABLE' in result.reason_codes


def test_s13_normalized_fraction_and_expiry_keep_raw_bbox_evidence():
    text = 'BTC 做多 入场 60000 止损 59000 仓位 10% 1小时有效 数量 2'
    numbers = [{'value': value, 'bbox': [i * 12, 0, i * 12 + 10, 10]} for i, value in enumerate([60000, 59000, 10, 1, 2])]
    provider = llm.RecordedOcr({'h': {'size': [256, 80], 'text': text, 'numbers': numbers}})
    result = extract.apply_ocr(extract.parse_message(text), text, ['h'], provider)
    assert result.reason_codes == []
    for field in ['size_hint.fraction', 'expires_after_s', 'size_hint.qty']:
        comparison = next(c for c in result.checks['text_image'] if c['field'] == field)
        assert comparison['match'] and comparison['evidence_valid']


def test_a18_verbatim_source_string_reaches_gold(tmp_path, monkeypatch):
    """G0 A18 兜底断言：夹具原文里的数字串（64094.879166666667 是 float 无法表示的值）走完整条产线后，gold 里的值 str() 与源串逐字相等。
    任何有损中间转换、四舍五入、单位换算或重新格式化都会让本断言变红，不依赖挑对量级。"""
    import json as _json
    import shutil
    from datetime import UTC, datetime
    from decimal import Decimal
    from quant_lab.data import api
    from quant_lab.data.lake import Layout

    src_entry, src_stop, src_tp = "64094.879166666667", "63000.123456789123", "65500.000000000001"
    assert str(Decimal(src_entry)) != repr(float(src_entry)).rstrip("0")  # 该值经 float 会被改写（前提成立）
    fx = tmp_path / "fx"
    shutil.copytree(FIX, fx)
    rj = fx / "AlphaSignals" / "result.json"
    doc = _json.loads(rj.read_text())
    ts = int(datetime(2026, 2, 1, 9, 0, tzinfo=UTC).timestamp())
    doc["messages"].append({"id": 9901, "type": "message", "date": "2026-02-01T09:00:00", "date_unixtime": str(ts), "from": "Alpha Signals", "from_id": "channel2000000001",
                            "text": f"BTC\n方向：做多\n入场：{src_entry}附近\n止盈：点位1：{src_tp}\n止损：小幅跌破{src_stop}一点。", "text_entities": []})
    rj.write_text(_json.dumps(doc, ensure_ascii=False))
    root = tmp_path / "root"
    api.build(fx, Layout.from_root(root), graph_version="fixture-v1", ingested_at=datetime(2026, 9, 11, tzinfo=UTC))
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(root))
    d = api.load_episodes("fixture-v1")
    row = d.filter((pl.col("channel_id") == ANCH["peers"]["A"]) & (pl.col("root_message_id") == 9901)).row(0, named=True)
    assert str(row["order_plan"]["entries"][0]["price_lo"]) == src_entry
    assert str(row["order_plan"]["stop"]["price"]) == src_stop
    assert str(row["order_plan"]["tps"][0]["level"]) == src_tp
    assert str(row["stop_at_t_dec"]) == src_stop


def test_w01_w02_llm_numeric_evidence_lossless(tmp_path):
    """W01：合法 fraction/qty/notional 证据不崩；W02：录制 JSON 高精度数值无损进入 EX，float 载体拒收。"""
    import json as _json
    from decimal import Decimal
    from quant_lab.data.extract import llm_extract
    from quant_lab.data.llm import SCHEMA_NAME_EXTRACT, RecordedClient, build_extract_prompt, record_key, validate_evidence
    text = "BTC 做多 入场 64094.879166666667 止损 63000.123456789123 仓位 10% 数量 2 名义 10000"
    system, user = build_extract_prompt(text, channel_name="c", message_date=None)
    key = record_key(system, user, SCHEMA_NAME_EXTRACT)
    payload = {"kind": "entry_proposal", "symbol_raw": "BTC", "side": "long", "entry": {"lo": "__E__", "hi": "__E__", "kind": "limit"}, "entries": [], "stop": "__S__",
               "tps": [], "size_hint": {"fraction": "0.1", "qty": "2", "notional": "10000"},
               "spans": [{"field": "entry.lo", "start": text.index("64094"), "end": text.index("64094") + len("64094.879166666667")}, {"field": "entry.hi", "start": text.index("64094"), "end": text.index("64094") + len("64094.879166666667")},
                         {"field": "stop", "start": text.index("63000"), "end": text.index("63000") + len("63000.123456789123")},
                         {"field": "size_hint.fraction", "start": text.index("10%"), "end": text.index("10%") + 3}, {"field": "size_hint.qty", "start": text.index("数量 2") + 3, "end": text.index("数量 2") + 4},
                         {"field": "size_hint.notional", "start": text.index("10000"), "end": text.index("10000") + 5}], "reason_codes": []}
    f = tmp_path / "rec.json"
    raw = _json.dumps({"version": "v", "items": {key: {"response": payload}}}).replace('"__E__"', "64094.879166666667").replace('"__S__"', "63000.123456789123")
    f.write_text(raw)  # JSON 里是高精度数字字面量（不经 Python float）
    client = RecordedClient.from_file(f)  # parse_float=Decimal → 无损
    res, ab, meta = llm_extract(text, client=client, channel_name="c", message_date=None)
    assert ab is None and res is not None, (ab, meta)
    assert str(res.entry["lo"]) == "64094.879166666667" and str(res.stop) == "63000.123456789123"
    # float 载体（内存 fixture 里的 Python float）= 有损 → 拒收，不得当作有证据的正确数值
    lossy = RecordedClient({key: {"response": {**payload, "entry": {"lo": float("64094.879166666667"), "hi": float("64094.879166666667"), "kind": "limit"}, "stop": float("63000.123456789123")}}})  # Python float 载体
    res2, ab2, _ = llm_extract(text, client=lossy, channel_name="c", message_date=None)
    assert res2 is None and ab2 is not None and ("span_mismatch" in ab2.note or "bad_type" in ab2.note)  # float 载体 = 有损，拒收
    # 合法 size_hint 各字段不抛、非法拒收
    exact = {**payload, "entry": {"lo": Decimal("64094.879166666667"), "hi": Decimal("64094.879166666667"), "kind": "limit"}, "stop": Decimal("63000.123456789123")}
    assert validate_evidence(exact, text) == []
    assert any("size_hint" in e for e in validate_evidence({"kind": "x", "size_hint": {"fraction": "abc"}, "spans": []}, text))
    assert any("size_hint" in e for e in validate_evidence({"kind": "x", "size_hint": {"qty": 2.5}, "spans": []}, text))  # float 载体拒收
    assert any("size_hint" in e for e in validate_evidence({"kind": "x", "size_hint": {"fraction": "0.1"}, "spans": []}, text))  # 无 span 不算证据


def test_w03_formal_acceptance_rejects_empty_label_values(tmp_path, monkeypatch):
    """W03：正式抽审/小批全审：关键字段值为空且无 not_applicable 依据 → 不产生 pass/signoff；完整标签仍可 pass。"""
    from datetime import UTC, datetime
    from quant_lab.data import api, audit
    from quant_lab.data.graph import resolve_alias, read_manifest
    from quant_lab.data.lake import Layout
    root = tmp_path / "root"
    api.build(FIX, Layout.from_root(root), graph_version="fixture-v1", ingested_at=datetime(2026, 9, 11, tzinfo=UTC))
    lay = Layout.from_root(root)
    gv = resolve_alias(lay, "fixture-v1")
    man = read_manifest(lay, gv)
    ep = pl.read_parquet(lay.episode(gv))
    assert ep["batch_id"].n_unique() == 1
    batch = ep["batch_id"][0]
    ids = tuple(ep["episode_id"].to_list())  # 小批全审：全部实物 episode
    n = len(ids)
    def labels(empty: bool):
        return tuple({"sample_id": i, "reviewer_id": "rev-a", "severity": "ok",
                      "annotation": {"kind": "entry_proposal", "instrument_id": None if empty else "BTCUSDT-PERP.BINANCE-UM", "side": None if empty else "long",
                                     "entry": None if empty else "60000", "stop": None if empty else "59000"}} for i in ids)
    common = dict(errors_general=0, errors_fatal=0, plan=audit.AcceptancePlan(n=200, c=3), layout=lay, batch_id=batch, batch_size=n, sample_ids=ids, sample_ids_hash=audit.sample_identity(list(ids)),
                  reviewer_ids=("rev-a",), producer_ids=("prod-x",), graph_version=gv, input_hash=man["input_hash"], full_audit=True)  # 小批（<200）全审
    r_empty = audit.acceptance_decision(n_sampled=n, labels=labels(True), **common)
    assert r_empty.result != "pass" and audit.read_acceptance_records(lay).height == 0
    r_ok = audit.acceptance_decision(n_sampled=n, labels=labels(False), **common)
    assert r_ok.result == "pass" and audit.read_acceptance_records(lay).height == 1, (r_ok.result, r_ok.reason)
