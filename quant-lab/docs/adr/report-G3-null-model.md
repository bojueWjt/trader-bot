# report-G3-null-model：空模型 FPR / 功效验收（合成 T1 规模，synthetic_validation）

日期：2026-09-11 04:01 UTC。状态：合成数据实测；**claim_status = descriptive_only**（G-STAT-CLAIM pending），本报告不构成任何研究优势声明，也不替代真实数据前的空模型验收。
依据：合并稿 D.5、ADR-G3 §10、Claude 验收 R03（按档分级：T1/T2 各机制 1000 次；T3 200 次预注册更宽精确区间）。
命令：`python -m quant_lab.research.nullmodel`

## 1. 设定

- 合成世界：WorldConfig(mechanism='common_shock', n_clusters=1600, span_days=360, n_candidates=12, delta=0.0, pi=0.3, censor_frac=0.05, cluster_size_max=4, noise_scale=1.0, seed=0)；候选池：36 个（12 独立特征 × 规则 {gt_q30, lt_q30, gt_q70}（36 提交，T1 cap=12 → 按规范顺序前 12 个 = f00..f03 × 3 规则，含植入候选 f00:gt_q30））；流水线：PipelineConfig(scheme='expanding', test_span_days=60, min_train_span_days=180, embargo_days=1.0, label_maturity_days=0.0, min_train_clusters=20, search_frac=0.5, block_len_days=None, block_len_sensitivity=(1, 3, 7), B=2000, alpha=0.05, config_cap=None, seed=0, max_folds=None)。
- 空模型：训练窗（前 50% 天）拟合联合残差 → 整 L 日块（全品种联动）重采样格点冲击 + 整簇重采样特异残差 → 固定 episode 布局暴露映射；因果特征独立随机流重生成；**禁止逐 episode 洗牌**（结构断言 + 诊断见 §4）。
- 每次 replicate 完整流程：walk-forward 折 → 分档/预算 → search（阈值只在 search 子窗拟合）→ selection（共同块 max-t 一次，B=2000，α=0.05）→ 外折每机会一次预测 → final 全时间共同块 θ/LB（主 L=auto(1/3/7 训练诊断)：由首折训练窗残差块均值 lag-1 自相关诊断在 1/3/7 中预注册，不看候选结果；1/3/7 日敏感性另报）。
- FPR 事件：final LB > 0（合成声明函数，输出 synthetic_validation）；失败/insufficient 单列并给最坏界。
- 验收阈值：FPR 双侧 95% Clopper–Pearson 上界 ≤ 7%；功效（δ=0.2 R/种子，π=0.3）下界 ≥ 80%。
- 环境：arm64 / Python 3.12.13；seed 清单：每机制 seed0×100000+i（seed0 见表）。

## 2. FPR（每种预注册空机制）

| 机制 | 计划 n | 完成 | 失败/insufficient | 阳性 x | FPR 点估计 | FPR 95% CI（区间） | 最坏界（失败计阳性）FPR / CI | 有搜索 replicate（非 T0）n / 最坏界 FPR / CI | 阳性数按块长 L=1/3/7（敏感性，不选） | 档分布 | 耗时 s | 判定 |
|---|---:|---:|---:|---:|---:|---|---|---|---|---|---:|---|
| common_shock | 1000 | 1000 | 206 | 2 | 0.25% | [0.03%, 0.91%] | 20.80% / [18.32%, 23.45%] | 871 / 23.88% / [21.08%, 26.86%] | 3 / 1 / 2（主 L 分布 {"L=7": 265, "L=1": 200, "L=3": 329}） | {"T0": 129, "T1": 665, "invalid": 206} | 1169.0 | **invalid_null_model** |
| cluster_heavy_tail | 1000 | 1000 | 696 | 0 | 0.00% | [0.00%, 1.21%] | 69.60% / [66.64%, 72.44%] | 990 / 70.30% / [67.35%, 73.14%] | 0 / 0 / 0（主 L 分布 {"L=3": 158, "L=7": 136, "L=1": 11}） | {"T1": 295, "invalid": 695, "T0": 10} | 571.1 | **invalid_null_model** |
| nonuniform_density | 1000 | 1000 | 842 | 0 | 0.00% | [0.00%, 2.31%] | 84.20% / [81.79%, 86.41%] | 937 / 89.86% / [87.75%, 91.72%] | 0 / 0 / 1（主 L 分布 {"L=3": 24, "L=1": 112, "L=7": 22}） | {"invalid": 842, "T0": 63, "T1": 95} | 353.8 | **invalid_null_model** |
| circular_shift | 1000 | 1000 | 374 | 2 | 0.32% | [0.04%, 1.15%] | 37.60% / [34.59%, 40.69%] | 994 / 37.83% / [34.80%, 40.92%] | 2 / 1 / 4（主 L 分布 {"L=1": 613, "L=7": 10, "L=3": 3}） | {"T1": 620, "invalid": 374, "T0": 6} | 993.8 | **invalid_null_model** |

