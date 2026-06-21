from __future__ import annotations

import time
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Callable, Iterable

from .position_identity import merge_position_snapshots, residual_positions


Sleep = Callable[[float], None]
CloseOne = Callable[..., Any]


@dataclass(frozen=True)
class CloseAllSettings:
    max_attempts: int = 3
    inter_position_delay_seconds: float = 0.05
    retry_delay_seconds: float = 0.05
    verify_attempts: int = 3
    verify_delay_seconds: float = 0.05


def close_all(
    conn,
    *,
    account_id: str,
    venue: Any,
    positions: Iterable[Any] | None = None,
    close_one: CloseOne | None = None,
    settings: CloseAllSettings | dict[str, Any] | None = None,
    sleep: Sleep = time.sleep,
) -> dict[str, Any]:
    cfg = _settings(settings)
    closer = close_one or _default_close_one
    open_positions = merge_position_snapshots(
        account_id,
        positions if positions is not None else _list_open_positions(venue, account_id=account_id),
    )
    target_keys = [position["position_key"] for position in open_positions]

    results = []
    for index, position in enumerate(open_positions):
        results.append(
            _close_position(
                closer,
                conn=conn,
                account_id=account_id,
                position=position,
                settings=cfg,
                sleep=sleep,
            )
        )
        if index < len(open_positions) - 1:
            _sleep(sleep, cfg.inter_position_delay_seconds)

    residual = _verify_flat(
        venue,
        account_id=account_id,
        target_keys=target_keys,
        settings=cfg,
        sleep=sleep,
    )
    flat = not residual
    return {
        "status": "completed" if flat else "partial",
        "positions": results,
        "residual_positions": residual,
        "verification": {
            "phase": "venue_final_verification",
            "flat": flat,
            "target_position_keys": target_keys,
        },
    }


def _close_position(
    close_one: CloseOne,
    *,
    conn,
    account_id: str,
    position: dict[str, Any],
    settings: CloseAllSettings,
    sleep: Sleep,
) -> dict[str, Any]:
    position_key = position["position_key"]
    attempts = max(settings.max_attempts, 1)
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            raw = _call_close_one(
                close_one,
                conn=conn,
                account_id=account_id,
                position_key=position_key,
                position=position,
            )
            result = _result_dict(raw)
            if result.get("success") is False or result.get("status") in {"failed", "partial"}:
                raise RuntimeError(str(result.get("error") or result.get("reason") or "close failed"))
            return {
                "position_key": position_key,
                "status": "closed",
                "attempts": attempt,
                "reduce_only": True,
                "result": result,
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt < attempts:
                _sleep(sleep, settings.retry_delay_seconds)
    return {
        "position_key": position_key,
        "status": "failed",
        "attempts": attempts,
        "reduce_only": True,
        "error": last_error or "close failed",
    }


def _verify_flat(
    venue: Any,
    *,
    account_id: str,
    target_keys: list[str],
    settings: CloseAllSettings,
    sleep: Sleep,
) -> list[dict[str, Any]]:
    residual: list[dict[str, Any]] = []
    attempts = max(settings.verify_attempts, 1)
    for attempt in range(1, attempts + 1):
        residual = residual_positions(
            account_id,
            target_keys,
            _list_open_positions(venue, account_id=account_id),
        )
        if not residual:
            return []
        if attempt < attempts:
            _sleep(sleep, settings.verify_delay_seconds)
    return residual


def _call_close_one(
    close_one: CloseOne,
    *,
    conn,
    account_id: str,
    position_key: str,
    position: dict[str, Any],
) -> Any:
    try:
        return close_one(
            conn=conn,
            account_id=account_id,
            position_key=position_key,
            position=position,
            reduce_only=True,
        )
    except TypeError:
        return close_one(conn, account_id, position_key)


def _default_close_one(*, conn, account_id: str, position_key: str, **_kwargs: Any) -> Any:
    from order_management.exits import close_position  # type: ignore

    return close_position(conn, account_id, position_key)


def _list_open_positions(venue: Any, *, account_id: str) -> list[Any]:
    method = getattr(venue, "list_open_positions", None)
    if not callable(method):
        raise RuntimeError("venue does not expose list_open_positions")
    try:
        return list(method(account_id=account_id))
    except TypeError:
        return list(method())


def _settings(settings: CloseAllSettings | dict[str, Any] | None) -> CloseAllSettings:
    if settings is None:
        return CloseAllSettings()
    if isinstance(settings, CloseAllSettings):
        return settings
    return CloseAllSettings(
        max_attempts=int(settings.get("max_attempts", settings.get("max_close_attempts", 3))),
        inter_position_delay_seconds=float(settings.get("inter_position_delay_seconds", 0.05)),
        retry_delay_seconds=float(settings.get("retry_delay_seconds", 0.05)),
        verify_attempts=int(settings.get("verify_attempts", 3)),
        verify_delay_seconds=float(settings.get("verify_delay_seconds", 0.05)),
    )


def _result_dict(result: Any) -> dict[str, Any]:
    if result is None:
        return {"accepted": True}
    if isinstance(result, dict):
        return dict(result)
    if is_dataclass(result):
        return asdict(result)
    data = getattr(result, "__dict__", None)
    if isinstance(data, dict):
        return dict(data)
    return {"result": str(result)}


def _sleep(sleep: Sleep, seconds: float) -> None:
    if seconds > 0:
        sleep(seconds)
