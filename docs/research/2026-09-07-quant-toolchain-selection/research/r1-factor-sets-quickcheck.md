# 开源因子集作为"交易员出手时刻"事件级特征池 —— 专项快查（截止 2026-09-07）

> 可信度标注：**高** = 一手源码/官方文档直接核对；**中** = 官方页面/论文摘要或工具辅助解析；**低** = 二手转述或估算。

---

## 1. 候选开源因子集清单

| 名称 | 来源 / 许可证 | 因子数 | 横截面 rank 型 vs 单标的时序型 | 输入字段 | Python 实现与维护 | 官方 URL |
|---|---|---|---|---|---|---|
| **WorldQuant Alpha101** | Kakushadze 2016 论文 (arXiv) / 论文公式本身无软件许可证；主流实现 `yli188/WorldQuant_alpha101_code` **未声明许可证**（859★，无近 6 月发布）；`lvlh2/alpha101`、`STHSF/alpha101`、`KunQuant` 等 | 101 | **混合，以横截面为主**：`rank` 出现 90+ 次，26 条需 `IndNeutralize`；纯时序仅 ~18 条（见 §2） | open/high/low/close/volume/vwap/returns/adv{N}/cap/industry | 多个 pandas 实现，无一为"官方"；均为多股票 MultiIndex 设计 | 论文 https://arxiv.org/abs/1601.00991 ；实现 https://github.com/yli188/WorldQuant_alpha101_code |
| **国泰君安 Alpha191** | 国泰君安研报《基于短周期价量特征的多因子选股体系》/ 研报无软件许可证；实现：`Daic115/alpha191`、`wpwpwpwpwpwpwpwpwp/Alpha-101-GTJA-191`、`msd-rs/py-alpha-lib`（Rust+Python，190/191）、`yupoet/aurumq-rl`（polars，MIT，105 Alpha101 + 191 GTJA） | 191 | **混合，横截面为主**：约 63%（~120 条）用 `.rank(axis=1)`；纯时序约 20–25 条（低可信估算） | open/high/low/close/volume/amount/vwap，部分需 turnover、少数需基准指数 | 多个社区实现；`py-alpha-lib` 与 `aurumq-rl` 较新 | https://github.com/Daic115/alpha191 ；https://github.com/msd-rs/py-alpha-lib ；https://github.com/yupoet/aurumq-rl |
| **Qlib Alpha158** | Microsoft Qlib / MIT | 158 = KBAR 9 + PRICE 4 + ROLLING 29 算子 × 5 窗口(5/10/20/30/60) | **全部单标的时序型**（含 `Rank` 也是滚动窗口内百分位，非横截面） | open/high/low/close/volume/vwap | Qlib 内置 `Alpha158DL`，活跃维护 | https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py |
| **Qlib Alpha360** | Microsoft Qlib / MIT | 360 = 6 字段 × 过去 60 根 bar 原始值（除以最新 close 归一） | 单标的时序型（无算子，纯滞后值） | OHLCV + VWAP | 同上 | 同上 |
| **AlphaGen（RL 生成公式因子）** | ICT-FinD-Lab/RL-MLDM，KDD 2023 / 仓库页面**未见 LICENSE 声明**（中） | 生成式，不定 | 算子库以时序为主（Ref/Mean/Sum/Std/Var/Skew/Kurt/Max/Min/Med/Mad/Rank(滚动)/Delta/WMA/EMA/Cov/Corr），另有 1 个横截面 `CSRank`；**评估用横截面 IC/RankIC**，需多股票面板 | Qlib 格式 OHLCV | 1.2k★，98 commits，23 open issues；单标的需自写 `AlphaCalculator` | https://github.com/RL-MLDM/alphagen |
| **gplearn** 遗传编程因子 | gplearn / BSD-3 | 生成式 | 取决于自定义算子；默认无横截面概念，可做单标的 | 任意 | 维护缓慢；AlphaGen 仓库内含改版 gplearn 作 baseline | https://github.com/trevorstephens/gplearn |
| **tsfresh** | blue-yonder / MIT | 最多 ~1558 个（`ComprehensiveFCParameters`） | 通用单序列时序特征 | 任意单变量序列 | 活跃，v0.21.x | https://tsfresh.readthedocs.io/ |
| **TSFEL** | Fraunhofer Portugal / BSD-3 | 65+（统计/时域/频域/分形） | 通用单序列 | 单变量 | v0.2.0，更新慢 | https://github.com/fraunhoferportugal/tsfel |
| **catch22 / pycatch22** | DynamicsAndNeuralSystems / **GPL-3** | 22（另有 catch24 加 mean/std） | 通用单序列，从 hctsa 7000+ 特征聚类精选 | 单变量 | 有维护；aeon/tsflex 集成 | https://github.com/DynamicsAndNeuralSystems/pycatch22 |
| **tsflex** | predict-idlab / MIT | 框架（不自带特征），可包装 tsfresh/tsfel/catch22/seglearn/antropy | 通用单序列，支持多步长滚窗 | 任意 | v0.4.1（2024-09） | https://github.com/predict-idlab/tsflex |
| **freqtrade FreqAI 特征工程** | freqtrade / GPL-3 | 示例：RSI/MFI/ADX/SMA/EMA/ROC/BB 宽度/收盘-下轨比/相对成交量/pct_change/raw_volume/raw_price/day_of_week/hour_of_day；按 `indicator_periods_candles × include_timeframes × include_shifted_candles × corr_pairs` 展开 | 单标的时序（可加相关对） | OHLCV | 活跃 | https://docs.freqtrade.io/en/latest/freqai-feature-engineering/ |
| **jesse 指标库** | jesse-ai / MIT | 150+（Rust 加速，`sequential` 参数） | 单标的时序 | candles ndarray | 活跃 | https://docs.jesse.trade/docs/indicators/reference.html |
| **hummingbot quants-lab** | hummingbot / Apache-2.0 | 依赖 pandas_ta / TA-Lib，无自有因子集 | 单标的 | OHLCV | **quants-lab 已停止官方维护**（仓库仍可用） | https://github.com/hummingbot/quants-lab |
| **`crypto-factor` / `cryptofactors`** | — | — | — | — | **未找到**同名可用库（见下） | — |
| **ml-quant-trading（213 因子）** | initial-d / MIT，arXiv 2507.07107 | 213 = 9 Alpha101 + 204 手工 | A 股横截面设计，含 cs_rank 组 | OHLCV+amount+turnover | PyTorch 张量实现 | https://github.com/initial-d/ml-quant-trading |

