# report-G3-null-model：空模型 FPR / 功效验收（合成 T1 规模，synthetic_validation）

生成本报告的研究代码 sha256：`2f3650b566840d7ee5acab1e30198dfca07bc9c80d7b37397573ee596e2317ad`（A31：制品须由不旧于门代码的版本生成，R-08 verify 机械比对）。

日期：2026-09-12 01:38 UTC。状态：合成数据实测；**claim_status = descriptive_only**（G-STAT-CLAIM pending），本报告不构成任何研究优势声明，也不替代真实数据前的空模型验收。
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
| common_shock | 1000 | 1000 | 1 | 2 | 0.20% | [0.02%, 0.72%] | 0.30% / [0.06%, 0.87%] | 842 / 0.36% / [0.07%, 1.04%] | 3 / 3 / 3（主 L 分布 {"L=7": 325, "L=1": 281, "L=3": 394}） | {"T0": 158, "T1": 842} | 1753.7 | **pass** |
| cluster_heavy_tail | 1000 | 1000 | 3 | 6 | 0.60% | [0.22%, 1.31%] | 0.90% / [0.41%, 1.70%] | 974 / 0.92% / [0.42%, 1.75%] | 7 / 6 / 6（主 L 分布 {"L=3": 505, "L=7": 451, "L=1": 44}） | {"T1": 974, "T0": 26} | 1798.3 | **pass** |
| nonuniform_density | 1000 | 1000 | 11 | 8 | 0.81% | [0.35%, 1.59%] | 1.90% / [1.15%, 2.95%] | 725 / 2.62% / [1.59%, 4.06%] | 9 / 6 / 8（主 L 分布 {"L=1": 786, "L=3": 119, "L=7": 95}） | {"T1": 725, "T0": 275} | 1882.6 | **pass** |
| circular_shift | 1000 | 1000 | 0 | 4 | 0.40% | [0.11%, 1.02%] | 0.40% / [0.11%, 1.02%] | 993 / 0.40% / [0.11%, 1.03%] | 4 / 4 / 6（主 L 分布 {"L=1": 980, "L=7": 14, "L=3": 6}） | {"T1": 993, "T0": 7} | 1846.8 | **pass** |

- 最坏机制 FPR CI 上界：2.95%（阈值 7%）。所有机制均须过门，不混池稀释。
- T0 replicate（基线 DEFF 降档 → cap=0 无搜索）必然 no_claim，不作 FPR 证据；验收以「有搜索 replicate」条件最坏界为准，并要求其占多数且 ≥ 500 次。

## 3. 功效（注入 δ）

| 机制 | 计划 n | 完成 | 失败 | 检出 x | 功效点估计 | 功效 95% CI | 最坏界（失败计未检出）功效 / CI | 规则找回率 | base_R sd（噪声） | 耗时 s | 判定 |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---|
| common_shock | 1000 | 1000 | 34 | 327 | 33.85% | [30.87%, 36.93%] | 32.70% / [29.80%, 35.71%]（有搜索 768：42.58% / [39.05%, 46.16%]） | 42.4% | 1.77 | 1781.5 | **fail** |

- 功效未达标时按 D.5「未达标限制声明或扩新样本」处理：本档（T1，约 1600 簇 / 4013 机会，噪声 sd≈1.77R）对 δ=0.2R 的全流程功效见上表；§3.1 给出效应/噪声/样本规模的适用范围。流程的功效瓶颈在 selection（内层 max-t 只见训练窗一半样本）。

## 3.2 限制声明（用户裁定出口 A，2026-09-11；规范性，随本报告发布）

> 在 T1 档合成规模（约 1600 经济簇）下，本协议对每种子 0.2R 的真实过滤增益的全流程检出功效约 33.9%（条件于实际进入搜索的 replicate 为 42.6%，95% 最坏界下界约 29.8%）。因此：
> 1. 本协议在该规模下**不能**把「未检出」解释为「无增益」；未检出只描述为「在该功效下未检出」。
> 2. 任何 P2 真实数据研究若样本规模/噪声与该档相当，只能对更大量级的效应或更大样本作检出声明；本档效应量需预注册扩样本后另验。
> 3. 上述数字均为**合成**世界预注册假设下的结果，**不是真实**频道数据的功效；真实功效须在 G1/G2 真实接缝就绪后按实测 K/DEFF 重估。
> 4. FPR 结论不受功效不足影响：假阳性控制按上表逐机制判定。

GOAL-3 §7 的 P1 DoD「功效 ≥ 80%」按用户裁定改为「功效报告 + 本限制声明」，原条件未达标的记录保留于 §3。

### 3.1 功效敏感性（效应 / 噪声 / 样本规模；每格 n=200，非验收，只描述阈值适用范围）

