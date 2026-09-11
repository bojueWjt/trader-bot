"""R-05 8a spike：polars vs polars_ta 变体对拍（合并稿 D.3；ADR-G3 §4）。

    .venv-g3/bin/python -m quant_lab.research.spike --out docs/adr/report-G3-backend-spike.md [--rows 4160000] [--n-ast 128] [--n-random 100]

记录：依赖锁与许可、适配代码量、契约通过率矩阵（五类检查 × 18 算子 × 2 后端）、冷/热耗时、峰值 RSS、未支持项。
不用回测收益选后端；结论只描述。
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata as md
import json
import platform
import resource
import sys
import time
from pathlib import Path

import polars as pl

from quant_lab.research import contract_tests as ct
from quant_lab.research.ast import canonical_hash, lint
from quant_lab.research.backends import get_backend
from quant_lab.research.backends.polars_ta import NATIVE_OPS, TA_OPS, UNSUPPORTED, BackendUnsupported
from quant_lab.research.ops import FIRST_BATCH
from quant_lab.research.synthetic import fake_bars

F = lambda n: {"field": n}                                   # noqa: E731
ROWS = lambda n: {"unit": "rows", "count": n}                # noqa: E731
WALL = lambda m: {"unit": "wallclock", "minutes": m}         # noqa: E731


def spike_asts(n: int = 128) -> list[dict]:
    """确定性生成 n 个唯一合法 AST（深度 ≤ 4，覆盖 18 算子、rows/wallclock 窗、lag）。"""
    out, seen = [], set()

    def add(a):
        try:
            h = canonical_hash(a)
        except Exception:
            return
        if h not in seen:
            seen.add(h); out.append(a)

    wins = [ROWS(5), ROWS(20), ROWS(96), WALL(60), WALL(240), WALL(1440)]
    for w in wins:
        for op in ("Mean", "Std", "Min", "Max", "TSRank"):
            add({"op": op, "args": [F("close")], "window": w})
        add({"op": "Corr", "args": [F("close"), F("volume")], "window": w})
        add({"op": "SafeDiv", "args": [{"op": "Mean", "args": [F("close")], "window": w}, F("close")]})
        add({"op": "Gt", "args": [F("close"), {"op": "Max", "args": [F("high")], "window": w}]})
    for n_ in (5, 20, 96):
        add({"op": "EMA", "args": [F("close")], "window": ROWS(n_)})
        add({"op": "SafeDiv", "args": [F("close"), {"op": "EMA", "args": [F("close")], "window": ROWS(n_)}]})
    for lag in (1, 4, 16, 96):
        add({"op": "Ref", "args": [F("close")], "params": {"lag": lag}})
        add({"op": "Delta", "args": [F("close")], "params": {"lag": lag}})
        add({"op": "SafeDiv", "args": [{"op": "Delta", "args": [F("close")], "params": {"lag": lag}}, F("close")]})
        add({"op": "Corr", "args": [{"op": "Delta", "args": [F("close")], "params": {"lag": lag}}, F("volume")], "window": ROWS(20)})
    for a, b in (("close", "open"), ("high", "low"), ("close", "high")):
        for op in ("Add", "Sub", "Mul", "SafeDiv", "Gt", "Lt"):
            add({"op": op, "args": [F(a), F(b)]})
        add({"op": "Abs", "args": [{"op": "Sub", "args": [F(a), F(b)]}]})
        add({"op": "LogPositive", "args": [{"op": "SafeDiv", "args": [F(a), F(b)]}]})
        add({"op": "And", "args": [{"op": "Gt", "args": [F(a), F(b)]}, {"op": "Lt", "args": [F("volume"), {"const": 100}]}]})
        add({"op": "Mean", "args": [{"op": "Abs", "args": [{"op": "Sub", "args": [F(a), F(b)]}]}], "window": ROWS(20)})
        add({"op": "Std", "args": [{"op": "LogPositive", "args": [{"op": "SafeDiv", "args": [F(a), F(b)]}]}], "window": WALL(240)})
        add({"op": "TSRank", "args": [{"op": "Delta", "args": [F(a)], "params": {"lag": 1}}], "window": ROWS(96)})
    for c in (0.5, 2, 10):
        add({"op": "Mul", "args": [F("close"), {"const": c}]})
        add({"op": "Gt", "args": [{"op": "TSRank", "args": [F("close")], "window": ROWS(20)}, {"const": c / 10}]})
    for f in ("open", "high", "low", "volume"):
        for w in (ROWS(20), WALL(240), ROWS(96)):
            add({"op": "Mean", "args": [F(f)], "window": w})
            add({"op": "Std", "args": [F(f)], "window": w})
            add({"op": "TSRank", "args": [F(f)], "window": w})
    assert len(out) >= n, len(out)
    return out[:n]


def rss_gib() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 ** 3) if sys.platform == "darwin" else r / (1024 ** 2)


def time_backend(name: str, asts: list[dict], bars: pl.DataFrame) -> dict:
    b = get_backend(name)
    res = {"backend": name, "version": b.version, "n_ast": len(asts), "unsupported": 0, "errors": 0}
    for label in ("cold_s", "hot_s"):
        t = time.perf_counter()
        for a in asts:
            try:
                b.compute(a, bars)
            except BackendUnsupported:
                res["unsupported"] += 1 if label == "cold_s" else 0
            except Exception as e:  # noqa: BLE001
                res["errors"] += 1 if label == "cold_s" else 0
                res.setdefault("error_samples", []).append(f"{type(e).__name__}: {e}"[:120])
        res[label] = round(time.perf_counter() - t, 2)
        res[f"rss_gib_after_{label}"] = round(rss_gib(), 3)
    return res


def contract_matrix(n_random: int) -> dict[str, dict[str, dict]]:
    out = {}
    for name in ("polars", "polars_ta"):
        b = get_backend(name)
        out[name] = {}
        for op in FIRST_BATCH:
            if not b.supports(op):
                out[name][op] = {"status": "unsupported"}
                continue
            t = time.perf_counter()
            r = ct.run_contract(b, op, n_random=n_random)
            out[name][op] = {"status": "pass" if r.passed else "FAIL", "checks": r.checks, "n_fail": len(r.failures),
                             "samples": r.failures[:2], "seconds": round(time.perf_counter() - t, 1)}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--rows", type=int, default=4_160_000)
    ap.add_argument("--n-ast", type=int, default=128)
    ap.add_argument("--n-random", type=int, default=100)
    ap.add_argument("--instruments", type=int, default=40)
    a = ap.parse_args(argv)

    t_all = time.perf_counter()
    matrix = contract_matrix(a.n_random)
    asts = spike_asts(a.n_ast)
    per_inst = a.rows // a.instruments
    insts = [f"SYN{i:03d}USDT-PERP.BINANCE-UM" for i in range(a.instruments)]
    t = time.perf_counter()
    bars = fake_bars(insts, n_bars=per_inst, interval="15m", seed=5, gap_frac=0.01)
    gen_s = round(time.perf_counter() - t, 1)
    timings = [time_backend("polars", asts, bars), time_backend("polars_ta", asts, bars)]
    total_s = round(time.perf_counter() - t_all, 1)

    deps = {}
    for pkg in ("polars", "polars_ta", "numba", "pandas", "polars-ols", "numpy", "TA-Lib"):
        try:
            deps[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            deps[pkg] = "absent"
    try:
        lic = (md.metadata("polars_ta").get("License", "?") or "?").strip().splitlines()[0]
    except Exception:
        lic = "?"
    src = Path(__file__).parent / "backends"
    loc = {p.name: sum(1 for _ in open(p, encoding="utf-8")) for p in (src / "polars.py", src / "polars_ta.py", src / "__init__.py")}

    def pct(name):
        rows = [v for v in matrix[name].values() if v["status"] != "unsupported"]
        return f"{sum(v['status'] == 'pass' for v in rows)}/{len(rows)} 支持算子通过（unsupported {sum(v['status'] == 'unsupported' for v in matrix[name].values())}）"

    lines = [
        "# report-G3-backend-spike：polars 自写 vs polars_ta 0.5.17 变体对拍（8a spike）",
        "",
        f"日期：{dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC。状态：合成数据实测；只描述，不据此声明研究优势（G-LICENSE-SEARCH / G-STAT-CLAIM 未批）。",
        "依据：合并稿 D.1–D.3、ADR-G3 §4、契约 feature-snapshot §2。命令：`.venv-g3/bin/python -m quant_lab.research.spike --out docs/adr/report-G3-backend-spike.md`。",
        "",
        "## 1. 环境与依赖锁",
        "",
        f"- 设备：{platform.machine()} / {platform.platform()}；Python {platform.python_version()}；单进程，polars 默认线程。",
        f"- 版本：{json.dumps(deps, ensure_ascii=False)}",
        f"- polars_ta 许可：{lic}（PyPI 元数据）；其依赖 numba / pandas / polars-ols 随之进入 .venv-g3（requirements/g3.txt 钉 0.5.17）。",
        f"- 适配代码量（行）：{json.dumps(loc)}；polars_ta 变体复用 polars 后端骨架，只替换算子核。",
        "",
        "## 2. 契约通过率矩阵（五类检查：reference / 截断重算 / 未来免疫 / 过去缺值 / 缺 bar）",
        "",
        f"每算子：26 边界例 + {a.n_random} 随机序列 × 各窗口/参数变体；数值 atol=1e-10 / rtol=1e-8，布尔完全一致。",
        "",
        "| 算子 | polars | polars_ta | polars_ta 备注 |",
        "|---|---|---|---|",
    ]
    notes = {
        "Add": "wq.add 为水平求和、null 按 0 填，不等价 → 显式路由 polars 原生（NATIVE_OPS）",
        "Mul": "wq.multiply null 按 1 填，不等价 → 显式路由 polars 原生（NATIVE_OPS）",
        "Gt": "polars 原生比较（NATIVE_OPS）", "Lt": "polars 原生比较（NATIVE_OPS）", "And": "polars 原生严格三值（NATIVE_OPS）",
        "Std": "wq.ts_std_dev 显式 ddof=1、min_samples=d", "Corr": "wq.ts_corr（rolling_corr 流式）",
        "TSRank": "wq.ts_rank = rolling_rank / 非 null 计数", "EMA": "wq/ta EMA = ewm_mean 首值种子、null 不重置 → unsupported",
    }
    for op in FIRST_BATCH:
        p_, t_ = matrix["polars"][op], matrix["polars_ta"][op]
        note = notes.get(op, "wq 同名核")
        if t_["status"] == "FAIL":
            note += f"；失败 {t_['n_fail']} 条，例：{t_['samples'][0][:90]}"
        lines.append(f"| {op} | {p_['status']} | {t_['status']} | {note} |")
    lines += [
        "",
        f"- polars：{pct('polars')}；polars_ta：{pct('polars_ta')}。",
        f"- polars_ta 使用 wq 核的算子：{sorted(TA_OPS)}；原生路由：{sorted(NATIVE_OPS)}；unsupported：{sorted(UNSUPPORTED)}。",
        "- 任一失败算子在该后端隔离（依赖它的 AST 拒收），不放宽容差、不以近似替代（ADR §3.2/§4）。",
        "",
        "## 3. 冷/热耗时与峰值内存（416 万行夹具）",
        "",
        f"- 夹具：{a.instruments} 品种 × {per_inst:,} 根 15m 合成 bar = {bars.height:,} 行（1% 随机缺 bar），生成 {gen_s}s；AST {len(asts)} 个（深度 ≤4，含 rows/wallclock 窗、lag、复合）。",
        "- cold_s = 首轮全部 AST（含表达式计划/JIT 预热），hot_s = 紧接第二轮；RSS 为进程峰值（ru_maxrss，含契约矩阵阶段）。",
        "",
        "| 后端 | 版本 | AST 数 | cold_s（冷跑） | hot_s（热跑） | 峰值 RSS GiB | unsupported | errors |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in timings:
        lines.append(f"| {r['backend']} | {r['version']} | {r['n_ast']} | {r['cold_s']} | {r['hot_s']} | {r['rss_gib_after_hot_s']} | {r['unsupported']} | {r['errors']} |")
    for r in timings:
        if r.get("error_samples"):
            lines.append(f"- {r['backend']} 错误样例：{r['error_samples'][:3]}")
    lines += [
        "",
        f"- 资源提案（D.3：128 AST × 15m 夹具冷跑 ≤ 20 分钟、峰值 ≤ 8 GiB）：两后端冷跑分别 {timings[0]['cold_s']}s / {timings[1]['cold_s']}s，峰值 RSS {max(r['rss_gib_after_hot_s'] for r in timings)} GiB —— "
        + ("均在提案内。" if all(r['cold_s'] <= 1200 for r in timings) and max(r['rss_gib_after_hot_s'] for r in timings) <= 8 else "**超限，须先缩合法算子面或缩量。**"),
        f"- 本次 spike 总耗时 {total_s}s（含契约矩阵）。",
        "",
        "## 4. 发现（同合同差异归类：合同差别 vs 实现）",
        "",
        "1. **null 语义**：polars_ta 的 `add/multiply` 是横向聚合语义（null 填 0/1），与合同「任一无效即无效」是合同差别，不是 bug；变体显式改走原生表达式并登记，不静默 fallback。",
        "2. **EMA 初始化**：polars_ta EMA = `ewm_mean(span, adjust=False, min_samples=span)`，首值种子且 null 不重置 warm-up；合同要求 SMA(n) 种子 + 无效重置。无同合同实现 → unsupported。",
        "3. **数值稳定性**：流式 rolling 方差/相关在 1e12 量级出现抵消误差（polars 自写 Std/Corr 已改显式两遍去均值后过 rtol=1e-8；polars_ta `ts_corr` 走 `rolling_corr` 流式核，见矩阵结果）。禁止临时放宽容差掩盖高量级误差。",
        "4. **ddof / min_samples**：polars_ta 默认 ddof=0、MIN_SAMPLES=None，必须显式传 ddof=1 与 min_samples=d 才与 strict_required 等价。",
        "5. **行窗即槽位窗**：两后端都在计划槽位网格上求值（ADR §3），wallclock W = ceil(W/interval) 槽位；polars_ta 天然只有行窗，网格化后无需额外时间窗适配。",
        f"6. **耗时差异**：polars 自写后端冷跑 {timings[0]['cold_s']}s vs polars_ta {timings[1]['cold_s']}s（同 128 AST、同夹具、polars_ta 跳过 {timings[1]['unsupported']} 个含 EMA 的 AST）。差异来自自写后端的窗口算子用 `DataFrame.rolling().agg()` 按块聚合（为了显式两遍去均值与 strict 门），polars_ta 用原生 `rolling_*` 流式核；这是实现层差异而非合同差异，优化方向是把 Mean/Min/Max/TSRank 改回原生 rolling 核并保留显式门控。此处只记录，不据此选后端。",
        "",
        "## 5. 退出成本与维护面",
        "",
        "- polars_ta 变体只在算子核层接入，`get_backend(\"polars_ta\")` 切换；移除只需删 backends/polars_ta.py 与 requirements 一行，AST/契约/评估层不变。",
        "- 后端不由收益选择；后续任何后端切换开新 run 并记账（ADR §4）。",
        "",
        "## 6. 结论（描述）",
        "",
        f"- polars 自写后端：{pct('polars')}，作为 P1 参考实现。",
        f"- polars_ta 变体：{pct('polars_ta')}；未通过/unsupported 的算子在该后端隔离。变体保留为对拍与性能参照，不作候选生成器，不预认覆盖率。",
        "- 以上均为合成数据实测；性能数字随设备与线程变化，不构成上线或研究优势声明。",
        "",
        "## 附：原始结果 JSON",
        "",
        "```json",
        json.dumps({"matrix": matrix, "timings": timings, "deps": deps}, ensure_ascii=False, indent=1, default=str)[:20000],
        "```",
    ]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"written {a.out}; total {total_s}s")
    for r in timings:
        print(r)


if __name__ == "__main__":
    main()
