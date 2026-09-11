# report-G3-null-model：空模型 FPR / 功效验收（合成 T1 规模，synthetic_validation）

日期：2026-09-11 04:55 UTC。状态：合成数据实测；**claim_status = descriptive_only**（G-STAT-CLAIM pending），本报告不构成任何研究优势声明，也不替代真实数据前的空模型验收。
依据：合并稿 D.5、ADR-G3 §10、Claude 验收 R03（按档分级：T1/T2 各机制 1000 次；T3 200 次预注册更宽精确区间）。
命令：`python -m quant_lab.research.nullmodel --out docs/adr/report-G3-null-model.md --n-rep 1000 --jobs 5`

## 1. 设定

- 合成世界：WorldConfig(n_clusters=1600, span_days=360, n_candidates=12, pi=0.3, censor_frac=0.05)；候选池：36 个（12 独立特征 × 规则 {gt_q30, lt_q30, gt_q70}（T1 cap=12））；流水线：PipelineConfig(B=2000, block_len_days=auto(1/3/7 训练诊断))。
- 空模型：训练窗（前 50% 天）拟合联合残差 → 整 L 日块（全品种联动）重采样格点冲击 + 整簇重采样特异残差 → 固定 episode 布局暴露映射；因果特征独立随机流重生成；**禁止逐 episode 洗牌**（结构断言 + 诊断见 §4）。
- 每次 replicate 完整流程：walk-forward 折 → 分档/预算 → search（阈值只在 search 子窗拟合）→ selection（共同块 max-t 一次，B=2000，α=0.05）→ 外折每机会一次预测 → final 全时间共同块 θ/LB（主 L=auto(1/3/7 训练诊断)：由首折训练窗残差块均值 lag-1 自相关诊断在 1/3/7 中预注册，不看候选结果；1/3/7 日敏感性另报）。
- FPR 事件：final LB > 0（合成声明函数，输出 synthetic_validation）；失败/insufficient 单列并给最坏界。
- 验收阈值：FPR 双侧 95% Clopper–Pearson 上界 ≤ 7%；功效（δ=0.2 R/种子，π=0.3）下界 ≥ 80%。
- 环境：arm64 / Python 3.12.13；seed 清单：每机制 seed0×100000+i（seed0 见表）。

## 2. FPR（每种预注册空机制）

| 机制 | 计划 n | 完成 | 失败/insufficient | 阳性 x | FPR 点估计 | FPR 95% CI（区间） | 最坏界（失败计阳性）FPR / CI | 有搜索 replicate（非 T0）n / 最坏界 FPR / CI | 阳性数按块长 L=1/3/7（敏感性，不选） | 档分布 | 耗时 s | 判定 |
|---|---:|---:|---:|---:|---:|---|---|---|---|---|---:|---|
| common_shock | 1000 | 1000 | 11 | 2 | 0.20% | [0.02%, 0.73%] | 1.30% / [0.69%, 2.21%] | 846 / 1.54% / [0.82%, 2.61%] | 3 / 3 / 3（主 L 分布 {"L=7": 320, "L=1": 279, "L=3": 391}） | {"T0": 154, "T1": 836, "invalid": 10} | 1329.0 | **pass** |
| cluster_heavy_tail | 1000 | 1000 | 10 | 6 | 0.61% | [0.22%, 1.31%] | 1.60% / [0.92%, 2.59%] | 975 / 1.64% / [0.94%, 2.65%] | 7 / 6 / 6（主 L 分布 {"L=3": 502, "L=7": 448, "L=1": 43}） | {"T1": 968, "T0": 25, "invalid": 7} | 1362.0 | **invalid_null_model** |
| nonuniform_density | 1000 | 1000 | 13 | 8 | 0.81% | [0.35%, 1.59%] | 2.10% / [1.30%, 3.19%] | 726 / 2.89% / [1.80%, 4.39%] | 9 / 6 / 8（主 L 分布 {"L=1": 784, "L=3": 119, "L=7": 95}） | {"T1": 724, "T0": 274, "invalid": 2} | 1460.0 | **invalid_null_model** |
| circular_shift | 1000 | 1000 | 4 | 3 | 0.30% | [0.06%, 0.88%] | 0.70% / [0.28%, 1.44%] | 993 / 0.70% / [0.28%, 1.45%] | 3 / 4 / 5（主 L 分布 {"L=1": 976, "L=7": 14, "L=3": 6}） | {"T1": 989, "T0": 7, "invalid": 4} | 1417.4 | **pass** |

- 最坏机制 FPR CI 上界：3.19%（阈值 7%）。所有机制均须过门，不混池稀释。
- T0 replicate（基线 DEFF 降档 → cap=0 无搜索）必然 no_claim，不作 FPR 证据；验收以「有搜索 replicate」条件最坏界为准，并要求其占多数且 ≥ 500 次。

## 3. 功效（注入 δ）

| 机制 | 计划 n | 完成 | 失败 | 检出 x | 功效点估计 | 功效 95% CI | 最坏界（失败计未检出）功效 / CI | 规则找回率 | base_R sd（噪声） | 耗时 s | 判定 |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---|
| common_shock | 1000 | 1000 | 36 | 327 | 33.92% | [30.93%, 37.01%] | 32.70% / [29.80%, 35.71%]（有搜索 769：42.52% / [39.00%, 46.11%]） | 42.4% | 1.77 | 1331.4 | **fail** |

- 功效未达标时按 D.5「未达标限制声明或扩新样本」处理：本档（T1，约 1600 簇 / 4013 机会，噪声 sd≈1.77R）对 δ=0.2R 的全流程功效见上表；§3.1 给出效应/噪声/样本规模的适用范围。流程的功效瓶颈在 selection（内层 max-t 只见训练窗一半样本）。

## 3.2 限制声明（用户裁定出口 A，2026-09-11；规范性，随本报告发布）

> 在 T1 档合成规模（约 1600 经济簇 / 4000 机会）下，本协议对每种子 0.2R 的真实过滤增益的全流程检出功效约 33.9%（条件于实际进入搜索的 replicate 为 42.5%，95% 最坏界下界约 29.8%）。因此：
> 1. 本协议在该规模下**不能**把「未检出」解释为「无增益」；未检出只描述为「在该功效下未检出」。
> 2. 任何 P2 真实数据研究若样本规模/噪声与该档相当，只能对更大量级的效应或更大样本作检出声明；本档效应量需预注册扩样本后另验。
> 3. 上述数字均为**合成**世界预注册假设下的结果，**不是真实**频道数据的功效；真实功效须在 G1/G2 真实接缝就绪后按实测 K/DEFF 重估。
> 4. FPR 结论不受功效不足影响：假阳性控制按上表逐机制判定。

