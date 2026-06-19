"""External signal parsing, risk checks, runtime state, and reservations."""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import unquote, urlparse

from freqtrade.signal_strategy.domain import (
    ApprovalResult,
    EntryPlan,
    LeveragePlan,
    MediaAsset,
    ReservationResult,
    RiskPolicyResult,
    SignalStatus,
    TakeProfit,
    TradingSignal,
)
from freqtrade.signal_strategy.parser import SignalParser, map_pair_to_freqtrade, parse_signal
from freqtrade.signal_strategy.risk import RiskContext, RiskGovernor, RiskPolicy
from freqtrade.signal_strategy.store import (
    InMemorySignalStore,
    SQLiteSignalOperationStore,
    SQLiteSignalStore,
    make_signal_store_from_url,
)


_RUNTIME_STATE: dict[str, Any] = {
    "kill_switch_enabled": False,
    "kill_switch_reason": "",
    "kill_switch_close_all": False,
    "kill_switch_updated_at": "",
}

_TERMINAL_SIGNAL_STATUSES = {"entered", "partially_exited", "exited"}


def reset_signal_strategy_runtime_state() -> None:
    _RUNTIME_STATE["kill_switch_enabled"] = False
    _RUNTIME_STATE["kill_switch_reason"] = ""
    _RUNTIME_STATE["kill_switch_close_all"] = False
    _RUNTIME_STATE["kill_switch_updated_at"] = ""


def activate_kill_switch(payload: dict[str, Any]) -> dict[str, Any]:
    reason = str(payload.get("reason", "manual risk stop"))
    close_all = bool(payload.get("close_all", False))
    updated_at = datetime.now(tz=UTC).isoformat()
    _RUNTIME_STATE["kill_switch_enabled"] = True
    _RUNTIME_STATE["kill_switch_reason"] = reason
    _RUNTIME_STATE["kill_switch_close_all"] = close_all
    _RUNTIME_STATE["kill_switch_updated_at"] = updated_at
    return {
        "enabled": True,
        "close_all": close_all,
        "reason": reason,
        "risk_state": "blocked_new_entries",
        "blocking_reasons": ["kill_switch_enabled"],
        "updated_at": updated_at,
    }


def is_kill_switch_enabled() -> bool:
    return bool(_RUNTIME_STATE["kill_switch_enabled"])


def get_signal_strategy_runtime_state() -> dict[str, Any]:
    return dict(_RUNTIME_STATE)


def get_signal_dashboard_snapshot(report_date: str | None = None) -> dict[str, Any]:
    active_date = _report_date(report_date)
    store_path = _signal_store_path()
    if not store_path:
        return {}
    if not store_path.exists():
        return {}

    with _connect_signal_store(store_path) as connection:
        status_counts = _status_counts(connection)
        recent_events = _recent_events(connection)
        signal_rows = _recent_signals(connection)
        risk_event_count = _event_count(connection, "risk")
        audit_event_count = _event_count(connection, "audit")
        operations_count = _operations_count(connection)

    total_signals = sum(status_counts.values())
    approved_count = status_counts.get("approved", 0) + status_counts.get("reserved", 0)
    executed_count = sum(status_counts.get(status, 0) for status in _TERMINAL_SIGNAL_STATUSES)
    rejected_count = status_counts.get("rejected", 0)
    needs_review_count = status_counts.get("needs_review", 0)
    blocked_reasons = []
    if _RUNTIME_STATE["kill_switch_enabled"]:
        blocked_reasons.append("kill_switch_enabled")

    snapshot_id = f"daily-provider-{active_date}"
    risk_state = "normal"
    if blocked_reasons:
        risk_state = "blocked_new_entries"

    report = {
        "snapshot_id": snapshot_id,
        "timezone": "UTC",
        "account": {
            "equity": 0.0,
            "realized_pnl_today": 0.0,
            "unrealized_pnl": 0.0,
            "max_drawdown_pct": 0.0,
        },
        "trades": {
            "closed_today_count": 0,
            "win_count": 0,
            "profit_factor": 0.0,
            "avg_r": 0.0,
        },
        "signals": {
            "received_count": total_signals,
            "accepted_count": approved_count,
            "rejected_count": rejected_count,
            "executed_count": executed_count,
            "needs_review_count": needs_review_count,
        },
        "risk_events": {
            "count": risk_event_count + audit_event_count,
            "blocking_count": len(blocked_reasons),
            "kill_switch_count": 1 if _RUNTIME_STATE["kill_switch_enabled"] else 0,
        },
        "open_positions": {
            "count": 0,
            "notional": 0.0,
        },
    }

    return {
        "data_source": "provider",
        "status": "ok",
        "status_reason": "signal_store",
        "overview": {
            "bot_status": "running",
            "run_mode": "dry_run",
            "equity": 0.0,
            "free_balance": 0.0,
            "margin_used": 0.0,
            "realized_pnl_today": 0.0,
            "unrealized_pnl": 0.0,
            "open_trade_count": 0,
            "open_order_count": operations_count,
            "today_signal_count": total_signals,
            "today_execution_count": executed_count,
            "today_executed_count": executed_count,
            "risk_state": risk_state,
            "recent_events": recent_events,
        },
        "open_trades": [],
        "risk": {
            "single_trade_risk_usage_pct": 0.0,
            "total_open_risk_usage_pct": 0.0,
            "daily_loss_usage_pct": 0.0,
            "no_sl_trade_count": 0,
            "high_leverage_trade_count": 0,
            "risk_state": risk_state,
            "blocking_reasons": blocked_reasons,
            "pair_locks": [],
            "recent_signals": signal_rows,
        },
        "reports": {
            snapshot_id: report,
        },
    }


