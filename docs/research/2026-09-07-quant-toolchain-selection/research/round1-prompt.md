# 第 1 轮 · 广度收集（2026-09-07，三视角并行）

统一背景：加密永续合约交易机器人；生产节点 NautilusTrader；watcher GramJS；生产库仅 836 条消息（2026-02 起），频道历史更早，需全量抓取；回测语义要求订单状态机/mark vs last/filters/post-only/IOC/同 bar 上下界/费用分列；样本数百级；单人 Python 项目。截止 2026-09-07，只看 2025-01 后信息。

## 视角 A：Telegram 抓取 + 标注流水线
1. 历史抓取库：Telethon / Pyrogram 系 / GramJS / tdl / Telegram Desktop 导出 / TDLib；按 channel+message id 精确拉取、date/edit_date/reply_to/媒体、FloodWait 口径、会话隔离、纯只读
2. 封号/合规：ToS、2025-2026 封号案例、备用只读账号 vs 主账号
3. LLM 结构化抽取：Claude/OpenAI/Gemini Batch 价格；Instructor/Outlines/BAML/LangExtract；截图视觉抽取；每百条成本
4. 人工标注工具：Label Studio / Argilla / doccano / Prodigy / 自建；预填草稿、版本、标注者时间戳、单人部署成本
5. 锁箱：DVC / git-annex / lakeFS / JSONL+sha256

## 视角 B：回测引擎 + 行情数据
1. 引擎：NautilusTrader BacktestEngine（FillModel、post-only/IOC/reduce-only、mark 触发、live 共享、永续 instrument、费用/资金费率）、vectorbt(PRO)、backtrader、hftbacktest、freqtrade、Backtesting.py、Zipline-reloaded、Lean、hummingbot、自研；能否直接消费外部订单计划
2. 数据源：Binance Vision（含 markPriceKlines 归档？）、API 资金费率/OI、Tardis/Kaiko/CoinAPI/Amberdata/CCData 价格、ccxt
3. 存储：Parquet+DuckDB / ClickHouse / Timescale / ArcticDB / QuestDB / Postgres
4. 同类"信号回测"项目与 low<=price 成交假设争议

## 视角 C：特征 + 研究协议 + 统计
1. 指标库：TA-Lib / pandas-ta / vectorbt / ta / tulipy / polars-talib / finta；防前视、多周期对齐、非价格特征
2. as-of join 与 point-in-time 校验工具
3. 预注册与锁箱：DVC / MLflow / W&B / Sacred+Hydra / Guild / lakeFS / git+JSONL+sha256
4. 统计：purged CV（mlfinlab 现状/skfolio/mlfinpy/timeseriescv）、arch block bootstrap、deflated Sharpe/PBO、多重检验、不平衡评估
5. 可解释规则：sklearn / imodels / skope-rules / interpret
6. Alpha-GPT 及同类 LLM 因子挖掘的数据结构可借用性

输出五件套：分节 markdown 表格；每事实 [来源, URL, 日期] + 可信度；查不到写明搜索词；≤6 条初步含义；直接返回全文。
