# 第 3 轮对抗审稿（工具调用 8 次，按预算停止搜索）

## A. Telethon 维护模式 + Codeberg

**反驳句**：「Telethon 的 layer 落后不是'慢慢降级'，而是断崖式失效——历史上 Telegram 抬高最低 layer 门槛时，旧客户端直接收到 RPCError 406 `UPDATE_APP_TO_LOGIN`，登录整个失败；作者当时是'赶工发布'一个刚够用的 layer 才救回来的，而现在 GitHub 仓库已归档、v1 只维护，你们把它当主抓取器等于把整条数据管线押在一个人是否及时赶工上。」

**证据**：
- [telethon: RPCError 406: UPDATE_APP_TO_LOGIN · Issue #3226, https://github.com/LonamiWebs/Telethon/issues/3226, 2021] — 可信度高（一手 issue，历史先例）
- [Telethon 1.44.0 Changelog（"rushed release … layer recent enough to not fail with UPDATE_APP_TO_LOGIN, but still not the latest"）, https://docs.telethon.dev/en/stable/misc/changelog.html, 2025–2026] — 可信度高（作者自述）
- [GitHub 仓库 2026-02-21 归档只读，迁 Codeberg（安装方式变为 `pip install https://codeberg.org/Lonami/Telethon/archive/v1.zip`）, https://codeberg.org/Lonami/Telethon/issues, 2026-02] — 可信度高
- [Changes to login and sign-up for third-party applications · Issue #4050, https://github.com/LonamiWebs/Telethon/issues/4050, 2023] — 可信度高，说明第三方登录政策会单方面变更

**未找到的反例**：2026 年"layer 落后导致无法读频道/媒体"的具体 issue（搜索词：`Telethon 2026 layer outdated channel messages Codeberg`）。

**我仍认为的最大风险**：不是读不到消息，而是**新 constructor 解析失败**——频道历史里一旦出现旧 schema 不认识的媒体类型（付费媒体、新贴纸/故事引用等），`iter_messages` 会在中途抛 `TypeNotFoundError`，而不是跳过。全量历史抓取一次性跑几万条，一个未知 constructor 就让整个游标断掉。另外「主号冻结 = session 作废」严重低估了后果：冻结的是**账号**不是 session，你们拿"有历史的主号"配一个全新 API_ID 去拉全量历史，正是 2025-05 后触发冻结的典型模式。

---

## B. 两厂商 Batch 交叉抽取取分歧

**反驳句**：「ICML 2025 用 350 个模型在三个数据集上证明：两个模型都错时，它们答成同一个错答案的概率约 60%，且越大越准的模型跨厂商相关性越高——所以'两家一致'不是正确性证书，交叉抽取只能捞到分歧那一小撮，真正致命的是你们打算不看的'一致集'。」

**证据**：
- [Correlated Errors in Large Language Models (Kim et al., ICML 2025), https://arxiv.org/pdf/2506.07962, 2025-06] — 可信度高（顶会、350 模型、三数据集）
- [Nine Judges, Two Effective Votes: Correlated Errors Undermine LLM Evaluation Panels, https://arxiv.org/html/2605.29800v1, 2026-05] — 可信度中高（预印本；九评委实际只有两票有效）
- [Three Models Agreed. It Was Still Wrong, https://www.digitalapplied.com/blog/cross-model-review-consensus-verification-2026, 2026] — 可信度低（博客，但案例贴合）
- 搜索摘要提到 2026-07 一篇 26.5 万样本审计：一致度与正确率 Spearman ρ 仅 0.20–0.59 — 可信度中（未核对原文）

**具体打击点**：交易员频道的"模糊信号"（如"关注区域""分批止盈看情况"）恰恰是两个 LLM 会**同向脑补**的类型——都倾向把区间中点当入场价、把"看情况"补成默认止盈档。你们的成本估算 $0.1–5/千条说明预算完全不是约束，约束是"一致集的人工审计率"，这一项方案里是 0。

---

## C. Binance Vision 数据质量

**反驳句**：「data.binance.vision 的月包**曾静默缺失整整两天**（2022-04 期货 5m klines 多品种缺 04-01 00:00 至 04-02 23:55），而且 2025-01-01 起 SPOT 时间戳改为微秒、期货仍是毫秒——你们把这些直接丢进 DuckDB 做 ASOF JOIN，跨市场对齐会差 1000 倍而不报错。」

**证据**：
- [2022-04 monthly future 5m kline data missing 04-01 to 04-02 · Issue #297, https://github.com/binance/binance-public-data/issues/297, 2022] — 可信度高（一手 issue）
- [binance-public-data README：SPOT 数据自 2025-01-01 起时间戳为微秒, https://github.com/binance/binance-public-data/blob/master/README.md, 2025] — 可信度高（官方）
- [Receive Wrong Data Using get_historical_klines · Issue #1095, https://github.com/sammchardy/python-binance/issues/1095] — 可信度中（API 侧，非 Vision，但说明 kline 返回本身有不一致先例）

**未找到的反例**：markPriceKlines 与 REST API 不一致的报告（搜索词：`markPriceKlines data.binance.vision inconsistent API`）。

**我仍认为的最大风险**：(1) 旧期货 CSV 无表头、新文件有表头，同一目录混用，polars 推断会把第一行数据当表头吃掉；(2) 缺日不会报错，`ASOF JOIN` 会把信号对到缺口前最后一根 bar，止损触发时间被系统性推后；(3) 你们的止损口径是 mark price，但 markPriceKlines 只有 OHLC，无法区分 bar 内先到高还是先到低，"同 bar 双界"只能给区间不能给点估计——这是 D 的"悲观/乐观界"能落地的前提，要先承认。

---

## D. Nautilus 壳策略方案

**反驳句**：「Nautilus 官方自己在 Issue #2194 承认现有 FillModel 对'限价触及即成交'建模不够、要重做，而 EOD bar 回测权益曲线还被报过直接算错（#1476）——你们本机读源码得出的'乐观'结论是对的，但结论应该是'Nautilus 不适合做主引擎'，而不是'配个几百行差分器补丁'。」

**证据**：
- [Enhanced order-fill simulation in backtesting · Issue #2194, https://github.com/nautechsystems/nautilus_trader/issues/2194, 2025-01] — 可信度高（官方 issue，明确说要扩展 FillModel 做触及部分成交）
- [Backtest Equity with EOD bar data seems to be broken · Issue #1476, https://github.com/nautechsystems/nautilus_trader/issues/1476, 2024] — 可信度高

**未找到的反例**：批量"每计划一个 engine"性能抱怨（搜索词：`nautilus_trader BacktestEngine many small runs slow`）。

**我仍认为的最大风险**：
1. **主次倒置**：mark 触发、funding、同 bar 双界这三样对永续合约止损/盈亏是决定性的，全部只在自研差分器里有；Nautilus 那条线只是"没有这些的乐观版"。那么主数字来自几百行未经审计的自研代码，Nautilus 反而是审计对象——你们把它写反了。
2. **批量效率**：BacktestEngine 是为"一次长跑、多标的"设计的，每个计划起一个 engine 要重复加载数据、初始化 Rust 核心，几千个计划就是几千次冷启动；正确用法是一个 engine 一次长跑、壳策略按时间表调度全部计划、用 `client_order_id` 标签区分计划做 per-plan 归因。
3. **LGPL-3.0 是伪风险**：内部使用、不分发，LGPL 零义务；把它从风险表删掉，别浪费审阅者注意力。
4. "与生产同版本"的对齐价值很弱：壳策略永远不会是生产策略，版本一致换不来行为一致。

---

## E. DuckDB 单文件研究湖

**反驳句**：「DuckDB 官方文档写明 v1.4 出过临时性破坏兼容的变更（1.4.1 后又发现一个，到 1.4.2 才修），磁盘空间不足时会把库文件写坏且不可恢复（#9667），且它是单写者模型——如果你们的'研究湖'真的把规范数据放进 .duckdb 文件，一次并发写或一次升级就能让锁箱哈希对不上。」

**证据**：
- [Storage Versions and Format（v1.0–v1.5 默认写 storage version 64；跨版本迁移需 EXPORT/IMPORT）, https://duckdb.org/docs/current/internals/storage, 2026] — 可信度高（官方）
- [Low disk space can result in database corruption · Issue #9667, https://github.com/duckdb/duckdb/issues/9667, 2023] — 可信度高
- [Compatibility Issue Between Versions of DuckDB, https://discourse.julialang.org/t/compatablity-issue-between-versions-of-duckdb/105624] — 可信度中
- [Release v1.5.5 Bugfix（含 RLE corruption 报错改进）, https://github.com/duckdb/duckdb/releases/tag/v1.5.5, 2026] — 可信度高

**具体打击点**：方案写"Parquet + DuckDB"，但没写**谁是真身**。Parquet 是真身则 .duckdb 只是可丢弃缓存，上述风险全部消失；反之全部成立。这一句必须写死。

---

## F. Qlib Alpha158 "无前视"

**反驳句**：「Alpha158 的 158 个特征确实只用正向 `Ref`，但它整个 handler 的隐含约定是'日频、在 t 收盘后算特征、t+1 开盘交易'（标签是 `Ref($close,-2)/Ref($close,-1)-1`，#1514 里连作者都要解释为什么），搬到 24/7 永续 + 分钟级 Telegram 信号上，泄漏不会来自表达式，而来自你们把信号时间戳 ASOF 对到 bar 的 **open_time** 而不是 **close_time**——那是拿未来 5 分钟的 OHLC 算特征。」

**证据**：
- [Alpha158 Label Calculation (Why use close price 2 days from now?) · Issue #1514, https://github.com/microsoft/qlib/issues/1514] — 可信度高
- [Loading data by Alpha158 handler with my own data … Alpha158vwap 报错 · Issue #785, https://github.com/microsoft/qlib/issues/785] — 可信度高（vwap 变体在自定义数据上本就不稳）
- [Is it possible using alpha 158 outside qlib? · Issue #1564, https://github.com/microsoft/qlib/issues/1564] — 可信度中

**未找到的反例**：Alpha158 在非日频数据上的具体踩坑帖（搜索词：`qlib Alpha158 intraday minute bars`）。

**我仍认为的最大风险**：
1. `$vwap` 口径：qlib 数据集里 `$vwap` 是日 VWAP（缺失时部分采集器直接用 close 顶替）；Binance klines 没有 vwap，你们会用 `quote_volume/volume` 算 bar VWAP——两者不是一个量，"复现 Alpha158"在这一维已经不成立，要明说是"Alpha158 变体"。
2. 滚动窗口 5/10/20/30/60 是**天数**设计的；在 5m bar 上变成 25 分钟到 5 小时，特征语义全变，"158 全为单标的时序无前视"这句在语法上对、在语义上没意义。窗口应按墙钟时间重新指定。
3. EPV≥10、≤30 维 ⇒ 需要 ≥300 个**正事件**。频道全量历史有没有 300 个已闭合的可判定信号？方案里没有这个数字，特征维度上限应由它反推，而不是先定 30。

---

## G. purgedcv 单作者新库 / skfolio CPCV 与事件级标签不匹配

**反驳句**：「skfolio 的 CombinatorialPurgedCV 是按**连续时间索引**分折、面向组合收益序列，它不接受每个样本自己的 `t1`（事件结束时间），而 Telegram 信号是每单持仓期不同的重叠事件标签——拿它对拍等于用一个定义不同的东西校验另一个；purgedcv 0.1.6 是 2026-09-04 单作者刚发的，你们的'对拍'两边一个错口径一个没经过时间检验，结论是 AFML 那 40 行 `PurgedKFold(t1=...)` 必须是主实现，库只能当旁证。」

**证据**：
- [skfolio.model_selection.CombinatorialPurgedCV（k−p 训练折，索引式分折）, https://skfolio.org/generated/skfolio.model_selection.CombinatorialPurgedCV.html] — 可信度高（官方 API 文档）
- [purgedcv · PyPI（0.1.6）, https://pypi.org/project/purgedcv/, 2026-09-04] — 可信度高
- [purged-cross-validation paper.md（JOSS 投稿稿：mlfinlab 已闭源收费；timeseriescv 2018 后无发布且有已知正确性问题）, https://github.com/eslazarev/purged-cross-validation/blob/main/paper/paper.md, 2026] — 可信度中（作者自述，但符合公开事实）

**被漏掉的更稳替代**：
- `sklearn.model_selection.TimeSeriesSplit(gap=…)` 作为**零依赖 embargo 基线**（不做 t1 purge，但不会错）；
- 事件级**非重叠子采样**（AFML 第 4 章 sequential bootstrap / uniqueness 加权）——几百个事件时比 CPCV 更诚实；
- 走前 + embargo 作主 CV，CPCV 只作稳健性附录；样本量 <300 时 CPCV 的多路径会给出虚假的置信区间。

---

## 应改的结论（6 条）

1. **Telegram 主源改为 TDesktop 官方导出（JSON）**，作为一次性全量样本的规范来源；Telethon 降级为增量/对照抓取，且**用一个有历史的次号**跑，主号绝不碰新 API_ID；安装源固定 Codeberg tag，抓取器外套 `TypeNotFoundError` 跳过并记录，游标可续。
2. **交叉抽取改为"LLM + 确定性语法/正则解析器"这对去相关组合**（而非两家 LLM），并预注册"一致集人工抽审 10%，报告一致集错误率"为交付指标；两家 LLM 仅用于分歧集三方裁决。
3. **Binance Vision 入湖前加完整性闸门**：每文件校验官方 `.CHECKSUM`、按月核对 bar 数=期望数、缺口用日包回填、时间戳统一归一到毫秒（显式检测 µs）、逐文件检测有无表头；全部写进 manifest。
4. **回测主次对调**：自研差分模拟器（mark 触发、funding、同 bar 乐观/悲观界）是主引擎并必须有单元测试与对拍用例；Nautilus 只作交叉审计，且改为**单 engine 一次长跑、壳策略按时间表调度全部计划、按 order tag 归因**；LGPL 从风险表删除。
5. **写死"Parquet 是唯一真身，.duckdb 是可丢弃缓存"**：所有查询走 `read_parquet` 视图，重建脚本入库，单写者纪律，DuckDB 版本进 lockfile，升级只走 EXPORT/IMPORT。
6. **特征与 CV 重述**：信号→bar 的 ASOF JOIN 必须以 `close_time <= signal_ts` 对齐；Alpha158 改称"Alpha158 变体"、滚动窗按墙钟时间指定、`$vwap` 明确为 bar VWAP；先数出已闭合正事件数 N，特征维 ≤ N/10；主 CV 为自写 AFML `PurgedKFold(t1)` + embargo 的走前验证，purgedcv/skfolio 只作旁证，N<300 时不用 CPCV。

Sources: [Telethon #3226](https://github.com/LonamiWebs/Telethon/issues/3226) · [Telethon Changelog](https://docs.telethon.dev/en/stable/misc/changelog.html) · [Telethon Codeberg](https://codeberg.org/Lonami/Telethon/issues) · [Telethon #4050](https://github.com/LonamiWebs/Telethon/issues/4050) · [Correlated Errors in LLMs](https://arxiv.org/pdf/2506.07962) · [Nine Judges, Two Effective Votes](https://arxiv.org/html/2605.29800v1) · [Three Models Agreed](https://www.digitalapplied.com/blog/cross-model-review-consensus-verification-2026) · [binance-public-data #297](https://github.com/binance/binance-public-data/issues/297) · [binance-public-data README](https://github.com/binance/binance-public-data/blob/master/README.md) · [python-binance #1095](https://github.com/sammchardy/python-binance/issues/1095) · [nautilus_trader #2194](https://github.com/nautechsystems/nautilus_trader/issues/2194) · [nautilus_trader #1476](https://github.com/nautechsystems/nautilus_trader/issues/1476) · [DuckDB Storage](https://duckdb.org/docs/current/internals/storage) · [duckdb #9667](https://github.com/duckdb/duckdb/issues/9667) · [DuckDB v1.5.5](https://github.com/duckdb/duckdb/releases/tag/v1.5.5) · [Julia DuckDB compat](https://discourse.julialang.org/t/compatablity-issue-between-versions-of-duckdb/105624) · [qlib #1514](https://github.com/microsoft/qlib/issues/1514) · [qlib #785](https://github.com/microsoft/qlib/issues/785) · [qlib #1564](https://github.com/microsoft/qlib/issues/1564) · [skfolio CombinatorialPurgedCV](https://skfolio.org/generated/skfolio.model_selection.CombinatorialPurgedCV.html) · [purgedcv PyPI](https://pypi.org/project/purgedcv/) · [purged-cross-validation paper](https://github.com/eslazarev/purged-cross-validation/blob/main/paper/paper.md)