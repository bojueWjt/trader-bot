# 交易员风格刻画：指标特征库 / 研究协议 / 小样本统计工具 —— 第 1 轮候选广度收集

> 说明：本轮只铺候选、不下结论。所有事实标注 `[来源, URL, 日期]` 与可信度（高=官方文档/源码/PyPI；中=知名技术博客/论文；低=论坛/Issue 评论）。检索日期 2026-09-06/07。"stars" 为 GitHub 页面抓取值，属于快照。

---

## 1. 指标特征库

| 候选 | 定位 | 成熟度信号 | 关键能力对照需求 | 官方文档 | 初步疑虑 |
|---|---|---|---|---|---|
| **TA-Lib (ta-lib-python)** | C 库的 Python 绑定，行业基准实现 | 0.7.1，2026-07-16；BSD-2；官方 wheel 覆盖 Linux/macOS/Windows、Py3.9–3.14（自 0.6.5 起）`[TA-Lib PyPI, https://pypi.org/project/TA-Lib/, 2026-07]` 高 | 全套趋势/动量/波动指标（EMA/ADX/RSI/MACD/ATR/BBANDS）；纯因果滚动计算，天然只用历史窗口；输入 numpy 数组，不管周期对齐，需自备重采样 | https://github.com/ta-lib/ta-lib-python | 早年"安装难"的痛点在 2025-08 后基本消失（官方 wheel）；无资金费率/OI 概念，非价格特征需自己拼；无 DataFrame 原生 API |
| **pandas-ta (twopirllc / pandas-ta.dev)** | 曾是最流行的 pandas 扩展指标库 | PyPI 最新 0.4.71b0 预发布，2025-09-14，要求 Py≥3.12 `[pandas-ta PyPI, https://pypi.org/project/pandas-ta/, 2025-09]` 高；原 GitHub 仓库已移除，2025-07-01 之后"企业付费、其他人订阅制"，"若 2026-07-01 前无足够支持将归档" `[MerlinR/Pandas-ta-fork README, https://github.com/MerlinR/Pandas-ta-fork, 2025]` 中；社区疑虑："PyPI 历史被清空""像供应链攻击" `[pandas-ta-classic issue #30, https://github.com/xgboosted/pandas-ta-classic/issues/30, 2025-09-17]` 低 | 130+ 指标、`df.ta.strategy()` 批量算；有 VWAP、供需/结构类指标 | https://www.pandas-ta.dev/api/ | **许可证与维护方向已变**（license 页面两次抓取失败，具体条款未核实）；PyPI 历史被清、维护者更换、"只看 2025 后信息"视角下风险最高的候选 |
| **pandas-ta-classic** | 社区接管的 pandas-ta 分叉 | 0.6.52，2026-06-24；MIT；193 指标 + 62 蜡烛形态，核心不依赖 TA-Lib，可选 TA-Lib 加速 34 个指标 `[pandas-ta-classic PyPI, https://pypi.org/project/pandas-ta-classic/, 2026-06]` 高 | 与原 pandas-ta API 兼容，pandas 原生；VWAP/BBANDS/ADX/RSI/MACD/ATR 齐全 | https://github.com/xgboosted/pandas-ta-classic | 单人/小团队维护；需核对个别指标是否有整列（非滚动）计算导致前视（原 pandas-ta 部分指标如 `vwap` 依赖锚定周期，需检查锚点是否闭合） |
| **polars_ta (wukan1986)** | Polars 表达式风格的 TA-Lib/WorldQuant 算子重写 | 0.5.17，2026-01-27；MIT；262 stars；可选 `[talib]` extra；中英双语文档 `[polars-ta PyPI, https://pypi.org/project/polars-ta/, 2026-01]` 高；`[GitHub, https://github.com/wukan1986/polars_ta]` 高 | 优先实现 WQ Alpha 公式算子，再 TA-Lib 同签名指标；`MIN_SAMPLES` 全局最小样本；表达式天然是滚动/因果的 | https://github.com/wukan1986/polars_ta | 面向 A 股横截面因子文化，事件级用法需自己验证；作者单人；文档偏中文 |
| **polars-ta (noahbclarkson, Rust crate)** | Rust/Polars 插件式指标 | 0.1.0 初版，0 stars，Polars 0.37+；20+ 指标含 ADX/VWAP/ATR/Bollinger/分数差分 `[GitHub, https://github.com/noahbclarkson/polars-ta]` 高 | 与 TA-Lib 数值一致的声明 | https://lib.rs/crates/polars-ta | 极早期，无 Python 包，不建议依赖 |
| **vectorbt (open source)** | 向量化回测框架，自带 IndicatorFactory | 1.1.0，2026-07-05；Py 3.11–<3.15；许可 Apache-2.0 + Commons Clause（"fair-code"）`[vectorbt PyPI, https://pypi.org/project/vectorbt/, 2026-07]` 高；PRO 版闭源付费 `[vectorbt.pro, https://vectorbt.pro/features/indicators/]` 高 | IndicatorFactory 可包 TA-Lib/pandas-ta/自定义；多参数网格广播；回测与指标同库 | https://vectorbt.dev/ | Commons Clause 不是 OSI 许可（单人研究无碍，发布工具链需注意）；重量级依赖（numba）；PRO/OSS 功能分叉 |
| **ta (bukosabino)** | pandas/numpy 的 43 个指标 | 5.2k stars；MIT；122 open issues；PyPI 页面抓取失败，最新版本/日期未核 `[GitHub, https://github.com/bukosabino/ta]` 高 | 五类（趋势 15/动量 11/波动 5/成交量 9）；纯滚动 | https://technical-analysis-library-in-python.readthedocs.io/ | 更新频率低（2025 后活动未查到）；无 ADX 之外的结构类指标 |
| **tulipy** | Tulip Indicators C 库绑定 | 0.4.0，2019-04-11；LGPLv3 `[tulipy PyPI, https://pypi.org/project/tulipy/, 2019]` 高 | 100+ 指标、快 | https://github.com/cirla/tulipy | **7 年未更新**；LGPL；Py3.12+ 兼容未知 |
| **finta** | pandas 的 80+ 指标 | **仓库 2022-09-02 归档只读**；2.3k stars；LGPL-3.0 `[GitHub, https://github.com/peerchemist/finta]` 高 | — | — | 已归档，排除 |
| **TA-Lib-Precompiled / cgohlke/talib-build** | TA-Lib 预编译 wheel 的第三方兜底 | `[PyPI, https://pypi.org/project/TA-Lib-Precompiled/]` 中 | 在官方 wheel 不覆盖的平台救急 | — | 官方 wheel 已覆盖后价值下降 |

