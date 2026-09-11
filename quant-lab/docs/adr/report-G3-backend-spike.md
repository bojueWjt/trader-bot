# report-G3-backend-spike：polars 自写 vs polars_ta 0.5.17 变体对拍（8a spike）

日期：2026-09-10 19:04 UTC。状态：合成数据实测；只描述，不据此声明研究优势（G-LICENSE-SEARCH / G-STAT-CLAIM 未批）。
依据：合并稿 D.1–D.3、ADR-G3 §4、契约 feature-snapshot §2。命令：`.venv-g3/bin/python -m quant_lab.research.spike --out docs/adr/report-G3-backend-spike.md`。

## 1. 环境与依赖锁

- 设备：arm64 / macOS-26.5.2-arm64-arm-64bit；Python 3.12.13；单进程，polars 默认线程。
- 版本：{"polars": "1.44.2", "polars_ta": "0.5.17", "numba": "0.67.0", "pandas": "3.0.5", "polars-ols": "0.3.5", "numpy": "2.5.3", "TA-Lib": "0.7.1"}
- polars_ta 许可：MIT License（PyPI 元数据）；其依赖 numba / pandas / polars-ols 随之进入 .venv-g3（requirements/g3.txt 钉 0.5.17）。
- 适配代码量（行）：{"polars.py": 167, "polars_ta.py": 108, "__init__.py": 98}；polars_ta 变体复用 polars 后端骨架，只替换算子核。

## 2. 契约通过率矩阵（五类检查：reference / 截断重算 / 未来免疫 / 过去缺值 / 缺 bar）

每算子：26 边界例 + 100 随机序列 × 各窗口/参数变体；数值 atol=1e-10 / rtol=1e-8，布尔完全一致。

| 算子 | polars | polars_ta | polars_ta 备注 |
|---|---|---|---|
| Add | pass | pass | wq.add 为水平求和、null 按 0 填，不等价 → 显式路由 polars 原生（NATIVE_OPS） |
| Sub | pass | pass | wq 同名核 |
| Mul | pass | pass | wq.multiply null 按 1 填，不等价 → 显式路由 polars 原生（NATIVE_OPS） |
| SafeDiv | pass | pass | wq 同名核 |
| Abs | pass | pass | wq 同名核 |
| LogPositive | pass | pass | wq 同名核 |
| Gt | pass | pass | polars 原生比较（NATIVE_OPS） |
| Lt | pass | pass | polars 原生比较（NATIVE_OPS） |
| And | pass | pass | polars 原生严格三值（NATIVE_OPS） |
| Ref | pass | pass | wq 同名核 |
| Delta | pass | pass | wq 同名核 |
| Mean | pass | pass | wq 同名核 |
| Std | pass | pass | wq.ts_std_dev 显式 ddof=1、min_samples=d |
| Min | pass | pass | wq 同名核 |
| Max | pass | pass | wq 同名核 |
| TSRank | pass | pass | wq.ts_rank = rolling_rank / 非 null 计数 |
| Corr | pass | FAIL | wq.ts_corr（rolling_corr 流式）；失败 18 条，例：Corr[rows=3]/huge: 行 2 got=0.9990234375 exp=1.0 |
| EMA | pass | unsupported | wq/ta EMA = ewm_mean 首值种子、null 不重置 → unsupported |

- polars：18/18 支持算子通过（unsupported 0）；polars_ta：16/17 支持算子通过（unsupported 1）。
- polars_ta 使用 wq 核的算子：['Abs', 'Corr', 'Delta', 'LogPositive', 'Max', 'Mean', 'Min', 'Ref', 'SafeDiv', 'Std', 'Sub', 'TSRank']；原生路由：['Add', 'And', 'Gt', 'Lt', 'Mul']；unsupported：['EMA']。
- 任一失败算子在该后端隔离（依赖它的 AST 拒收），不放宽容差、不以近似替代（ADR §3.2/§4）。**落地（review-G3-P1 S04 闭合）**：`backends.BACKEND_QUARANTINE` 源码常量登记 `(polars_ta, Corr)`，`supports('Corr')` 在新进程即为 False，`compute` / `feature_snapshot` 对依赖 AST 抛 `BackendOpQuarantined`；`contract_tests.run_contract` 任一失败即运行期登记隔离。

## 3. 冷/热耗时与峰值内存（416 万行夹具）

- 夹具：40 品种 × 104,000 根 15m 合成 bar = 4,118,333 行（1% 随机缺 bar），生成 5.2s；AST 128 个（深度 ≤4，含 rows/wallclock 窗、lag、复合）。
- cold_s = 首轮全部 AST（含表达式计划/JIT 预热），hot_s = 紧接第二轮；RSS 为进程峰值（ru_maxrss，含契约矩阵阶段）。

