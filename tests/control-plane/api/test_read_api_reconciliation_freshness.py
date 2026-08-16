from datetime import datetime, timedelta, timezone

import read_api


def test_reconciliation_health_uses_payload_timestamp() -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "reconciliation_state": "healthy",
        "ts": now.isoformat(),
    }

    assert read_api._reconciliation_health_is_fresh(payload, now) is True


def test_reconciliation_health_rejects_stale_payload_timestamp() -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "reconciliation_state": "healthy",
        "ts": (now - timedelta(seconds=20)).isoformat(),
    }

    assert read_api._reconciliation_health_is_fresh(payload, now) is False


def test_reconciliation_health_rejects_unhealthy_state() -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "reconciliation_state": "degraded",
        "ts": now.isoformat(),
    }

    assert read_api._reconciliation_health_is_fresh(payload, now) is False