| 设定 | 机制 | n | 检出 | 功效 | 95% CI | 规则找回率（植入候选被选中 ≥1 折） | base_R sd | 簇数 | 档分布 |
|---|---|---:|---:|---:|---|---:|---:|---:|---|
| δ=0.2 noise×1.0 clusters=1600 | common_shock | 200 | 96 | 50.0% | [42.72%, 57.28%] | 56.5% | 1.75 | 1600 | {"T1": 173, "T0": 27} |
| δ=0.2 noise×1.0 clusters=2400 | common_shock | 200 | 146 | 73.7% | [67.03%, 79.72%] | 82.0% | 1.75 | 2400 | {"T1": 190, "T0": 10} |
| δ=0.2 noise×0.6 clusters=1600 | common_shock | 200 | 115 | 60.2% | [52.89%, 67.20%] | 59.5% | 1.05 | 1600 | {"T0": 65, "T1": 135} |
| δ=0.2 noise×0.6 clusters=2400 | common_shock | 200 | 169 | 85.4% | [79.65%, 89.97%] | 83.5% | 1.05 | 2400 | {"T1": 173, "T0": 27} |
| δ=0.4 noise×1.0 clusters=1600 | common_shock | 200 | 94 | 49.0% | [41.69%, 56.26%] | 47.0% | 1.75 | 1600 | {"T0": 93, "T1": 107} |
| δ=0.4 noise×1.0 clusters=2400 | common_shock | 200 | 141 | 70.9% | [64.01%, 77.07%] | 69.5% | 1.75 | 2400 | {"T1": 145, "T0": 55} |
| δ=0.4 noise×0.6 clusters=1600 | common_shock | 200 | 27 | 13.5% | [9.09%, 19.03%] | 10.5% | 1.05 | 1600 | {"T0": 172, "T1": 28} |
| δ=0.4 noise×0.6 clusters=2400 | common_shock | 200 | 50 | 25.1% | [19.26%, 31.75%] | 23.5% | 1.05 | 2400 | {"T1": 51, "T0": 149} |

## 4. 空模型诊断（块内相关是否保留）

| 机制 | 训练窗天数 / 观测 | 块均值 lag-1 自相关 | 跨品种格点相关 | 块 ICC orig / null / 逐 episode 洗牌 | 逐 replicate 门 | 格点总体门：拟合 → null 均值（判定依据） |
|---|---|---:|---:|---|---|---|
| common_shock | 180 / 1859（未成熟排除 24） | 0.055 | 0.491 | 0.1481 / 0.2307 / 0.0038 | 失败 0 / 1000 = 0.00%（带 ≤ 5%） | 0.559→0.559（|Δ|=0.0000 vs 带 0.0300）、0.504→0.510（|Δ|=0.0060 vs 带 0.0300）、0.424→0.430（|Δ|=0.0057 vs 带 0.0300） → **True**（invalid_reason=None） |
| cluster_heavy_tail | 180 / 1909（未成熟排除 8） | 0.021 | 0.119 | 0.0721 / 0.1637 / -0.0026 | 失败 0 / 1000 = 0.00%（带 ≤ 5%） | 0.139→0.141（|Δ|=0.0021 vs 带 0.0300）、0.177→0.181（|Δ|=0.0041 vs 带 0.0300）、0.180→0.182（|Δ|=0.0021 vs 带 0.0300） → **True**（invalid_reason=None） |
| nonuniform_density | 180 / 2096（未成熟排除 27） | -0.020 | 0.446 | 0.1754 / 0.1808 / -0.0019 | 失败 0 / 1000 = 0.00%（带 ≤ 5%） | 0.337→0.334（|Δ|=0.0035 vs 带 0.0300）、0.516→0.512（|Δ|=0.0042 vs 带 0.0300）、0.340→0.342（|Δ|=0.0012 vs 带 0.0300） → **True**（invalid_reason=None） |
| circular_shift | 180 / 1812（未成熟排除 11） | 0.174 | 0.549 | 0.1924 / 0.2041 / -0.0016 | 失败 0 / 1000 = 0.00%（带 ≤ 5%） | 0.552→0.552（|Δ|=0.0000 vs 带 0.0300）、0.573→0.573（|Δ|=0.0000 vs 带 0.0300）、0.521→0.521（|Δ|=0.0000 vs 带 0.0300） → **True**（invalid_reason=None） |

总体门的带宽 = max(6×SE(均值), 0.03), SE=sd/√n_grid（n_grid 为参与统计的 replicate 数）。**为什么不是绝对容差**（R5-W）：绝对容差把 0.14–0.18 这类真实但弱的拟合相关整体划进「允许归零」区，结构被完全摧毁也不报。校准依据（4 机制 × 200 次重采样实测）：正常重采样 |Δ| 最大 0.0096、|Δ|/SE 最大 1.8；整块重排摧毁跨品种结构后 |Δ| 最小 0.1379（弱相关机制 cluster_heavy_tail）。地板 0.03 两侧各留约 3 倍余量；SE 项只在样本少时**放宽**带宽——样本不足时本就无从区分，宁可不报也不制造假警报。

**两个门，口径不同，不得混称**（R4-V）：