**横切能力对照（本项目三大关注）：**

- *天然防前视*：TA-Lib、tulipy、polars_ta、ta 皆为滚动因果算子；风险点在 pandas-ta 系的"锚定"类指标（VWAP 按日锚定、ZigZag、Supertrend 回填）以及任何用整列统计（如分位/`mean()`）的自定义特征。没有一个库提供"as-of 断言"，前视防护要靠 §2 的 as-of join 与 §2 的 lookahead 对照测试。
- *多周期对齐（1m→15m/1h 闭合 bar）*：没有指标库内置；用 Polars `group_by_dynamic`（`closed`/`label` 参数，社区曾为 `label='right'` 提 issue #9134）`[Polars docs, https://docs.pola.rs/py-polars/html/reference/dataframe/api/polars.DataFrame.group_by_dynamic.html]` 高 或 pandas `resample(closed='right', label='right')` 自行重采样，再把"bar 收盘时间"作为 as-of 键。
- *资金费率/持仓量*：ccxt 提供 `fetchFundingRateHistory` / `fetchOpenInterestHistory(symbol, timeframe)`，后者单次 200 条需分页 `[CCXT docs, https://docs.ccxt.com/docs/exchanges/binance]` 高；**Binance `openInterestHist` 仅提供最近 30 天** `[Binance dev community, https://dev.binance.vision/t/open-interest-data/15681]` 中 → 回溯到 2025-01 的 OI 历史必须走 data.binance.vision 的 `metrics` 归档或第三方（Amberdata 等）`[Amberdata blog, https://blog.amberdata.io/where-to-find-binance-open-interest-data-for-enterprises-amberdata]` 中。资金费率每 8h 结算，as-of 对齐要以"结算时间"而非"公布时间"为键（预测费率与已结算费率是两个不同的 as-of 序列）。

---

## 2. as-of 特征与时序泄漏防护