| 后端 | 版本 | AST 数 | cold_s（冷跑） | hot_s（热跑） | 峰值 RSS GiB | unsupported | errors |
|---|---|---:|---:|---:|---:|---:|---:|
| polars | 0.2 | 128 | 206.77 | 197.41 | 1.759 | 0 | 0 |
| polars_ta | 0.1+polars_ta-0.5.17 | 128 | 36.11 | 34.95 | 1.759 | 6 | 0 |

- 资源提案（D.3：128 AST × 15m 夹具冷跑 ≤ 20 分钟、峰值 ≤ 8 GiB）：两后端冷跑分别 206.77s / 36.11s，峰值 RSS 1.759 GiB —— 均在提案内。
- 本次 spike 总耗时 636.7s（含契约矩阵）。

## 4. 发现（同合同差异归类：合同差别 vs 实现）

1. **null 语义**：polars_ta 的 `add/multiply` 是横向聚合语义（null 填 0/1），与合同「任一无效即无效」是合同差别，不是 bug；变体显式改走原生表达式并登记，不静默 fallback。
2. **EMA 初始化**：polars_ta EMA = `ewm_mean(span, adjust=False, min_samples=span)`，首值种子且 null 不重置 warm-up；合同要求 SMA(n) 种子 + 无效重置。无同合同实现 → unsupported。
3. **数值稳定性**：流式 rolling 方差/相关在 1e12 量级出现抵消误差（polars 自写 Std/Corr 已改显式两遍去均值后过 rtol=1e-8；polars_ta `ts_corr` 走 `rolling_corr` 流式核，见矩阵结果）。禁止临时放宽容差掩盖高量级误差。
4. **ddof / min_samples**：polars_ta 默认 ddof=0、MIN_SAMPLES=None，必须显式传 ddof=1 与 min_samples=d 才与 strict_required 等价。
5. **行窗即槽位窗**：两后端都在计划槽位网格上求值（ADR §3），wallclock W = ceil(W/interval) 槽位；polars_ta 天然只有行窗，网格化后无需额外时间窗适配。

6. **耗时差异**：polars 自写后端冷跑 206.77s vs polars_ta 36.11s（同 128 AST、同夹具，但 polars_ta 跳过 6 个含 EMA 的 AST，**不是同等工作量的速度比**；峰值 RSS 为同一进程累积水位，不能归因单个后端——见 A07）。差异来自自写后端的窗口算子用 `DataFrame.rolling().agg()` 按块聚合（为了显式两遍去均值与 strict 门），polars_ta 用原生 `rolling_*` 流式核；这是实现层差异而非合同差异，优化方向是把 Mean/Min/Max/TSRank 改回原生 rolling 核并保留显式门控。此处只记录，不据此选后端。

## 5. 退出成本与维护面

- polars_ta 变体只在算子核层接入，`get_backend("polars_ta")` 切换；移除只需删 backends/polars_ta.py 与 requirements 一行，AST/契约/评估层不变。
- 后端不由收益选择；后续任何后端切换开新 run 并记账（ADR §4）。

## 6. 结论（描述）

- polars 自写后端：18/18 支持算子通过（unsupported 0），作为 P1 参考实现。
- polars_ta 变体：16/17 支持算子通过（unsupported 1）；未通过/unsupported 的算子在该后端隔离。变体保留为对拍与性能参照，不作候选生成器，不预认覆盖率。
- 以上均为合成数据实测；性能数字随设备与线程变化，不构成上线或研究优势声明。

## 附：原始结果 JSON

