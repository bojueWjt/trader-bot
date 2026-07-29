from __future__ import annotations

import importlib
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from settings.service import SettingsService, SettingsServiceError


def _actor(role: str = "risk_admin") -> dict[str, str]:
    return {"actor_id": f"{role}-actor", "role": role}


def _count_settings(conn, scope: str, scope_key: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM order_management_settings WHERE scope=%s AND scope_key=%s",
            (scope, scope_key),
        )
        return int(cur.fetchone()[0])


def test_patch_rejects_invalid_and_conflicting_updates_without_db_write(db_conn) -> None:
    service = SettingsService(db_conn)
    account_id = f"acct-{uuid4().hex}"

    with pytest.raises(SettingsServiceError) as invalid:
        service.patch_settings(
            scope="account",
            scope_key=account_id,
            patch={"general": {"execution_mode": "paper"}},
            expected_version=0,
            reason="invalid execution mode",
            request_id=str(uuid4()),
            actor=_actor(),
        )
    assert invalid.value.code == "validation_failed"
    assert _count_settings(db_conn, "account", account_id) == 0

    first = service.patch_settings(
        scope="account",
        scope_key=account_id,
        patch={"entry": {"max_slippage_bps": 12}},
        expected_version=0,
        reason="seed account override",
        request_id=str(uuid4()),
        actor=_actor(),
    )
    assert first["version"] == 1

    with pytest.raises(SettingsServiceError) as conflict:
        service.patch_settings(
            scope="account",
            scope_key=account_id,
            patch={"entry": {"max_slippage_bps": 20}},
            expected_version=0,
            reason="stale write",
            request_id=str(uuid4()),
            actor=_actor(),
        )
    assert conflict.value.code == "version_conflict"
    assert _count_settings(db_conn, "account", account_id) == 1


def test_patch_effective_and_delete_restore_inheritance(db_conn) -> None:
    service = SettingsService(db_conn)
    account_id = f"acct-{uuid4().hex}"

    created = service.patch_settings(
        scope="account",
        scope_key=account_id,
        patch={"entry": {"max_slippage_bps": 9}},
        expected_version=0,
        reason="override slippage",
        request_id=str(uuid4()),
        actor=_actor(),
    )
    effective = service.get_effective(account_id=account_id)
    assert effective["settings"]["entry"]["max_slippage_bps"]["value"] == 9
    assert effective["settings"]["entry"]["max_slippage_bps"]["source_scope"] == "account"

    service.patch_settings(
        scope="account",
        scope_key=account_id,
        patch={"entry": {"max_slippage_bps": None}},
        expected_version=created["version"],
        reason="remove account override",
        request_id=str(uuid4()),
        actor=_actor(),
    )

    restored = service.get_effective(account_id=account_id)
    assert restored["settings"]["entry"]["max_slippage_bps"]["value"] == 25
    assert restored["settings"]["entry"]["max_slippage_bps"]["source_scope"] == "system_default"


