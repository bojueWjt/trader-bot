"""五审（R-10 round 5）必修项回归：R5-C 失败记账、R5-G 相关向量形状与值域、R5-O 首个 replicate 特权、
R5-H worker 生成身份绑定、R5-W 弱相关破坏的总体门功效。全部复现审查报告里的工程反例。"""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest
from dataclasses import replace

from quant_lab.research import nullmodel as NM
from quant_lab.research.nullmodel import clopper_pearson, grid_pair_labels, verify_report_text
from tests.research.test_review_p1_round3 import _restamped_report


def _payload():
    text, payload, start, end = _restamped_report()
    return text, payload, start, end


def _render(text, start, end, payload):
    return text[:start] + json.dumps(payload) + text[end:]


def _move_to_invalid(r, k, *, pay_failed):
    """把 k 个 replicate 记成结构门失败。pay_failed=False 复现 R5-C 反例：只改 flags/档位，不付失败费。"""
    d = r["diagnostics"]; n = r["n_done"]
    d["n_invalid_null_model"] = k
    d["guard_fail_rate"] = k / n
    d["guard_failures_by_check"] = {c: (k if c == "icc" else 0) for c in d["shuffle_guard"]["checks"]}
    r["tiers"]["invalid"] = k
    r["tiers"]["T1"] -= k
    d["chosen_block_len"] = {"L=3": n - k}                 # 结构早退者没进流水线，不该有主 L
    if pay_failed:
        r["n_failed"] += k
        x, failed, ns = r["n_positive"], r["n_failed"], r["n_searched"]
        r["rate"] = x / (n - failed); r["ci"] = list(clopper_pearson(x, n - failed))
        w = x + failed
        r["worst_case_rate"] = w / n; r["worst_case_ci"] = list(clopper_pearson(w, n))
        r["searched_worst_rate"] = w / ns; r["searched_worst_ci"] = list(clopper_pearson(w, ns))


def test_R5C_structural_failures_must_be_charged_to_failure_count():
    """R5-C：结构失败必须真正进 n_failed。否则保留全部真实计数与区间、只搬档位，就能让本应超 7% 的最坏界假绿。"""
    text, base, start, end = _payload()

    q = copy.deepcopy(base); _move_to_invalid(q["results"][0], 50, pay_failed=False)
    with pytest.raises(ValueError, match="未全部计入失败数"):
        verify_report_text(_render(text, start, end, q))

    q = copy.deepcopy(base); _move_to_invalid(q["results"][0], 50, pay_failed=True)
    with pytest.raises(ValueError, match="FPR 未通过"):       # 正确收费后最坏界上界越过 7%——准入确实被翻转
        verify_report_text(_render(text, start, end, q))

    q = copy.deepcopy(base); _move_to_invalid(q["results"][0], 5, pay_failed=True)
    verify_report_text(_render(text, start, end, q))          # 设计误拒率带内且全部正确入账：仍应通过

    q = copy.deepcopy(base)                                   # 主 L 不该覆盖结构早退者
    r = q["results"][0]; _move_to_invalid(r, 5, pay_failed=True)
    r["diagnostics"]["chosen_block_len"] = {"L=3": r["n_done"]}
    with pytest.raises(ValueError, match="块长计数不一致"):
        verify_report_text(_render(text, start, end, q))


@pytest.mark.parametrize("damage", ["truncate", "duplicate_pair", "over_one", "under_minus_one",
                                    "bool_value", "unknown_instrument", "n_grid_shrunk", "sd_negative"])
def test_R5G_cross_pair_vector_shape_and_domain(damage):
    """R5-G：相关向量须按冻结世界核齐全集与顺序，并落在相关系数值域内；只比两边长度相等不够。"""
    text, base, start, end = _payload()
    q = copy.deepcopy(base)
    g = q["results"][0]["diagnostics"]["grid_dependence"]
    keys = ("fitted_cross", "null_mean_cross", "null_sd_cross", "pairs", "band")
    if damage == "truncate":
        for k in keys: g[k] = g[k][:1]
    elif damage == "duplicate_pair":
        for k in keys: g[k] = g[k][:2] + [g[k][1]]
    elif damage == "over_one":
        g["null_mean_cross"] = [2.0] * len(g["null_mean_cross"])
    elif damage == "under_minus_one":
        g["fitted_cross"] = [-2.0] * len(g["fitted_cross"])
    elif damage == "bool_value":
        g["null_mean_cross"] = [True] * len(g["null_mean_cross"])
    elif damage == "unknown_instrument":
        g["pairs"] = [["XXX", "YYY"]] + g["pairs"][1:]
    elif damage == "n_grid_shrunk":
        g["n_grid"] = 3
    elif damage == "sd_negative":
        g["null_sd_cross"] = [-0.1] + list(g["null_sd_cross"])[1:]
    with pytest.raises(ValueError):
        verify_report_text(_render(text, start, end, q))


def test_R5G_frozen_world_has_exactly_three_pairs():
    assert grid_pair_labels() == [[a, b] for i, a in enumerate(NM.INSTRUMENTS) for b in NM.INSTRUMENTS[i + 1:]]
    assert len(grid_pair_labels()) == 3 and len({tuple(p) for p in grid_pair_labels()}) == 3


