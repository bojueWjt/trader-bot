# Zone Ladder 再校准 Runbook

## 触发条件

累计 >=50 条新鲜 `entry_type='zone'` 信号后重跑本流程。信号时间以 `raw_messages.source_received_at` 为准，不使用回放写入时的 `hermes_decisions.created_at` 作为行情起点。

## Live 重跑

准备 hk v3 Postgres 连接串和本地 K 线缓存目录：

```bash
export HK_V3_DB_URL='postgresql://USER:PASSWORD@HOST:5432/DBNAME'
export KLINE_CACHE_DIR=/var/tmp/hermes-zone-klines
```

直接从 hk v3 库读取信号并回放 Binance Vision 1m K 线：

```bash
python -m scripts.analysis.zone_penetration_stats \
  --db-url "$HK_V3_DB_URL" \
  --cache-dir "$KLINE_CACHE_DIR" \
  --base-url https://data.binance.vision \
  --market um \
  --window-hours 24 \
  --output zone-penetration-stats-live.json
```

只导出 DB 信号，不下载 K 线、不跑统计：

```bash
python -m scripts.analysis.zone_penetration_stats \
  --db-url "$HK_V3_DB_URL" \
  --export zone-signals-export.json
```

用离线导出的信号复跑：

```bash
python -m scripts.analysis.zone_penetration_stats \
  --signals-json zone-signals-export.json \
  --cache-dir "$KLINE_CACHE_DIR" \
  --base-url https://data.binance.vision \
  --market um \
  --window-hours 24 \
  --output zone-penetration-stats-offline.json
```

`base_url` 必须可配置；默认使用 `https://data.binance.vision`。不要把本流程绑定到 `fapi.binance.com`，该域名在本地和多数运行环境不可达。`XAUUSDT` 不在 Binance UM 日度 K 线归档内，脚本会跳过并在 `skipped` 中记录。

## 输出解读

核心字段：

- `sample_count`: 成功拿到行情并纳入统计的 zone 信号数。
- `no_retrace`: TP1 方向目标在触及 zone near 沿前先到的样本数和比例。
- `retrace`: 已回踩 near 沿的样本数、中位刺入深度、打穿远沿比例。`penetration_depth=1.0` 表示正好到远沿，`>1.0` 表示打穿整个区间。
- `fill_rates`: 0%、25%、50%、85%、100% 深度挂单在信号后窗口内的触及成交率。
- `chase_window`: 信号后第一根 1m K 线开盘价是否落在 0.35% 追入窗口内的命中数。
- `zone_height`: 区间高度的中位 `price%`，以及区间高度相对 1h ATR 的中位倍数。
- `per_signal`: 每条信号的分类、刺入深度、各深度成交布尔值，用于抽查异常样本。
- `skipped`: 无行情源、无 post-signal K 线或 Binance 归档缺失的样本。

首轮 live 验收基准来自 2026-07-02 §7：`n=17` 新鲜 zone 信号；约 `7/17` no-retrace；`10/17` 回踩，中位刺入约 `130%`，其中 `8/10` 打穿；0% 到 100% 深度成交率约 `59% -> 47%`，且 0% 与 25% 成交集合相同；0.35% 追入窗口约命中 `3-5/17`；中位区间高度约 `1.18% price / 1.42 ATR(1h)`。新样本不会要求逐字相等，但结构性偏离必须复审。

## 强制复审

每次 >=50 条样本重跑后必须做以下复审，不允许只看脚本退出码：

1. 前置权重 vs 压深：比较 0%、25%、50%、85%、100% 的成交率、共同成交集合和 per-signal 均价改善空间。若 50% 或 85% 的成交率相对 0% 下降小于 10pp，同时回踩样本中 `penetration_depth >= 1.0` 仍占多数，必须评审是否把更多权重压到深档。
2. 参与优先是否仍成立：若 no-retrace 比例 >=35%，且 0%/25% 明显多于深档成交，保留前置权重有数据基础；若 no-retrace 比例降到 <25%，优先讨论降低 T1 权重。
3. 0.35% 追入窗口：若 `chase_window.hits / sample_count` 明显低于 15% 或高于 40%，抽查 `per_signal`，判断窗口是否过窄或过宽。扩大窗口前必须同时检查滑点和止损距离。
4. `-0.2h` 击穿缓冲：若回踩样本中打穿远沿比例仍 >=60%，保留机械失效缓冲；若打穿比例显著下降且 near/mid 成交后常反转，单独提案复审缓冲。
5. 样本质量：确认 `skipped` 中没有 BTCUSDT/SOLUSDT 这类应可下载的 Binance UM symbol；如有，先修复缓存或网络问题再解读统计。

复审结论需要记录：样本时间范围、`sample_count`、跳过样本、五档成交率、no-retrace 比例、回踩中位刺入、打穿比例、追入窗口命中数，以及是否调整三档权重、档位深度、追入窗口或击穿缓冲。
