from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    from app.dependencies import signal_store as get_signal_store
except ImportError:  # pragma: no cover - supports package-style imports in external callers.
    from apps.api.app.dependencies import signal_store as get_signal_store

from app.services.signal_parser import ParsedSignal, SignalStatus
from app.services.signal_store import SignalStore


@dataclass(frozen=True)
class DailySnapshot:
    date: str
    account: dict[str, Any]
    trades: list[dict[str, Any]]
    signals: list[dict[str, Any]]
    risk: dict[str, Any]
    positions: list[dict[str, Any]]
    collected_at: str


async def build_snapshot(
    date: str,
    *,
    request: Any | None = None,
    signal_store: SignalStore | None = None,
) -> DailySnapshot:
    store = signal_store
    if store is None and request is not None:
        store = get_signal_store(request)

    return DailySnapshot(
        date=date,
        account=_collect_account(),
        trades=_collect_trades(),
        signals=_collect_signals(store),
        risk=_collect_risk(),
        positions=_collect_positions(),
        collected_at=_now_iso(),
    )


def snapshot_is_complete(snapshot: DailySnapshot) -> bool:
    return all(
        hasattr(snapshot, field)
        for field in ("date", "account", "trades", "signals", "risk", "positions", "collected_at")
    )


def _collect_account() -> dict[str, Any]:
    return {}


def _collect_trades() -> list[dict[str, Any]]:
    return []


def _collect_risk() -> dict[str, Any]:
    return {}


def _collect_positions() -> list[dict[str, Any]]:
    return []


def _collect_signals(store: SignalStore | None) -> list[dict[str, Any]]:
    if store is None:
        return []

    repository = getattr(store, "repository", None)
    if repository is None or not hasattr(repository, "list_by_status"):
        return []

    signals: list[ParsedSignal] = []
    seen_ids: set[str] = set()
    for status in SignalStatus:
        for signal in repository.list_by_status(status):
            if signal.signal_id in seen_ids:
                continue
            seen_ids.add(signal.signal_id)
            signals.append(signal)

    signals.sort(key=lambda signal: (_signal_time(signal), signal.signal_id), reverse=True)
    return [_signal_to_dict(signal) for signal in signals]


def _signal_to_dict(signal: ParsedSignal) -> dict[str, Any]:
    return {
        "signal_id": signal.signal_id,
        "source": signal.source,
        "pair": signal.pair_freqtrade,
        "side": signal.side,
        "status": signal.status.value,
        "received_at": _datetime_iso(signal.received_at),
        "entry_mode": signal.entry.mode,
        "entry_price": signal.entry.primary_price,
        "stop_loss": signal.stop_loss,
        "take_profits": [take_profit.price for take_profit in signal.take_profits],
        "leverage": {
            "min": signal.leverage.min,
            "max": signal.leverage.max,
            "selected": signal.leverage.selected,
        },
    }


def _signal_time(signal: ParsedSignal) -> datetime:
    received_at = signal.received_at
    if received_at.tzinfo is None:
        return received_at.replace(tzinfo=timezone.utc)
    return received_at.astimezone(timezone.utc)


def _datetime_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