| 候选 | 定位 | 成熟度信号 | 关键能力对照需求 | 官方文档 | 初步疑虑 |
|---|---|---|---|---|---|
| **polars `join_asof`** | 内存 DataFrame 的 as-of join | Polars 主线（持续发布）`[Polars docs, https://docs.pola.rs/py-polars/html/reference/dataframe/api/polars.DataFrame.join_asof.html]` 高（页面抓取仅得导航，参数细节按已知 API：`strategy=backward/forward/nearest`、`by`、`tolerance`、`allow_exact_matches`、`check_sortedness`）中 | `backward` 默认含等号 → 需用 `allow_exact_matches=False` 或把右表键改为"bar 闭合时间 + 1ns"来实现"严格早于信号时刻"；`by=symbol` 分组 | 同上 | 等号语义易错，是本项目最典型的泄漏点；需写单测 |
| **DuckDB `ASOF JOIN`** | SQL 侧 as-of，可直接跑 Parquet | 文档以 `>=` 为常用形式，示例含 `ticker` 等值分区键 `[DuckDB docs, https://duckdb.org/docs/current/guides/sql_features/asof_join, 2026]` 高 | 与过滤/窗口函数同管道；`>`/`>=` 由用户写明；适合做"特征快照表"物化 | 同上 | 平局（相同时间戳）处理未在文档中明说，需测 |
| **pandas `merge_asof`** | 经典实现 | pandas 主线 `[pandas docs, https://pandas.pydata.org/docs/reference/api/pandas.merge_asof.html]` 高 | `direction`、`allow_exact_matches`、`by`、`tolerance` | 同上 | 性能一般；行为与 polars 等价可作交叉校验 |
| **Feast（offline store = DuckDB/文件）** | 开源 feature store，`get_historical_features()` 做 point-in-time 正确 join | 0.66.0，2026-08-21；Apache-2.0；DuckDB offline store 通过 ibis `[Feast PyPI, https://pypi.org/project/feast/, 2026-08]` 高；`[Feast DuckDB docs, https://docs.feast.dev/reference/offline-stores/duckdb]` 高 | 内建 PIT 语义、`ttl`、事件时间戳；能把"特征定义"当契约登记 | https://docs.feast.dev/ | 对单人项目是重量级（registry/feature view 样板代码）；PIT 语义按 `event_timestamp <= entity_timestamp` 含等号，同样要处理闭合 bar 偏移 |
| **Databricks / SageMaker / Azure ML Feature Store** | 托管 PIT join | `[Databricks docs, https://docs.databricks.com/aws/en/machine-learning/feature-store/time-series]` 高 | 参考其"timestamp key"设计 | — | 云依赖，不符合离线单人项目 |
| **freqtrade lookahead-analysis** | "切片回测 vs 全量回测比对"的前视探测器 | `[freqtrade docs, https://www.freqtrade.io/en/stable/lookahead-analysis/]` 高 | 方法可移植：对同一特征函数分别在"截断到 t 的数据"和"全量数据"上计算，值不一致即前视；文档明说未触发的信号不会被验证、可能误报 | 同上 | 绑定 freqtrade 策略框架；只能借思路自写 |
| **dataleaks / targetleak / leak-detect（PyPI）** | 通用泄漏检测小库 | dataleaks 0.1.0 含"temporal edge cases"测试 `[PyPI, https://pypi.org/project/dataleaks/0.1.0/]` 中；leak-detect 用复数/NaN 注入把特征管道当黑盒测 `[PyPI, https://pypi.org/project/leak-detect/]` 中 | leak-detect 的"NaN 注入传播"思路适合验证"t 之后的 K 线是否影响 t 时刻特征" | — | 均为 0.x 早期、下载量小；成熟度低，仅作参考实现 |
| **LeakageDetector 2.0 (VS Code)** | notebook 静态分析 | `[arXiv 2509.15971, https://arxiv.org/html/2509.15971, 2025-09]` 中 | 检测预处理泄漏 | — | 面向 notebook，非时序 |

**"point-in-time 正确性"现成校验工具：未找到通用、成熟的 Python 包**（搜索词：`point-in-time feature leakage tools python`、`lookahead bias checker python`、`temporal leakage detection pip`）。可行路线是自写两类测试：(a) 截断重算一致性（freqtrade 思路）；(b) NaN/复数注入传播（leak-detect 思路），并把特征表的每行都带 `asof_ts`、`bar_close_ts` 两列做断言 `bar_close_ts < signal_ts`。

---

## 3. 预注册与锁箱