- 最坏机制 FPR CI 上界：86.41%（阈值 7%）。所有机制均须过门，不混池稀释。
- T0 replicate（基线 DEFF 降档 → cap=0 无搜索）必然 no_claim，不作 FPR 证据；验收以「有搜索 replicate」条件最坏界为准，并要求其占多数且 ≥ 500 次。

## 3. 功效（注入 δ）

| 机制 | 计划 n | 完成 | 失败 | 检出 x | 功效点估计 | 功效 95% CI | 最坏界（失败计未检出）功效 / CI | 规则找回率 | base_R sd（噪声） | 耗时 s | 判定 |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---|
| common_shock | 1000 | 1000 | 663 | 102 | 30.27% | [25.41%, 35.48%] | 10.20% / [8.39%, 12.24%]（有搜索 885：11.53% / [9.50%, 13.81%]） | 13.3% | 1.77 | 610.0 | **invalid_null_model** |

- 功效未达标时按 D.5「未达标限制声明或扩新样本」处理：本档（T1，约 1600 簇 / 4013 机会，噪声 sd≈1.77R）对 δ=0.2R 的全流程功效见上表；§3.1 给出效应/噪声/样本规模的适用范围。流程的功效瓶颈在 selection（内层 max-t 只见训练窗一半样本）。

### 3.1 功效敏感性（效应 / 噪声 / 样本规模；每格 n=200，非验收，只描述阈值适用范围）

| 设定 | 机制 | n | 检出 | 功效 | 95% CI | 规则找回率（植入候选被选中 ≥1 折） | base_R sd | 簇数 | 档分布 |
|---|---|---:|---:|---:|---|---:|---:|---:|---|
| δ=0.2 noise×1.0 clusters=1600 | common_shock | 200 | 46 | 51.7% | [40.84%, 62.41%] | 26.5% | 1.75 | 1600 | {"T1": 78, "T0": 14, "invalid": 108} |
| δ=0.2 noise×1.0 clusters=2400 | common_shock | 200 | 97 | 77.6% | [69.28%, 84.57%] | 51.5% | 1.75 | 2400 | {"T1": 118, "invalid": 74, "T0": 8} |
| δ=0.2 noise×0.6 clusters=1600 | common_shock | 200 | 80 | 58.4% | [49.67%, 66.75%] | 42.0% | 1.05 | 1600 | {"T0": 50, "T1": 95, "invalid": 55} |
| δ=0.2 noise×0.6 clusters=2400 | common_shock | 200 | 145 | 84.8% | [78.52%, 89.82%] | 72.0% | 1.05 | 2400 | {"T1": 149, "T0": 24, "invalid": 27} |
| δ=0.4 noise×1.0 clusters=1600 | common_shock | 200 | 71 | 45.8% | [37.79%, 53.99%] | 36.0% | 1.75 | 1600 | {"T0": 81, "T1": 82, "invalid": 37} |
| δ=0.4 noise×1.0 clusters=2400 | common_shock | 200 | 129 | 70.1% | [62.94%, 76.62%] | 64.5% | 1.75 | 2400 | {"T1": 133, "T0": 52, "invalid": 15} |
| δ=0.4 noise×0.6 clusters=1600 | common_shock | 200 | 26 | 14.7% | [9.83%, 20.78%] | 10.0% | 1.05 | 1600 | {"T0": 150, "T1": 27, "invalid": 23} |
| δ=0.4 noise×0.6 clusters=2400 | common_shock | 200 | 48 | 28.9% | [22.15%, 36.45%] | 22.5% | 1.05 | 2400 | {"T1": 48, "T0": 118, "invalid": 34} |

