from __future__ import annotations

from dataclasses import asdict, is_dataclass
from decimal import Decimal
from typing import Any

from account_budget import AccountBudget
from exchange_filters import FilterResult
from position_sizing import SizingResult


SECRET_MARKERS = ("secret", "token", "password", "api_key", "apikey", "private_key")


def build_risk_explain(
    *,
    account_budget: AccountBudget,
    sizing_result: SizingResult,
    exchange_result: FilterResult | None = None,
    extra_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    explain = {
        "inputs": {
            "account_id": account_budget.account_id,
            "currency": account_budget.currency,
            "equity": account_budget.equity,
            "free_margin": account_budget.free_margin,
            "margin_ratio": account_budget.margin_ratio,
            "stale_reasons": account_budget.stale_reasons,
            "stop_distance_pct": sizing_result.stop_distance_pct,
            **(extra_inputs or {}),
        },
        "limits": {
            "risk_amount": sizing_result.risk_amount,
            "raw_notional": sizing_result.raw_notional,
            "allowed_notional": sizing_result.allowed_notional,
        },
        "final": {
            "status": sizing_result.status,
            "reason": sizing_result.reason,
            "quantity": sizing_result.quantity,
            "notional": sizing_result.notional,
        },
        "headroom": sizing_result.headroom,
        "checks": list(sizing_result.checks),
    }
    if exchange_result is not None:
        explain["exchange"] = {
            "status": exchange_result.status,
            "reason": exchange_result.reason,
            "checks": exchange_result.checks,
            "metadata": exchange_result.metadata,
        }
    return _safe(explain)


def _safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if is_dataclass(value):
        return _safe(asdict(value))
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            text_key = str(key)
            if any(marker in text_key.lower() for marker in SECRET_MARKERS):
                continue
            clean[text_key] = _safe(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value

