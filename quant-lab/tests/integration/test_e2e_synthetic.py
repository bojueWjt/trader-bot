"""OR-04 端到端合成冒烟与 OR-05 I06 判别力验收（G0 集成，不改模块代码）。

链路：G1 load_episodes（合成夹具湖） → G2 build_request + simulate_batch（合成行情）
      → G3 freeze_opportunity_set + feature_snapshot + evaluate。

保留 θ、账本、非退化门；加固逐值时钟、完整配对、本批 Q/MAP/LOSS 与突变拒绝证据。
每个接缝独立成 test，失败即定位到具体接缝，
G0 据此判 P1 缺项；**不得**为了让它变绿去改 src/quant_lab/*。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import polars as pl
import pytest

POLICY = "base-v1"
RISK_BUDGET = Decimal("100")
MANIFEST = "mm-e2e-0001"
GRAPH = "or05-i06-fresh"
T_BUILD = dt.datetime(2026, 9, 11, tzinfo=dt.UTC)


# --------------------------------------------------------------- G1 接缝
@pytest.fixture(scope="module")
def fresh_lake(tmp_path_factory):
    """每轮从空湖重建；记录原始输入、当前代码与本轮发布身份，不借用已发布 gold。"""
    from quant_lab.data import api
    from quant_lab.data.lake import Layout

    project = Path(__file__).resolve().parents[2]
    fixtures = project / "tests/data/fixtures"
    root = tmp_path_factory.mktemp("or05-i06") / "data"
    root.mkdir()
    assert not list(root.iterdir())
    inputs = sorted((fixtures / "tdesktop_sample").rglob("*")) + [
        fixtures / "llm_recorded/extract_v1.json", fixtures / "llm_recorded/ocr_v1.json"]
    sources = sorted((project / "src/quant_lab/data").glob("*.py"))
    hashes = {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in inputs + sources if p.is_file()}
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("QUANT_LAB_DATA_ROOT", str(root))
        layout = Layout.from_root(None)
        started = time.perf_counter()
        built = api.build(fixtures / "tdesktop_sample", layout, graph_version=GRAPH,
                          llm_fixture=fixtures / "llm_recorded/extract_v1.json",
                          ocr_fixture=fixtures / "llm_recorded/ocr_v1.json", ingested_at=T_BUILD)
        elapsed = time.perf_counter() - started
        trace = {"graph_version": GRAPH, "batch_id": built["normalize"]["batch_id"],
                 "input_hash": built["lifecycle"]["input_hash"], "files": hashes,
                 "build_seconds": elapsed}
        (root / "integration-build.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
        print(f"\nI06 fresh build: {elapsed:.3f}s; root={root}; batch={trace['batch_id']}")
        yield {"layout": layout, "trace": trace, "project": project}


@pytest.fixture(scope="module")
def episodes(fresh_lake) -> pl.DataFrame:
    from quant_lab.data.api import load_episodes
    return load_episodes(GRAPH)


def _assert_build_identity(fresh_lake, episodes):
    from quant_lab.data.graph import verify_manifest
    from quant_lab.data.lifecycle import input_hash_of
    from quant_lab.data.lake import Layout

    layout, trace = fresh_lake["layout"], fresh_lake["trace"]
    assert Layout.from_root(None) == layout, "本轮数据根错配"
    manifest = verify_manifest(layout, GRAPH)
    assert set(episodes["graph_version"]) == {GRAPH}, "本轮图身份错配"
    assert set(episodes["batch_id"]) == {trace["batch_id"]}, "本轮批次错配"
    assert manifest["input_hash"] == trace["input_hash"] == input_hash_of(layout, ingested_at=T_BUILD)
    for name, expected in trace["files"].items():
        assert hashlib.sha256((fresh_lake["project"] / name).read_bytes()).hexdigest() == expected


def test_seam_g1_fresh_build_identity(fresh_lake, episodes):
    _assert_build_identity(fresh_lake, episodes)


def test_seam_g1_decision_view(episodes):
    """G1 决策视图提供 G2/G3 需要的全部契约列（research-schema §9.1）。"""
    assert episodes.height > 0, "load_episodes 返回空 —— 静默为空算 fail"
    required = ("episode_id", "graph_version", "decision_snapshot_hash", "t_dec", "order_plan",
                "instrument_id", "cluster_id", "is_tombstone", "right_censored")
    missing = [c for c in required if c not in episodes.columns]
    assert not missing, f"gold/episode 缺契约列 {missing}"
    assert episodes["t_dec"].null_count() < episodes.height, "t_dec 全空"


# --------------------------------------------------------------- G2 接缝
def _resolver(req):
    """合成行情：从 t_start 起每分钟一个价点，由入场中值走向首个 TP 并小幅超越（保证成交与出场）。

    同时提供**真实档位规则**：默认 `Rules()` 的 step_size=1 会让 ETH/BTC 这类价格的按风险预算定量
    直接撞 LOT_SIZE 被拒（G2 的 fail-closed 正确行为），那样整批样本零成交，θ 会在"没有实验"的
    样本上算出来——OR-04 曾因此连绿六轮（见 G0 R28 记录）。
    """
    from quant_lab.market.contract import MarketView, PricePoint, Rules
    plan = req.order_plan
    lo = min(e.price_lo for e in plan.entries)
    hi = max(e.price_hi for e in plan.entries)
    mid = (lo + hi) / 2
    if plan.tps:
        target = plan.tps[-1].level
    else:
        target = mid * Decimal("1.02") if plan.side == "long" else mid * Decimal("0.98")
    over = Decimal("1.01") if plan.side == "long" else Decimal("0.99")
    target = target * over
    n = 240
    tick = Decimal("0.01")
    pts = []
    t = req.t_start or req.t_dec
    for i in range(n + 1):
        ts = t + dt.timedelta(minutes=i)
        if ts > req.horizon_end:
            break
        # 必须按 tick 量化：直接相除会产生超过标度 12 的小数，撞 §9.10.1 A8 的 Decimal(38,12) 域校验，
        # 而 simulate_batch(strict=False) 会把这类 ContractError 吞成 null 行（G0 R28 自查发现）。
        raw = mid + (target - mid) * Decimal(i) / Decimal(n)
        pts.append(PricePoint(ts=ts, price=(raw / tick).quantize(Decimal(1), rounding=ROUND_DOWN) * tick))
    rules = Rules(tick_size=Decimal("0.01"), step_size=Decimal("0.001"),
                  min_notional=Decimal("5"), min_qty=Decimal("0.001"))
    return MarketView(manifest_id=req.market_manifest, last=list(pts), mark=list(pts), rules=rules)


@pytest.fixture(scope="module")
def execution(episodes) -> pl.DataFrame:
    return _execute(episodes)


def _execute(episodes):
    from quant_lab.market.contract import build_request, resolve_policy
    from quant_lab.market.execution import simulate_batch
    ph = resolve_policy(POLICY).content_hash
    reqs = []
    for row in episodes.filter(pl.col("t_dec").is_not_null()).iter_rows(named=True):
        if row.get("order_plan") is None:
            continue
        reqs.append(build_request(row, policy_version=POLICY, policy_hash=ph, risk_budget=RISK_BUDGET,
                                 market_manifest=MANIFEST))
    assert reqs, "build_request 未能从 G1 episode 构造出任何请求"
    return simulate_batch(reqs, kernel="A", resolver=_resolver, strict=False)


def test_seam_g2_build_request_is_sole_entrypoint(execution, opportunity):
    """G2 侧：请求只经 build_request 构造（research-schema §9.4 裁定 A3），输出一行一 episode。"""
    assert execution.height > 0, "simulate_batch 返回空表"
    _assert_complete_execution(execution, opportunity)
    assert "policy_hash" in execution.columns, "配对键缺 policy_hash（execution-interface §5.5）"
    assert execution["policy_hash"].n_unique() == 1
    if "error" in execution.columns:
        errs = execution.filter(pl.col("error").is_not_null())
        assert errs.height == 0, f"内核报错 {errs.height} 行：{errs['error'].to_list()[:3]}"


def test_e2e_sample_is_not_degenerate(execution):
    """样本非退化门（G0 R28 新增）。

    OR-04 曾连绿六轮而整批零成交：行情湖只登记了 BTCUSDT 规则，其余品种走 `Rules()` 默认
    step_size=1，按风险预算定的量直接撞 LOT_SIZE，G2 fail-closed 全部拒单——而主断言只查
    「θ 非空」，于是 θ 在一个**没有发生过实验**的样本上被算了出来并判绿。
    这是"静默为空"的变体：表不空，但里面没有事件。故单列此门。
    """
    n = execution.height
    if "error" in execution.columns:
        assert execution["error"].null_count() == n, "有请求被 strict=False 吞成 null 行，先修再谈 θ"
    filled = execution.filter(pl.col("fill_status") != "none").height
    assert filled >= n // 2, f"成交样本仅 {filled}/{n} —— 样本退化，θ 无意义"
    kinds = set(execution["outcome_kind"].drop_nulls().to_list())
    assert not kinds <= {"rejected", "unevaluable"}, f"全部为未挂出/不可评估 {kinds} —— 没有实验"
    # G0 R69 自审修正：原断言只查「取值数 ≥ 2」，78 个 right_censored + 2 个 rejected 即可满足，
    # 而那是一个「什么都没走完」的样本。退化与否要看**有没有真正走完的结局**，不是标签种类数。
    resolved = execution.filter(pl.col("outcome_kind").is_in(["tp_hit", "stopped", "filled_closed"])).height
    assert resolved >= execution.height // 4, (
        f"走完结局的样本仅 {resolved}/{execution.height} —— 全部停在删失/未挂出，θ 建立在无人走完的样本上")


# --------------------------------------------------------------- G3 接缝
@pytest.fixture(scope="module")
def opportunity(episodes):
    from quant_lab.research.evaluator import freeze_opportunity_set
    return freeze_opportunity_set(episodes)


def test_seam_g3_opportunity_set_frozen(opportunity):
    assert opportunity.n_clusters > 0, "机会集为空"
    assert len(opportunity.episode_ids) > 0


def test_seam_g3_pair_arms_consumes_g2_output(execution, opportunity):
    """G3 的两臂配对能直接消费 G2 simulate_batch 输出（不经 fake_execution）。"""
    from quant_lab.research.evaluator import pair_arms
    ids = _assert_complete_execution(execution, opportunity)
    pairs = pair_arms(execution, ids)
    assert pairs.height == len(ids)
    assert set(pairs["episode_id"]) == set(ids)
    for c in ("base_R", "cand_R", "base_censor", "cand_censor"):
        assert c in pairs.columns


def _assert_complete_execution(execution, opportunity):
    ids = opportunity.episode_ids
    assert ids, "完整机会集为空"
    assert set(execution["episode_id"]) == set(ids), "执行结果与完整机会集不相等"
    assert execution.height == len(ids), "执行结果不是一行一 episode"
    return ids


T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
MINUTE = dt.timedelta(minutes=1)
US = dt.timedelta(microseconds=1)
INST = "BTCUSDT-PERP.BINANCE-UM"
CLOSE = {"field": "close"}
REF = {"op": "Ref", "args": [CLOSE], "params": {"lag": 1}}


def _clock_bars():
    return pl.DataFrame({"instrument_id": [INST] * 6, "interval": ["1m"] * 6,
                         "close_time": [T0 + i * MINUTE for i in range(6)],
                         "close": [100., 200., 300., 400., 500., 600.]})


def _anchors(times):
    return pl.DataFrame({"episode_id": [f"clock-{i}" for i in range(len(times))],
                         "instrument_id": [INST] * len(times), "t_dec": times})


def _assert_snapshot(snap, anchors, ast, expected):
    """逐 ID 检查身份、有效位与值，invalid 也必须是 None，不能靠列名蒙混过关。"""
    from quant_lab.research.ast import canonical_hash
    assert snap.height == anchors.height, "feature_snapshot 行数与 anchors 不一致"
    h = canonical_hash(ast)
    assert {f"f_{h}", f"validity_{h}"} <= set(snap.columns)
    assert snap["episode_id"].n_unique() == anchors.height
    rows = {r["episode_id"]: r for r in snap.iter_rows(named=True)}
    assert set(rows) == set(anchors["episode_id"])
    for anchor, value in zip(anchors.iter_rows(named=True), expected, strict=True):
        row = rows[anchor["episode_id"]]
        assert row["t_dec"] == anchor["t_dec"]
        assert row[f"validity_{h}"] is (value is not None), "时钟有效性不符"
        assert row[f"f_{h}"] == value, "时钟特征值不符"


def _assert_feature_clock(bars, anchors, ast, expected, **kwargs):
    from quant_lab.research import features
    snap = features.feature_snapshot([ast], anchors, bars=bars, **kwargs)
    _assert_snapshot(snap, anchors, ast, expected)
    # 只改所有决策之后的价格；必须实际存在未来行，避免无效扰动。
    future = pl.col("close_time") > anchors["t_dec"].max()
    assert bars.filter(future).height > 0
    changed = bars.with_columns(pl.when(future).then(999999.).otherwise(pl.col("close")).alias("close"))
    after = features.feature_snapshot([ast], anchors, bars=changed, **kwargs)
    _assert_snapshot(after, anchors, ast, expected)
    assert after.equals(snap), "未来扰动改变了过去快照"


def _time_kwargs(path, latency):
    from quant_lab.research.features import SnapshotContext
    if path == "context":
        return {"ctx": SnapshotContext(latency=latency)}
    return {"latency": latency}


@pytest.mark.parametrize("path", ["convenience", "context"])
@pytest.mark.parametrize("latency", [dt.timedelta(0), US, MINUTE])
def test_seam_g3_feature_snapshot_respects_t_dec(path, latency):
    """H0 等号可见；等号前 1us 的 Ref 尚无历史，不能伪造成任意有效值。"""
    edge = T0 + MINUTE + latency
    anchors = _anchors([edge - US, edge, edge + MINUTE])
    _assert_feature_clock(_clock_bars(), anchors, REF, [None, 100., 200.], **_time_kwargs(path, latency))


def test_seam_g3_unknown_dependency_clock():
    """I03：当前 bar 已知不代表 Ref 所依赖的历史 bar 已知。"""
    bars = _clock_bars().with_columns(pl.col("close_time").alias("available_at"))
    anchors = _anchors([T0 + MINUTE, T0 + 2 * MINUTE, T0 + 3 * MINUTE])
    for available, expected in [(None, None), (T0 + 2 * MINUTE, 200.), (T0 + 2 * MINUTE + US, None)]:
        changed = bars.with_columns(pl.when(pl.col("close_time") == T0 + MINUTE)
                                   .then(pl.lit(available, dtype=pl.Datetime("us", "UTC")))
                                   .otherwise(pl.col("available_at")).alias("available_at"))
        _assert_feature_clock(changed, anchors, REF, [100., expected, 300.])


def _assert_market_clock():
    from quant_lab.market import asof
    bars = _clock_bars().head(3).with_columns(pl.Series("available_at", [
        T0 + 2 * MINUTE, T0 + 4 * MINUTE, T0 + 8 * MINUTE]))
    # 显式到达无顺序证据，等号仍不可见；晚到的已闭合 bar 不抢占旧值。
    for at, expected in [(T0 + 2 * MINUTE, None), (T0 + 2 * MINUTE + US, 100.),
                         (T0 + 4 * MINUTE, 100.), (T0 + 4 * MINUTE + US, 200.)]:
        future = pl.col("available_at") > at
        changed = bars.with_columns(pl.when(future).then(999999.).otherwise(pl.col("close")).alias("close"))
        for data in (bars, changed):
            mark = asof.mark_bar_at(data, at, INST, max_staleness_s=600)
            row = asof.last_closed_bar(data, at=at, instrument_id=INST, interval="1m")
            price = None if row is None else row["close"][0]
            assert price == expected, "G2 晚到 bar 可见性不符"
            assert mark.price == expected, "G2 标记价可见性不符"
            assert mark.reason == ("MARK_STALE" if expected is None else None)
    # H0 无显式到达时采用 close+latency，等号可见；前 1us 不可见。
    for latency in (dt.timedelta(0), US, MINUTE):
        data = _clock_bars().head(1)
        for at, expected in [(T0 + latency - US, None), (T0 + latency, 100.)]:
            result = asof.mark_bar_at(data, at, INST, latency=latency)
            assert result.price == expected, "G2 H0 等号不符"


def test_seam_g2_late_bar_clock():
    _assert_market_clock()


def _assert_negative_latency(path, latency):
    from quant_lab.market import asof
    from quant_lab.research import features
    bars, anchors = _clock_bars(), _anchors([T0])
    # 用显式 AssertionError 表达守域失败，使突变证据仅捕获测试自己的拒绝。
    try:
        features.feature_snapshot([CLOSE], anchors, bars=bars, **_time_kwargs(path, latency))
    except features.SnapshotInvalid as exc:
        assert "latency" in str(exc)
    else:
        raise AssertionError("G3 接受负 latency")
    for call in (lambda: asof.mark_bar_at(bars, T0, INST, latency=latency),
                 lambda: asof.last_closed_bar(bars, at=T0, instrument_id=INST, interval="1m", latency=latency)):
        try:
            call()
        except asof.LatencyDomainInvalid:
            pass
        else:
            raise AssertionError("G2 接受负 latency")


@pytest.mark.parametrize("path", ["convenience", "context"])
@pytest.mark.parametrize("latency", [-US, -MINUTE])
def test_seam_negative_latency_clock(path, latency):
    _assert_negative_latency(path, latency)


def test_e2e_theta_ledger_loss_quarantine(episodes, execution, opportunity, tmp_path, fresh_lake):
    """OR-04 主断言：θ 非空、账本 ≥1 行（本次 run 有 completed）、损耗表 ≥5 层、quarantine 可读。

    真链路：G1 决策视图 → G2 build_request/simulate_batch（合成行情）→ G3 feature_snapshot + evaluate。
    账本用临时 lockbox（research-schema §9.8 A6 / G0 R7 裁定：测试不得读写持久 data/lockbox）。
    两臂同 policy（base-v1），θ 期望为 0 但必须非 None——"非空"是接缝断言，不是效应声明。
    """
    from quant_lab.research import synthetic
    from quant_lab.research.ast import canonical_hash
    from quant_lab.research.evaluator import evaluate
    from quant_lab.research.features import feature_snapshot
    from quant_lab.research.ledger import Ledger

    # --- θ：特征快照 + 评估
    ids = _assert_complete_execution(execution, opportunity)
    anchors = episodes.filter(pl.col("episode_id").is_in(ids)).select("episode_id", "instrument_id", "t_dec")
    insts = tuple(anchors["instrument_id"].unique().sort().to_list())
    t0 = anchors["t_dec"].min() - dt.timedelta(days=30)
    # bars 必须覆盖全部 anchors 的 t_dec（否则 validity=False 触发 NAN_RATE 拒评，那是夹具问题不是模块问题）
    span = anchors["t_dec"].max() - t0
    n_bars = int(span / dt.timedelta(minutes=15)) + 200
    bars = synthetic.fake_bars(insts, start=t0, n_bars=n_bars, interval="15m", seed=11)
    ast = {"op": "Ref", "args": [{"field": "close"}], "params": {"lag": 1}}
    feats = feature_snapshot([ast], anchors, bars=bars)
    h = canonical_hash(ast)

    ledger = Ledger(root=tmp_path / "lockbox", scope="or04-e2e")
    attempt = ledger.reserve(origin="human", canonical_hash=h, params={"lag": 1}, fold_id="e2e-f0",
                             visible_cutoff=anchors["t_dec"].max(), objective="theta",
                             data_manifest=str(episodes["graph_version"][0]), seed=11, stage="outer",
                             market_manifest=MANIFEST, graph_version=str(episodes["graph_version"][0]))
    res = evaluate(ast, opportunity, features=feats, rule=lambda f: pl.Series([True] * f.height),
                   execution=execution, fold_id="e2e-f0",
                   attempt_id=attempt, ledger=ledger)
    assert res.status == "ok", f"evaluate 非 ok：status={res.status} reason={res.reason}"
    assert res.theta is not None, "θ 为空 —— 静默为空算 fail"
    assert res.n_evaluated > 0, "共同可评机会数为 0"
    # 非退化：θ 必须建立在真的发生过成交的样本上（见 test_e2e_sample_is_not_degenerate）
    assert execution.filter(pl.col("fill_status") != "none").height >= execution.height // 2, \
        "θ 建立在零成交样本上 —— 不是没有效应，是没有实验"

    # --- 账本：本次 run 至少 1 行 completed
    led = ledger.read()
    assert led.height >= 1, "账本为空"
    assert (led["status"] == "completed").sum() >= 1, f"账本无 completed 行：{led['status'].to_list()}"

    # --- 本轮损耗与固定隔离对象（独立子测试也复用同一断言）
    _assert_loss_quarantine(fresh_lake)


def test_e2e_loss_table_and_quarantine_readable(fresh_lake):
    """不经 G2 的独立子条件：本批六层损耗、隔离对象与逐 ID 映射均可对账。"""
    _assert_loss_quarantine(fresh_lake)


# 期望来自原始夹具 ANCHORS，不能从待检的 Q/MAP 反推，否则漏样本仍会绿。
QUARANTINED = {9: "TIME_UNIT_INVALID", 10: "MEDIA_MISSING", 11: "SCHEMA_DRIFT"}
QUARANTINE_PEER = -1002000000003


def _read_mapping(layout, batch, layer):
    return pl.read_parquet(layout.mapping(batch, layer))


def _assert_loss_quarantine(fresh_lake):
    from quant_lab.data import api
    from quant_lab.data.lake import Layout

    layout, trace = fresh_lake["layout"], fresh_lake["trace"]
    assert Layout.from_root(None) == layout
    batch = trace["batch_id"]
    loss = api.loss_table(batch)
    assert loss.height > 0, "损耗表为空 —— 静默为空算 fail"
    assert set(loss["batch_id"]) == {batch}, "损耗表批次错配"
    assert set(loss["layer"]) == {1, 2, 3, 4, 5, 6}, "损耗表层数或层身份不符"
    q = api.quarantine("telegram")
    assert set(q["batch_id"]) == {batch}, "隔离表批次错配"
    mv = pl.read_parquet(layout.message_version)
    mapping = _read_mapping(layout, batch, 1)
    for mid, reason in QUARANTINED.items():
        object_id = f"{QUARANTINE_PEER}:{mid}"
        original = mv.filter((pl.col("channel_id") == QUARANTINE_PEER)
                             & (pl.col("source_id").struct.field("message_id") == mid))
        assert original.height == 1, f"固定输入消失 {object_id}"
        source = original.row(0, named=True)
        assert source["quality_status"] == "quarantined"
        isolated = q.filter((pl.col("object_id") == object_id) & (pl.col("reason_code") == reason))
        assert isolated.height == 1, f"固定隔离对象消失 {object_id}"
        assert isolated["object_version"][0] == source["source_version_id"]
        assert isolated["status"][0] == "open"
        input_ref = f"{object_id}:{source['raw_hash'][:8]}:{source['raw_index']}:{source['source_version_id']}"
        mapped = mapping.filter(pl.col("input_ref") == input_ref)
        assert mapped.height == 1, f"固定 MAP 对象消失 {object_id}"
        assert mapped["output_ref"][0] == source["source_version_id"]
        assert mapped["status"][0] == "quarantine" and reason in mapped["reason_codes"][0]
        stratum = mapped["stratum"][0]
        lost = loss.filter((pl.col("layer") == 1) & (pl.col("stratum") == stratum))
        assert lost.height == 1, f"固定 LOSS 分层消失 {object_id}"
        assert json.loads(lost["primary_reason_dist"][0]).get(reason, 0) >= 1
    # LOSS 只有聚合数；以 MAP 的逐 ID 状态重算各层/分层，防止只保留空壳或伪造守恒。
    cumulative = {}
    for layer in range(1, 7):
        mapping = _read_mapping(layout, batch, layer)
        assert set(mapping["batch_id"]) == {batch}
        assert set(mapping["layer"]) == {layer}
        layer_loss = loss.filter(pl.col("layer") == layer)
        assert set(layer_loss["stratum"]) == set(mapping["stratum"])
        for row in layer_loss.iter_rows(named=True):
            mapped = mapping.filter(pl.col("stratum") == row["stratum"])
            inputs = mapped.select("input_ref", "status").unique()
            assert inputs["input_ref"].n_unique() == inputs.height
            assert row["input_n"] == inputs.height, "LOSS 输入计数与 MAP 不符"
            for state in ("ok", "review", "quarantine", "dup_ref"):
                assert row[f"n_{state}"] == inputs.filter(pl.col("status") == state).height, "LOSS 状态计数与 MAP 不符"
            assert row["input_n"] == sum(row[f"n_{s}"] for s in ("ok", "review", "quarantine", "dup_ref"))
            excluded = mapped.filter(pl.col("status").is_in(["review", "quarantine", "dup_ref"])
                                     | (pl.col("relation") == "excluded"))
            seen = cumulative.setdefault(row["stratum"], set())
            seen.update(excluded["input_ref"])
            assert row["cum_excluded_ids"] == len(seen), "LOSS 累计排除 ID 不符"


# --------------------------------------------------------------- 突变证据
# 只 patch 调用边界，不改源码；运行上面的同一断言，只有明确捕获到拒绝才算通过。
@pytest.mark.parametrize("clock", ["h0", "unknown"])
def test_mutation_feature_all_invalid(monkeypatch, clock):
    from quant_lab.research import features
    from quant_lab.research.ast import canonical_hash

    def forged(asts, anchors, **kwargs):
        h = canonical_hash(asts[0])
        return anchors.select("episode_id", "t_dec").with_columns(
            pl.lit(999999.).alias(f"f_{h}"), pl.lit(False).alias(f"validity_{h}"))

    monkeypatch.setattr(features, "feature_snapshot", forged)
    with pytest.raises(AssertionError, match="时钟"):
        if clock == "h0":
            test_seam_g3_feature_snapshot_respects_t_dec("convenience", dt.timedelta(0))
        else:
            test_seam_g3_unknown_dependency_clock()


def test_mutation_feature_future_changes_past(monkeypatch):
    from quant_lab.research import features
    from quant_lab.research.ast import canonical_hash
    real = features.feature_snapshot

    def leaking(asts, anchors, *, bars, **kwargs):
        snap = real(asts, anchors, bars=bars, **kwargs)
        if bars["close"].max() == 999999.:
            h = canonical_hash(asts[0])
            snap = snap.with_columns((pl.col(f"f_{h}") + 1).alias(f"f_{h}"))
        return snap

    monkeypatch.setattr(features, "feature_snapshot", leaking)
    with pytest.raises(AssertionError, match="时钟特征值"):
        test_seam_g3_feature_snapshot_respects_t_dec("convenience", dt.timedelta(0))


def test_mutation_unknown_dependency_assumed_known(monkeypatch):
    from quant_lab.research import features
    real = features.feature_snapshot

    def unknown_is_close(asts, anchors, *, bars, **kwargs):
        bars = bars.with_columns(pl.col("available_at").fill_null(pl.col("close_time")))
        return real(asts, anchors, bars=bars, **kwargs)

    monkeypatch.setattr(features, "feature_snapshot", unknown_is_close)
    with pytest.raises(AssertionError, match="时钟有效性"):
        test_seam_g3_unknown_dependency_clock()


@pytest.mark.parametrize("entry", ["mark_bar_at", "last_closed_bar"])
def test_mutation_late_bar_ignores_arrival(monkeypatch, entry):
    from quant_lab.market import asof
    real = getattr(asof, entry)

    def close_only(bars, *args, **kwargs):
        return real(bars.drop("available_at", strict=False), *args, **kwargs)

    monkeypatch.setattr(asof, entry, close_only)
    with pytest.raises(AssertionError, match="G2 .*可见性"):
        test_seam_g2_late_bar_clock()


@pytest.mark.parametrize("path", ["convenience", "context"])
@pytest.mark.parametrize("latency", [-US, -MINUTE])
def test_mutation_feature_accepts_negative_latency(monkeypatch, path, latency):
    from dataclasses import replace
    from quant_lab.research import features
    real = features.feature_snapshot

    def accepts(asts, anchors, **kwargs):
        if "ctx" in kwargs:
            kwargs["ctx"] = replace(kwargs["ctx"], latency=dt.timedelta(0))
        else:
            kwargs["latency"] = dt.timedelta(0)
        return real(asts, anchors, **kwargs)

    monkeypatch.setattr(features, "feature_snapshot", accepts)
    with pytest.raises(AssertionError, match="G3 接受负 latency"):
        test_seam_negative_latency_clock(path, latency)


@pytest.mark.parametrize("entry", ["mark_bar_at", "last_closed_bar"])
def test_mutation_market_accepts_negative_latency(monkeypatch, entry):
    from quant_lab.market import asof
    real = getattr(asof, entry)

    def accepts(*args, **kwargs):
        kwargs["latency"] = dt.timedelta(0)
        return real(*args, **kwargs)

    monkeypatch.setattr(asof, entry, accepts)
    with pytest.raises(AssertionError, match="G2 接受负 latency"):
        test_seam_negative_latency_clock("convenience", -US)


def test_mutation_execution_missing_episode(monkeypatch, episodes, execution, opportunity):
    from quant_lab.market import execution as market_execution
    assert execution.height > 1
    monkeypatch.setattr(market_execution, "simulate_batch", lambda *args, **kwargs: execution.slice(1))
    reduced = _execute(episodes)
    for check in (test_seam_g2_build_request_is_sole_entrypoint, test_seam_g3_pair_arms_consumes_g2_output):
        with pytest.raises(AssertionError, match="完整机会集不相等"):
            check(reduced, opportunity)


@pytest.mark.parametrize("mid", QUARANTINED)
def test_mutation_quarantine_known_object_disappears(monkeypatch, fresh_lake, mid):
    from quant_lab.data import api
    real = api.quarantine

    def missing(*args, **kwargs):
        return real(*args, **kwargs).filter(pl.col("object_id") != f"{QUARANTINE_PEER}:{mid}")

    monkeypatch.setattr(api, "quarantine", missing)
    with pytest.raises(AssertionError, match="固定隔离对象消失"):
        test_e2e_loss_table_and_quarantine_readable(fresh_lake)


def test_mutation_mapping_known_object_disappears(monkeypatch, fresh_lake):
    real = pl.read_parquet
    target = fresh_lake["layout"].mapping(fresh_lake["trace"]["batch_id"], 1)

    def missing(path, *args, **kwargs):
        frame = real(path, *args, **kwargs)
        if Path(path) == target:
            frame = frame.filter(~pl.col("input_ref").str.starts_with(f"{QUARANTINE_PEER}:9:"))
        return frame

    monkeypatch.setattr(pl, "read_parquet", missing)
    with pytest.raises(AssertionError, match="固定 MAP 对象消失"):
        test_e2e_loss_table_and_quarantine_readable(fresh_lake)


@pytest.mark.parametrize("kind", ["counts", "batch", "stratum"])
def test_mutation_loss_fabricated(monkeypatch, fresh_lake, kind):
    from quant_lab.data import api
    real = api.loss_table

    def forged(batch):
        loss = real(batch)
        if kind == "batch":
            return loss.with_columns(pl.lit("foreign-batch").alias("batch_id"))
        if kind == "stratum":
            return loss.filter(~((pl.col("layer") == 1) & pl.col("stratum").str.contains("unknown")))
        # 保持 input_n 的算术守恒，只有逐 ID 对账才能发现状态被转移。
        return loss.with_columns((pl.col("n_quarantine") + 1).alias("n_quarantine"),
                                 (pl.col("n_ok") - 1).alias("n_ok"))

    monkeypatch.setattr(api, "loss_table", forged)
    with pytest.raises(AssertionError, match="损耗表批次|LOSS"):
        test_e2e_loss_table_and_quarantine_readable(fresh_lake)


@pytest.mark.parametrize("column", ["graph_version", "batch_id"])
def test_mutation_published_episodes_wrong_identity(monkeypatch, fresh_lake, column):
    from quant_lab.data import api
    real = api.load_episodes

    def foreign(*args, **kwargs):
        return real(*args, **kwargs).with_columns(pl.lit("foreign").alias(column))

    monkeypatch.setattr(api, "load_episodes", foreign)
    with pytest.raises(AssertionError, match="本轮.*错配"):
        test_seam_g1_fresh_build_identity(fresh_lake, api.load_episodes(GRAPH))