def get_daily_report(report_date: str | None = None) -> dict[str, Any]:
    active_date = _report_date(report_date)
    snapshot = get_signal_dashboard_snapshot(active_date)
    reports = snapshot.get("reports")
    if not isinstance(reports, dict):
        return {}

    report = reports.get(f"daily-provider-{active_date}")
    if isinstance(report, dict):
        return report

    return {}


def get_audit_events(limit: int = 50) -> list[dict[str, Any]]:
    store_path = _signal_store_path()
    if not store_path:
        return []
    if not store_path.exists():
        return []

    safe_limit = max(1, min(int(limit), 200))
    with _connect_signal_store(store_path) as connection:
        return _audit_events(connection, safe_limit)


def record_audit_event(event_type: str, signal_id: str = "", **payload: Any) -> bool:
    store_path = _signal_store_path()
    if not store_path:
        return False

    store = SQLiteSignalStore(store_path)
    store.record_audit_event(event_type, signal_id, **payload)
    return True


def transition_signal(
    signal_id: str,
    target_status: str,
    actor: str = "system",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store_path = _signal_store_path()
    if not store_path:
        return {
            "approved": False,
            "signal_id": signal_id,
            "status": "failed",
            "reason": "signal_store_unavailable",
        }

    active_context = context
    if active_context is None:
        active_context = {}

    store = SQLiteSignalStore(store_path)
    result = store.transition_signal(
        signal_id,
        SignalStatus(target_status),
        actor,
        active_context,
    )
    return {
        "approved": result.approved,
        "signal_id": result.signal_id,
        "status": result.status.value,
        "reason": result.reason,
    }


def _report_date(report_date: str | None) -> str:
    if report_date:
        return report_date

    return datetime.now(tz=UTC).date().isoformat()


def _signal_store_path() -> Path | bool:
    raw_url = os.environ.get("HERMES_SIGNAL_STORE_URL", "")
    if not raw_url:
        raw_url = os.environ.get("SIGNAL_STORE_URL", "")
    if not raw_url:
        return False
    if not raw_url.startswith("sqlite:///"):
        return False

    parsed = urlparse(raw_url)
    db_path = unquote(parsed.path)
    if raw_url.startswith("sqlite:////"):
        while db_path.startswith("//"):
            db_path = db_path[1:]
        return Path(db_path)

    if db_path.startswith("/"):
        db_path = db_path[1:]

    return Path(db_path)


def _connect_signal_store(store_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(store_path)
    connection.row_factory = sqlite3.Row
    return connection


def _status_counts(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT status, COUNT(*) AS total
        FROM signals
        GROUP BY status
        """
    ).fetchall()
    counts = {}
    for row in rows:
        counts[str(row["status"])] = int(row["total"])
    return counts


def _operations_count(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) AS total FROM signal_operations").fetchone()
    if not row:
        return 0
    return int(row["total"])


def _event_count(connection: sqlite3.Connection, event_kind: str) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM signal_events
        WHERE event_kind = ?
        """,
        (event_kind,),
    ).fetchone()
    if not row:
        return 0
    return int(row["total"])


def _recent_events(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, event_type, signal_id, created_at
        FROM signal_events
        ORDER BY id DESC
        LIMIT 20
        """
    ).fetchall()
    events = []
    for row in rows:
        message = str(row["event_type"])
        signal_id = str(row["signal_id"])
        if signal_id:
            message = f"{message}: {signal_id}"
        events.append(
            {
                "event_id": f"signal-event-{row['id']}",
                "type": str(row["event_type"]),
                "message": message,
                "timestamp": str(row["created_at"]),
            }
        )
    return events


def _audit_events(connection: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, event_kind, event_type, signal_id, payload, created_at
        FROM signal_events
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    events = []
    for row in rows:
        payload = _decode_payload(row["payload"])
        signal_id = str(row["signal_id"])
        if not signal_id:
            signal_id = str(payload.get("signal_id", ""))
        event_id = f"signal-event-{row['id']}"
        created_at = str(row["created_at"])
        actor = str(payload.get("actor", ""))
        actor_role = str(payload.get("actor_role", ""))
        if actor and not actor_role:
            actor_role = "risk_admin"
        request_id = str(payload.get("request_id", ""))
        if not request_id:
            request_id = event_id
        correlation_id = str(payload.get("correlation_id", ""))
        if not correlation_id:
            correlation_id = signal_id
        if not correlation_id:
            correlation_id = event_id
        message = str(row["event_type"])
        if signal_id:
            message = f"{message}: {signal_id}"
        events.append(
            {
                "event_id": event_id,
                "event_kind": str(row["event_kind"]),
                "event_type": str(row["event_type"]),
                "type": str(row["event_type"]),
                "signal_id": signal_id,
                "actor": str(payload.get("actor", "")),
                "actor_id": actor,
                "actor_role": actor_role,
                "occurred_at": created_at,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "reason": str(payload.get("reason", "")),
                "message": message,
                "payload": payload,
                "payload_redacted": payload,
                "result": payload.get("result", {}),
                "timestamp": created_at,
            }
        )
    return events


def _recent_signals(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT signal_id, status, pair, payload, received_at
        FROM signals
        ORDER BY received_at DESC, signal_id DESC
        LIMIT 20
        """
    ).fetchall()
    signals = []
    for row in rows:
        payload = _decode_payload(row["payload"])
        signals.append(
            {
                "signal_id": str(row["signal_id"]),
                "status": str(row["status"]),
                "pair": str(row["pair"]),
                "side": str(payload.get("side", "")),
                "received_at": str(row["received_at"]),
            }
        )
    return signals


def _decode_payload(payload_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


__all__ = [
    "ApprovalResult",
    "EntryPlan",
    "InMemorySignalStore",
    "LeveragePlan",
    "MediaAsset",
    "ReservationResult",
    "RiskContext",
    "RiskGovernor",
    "RiskPolicy",
    "RiskPolicyResult",
    "SignalParser",
    "SignalStatus",
    "SQLiteSignalOperationStore",
    "SQLiteSignalStore",
    "TakeProfit",
    "TradingSignal",
    "activate_kill_switch",
    "get_audit_events",
    "get_signal_strategy_runtime_state",
    "get_daily_report",
    "get_signal_dashboard_snapshot",
    "is_kill_switch_enabled",
    "make_signal_store_from_url",
    "map_pair_to_freqtrade",
    "parse_signal",
    "record_audit_event",
    "reset_signal_strategy_runtime_state",
    "transition_signal",
]
