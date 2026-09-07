# Binance 永续历史数据源 + 单人研究湖存储选型（第 1 轮广度收集）

> 工具调用 12/12 已用完；以下 Binance 归档部分为**本机实测**（S3 listing + HEAD + 解压抽样，2026-09-07），其余为公开网页信息。可信度标注：★★★ 一手/实测，★★ 官方文档或官方页面，★ 第三方转述。

---

## 1. Binance 归档核实（data.binance.vision `futures/um`）

**URL 模式**（实测有效）：

- 月包：`https://data.binance.vision/data/futures/um/monthly/<type>/<SYMBOL>/[<interval>/]<SYMBOL>-[<interval>|<type>]-YYYY-MM.zip`
- 日包：`https://data.binance.vision/data/futures/um/daily/<type>/<SYMBOL>/[<interval>/]<SYMBOL>-[<interval>|<type>]-YYYY-MM-DD.zip`
- 目录枚举（无需登录）：`https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/futures/um/monthly/`
- 每个 zip 旁有同名 `.CHECKSUM` 文件。

| 数据类型 | 存在？ | 日包 | 月包 | 实测最早（BTCUSDT） | 实测最新 | 示例文件（HEAD 200） | 可信度 |
|---|---|---|---|---|---|---|---|
| klines | 是 | 是 | 是 | （已有脚本，未复测） | — | — | ★★★ |
| **markPriceKlines** | **是** | 是 | 是 | **2020-01（月包）** | 2026-08 月包 | `.../monthly/markPriceKlines/BTCUSDT/1m/BTCUSDT-1m-2025-06.zip`（1,017,240 B） | ★★★ |
| **indexPriceKlines** | 是 | 是 | 是 | 2020-01 | — | `.../monthly/indexPriceKlines/BTCUSDT/1m/BTCUSDT-1m-2020-01.zip` | ★★★ |
| premiumIndexKlines | 是 | 是 | 是 | 2020-01 | — | `.../monthly/premiumIndexKlines/BTCUSDT/1m/BTCUSDT-1m-2020-01.zip` | ★★★ |
| aggTrades | 是 | 是 | 是 | 2019-12-31（日包） | — | — | ★★★ |
| trades | 是 | 是 | 是 | — | — | — | ★★★ |
| bookTicker | 是 | 是 | 是 | 2023-05-16（日包） | — | — | ★★★ |
| bookDepth | 是 | **仅日包** | 否 | 2023-01-01 | — | — | ★★★ |
| **metrics**（持仓量等） | **是** | **仅日包** | **无月包目录** | **2020-09-01** | — | `.../daily/metrics/BTCUSDT/BTCUSDT-metrics-2025-06-01.zip`（11,099 B） | ★★★ |
| **fundingRate** | **是** | 目录存在但本次未见文件（prefix 空） | **是** | **2020-01** | 2026-08 月包 | `.../monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2025-06.zip`（946 B） | ★★★ |
| liquidationSnapshot | **未在 daily/monthly 目录中出现** | — | — | — | — | 搜索词：`binance vision liquidationSnapshot removed` | ★★★（目录枚举） |

**实测 CSV 结构**：

- `metrics`：**5 分钟粒度**，列 `create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio`。示例行 `2025-06-01 00:00:00,BTCUSDT,83624.198,8736988614.45,...`。注意时间戳是**字符串日期**而非毫秒。
- `fundingRate`：列 `calc_time,funding_interval_hours,last_funding_rate`，示例 `1748736000001,8,-0.00000582`（毫秒时间戳；含 `funding_interval_hours`，可直接处理 4h/8h 变更）。
- `markPriceKlines` 1m：与 klines 同 12 列布局（open/high/low/close 为标记价，成交量列为 0）。[Binance Public Data 仓库约定，未本轮复核]

**合约 REST API 回溯限制**：

| 端点 | 限制 | 来源 |
|---|---|---|
| `GET /fapi/v1/fundingRate` | `limit` 默认 100、最大 1000；不传时间返回最近记录；可用 `startTime/endTime` 分页回溯到上市 | [Get Funding Rate History, https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History, 2026] ★★ |
| `GET /futures/data/openInterestHist` | **仅提供最近 1 个月**数据；period 5m…1d；不传时间返回最新 | [Open Interest Statistics, https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics, 2026] ★★ |
| 结论 | OI 历史必须走 `daily/metrics` 归档（2020-09 起、5m 粒度），API 只能补最近 30 天 | 实测 + 官方文档 |

---

## 2. 第三方数据源

