## 第 2 轮关键验证结果（8 次工具调用，已停止搜索）

### 1. purgedcv 正确性

**结论**：purge 是**事件级标签区间重叠**（不是按样本数）；embargo 三种单位互斥（时间/行数/比例）；**无**对比 timeseriescv / AFML 参考实现的测试；仓库很小（31 stars / 7 forks / 120 commits），单作者，无近期 commit 日期信息。

**证据**：`[purged-cross-validation README, https://github.com/eslazarev/purged-cross-validation, 抓取 2026-09-07]` 可信度：高（一手 README，但 README ≠ 源码测试）
- purge 定义：以 `prediction_times`（特征已知时刻）+ `evaluation_times`（标签落定时刻）两条序列判定重叠；原文："Any training observation whose window reaches into the test period has already seen test data and must be dropped."——即 AFML 第 7 章 `t1` 语义。
- embargo 定义：`embargo="3D"`（时间窗）/ `embargo_observations=10`（行数）/ `embargo_fraction=0.01`（`floor(n*0.01)` 行），三者只能选一；仅施加于测试块**之后**，不加在之前。
- 另有 `WalkForwardSplit(purge_horizon="2D")` 时间型 purge。
- 类清单：`WalkForwardSplit / PurgedKFold / PurgedGroupKFold / CombinatorialPurgedCV / CombinatoriallySymmetricCV`；指标 `probabilistic_sharpe_ratio / deflated_sharpe_ratio(_full) / probability_of_backtest_overfitting / min_track_record_length / minimum_backtest_length`；工具 `purge() / apply_embargo() / reconstruct_paths() / path_metrics() / audit_splitter()`。
- Python 3.10–3.14，MIT。

**反面证据**：README 未给出与 timeseriescv / mlfinlab 的数值对比测试，只声称 "The free alternative has been unmaintained since 2018" 并引用 AFML Ch.7/12；未能从页面拿到最近 commit 日期、contributors 数、最新 tag（页面未渲染）。`audit_splitter()` 诊断函数是自检工具，不能替代第三方参考对拍。

### 2. skfolio CombinatorialPurgedCV

**结论**：`purged_size` / `embargo_size` 均为 **int 观测数（行数）**，**不支持事件级不等长标签**（无 `t1` / 事件区间入参）。

**证据**：`[skfolio API: CombinatorialPurgedCV, https://skfolio.org/generated/skfolio.model_selection.CombinatorialPurgedCV.html, 抓取 2026-09-07]` 可信度：高（官方 API 文档原文）
- 签名 `CombinatorialPurgedCV(n_folds=10, n_test_folds=8, purged_size=0, embargo_size=0)`；`n_folds ≥ 3`，`n_test_folds ≥ 2`。
- `purged_size` (int)："Number of observations to exclude from the start of each train set that are after a test set **and** the number of observations to exclude from the end of each training set that are before a test set."
- `embargo_size` (int)："Number of observations to exclude from the start of each training set that are after a test set."
- `split(X, y=None, groups=None)`，无任何标签区间参数；文档引用 AFML 2018。

**反面证据**：页面未显示 skfolio 版本号（无法从该页确认 1.0.3）；未找到 `compute_all` 参数（搜索词：compute_all）。

### 3. polars_ta 对 Alpha158 算子的覆盖

**结论**：`wq` 模块已覆盖大部分基础滚动算子；**回归类（BETA/RSQR/RESI）、计数/条件求和类（CNTP/CNTN/SUMP/SUMN）、加权类（WVMA）在本次抓取中未见对应函数名**，需自写或组合。

**证据**：`[polars_ta GitHub README, https://github.com/wukan1986/polars_ta, 抓取 2026-09-07]` 可信度：中（README 概览可见，未逐文件读源码；262 stars / 58 forks / 216 commits，MIT，设计明确声明 "Use `wq` first. It mimics WorldQuant Alpha"）

| Alpha158 算子 | polars_ta 现成 | 备注 |
|---|---|---|
| ROC | `ts_returns` | ✔ |
| MA / STD | `ts_mean` / `ts_std_dev` | ✔ |
| MAX / MIN | `ts_max` / `ts_min` | ✔ |
| QTLU / QTLD | `ts_quantile` | ✔（传 0.8 / 0.2） |
| ts_Rank | `ts_rank` | ✔ |
| IMAX / IMIN | `ts_arg_max` / `ts_arg_min` | ✔（需核对索引方向与 Qlib 一致） |
| CORR | `ts_corr` | ✔（CORD 为 corr(close/ref, vol/ref)，用 ts_corr 组合） |
| RSV | `RSV`（ta/tdx 模块） | ✔ |
| BETA(Slope) / RSQR / RESI | **未见** `ts_regression`/`ts_slope`/`ts_rsquare`/`ts_resid` | 需自写滚动 OLS（可用 `ts_covariance`/`ts_std_dev` 组合出 slope 与 R²，RESI 需再算） |
| CNTP / CNTN / SUMP / SUMN | **未见** | 用 `ts_mean((Δ>0).cast)` 与 `ts_sum(max(Δ,0))/ts_sum(|Δ|)` 组合即可 |
| WVMA | **未见** | vol-weighted std of returns，需自写 |
| 附加 | `ts_sum / ts_product / ts_delta / ts_delay / ts_zscore / ts_covariance / ts_decay_linear` | 可用 |