def test_publish_writes_immutable_version_outbox_and_structured_audit(db_conn) -> None:
    service = SettingsService(db_conn)
    account_id = f"acct-{uuid4().hex}"
    request_id = str(uuid4())

    result = service.patch_settings(
        scope="account",
        scope_key=account_id,
        patch={"entry": {"max_slippage_bps": 11}},
        expected_version=0,
        reason="audit and outbox evidence",
        request_id=request_id,
        actor=_actor(),
    )

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT settings
            FROM order_management_setting_versions
            WHERE scope='account' AND scope_key=%s AND version=%s
            """,
            (account_id, result["version"]),
        )
        version_row = cur.fetchone()
        cur.execute(
            "SELECT event_type, payload FROM outbox_events WHERE payload->>'request_id'=%s",
            (request_id,),
        )
        outbox_row = cur.fetchone()
        cur.execute(
            """
            SELECT actor, action, target, before_state, after_state, reason, request_id::text
            FROM audit_events
            WHERE request_id=%s
            """,
            (request_id,),
        )
        audit_row = cur.fetchone()

    assert version_row[0]["entry"]["max_slippage_bps"] == 11
    assert outbox_row[0] == "settings.changed"
    assert outbox_row[1]["version"] == result["version"]
    assert audit_row[0] == "risk_admin-actor"
    assert audit_row[1] == "settings.patch"
    assert audit_row[2] == f"account:{account_id}"
    assert audit_row[3] == {}
    assert audit_row[4]["entry"]["max_slippage_bps"] == 11
    assert audit_row[5] == "audit and outbox evidence"
    assert audit_row[6] == str(UUID(request_id))


def test_version_diff_and_rollback_create_new_history_row(db_conn) -> None:
    service = SettingsService(db_conn)
    account_id = f"acct-{uuid4().hex}"

    v1 = service.patch_settings(
        scope="account",
        scope_key=account_id,
        patch={"general": {"max_concurrent_execution_jobs": 2}},
        expected_version=0,
        reason="initial jobs",
        request_id=str(uuid4()),
        actor=_actor(),
    )
    v2 = service.patch_settings(
        scope="account",
        scope_key=account_id,
        patch={"general": {"max_concurrent_execution_jobs": 4}},
        expected_version=v1["version"],
        reason="increase jobs",
        request_id=str(uuid4()),
        actor=_actor(),
    )

    diff = service.diff_versions("account", account_id, v1["version"], v2["version"])
    assert diff["changes"] == [
        {"path": "general.max_concurrent_execution_jobs", "before": 2, "after": 4}
    ]

    rollback = service.rollback_settings(
        scope="account",
        scope_key=account_id,
        target_version=v1["version"],
        expected_version=v2["version"],
        reason="restore previous jobs",
        request_id=str(uuid4()),
        actor=_actor(),
        confirm=True,
    )
    assert rollback["version"] == v2["version"] + 1
    assert rollback["settings"]["general"]["max_concurrent_execution_jobs"] == 2


def test_fastapi_settings_endpoints_validate_patch_and_effective(
    database_url: str,
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RISK_ADMIN_TOKEN", "risk-token")
    monkeypatch.setenv("VIEWER_TOKEN", "viewer-token")
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-token")

    import read_api
    import settings.router as settings_router

    api_app = importlib.reload(read_api)
    monkeypatch.setattr(settings_router, "connect", lambda: db_conn)
    client = TestClient(api_app.app)
    account_id = f"acct-{uuid4().hex}"

    invalid = client.post(
        "/v1/order-management/settings/validate",
        headers={"Authorization": "Bearer viewer-token"},
        json={"settings": {"entry": {"max_slippage_bps": -1}}},
    )
    assert invalid.status_code == 200
    assert invalid.json()["valid"] is False

    created = client.patch(
        "/v1/order-management/settings",
        headers={"Authorization": "Bearer risk-token"},
        json={
            "scope": "account",
            "scope_key": account_id,
            "settings": {"entry": {"max_slippage_bps": 8}},
            "expected_version": 0,
            "reason": "api patch",
            "request_id": str(uuid4()),
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["version"] == 1

    effective = client.get(
        f"/v1/order-management/settings/effective?account_id={account_id}",
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert effective.status_code == 200
    field = effective.json()["settings"]["entry"]["max_slippage_bps"]
    assert field["value"] == 8
    assert field["source_scope"] == "account"

    conflict = client.patch(
        "/v1/order-management/settings",
        headers={"Authorization": "Bearer risk-token"},
        json={
            "scope": "account",
            "scope_key": account_id,
            "settings": {"entry": {"max_slippage_bps": 13}},
            "expected_version": 0,
            "reason": "stale api patch",
            "request_id": str(uuid4()),
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "version_conflict"

    forbidden = client.patch(
        "/v1/order-management/settings",
        headers={"Authorization": "Bearer viewer-token"},
        json={
            "scope": "account",
            "scope_key": f"acct-{uuid4().hex}",
            "settings": {"entry": {"max_slippage_bps": 9}},
            "expected_version": 0,
            "reason": "viewer should not write",
            "request_id": str(uuid4()),
        },
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "permission_denied"