| 名称 | 覆盖（mark price / 资金费率 / 清算） | 2026 最小套餐价格口径 | 免费层 | 来源 | 可信度 |
|---|---|---|---|---|---|
| **Tardis.dev** | Binance USDS-M 自 2019-11-17 起；Derivatives 计划含 trades/L2/quotes/funding/mark price/liquidations（原始 WS 流回放） | 官网价目：Academic $350/月（Perpetuals，仅季付/年付）、**Solo $700/月（Perpetuals）**、Pro $1,000/月、Business $3,000/月；All Exchanges Solo $1,200/月 | 无免费层；文档提及每月首日样本可免费下载（本轮未复核） | [tardis.dev 首页价目, https://tardis.dev/, 2026-09-07 抓取]；[Binance USDS-M Futures, https://docs.tardis.dev/historical-data-details/binance-futures] | ★★ |
| **Kaiko** | L1/L2 tick、衍生品参考数据含 mark/index price、合约规格；2026 已并购 Amberdata | 无公开自助价目，销售询价 | 无 | [Kaiko Reference Data, https://www.kaiko.com/products/reference-data, 2026]；[CoinAPI 博客提及并购, https://www.coinapi.io/blog/best-institutional-crypto-market-data-api, 2026] | ★★ / ★ |
| **CoinAPI** | 资金费率（Binance 等自 2021 初）、OI、清算、IV 指标 | 按量 Pay-As-You-Go；Business $599/月（10 万次/日） | 注册送 $25 一次性额度（需绑卡） | [Is there a free tier, https://docs.coinapi.io/general/faq/Billing-Payment-and-Subscriptions/Is-there-a-free-tier-for-CoinAPI-Market-Data-API, 2026]；[Historical funding rates, https://www.coinapi.io/blog/historical-crypto-funding-rates-api-coinapi] | ★★ |
| **Amberdata** | CEX 行情 + 链上/DeFi 为主 | 已被 Kaiko 收购，价格随 Kaiko 询价 | 无 | 同 Kaiko 行 | ★ |
| **CCData / CryptoCompare（现 CoinDesk Data）** | data-api 有 Futures/Options 板块（含 funding、OI） | **2026-05-21 起免费层完全下线，全部转销售询价** | 已取消 | [CoinDesk API alternatives, https://www.coingecko.com/learn/coindesk-api-alternatives, 2026]；[api-evangelist profile, https://github.com/api-evangelist/cryptocompare] | ★ |
| **Coinglass** | 清算历史/聚合清算、OI OHLC 历史、资金费率、多空比，30+ 交易所；API v4 | **Hobbyist $29/月**、Startup $79/月、Standard $299/月（商用与更深历史需 Standard） | 无免费 API 层 | [CoinGlass pricing, https://www.coinglass.com/pricing]；[Liquidation history, https://docs.coinglass.com/reference/liquidation-history]；[dev.to review, https://dev.to/great-time-flies/coinglass-api-review-2026-is-it-worth-it-for-crypto-quant-traders-2bcf, 2026] | ★★ / ★ |
| **ccxt 免费拉取** | 直连 Binance 公共端点：`fetchFundingRateHistory`（可回溯）、`fetchOpenInterestHistory`（受 30 天限制）、mark price 仅现价 K 线 API `markPriceKlines`（有 1500 根/次限制，可分页） | $0 | 全免费，受 IP 限速 | [python-binance Endpoints, https://github.com/sammchardy/python-binance/blob/master/Endpoints.md]；Binance 官方文档同上 | ★★ |

> 未找到公开信息：Kaiko/Amberdata 2026 自助价目（搜索词 `kaiko pricing 2026 self-serve`）；CoinDesk Data 当前 Futures 板块最低套餐价（搜索词 `coindesk data api pricing 2026 futures`）；Tardis 单交易所（Binance-only）计划价格（搜索词 `tardis.dev single exchange plan price`）。

---

## 3. 研究湖存储候选

| 候选 | 最新版本（2026） | 许可证 | 个位数 GB 时序查询体验 | pandas/polars 集成 | ASOF JOIN | 不可变快照/版本化 | 单人运维成本 | 文档 |
|---|---|---|---|---|---|---|---|---|
| **Parquet + DuckDB** | **1.5.5**（2026-07-22；1.5.0 2026-03-09） | MIT | 极佳：单进程直读 Parquet，GB 级 1m K 线秒级聚合 | 原生：`duckdb.sql(...).df()/.pl()`，可直接查询 pandas/polars/Arrow 对象零拷贝 | **原生 `ASOF JOIN`**（含 `ASOF LEFT JOIN`），正是 mark vs last 对齐场景 | 文件级：按 `symbol/month` 分区 Parquet 即天然不可变；需要表级版本可加 DuckLake（1.5.2 起支持 DuckLake v1.0） | **最低**：无服务进程、pip 即装 | [Announcing DuckDB 1.5.5, https://duckdb.org/2026/07/22/announcing-duckdb-155]；[AsOf Join guide, https://duckdb.org/docs/0.10/guides/sql_features/asof_join]；[DuckDB ASOF blog, https://duckdb.org/2023/09/15/asof-joins-fuzzy-temporal-lookups] ★★ |
| ClickHouse | **26.7.5 stable**（2026-08-21） | Apache 2.0 | 很强，但个位数 GB 用不满；chDB（嵌入式）可替代服务端 | clickhouse-connect（约 200 万月下载）返回 pandas/Arrow；polars 经 Arrow | 支持 `ASOF JOIN`（引擎原生） | 无内建版本化；可用分区 + 只读表模拟 | 中：需常驻服务、配置较多；chDB 可降到低 | [ClickHouse wiki, https://en.wikipedia.org/wiki/ClickHouse]；[对比文, https://www.index.dev/skill-vs-skill/database-timescaledb-vs-clickhouse-vs-questdb, 2026] ★ |
| TimescaleDB | **2.26.0**（2026-03-24；2.24.0 2025-12-08） | Apache 2.0 + Timescale License（压缩/连续聚合等在 TSL 下） | 好但 Postgres 行存对 1m K 线聚合不如列存；GB 级可接受 | psycopg/SQLAlchemy → pandas；polars 经 `read_database` | 无原生 ASOF；用 `LATERAL` + `ORDER BY ts DESC LIMIT 1` 模拟 | 无内建版本化 | 中：要维护一套独立 PG 实例（研究湖不能复用生产 PG） | [Release 2.26.0, https://github.com/timescale/timescaledb/releases/tag/2.26.0]；[Tiger Data changelog, https://www.tigerdata.com/docs/about/latest/changelog] ★★ |
| **ArcticDB** | **3.0.x**（docs 站点已有 3.0.0 版本页） | **BSL 1.1**（非生产/非数据库服务免费；Change Date 后转 Apache 2.0） | 面向 DataFrame 的时序按 `date_range` 切片极快，LMDB/S3 后端 | **写入 pandas，读出 pandas/polars/PyArrow** | 无 SQL ASOF；靠 pandas `merge_asof` 在内存做 | **内建**：每次写入生成版本，支持 time-travel `as_of` | 低：pip 安装、本地 LMDB 目录；但许可证需确认"研究用"边界 | [ArcticDB README, https://github.com/man-group/ArcticDB/blob/master/README.md]；[LICENSE, https://github.com/man-group/ArcticDB/blob/master/LICENSE.txt]；[docs 3.0.0, https://docs.arcticdb.io/3.0.0/] ★★ |
| QuestDB | **9.4.3**（9.0 于 2025-07-15；9.1.0 已发） | Apache 2.0 | 很强的写入与 `SAMPLE BY` 时序语法；GB 级大材小用 | PG wire → pandas；polars 经 `read_database`；ILP 写入 | **原生 `ASOF JOIN`/`LT JOIN`** | 无内建版本化 | 中：常驻 JVM 服务 | [QuestDB 9.1.0 release, https://github.com/questdb/questdb/releases/tag/9.1.0]；[QuestDB best TSDB 2026, https://questdb.com/blog/best-time-series-databases/] ★★ |
| 直接 Postgres（新实例） | PG 17/18（本轮未查具体） | PostgreSQL License | 可用，但无压缩、无时序语法，1m 全市场 K 线聚合最慢 | 同 TimescaleDB | 无原生 ASOF（LATERAL 模拟） | 无 | 中：独立实例运维；与生产 PG 隔离要求下无优势 | 未专门检索（搜索词 `postgresql 18 release 2026`） | ★ |

> 未找到公开信息：ArcticDB 2026 精确最新 tag（搜索词 `man-group ArcticDB releases 2026 v3`）；polars 与 QuestDB/ClickHouse 的官方原生连接器（当前均走 Arrow/PG wire）。

---

## 对本项目的初步含义（≤4 条）

1. **历史 mark price 1m K 线可以免费获得**：data.binance.vision `futures/um/{monthly,daily}/markPriceKlines/<SYMBOL>/1m/` 自 2020-01 起完整存在，月包约 1 MB/symbol/月，与现有 klines 下载脚本同 URL 骨架，只需把 `klines` 换成 `markPriceKlines`（同理 `indexPriceKlines`、`premiumIndexKlines`）。止损触发基准建模不需要任何付费源。
2. **资金费率与持仓量也免费但口径不同**：`monthly/fundingRate` 自 2020-01 起，含 `funding_interval_hours` 列；OI 只能走 `daily/metrics`（**仅日包、5 分钟粒度、2020-09 起**），REST `openInterestHist` 仅回溯 1 个月，不能作为历史来源。清算数据 Binance 归档中已无 `liquidationSnapshot`，若需要清算只能付费（Coinglass $29/月起，或 Tardis Solo $700/月）。
3. **存储首选 Parquet + DuckDB 1.5.x**：MIT、零运维、原生 `ASOF JOIN` 直接解决 mark/last/funding 三表对齐；按 `type/symbol/year-month` 分区的 Parquet 即不可变快照。ArcticDB 的内建版本化有吸引力，但 BSL 1.1 许可需确认研究用途边界，且缺 SQL ASOF；ClickHouse/QuestDB/Timescale 在个位数 GB 单人场景下都是常驻服务的额外运维负担。
4. **付费源全部超出单人预算量级**：Tardis 最低 $350/月（学术、季付起）、Solo $700/月；CoinAPI 无真正免费层；CCData 2026-05 已取消免费层；Kaiko 并购 Amberdata 后仅询价。第 2 轮如需补充，优先核实 Coinglass Hobbyist 层的历史深度与商用限制，以及 Binance `daily/fundingRate` 目录为何为空（月包已足够）。