## 4. 空模型诊断（块内相关是否保留）

| 机制 | 训练窗天数 / 观测 | 块均值 lag-1 自相关 | 跨品种格点相关 | 块 ICC orig / null / 逐 episode 洗牌 | 通过 |
|---|---|---:|---:|---|---|
| common_shock | 180 / 1859（未成熟排除 24） | 0.055 | 0.491 | 0.1481 / 0.2307 / 0.0038 | True（逐 replicate 诊断失败 206 次） |
| cluster_heavy_tail | 180 / 1909（未成熟排除 8） | 0.021 | 0.119 | 0.0721 / 0.1637 / -0.0026 | True（逐 replicate 诊断失败 695 次） |
| nonuniform_density | 180 / 2096（未成熟排除 27） | -0.020 | 0.446 | 0.1754 / 0.1808 / -0.0019 | False（逐 replicate 诊断失败 842 次） |
| circular_shift | 180 / 1812（未成熟排除 11） | 0.174 | 0.549 | 0.1924 / 0.2041 / -0.0016 | True（逐 replicate 诊断失败 374 次） |

每个 replicate 都跑块 ICC 诊断；任一失败计入失败最坏界并使该机制 verdict=invalid_null_model（不得 pass）；全部落 T0 的机制标 not_run_T0（cap=0 无搜索，FPR 平凡为 0，不作 T1 验收替身）。

## 5. 总判定（synthetic_validation）

- FPR：common_shock=invalid_null_model、cluster_heavy_tail=invalid_null_model、nonuniform_density=invalid_null_model、circular_shift=invalid_null_model；扩展：无。
- 功效（δ=0.2）：common_shock=invalid_null_model。
- 全部结果只描述；任何档位的真实数据声明仍需 G-STAT-CLAIM。

## 6. 资源与限制

- 总耗时 6293s；单 replicate 均值 0.954s；峰值 RSS 见 report.json。
- 限制：合成世界的相关结构是预注册假设，不等于真实频道数据；T3 档（200 次）未运行；块长敏感性只报告不选择；max-t 是依赖假设下近似，不是有限样本保证。
- 任何真实数据的 θ 声明须另行通过 G-STAT-CLAIM、最终 V 窗口与 latency=1s 敏感性；本报告结果只描述。

## 附：原始结果 JSON