1. **逐 replicate 结构门**（冲击格点上的 Fisher-z 判据）有设计误拒率，按校准带约 1–2%。单次失败的 replicate 直接计失败，并按保守方向进最坏界（空机制计阳性、功效计未检出），**但不单独使整轮判废**——那等于拿估计噪声当结构破坏。只有失败率 > 5%（远超设计误拒率）才判 invalid_null_model。
2. **格点总体门**（上表末列）比较拟合格点与全 replicate 均值的跨品种相关：均值比单次稳，是真正的判定依据。它失败即整轮 invalid_null_model，与逐 replicate 失败次数是否为零无关。

R-08 的机器判读（verify_report_text）用**同一套规则从原始诊断重算**这两条，并要求 `grid_dependence.ok` / `dependence_aggregate.ok` / `invalid_reason` / `verdict` / `tiers.invalid` / `n_invalid_null_model` / `guard_fail_rate` / `guard_failures_by_check` 八处彼此自洽——单改任意一处即被拒收。

**带宽的分母与 sd 都不是自由参数**（R6-G）：带宽用**逐对**有效样本数 `n_cross`（某对不可估时它小于 `n_grid`，拿共同 n_grid 当分母会把带宽算窄）；相关系数逐次取值恒在 [-1,1]，故样本 sd 有硬上界 精确上界（见 `max_realizable_sd`）——超界的 sd 不可能由任何合法抽样产生，判读按该上界拒收，`band` 也必须能由 (fitted, mean, sd, n_cross) 重算出来。这条不属溯源边界：不必相信 fitted 是真值、也不必重跑 MC 就能否证。用的是**精确**上界而非 `n/(n−1)·(1−m²)`：后者只是必要条件，有限 n 时未必可达（n=2、m=−0.9 时它给 0.616，真实上界是 0.141，R7-G）。

**逐对缺测必须在总账里出现**（R7-G）：fitted 有限时，某次该对不可估会让逐 replicate 的 `cross_instrument` 判据为假、该次计 invalid。因此「`n_cross` 远小于 `n_grid`」与「`cross_instrument` 零失败」不能同时成立，判读强制 `max(n_grid − n_cross) ≤ guard_failures_by_check['cross_instrument'] ≤ n_invalid`。否则把某对的有效样本数报成 2，就能用一个**本身可实现**的 sd 把带宽撑开。

**主 L 分布显式声明模式**（R6-L）：`block_len_mode` 为 auto 时必须给出合法分布并无条件核上下界；为 fixed 时必须声明固定 L 且不给分布。原先「空字典」既表示固定 L、又能表示分布被清空，于是清空即可跳过整个计数门。

另有三条记账约束（R5-C / R5-G / R5-O）：**(a)** `tiers.invalid + tiers.error ≤ n_failed`，且主 L 分布只覆盖真正进入流水线的 replicate——结构早退者不该有主 L；结构失败必须真正计进失败数，否则三分母最坏界会被低估到足以翻转 7% 准入。**(b)** 跨品种相关向量按冻结世界核对齐全集与顺序（3 品种恰好 3 对，见上表 `pairs`），取值须为有限实数且落在 [-1,1]，缺项/重复/越界一律拒收。**(c)** 上表展示的 `shuffle_guard` 只是第一个 replicate 的快照，**不是**「首个必须零失败」的门；它失败时必须在 per-check 与 invalid 总量里有对应记账。

生成身份（A31 / R5-H）：MC 起跑冻结父进程源码摘要，**每个 worker 另行回传自己起跑与收尾的摘要**，父进程逐份核对，任何不等或缺回执都拒绝落盘。本报告的 worker 回执数：13，回执摘要集合：['2f3650b566840d7ee5acab1e30198dfca07bc9c80d7b37397573ee596e2317ad']。**两种身份都要核**（R6-H）：磁盘摘要只回答「文件长什么样」，回答不了「这个 worker 实际跑的是哪一版」——导入缓存或驻留修订能让两者背离。因此每个 worker 另报一份**执行修订**摘要：对本包全部模块命名空间里函数/方法的 code object（含常量）取摘要，覆盖集合由包本身决定而非「当前恰好加载了哪些」。实测：两个真实 worker 装上只改返回标签的驻留修订后，磁盘摘要全等而执行摘要不同，父进程拒绝发布。本报告的父进程执行修订：d21302bd0549c1c7…，worker 执行修订集合：['d21302bd0549c1c7…']。

执行摘要覆盖的不只是字节码（R7-H）：函数的 `__defaults__` / `__kwdefaults__` / 闭包 cell 与模块级常量都在定义时绑定，改掉它们可以在**函数体字节码完全不变**的前提下改变实际生成的数据（例如把 `_garch_path` 的默认 omega 从 0.1 改成 0.5），驻留旧函数与当前磁盘可以同时存在。故摘要连同默认值、闭包与 UPPER_CASE 模块常量一起取；数据结构里装着的本包函数按其 code 与默认值编码，以覆盖「外层 wrapper 委派到未被遍历的函数对象」。

**仍未实现的边界**：未做到「从同一份只读源码快照/安装制品以新解释器启动整组 worker」——当前是**检出**执行修订不一致并拒绝发布，而不是从源头消除混版可能。父进程异常退出时不落盘，代价是丢弃 worker 已算结果。