**加密专用因子库查找结果**：搜过 `github cryptofactors`、`"crypto-factor" python library`、`pypi cryptofactors OR "crypto-factors" OR "cryptofactor"`、`crypto factor library github 2025 momentum funding rate open interest`——**没有找到一个成型、有维护的加密因子 Python 库**；命中的只有零散仓库（`funding-rate-arbitrage`、`crypto-factor-modelling-` 学生项目、`cryptory` 数据抓取包）。结论：加密"因子集"目前主要存在于论文而非库 [搜索结果, https://github.com/topics/funding-rates, 2026-09-07]（高：搜索覆盖较全，但不排除小众仓库遗漏）。

**加密学术因子（2025–2026）要点**：
- Liu-Tsyvinski-Wu《Common Risk Factors in Cryptocurrency》JF 2022：市场/规模/动量三因子解释横截面 [https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.13119]（高）。
- 《A Trend Factor for the Cross Section of Cryptocurrency Returns》JFQA：多尺度均线趋势因子 [https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/trend-factor-for-the-cross-section-of-cryptocurrency-returns/4C1509ACBA33D5DCAF0AC24379148178]（中）。
- SSRN 6725492（2026）：MVRV、**资金费率、OI** 在各 regime 均预测 BTC 收益 [https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6725492]（中）。
- BIS WP 1087《Crypto carry》：carry 预测空头清算，10% 标准化 carry 上升 → 次月空头清算增 22% OI [https://www.bis.org/publ/work1087.pdf]（中）。
- arXiv 2607.09426《The Quarter-Hour Effect》(2026)：加密期货在 1/5/15 分钟整点有周期性波动/成交爆发，开盘 order imbalance 预测 4–12 小时收益——**与本项目 15m bar 直接相关** [https://arxiv.org/html/2607.09426v2]（中）。
- arXiv 2506.08573《Designing funding rates for perpetual futures》：资金费率作为反馈规则引致均值回复 basis [https://arxiv.org/abs/2506.08573]（中）。
- MDPI《Two-Tiered Structure of Cryptocurrency Funding Rate Markets》(2026-01)：26 交易所 749 品种，17% 观测有 ≥20bp 套利价差但税后仅 40% 为正 [https://www.mdpi.com/2227-7390/14/2/346]（中）。
- 上述论文**均未附可直接复用的因子代码库**（搜过 `crypto factor zoo replication code github 2025 2026`）。

---

## 2. 可迁移性判断

### Alpha101：纯时序算子（无 `rank`/`scale`/`IndNeutralize`）的条目
按 arXiv 1601.00991 附录公式逐条核对（自查，中高可信；WebFetch 对 gist 的自动解析给出 14 条但含 #4/#10/#19/#20 等明显含 `rank` 的误判，已弃用）：

**18 条**：#6, #7, #9, #12, #21, #23, #24, #26, #35, #41, #43, #46, #49, #51, #53, #54, #84, #101

例：#6 `-corr(open, volume, 10)`；#9/#46/#49/#51 是基于 `delta/delay` 的趋势条件因子；#23 `sum(high,20)/20 < high ? -delta(high,2) : 0`；#26/#35/#43/#84 只用 `ts_rank`；#41 `sqrt(high*low) - vwap`；#53 `-delta(((close-low)-(high-close))/(close-low), 9)`；#101 `(close-open)/(high-low+.001)`。

补充判断：
- 剩余 ~83 条中大多数是"时序核心 + 外层 `rank` 做尺度归一"（如 #10、#19、#20 只在外层套一个 rank）。Coriva 的说法是 rank "解决尺度问题"，因此**把 `rank` 换成滚动 `ts_rank`/z-score 即可得到单品种版本**，这是社区常见做法，但改后已不是原因子 [WorldQuant Alpha 101 Explained, https://coriva.eu.org/en/alpha101-overview/, 2026-04-14]（中）。
- DolphinDB 官方 wq101alpha 模块声明"所有因子都同时涉及横截面与时序计算" [WorldQuant 101 Alphas, https://docs.dolphindb.com/en/Tutorials/wq101alpha.html]（中）。
- 26 条需行业中性化，在加密只有 44 个品种时无意义。

### Alpha191
工具辅助解析 `Daic115/alpha191.py`：约 120 条（63%）用 `.rank(axis=1)`，纯时序约 20–25 条（如 #2/#9/#13/#14/#15/#18/#126/#150/#171）[https://raw.githubusercontent.com/Daic115/alpha191/main/alpha191.py]（低：自动解析，未逐条人工核对）。少数因子还需基准指数收盘价，加密需自造（如 BTC 或等权指数）。

### Qlib Alpha158：是否本来就是单标的时序？
**是，158 条全部是单标的时序特征**（高，源码核对 `qlib/contrib/data/loader.py`）：
- **KBAR 9 条**：KMID/KLEN/KMID2/KUP/KUP2/KLOW/KLOW2/KSFT/KSFT2——全是当根 K 线形态比例（实体、上下影、收盘位置），只用当根 OHLC。
- **PRICE 4 条**：`$open/$close`, `$high/$close`, `$low/$close`, `$vwap/$close`（窗口 [0]）。
- **ROLLING 29 算子 × 5 窗口 = 145 条**：ROC/MA/STD/BETA(Slope)/RSQR/RESI/MAX/MIN/QTLU/QTLD/RANK/RSV/IMAX/IMIN/IMXD/CORR/CORD/CNTP/CNTN/CNTD/SUMP/SUMN/SUMD + 量能 VMA/VSTD/WVMA/VSUMP/VSUMN/VSUMD。
- 其中 `RANK = Rank($close, d)` 在 Qlib 语义里是"当前 close 在过去 d 根 close 中的百分位"，即 **ts_rank，不是横截面**（`qlib.data.ops.Rank` 属 Rolling 算子族）[https://github.com/microsoft/qlib/blob/main/qlib/data/ops.py]（高）。注意 WebFetch 的一次自动解析误把它标成横截面，已按源码更正。
- 默认配置不含 VOLUME 组（`$volume/($volume+1e-12)` 恒为 1，只是占位）；9+4+145=158 恰好对上。
- 所有表达式均用 `$close` 归一，天然去量纲，**可直接跨 44 个品种共用**——这正是本项目要的"单品种、无横截面依赖"。

**Alpha360** 是 60 根 bar 的原始滞后值（360 维），不可解释、共线性极强，不适合浅模型。

**AlphaGen**：算子库除 `CSRank` 外全是时序算子，`Rank` 是滚动窗口内排名，无未来数据 [expression.py, https://raw.githubusercontent.com/RL-MLDM/alphagen/master/alphagen/data/expression.py]（高）；但训练目标是横截面 IC，单品种需自定义 calculator，且生成因子不可解释——**不建议作为本项目起点**。

---

## 3. 前视（look-ahead）与对齐

| 库 | 滚动窗口是否只用历史闭合 bar | 已知争议 |
|---|---|---|
| Qlib Alpha158/360 | **是**：所有特征表达式只用 `Ref(x, +d)`（向后看）与 Rolling 算子；无负向 Ref。**唯一负向引用在 label**：`Ref($close,-2)/Ref($close,-1)-1`（T+1 开仓 T+2 平仓约定） [loader.py 源码]（高）。Issue #1514 质疑该 label 定义 [https://github.com/microsoft/qlib/issues/1514]（高）。Qlib 另有 PIT 数据库，但只针对季报/年报基本面，与价量特征无关 [https://qlib.readthedocs.io/en/latest/advanced/PIT.html]（高） | 复权价"用前复权隐含未来信息"是 Qlib 文档提到的数据层风险；加密无复权，不受影响 |
| Alpha101/191 社区实现 | 公式层面 `delay/ts_*/sum/stddev` 均向后看；但各 pandas 实现的 `rolling` 若 `center=True` 或用全样本 `.mean()` 就会泄漏，需逐实现审查；`yli188` 实现无许可证且无近期维护 | 未见针对性 GitHub issue（搜过 `alpha101 lookahead issue`，无命中） |
| tsfresh | `roll_time_series` 设计上每个窗口只含截至该点的数据；但 **Issue #807**：`extract_features(impute=...)` 在合并后的全表做 impute → 跨窗口泄漏；**Issue #1074**：用户报告特征与标签同行导致泄漏（属用法错误，需对齐后 shift） [https://github.com/blue-yonder/tsfresh/issues/807 ；https://github.com/blue-yonder/tsfresh/issues/1074]（高） | 有明确 issue |
| pandas-ta（FreqAI/hummingbot 常用） | 大多数指标向后看；**`dpo` 与 `ichimoku` 有 centered 计算，需 `lookahead=False`**（ichimoku 会丢 Chikou Span） [pandas-ta 文档, https://github.com/xgboosted/pandas-ta-classic]（高） | 官方文档明示 "Potential Data Leaks: dpo, ichimoku" |
| freqtrade | 提供 `lookahead-analysis` 子命令：整段回测 vs 逐信号切片回测对比指标值/进出场是否变化，可检出 `shift(-n)`、无窗口 `.mean()/.max()`、循环越界；FreqAI 目标列会被误报（可忽略） [https://www.freqtrade.io/en/stable/lookahead-analysis/]（高） | 另有 `recursive-analysis` 检测指标启动期不稳定 |
| catch22/TSFEL/tsflex | 本身对整段序列计算，无"时间"概念；是否前视完全取决于你切窗方式（tsflex 的 stride/window 机制可保证只用过去） | 无 |

**对本项目的对齐要点**：交易员信号时间戳 t 要映射到"t 之前最后一根已闭合 bar"，特征在该 bar 收盘值上计算；15m bar 内出手的信号不能用当根 bar（未闭合）。Quarter-Hour Effect 论文表明整点 15 分钟处有成交/波动脉冲，正好是 bar 边界，需谨慎处理边界归属 [arXiv 2607.09426]（中）。

---

## 4. 小样本适用性（数百正样本 vs 158/360 特征）

**风险量化**：
- 经典 EPV 规则：逻辑回归每个预测变量至少 10 个事件（Peduzzi 1996） [https://pubmed.ncbi.nlm.nih.gov/8970487/]（高）；后续研究认为可放宽但仍与 EPV 强相关 [van Smeden 2016, https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5122171/ ；Austin & Steyerberg 2017]（高）。以每人 50–300 条正样本计，**可支撑 5–30 个特征**；直接喂 158 维 EPV < 2，过拟合几乎必然。
- tsfresh 自身在 Henderson & Fulcher (2021) 评测中"集合内冗余度最高"；catch22 22 维需 11 个主成分才解释 90% 方差，是同规模最"去冗余"的集合 [An Empirical Evaluation of Time-Series Feature Sets, https://arxiv.org/abs/2110.10914]（高）。

**社区/论文标准做法与工具**：
1. **相关聚类去冗余后再选**：López de Prado《Machine Learning for Asset Managers》——去噪/去调性相关矩阵 → ONC 聚类 → **簇级 MDI/MDA**（clustered feature importance），避免高相关特征互相"替代" [SSRN 3517595, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3517595 ；mlfinlab 文档 https://random-docs.readthedocs.io/en/latest/implementations/feature_clusters.html]（高）。
2. **tsfresh `select_features`**：逐特征假设检验 + Benjamini-Hochberg FDR（默认 0.05，`hypotheses_independent=False`）——适合先砍到几十维 [https://tsfresh.readthedocs.io/en/latest/api/tsfresh.feature_selection.html]（高）。
3. **featurewiz（SULOV/MRMR，最小最优集，剔相关）** vs **Boruta（全相关集，保留冗余）**：小样本+高相关场景倾向 featurewiz 类 MRMR；Boruta 在金融时序预测评测中 MSE 偏高 [https://github.com/AutoViML/featurewiz ；Feature Selection with Annealing, https://arxiv.org/pdf/2303.02223]（中）。
4. Alpha158 内部 5 个窗口同算子高度共线（MA5/MA10/MA20…），通常先按算子族聚类只留 1–2 个窗口。
5. 事件级问题的"负样本"定义（非出手时刻）与样本权重比特征数更影响过拟合——这是产品判断，不在本快查范围。

---

## 5. 对本项目的建议（5 条）

1. **起点选 Qlib Alpha158 的表达式定义（不是 Qlib 框架本身）**：158 条全是单标的、close 归一、无横截面、无前视的时序特征，字段只需 OHLCV+VWAP，Binance 永续 15m/1h 直接可算；用 pandas/polars 复现表达式即可，避免引入 Qlib 数据层。Alpha101 只取那 18 条纯时序条目作补充（#6/#9/#23/#24/#41/#46/#49/#51/#53/#54/#101 尤其可解释）；Alpha191 与 AlphaGen 不用。
2. **裁剪到 ≤30 维的流程**：(a) 29 个 ROLLING 算子每个先只留 2 个窗口（15m 用 20/60，1h 用 10/30）+ 9 KBAR + 4 PRICE ≈ 71 维；(b) Spearman 相关 |ρ|>0.8 做层次聚类，每簇留最可解释一条；(c) 对"出手 vs 未出手"做 tsfresh 式单变量检验（Mann-Whitney/KS + BH-FDR）或 clustered MDA；(d) 最终按 EPV≥10 定上限（100 条正样本 → ≤10 维，300 条 → ≤30 维）。
3. **与手工四类指标的映射关系**（Alpha158 基本覆盖，可直接替代大部分手工指标）：趋势 = MA/BETA(Slope)/RSQR/RESI/CNTD；位置 = RSV/QTLU/QTLD/RANK/MAX/MIN/IMAX/IMIN；动量 = ROC/SUMP/SUMN/SUMD；波动 = STD/KLEN；额外得到"量价关系"第五类 = CORR/CORD/VSUMD/WVMA，是手工集合里缺的。KBAR 9 条直接刻画"出手当根 K 线形态"，对事件级问题很贴切。
4. **加密专属字段单独加 3–5 维，不从开源因子集找**：资金费率（当前值、8h 变化）、OI 变化率（1h/4h）、清算量/方向、basis——文献一致支持其预测力（SSRN 6725492、BIS 1087、arXiv 2506.08573），且没有现成库，需自建；保持这几维可解释，别做交叉。
5. **前视防线**：所有特征只用信号时刻前最后一根闭合 bar；禁用 pandas-ta 的 dpo/ichimoku（或 `lookahead=False`）；若用 tsfresh 做补充特征，impute 必须逐窗口做（Issue #807）；建议照搬 freqtrade `lookahead-analysis` 的思路做一个"截断重算对比"单元测试，验证每个特征在截断数据集上数值不变。

---

## 未查到 / 存疑事项
- `crypto-factor`、`cryptofactors` 同名库：PyPI 与 GitHub 均未找到（搜 3 组关键词）。
- Alpha191 纯时序条数为自动解析估算（20–25），未逐条核对。
- AlphaGen 许可证：仓库页面抓取未见 LICENSE 声明，使用前需人工确认。
- Alpha101 的 18 条纯时序清单为本人按论文逐条核对，建议落地前再对照 arXiv 附录复核一遍。

Sources:
- [qlib loader.py](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py)
- [qlib ops.py](https://github.com/microsoft/qlib/blob/main/qlib/data/ops.py)
- [qlib Issue #1514](https://github.com/microsoft/qlib/issues/1514)
- [Qlib PIT](https://qlib.readthedocs.io/en/latest/advanced/PIT.html)
- [101 Formulaic Alphas arXiv](https://arxiv.org/abs/1601.00991)
- [Coriva Alpha101 overview](https://coriva.eu.org/en/alpha101-overview/)
- [DolphinDB WQ101](https://docs.dolphindb.com/en/Tutorials/wq101alpha.html)
- [yli188/WorldQuant_alpha101_code](https://github.com/yli188/WorldQuant_alpha101_code)
- [lvlh2/alpha101](https://github.com/lvlh2/alpha101)
- [Daic115/alpha191](https://github.com/Daic115/alpha191)
- [msd-rs/py-alpha-lib](https://github.com/msd-rs/py-alpha-lib)
- [yupoet/aurumq-rl](https://github.com/yupoet/aurumq-rl)
- [RL-MLDM/alphagen](https://github.com/RL-MLDM/alphagen)
- [alphagen expression.py](https://raw.githubusercontent.com/RL-MLDM/alphagen/master/alphagen/data/expression.py)
- [tsfresh feature_selection](https://tsfresh.readthedocs.io/en/latest/api/tsfresh.feature_selection.html)
- [tsfresh Issue #807](https://github.com/blue-yonder/tsfresh/issues/807)
- [tsfresh Issue #1074](https://github.com/blue-yonder/tsfresh/issues/1074)
- [pycatch22](https://github.com/DynamicsAndNeuralSystems/pycatch22)
- [tsfel](https://github.com/fraunhoferportugal/tsfel)
- [tsflex](https://github.com/predict-idlab/tsflex)
- [Empirical Evaluation of Time-Series Feature Sets](https://arxiv.org/abs/2110.10914)
- [freqtrade FreqAI feature engineering](https://docs.freqtrade.io/en/latest/freqai-feature-engineering/)
- [freqtrade lookahead-analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/)
- [pandas-ta-classic](https://github.com/xgboosted/pandas-ta-classic)
- [jesse indicators reference](https://docs.jesse.trade/docs/indicators/reference.html)
- [hummingbot quants-lab](https://github.com/hummingbot/quants-lab)
- [initial-d/ml-quant-trading](https://github.com/initial-d/ml-quant-trading)
- [Liu-Tsyvinski-Wu JF 2022](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.13119)
- [Trend factor crypto JFQA](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/trend-factor-for-the-cross-section-of-cryptocurrency-returns/4C1509ACBA33D5DCAF0AC24379148178)
- [SSRN 6725492 Bitcoin predictability](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6725492)
- [BIS Crypto carry](https://www.bis.org/publ/work1087.pdf)
- [Quarter-Hour Effect arXiv 2607.09426](https://arxiv.org/html/2607.09426v2)
- [Designing funding rates arXiv 2506.08573](https://arxiv.org/abs/2506.08573)
- [Two-Tiered Funding Rate Markets MDPI](https://www.mdpi.com/2227-7390/14/2/346)
- [Peduzzi 1996 EPV](https://pubmed.ncbi.nlm.nih.gov/8970487/)
- [van Smeden 2016 EPV](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5122171/)
- [Clustered Feature Importance SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3517595)
- [mlfinlab feature clusters](https://random-docs.readthedocs.io/en/latest/implementations/feature_clusters.html)
- [featurewiz](https://github.com/AutoViML/featurewiz)
- [Feature Selection with Annealing](https://arxiv.org/pdf/2303.02223)