GOAL-3 §7 的 P1 DoD「功效 ≥ 80%」按用户裁定改为「功效报告 + 本限制声明」，原条件未达标的记录保留于 §3。

### 3.1 功效敏感性（效应 / 噪声 / 样本规模；每格 n=200，非验收，只描述阈值适用范围）

| 设定 | 机制 | n | 检出 | 功效 | 95% CI | 规则找回率（植入候选被选中 ≥1 折） | base_R sd | 簇数 | 档分布 |
|---|---|---:|---:|---:|---|---:|---:|---:|---|
| δ=0.2 noise×1.0 clusters=1600 | common_shock | 200 | 96 | 50.3% | [42.95%, 57.56%] | 56.5% | 1.75 | 1600 | {"T1": 173, "T0": 26, "invalid": 1} |
| δ=0.2 noise×1.0 clusters=2400 | common_shock | 200 | 146 | 73.7% | [67.03%, 79.72%] | 82.0% | 1.75 | 2400 | {"T1": 190, "T0": 10} |
| δ=0.2 noise×0.6 clusters=1600 | common_shock | 200 | 112 | 60.9% | [53.42%, 67.97%] | 57.5% | 1.05 | 1600 | {"T0": 61, "T1": 131, "invalid": 8} |
| δ=0.2 noise×0.6 clusters=2400 | common_shock | 200 | 168 | 85.3% | [79.55%, 89.91%] | 83.0% | 1.05 | 2400 | {"T1": 172, "T0": 27, "invalid": 1} |
| δ=0.4 noise×1.0 clusters=1600 | common_shock | 200 | 90 | 50.3% | [42.72%, 57.82%] | 44.5% | 1.75 | 1600 | {"T0": 84, "T1": 102, "invalid": 14} |
| δ=0.4 noise×1.0 clusters=2400 | common_shock | 200 | 139 | 70.9% | [64.02%, 77.17%] | 69.0% | 1.75 | 2400 | {"T1": 143, "T0": 54, "invalid": 3} |
| δ=0.4 noise×0.6 clusters=1600 | common_shock | 200 | 18 | 18.8% | [11.51%, 28.00%] | 7.5% | 1.05 | 1600 | {"T0": 77, "invalid": 104, "T1": 19} |
| δ=0.4 noise×0.6 clusters=2400 | common_shock | 200 | 40 | 34.8% | [26.14%, 44.23%] | 19.5% | 1.05 | 2400 | {"T1": 40, "invalid": 85, "T0": 75} |

## 4. 空模型诊断（块内相关是否保留）

| 机制 | 训练窗天数 / 观测 | 块均值 lag-1 自相关 | 跨品种格点相关 | 块 ICC orig / null / 逐 episode 洗牌 | 通过 |
|---|---|---:|---:|---|---|
| common_shock | 180 / 1859（未成熟排除 24） | 0.055 | 0.491 | 0.1481 / 0.2307 / 0.0038 | True（逐 replicate 诊断失败 10 次） |
| cluster_heavy_tail | 180 / 1909（未成熟排除 8） | 0.021 | 0.119 | 0.0721 / 0.1637 / -0.0026 | True（逐 replicate 诊断失败 7 次） |
| nonuniform_density | 180 / 2096（未成熟排除 27） | -0.020 | 0.446 | 0.1754 / 0.1808 / -0.0019 | True（逐 replicate 诊断失败 2 次） |
| circular_shift | 180 / 1812（未成熟排除 11） | 0.174 | 0.549 | 0.1924 / 0.2041 / -0.0016 | True（逐 replicate 诊断失败 4 次） |

每个 replicate 都跑块 ICC 诊断；任一失败计入失败最坏界并使该机制 verdict=invalid_null_model（不得 pass）；全部落 T0 的机制标 not_run_T0（cap=0 无搜索，FPR 平凡为 0，不作 T1 验收替身）。

## 5. 总判定（synthetic_validation）

- FPR：common_shock=pass、cluster_heavy_tail=invalid_null_model、nonuniform_density=invalid_null_model、circular_shift=pass；扩展：无。
- 功效（δ=0.2）：common_shock=fail。
- 全部结果只描述；任何档位的真实数据声明仍需 G-STAT-CLAIM。

## 6. 资源与限制

- 总耗时 9471s；单 replicate 均值 1.435s；峰值 RSS 见 report.json。
- 限制：合成世界的相关结构是预注册假设，不等于真实频道数据；T3 档（200 次）未运行；块长敏感性只报告不选择；max-t 是依赖假设下近似，不是有限样本保证。
- 任何真实数据的 θ 声明须另行通过 G-STAT-CLAIM、最终 V 窗口与 latency=1s 敏感性；本报告结果只描述。

## 附：原始结果 JSON