全部落 T0 的机制标 not_run_T0（cap=0 无搜索，FPR 平凡为 0，不作 T1 验收替身）。

## 5. 总判定（synthetic_validation）

- FPR：common_shock=pass、cluster_heavy_tail=pass、nonuniform_density=pass、circular_shift=pass；扩展：无。
- 功效（δ=0.2）：common_shock=fail。
- 全部结果只描述；任何档位的真实数据声明仍需 G-STAT-CLAIM。

## 6. 资源与限制

- 总耗时 13607s；单 replicate 均值 2.062s；峰值 RSS 见 report.json。
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
  "pi": 0.3,
  "pipeline_block_len_days": null,
  "worker_receipts_confirmed": 13,
  "worker_code_sha256": [
   "2f3650b566840d7ee5acab1e30198dfca07bc9c80d7b37397573ee596e2317ad"
  ],
  "worker_exec_revision": [
   "d21302bd0549c1c7766f8219ef3ba30fb5d14558438108ecf1e42eeab10aa6cb"
  ],
  "parent_exec_revision": "d21302bd0549c1c7766f8219ef3ba30fb5d14558438108ecf1e42eeab10aa6cb",
  "research_code_sha256": "2f3650b566840d7ee5acab1e30198dfca07bc9c80d7b37397573ee596e2317ad"
 },
 "results": [
  {
   "mechanism": "common_shock",
   "kind": "null",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 2,
   "n_failed": 1,
   "seeds": [
    100000,
    100001,
    100002,
    100003,
    100004
   ],
   "rate": 0.002002002002002002,
   "ci": [
    0.00024254375262666952,
    0.007213033101571868
   ],
   "worst_case_rate": 0.003,
   "worst_case_ci": [
    0.0006190999316495711,
    0.008742023238478303
   ],
   "tiers": {
    "T0": 158,
    "T1": 842
   },
   "wall_s": 1753.7,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.054733956017369176,
       "null": 0.08334326004323793
      },
      "cross_instrument": {
       "fitted": [
        0.5589960708759241,
        0.5040997763236431,
        0.4244275959833148
       ],
       "null": [
        0.571275005327457,
        0.5325821463547059,
        0.5052412934514777
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 3,
     "L=3": 3,
     "L=7": 3
    },
    "grid_dependence": {
     "fitted_ac1": 0.054733956017369176,
     "null_mean_ac1": -0.011992350420685518,
     "fitted_cross": [
      0.5589960708759241,
      0.5040997763236431,
      0.4244275959833148
     ],
     "null_mean_cross": [
      0.5590458605494382,
      0.5100828575540027,
      0.4300975486850363
     ],
     "null_sd_cross": [
      0.05638996734315003,
      0.05104859617700653,
      0.07345864594316859
     ],
     "n_grid": 1000,
     "n_cross": [
      1000,
      1000,
      1000
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.03,
      0.03
     ],
     "ok": true
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
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=7": 325,
     "L=1": 281,
     "L=3": 394
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "pass",
   "n_recovered": 15,
   "label": "",
   "n_T0": 158,
   "n_searched": 842,
   "searched_worst_rate": 0.0035629453681710215,
   "searched_worst_ci": [
    0.0007353685559325966,
    0.010376831619745201
   ]
  },
  {
   "mechanism": "cluster_heavy_tail",
   "kind": "null",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 6,
   "n_failed": 3,
   "seeds": [
    200000,
    200001,
    200002,
    200003,
    200004
   ],
   "rate": 0.006018054162487462,
   "ci": [
    0.0022116266809733224,
    0.013052442167313738
   ],
   "worst_case_rate": 0.009,
   "worst_case_ci": [
    0.004123395660342472,
    0.017015783069894586
   ],
   "tiers": {
    "T1": 974,
    "T0": 26
   },
   "wall_s": 1798.3,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.020652651142966244,
       "null": -0.060873708045252134
      },
      "cross_instrument": {
       "fitted": [
        0.1392838284143582,
        0.1769549842158233,
        0.17954215469804016
       ],
       "null": [
        0.138125740208326,
        0.2541223825753843,
        0.18570438482826798
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 7,
     "L=3": 6,
     "L=7": 6
    },
    "grid_dependence": {
     "fitted_ac1": 0.020652651142966244,
     "null_mean_ac1": -0.004850899315197338,
     "fitted_cross": [
      0.1392838284143582,
      0.1769549842158233,
      0.17954215469804016
     ],
     "null_mean_cross": [
      0.14141717868779494,
      0.18106912945144757,
      0.18167559855509102
     ],
     "null_sd_cross": [
      0.10860894900728148,
      0.09016279665122696,
      0.08372988502670506
     ],
     "n_grid": 1000,
     "n_cross": [
      1000,
      1000,
      1000
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.03,
      0.03
     ],
     "ok": true
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
     "ok": true,
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=3": 505,
     "L=7": 451,
     "L=1": 44
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "pass",
   "n_recovered": 3,
   "label": "",
   "n_T0": 26,
   "n_searched": 974,
   "searched_worst_rate": 0.009240246406570842,
   "searched_worst_ci": [
    0.004233686467841858,
    0.017468112745586222
   ]
  },
  {
   "mechanism": "nonuniform_density",
   "kind": "null",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 8,
   "n_failed": 11,
   "seeds": [
    300000,
    300001,
    300002,
    300003,
    300004
   ],
   "rate": 0.008088978766430738,
   "ci": [
    0.0034985285413397893,
    0.01587600573645218
   ],
   "worst_case_rate": 0.019,
   "worst_case_ci": [
    0.011477036993100946,
    0.0295124016250978
   ],
   "tiers": {
    "T1": 725,
    "T0": 275
   },
   "wall_s": 1882.6,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": -0.019745157286099397,
       "null": 0.008171234273067922
      },
      "cross_instrument": {
       "fitted": [
        0.33724128941781295,
        0.5164425518064831,
        0.34035073268047855
       ],
       "null": [
        0.457065551745459,
        0.6066121064242199,
        0.4714263566398253
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 9,
     "L=3": 6,
     "L=7": 8
    },
    "grid_dependence": {
     "fitted_ac1": -0.019745157286099397,
     "null_mean_ac1": -0.007054208727233611,
     "fitted_cross": [
      0.33724128941781295,
      0.5164425518064831,
      0.34035073268047855
     ],
     "null_mean_cross": [
      0.3337143972242893,
      0.5122603377169962,
      0.34154699478303313
     ],
     "null_sd_cross": [
      0.08716914130817602,
      0.07253579969035054,
      0.08511056440215932
     ],
     "n_grid": 1000,
     "n_cross": [
      1000,
      1000,
      1000
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.03,
      0.03
     ],
     "ok": true
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
     "ok": true,
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=1": 786,
     "L=3": 119,
     "L=7": 95
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "pass",
   "n_recovered": 10,
   "label": "",
   "n_T0": 275,
   "n_searched": 725,
   "searched_worst_rate": 0.02620689655172414,
   "searched_worst_ci": [
    0.015850368253145182,
    0.0406237526445681
   ]
  },
  {
   "mechanism": "circular_shift",
   "kind": "null",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 4,
   "n_failed": 0,
   "seeds": [
    400000,
    400001,
    400002,
    400003,
    400004
   ],
   "rate": 0.004,
   "ci": [
    0.0010909079877259714,
    0.010209664683929871
   ],
   "worst_case_rate": 0.004,
   "worst_case_ci": [
    0.0010909079877259714,
    0.010209664683929871
   ],
   "tiers": {
    "T1": 993,
    "T0": 7
   },
   "wall_s": 1846.8,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.17428462643994658,
       "null": 0.1549201041716031
      },
      "cross_instrument": {
       "fitted": [
        0.5521197927365366,
        0.5726065396825362,
        0.5205994123655606
       ],
       "null": [
        0.5521197927365367,
        0.5726065396825363,
        0.5205994123655607
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 4,
     "L=3": 4,
     "L=7": 6
    },
    "grid_dependence": {
     "fitted_ac1": 0.17428462643994658,
     "null_mean_ac1": 0.15436523687938802,
     "fitted_cross": [
      0.5521197927365366,
      0.5726065396825362,
      0.5205994123655606
     ],
     "null_mean_cross": [
      0.552119792736549,
      0.5726065396825232,
      0.52059941236557
     ],
     "null_sd_cross": [
      1.2402288487165051e-14,
      1.3065800621902232e-14,
      9.265720940071938e-15
     ],
     "n_grid": 1000,
     "n_cross": [
      1000,
      1000,
      1000
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.03,
      0.03
     ],
     "ok": true
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
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=1": 980,
     "L=7": 14,
     "L=3": 6
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "pass",
   "n_recovered": 18,
   "label": "",
   "n_T0": 7,
   "n_searched": 993,
   "searched_worst_rate": 0.004028197381671702,
   "searched_worst_ci": [
    0.0010986055888245786,
    0.010281409777837296
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power",
   "n_planned": 1000,
   "n_done": 1000,
   "n_positive": 327,
   "n_failed": 34,
   "seeds": [
    1100000,
    1100001,
    1100002,
    1100003,
    1100004
   ],
   "rate": 0.3385093167701863,
   "ci": [
    0.30868189908801363,
    0.3693250230982214
   ],
   "worst_case_rate": 0.327,
   "worst_case_ci": [
    0.2979696876186015,
    0.3570528972764215
   ],
   "tiers": {
    "T0": 232,
    "T1": 768
   },
   "wall_s": 1781.5,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.002373027810941563,
       "null": -0.005696456628217498
      },
      "cross_instrument": {
       "fitted": [
        0.4546284333418333,
        0.448125738712611,
        0.36885154494765643
       ],
       "null": [
        0.43295915916072947,
        0.49545042092273023,
        0.444559610167991
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 361,
     "L=3": 348,
     "L=7": 304
    },
    "grid_dependence": {
     "fitted_ac1": 0.002373027810941563,
     "null_mean_ac1": -0.014308647211862213,
     "fitted_cross": [
      0.4546284333418333,
      0.448125738712611,
      0.36885154494765643
     ],
     "null_mean_cross": [
      0.45201122707362074,
      0.4438002196813772,
      0.36701276603025257
     ],
     "null_sd_cross": [
      0.07722236387616999,
      0.06876386926500107,
      0.08232641389276181
     ],
     "n_grid": 1000,
     "n_cross": [
      1000,
      1000,
      1000
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.03,
      0.03
     ],
     "ok": true
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
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=3": 470,
     "L=7": 483,
     "L=1": 47
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "fail",
   "n_recovered": 424,
   "label": "",
   "n_T0": 232,
   "n_searched": 768,
   "searched_worst_rate": 0.42578125,
   "searched_worst_ci": [
    0.39050274645009997,
    0.46163175773831505
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 96,
   "n_failed": 8,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.5,
   "ci": [
    0.4271546933550074,
    0.5728453066449926
   ],
   "worst_case_rate": 0.48,
   "worst_case_ci": [
    0.4090139996380063,
    0.5515876013394261
   ],
   "tiers": {
    "T1": 173,
    "T0": 27
   },
   "wall_s": 456.0,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.12755664694584193,
       "null": -0.09652976332140926
      },
      "cross_instrument": {
       "fitted": [
        0.5822643311603736,
        0.391312253515749,
        0.41776484020326843
       ],
       "null": [
        0.6875790188499749,
        0.5049977757522123,
        0.4689865878142934
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 100,
     "L=3": 101,
     "L=7": 94
    },
    "grid_dependence": {
     "fitted_ac1": 0.12755664694584193,
     "null_mean_ac1": -0.025040655584521912,
     "fitted_cross": [
      0.5822643311603736,
      0.391312253515749,
      0.41776484020326843
     ],
     "null_mean_cross": [
      0.5781474324386024,
      0.399396236668046,
      0.4200018123106667
     ],
     "null_sd_cross": [
      0.06562517989806455,
      0.07819750818969817,
      0.07430746539820018
     ],
     "n_grid": 200,
     "n_cross": [
      200,
      200,
      200
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.033176392987695695,
      0.03152598760551125
     ],
     "ok": true
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
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=3": 92,
     "L=7": 77,
     "L=1": 31
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "fail",
   "n_recovered": 113,
   "label": "δ=0.2 noise×1.0 clusters=1600",
   "n_T0": 27,
   "n_searched": 173,
   "searched_worst_rate": 0.5549132947976878,
   "searched_worst_ci": [
    0.47757047067463965,
    0.6303419974918443
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
   "wall_s": 821.0,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.09411843066890975,
       "null": 0.016234887742433663
      },
      "cross_instrument": {
       "fitted": [
        0.6467965389576839,
        0.5969020066483463,
        0.5896220642718007
       ],
       "null": [
        0.7502308777934231,
        0.5877433438033499,
        0.6352493134664698
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 149,
     "L=3": 147,
     "L=7": 141
    },
    "grid_dependence": {
     "fitted_ac1": 0.09411843066890975,
     "null_mean_ac1": -0.018646980133512694,
     "fitted_cross": [
      0.6467965389576839,
      0.5969020066483463,
      0.5896220642718007
     ],
     "null_mean_cross": [
      0.6517742726705825,
      0.5963489500887243,
      0.595685379714954
     ],
     "null_sd_cross": [
      0.050309915771327096,
      0.054622994989003006,
      0.04611744110495231
     ],
     "n_grid": 200,
     "n_cross": [
      200,
      200,
      200
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.03,
      0.03
     ],
     "ok": true
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
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
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
   "n_positive": 115,
   "n_failed": 9,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.6020942408376964,
   "ci": [
    0.528918260388816,
    0.6720494073404496
   ],
   "worst_case_rate": 0.575,
   "worst_case_ci": [
    0.5033041318348098,
    0.6444388227412944
   ],
   "tiers": {
    "T0": 65,
    "T1": 135
   },
   "wall_s": 423.5,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.12755664694584198,
       "null": -0.09652976332140926
      },
      "cross_instrument": {
       "fitted": [
        0.5822643311603736,
        0.391312253515749,
        0.4177648402032685
       ],
       "null": [
        0.687579018849975,
        0.5049977757522122,
        0.46898658781429337
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 125,
     "L=3": 122,
     "L=7": 114
    },
    "grid_dependence": {
     "fitted_ac1": 0.12755664694584198,
     "null_mean_ac1": -0.025040655584521912,
     "fitted_cross": [
      0.5822643311603736,
      0.391312253515749,
      0.4177648402032685
     ],
     "null_mean_cross": [
      0.5781474324386024,
      0.399396236668046,
      0.4200018123106667
     ],
     "null_sd_cross": [
      0.06562517989806453,
      0.07819750818969816,
      0.0743074653982002
     ],
     "n_grid": 200,
     "n_cross": [
      200,
      200,
      200
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.03317639298769569,
      0.03152598760551126
     ],
     "ok": true
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
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=7": 100,
     "L=3": 87,
     "L=1": 13
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "fail",
   "n_recovered": 119,
   "label": "δ=0.2 noise×0.6 clusters=1600",
   "n_T0": 65,
   "n_searched": 135,
   "searched_worst_rate": 0.8518518518518519,
   "searched_worst_ci": [
    0.7805102651580698,
    0.9070970565899277
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 169,
   "n_failed": 2,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.8535353535353535,
   "ci": [
    0.7964718365274299,
    0.8996645032760584
   ],
   "worst_case_rate": 0.845,
   "worst_case_ci": [
    0.787262356784294,
    0.8921905998959034
   ],
   "tiers": {
    "T1": 173,
    "T0": 27
   },
   "wall_s": 810.3,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.09411843066890979,
       "null": 0.016234887742433677
      },
      "cross_instrument": {
       "fitted": [
        0.646796538957684,
        0.5969020066483463,
        0.5896220642718009
       ],
       "null": [
        0.7502308777934231,
        0.5877433438033498,
        0.63524931346647
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 171,
     "L=3": 171,
     "L=7": 169
    },
    "grid_dependence": {
     "fitted_ac1": 0.09411843066890979,
     "null_mean_ac1": -0.018646980133512688,
     "fitted_cross": [
      0.646796538957684,
      0.5969020066483463,
      0.5896220642718009
     ],
     "null_mean_cross": [
      0.6517742726705825,
      0.5963489500887245,
      0.595685379714954
     ],
     "null_sd_cross": [
      0.050309915771327096,
      0.05462299498900298,
      0.046117441104952306
     ],
     "n_grid": 200,
     "n_cross": [
      200,
      200,
      200
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.03,
      0.03
     ],
     "ok": true
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
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=3": 36,
     "L=1": 123,
     "L=7": 41
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "insufficient",
   "n_recovered": 167,
   "label": "δ=0.2 noise×0.6 clusters=2400",
   "n_T0": 27,
   "n_searched": 173,
   "searched_worst_rate": 0.976878612716763,
   "searched_worst_ci": [
    0.9418607146767426,
    0.9936650860937843
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 94,
   "n_failed": 8,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.4895833333333333,
   "ci": [
    0.41691646503371776,
    0.5625767765045209
   ],
   "worst_case_rate": 0.47,
   "worst_case_ci": [
    0.39923294119711444,
    0.541669499588525
   ],
   "tiers": {
    "T0": 93,
    "T1": 107
   },
   "wall_s": 395.9,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.12755664694584193,
       "null": -0.09652976332140926
      },
      "cross_instrument": {
       "fitted": [
        0.5822643311603736,
        0.391312253515749,
        0.41776484020326843
       ],
       "null": [
        0.6875790188499749,
        0.5049977757522123,
        0.4689865878142934
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 103,
     "L=3": 101,
     "L=7": 94
    },
    "grid_dependence": {
     "fitted_ac1": 0.12755664694584193,
     "null_mean_ac1": -0.025040655584521912,
     "fitted_cross": [
      0.5822643311603736,
      0.391312253515749,
      0.41776484020326843
     ],
     "null_mean_cross": [
      0.5781474324386024,
      0.399396236668046,
      0.4200018123106667
     ],
     "null_sd_cross": [
      0.06562517989806455,
      0.07819750818969817,
      0.07430746539820018
     ],
     "n_grid": 200,
     "n_cross": [
      200,
      200,
      200
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.033176392987695695,
      0.03152598760551125
     ],
     "ok": true
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
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=7": 120,
     "L=3": 77,
     "L=1": 3
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "fail",
   "n_recovered": 94,
   "label": "δ=0.4 noise×1.0 clusters=1600",
   "n_T0": 93,
   "n_searched": 107,
   "searched_worst_rate": 0.8785046728971962,
   "searched_worst_ci": [
    0.801202872953096,
    0.9336957874524169
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 141,
   "n_failed": 1,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.7085427135678392,
   "ci": [
    0.6401018441199158,
    0.7706514302505901
   ],
   "worst_case_rate": 0.705,
   "worst_case_ci": [
    0.6365768666820897,
    0.7672312157768753
   ],
   "tiers": {
    "T1": 145,
    "T0": 55
   },
   "wall_s": 695.8,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.09411843066890975,
       "null": 0.016234887742433663
      },
      "cross_instrument": {
       "fitted": [
        0.6467965389576839,
        0.5969020066483463,
        0.5896220642718007
       ],
       "null": [
        0.7502308777934231,
        0.5877433438033499,
        0.6352493134664698
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 143,
     "L=3": 143,
     "L=7": 141
    },
    "grid_dependence": {
     "fitted_ac1": 0.09411843066890975,
     "null_mean_ac1": -0.018646980133512694,
     "fitted_cross": [
      0.6467965389576839,
      0.5969020066483463,
      0.5896220642718007
     ],
     "null_mean_cross": [
      0.6517742726705825,
      0.5963489500887243,
      0.595685379714954
     ],
     "null_sd_cross": [
      0.050309915771327096,
      0.054622994989003006,
      0.04611744110495231
     ],
     "n_grid": 200,
     "n_cross": [
      200,
      200,
      200
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.03,
      0.03
     ],
     "ok": true
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
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=1": 102,
     "L=3": 33,
     "L=7": 65
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "fail",
   "n_recovered": 139,
   "label": "δ=0.4 noise×1.0 clusters=2400",
   "n_T0": 55,
   "n_searched": 145,
   "searched_worst_rate": 0.9724137931034482,
   "searched_worst_ci": [
    0.9308762019977,
    0.9924336089215673
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 27,
   "n_failed": 0,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.135,
   "ci": [
    0.09088710175489777,
    0.19030917268339836
   ],
   "worst_case_rate": 0.135,
   "worst_case_ci": [
    0.09088710175489777,
    0.19030917268339836
   ],
   "tiers": {
    "T0": 172,
    "T1": 28
   },
   "wall_s": 326.0,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.12755664694584198,
       "null": -0.09652976332140926
      },
      "cross_instrument": {
       "fitted": [
        0.5822643311603736,
        0.391312253515749,
        0.4177648402032685
       ],
       "null": [
        0.687579018849975,
        0.5049977757522122,
        0.46898658781429337
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 27,
     "L=3": 27,
     "L=7": 27
    },
    "grid_dependence": {
     "fitted_ac1": 0.12755664694584198,
     "null_mean_ac1": -0.025040655584521912,
     "fitted_cross": [
      0.5822643311603736,
      0.391312253515749,
      0.4177648402032685
     ],
     "null_mean_cross": [
      0.5781474324386024,
      0.399396236668046,
      0.4200018123106667
     ],
     "null_sd_cross": [
      0.06562517989806453,
      0.07819750818969816,
      0.0743074653982002
     ],
     "n_grid": 200,
     "n_cross": [
      200,
      200,
      200
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.03317639298769569,
      0.03152598760551126
     ],
     "ok": true
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
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=7": 169,
     "L=3": 31
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "fail",
   "n_recovered": 21,
   "label": "δ=0.4 noise×0.6 clusters=1600",
   "n_T0": 172,
   "n_searched": 28,
   "searched_worst_rate": 0.9642857142857143,
   "searched_worst_ci": [
    0.8165224024553763,
    0.999096201244342
   ]
  },
  {
   "mechanism": "common_shock",
   "kind": "power_sens",
   "n_planned": 200,
   "n_done": 200,
   "n_positive": 50,
   "n_failed": 1,
   "seeds": [
    4100000,
    4100001,
    4100002,
    4100003,
    4100004
   ],
   "rate": 0.25125628140703515,
   "ci": [
    0.19260374658281726,
    0.31747866572672995
   ],
   "worst_case_rate": 0.25,
   "worst_case_ci": [
    0.19160716962258745,
    0.315962833329869
   ],
   "tiers": {
    "T1": 51,
    "T0": 149
   },
   "wall_s": 615.8,
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
     "grid": {
      "used": true,
      "ac1": {
       "fitted": 0.09411843066890979,
       "null": 0.016234887742433677
      },
      "cross_instrument": {
       "fitted": [
        0.646796538957684,
        0.5969020066483463,
        0.5896220642718009
       ],
       "null": [
        0.7502308777934231,
        0.5877433438033498,
        0.63524931346647
       ]
      }
     },
     "ok": true,
     "sample_index": 0
    },
    "positive_by_block_len": {
     "L=1": 51,
     "L=3": 51,
     "L=7": 50
    },
    "grid_dependence": {
     "fitted_ac1": 0.09411843066890979,
     "null_mean_ac1": -0.018646980133512688,
     "fitted_cross": [
      0.646796538957684,
      0.5969020066483463,
      0.5896220642718009
     ],
     "null_mean_cross": [
      0.6517742726705825,
      0.5963489500887245,
      0.595685379714954
     ],
     "null_sd_cross": [
      0.050309915771327096,
      0.05462299498900298,
      0.046117441104952306
     ],
     "n_grid": 200,
     "n_cross": [
      200,
      200,
      200
     ],
     "pairs": [
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "ETHUSDT-PERP.BINANCE-UM"
      ],
      [
       "BTCUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ],
      [
       "ETHUSDT-PERP.BINANCE-UM",
       "SOLUSDT-PERP.BINANCE-UM"
      ]
     ],
     "band": [
      0.03,
      0.03,
      0.03
     ],
     "ok": true
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
     "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"
    },
    "guard_fail_rate": 0.0,
    "invalid_reason": null,
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
     "L=1": 20,
     "L=3": 44,
     "L=7": 136
    },
    "block_len_mode": "auto",
    "block_len_fixed": null
   },
   "verdict": "fail",
   "n_recovered": 47,
   "label": "δ=0.4 noise×0.6 clusters=2400",
   "n_T0": 149,
   "n_searched": 51,
   "searched_worst_rate": 0.9803921568627451,
   "searched_worst_ci": [
    0.8955251036044829,
    0.9995036955922623
   ]
  }
 ]
}
```