| 候选 | 定位 | 成熟度信号 | 记账成本（单人） | 能否证明"看结果前已登记" | 离线可用 | 初步疑虑 |
|---|---|---|---|---|---|---|
| **git + JSONL + sha256 manifest（自建）** | 最小协议：假设登记→commit→holdout 数据哈希→评估 | 无依赖 | 最低 | git commit 时间戳可被本地伪造；需外部锚定（见下） | 完全离线 | 全靠纪律；需自写 `hypotheses.jsonl` schema 与校验脚本 |
| **+ OpenTimestamps（python-opentimestamps）** | 用比特币区块链锚定文件哈希，免费、去信任 | `[opentimestamps.org, https://opentimestamps.org/]` 高；`[python-opentimestamps, https://github.com/opentimestamps/python-opentimestamps]` 高 | 一条命令 `ots stamp manifest.json` | **可**：第三方可验证哈希在某区块之前存在 | 盖章需联网（一次性），验证可离线（有区块头） | 确认延迟数小时；库更新不频繁 |
| **+ RFC 3161 TSA（rfc3161-client / rfc3161ng）** | 传统可信时间戳（freetsa、DigiCert 等） | rfc3161-client（Sigstore 团队）`[PyPI, https://pypi.org/project/rfc3161-client/]` 高；rfc3161ng `[GitHub, https://github.com/trbs/rfc3161ng]` 中 | 一次 HTTP | **可**：TSA 签名的时间戳 | 需联网 | 依赖 TSA 存续 |
| **DVC** | 数据版本 + `dvc exp`（隐藏 git commit 记实验，无服务器） | 3.67.1，2026-03-31；Apache-2.0 `[DVC PyPI, https://pypi.org/project/dvc/, 2026-03]` 高；`[DVC docs, https://dvc.org/doc/use-cases/experiment-tracking]` 高 | 中（`dvc.yaml` pipeline + params.yaml） | 同 git（本地时间戳）；holdout 文件 md5 在 `.dvc` 中天然是锁箱哈希 | 是 | 对几十 MB 的 K 线数据略重；exp 分支管理有学习曲线 |
| **MLflow（本地 file store）** | 实验记账 + UI | 3.16.0，2026-09-04；Apache-2.0；`mlflow server` 本地 `[MLflow PyPI, https://pypi.org/project/mlflow/, 2026-09]` 高 | 中低（几行 `log_param/log_metric`） | 运行记录可被删改，无不可篡改性 | 是 | 3.x 重心转向 GenAI tracing，功能膨胀 |
| **Weights & Biases** | SaaS 实验跟踪；有 offline 与自托管 | 免费层仅限个人；Pro $60/user/mo `[W&B pricing, https://wandb.ai/site/pricing/]` 高；offline 模式 `[docs, https://docs.wandb.ai/models/ref/cli/wandb-offline]` 高 | 低 | 服务器时间戳由 W&B 背书（若用云端）；但可删除 run | offline 可 | 云依赖、隐私；单人项目性价比低 |
| **Sacred** | 配置作用域 + 观察者（Mongo/文件） | 0.8.7，2024-11-26；MIT；Py≥3.8 `[Sacred PyPI, https://pypi.org/project/sacred/, 2024-11]` 高 | 中 | 无 | 是（FileStorageObserver） | 近两年只发兼容性小版本，社区活跃度低 |
| **Hydra** | 配置管理（非跟踪） | 已迁至 hydra-ecosystem 组织，MIT，最近发布 2026-08-29，Py3.10–3.14 `[hydra-core PyPI, https://pypi.org/project/hydra-core/]` 高；`[issue #3075, https://github.com/facebookresearch/hydra/issues/3075]` 中 | 低 | 无 | 是 | 只解决配置组合，不解决登记 |
| **Guild AI** | 无侵入实验跟踪 | **0.9.0，2023-02-25 为最后 PyPI 发布**；906 stars `[guildai PyPI, https://pypi.org/project/guildai/]` 高 | 低 | 无 | 是 | 3.5 年无发布，视为停滞 |
| **lakeFS** | 对象存储上的 git 式数据版本 | 面向大规模/团队；有 local 模式 `[lakeFS blog, https://lakefs.io/blog/dvc-vs-git-vs-dolt-vs-lakefs/]` 中（厂商内容） | 高（需跑服务） | 无 | 需服务 | 对单人小数据过重 |

**预注册的学术背景**：ML 领域已有预注册 workshop 与"可疑实践"清单 `[Questionable practices in ML, https://arxiv.org/pdf/2407.12220, 2024]` 中；2026 有专门讨论 AI agent 实验预注册的论文 `[Preregistration for Experiments with AI Agents, https://arxiv.org/pdf/2606.11217, 2026-06]` 中。未找到专为"ML 研究假设登记 + 锁箱"打包的现成 Python 工具（搜索词：`preregistration machine learning experiment hash lockbox`、`ML preregistration tool python`）。

