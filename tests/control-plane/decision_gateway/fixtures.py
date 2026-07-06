from __future__ import annotations

from datetime import datetime, timezone

from zone_ladder import MarketContext, RiskContext, ZoneSignal

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
VALID_UNTIL = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)

# 待单元 5 收编 17 样本后扩充。
SHUQIN_BTC_SHORT = ZoneSignal(
    side="short",
    price_min=62300.0,
    price_max=62700.0,
    stop_loss=63100.0,
    take_profits=(61500.0, 60800.0, 60000.0),
    valid_until=VALID_UNTIL,
)
SHUQIN_BTC_MARKET = MarketContext(
    p0=61662.0,
    now=NOW,
    price_increment="0.10",
    quantity_increment="0.001",
)

SHUQIN_SOL_SHORT = ZoneSignal(
    side="short",
    price_min=82.0,
    price_max=83.0,
    stop_loss=85.0,
    take_profits=(79.0, 76.0, 70.0),
    valid_until=VALID_UNTIL,
)
SHUQIN_SOL_MARKET = MarketContext(
    p0=82.5,
    now=NOW,
    price_increment="0.01",
    quantity_increment="0.001",
)

FIXTURE_RISK = RiskContext(
    risk_budget_usd=100.0,
    max_notional=1_000_000.0,
    max_leverage=5.0,
)
