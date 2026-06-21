from __future__ import annotations

from datetime import datetime, timedelta, timezone

from order_management.alerts import (
    AlertCondition,
    AlertEngine,
    OutboxNotificationSink,
    condition_from_close_all_result,
    condition_from_reconciliation_finding,
    conditions_from_freshness_reasons,
    severity_for_condition,
)


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_each_required_critical_condition_emits_alert() -> None:
    emitted = []
    engine = AlertEngine(cooldown=timedelta(minutes=5), sink=emitted.append)

    conditions = [
        condition_from_reconciliation_finding(
            {
                "account_id": "acct-alert",
                "finding_type": "missing_protection",
                "position_key": "acct-alert:BTCUSDT",
                "payload": {"lifecycle_role": "stop_loss"},
            }
        ),
        conditions_from_freshness_reasons(
            account_id="acct-alert",
            reasons=["market_data"],
            observed_at=NOW,
        )[0],
        condition_from_reconciliation_finding(
            {
                "account_id": "acct-alert",
                "finding_type": "position_drift",
                "position_key": "acct-alert:BTCUSDT",
                "payload": {"drift_class": "severe", "local_quantity": "1", "venue_quantity": "2"},
            }
        ),
        condition_from_close_all_result(
            account_id="acct-alert",
            request_id="req-close-all",
            result={"status": "partial", "verification": {"flat": False}},
            observed_at=NOW,
        ),
    ]

    notifications = [engine.evaluate(condition, now=NOW) for condition in conditions]

    assert len(emitted) == 4
    assert all(notification is not None for notification in notifications)
    assert {notification.condition_type for notification in emitted} == {
        "no_protection",
        "stale",
        "drift",
        "close_all_failure",
    }
    assert all(notification.severity == "critical" for notification in emitted)
    assert all(notification.status == "firing" for notification in emitted)


def test_severity_map_covers_plan_levels() -> None:
    assert severity_for_condition("no_protection") == "critical"
    assert severity_for_condition("close_all_failure") == "critical"
    assert severity_for_condition("drift", {"drift_class": "severe"}) == "critical"
    assert severity_for_condition("stale", {"freshness_kind": "market_data"}) == "critical"
    assert severity_for_condition("lost_order") == "high"
    assert severity_for_condition("account_stale") == "high"
    assert severity_for_condition("daily_loss_gate") == "high"
    assert severity_for_condition("drawdown_gate") == "high"
    assert severity_for_condition("reprice_exhausted") == "medium"
    assert severity_for_condition("partial_fill_timeout") == "medium"
    assert severity_for_condition("node_settings_behind") == "medium"
    assert severity_for_condition("normal_fill") == "info"
    assert severity_for_condition("settings_published") == "info"
    assert severity_for_condition("reconciliation_ok") == "info"


def test_dedupe_suppresses_same_alert_key_until_cooldown_expires() -> None:
    emitted = []
    engine = AlertEngine(cooldown=timedelta(minutes=5), sink=emitted.append)
    condition = AlertCondition.active(
        condition_type="no_protection",
        account_id="acct-alert",
        entity_id="acct-alert:BTCUSDT",
        payload={"lifecycle_role": "stop_loss"},
        observed_at=NOW,
    )

    first = engine.evaluate(condition, now=NOW)
    repeated = engine.evaluate(condition, now=NOW + timedelta(minutes=1))
    after_cooldown = engine.evaluate(condition, now=NOW + timedelta(minutes=6))

    assert first is not None
    assert repeated is None
    assert after_cooldown is not None
    assert [notification.status for notification in emitted] == ["firing", "firing"]


def test_recovery_is_emitted_when_condition_clears() -> None:
    emitted = []
    engine = AlertEngine(cooldown=timedelta(minutes=5), sink=emitted.append)
    firing = AlertCondition.active(
        condition_type="stale",
        account_id="acct-alert",
        entity_id="market_data",
        payload={"freshness_kind": "market_data"},
        observed_at=NOW,
    )
    cleared = firing.cleared(observed_at=NOW + timedelta(seconds=30))

    engine.evaluate(firing, now=NOW)
    recovery = engine.evaluate(cleared, now=NOW + timedelta(seconds=30))
    duplicate_recovery = engine.evaluate(cleared, now=NOW + timedelta(seconds=45))

    assert recovery is not None
    assert duplicate_recovery is None
    assert [notification.status for notification in emitted] == ["firing", "recovery"]
    assert emitted[1].event_type == "notification.recovery"
    assert emitted[1].visible is True


def test_outbox_sink_routes_by_notification_channel_and_severity() -> None:
    conn = RecordingConnection()
    sink = OutboxNotificationSink(conn)
    engine = AlertEngine(cooldown=timedelta(minutes=5), sink=sink)

    notification = engine.evaluate(
        AlertCondition.active(
            condition_type="close_all_failure",
            account_id="acct-alert",
            entity_id="req-close-all",
            payload={"status": "partial"},
            observed_at=NOW,
        ),
        now=NOW,
    )

    assert notification is not None
    row = conn.inserts[0]
    assert row["aggregate_type"] == "notification"
    assert row["aggregate_id"] == notification.alert_key
    assert row["event_type"] == "notification.alert"
    assert row["payload"]["channel"] == "order-management.notifications"
    assert row["payload"]["route"] == {"channel": "order-management.notifications", "severity": "critical"}
    assert row["payload"]["severity"] == "critical"
    assert row["payload"]["status"] == "firing"
    assert row["payload"]["visible"] is True


class RecordingConnection:
    def __init__(self) -> None:
        self.inserts: list[dict] = []

    def cursor(self) -> "RecordingCursor":
        return RecordingCursor(self)


class RecordingCursor:
    def __init__(self, conn: RecordingConnection) -> None:
        self.conn = conn
        self._row: tuple[str] | None = None

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> None:
        assert "INSERT INTO outbox_events" in sql
        (
            outbox_event_id,
            aggregate_type,
            aggregate_id,
            event_type,
            payload,
            trace_id,
        ) = params
        self.conn.inserts.append(
            {
                "outbox_event_id": outbox_event_id,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "event_type": event_type,
                "payload": getattr(payload, "adapted", payload),
                "trace_id": trace_id,
            }
        )
        self._row = (outbox_event_id,)

    def fetchone(self) -> tuple[str] | None:
        return self._row