---

## 4. 小样本金融统计工具

### 4.1 Purged / Embargo / CPCV / Walk-forward

| 候选 | 定位 | 成熟度信号 | 关键能力 | 文档 | 初步疑虑 |
|---|---|---|---|---|---|
| **purgedcv (eslazarev/purged-cross-validation)** | sklearn 兼容分割器，专为此缺口而建 | 0.1.6，**2026-09-04**；MIT；Py≥3.10；31 stars；有 JOSS 论文稿 `[PyPI, https://pypi.org/project/purgedcv/, 2026-09]` 高；`[GitHub, https://github.com/eslazarev/purged-cross-validation]` 高 | Purged K-fold、embargo、walk-forward、CPCV + 回测路径重建、group-aware purged k-fold、DSR/PSR/PBO；可入 `cross_val_score`/`GridSearchCV` | 同上 | 极新、stars 少、单作者；需自行核对 purge 定义（按标签区间 t0–t1） |
| **skfolio `CombinatorialPurgedCV` / `WalkForward` / `MultipleRandomizedCV`** | 组合优化库附带的 CV | 1.0.3，2026-08-31；BSD-3；Py≥3.10 `[skfolio PyPI, https://pypi.org/project/skfolio/, 2026-08]` 高；`[API, https://skfolio.org/generated/skfolio.model_selection.CombinatorialPurgedCV.html]` 高 | `n_folds/n_test_folds/purged_size/embargo_size`；`split()` 接 array-like，可用于任意 sklearn 估计器 | https://skfolio.org/ | purge 按"样本个数"而非标签时间区间，事件级不等长持有期需自己换算；库整体面向组合而非分类 |
| **timeseriescv (sam31415)** | 早期 CPCV 实现 | 290 stars；MIT；**2018 后未发布**，被 purgedcv 论文指出存在正确性问题 `[purgedcv paper.md, https://github.com/eslazarev/purged-cross-validation/blob/main/paper/paper.md]` 中 | `PurgedWalkForwardCV`、`CombPurgedKFoldCV`（支持 pred_times/eval_times 区间） | https://github.com/sam31415/timeseriescv | 不维护；可作为参考实现对拍 |
| **mlfinlab (Hudson & Thames)** | AFML 的规范实现 | **全权保留、非开源、商业需付费** `[LICENSE, https://github.com/hudson-and-thames/mlfinlab/blob/master/LICENSE.txt]` 高 | PurgedKFold、CPCV、样本权重、DSR | https://hudsonthames.org/mlfinlab/ | 排除为依赖；仅作方法对照 |
| **mlfinpy (baobach)** | mlfinlab 的 MIT 重写 | 0.1.2，2024-10-09；Alpha `[PyPI, https://pypi.org/project/mlfinpy/, 2024-10]` 高 | 文档未明示 CV 模块 | https://mlfinpy.readthedocs.io/ | 近两年无更新 |
| **自写参考实现** | 按 AFML Ch.7 定义（标签区间 overlap purge + embargo） | — | 完全可控、与事件级标签（每条信号有 [t_entry, t_exit]）契合 | — | 需用 timeseriescv/purgedcv 对拍验证 |

### 4.2 Block bootstrap

| 候选 | 成熟度 | 能力 | 文档 | 疑虑 |
|---|---|---|---|---|
| **arch.bootstrap** | arch 8.0.0，2025-10-21；NCSA；Py≥3.10 `[arch PyPI, https://pypi.org/project/arch/, 2025-10]` 高 | `StationaryBootstrap`（几何块长）、`CircularBlockBootstrap`、`MovingBlockBootstrap`；8.0 改用 `seed`/`default_rng` `[arch docs, https://arch.readthedocs.io/en/latest/bootstrap/timeseries-bootstraps.html]` 高 | 同上 | 按行块抽样；"按日/事件簇"的簇 bootstrap 需先把事件聚合为簇再喂入（或自写 cluster bootstrap） |
| **tsbootstrap** | 0.7.2，2026-08-29；MIT；Py3.10–<3.15；有 sktime 适配器 `[PyPI, https://pypi.org/project/tsbootstrap/, 2026-08]` 高 | MovingBlock/CircularBlock/StationaryBlock/NonOverlappingBlock/TaperedBlock + 残差/sieve bootstrap；2026-07 有论文 `[arXiv 2607.06690, https://arxiv.org/html/2607.06690v1]` 中 | https://tsbootstrap.readthedocs.io/ | 依赖较重（sktime 生态） |

