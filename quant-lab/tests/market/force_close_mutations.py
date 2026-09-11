"""任务一可复跑突变自证：只改 tests/market 下隔离副本，逐条 RED → SHA256 还原 → GREEN。

A24/禁令是结构/语法证据；消费方、未来价哨兵均为夹具证据，不是 G3 θ 证据。
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "tests/market/force_close_mutation_results.json"
C = "src/quant_lab/market/contract.py"
G = "src/quant_lab/market/single_source.py"
K = "src/quant_lab/market/kernel_a.py"
T = "tests/market/test_force_close_net_r.py"
O = "tests/market/test_outcome_kind.py"
P = "src/quant_lab/market/policy_hashes.json"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def mutation_cases():
    accounting = T + "::test_accounting_and_read_only"
    yield "M01 omit close_fee", C, "- res.fees - close_fee + res.funding", "- res.fees + res.funding", accounting
    yield "M02 reverse sign", C, 'if side == "short":', 'if side == "long":', accounting
    yield "M03 future mark (fixture only)", T, "if bar.open_time + dt.timedelta(seconds=bar.interval_s) <= horizon_end", "if True", T + "::test_fixture_asof_and_sentinel"
    yield "M04 omit funding", C, "- close_fee + res.funding", "- close_fee", accounting
    yield "M05 omit realized", C, "net_forced = res.gross_pnl + unrealized", "net_forced = unrealized", accounting
    yield "M06 omit accumulated fees", C, "- res.fees - close_fee", "- close_fee", accounting
    yield "M07 write net_R", C, "    return ForceCloseValuation(", '    object.__setattr__(res, "net_R", Decimal(0))\n    return ForceCloseValuation(', accounting
    yield "M08 append event", C, "    return ForceCloseValuation(", "    res.canonical_events.append(res.canonical_events[0])\n    return ForceCloseValuation(", accounting
    yield "M09 change fill_status", C, "    return ForceCloseValuation(", '    object.__setattr__(res, "fill_status", "partial")\n    return ForceCloseValuation(', accounting
    yield "M10 change censor", C, "    return ForceCloseValuation(", '    object.__setattr__(res, "censor_reason", "BAR_GAP")\n    return ForceCloseValuation(', accounting
    yield "M11 full filled qty as residual", C, "residual = res.filled_qty - exited", "residual = res.filled_qty", accounting
    yield "M12 omit multiplier", C, "* residual * sign * multiplier", "* residual * sign", accounting
    yield "M13 wrong fee scenario", C, "policy.cost(cost_scenario).taker_fee * multiplier", 'policy.cost("base").taker_fee * multiplier', accounting
    yield "M14 zero instead of None", C, "    if residual == 0:\n        return None", "    if residual == 0:\n        return Decimal(0)", T + "::test_no_residual_is_none"
    yield "M15 rebuild missing gross", C, 'raise ContractError("内核未提供已实现损益 gross_pnl；禁止从事件重建")', 'object.__setattr__(res, "gross_pnl", sum((e.cash_delta or Decimal(0) for e in res.canonical_events), Decimal(0)))', T + "::test_missing_kernel_value_is_error[gross]"
    yield "M16 missing entry silently supplied", C, 'raise ContractError("余仓估值需要 entry_avg_price")', 'object.__setattr__(res, "entry_avg_price", mark)', T + "::test_missing_kernel_value_is_error[entry]"
    yield "M17 bypass fixture consumption", T, "    return c.force_close_net_R(res, **args)", '    return c.ForceCloseValuation(net_R_forced=D("19.24"), mark=bar.c, mark_at=bar.open_time, mark_source="fixture-bars", residual_qty=D(6), close_fee=D("1.32"))', T + "::test_fixture_asof_and_sentinel"
    yield "M18 disable hold censor", K, "if (mo.hold_end or (self.hold_end is not None and ts >= self.hold_end)) and self.pos != 0:", "if False:", T + "::test_right_censor_routes_remain_reachable[hold_end]"
    yield "M19 change horizon censor", K, 'if mo.horizon and self.pos != 0:\n                    self.censor_now("LABEL_RIGHT_CENSORED")', 'if mo.horizon and self.pos != 0:\n                    self.censor_now("BAR_GAP")', T + "::test_right_censor_routes_remain_reachable[horizon]"
    yield "M20 fake G3 caller registration", G, '"force_close_net_R": set(),', '"force_close_net_R": {"research/fake.py:theta"},', T + "::test_force_close_empty_registration"
    yield "M21 fake G3 count registration", G, '"force_close_net_R": {},', '"force_close_net_R": {"research/fake.py:theta": 1},', T + "::test_force_close_empty_registration"
    yield "M22 new actual caller", C, "\n__all__ = [", '\ndef unregistered_probe():\n    return force_close_net_R(res)\n\n__all__ = [', T + "::test_force_close_empty_registration"
    for case in ("extra", "bypass"):
        yield "M23 suppress registry " + case, G, "    return problems", "    return []", T + "::test_force_close_fixture_registration[" + case + "]"
    yield "M24 suppress use counts", G, "self.counts[name][where] += 1", "self.counts[name][where] = 2", T + "::test_force_close_fixture_registration[internal_bypass]"
    yield "M25 suppress inline ban", G, 'self._hit("P8_inline_force_close", node)', 'pass # injected missing P8', T + "::test_force_close_inline_ban_independent"
    yield "M26 allow inline outside owner", G, '"P8_inline_force_close": {"market/contract.py:force_close_net_R"}', '"P8_inline_force_close": {"market/contract.py:force_close_net_R", "market/consumer.py:consumer"}', T + "::test_force_close_inline_ban_independent"
    yield "M27 S30 original missing latency", C, "if self.horizon_end <= exp_t_start:", "if self.horizon_end <= (self.t_start or self.t_dec):", O + "::test_s30_window_lower_bound_uses_derived_start_not_t_dec"
    yield "M28 S30 real file write (isolated replica)", O, 'reg = tmp_path / "policy_hashes.json"', 'reg = original', O + "::test_s30_window_lower_bound_uses_derived_start_not_t_dec"


    yield "M29 inline bypass one registered use", T, "first = force_close_net_R(res)", "first = (mark - res.entry_avg_price)", T + "::test_force_close_fixture_registration[internal_bypass]"
    yield "M30 inline bypass all registered uses", T, "first = force_close_net_R(res)\\n    return force_close_net_R(res)", "first = (mark - res.entry_avg_price)\\n    return first", T + "::test_force_close_fixture_registration[bypass]"
    yield "M31 inline expression real tree gate", C, "\n__all__ = [", "\ndef forbidden_valuation():\n    return (mark - res.entry_avg_price) * qty\n\n__all__ = [", "tests/market/test_single_source.py::test_no_inline_reexpression_of_single_sources_anywhere_in_src"
    yield "M32 unclosed mark (fixture only)", T, "bar.open_time + dt.timedelta(seconds=bar.interval_s) <= horizon_end", "bar.open_time <= horizon_end", T + "::test_fixture_asof_and_sentinel"
    yield "M33 mutable valuation", C, "@dataclass(frozen=True)", "@dataclass(frozen=False)", accounting


def main():
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    originals = {name: (ROOT / name).read_bytes() for name in (C, G, K, T, O, P)}
    policy_mtime = (ROOT / P).stat().st_mtime_ns
    report = {"scope": "isolated replicas; G3 not integrated", "cases": []}
    with tempfile.TemporaryDirectory(prefix="_force_close_mutation_", dir=ROOT / "tests/market") as directory:
        mirror = Path(directory)
        market = mirror / "src/quant_lab/market"
        market.mkdir(parents=True)
        for path in (ROOT / "src/quant_lab/market").iterdir():
            if path.is_file():
                shutil.copy2(path, market / path.name)
        shutil.copy2(ROOT / "src/quant_lab/__init__.py", market.parent / "__init__.py")
        (market.parent / "data").symlink_to(ROOT / "src/quant_lab/data", target_is_directory=True)
        testdir = mirror / "tests/market"
        testdir.mkdir(parents=True)
        for path in (ROOT / "tests/market").glob("*.py"):
            shutil.copy2(path, testdir / path.name)
        (testdir / "fixtures").symlink_to(ROOT / "tests/market/fixtures", target_is_directory=True)
        (mirror / "pytest.ini").write_text("[pytest]\npythonpath = src\n")
        env = {**os.environ, "PYTHONPATH": str(mirror / "src")}

        def run(test):
            result = subprocess.run([sys.executable, "-c", "import pathlib, pytest; from quant_lab.market import contract; "
                                     "assert pathlib.Path(contract.__file__).is_relative_to(pathlib.Path.cwd()); "
                                     "raise SystemExit(pytest.main(__import__('sys').argv[1:]))",
                                     "-c", str(mirror / "pytest.ini"), test, "-q", "-p", "no:cacheprovider"],
                                    cwd=mirror, env=env, text=True, capture_output=True)
            return {"exit": result.returncode, "output": result.stdout + result.stderr}

        for label, name, old, new, test in mutation_cases():
            target = mirror / name
            raw = target.read_bytes()
            text = raw.decode()
            assert old in text, label
            row = {"case": label, "target": name, "test": test, "old": old, "new": new,
                   "sha256_before": sha(raw)}
            try:
                target.write_text(text.replace(old, new, 1))
                row["sha256_mutant"] = sha(target.read_bytes())
                row["injected"] = run(test)
            finally:
                target.write_bytes(raw)
                (mirror / P).write_bytes(originals[P])
                row["sha256_after"] = sha(target.read_bytes())
                row["SHA256_EQUAL"] = row["sha256_before"] == row["sha256_after"]
                assert row["SHA256_EQUAL"], row
            row["restored"] = run(test)
            report["cases"].append(row)
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(label, "RED", row["injected"]["exit"], "SHA256_EQUAL", row["SHA256_EQUAL"], "GREEN", row["restored"]["exit"], flush=True)
            assert row["injected"]["exit"] == 1, "A26 overlay required; stop to inspect: " + label
            assert row["restored"]["exit"] == 0, row
    report["original_sources_unchanged"] = {name: sha((ROOT / name).read_bytes()) == sha(raw) for name, raw in originals.items()}
    report["policy_mtime_ns"] = [policy_mtime, (ROOT / P).stat().st_mtime_ns]
    assert all(report["original_sources_unchanged"].values())
    assert report["policy_mtime_ns"][0] == report["policy_mtime_ns"][1]
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