```json
{
 "meta": {
  "command": "python -m quant_lab.research.nullmodel --out docs/adr/report-G3-null-model.md --n-rep 1000 --jobs 5",
  "world": "WorldConfig(n_clusters=1600, span_days=360, n_candidates=12, pi=0.3, censor_frac=0.05)",
  "world_summary": "约 1600 经济簇 / 4000 机会",
  "n_candidates": 36,
  "candidates": "12 独立特征 × 规则 {gt_q30, lt_q30, gt_q70}（T1 cap=12）",
  "pipeline": "PipelineConfig(B=2000, block_len_days=auto(1/3/7 训练诊断))",
  "B": 2000,
  "alpha": 0.05,
  "L": "auto(1/3/7 训练诊断)",
  "delta": 0.2,
  "pi": 0.3
 },
 "results": [
  {
   "mechanism": "common_shock",
   "kind": "null",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 2,
   "n_failed": 11,
   "seeds": [
    100000,
    100001,
    100002,
    100003,
    100004
   ],
   "rate": 0.0020222446916076846,
   "ci": [
    0.00024499710726171606,
    0.007285773049121517
   ],
   "worst_case_rate": 0.013,
   "worst_case_ci": [
    0.006939617502851475,
    0.022127803636778486
   ],
   "tiers": {
    "T0": 154,
    "T1": 836,
    "invalid": 10
   },
   "wall_s": 1329.0,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 1859,
     "n_immature_excluded": 24,
     "block_mean_ac1": 0.054733956017369176,
     "cross_inst_corr": 0.4914068914238893,
     "n_clusters_train": 793,
     "grid_mu_removed": [
      0.13587985616786466,
      -0.05904228246215474,
      0.047474702595209355
     ],
     "idio_mu_removed": 0.009979708734839107
    },
    "base_R_sd": 1.6913538860342283,
    "n_episodes": 3938,
    "n_clusters": 1600,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.14807208807865466,
      "null": 0.23072140791044943,
      "episode_shuffle": 0.0037919364095885697
     },
     "sd": {
      "orig": 1.6913538860342283,
      "null": 1.7493745127697127
     },
     "kurtosis": {
      "orig": 3.6771038270891814,
      "null": 3.1932445387290405
     },
     "missing_rate": {
      "orig": 0.05510411376333164,
      "null": 0.05510411376333164
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": -0.07655417151831004,
      "null": 0.06085215497532561
     },
     "cross_instrument": {
      "orig": [
       0.3104279346587608,
       0.27808788484835506,
       0.3467927885941514
      ],
      "null": [
       0.3971523199885264,
       0.34931993088684704,
       0.4171508651517406
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 3,
     "L=3": 3,
     "L=7": 3
    },
    "dependence_aggregate": {
     "block_ac1": [
      -0.013154384439303108,
      -0.07655417151831004
     ],
     "cross_instrument": [
      [
       0.31385619278135485,
       0.3104279346587608
      ],
      [
       0.2800158529302134,
       0.27808788484835506
      ],
      [
       0.27141108933031916,
       0.3467927885941514
      ]
     ],
     "ok": true,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.27934265029346356,
       0.2622787745208851
      ],
      "cross_bands": [
       [
        0.04949646959733234,
        0.5794151467463275
       ],
       [
        0.0031814095026152095,
        0.4894471050290372
       ],
       [
        -0.011893034575876524,
        0.585433928044088
       ]
      ]
     }
    },
    "guard_fail_rate": 0.01,
    "invalid_reason": null,
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 10,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 3,
     "cross_instrument": 7,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=7": 320,
     "L=1": 279,
     "L=3": 391
    }
   },
   "verdict": "pass",
   "n_recovered": 15,
   "label": "",
   "n_T0": 154,
   "n_searched": 846,
   "searched_worst_rate": 0.015366430260047281,
   "searched_worst_ci": [
    0.008206681613178415,
    0.026133722662101188
   ]
  },
  {
   "mechanism": "cluster_heavy_tail",
   "kind": "null",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 6,
   "n_failed": 10,
   "seeds": [
    200000,
    200001,
    200002,
    200003,
    200004
   ],
   "rate": 0.006060606060606061,
   "ci": [
    0.0022272866241055496,
    0.01314440260373584
   ],
   "worst_case_rate": 0.016,
   "worst_case_ci": [
    0.009172319269222079,
    0.02585324908137346
   ],
   "tiers": {
    "T1": 968,
    "T0": 25,
    "invalid": 7
   },
   "wall_s": 1362.0,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 1909,
     "n_immature_excluded": 8,
     "block_mean_ac1": 0.020652651142966244,
     "cross_inst_corr": 0.11946239179897793,
     "n_clusters_train": 800,
     "grid_mu_removed": [
      0.18107418289786004,
      -0.07018879896289486,
      -0.20458834206850035
     ],
     "idio_mu_removed": -0.018950445759594883
    },
    "base_R_sd": 2.4971983920397878,
    "n_episodes": 3962,
    "n_clusters": 1600,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.07209811102152532,
      "null": 0.1637452660541624,
      "episode_shuffle": -0.002613938887759823
     },
     "sd": {
      "orig": 2.4971983920397878,
      "null": 2.720968664407409
     },
     "kurtosis": {
      "orig": 7.021691938515541,
      "null": 4.730563805898108
     },
     "missing_rate": {
      "orig": 0.05022715800100959,
      "null": 0.05022715800100959
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": -0.12629188750682344,
      "null": -0.03409702487971942
     },
     "cross_instrument": {
      "orig": [
       -0.011023567590563233,
       0.11228734262672135,
       0.25726487100481565
      ],
      "null": [
       0.15744748115968782,
       0.15921635644787827,
       0.17008880674293886
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 7,
     "L=3": 6,
     "L=7": 6
    },
    "dependence_aggregate": {
     "block_ac1": [
      -0.0030451104429366194,
      -0.12629188750682344
     ],
     "cross_instrument": [
      [
       0.08144577251778116,
       -0.011023567590563233
      ],
      [
       0.08302087282960227,
       0.11228734262672135
      ],
      [
       0.09831648854919822,
       0.25726487100481565
      ]
     ],
     "ok": false,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.37672407558092724,
       0.34498703198217856
      ],
      "cross_bands": [
       [
        -0.18775594640286192,
        0.38085216351517703
       ],
       [
        -0.2562618463464149,
        0.4047033639562726
       ],
       [
        -0.2063925589995935,
        0.3512195741090054
       ]
      ]
     }
    },
    "guard_fail_rate": 0.007,
    "invalid_reason": "AGGREGATE_DEPENDENCE_NOT_PRESERVED",
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 7,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 0,
     "cross_instrument": 7,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=3": 502,
     "L=7": 448,
     "L=1": 43
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 3,
   "label": "",
   "n_T0": 25,
   "n_searched": 975,
   "searched_worst_rate": 0.01641025641025641,
   "searched_worst_ci": [
    0.009408219841936944,
    0.026512739194480016
   ]
  },
  {
   "mechanism": "nonuniform_density",
   "kind": "null",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 8,
   "n_failed": 13,
   "seeds": [
    300000,
    300001,
    300002,
    300003,
    300004
   ],
   "rate": 0.008105369807497468,
   "ci": [
    0.0035056305551665803,
    0.015908049140209973
   ],
   "worst_case_rate": 0.021,
   "worst_case_ci": [
    0.013045192290387683,
    0.0319223351804155
   ],
   "tiers": {
    "T1": 724,
    "T0": 274,
    "invalid": 2
   },
   "wall_s": 1460.0,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 2096,
     "n_immature_excluded": 27,
     "block_mean_ac1": -0.019745157286099397,
     "cross_inst_corr": 0.445839694686786,
     "n_clusters_train": 889,
     "grid_mu_removed": [
      0.0016022805617793216,
      0.09298274095224139,
      0.06044478910974912
     ],
     "idio_mu_removed": 0.01091035724684454
    },
    "base_R_sd": 1.767495923449801,
    "n_episodes": 4077,
    "n_clusters": 1600,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.17538626505698038,
      "null": 0.18079375746411966,
      "episode_shuffle": -0.0019146228342183803
     },
     "sd": {
      "orig": 1.767495923449801,
      "null": 2.0564809435810423
     },
     "kurtosis": {
      "orig": 3.9420909658944394,
      "null": 3.974207908336098
     },
     "missing_rate": {
      "orig": 0.09614912926171204,
      "null": 0.09614912926171204
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": 0.11260297420596672,
      "null": -0.24502851272432444
     },
     "cross_instrument": {
      "orig": [
       0.4127375191237253,
       0.09753362107472027,
       0.34533824431764604
      ],
      "null": [
       0.2691532765204431,
       0.17196459960645785,
       0.37275546064586107
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 9,
     "L=3": 6,
     "L=7": 8
    },
    "dependence_aggregate": {
     "block_ac1": [
      0.004550458794383992,
      0.11260297420596672
     ],
     "cross_instrument": [
      [
       0.17807033465617056,
       0.4127375191237253
      ],
      [
       0.27049079251631847,
       0.09753362107472027
      ],
      [
       0.176937953226315,
       0.34533824431764604
      ]
     ],
     "ok": false,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.3632133628209186,
       0.37589686529512767
      ],
      "cross_bands": [
       [
        -0.2133321223131498,
        0.5741008657441822
       ],
       [
        -0.05956102447511733,
        0.6645020661199925
       ],
       [
        -0.1947812380432713,
        0.5970908412333429
       ]
      ]
     }
    },
    "guard_fail_rate": 0.002,
    "invalid_reason": "AGGREGATE_DEPENDENCE_NOT_PRESERVED",
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 2,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 0,
     "cross_instrument": 2,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=1": 784,
     "L=3": 119,
     "L=7": 95
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 10,
   "label": "",
   "n_T0": 274,
   "n_searched": 726,
   "searched_worst_rate": 0.028925619834710745,
   "searched_worst_ci": [
    0.01799268661652062,
    0.0438772540848671
   ]
  },
  {
   "mechanism": "circular_shift",
   "kind": "null",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 3,
   "n_failed": 4,
   "seeds": [
    400000,
    400001,
    400002,
    400003,
    400004
   ],
   "rate": 0.0030120481927710845,
   "ci": [
    0.0006215880038488219,
    0.008777030081934173
   ],
   "worst_case_rate": 0.007,
   "worst_case_ci": [
    0.002818858759620524,
    0.014369194978918632
   ],
   "tiers": {
    "T1": 989,
    "T0": 7,
    "invalid": 4
   },
   "wall_s": 1417.4,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 1812,
     "n_immature_excluded": 11,
     "block_mean_ac1": 0.17428462643994658,
     "cross_inst_corr": 0.5487551859624585,
     "n_clusters_train": 773,
     "grid_mu_removed": [
      -0.15985652535081996,
      -0.052303506874774086,
      0.04437679785874452
     ],
     "idio_mu_removed": 0.005806388389601082
    },
    "base_R_sd": 1.749074818707051,
    "n_episodes": 3982,
    "n_clusters": 1600,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.19236050990708312,
      "null": 0.20407750908912728,
      "episode_shuffle": -0.0015575031782725055
     },
     "sd": {
      "orig": 1.7490748187070513,
      "null": 1.8228189329373634
     },
     "kurtosis": {
      "orig": 3.0435398106808886,
      "null": 2.9668104035907237
     },
     "missing_rate": {
      "orig": 0.053490708186840784,
      "null": 0.053490708186840784
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": 0.012285292902872407,
      "null": 0.15540336775021682
     },
     "cross_instrument": {
      "orig": [
       0.45193398022330256,
       0.5463066751413748,
       0.4177664603553015
      ],
      "null": [
       0.340013142082322,
       0.4111562763872431,
       0.28801484411463657
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 3,
     "L=3": 4,
     "L=7": 5
    },
    "dependence_aggregate": {
     "block_ac1": [
      0.12324971783218262,
      0.012285292902872407
     ],
     "cross_instrument": [
      [
       0.3455990599694977,
       0.45193398022330256
      ],
      [
       0.335402193234277,
       0.5463066751413748
      ],
      [
       0.31235199427270244,
       0.4177664603553015
      ]
     ],
     "ok": true,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.0943634785474477,
       0.3065531440000737
      ],
      "cross_bands": [
       [
        0.08716311587077756,
        0.5403182470701771
       ],
       [
        0.07006684589499212,
        0.5638957073078157
       ],
       [
        0.10770126140627595,
        0.5325111297373667
       ]
      ]
     }
    },
    "guard_fail_rate": 0.004,
    "invalid_reason": null,
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 4,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 0,
     "cross_instrument": 4,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=1": 976,
     "L=7": 14,
     "L=3": 6
    }
   },
   "verdict": "pass",
   "n_recovered": 18,
   "label": "",
   "n_T0": 7,
   "n_searched": 993,
   "searched_worst_rate": 0.007049345417925478,
   "searched_worst_ci": [
    0.0028387618767474155,
    0.014470109073624018
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 327,
   "n_failed": 36,
   "seeds": [
    1100000,
    1100001,
    1100002,
    1100003,
    1100004
   ],
   "rate": 0.3392116182572614,
   "ci": [
    0.3093361265840949,
    0.3700731506088358
   ],
   "worst_case_rate": 0.327,
   "worst_case_ci": [
    0.2979696876186015,
    0.3570528972764215
   ],
   "tiers": {
    "T0": 231,
    "T1": 767,
    "invalid": 2
   },
   "wall_s": 1331.4,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 1883,
     "n_immature_excluded": 13,
     "block_mean_ac1": 0.002373027810941563,
     "cross_inst_corr": 0.4847531336915453,
     "n_clusters_train": 785,
     "grid_mu_removed": [
      -0.029257595719661293,
      0.14494307466025308,
      0.1780055551672457
     ],
     "idio_mu_removed": -0.014660437370500223
    },
    "base_R_sd": 1.7665101862523054,
    "n_episodes": 4013,
    "n_clusters": 1600,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.17364419932747285,
      "null": 0.1958949609734302,
      "episode_shuffle": -0.0007281700938828509
     },
     "sd": {
      "orig": 1.7665101862523054,
      "null": 1.831639089584655
     },
     "kurtosis": {
      "orig": 3.5273264646398625,
      "null": 3.1900744152321034
     },
     "missing_rate": {
      "orig": 0.05108397707450785,
      "null": 0.05108397707450785
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": 0.12653062129422826,
      "null": -0.10469290267324204
     },
     "cross_instrument": {
      "orig": [
       0.4353658458458776,
       0.5108732320599888,
       0.4276319744006372
      ],
      "null": [
       0.2500873408375453,
       0.3898817879396593,
       0.288896489012328
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 361,
     "L=3": 348,
     "L=7": 304
    },
    "dependence_aggregate": {
     "block_ac1": [
      0.01600634088573119,
      0.12653062129422826
     ],
     "cross_instrument": [
      [
       0.2895171744178191,
       0.4353658458458776
      ],
      [
       0.303992647400726,
       0.5108732320599888
      ],
      [
       0.2257244245302985,
       0.4276319744006372
      ]
     ],
     "ok": true,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.2677424562736512,
       0.3563547044376841
      ],
      "cross_bands": [
       [
        -0.05421757720275688,
        0.5601005680707473
       ],
       [
        -0.035030323857534955,
        0.547988070707258
       ],
       [
        -0.1750076208644203,
        0.4794438148962651
       ]
      ]
     }
    },
    "guard_fail_rate": 0.002,
    "invalid_reason": null,
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 2,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 0,
     "cross_instrument": 2,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=3": 469,
     "L=7": 482,
     "L=1": 47
    }
   },
   "verdict": "fail",
   "n_recovered": 424,
   "label": "",
   "n_T0": 231,
   "n_searched": 769,
   "searched_worst_rate": 0.42522756827048114,
   "searched_worst_ci": [
    0.3899801772590262,
    0.46105047618944006
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 96,
   "n_failed": 9,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.5026178010471204,
   "ci": [
    0.4295375553238755,
    0.5756155403646044
   ],
   "worst_case_rate": 0.48,
   "worst_case_ci": [
    0.4090139996380063,
    0.5515876013394261
   ],
   "tiers": {
    "T1": 173,
    "T0": 26,
    "invalid": 1
   },
   "wall_s": 265.3,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 1825,
     "n_immature_excluded": 9,
     "block_mean_ac1": 0.12755664694584193,
     "cross_inst_corr": 0.5323762749517615,
     "n_clusters_train": 771,
     "grid_mu_removed": [
      -0.10615906035752908,
      -0.05324762763818167,
      0.13016679421500102
     ],
     "idio_mu_removed": 0.010154039565334862
    },
    "base_R_sd": 1.7472250377935594,
    "n_episodes": 3971,
    "n_clusters": 1600,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.17896560099951855,
      "null": 0.18405323626149425,
      "episode_shuffle": -0.0014899154985594024
     },
     "sd": {
      "orig": 1.7472250377935594,
      "null": 1.6718333877533627
     },
     "kurtosis": {
      "orig": 3.359408510573203,
      "null": 2.995669920101139
     },
     "missing_rate": {
      "orig": 0.051120624527826744,
      "null": 0.051120624527826744
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": 0.03643321584844489,
      "null": 0.03552222718666951
     },
     "cross_instrument": {
      "orig": [
       0.4713859233958017,
       0.3784599147339644,
       0.41576851319836267
      ],
      "null": [
       0.37838937117101473,
       0.21663579336366706,
       0.23525821302111147
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 100,
     "L=3": 101,
     "L=7": 94
    },
    "dependence_aggregate": {
     "block_ac1": [
      0.014776186666978434,
      0.03643321584844489
     ],
     "cross_instrument": [
      [
       0.3461079772817359,
       0.4713859233958017
      ],
      [
       0.23998001544142408,
       0.3784599147339644
      ],
      [
       0.24775790870521625,
       0.41576851319836267
      ]
     ],
     "ok": true,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.36253055306107335,
       0.24916272614606635
      ],
      "cross_bands": [
       [
        -0.003493819394146905,
        0.6241933215374487
       ],
       [
        -0.05799096158451616,
        0.45566225399131655
       ],
       [
        -0.13627160464506002,
        0.5267893249751319
       ]
      ]
     }
    },
    "guard_fail_rate": 0.005,
    "invalid_reason": null,
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 1,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 1,
     "cross_instrument": 0,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=3": 92,
     "L=7": 76,
     "L=1": 31
    }
   },
   "verdict": "fail",
   "n_recovered": 113,
   "label": "δ=0.2 noise×1.0 clusters=1600",
   "n_T0": 26,
   "n_searched": 174,
   "searched_worst_rate": 0.5517241379310345,
   "searched_worst_ci": [
    0.4746168660929601,
    0.6270390263622934
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 146,
   "n_failed": 2,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.7373737373737373,
   "ci": [
    0.6702772379536669,
    0.7972144452821744
   ],
   "worst_case_rate": 0.73,
   "worst_case_ci": [
    0.6628474093126695,
    0.7901966625338547
   ],
   "tiers": {
    "T1": 190,
    "T0": 10
   },
   "wall_s": 508.9,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 2749,
     "n_immature_excluded": 9,
     "block_mean_ac1": 0.09411843066890975,
     "cross_inst_corr": 0.6378681133166559,
     "n_clusters_train": 1158,
     "grid_mu_removed": [
      -0.10259295729661713,
      0.06689423608727817,
      0.04788112634555819
     ],
     "idio_mu_removed": 0.0020399067467994085
    },
    "base_R_sd": 1.753488453607081,
    "n_episodes": 5899,
    "n_clusters": 2400,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.18836823256343158,
      "null": 0.18409425083252687,
      "episode_shuffle": 0.00021275968711119002
     },
     "sd": {
      "orig": 1.753488453607081,
      "null": 1.6659335288737147
     },
     "kurtosis": {
      "orig": 3.764843641344492,
      "null": 3.186848998248296
     },
     "missing_rate": {
      "orig": 0.04916087472452958,
      "null": 0.04916087472452958
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": 0.04075986895268823,
      "null": 0.06956342091858832
     },
     "cross_instrument": {
      "orig": [
       0.529160922473061,
       0.6154184374036676,
       0.6169203400540934
      ],
      "null": [
       0.5517134722972519,
       0.4184688439603238,
       0.46784057158498593
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 149,
     "L=3": 147,
     "L=7": 141
    },
    "dependence_aggregate": {
     "block_ac1": [
      0.011394381784461043,
      0.04075986895268823
     ],
     "cross_instrument": [
      [
       0.4401436699302134,
       0.529160922473061
      ],
      [
       0.41858876959923746,
       0.6154184374036676
      ],
      [
       0.4238207700521208,
       0.6169203400540934
      ]
     ],
     "ok": true,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.28129989299914904,
       0.3197467386781529
      ],
      "cross_bands": [
       [
        0.1432708020972901,
        0.6712883558036618
       ],
       [
        0.1261430635922882,
        0.6273644601980011
       ],
       [
        0.08018787034846611,
        0.6414123613446672
       ]
      ]
     }
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 0,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 0,
     "cross_instrument": 0,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=3": 25,
     "L=1": 154,
     "L=7": 21
    }
   },
   "verdict": "fail",
   "n_recovered": 164,
   "label": "δ=0.2 noise×1.0 clusters=2400",
   "n_T0": 10,
   "n_searched": 190,
   "searched_worst_rate": 0.7684210526315789,
   "searched_worst_ci": [
    0.7018586533248204,
    0.826408774874931
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 112,
   "n_failed": 16,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.6086956521739131,
   "ci": [
    0.5341719115250574,
    0.6796573889428255
   ],
   "worst_case_rate": 0.56,
   "worst_case_ci": [
    0.48825026907469704,
    0.6299444206609564
   ],
   "tiers": {
    "T0": 61,
    "T1": 131,
    "invalid": 8
   },
   "wall_s": 245.5,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 1825,
     "n_immature_excluded": 9,
     "block_mean_ac1": 0.12755664694584198,
     "cross_inst_corr": 0.5323762749517613,
     "n_clusters_train": 771,
     "grid_mu_removed": [
      -0.06369543621451744,
      -0.03194857658290903,
      0.07810007652900051
     ],
     "idio_mu_removed": 0.006092423739200908
    },
    "base_R_sd": 1.0483350226761357,
    "n_episodes": 3971,
    "n_clusters": 1600,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.17896560099951866,
      "null": 0.19477784734480194,
      "episode_shuffle": -0.0014899154985594267
     },
     "sd": {
      "orig": 1.0483350226761357,
      "null": 1.0289595352074965
     },
     "kurtosis": {
      "orig": 3.359408510568075,
      "null": 3.0167496360894583
     },
     "missing_rate": {
      "orig": 0.051120624527826744,
      "null": 0.051120624527826744
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": 0.036433215848444986,
      "null": 0.09163400730011412
     },
     "cross_instrument": {
      "orig": [
       0.4713859233958016,
       0.37845991473396445,
       0.4157685131983625
      ],
      "null": [
       0.4089398586105471,
       0.24781483032037913,
       0.29717144277923047
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 121,
     "L=3": 118,
     "L=7": 111
    },
    "dependence_aggregate": {
     "block_ac1": [
      0.06361846375281793,
      0.036433215848444986
     ],
     "cross_instrument": [
      [
       0.3863648999479905,
       0.4713859233958016
      ],
      [
       0.28393502534628,
       0.37845991473396445
      ],
      [
       0.2914921901642086,
       0.4157685131983625
      ]
     ],
     "ok": true,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.3625305530610733,
       0.24916272614606635
      ],
      "cross_bands": [
       [
        -0.003493819394146877,
        0.6241933215374486
       ],
       [
        -0.05799096158451617,
        0.45566225399131655
       ],
       [
        -0.13627160464506002,
        0.5267893249751318
       ]
      ]
     }
    },
    "guard_fail_rate": 0.04,
    "invalid_reason": null,
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 8,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 4,
     "cross_instrument": 4,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=7": 94,
     "L=3": 85,
     "L=1": 13
    }
   },
   "verdict": "fail",
   "n_recovered": 115,
   "label": "δ=0.2 noise×0.6 clusters=1600",
   "n_T0": 61,
   "n_searched": 139,
   "searched_worst_rate": 0.8057553956834532,
   "searched_worst_ci": [
    0.7301128783642399,
    0.8679167068723417
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 168,
   "n_failed": 3,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.8527918781725888,
   "ci": [
    0.7954740683059629,
    0.8991430545788688
   ],
   "worst_case_rate": 0.84,
   "worst_case_ci": [
    0.7817009846486592,
    0.8879125023541982
   ],
   "tiers": {
    "T1": 172,
    "T0": 27,
    "invalid": 1
   },
   "wall_s": 515.6,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 2749,
     "n_immature_excluded": 9,
     "block_mean_ac1": 0.09411843066890979,
     "cross_inst_corr": 0.6378681133166558,
     "n_clusters_train": 1158,
     "grid_mu_removed": [
      -0.061555774377970295,
      0.0401365416523669,
      0.028728675807334816
     ],
     "idio_mu_removed": 0.0012239440480796362
    },
    "base_R_sd": 1.0520930721642485,
    "n_episodes": 5899,
    "n_clusters": 2400,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.18836823256343144,
      "null": 0.19147209293633297,
      "episode_shuffle": 0.00021275968711117511
     },
     "sd": {
      "orig": 1.0520930721642485,
      "null": 1.0235957273245113
     },
     "kurtosis": {
      "orig": 3.764843641338766,
      "null": 3.155500952561325
     },
     "missing_rate": {
      "orig": 0.04916087472452958,
      "null": 0.04916087472452958
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": 0.04075986895268822,
      "null": 0.07164759674788129
     },
     "cross_instrument": {
      "orig": [
       0.529160922473061,
       0.6154184374036675,
       0.6169203400540934
      ],
      "null": [
       0.5709388570977119,
       0.4486899573774962,
       0.48201158998019017
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 170,
     "L=3": 170,
     "L=7": 168
    },
    "dependence_aggregate": {
     "block_ac1": [
      0.061122411345015795,
      0.04075986895268822
     ],
     "cross_instrument": [
      [
       0.48044612154854166,
       0.529160922473061
      ],
      [
       0.46155925927858754,
       0.6154184374036675
      ],
      [
       0.4617078588768267,
       0.6169203400540934
      ]
     ],
     "ok": true,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.28129989299914915,
       0.31974673867815295
      ],
      "cross_bands": [
       [
        0.14327080209729,
        0.6712883558036619
       ],
       [
        0.12614306359228813,
        0.6273644601980013
       ],
       [
        0.08018787034846608,
        0.6414123613446673
       ]
      ]
     }
    },
    "guard_fail_rate": 0.005,
    "invalid_reason": null,
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 1,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 1,
     "cross_instrument": 0,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=3": 36,
     "L=1": 122,
     "L=7": 41
    }
   },
   "verdict": "insufficient",
   "n_recovered": 166,
   "label": "δ=0.2 noise×0.6 clusters=2400",
   "n_T0": 27,
   "n_searched": 173,
   "searched_worst_rate": 0.9710982658959537,
   "searched_worst_ci": [
    0.9338447052702621,
    0.9905504452848978
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 90,
   "n_failed": 21,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.5027932960893855,
   "ci": [
    0.42724471595342967,
    0.5782478472926174
   ],
   "worst_case_rate": 0.45,
   "worst_case_ci": [
    0.3797535899788847,
    0.5217506890064616
   ],
   "tiers": {
    "T0": 84,
    "T1": 102,
    "invalid": 14
   },
   "wall_s": 226.6,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 1825,
     "n_immature_excluded": 9,
     "block_mean_ac1": 0.12755664694584193,
     "cross_inst_corr": 0.5323762749517615,
     "n_clusters_train": 771,
     "grid_mu_removed": [
      -0.10615906035752908,
      -0.05324762763818167,
      0.13016679421500102
     ],
     "idio_mu_removed": 0.010154039565334862
    },
    "base_R_sd": 1.7472250377935594,
    "n_episodes": 3971,
    "n_clusters": 1600,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.17896560099951855,
      "null": 0.20285312171694975,
      "episode_shuffle": -0.0014899154985594024
     },
     "sd": {
      "orig": 1.7472250377935594,
      "null": 1.7460037773432915
     },
     "kurtosis": {
      "orig": 3.359408510573203,
      "null": 3.0182467618139395
     },
     "missing_rate": {
      "orig": 0.051120624527826744,
      "null": 0.051120624527826744
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": 0.03643321584844489,
      "null": 0.1233269400275682
     },
     "cross_instrument": {
      "orig": [
       0.4713859233958017,
       0.3784599147339644,
       0.41576851319836267
      ],
      "null": [
       0.42966904064444794,
       0.27035908919619234,
       0.332476339489206
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 98,
     "L=3": 96,
     "L=7": 90
    },
    "dependence_aggregate": {
     "block_ac1": [
      0.09060624574843783,
      0.03643321584844489
     ],
     "cross_instrument": [
      [
       0.41046938919612963,
       0.4713859233958017
      ],
      [
       0.31064257352072744,
       0.3784599147339644
      ],
      [
       0.31786372088834647,
       0.41576851319836267
      ]
     ],
     "ok": true,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.36253055306107335,
       0.24916272614606635
      ],
      "cross_bands": [
       [
        -0.003493819394146905,
        0.6241933215374487
       ],
       [
        -0.05799096158451616,
        0.45566225399131655
       ],
       [
        -0.13627160464506002,
        0.5267893249751319
       ]
      ]
     }
    },
    "guard_fail_rate": 0.07,
    "invalid_reason": "GUARD_FAIL_RATE 0.070 > 0.05",
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 14,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 8,
     "cross_instrument": 6,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=7": 108,
     "L=3": 75,
     "L=1": 3
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 89,
   "label": "δ=0.4 noise×1.0 clusters=1600",
   "n_T0": 84,
   "n_searched": 116,
   "searched_worst_rate": 0.7758620689655172,
   "searched_worst_ci": [
    0.6890855706739115,
    0.8480592205731532
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 139,
   "n_failed": 4,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.7091836734693877,
   "ci": [
    0.6402133667923662,
    0.7717033369898629
   ],
   "worst_case_rate": 0.695,
   "worst_case_ci": [
    0.6261321954662298,
    0.7579805629764088
   ],
   "tiers": {
    "T1": 143,
    "T0": 54,
    "invalid": 3
   },
   "wall_s": 447.2,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 2749,
     "n_immature_excluded": 9,
     "block_mean_ac1": 0.09411843066890975,
     "cross_inst_corr": 0.6378681133166559,
     "n_clusters_train": 1158,
     "grid_mu_removed": [
      -0.10259295729661713,
      0.06689423608727817,
      0.04788112634555819
     ],
     "idio_mu_removed": 0.0020399067467994085
    },
    "base_R_sd": 1.753488453607081,
    "n_episodes": 5899,
    "n_clusters": 2400,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.18836823256343158,
      "null": 0.1979501088785945,
      "episode_shuffle": 0.00021275968711119002
     },
     "sd": {
      "orig": 1.753488453607081,
      "null": 1.7344295660793358
     },
     "kurtosis": {
      "orig": 3.764843641344492,
      "null": 3.1326102036311485
     },
     "missing_rate": {
      "orig": 0.04916087472452958,
      "null": 0.04916087472452958
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": 0.04075986895268823,
      "null": 0.0824402340584448
     },
     "cross_instrument": {
      "orig": [
       0.529160922473061,
       0.6154184374036676,
       0.6169203400540934
      ],
      "null": [
       0.5846801508370695,
       0.470221689118524,
       0.4942412936528484
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 141,
     "L=3": 141,
     "L=7": 139
    },
    "dependence_aggregate": {
     "block_ac1": [
      0.08871477491738464,
      0.04075986895268823
     ],
     "cross_instrument": [
      [
       0.504220820128715,
       0.529160922473061
      ],
      [
       0.4869069441787984,
       0.6154184374036676
      ],
      [
       0.4844974873587523,
       0.6169203400540934
      ]
     ],
     "ok": true,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.28129989299914904,
       0.3197467386781529
      ],
      "cross_bands": [
       [
        0.1432708020972901,
        0.6712883558036618
       ],
       [
        0.1261430635922882,
        0.6273644601980011
       ],
       [
        0.08018787034846611,
        0.6414123613446672
       ]
      ]
     }
    },
    "guard_fail_rate": 0.015,
    "invalid_reason": null,
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 3,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 2,
     "cross_instrument": 1,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=1": 101,
     "L=3": 32,
     "L=7": 64
    }
   },
   "verdict": "fail",
   "n_recovered": 138,
   "label": "δ=0.4 noise×1.0 clusters=2400",
   "n_T0": 54,
   "n_searched": 146,
   "searched_worst_rate": 0.952054794520548,
   "searched_worst_ci": [
    0.9037108502118045,
    0.9805089913595076
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 18,
   "n_failed": 104,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.1875,
   "ci": [
    0.1150779622212126,
    0.2800462474267437
   ],
   "worst_case_rate": 0.09,
   "worst_case_ci": [
    0.054214254456096365,
    0.13850820847382211
   ],
   "tiers": {
    "T0": 77,
    "invalid": 104,
    "T1": 19
   },
   "wall_s": 106.8,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 1825,
     "n_immature_excluded": 9,
     "block_mean_ac1": 0.12755664694584198,
     "cross_inst_corr": 0.5323762749517613,
     "n_clusters_train": 771,
     "grid_mu_removed": [
      -0.06369543621451744,
      -0.03194857658290903,
      0.07810007652900051
     ],
     "idio_mu_removed": 0.006092423739200908
    },
    "base_R_sd": 1.0483350226761357,
    "n_episodes": 3971,
    "n_clusters": 1600,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.17896560099951866,
      "null": 0.24525884653715865,
      "episode_shuffle": -0.0014899154985594267
     },
     "sd": {
      "orig": 1.0483350226761357,
      "null": 1.155528753624875
     },
     "kurtosis": {
      "orig": 3.359408510568075,
      "null": 2.9403914319889335
     },
     "missing_rate": {
      "orig": 0.051120624527826744,
      "null": 0.051120624527826744
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": 0.036433215848444986,
      "null": 0.2383837861964675
     },
     "cross_instrument": {
      "orig": [
       0.4713859233958016,
       0.37845991473396445,
       0.4157685131983625
      ],
      "null": [
       0.5260348281993824,
       0.3797477414893937,
       0.4739033888506034
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 18,
     "L=3": 18,
     "L=7": 18
    },
    "dependence_aggregate": {
     "block_ac1": [
      0.18928259166420042,
      0.036433215848444986
     ],
     "cross_instrument": [
      [
       0.5120246878103734,
       0.4713859233958016
      ],
      [
       0.4258355063653088,
       0.37845991473396445
      ],
      [
       0.43066022585378877,
       0.4157685131983625
      ]
     ],
     "ok": true,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.3625305530610733,
       0.24916272614606635
      ],
      "cross_bands": [
       [
        -0.003493819394146877,
        0.6241933215374486
       ],
       [
        -0.05799096158451617,
        0.45566225399131655
       ],
       [
        -0.13627160464506002,
        0.5267893249751318
       ]
      ]
     }
    },
    "guard_fail_rate": 0.52,
    "invalid_reason": "GUARD_FAIL_RATE 0.520 > 0.05",
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 104,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 48,
     "cross_instrument": 80,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=7": 78,
     "L=3": 18
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 15,
   "label": "δ=0.4 noise×0.6 clusters=1600",
   "n_T0": 77,
   "n_searched": 123,
   "searched_worst_rate": 0.14634146341463414,
   "searched_worst_ci": [
    0.0890976630218232,
    0.22138825727517936
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 40,
   "n_failed": 85,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.34782608695652173,
   "ci": [
    0.2614225964610953,
    0.44227794610050253
   ],
   "worst_case_rate": 0.2,
   "worst_case_ci": [
    0.1468944882157588,
    0.2622263645896002
   ],
   "tiers": {
    "T1": 40,
    "invalid": 85,
    "T0": 75
   },
   "wall_s": 255.6,
   "diagnostics": {
    "residual_model": {
     "train_days": 180,
     "n_train_obs": 2749,
     "n_immature_excluded": 9,
     "block_mean_ac1": 0.09411843066890979,
     "cross_inst_corr": 0.6378681133166558,
     "n_clusters_train": 1158,
     "grid_mu_removed": [
      -0.061555774377970295,
      0.0401365416523669,
      0.028728675807334816
     ],
     "idio_mu_removed": 0.0012239440480796362
    },
    "base_R_sd": 1.0520930721642485,
    "n_episodes": 5899,
    "n_clusters": 2400,
    "shuffle_guard": {
     "block_icc": {
      "orig": 0.18836823256343144,
      "null": 0.23563544477192566,
      "episode_shuffle": 0.00021275968711117511
     },
     "sd": {
      "orig": 1.0520930721642485,
      "null": 1.1386655821141958
     },
     "kurtosis": {
      "orig": 3.764843641338766,
      "null": 3.001460099108495
     },
     "missing_rate": {
      "orig": 0.04916087472452958,
      "null": 0.04916087472452958
     },
     "checks": {
      "cluster_layout": true,
      "missing_layout": true,
      "block_ac1": true,
      "cross_instrument": true,
      "finite_diagnostics": true,
      "icc": true,
      "scale": true,
      "tail": true,
      "missing": true
     },
     "block_ac1": {
      "orig": 0.04075986895268822,
      "null": 0.15816621601488265
     },
     "cross_instrument": {
      "orig": [
       0.529160922473061,
       0.6154184374036675,
       0.6169203400540934
      ],
      "null": [
       0.6497422096378774,
       0.5734124209092691,
       0.5604804797098075
      ]
     },
     "ok": true
    },
    "positive_by_block_len": {
     "L=1": 40,
     "L=3": 40,
     "L=7": 40
    },
    "dependence_aggregate": {
     "block_ac1": [
      0.18996609466537812,
      0.04075986895268822
     ],
     "cross_instrument": [
      [
       0.6014547582480838,
       0.529160922473061
      ],
      [
       0.5906460694666632,
       0.6154184374036675
      ],
      [
       0.5805544825280735,
       0.6169203400540934
      ]
     ],
     "ok": true,
     "reference": {
      "n_calib": 60,
      "q": [
       0.1,
       99.9
      ],
      "ac1_band": [
       -0.28129989299914915,
       0.31974673867815295
      ],
      "cross_bands": [
       [
        0.14327080209729,
        0.6712883558036619
       ],
       [
        0.12614306359228813,
        0.6273644601980013
       ],
       [
        0.08018787034846608,
        0.6414123613446673
       ]
      ]
     }
    },
    "guard_fail_rate": 0.425,
    "invalid_reason": "GUARD_FAIL_RATE 0.425 > 0.05",
    "calibration_consistent_with_orig": true,
    "n_invalid_null_model": 85,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 18,
     "cross_instrument": 75,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=1": 18,
     "L=7": 70,
     "L=3": 27
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 39,
   "label": "δ=0.4 noise×0.6 clusters=2400",
   "n_T0": 75,
   "n_searched": 125,
   "searched_worst_rate": 0.32,
   "searched_worst_ci": [
    0.23941939107066357,
    0.40933746408180854
   ]
  }
 ]
}
```