### 4.3 DSR / PBO / 多重检验

| 候选 | 成熟度 | 能力 | 文档 | 疑虑 |
|---|---|---|---|---|
| **purgedcv** | 见上 | DSR、PSR、PBO 一并提供 | 见上 | 新 |
| **pypbo (esvhd)** | 140 stars；**AGPL-3.0**；依赖 statsmodels 0.8.0 `[GitHub, https://github.com/esvhd/pypbo]` 高 | PBO、DSR、PSR、MinTRL、MinBTL、随机占优 | 同上 | AGPL；依赖版本陈旧，疑似停滞 |
| **arch.bootstrap 多重比较：SPA / RealityCheck / StepM / MCS** | arch 主线 `[arch docs, https://arch.readthedocs.io/en/latest/multiple-comparison/multiple-comparisons.html]` 高 | 控制 FWER 的"多策略 vs 基准"检验，内建 bootstrap | 同上 | 输入是损失序列，需把事件级 R 序列对齐到共同时间轴 |
| **statsmodels `multipletests`** | 主线 `[statsmodels, https://www.statsmodels.org/stable/generated/statsmodels.stats.multitest.multipletests.html]` 高 | Bonferroni/Holm/BH/BY 等 | 同上 | 不做 max-T |
| **scipy `false_discovery_control`** | SciPy ≥1.11 `[SciPy docs, https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.false_discovery_control.html]` 高 | BH/BY | 同上 | 仅 FDR |
| **MNE `permutation_t_test`（tmax）** | MNE 1.12 `[MNE docs, https://mne.tools/stable/generated/mne.stats.permutation_t_test.html]` 高 | max-T 置换 FWER 校正 | 同上 | 神经科学包，依赖重；置换单位为独立样本，簇相关需自写 |
| **evalci** | 2026-07 论文 `[arXiv 2607.04429, https://arxiv.org/pdf/2607.04429]` 中 | Holm/BH + 配对置换检验 | — | 面向 LLM 评测 |
| **自写 max-T 簇置换** | — | 以"日/事件簇"为置换单位的 max-T，最贴合审稿要求 | — | 需验证 |

### 4.4 类别不平衡评估

| 候选 | 成熟度 | 能力 | 疑虑 |
|---|---|---|---|
| **scikit-learn 1.9** | `precision_recall_curve`/`average_precision_score`/`CalibrationDisplay`/`brier_score_loss`/`class_weight`/`sample_weight` `[sklearn docs, https://scikit-learn.org/stable/auto_examples/model_selection/plot_precision_recall.html]` 高 | 标准答案；`sample_weight` 可实现 IPW | 校准曲线在百级正样本下分箱噪声大，需自助区间 |
| **imbalanced-learn 0.14.2** | 2026-06-07，兼容 sklearn 1.9 `[whats_new, https://imbalanced-learn.org/stable/whats_new.html]` 高 | 重采样、`BalancedBaggingClassifier` | 对可解释规则模型，重采样会扭曲规则支持度，谨慎 |
| **IPW 专用库（zEpid / dowhy）** | — | 因果 IPW | 未针对本需求检索；本项目 IPW 可用 sklearn `sample_weight` 直接实现 |

---

## 5. 可解释规则模型