def test_R5O_first_replicate_has_no_zero_failure_privilege():
    """R5-O：展示样本是第一个 replicate 的快照，不是「首个必须零失败」的门；但它失败必须在总量里记账。"""
    text, base, start, end = _payload()

    q = copy.deepcopy(base)                                   # 首个失败、总量零记账 → 不自洽，拒收
    d = q["results"][0]["diagnostics"]
    d["shuffle_guard"]["checks"]["icc"] = False; d["shuffle_guard"]["ok"] = False
    with pytest.raises(ValueError, match="未计入 guard_failures_by_check"):
        verify_report_text(_render(text, start, end, q))

    q = copy.deepcopy(base)                                   # 同一次合法失败恰好落在首位：总量不变，应通过
    r = q["results"][0]; _move_to_invalid(r, 1, pay_failed=True)
    r["diagnostics"]["shuffle_guard"]["checks"]["icc"] = False
    r["diagnostics"]["shuffle_guard"]["ok"] = False
    verify_report_text(_render(text, start, end, q))


def test_R5H_worker_receipt_binding():
    """R5-H：父进程首尾摘要相等不证明各 worker 见到同一份源码——worker 必须回传自己的生成身份。"""
    frozen = "a" * 64
    assert NM.check_worker_receipt({"result": "R", "digest_start": frozen, "digest_end": frozen}, frozen) == "R"
    for bad in ({"result": "R", "digest_start": "b" * 64, "digest_end": frozen},      # worker 起跑时源码已不同
                {"result": "R", "digest_start": frozen, "digest_end": "b" * 64},      # worker 运行期源码变了
                {"result": "R", "digest_start": frozen},                              # 缺回执
                {"digest_start": frozen, "digest_end": frozen},                       # 缺结果
                "not-a-payload"):
        with pytest.raises(SystemExit):
            NM.check_worker_receipt(bad, frozen)


def test_R5H_real_run_records_worker_receipts(tmp_path):
    """真实多进程：落盘的报告必须记录 worker 回执数与回执摘要，且摘要等于当前源码身份。"""
    out = tmp_path / "r.md"
    NM._main(["--out", str(out), "--n-rep", "2", "--no-ext", "--jobs", "2", "--B", "100",
              "--n-clusters", "200", "--mechanisms", "common_shock", "--power-mechanisms", ""])
    text = out.read_text(encoding="utf-8")
    meta = json.loads(text[text.rindex("```json") + 7:text.rindex("```")])["meta"]
    assert meta["worker_receipts_confirmed"] >= 1
    assert meta["worker_code_sha256"] == [NM.research_code_digest()]


def test_R5W_aggregate_gate_separates_noise_from_weak_structure_loss():
    """R5-W：总体门判的是**均值**，带宽按均值的 SE 校准——弱相关（拟合 0.14）被摧毁到 0 必须触发。"""
    n = 1000
    # 实测值：正常重采样最大偏差（nonuniform_density 第二对）与弱相关机制被摧毁后的偏差
    assert NM.aggregate_pair_ok(0.5164, 0.5260, 0.0753, n)[0] is True
    assert NM.aggregate_pair_ok(0.1393, 0.0014, 0.0994, n)[0] is False      # 旧绝对容差 0.25 会放过这一条
    assert NM._correlation_preserved(0.1393, 0.0014) is True               # 旧判据确实放过（反例成立的根因）
    assert NM.aggregate_pair_ok(0.5590, 0.0133, 0.0910, n)[0] is False      # 强相关破坏仍被抓
    # 样本不足时带宽**放宽**而不是收紧：无从区分就不制造假警报
    assert NM.aggregate_pair_ok(0.1393, 0.0014, 0.0994, 8)[0] is True
    # 缺一端（结构信息丢失）显式判失败，不当作"无结构可保持"
    assert NM.aggregate_pair_ok(0.1393, float("nan"), 0.0994, n)[0] is False
    assert NM.aggregate_pair_ok(float("nan"), float("nan"), float("nan"), n)[0] is True


def test_R5W_weak_correlation_destruction_triggers_aggregate_gate():
    """端到端：对弱相关机制做独立整块重排（连同真实 base_R 一起改），总体门必须判 GRID_DEPENDENCE_NOT_PRESERVED。"""
    w = NM.synth_world(NM.WorldConfig(mechanism="cluster_heavy_tail", seed=2, n_clusters=800))
    m = NM.fit_residual_model(w, block_len_days=3, train_end_day=180)
    fitted = NM._grid_cross_corr(m.grid[:180], 3)
    assert max(abs(x) for x in fitted) < 0.35, fitted          # 这是"弱相关"世界，旧绝对容差覆盖不到

    def draws(broken, k=60):
        out = []
        for i in range(k):
            x = NM.resample_null(w, m, np.random.default_rng(4_200_000 + i))
            if broken:
                old = x.shock_grid; g = old.copy(); nb = len(g) // 3
                r2 = np.random.default_rng(4_300_000 + i)
                for j in range(g.shape[1]):
                    g[:nb * 3, j] = g[:nb * 3, j].reshape(nb, 3)[r2.permutation(nb)].reshape(-1)
                x = replace(x, shock_grid=g, base_R=x.base_R + g[w.day, w.inst] - old[w.day, w.inst])
            out.append(NM._grid_cross_corr(x.shock_grid, 3))
        a = np.array(out, dtype=float)
        return np.nanmean(a, axis=0).tolist(), np.nanstd(a, axis=0, ddof=1).tolist(), k

    mean, sd, k = draws(False)
    assert NM.aggregate_preserved(fitted, mean, sd, k) is True, (fitted, mean)      # 正常：不误拒
    mean, sd, k = draws(True)
    assert NM.aggregate_preserved(fitted, mean, sd, k) is False, (fitted, mean)     # 破坏：必须触发
