"""Live registration, exact root-version reads, L0 audit, and kernel A acceptance."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
import json

import polars as pl
import pytest

from quant_lab.data import api
from quant_lab.data.lake import Layout
from quant_lab.market import contract as c, l0_replay as l0
from quant_lab.market.execution import simulate
from quant_lab.market.kernel_a import KernelA
from quant_lab.market.partition_check import rules_path
from tests.market.test_l0_replay import built  # synthetic graph/market, G1 builds, G2 consumes
from tests.market.test_live_profile import plan
from tests.data.l0_fixtures import CHANNEL, T0

BASES = ("base-v1-timeexit", "base-v1-timeexit-be1", "base-v1-timeexit-w60", "base-v1-timeexit-w60-be1")
# Independent pinned registry from before this change, not derived from current policies.
LEGACY_HASHES = {
    "fixture-zero-v1": "12ca10ebb10ef27d10873584ee42a224c44efdf99e9b55c7656e8a838c58cd1d",
    "fixture-tick-v1": "79a818107c42ffc1aec9af8ca4beb672231adfaf4b514c8eb0b63664fbb0238f",
    "fixture-halftp-v1": "b5b9db9aaec5d88e1574d028a0807cdd34cff3d5141d93f2390edd69ccf338c9",
    "fixture-wallet99-v1": "982b510678f507dfec7269216e68ba24479f161624cdfebf48bc0b28d417f41f",
    "base-v1": "12f83216444dff123f7cc6e9b4f4752449b8a678555079f033cbb9980f07d5db",
    "base-v1-timeexit": "0e2a374b36d286f418d350f9159e34d73e4e56bf3280251df3531273450cf424",
    "base-v1-timeexit-w14": "a36932b0e2050366342a69d47e9fd73d84aa08474f95fa2cfc51bc202e898b53",
    "base-v1-timeexit-be1": "c608338ed32dc1738adb7cc08bd3e9b82f9809f59d672b50fb9033588c79f0d2",
    "base-v1-timeexit-w14-be1": "8fcc23b1ebcb2a3a0f4fe65b37aad11390096e44adc823aadefb1f6c68e906cd",
    "base-v1-timeexit-w60": "356e64c391e5e0bb93618b5c436b5c6ae9eb9fcaa82b84d3ff4c0f0f225b1108",
    "base-v1-timeexit-w60-be1": "65f4296ba72dc0cfa0b3b71e27c61a218c5a51a13e7604c01bd63513a1f73f51",
}


def row(source=None):
    return {"episode_id": "live-synthetic", "graph_version": "synthetic-v1", "decision_snapshot_hash": "synthetic",
            "root_source_version_id": "root-v1", "t_dec": T0, "instrument_id": "BTCUSDT-PERP.BINANCE-UM",
            "order_plan": plan() if source is None else source}


def request(source, version, horizon=180):
    return c.build_request(source, policy_version=version, policy_hash=c.resolve_policy(version).content_hash,
                           risk_budget=D(1), market_manifest="synthetic", horizon_end=T0 + timedelta(seconds=horizon))


def market(side="long"):
    prices = ("110", "100", "140") if side == "long" else ("90", "100", "60")
    points = [c.PricePoint(ts=T0 + timedelta(seconds=i * 60), price=D(p)) for i, p in enumerate(prices)]
    return c.MarketView(manifest_id="synthetic", last=points, mark=points,
                        rules=c.Rules(tick_size=D("0.0001"), step_size=D("0.000001")))


def test_old_hashes_and_disabled_policy_bytes_are_pinned():
    registry = c.load_policy_registry()
    for version, old_hash in LEGACY_HASHES.items():
        policy = c.resolve_policy(version)
        assert registry[version] == policy.content_hash == old_hash
        assert policy.live_execution_profile is False
        explicit = policy.model_copy(update={"live_execution_profile": False})
        assert c.canonical_json(policy.model_dump()) == c.canonical_json(explicit.model_dump())
        assert "live_execution_profile" not in policy.model_dump()
    follow_bases = ("base-v1-timeexit", "base-v1-timeexit-live", "base-v1-timeexit-w60", "base-v1-timeexit-w60-live")
    assert set(registry) == set(LEGACY_HASHES) | {v + "-live" for v in BASES} | {v + "-follow" for v in follow_bases}
    for base in BASES:
        live = c.resolve_policy(base + "-live")
        body = live.model_dump()
        assert body.pop("live_execution_profile") is True
        body["version"] = base
        assert c.canonical_json(body) == c.canonical_json(c.resolve_policy(base).model_dump())


@pytest.mark.parametrize("version", LEGACY_HASHES)
def test_disabled_request_and_kernel_result_bytes_are_identical(version):
    source = row()
    policy = c.resolve_policy(version)
    fixed, audit = l0.prepare_execution(source, policy=policy, text="入场100附近\n止损略破80", tick_size=D("NaN"))
    assert fixed is source and audit is None
    old, off = request(source, version), request(fixed, version)
    assert old.model_dump_json().encode() == off.model_dump_json().encode()
    assert simulate(old, market=market()).model_dump_json().encode() == simulate(off, market=market()).model_dump_json().encode()


@pytest.mark.parametrize("base", BASES)
@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("shape", ["limit", "zone", "pair"])
def test_four_live_policies_construct_and_run_kernel_a(base, side, shape):
    source = row(plan(side, zone=shape == "zone", pair=shape == "pair"))
    source["order_plan"]["tps"][0]["fraction"] = D("0.5")
    before = deepcopy(source)
    policy = c.resolve_policy(base + "-live")
    stop = source["order_plan"]["stop"]["price"]
    exact, _ = l0.prepare_execution(source, policy=policy, tick_size=D("0.0001"))
    assert exact["order_plan"]["stop"]["price"] == stop   # 精确止损不让点
    fixed, audit = l0.prepare_execution(source, policy=policy, text=f"止损{stop}附近", tick_size=D("0.0001"))
    req = request(fixed, policy.version)
    res = simulate(req, market=market(side))
    assert res.kernel == "A" and res.censor_reason is None and res.filled_qty > 0
    assert res.outcome_kind in {"tp_hit", "time_exit"}
    assert source == before and req.order_plan.stop.price != source["order_plan"]["stop"]["price"]
    if policy.breakeven_after_first_tp:
        assert any(e.kind == "amended" and e.order_id == "sl-0" and e.price == res.entry_avg_price for e in res.canonical_events)
    kernel = KernelA(req, market(side))
    total = kernel.sizing_total()
    quantities = [c.floor_step(total * f, kernel.rules.step_size) for f in req.entry_fractions]
    prices = [e.price_lo for e in req.order_plan.entries]
    risks = [q * abs(p - req.order_plan.stop.price) for q, p in zip(quantities, prices)]
    if shape == "zone":
        for actual, share in zip(risks, [D("0.55"), D("0.30"), D("0.15")]):
            assert abs(actual - D(1) * share) < D("0.0001")
    if shape == "pair":
        assert abs(quantities[0] * prices[0] - quantities[1] * prices[1]) < D("0.0002")


def test_bronze_accessor_uses_exact_root_versions_and_rejects_ambiguity(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path))
    layout = Layout.from_root(None)
    layout.ensure()
    pl.DataFrame({"source_version_id": ["root-v1", "root-v2", "management", "duplicate", "duplicate", "null"],
                  "text": ["入场100", "入场100附近", "止损略破80", "精确", "模糊", None]}).write_parquet(layout.message_version)
    assert api.load_message_texts(["root-v1", "management", "missing", "duplicate", "null"]) == {
        "root-v1": "入场100", "management": "止损略破80"}
    assert api.load_message_texts([]) == {}
    layout.message_version.unlink()
    assert api.load_message_texts(["root-v1"]) == {}


def test_l0_live_audit_and_summary_use_root_version_without_changing_source(built, monkeypatch):
    root, _ = built
    original = l0.load_episodes
    versions = original("l0-test").filter(pl.col("channel_id") == CHANNEL)["root_source_version_id"].to_list()
    text = {versions[0]: "BTC 做多 入场100附近\n止损略破90\n目标110"}
    calls = []

    def reader(ids):
        calls.append(ids)
        return text

    monkeypatch.setattr(l0, "load_message_texts", reader)
    captured = []
    build = l0.build_request

    def capture(source, **kwargs):
        req = build(source, **kwargs)
        captured.append(req)
        return req

    monkeypatch.setattr(l0, "build_request", capture)
    before = original("l0-test")
    report = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "live", policy_version=BASES[0] + "-live")
    table = pl.read_parquet(root / "live" / "trades.parquet")
    assert calls == [versions]
    assert original("l0-test").equals(before)
    audit = [json.loads(value) for value in table["live_execution_json"]]
    by_version = {a["source_version_id"]: a for a in audit}
    applied = by_version[versions[0]]
    assert applied["execution_plan"]["entries"][0]["price_lo"] == "100.1"
    assert applied["execution_plan"]["stop"]["price"] == "89.6"
    assert table["entries"][0][0]["price_lo"] == D(100) and table["stop"][0] == D(90)
    profile = report["live_execution_profile"]
    assert profile["n_applied"] == 2 and profile["n_root_text_unresolved"] == 1
    # 只有带措辞的那一单让点：入场附近、止损略破（外扩+放宽）；精确的「目标110」和无原文的那单原值执行。
    assert profile["rule_counts"] == {"entry_concession": 1, "stop_breakout": 1, "stop_widening": 1,
        "take_profit_concession": 0, "zone_ladder": 0, "equal_notional_pair": 0, "tick_rounding": 1}
    assert captured[0].order_plan.entries[0].price_lo == D("100.1")
    for path in ("summary.json", "summary.md"):
        content = (root / "live" / path).read_text()
        assert "live_execution_profile" in content and "BTC 做多" not in content
        assert "SKILL.md:27" in content


def test_l0_off_has_no_new_columns_or_source_reads(built, monkeypatch):
    root, _ = built

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled profile must not read text/rules or transform plans")

    monkeypatch.setattr(l0, "load_message_texts", forbidden)
    monkeypatch.setattr(l0, "load_rules", forbidden)
    monkeypatch.setattr(l0, "apply_live_profile", forbidden)
    report = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "off")
    assert "live_execution_profile" not in report
    assert "live_execution_json" not in pl.read_parquet(root / "off" / "trades.parquet").columns


def test_missing_tick_keeps_kernel_rule_censor_and_empty_live_output(built):
    root, lake = built
    rules_path(lake, "BTCUSDT-PERP.BINANCE-UM").unlink()
    report = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "missing", policy_version=BASES[0] + "-live")
    assert report["censor_counts"] == {"RULE_HISTORY_MISSING": 2}
    assert report["live_execution_profile"]["n_rules_unresolved"] == 2
    assert all(value == 0 for value in report["live_execution_profile"]["rule_counts"].values())
    empty = l0.replay(graph_version="l0-test", channel=-1009999999999, out=root / "empty", policy_version=BASES[0] + "-live")
    assert empty["live_execution_profile"]["n_applied"] == 0
    assert pl.read_parquet(root / "empty" / "trades.parquet").schema["live_execution_json"] == pl.String