| 候选 | 成熟度 | 输出人能读的 if-then | 复杂度控制 | 文档 | 疑虑 |
|---|---|---|---|---|---|
| **sklearn LogisticRegression / DecisionTree (`max_depth`, `min_samples_leaf`, `ccp_alpha`) / `export_text`** | 1.9 | 树可 `export_text`；LR 输出系数 | 深度/叶最小样本/CCP 剪枝/L1 | https://scikit-learn.org/ | 无规则列表；LR 需分箱才可读 |
| **imodels** | 3.0.0，2026-08-03；MIT；1.6k stars；Py≥3.9 `[imodels PyPI, https://pypi.org/project/imodels/, 2026-08]` 高；`[GitHub, https://github.com/csinva/imodels]` 高 | RuleFit、SkopeRules、Bayesian Rule List、Bayesian Rule Set、OneR、Greedy Rule List、FIGS、HS-trees 统一 `get_rules()` 返回 DataFrame | `max_rules`、`max_leaf_nodes`、`reg_param`、CV 后缀版本 | https://csinva.io/imodels/ | 学术维护，个别模型（BRL）慢；SkopeRules 为内置移植版，避免了 contrib 版失修 |
| **skope-rules (scikit-learn-contrib)** | 最后提交约 2023-02；曾因 `sklearn.externals.six` 移除报错 `[issue #41, https://github.com/scikit-learn-contrib/skope-rules/issues/41]` 中 | 高精度"圈定"规则 | precision_min/recall_min/n_estimators | https://github.com/scikit-learn-contrib/skope-rules | 实质停滞，用 imodels 内的版本 |
| **interpret (EBM)** | 0.7.8，2026-03-17；MIT；6.9k stars；Py3.10–3.14 `[interpret PyPI, https://pypi.org/project/interpret/, 2026-03]` 高 | 输出形状函数与成对交互，**不是 if-then 规则**；可编辑 | `max_bins`、`interactions`、单调约束（README 未明示，需查 API） | https://interpret.ml/ | 可读但非规则；适合做"每个指标的边际曲线"解释 |
| **CORELS（imodels 内 `OptimalRuleListClassifier`）** | 随 imodels | 最优规则列表 | `max_card`、`c` 正则 | 同上 | 需 corels 二进制依赖 |

---

## 6. LLM 辅助因子/假设挖掘参考

| 候选 | 定位 | 成熟度信号 | "登记→公式→评估"数据结构可借用点 | 评估对象 | 文档 | 疑虑 |
|---|---|---|---|---|---|---|
| **Alpha-GPT (arXiv 2308.00016) / Alpha-GPT 2.0 (2402.09746)** | 人提想法→LLM 形式化→搜索→回测→解释 | EMNLP 2025 System Demos 正式发表 `[ACL Anthology, https://aclanthology.org/2025.emnlp-demos.14/, 2025]` 高；**未找到官方代码**（搜索：`Alpha-GPT official code IDEA Research github`） | 论文中的"想法→表达式→回测报告→人反馈"四段循环 | 横截面 IC / WorldQuant 比赛指标 | https://arxiv.org/abs/2308.00016 | 无官方实现 |
| **parthmodi152/alpha-gpt（非官方）** | LangGraph + Zipline + Alphalens + PyGAD 复现 | 222 stars；MIT；"Under Development"，5 次提交 `[GitHub, https://github.com/parthmodi152/alpha-gpt]` 高 | 符号表达式 + Alphalens 报告 | 横截面 | 同上 | 半成品 |
| **QuantaAlpha** | LLM + 进化，多角色（Lead/Reviewer/Miner） | 1.5k stars；MIT；arXiv 2602.07085；依赖 Qlib；CSI300 `[GitHub, https://github.com/QuantaAlpha/QuantaAlpha]` 高 | `all_factors_library*.json` 因子库 JSON；"结构化假设-代码约束" | IC 0.047 / RankIC / IR / ARR / MDD | https://arxiv.org/abs/2602.07085 | A 股横截面、Qlib 强耦合 |
| **AlphaAgent (arXiv 2502.16789)** | LLM 挖掘 + 正则化探索抗衰减 | 407 stars；数据 Tushare；CSI1000 日频 `[GitHub, https://github.com/RndmVariableQ/AlphaAgent]` 高 | 因子存为 `.dsl` 表达式文件 → memmap 因子库；`eval_factor.py` | IC、换手、分位报告 | https://arxiv.org/abs/2502.16789 | 许可证未标明 |
| **AlphaGen (RL-MLDM)** | RL 生成公式因子，含 `alphagen_llm` 与 `llm_only.py` | 1.2k stars；需 Qlib `[GitHub, https://github.com/RL-MLDM/alphagen]` 高 | `Expression` 对象（token 化表达式树）可直接借用为"假设的形式化表示" | 单因子 IC / RankIC / 互 IC | 同上 | 许可未标 |
| **AlphaEval (arXiv 2508.13174)** | 因子挖掘评估框架 | 219 stars；MIT；BaoStock + Qlib；内含 gplearn/AlphaGen/AlphaForge/AlphaAgent 等基线 `[GitHub, https://github.com/LeoDingggg/AlphaEval]` 高 | 评估维度（预测力/稳定性/鲁棒性/可解释性/多样性）可借为"假设评估记分卡"字段 | 横截面 | https://arxiv.org/abs/2508.13174 | A 股 |
| **Microsoft RD-Agent(Q)** | 假设→实验→实现→反馈闭环，基于 Qlib | 14.5k stars；MIT；执行 trace 可下载 `[GitHub, https://github.com/microsoft/RD-Agent]` 高；`[docs, https://rdagent.readthedocs.io/en/latest/scens/quant_agent_fin.html]` 高 | **最值得借用**：hypothesis / experiment / feedback 的 trace 结构与"下一步决策"记录 | ARR、IC | 同上 | 重（多 agent、Docker、Qlib） |
| **Alpha-Jungle MCTS (arXiv 2505.11122)** | LLM + MCTS 公式挖掘 | 论文 `[arXiv, https://arxiv.org/html/2505.11122v2, 2025]` 中；**仓库未找到**（搜索：`"Alpha-Jungle" github`） | — | 横截面 | — | 无代码 |
| **QuantAgent (arXiv 2402.03755)** | 两层自改进循环，知识库记录"实现+想法+指标+专家评审" | **未找到公开代码**（搜索：`QuantAgent github code release`）`[arXiv, https://arxiv.org/abs/2402.03755v1]` 中 | 知识库条目四字段设计可借 | 横截面 | — | 无代码 |
| 2026 新论文：AlphaPROBE (2602.11917)、AlphaMemo (2606.20625)、FactorEngine (2603.16365)、Cognitive Alpha Mining (2511.18850)、HARLA (FCS 2026) | 搜索过程记忆、程序级知识注入 | `[arXiv 列表见上文 URL]` 中 | AlphaMemo 的"结构化搜索过程记忆"与预注册日志同构 | 横截面 | — | 代码情况未逐一核 |
| **Profit Mirage (arXiv 2510.07920)** | 反面材料：LLM 金融 agent 的信息泄漏 | `[arXiv, https://arxiv.org/pdf/2510.07920, 2025-10]` 中 | 审稿人会引用；LLM 假设抽取需记录模型知识截止与帖文时间 | — | — | — |