```json
{
 "matrix": {
  "polars": {
   "Add": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.5
   },
   "Sub": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.4
   },
   "Mul": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.3
   },
   "SafeDiv": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.4
   },
   "Abs": {
    "status": "pass",
    "checks": {
     "check_reference": 127,
     "check_truncation": 127,
     "check_future_immunity": 127,
     "check_past_null": 127,
     "check_gap": 127
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 1.2
   },
   "LogPositive": {
    "status": "pass",
    "checks": {
     "check_reference": 127,
     "check_truncation": 127,
     "check_future_immunity": 127,
     "check_past_null": 127,
     "check_gap": 127
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 1.3
   },
   "Gt": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.2
   },
   "Lt": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.1
   },
   "And": {
    "status": "pass",
    "checks": {
     "check_reference": 127,
     "check_truncation": 127,
     "check_future_immunity": 127,
     "check_past_null": 127,
     "check_gap": 127
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 1.2
   },
   "Ref": {
    "status": "pass",
    "checks": {
     "check_reference": 381,
     "check_truncation": 381,
     "check_future_immunity": 381,
     "check_past_null": 381,
     "check_gap": 381
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 4.3
   },
   "Delta": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 3.6
   },
   "Mean": {
    "status": "pass",
    "checks": {
     "check_reference": 635,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 635,
     "check_gap": 635
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 8.8
   },
   "Std": {
    "status": "pass",
    "checks": {
     "check_reference": 635,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 635,
     "check_gap": 635
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 9.7
   },
   "Min": {
    "status": "pass",
    "checks": {
     "check_reference": 635,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 635,
     "check_gap": 635
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 8.9
   },
   "Max": {
    "status": "pass",
    "checks": {
     "check_reference": 635,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 635,
     "check_gap": 635
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 8.9
   },
   "TSRank": {
    "status": "pass",
    "checks": {
     "check_reference": 635,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 635,
     "check_gap": 635
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 10.1
   },
   "Corr": {
    "status": "pass",
    "checks": {
     "check_reference": 635,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 635,
     "check_gap": 635
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 13.2
   },
   "EMA": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.8
   }
  },
  "polars_ta": {
   "Add": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.7
   },
   "Sub": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.5
   },
   "Mul": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.4
   },
   "SafeDiv": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.5
   },
   "Abs": {
    "status": "pass",
    "checks": {
     "check_reference": 127,
     "check_truncation": 127,
     "check_future_immunity": 127,
     "check_past_null": 127,
     "check_gap": 127
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 1.3
   },
   "LogPositive": {
    "status": "pass",
    "checks": {
     "check_reference": 127,
     "check_truncation": 127,
     "check_future_immunity": 127,
     "check_past_null": 127,
     "check_gap": 127
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 1.2
   },
   "Gt": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.1
   },
   "Lt": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.2
   },
   "And": {
    "status": "pass",
    "checks": {
     "check_reference": 127,
     "check_truncation": 127,
     "check_future_immunity": 127,
     "check_past_null": 127,
     "check_gap": 127
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 1.5
   },
   "Ref": {
    "status": "pass",
    "checks": {
     "check_reference": 381,
     "check_truncation": 381,
     "check_future_immunity": 381,
     "check_past_null": 381,
     "check_gap": 381
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 4.3
   },
   "Delta": {
    "status": "pass",
    "checks": {
     "check_reference": 254,
     "check_truncation": 254,
     "check_future_immunity": 254,
     "check_past_null": 254,
     "check_gap": 254
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 2.9
   },
   "Mean": {
    "status": "pass",
    "checks": {
     "check_reference": 635,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 635,
     "check_gap": 635
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 6.8
   },
   "Std": {
    "status": "pass",
    "checks": {
     "check_reference": 635,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 635,
     "check_gap": 635
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 6.7
   },
   "Min": {
    "status": "pass",
    "checks": {
     "check_reference": 635,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 635,
     "check_gap": 635
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 6.7
   },
   "Max": {
    "status": "pass",
    "checks": {
     "check_reference": 635,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 635,
     "check_gap": 635
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 6.8
   },
   "TSRank": {
    "status": "pass",
    "checks": {
     "check_reference": 635,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 635,
     "check_gap": 635
    },
    "n_fail": 0,
    "samples": [],
    "seconds": 7.2
   },
   "Corr": {
    "status": "FAIL",
    "checks": {
     "check_reference": 632,
     "check_truncation": 635,
     "check_future_immunity": 635,
     "check_past_null": 633,
     "check_gap": 634
    },
    "n_fail": 18,
    "samples": [
     "Corr[rows=3]/huge: 行 2 got=0.9990234375 exp=1.0",
     "Corr[rows=3]/huge: 行 3 got=0.9990234375 exp=1.0"
    ],
    "seconds": 8.6
   },
   "EMA": {
    "status": "unsupported"
   }
  }
 },
 "timings": [
  {
   "backend": "polars",
   "version": "0.2",
   "n_ast": 128,
   "unsupported": 0,
   "errors": 0,
   "cold_s": 206.77,
   "rss_gib_after_cold_s": 1.759,
   "hot_s": 197.41,
   "rss_gib_after_hot_s": 1.759
  },
  {
   "backend": "polars_ta",
   "version": "0.1+polars_ta-0.5.17",
   "n_ast": 128,
   "unsupported": 6,
   "errors": 0,
   "cold_s": 36.11,
   "rss_gib_after_cold_s": 1.759,
   "hot_s": 34.95,
   "rss_gib_after_hot_s": 1.759
  }
 ],
 "deps": {
  "polars": "1.44.2",
  "polars_ta": "0.5.17",
  "numba": "0.67.0",
  "pandas": "3.0.5",
  "polars-ols": "0.3.5",
  "numpy": "2.5.3",
  "TA-Lib": "0.7.1"
 }
}
```
