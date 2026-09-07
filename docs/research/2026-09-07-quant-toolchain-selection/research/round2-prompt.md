# 第 2 轮 · 关键验证（2026-09-07）

已确认结论（直接采信，不重复调研）：见主笔记"第 1 轮总摘要"。本轮对每个领先候选找决定性证据 + 主动搜负面证据，经不起验证的淘汰并留档理由。

- R2-1（主会话本机）：下载 nautilus_trader 1.227.0 sdist，grep matching_engine / simulated_exchange 源码核 MARK_PRICE 触发、funding、post_only、IOC、bar 拆分顺序、外部提交路径。
- R2-2（agent）：Telegram 抓取验证 — Telethon Codeberg 发版、2025+ FloodWait 实测帖、tdl 导出字段、TDesktop 导出限制、2025-05 freeze 机制、GramJS 归档/teleproto 状态。
- R2-3（agent）：抽取/标注验证 — 三家官方价格页；Label Studio 最新版与多字段动态表单模板；Argilla 维护状态；截图价格 OCR 相关评测。
- R2-4（agent）：统计/协议验证 — purgedcv purge 定义与测试；skfolio CPCV 语义；OpenTimestamps python 客户端状态；polars_ta 覆盖 Alpha158 算子程度；DuckDB ASOF 平局语义。