**评估对象差异（横截面 IC vs 事件级 R）**：上述框架全部在"每日横截面、数千只股票、日频 IC/RankIC"上评估，假设的"好坏"是相关系数排序；本项目是"每人几十到几百个离散事件、二分类/事件 R 倍数"，样本量小两到三个数量级，评估必须换成 PR-AUC、校准、bootstrap 区间与 DSR/PBO，且不能用它们的 Qlib 回测器。可借的是**数据结构**（假设文本→形式化表达式→参数网格→评估记分卡→人反馈）而非评估器。

---

## 对本项目的初步含义（≤6 条）

1. **指标层首选 TA-Lib 0.7 官方 wheel + Polars 自写重采样**；pandas-ta 原版因 2025-07 起的许可/维护变动与 PyPI 历史被清应排除，若需其 API 用 `pandas-ta-classic`（MIT，2026-06 仍在发）；polars_ta 可作 Polars 原生备选。
2. **资金费率/OI 是数据获取问题而非库问题**：Binance OI 历史 API 只回溯 30 天，回到 2025-01 必须靠 data.binance.vision 归档或第三方；as-of 键应为结算/快照时间。
3. **as-of join 用 polars `join_asof` 或 DuckDB ASOF，重点是等号语义**（`allow_exact_matches=False` 或 bar_close+ε）；没有成熟 PIT 校验包，需自写"截断重算一致性 + NaN 注入传播"两类测试。
4. **预注册/锁箱用"git + JSONL + sha256 manifest + OpenTimestamps/RFC3161 外部锚定"**即可满足"看结果前已登记"的可信时间戳；DVC/MLflow 只解决记账，W&B/lakeFS/Guild AI 对单人离线项目过重或已停滞。
5. **CV/统计：purgedcv（2026-09）与 skfolio CV 是仅有的活跃开源 purged/CPCV 实现，但都很新或非事件级定义，建议自写 AFML 定义并与二者对拍**；block bootstrap 用 arch 8.0 或 tsbootstrap，簇（日/事件）bootstrap 与 max-T 簇置换需自写；PBO/DSR 可用 purgedcv 或 pypbo（注意 AGPL）。
6. **规则模型用 imodels 3.0（统一 `get_rules()`）+ sklearn 浅树/LR，EBM 作边际解释补充**；LLM 假设循环借 RD-Agent 的 hypothesis/experiment/feedback trace 与 AlphaGen 的 `Expression` 表示，但评估器必须换成事件级 PR/校准/bootstrap，并记录 LLM 知识截止以应对 Profit Mirage 类质疑。