```json
{
 "meta": {
  "command": "python -m quant_lab.research.nullmodel",
  "world": "WorldConfig(mechanism='common_shock', n_clusters=1600, span_days=360, n_candidates=12, delta=0.0, pi=0.3, censor_frac=0.05, cluster_size_max=4, noise_scale=1.0, seed=0)",
  "n_candidates": 36,
  "candidates": "12 独立特征 × 规则 {gt_q30, lt_q30, gt_q70}（36 提交，T1 cap=12 → 按规范顺序前 12 个 = f00..f03 × 3 规则，含植入候选 f00:gt_q30）",
  "pipeline": "PipelineConfig(scheme='expanding', test_span_days=60, min_train_span_days=180, embargo_days=1.0, label_maturity_days=0.0, min_train_clusters=20, search_frac=0.5, block_len_days=None, block_len_sensitivity=(1, 3, 7), B=2000, alpha=0.05, config_cap=None, seed=0, max_folds=None)",
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
   "n_failed": 206,
   "seeds": [
    100000,
    100001,
    100002,
    100003,
    100004
   ],
   "rate": 0.0025188916876574307,
   "ci": [
    0.00030519517085261333,
    0.009069215002587363
   ],
   "worst_case_rate": 0.208,
   "worst_case_ci": [
    0.18323331957388345,
    0.234498525128187
   ],
   "tiers": {
    "T0": 129,
    "T1": 665,
    "invalid": 206
   },
   "wall_s": 1169.0,
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
     "L=3": 1,
     "L=7": 2
    },
    "n_invalid_null_model": 206,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 26,
     "cross_instrument": 186,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=7": 265,
     "L=1": 200,
     "L=3": 329
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 10,
   "label": "",
   "n_T0": 129,
   "n_searched": 871,
   "searched_worst_rate": 0.23880597014925373,
   "searched_worst_ci": [
    0.21084025504306644,
    0.26855077004834277
   ]
  },
  {
   "mechanism": "cluster_heavy_tail",
   "kind": "null",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 0,
   "n_failed": 696,
   "seeds": [
    200000,
    200001,
    200002,
    200003,
    200004
   ],
   "rate": 0.0,
   "ci": [
    0.0,
    0.012061146074207496
   ],
   "worst_case_rate": 0.696,
   "worst_case_ci": [
    0.6664435613103112,
    0.724397400780902
   ],
   "tiers": {
    "T1": 295,
    "invalid": 695,
    "T0": 10
   },
   "wall_s": 571.1,
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
     "L=1": 0,
     "L=3": 0,
     "L=7": 0
    },
    "n_invalid_null_model": 695,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 83,
     "cross_instrument": 667,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=3": 158,
     "L=7": 136,
     "L=1": 11
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 1,
   "label": "",
   "n_T0": 10,
   "n_searched": 990,
   "searched_worst_rate": 0.703030303030303,
   "searched_worst_ci": [
    0.6734887679015258,
    0.7313587994899058
   ]
  },
  {
   "mechanism": "nonuniform_density",
   "kind": "null",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 0,
   "n_failed": 842,
   "seeds": [
    300000,
    300001,
    300002,
    300003,
    300004
   ],
   "rate": 0.0,
   "ci": [
    0.0,
    0.023076897989719514
   ],
   "worst_case_rate": 0.842,
   "worst_case_ci": [
    0.8178923975121414,
    0.8640734838150351
   ],
   "tiers": {
    "invalid": 842,
    "T0": 63,
    "T1": 95
   },
   "wall_s": 353.8,
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
      "block_ac1": false,
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
     "ok": false
    },
    "positive_by_block_len": {
     "L=1": 0,
     "L=3": 0,
     "L=7": 1
    },
    "n_invalid_null_model": 842,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 83,
     "cross_instrument": 824,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=3": 24,
     "L=1": 112,
     "L=7": 22
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 1,
   "label": "",
   "n_T0": 63,
   "n_searched": 937,
   "searched_worst_rate": 0.8986125933831377,
   "searched_worst_ci": [
    0.8774819802081695,
    0.9171958626087546
   ]
  },
  {
   "mechanism": "circular_shift",
   "kind": "null",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 2,
   "n_failed": 374,
   "seeds": [
    400000,
    400001,
    400002,
    400003,
    400004
   ],
   "rate": 0.003194888178913738,
   "ci": [
    0.00038715023731868587,
    0.011492973299239772
   ],
   "worst_case_rate": 0.376,
   "worst_case_ci": [
    0.345881070345914,
    0.406851376024138
   ],
   "tiers": {
    "T1": 620,
    "invalid": 374,
    "T0": 6
   },
   "wall_s": 993.8,
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
     "L=1": 2,
     "L=3": 1,
     "L=7": 4
    },
    "n_invalid_null_model": 374,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 3,
     "cross_instrument": 374,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=1": 613,
     "L=7": 10,
     "L=3": 3
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 7,
   "label": "",
   "n_T0": 6,
   "n_searched": 994,
   "searched_worst_rate": 0.3782696177062374,
   "searched_worst_ci": [
    0.34801733439553323,
    0.4092453031062234
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 102,
   "n_failed": 663,
   "seeds": [
    1100000,
    1100001,
    1100002,
    1100003,
    1100004
   ],
   "rate": 0.3026706231454006,
   "ci": [
    0.2540622724288786,
    0.3547869475874547
   ],
   "worst_case_rate": 0.102,
   "worst_case_ci": [
    0.08393546004048677,
    0.12244515106555805
   ],
   "tiers": {
    "T0": 115,
    "invalid": 651,
    "T1": 234
   },
   "wall_s": 610.0,
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
     "L=1": 114,
     "L=3": 109,
     "L=7": 95
    },
    "n_invalid_null_model": 651,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 66,
     "cross_instrument": 619,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=3": 152,
     "L=7": 191,
     "L=1": 6
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 133,
   "label": "",
   "n_T0": 115,
   "n_searched": 885,
   "searched_worst_rate": 0.1152542372881356,
   "searched_worst_ci": [
    0.09495933372993559,
    0.13814966859902716
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 46,
   "n_failed": 111,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.5168539325842697,
   "ci": [
    0.408418739824148,
    0.6241360763706899
   ],
   "worst_case_rate": 0.23,
   "worst_case_ci": [
    0.1735808628015912,
    0.2946063945659807
   ],
   "tiers": {
    "T1": 78,
    "T0": 14,
    "invalid": 108
   },
   "wall_s": 153.5,
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
     "L=1": 49,
     "L=3": 49,
     "L=7": 46
    },
    "n_invalid_null_model": 108,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 0,
     "cross_instrument": 108,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=3": 41,
     "L=7": 38,
     "L=1": 13
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 53,
   "label": "δ=0.2 noise×1.0 clusters=1600",
   "n_T0": 14,
   "n_searched": 186,
   "searched_worst_rate": 0.24731182795698925,
   "searched_worst_ci": [
    0.18710910342392456,
    0.3157538618389775
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 97,
   "n_failed": 75,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.776,
   "ci": [
    0.6927850295430973,
    0.8456980662949503
   ],
   "worst_case_rate": 0.485,
   "worst_case_ci": [
    0.4139148296150438,
    0.5565363642507837
   ],
   "tiers": {
    "T1": 118,
    "invalid": 74,
    "T0": 8
   },
   "wall_s": 394.5,
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
     "L=1": 98,
     "L=3": 97,
     "L=7": 93
    },
    "n_invalid_null_model": 74,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 2,
     "cross_instrument": 72,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=3": 17,
     "L=1": 95,
     "L=7": 14
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 103,
   "label": "δ=0.2 noise×1.0 clusters=2400",
   "n_T0": 8,
   "n_searched": 192,
   "searched_worst_rate": 0.5052083333333334,
   "searched_worst_ci": [
    0.43228517097921704,
    0.5779682094268666
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 80,
   "n_failed": 63,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.583941605839416,
   "ci": [
    0.49669018958835426,
    0.6674835885496682
   ],
   "worst_case_rate": 0.4,
   "worst_case_ci": [
    0.33154626373563956,
    0.47146432720295395
   ],
   "tiers": {
    "T0": 50,
    "T1": 95,
    "invalid": 55
   },
   "wall_s": 221.0,
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
     "L=1": 89,
     "L=3": 86,
     "L=7": 80
    },
    "n_invalid_null_model": 55,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 2,
     "cross_instrument": 53,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=7": 74,
     "L=3": 64,
     "L=1": 7
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 84,
   "label": "δ=0.2 noise×0.6 clusters=1600",
   "n_T0": 50,
   "n_searched": 150,
   "searched_worst_rate": 0.5333333333333333,
   "searched_worst_ci": [
    0.450194463860646,
    0.6151295176294745
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 145,
   "n_failed": 29,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.847953216374269,
   "ci": [
    0.7852060839198709,
    0.8982102798416404
   ],
   "worst_case_rate": 0.725,
   "worst_case_ci": [
    0.6575745813762495,
    0.7856225847569046
   ],
   "tiers": {
    "T1": 149,
    "T0": 24,
    "invalid": 27
   },
   "wall_s": 500.7,
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
     "L=1": 147,
     "L=3": 147,
     "L=7": 145
    },
    "n_invalid_null_model": 27,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 2,
     "cross_instrument": 25,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=3": 33,
     "L=1": 105,
     "L=7": 35
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 144,
   "label": "δ=0.2 noise×0.6 clusters=2400",
   "n_T0": 24,
   "n_searched": 176,
   "searched_worst_rate": 0.8238636363636364,
   "searched_worst_ci": [
    0.7594038864267543,
    0.8770752800769427
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 71,
   "n_failed": 45,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.45806451612903226,
   "ci": [
    0.37788574694982624,
    0.5398772785864914
   ],
   "worst_case_rate": 0.355,
   "worst_case_ci": [
    0.28878377771358443,
    0.4255861669265797
   ],
   "tiers": {
    "T0": 81,
    "T1": 82,
    "invalid": 37
   },
   "wall_s": 234.3,
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
     "L=1": 80,
     "L=3": 78,
     "L=7": 71
    },
    "n_invalid_null_model": 37,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 3,
     "cross_instrument": 34,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=7": 101,
     "L=3": 61,
     "L=1": 1
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 72,
   "label": "δ=0.4 noise×1.0 clusters=1600",
   "n_T0": 81,
   "n_searched": 119,
   "searched_worst_rate": 0.5966386554621849,
   "searched_worst_ci": [
    0.5028050366309889,
    0.6855438285029896
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 129,
   "n_failed": 16,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.7010869565217391,
   "ci": [
    0.6293508556307363,
    0.7662133478921368
   ],
   "worst_case_rate": 0.645,
   "worst_case_ci": [
    0.5744138330734203,
    0.7112162222864156
   ],
   "tiers": {
    "T1": 133,
    "T0": 52,
    "invalid": 15
   },
   "wall_s": 480.7,
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
     "L=1": 131,
     "L=3": 131,
     "L=7": 129
    },
    "n_invalid_null_model": 15,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 2,
     "cross_instrument": 13,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=1": 94,
     "L=3": 32,
     "L=7": 59
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 129,
   "label": "δ=0.4 noise×1.0 clusters=2400",
   "n_T0": 52,
   "n_searched": 148,
   "searched_worst_rate": 0.8716216216216216,
   "searched_worst_ci": [
    0.8067999120002185,
    0.9209059469192896
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 26,
   "n_failed": 23,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.14689265536723164,
   "ci": [
    0.09825360520296568,
    0.20777836075501718
   ],
   "worst_case_rate": 0.13,
   "worst_case_ci": [
    0.0867083527685753,
    0.18465233052341257
   ],
   "tiers": {
    "T0": 150,
    "T1": 27,
    "invalid": 23
   },
   "wall_s": 221.5,
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
     "L=1": 26,
     "L=3": 26,
     "L=7": 26
    },
    "n_invalid_null_model": 23,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 23,
     "cross_instrument": 0,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=7": 146,
     "L=3": 31
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 20,
   "label": "δ=0.4 noise×0.6 clusters=1600",
   "n_T0": 150,
   "n_searched": 50,
   "searched_worst_rate": 0.52,
   "searched_worst_ci": [
    0.3741519450790943,
    0.66339490839654
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 48,
   "n_failed": 34,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.2891566265060241,
   "ci": [
    0.22152108853613184,
    0.3644910205062452
   ],
   "worst_case_rate": 0.24,
   "worst_case_ci": [
    0.1825719038924727,
    0.30530628564713935
   ],
   "tiers": {
    "T1": 48,
    "T0": 118,
    "invalid": 34
   },
   "wall_s": 389.4,
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
     "L=1": 48,
     "L=3": 48,
     "L=7": 48
    },
    "n_invalid_null_model": 34,
    "guard_failures_by_check": {
     "cluster_layout": 0,
     "missing_layout": 0,
     "block_ac1": 33,
     "cross_instrument": 1,
     "finite_diagnostics": 0,
     "icc": 0,
     "scale": 0,
     "tail": 0,
     "missing": 0
    },
    "all_T0": false,
    "chosen_block_len": {
     "L=1": 19,
     "L=3": 43,
     "L=7": 104
    }
   },
   "verdict": "invalid_null_model",
   "n_recovered": 45,
   "label": "δ=0.4 noise×0.6 clusters=2400",
   "n_T0": 118,
   "n_searched": 82,
   "searched_worst_rate": 0.5853658536585366,
   "searched_worst_ci": [
    0.4712057994534928,
    0.6931739258916321
   ]
  }
 ]
}
```
