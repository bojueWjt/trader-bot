"""quant_lab.data —— Telegram 消息 → episode 的产线（GOAL-1 / G1 窗口）。

分层：bronze(只读带哈希) → silver(去重/抽取/规范化/链接) → gold(episode 描述图 + 决策快照)。
契约：`contracts/research-schema.md`（v1，§9 为准）。原因码见 `reasons.Reason`（冻结后只增不改）。

铁律：
1. 零生产凭据：不 import `services/*`，不读生产 session，不连生产库。
2. 原始层只读带哈希；隔离不删除；不填零不猜值。
3. 三时钟不可省；as-of 顺序未知取严格 `<`。
4. 每条丢弃有且只有一个互斥 primary 原因码，进损耗表。
5. 不用后见之明修结局；"最终仍存活"不能当入组条件。
"""
from __future__ import annotations

__version__ = "0.1.0"

#: 产线模块顺序（清洗层 1..5 + 链接/生命周期 6）
PIPELINE = ("normalize", "dedup", "extract", "validate", "linker", "lifecycle")

__all__ = ["PIPELINE", "__version__"]