**反面证据**：README 未列全函数清单，上表"未见"仅表示 README 级别未出现，源码 `polars_ta/wq/time_series.py` 可能存在（未抓取；建议落地时 `grep ts_regression\|ts_rsquare` 一次确认）；未获得最新 release 版本/日期与 polars 版本要求。

### 4. DuckDB ASOF JOIN 平局与严格不等语义

**结论**：**未取到一手文档**（两次抓取均落到 redirect 页：`/docs/stable/...` → `/docs/current/guides/sql_features/asof_join.html`，第 2 次仍返回 redirect 壳）。

**证据**：无（搜索 URL：`duckdb.org/docs/stable/guides/sql_features/asof_join`、`.../asof_join.html`）。可信度：无。

**基于记忆的未验证说明**（须落地前用 `docs/current` 链接复核）：DuckDB 官方指南写明 ASOF 不等式支持 `>=`, `>`, `<=`, `<`（`USING` 语法隐含 `>=`），且"每行最多匹配一行"；对右表**同一时间戳多行**的选择，文档未定义确定性顺序，实践上应先在右表按 (key, ts) 去重/聚合再 ASOF。若需严格"只用 t 之前的信息"，用 `>` 而非 `>=`。

### 5. arch 8.0 StationaryBootstrap 按簇/组重采样

**结论**：**不支持**簇/组/自定义 block 索引；三个 block bootstrap 只有 `block_size, *args, seed`。

**证据**：`[arch 8.0.0 Time-series Bootstraps, https://bashtage.github.io/arch/bootstrap/timeseries-bootstraps.html, 抓取 2026-09-07]` 可信度：高（官方文档，版本 8.0.0 明示）
- `StationaryBootstrap(block_size, *args[, seed])`、`CircularBlockBootstrap(...)`、`MovingBlockBootstrap(...)`；无 `clusters/groups/block_indices` 参数。
- 提供 `optimal_block_length(x)`；文档警告 MovingBlockBootstrap 不环绕、首尾欠采样，"It is not recommended"。

**反面证据 / 未完成**：预算内未抓 `tsbootstrap` 0.7.2 文档（搜索词未执行：tsbootstrap BlockBootstrap custom block indices）。已知信息：tsbootstrap 提供 `BlockBootstrap` 系列（Moving/Stationary/Circular/NonOverlapping + Bartletts/Hamming 等加权窗），block 由 `block_length`/`block_length_distribution` 生成，**同样未见按外部 group 索引切块的一手证据**——事件级/按簇重采样大概率要自写（对 event id 做 block 抽样后 index 回行）。

### 6. imodels 3.0 release 与 get_rules()

**结论**：最新 release 为 **v3.0.0，2024-08-03**（距今 25 个月无新 release）；`get_rules()` 输出示例**未取到**。

**证据**：`[imodels Releases, https://github.com/csinva/imodels/releases, 抓取 2026-09-07]` 可信度：高
- v3.0.0 (2024-08-03) "Full-fledged support and compatibility across models"；v2.0.0 (2023-10-15) numpy 2 兼容；v1.4.5 (2023-05-23)；v1.4.3 (2023-03-12)；v1.4.2 (2023-02-29)。

**反面证据**：预算内未抓 API 页（搜索词未执行：imodels RuleFit `_get_rules` DataFrame columns）。记忆（未验证）：`RuleFitRegressor/Classifier._get_rules(exclude_zero_coef=True)` 返回 DataFrame，列为 `rule / type(linear|rule) / coef / support / importance`，rule 形如 `"X_3 <= 0.52 and X_7 > 1.1"`；`SkopeRules.rules_` 为 `(rule_str, (precision, recall, n))` 元组列表。需落地时核对。

---

### 对选型的含义（≤4 条）

1. **purge 语义分工已定**：需要事件级 `[t0,t1]` 重叠 purge 时只能用 `purgedcv`（skfolio 只有按行数 purge/embargo，对不等长 triple-barrier 标签会漏 purge）；但 purgedcv 极小众（31 stars、无第三方对拍测试），必须在 M3 门里**自写 10 行 AFML 参考 PurgedKFold 做数值对拍**，把它当"可替换实现"而非"可信黑盒"。
2. **skfolio 定位降级**为固定周期栅格（日频 bar、标签定长）下的 CPCV 备选；若标签跨度可变，直接排除。
3. **polars_ta 覆盖约 70%**：ROC/MA/STD/MAX/MIN/QTL/Rank/IMAX/IMIN/CORR/RSV 直接用；BETA/RSQR/RESI/CNTP/CNTN/SUMP/SUMN/WVMA 8 个需自写（回归三件套用 `ts_covariance`/`ts_std_dev` 组合，其余是 1–2 行表达式），工作量可控，不影响选型。
4. **两处待补一手证据**：DuckDB ASOF 平局/严格不等（取 `docs/current` 页）与 tsbootstrap 自定义 block；两者均有保守绕行方案（右表按 ts 预去重 + 用 `>`；按事件 id 自写 block 抽样），不阻塞立项，但应写进 M0 任务书的"验证项"。