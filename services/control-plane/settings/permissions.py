"""RBAC and dangerous-change checks for order-management settings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .schema import default_settings


class SettingsPermissionDenied(PermissionError):
    pass


SETTINGS_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"read", "validate", "export"}),
    "operator": frozenset({"read", "validate", "export", "patch"}),
    "risk_admin": frozenset({"read", "validate", "export", "patch", "rollback", "import"}),
}

RISK_INCREASE_FIELDS = {
    "money.fixed_notional",
    "money.equity_fraction",
    "money.risk_per_trade_pct",
    "money.max_notional_per_order",
    "money.max_leverage",
    "money.max_open_positions",
    "money.max_instrument_exposure",
    "money.max_correlated_exposure",
    "money.max_total_risk_pct",
    "money.daily_loss_limit_pct",
    "money.max_drawdown_pct",
}
RISK_DECREASE_FIELDS = {
    "money.minimum_free_margin_pct",
    "money.reserve_balance_pct",
}
DISCONNECT_ACTION_RANK = {"halt": 0, "reducing": 1, "hold": 2}


def has_settings_permission(role: str, operation: str) -> bool:
    return str(operation) in SETTINGS_ROLE_PERMISSIONS.get(str(role), frozenset())


def assert_can_write_settings(
    actor: Mapping[str, Any],
    *,
    operation: str,
    before_settings: Mapping[str, Any] | None = None,
    after_settings: Mapping[str, Any] | None = None,
    confirm: bool | None = None,
    operator_signoff: str | None = None,
) -> dict[str, str]:
    actor_id = str(actor.get("actor_id") or "").strip()
    role = str(actor.get("role") or "").strip()
    if not actor_id or not role:
        raise SettingsPermissionDenied("settings actor required")
    if not has_settings_permission(role, operation):
        raise SettingsPermissionDenied(f"{role} cannot {operation} order-management settings")
    if requires_live_risk_confirmation(before_settings or {}, after_settings or {}):
        if confirm is not True or not str(operator_signoff or "").strip():
            raise SettingsPermissionDenied("live risk relaxation requires confirm=true and operator_signoff")
    return {"actor_id": actor_id, "role": role}


def assert_can_read_settings(actor: Mapping[str, Any], *, operation: str = "read") -> dict[str, str]:
    actor_id = str(actor.get("actor_id") or "").strip()
    role = str(actor.get("role") or "").strip()
    if not actor_id or not role:
        raise SettingsPermissionDenied("settings actor required")
    if not has_settings_permission(role, operation):
        raise SettingsPermissionDenied(f"{role} cannot {operation} order-management settings")
    return {"actor_id": actor_id, "role": role}


def requires_live_risk_confirmation(
    before_settings: Mapping[str, Any],
    after_settings: Mapping[str, Any],
) -> bool:
    before = _with_defaults(before_settings)
    after = _with_defaults(after_settings)
    before_mode = _path(before, "general.execution_mode")
    after_mode = _path(after, "general.execution_mode")
    if after_mode != "live" and before_mode != "live":
        return False

    for path in RISK_INCREASE_FIELDS:
        if _number(_path(after, path)) > _number(_path(before, path)):
            return True
    for path in RISK_DECREASE_FIELDS:
        if _number(_path(after, path)) < _number(_path(before, path)):
            return True
    if _path(before, "protection.require_stop") is True and _path(after, "protection.require_stop") is False:
        return True
    before_action = _path(before, "price_monitor.disconnect_action")
    after_action = _path(after, "price_monitor.disconnect_action")
    if DISCONNECT_ACTION_RANK.get(str(after_action), 0) > DISCONNECT_ACTION_RANK.get(str(before_action), 0):
        return True
    return False


def _with_defaults(settings: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    merged = default_settings()
    for category, fields in (settings or {}).items():
        if category not in merged or not isinstance(fields, Mapping):
            continue
        for field_name, value in fields.items():
            if value is not None and field_name in merged[category]:
                merged[category][field_name] = value
    return merged


def _path(settings: Mapping[str, Any], dotted: str) -> Any:
    category, field_name = dotted.split(".", 1)
    fields = settings.get(category, {})
    if not isinstance(fields, Mapping):
        return None
    return fields.get(field_name)


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
