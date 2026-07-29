from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable


Sleep = Callable[[float], None]


@dataclass(frozen=True)
class CancelAllSettings:
    max_attempts: int = 3
    inter_order_delay_seconds: float = 0.05
    retry_delay_seconds: float = 0.05
    verify_attempts: int = 3
    verify_delay_seconds: float = 0.05


def cancel_all(
    venue: Any,
    *,
    account_id: str | None = None,
    instrument_ids: Iterable[str] | None = None,
    settings: CancelAllSettings | dict[str, Any] | None = None,
    sleep: Sleep = time.sleep,
) -> dict[str, Any]:
    cfg = _settings(settings)
    instruments = tuple(instrument_ids) if instrument_ids is not None else None
    initial_orders = _list_working_orders(venue, account_id=account_id, instrument_ids=instruments)
    order_results = []
    for index, order in enumerate(initial_orders):
        order_results.append(_cancel_one(venue, order, settings=cfg, sleep=sleep))
        if index < len(initial_orders) - 1:
            _sleep(sleep, cfg.inter_order_delay_seconds)

    remaining = _verify_no_working_orders(
        venue,
        account_id=account_id,
        instrument_ids=instruments,
        settings=cfg,
        sleep=sleep,
    )
    status = "completed" if not remaining else "partial"
    return {
        "status": status,
        "orders": order_results,
        "remaining_working_orders": [_order_to_dict(order) for order in remaining],
        "verification": {
            "phase": "venue_final_verification",
            "working_order_count": len(remaining),
        },
    }


def _cancel_one(
    venue: Any,
    order: Any,
    *,
    settings: CancelAllSettings,
    sleep: Sleep,
) -> dict[str, Any]:
    order_id = _order_id(order)
    last_error: str | None = None
    attempts = max(settings.max_attempts, 1)
    for attempt in range(1, attempts + 1):
        try:
            result = _call_cancel_order(venue, order)
            return {
                "order_id": order_id,
                "status": "cancel_requested",
                "attempts": attempt,
                "result": result if isinstance(result, dict) else {"accepted": True},
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt < attempts:
                _sleep(sleep, settings.retry_delay_seconds)
    return {
        "order_id": order_id,
        "status": "failed",
        "attempts": attempts,
        "error": last_error or "cancel failed",
    }


def _verify_no_working_orders(
    venue: Any,
    *,
    account_id: str | None,
    instrument_ids: tuple[str, ...] | None,
    settings: CancelAllSettings,
    sleep: Sleep,
) -> list[Any]:
    remaining: list[Any] = []
    attempts = max(settings.verify_attempts, 1)
    for attempt in range(1, attempts + 1):
        remaining = _list_working_orders(
            venue,
            account_id=account_id,
            instrument_ids=instrument_ids,
        )
        if not remaining:
            return []
        if attempt < attempts:
            _sleep(sleep, settings.verify_delay_seconds)
    return remaining


def _list_working_orders(
    venue: Any,
    *,
    account_id: str | None,
    instrument_ids: tuple[str, ...] | None,
) -> list[Any]:
    method = getattr(venue, "list_working_orders", None)
    if not callable(method):
        raise RuntimeError("venue does not expose list_working_orders")
    try:
        return list(method(account_id=account_id, instrument_ids=instrument_ids))
    except TypeError:
        return list(method())


def _call_cancel_order(venue: Any, order: Any) -> Any:
    method = getattr(venue, "cancel_order", None)
    if not callable(method):
        raise RuntimeError("venue does not expose cancel_order")
    try:
        return method(order)
    except TypeError:
        return method(order_id=_order_id(order), instrument_id=_order_field(order, "instrument_id"))


def _settings(settings: CancelAllSettings | dict[str, Any] | None) -> CancelAllSettings:
    if settings is None:
        return CancelAllSettings()
    if isinstance(settings, CancelAllSettings):
        return settings
    return CancelAllSettings(
        max_attempts=int(settings.get("max_attempts", settings.get("max_cancel_attempts", 3))),
        inter_order_delay_seconds=float(settings.get("inter_order_delay_seconds", 0.05)),
        retry_delay_seconds=float(settings.get("retry_delay_seconds", 0.05)),
        verify_attempts=int(settings.get("verify_attempts", 3)),
        verify_delay_seconds=float(settings.get("verify_delay_seconds", 0.05)),
    )


def _order_id(order: Any) -> str:
    for field in ("order_id", "venue_order_id", "client_order_id", "id"):
        value = _order_field(order, field)
        if value:
            return str(value)
    raise ValueError("working order missing order id")


def _order_to_dict(order: Any) -> dict[str, Any]:
    if isinstance(order, dict):
        return dict(order)
    return {
        field: value
        for field in ("order_id", "venue_order_id", "client_order_id", "instrument_id", "status")
        if (value := getattr(order, field, None)) is not None
    }


def _order_field(order: Any, field: str) -> Any:
    if isinstance(order, dict):
        return order.get(field)
    return getattr(order, field, None)


def _sleep(sleep: Sleep, seconds: float) -> None:
    if seconds > 0:
        sleep(seconds)
