from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import types
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE = ROOT / "services" / "control-plane"
CONTROL_PLANE_API = CONTROL_PLANE / "api"
NAUTILUS_NODE = ROOT / "services" / "nautilus-node"

for _path in (CONTROL_PLANE, CONTROL_PLANE_API, NAUTILUS_NODE):
    _p = str(_path)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from order_management.metrics import structured_log_line
from settings.import_export import export_payload, sanitize_export
from settings.schema import default_settings, load_descriptor, validate_settings
from settings.service import SettingsService, SettingsServiceError


SECRET_VALUE = "sk_live_OM8_DO_NOT_LEAK"
PLAN_15_3_SCENARIOS = [
    "market_open_sl_tp",
    "limit_timeout_cancel",
    "reprice_fill",
    "partial_fill_keep_remainder",
    "partial_fill_cancel_remainder",
    "stop_loss_fill",
    "batched_take_profit",
    "move_stop",
    "breakeven",
    "trailing_stop",
    "partial_close",
    "full_close",
    "cancel_all",
    "close_all_multi_position",
    "node_restart",
    "control_plane_restart",
    "ws_reconnect",
    "reconciliation_drift",
    "stale_market_account_fail_closed",
    "duplicate_intent_event_no_duplicate_orders",
    "two_account_isolation",
    "settings_publish_ack_rollback",
]


def test_no_anonymous_or_viewer_settings_and_command_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = _fake_settings_db()
    db_conn = fake_db.connect()
    client = _settings_client(monkeypatch, db_conn)

    settings_payload = {
        "scope": "account",
        "scope_key": f"acct-{uuid4().hex}",
        "settings": {"entry": {"max_slippage_bps": 9}},
        "expected_version": 0,
        "reason": "security audit write auth",
        "request_id": str(uuid4()),
    }
    for method, path, payload in (
        ("patch", "/v1/order-management/settings", settings_payload),
        (
            "post",
            "/v1/order-management/settings/rollback",
            {
                "scope": "account",
                "scope_key": "acct-missing",
                "target_version": 1,
                "expected_version": 2,
                "reason": "security audit rollback auth",
                "request_id": str(uuid4()),
                "confirm": True,
            },
        ),
        (
            "post",
            "/v1/order-management/settings/import",
            {
                "payload": {"settings": {"entry": {"max_slippage_bps": 8}}},
                "expected_version": 0,
                "reason": "security audit import auth",
                "request_id": str(uuid4()),
            },
        ),
    ):
        anonymous = getattr(client, method)(path, json=payload)
        assert anonymous.status_code == 401

        invalid_token = getattr(client, method)(
            path,
            headers={"Authorization": "Bearer invalid-token"},
            json=payload,
        )
        assert invalid_token.status_code == 403

    viewer_write = client.patch(
        "/v1/order-management/settings",
        headers={"Authorization": "Bearer viewer-token"},
        json=settings_payload | {"scope_key": f"acct-{uuid4().hex}"},
    )
    assert viewer_write.status_code == 403
    assert viewer_write.json()["detail"]["code"] == "permission_denied"

    operator_write = client.patch(
        "/v1/order-management/settings",
        headers={"Authorization": "Bearer operator-token"},
        json=settings_payload | {"scope_key": f"acct-{uuid4().hex}"},
    )
    assert operator_write.status_code == 200, operator_write.text

    anonymous_command = client.post(
        "/v1/commands",
        json={"type": "CLOSE_ALL", "reason": "security audit command auth", "confirm": True},
    )
    assert anonymous_command.status_code == 401

    viewer_command = client.post(
        "/v1/commands",
        headers={"Authorization": "Bearer viewer-token", "X-Request-Id": "req-viewer-close-all"},
        json={"type": "CLOSE_ALL", "reason": "security audit command auth", "confirm": True},
    )
    assert viewer_command.status_code == 403

    risk_admin_without_request_id = client.post(
        "/v1/commands",
        headers={"Authorization": "Bearer risk-token"},
        json={"type": "CLOSE_ALL", "reason": "security audit command auth", "confirm": True},
    )
    assert risk_admin_without_request_id.status_code == 400
    assert "request_id" in risk_admin_without_request_id.json()["detail"]


def test_no_secret_echo_store_export_or_business_table_secret_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_payload = {
        "general": {"order_manager_enabled": True},
        "advanced": {
            "api_key": SECRET_VALUE,
            "token": SECRET_VALUE,
            "exchange_secret": SECRET_VALUE,
        },
    }
    exported = sanitize_export(secret_payload)
    payload = export_payload(
        scope="global",
        scope_key="global",
        version=1,
        settings=secret_payload,
        environment="testnet",
    )
    assert exported == {"general": {"order_manager_enabled": True}}
    assert SECRET_VALUE not in str(payload)
    assert "api_key" not in str(payload).lower()
    assert "token" not in str(payload).lower()
    assert "exchange_secret" not in str(payload).lower()

    descriptor = load_descriptor()
    assert all(field.get("secret", False) is False for _, _, field in _iter_descriptor_fields(descriptor))
    assert _contains_secret_marker(default_settings(descriptor)) is False

    validation = validate_settings({"advanced": {"api_key": SECRET_VALUE}})
    assert validation["valid"] is False
    assert SECRET_VALUE not in str(validation)

    line = structured_log_line(
        "security.audit",
        api_key=SECRET_VALUE,
        nested={"access_token": SECRET_VALUE, "Authorization": f"Bearer {SECRET_VALUE}"},
    )
    assert SECRET_VALUE not in line
    assert "[REDACTED]" in line

    migration_sql = (ROOT / "db" / "migrations" / "0005_order_management.up.sql").read_text(encoding="utf-8")
    forbidden_column = re.compile(r"\b(secret|password|credential|api[_-]?key|access[_-]?token|refresh[_-]?token)\b", re.I)
    for table_name, column_name in _migration_columns(migration_sql):
        assert not forbidden_column.search(column_name), f"{table_name}.{column_name} stores a secret-like value"

    fake_db = _fake_settings_db()
    db_conn = fake_db.connect()
    client = _settings_client(monkeypatch, db_conn)
    response = client.post(
        "/v1/order-management/settings/validate",
        headers={"Authorization": "Bearer viewer-token"},
        json={"settings": {"advanced": {"api_key": SECRET_VALUE}}},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert SECRET_VALUE not in response.text


def test_dangerous_settings_writes_audit_actor_reason_and_request_id() -> None:
    fake_db = _fake_settings_db()
    db_conn = fake_db.connect()
    service = SettingsService(db_conn)

    live_relaxation_request_id = str(uuid4())
    patched = service.patch_settings(
        scope="account",
        scope_key="acct-live-risk",
        patch={
            "general": {"execution_mode": "live"},
            "money": {"max_notional_per_order": 1000},
        },
        expected_version=0,
        reason="operator approved testnet live-risk relaxation rehearsal",
        request_id=live_relaxation_request_id,
        actor={"actor_id": "risk-admin-1", "role": "risk_admin"},
        confirm=True,
        operator_signoff="operator-1",
    )
    assert patched["version"] == 1
    audit = _audit_row(fake_db, live_relaxation_request_id)
    assert audit["actor"] == "risk-admin-1"
    assert audit["action"] == "settings.patch"
    assert audit["reason"] == "operator approved testnet live-risk relaxation rehearsal"
    assert audit["request_id"] == str(UUID(live_relaxation_request_id))

    v2_request_id = str(uuid4())
    v2 = service.patch_settings(
        scope="account",
        scope_key="acct-live-risk",
        patch={"entry": {"max_slippage_bps": 12}},
        expected_version=patched["version"],
        reason="prepare rollback target",
        request_id=v2_request_id,
        actor={"actor_id": "risk-admin-1", "role": "risk_admin"},
    )
    rollback_request_id = str(uuid4())
    rollback = service.rollback_settings(
        scope="account",
        scope_key="acct-live-risk",
        target_version=patched["version"],
        expected_version=v2["version"],
        reason="rollback to previous live-risk version",
        request_id=rollback_request_id,
        actor={"actor_id": "risk-admin-1", "role": "risk_admin"},
        confirm=True,
        operator_signoff="operator-1",
    )
    assert rollback["version"] == 3
    audit = _audit_row(fake_db, rollback_request_id)
    assert audit["actor"] == "risk-admin-1"
    assert audit["action"] == "settings.rollback"
    assert audit["reason"] == "rollback to previous live-risk version"
    assert audit["request_id"] == str(UUID(rollback_request_id))


def test_settings_write_commands_require_request_id() -> None:
    fake_db = _fake_settings_db()
    db_conn = fake_db.connect()
    service = SettingsService(db_conn)

    with pytest.raises(SettingsServiceError, match="request_id"):
        service.patch_settings(
            scope="account",
            scope_key="acct-request-id",
            patch={"entry": {"max_slippage_bps": 7}},
            expected_version=0,
            reason="missing request id",
            request_id="",
            actor={"actor_id": "risk-admin-1", "role": "risk_admin"},
        )

    v1 = service.patch_settings(
        scope="account",
        scope_key="acct-request-id",
        patch={"entry": {"max_slippage_bps": 7}},
        expected_version=0,
        reason="seed rollback test",
        request_id=str(uuid4()),
        actor={"actor_id": "risk-admin-1", "role": "risk_admin"},
    )
    v2 = service.patch_settings(
        scope="account",
        scope_key="acct-request-id",
        patch={"entry": {"max_slippage_bps": 8}},
        expected_version=v1["version"],
        reason="advance rollback test",
        request_id=str(uuid4()),
        actor={"actor_id": "risk-admin-1", "role": "risk_admin"},
    )
    with pytest.raises(SettingsServiceError, match="request_id"):
        service.rollback_settings(
            scope="account",
            scope_key="acct-request-id",
            target_version=v1["version"],
            expected_version=v2["version"],
            reason="missing rollback request id",
            request_id="",
            actor={"actor_id": "risk-admin-1", "role": "risk_admin"},
            confirm=True,
        )


def test_close_all_command_records_durable_audit_with_reason_and_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = _fake_settings_db()
    db_conn = fake_db.connect()
    client = _settings_client(monkeypatch, db_conn)

    command_conn = FakeCommandConnection()
    monkeypatch.setenv("DATABASE_URL", "postgresql:///om8-command-audit")
    import read_api

    monkeypatch.setattr(read_api.psycopg2, "connect", lambda url: command_conn)
    monkeypatch.setitem(sys.modules, "commands", _fake_commands_module())
    monkeypatch.delitem(sys.modules, "audit", raising=False)

    response = client.post(
        "/v1/commands",
        headers={"Authorization": "Bearer risk-token", "X-Request-Id": "req-close-all-audit"},
        json={
            "type": "CLOSE_ALL",
            "reason": "operator close_all audit completeness drill",
            "confirm": True,
            "target_nodes": ["node-a"],
            "scope": {"account_id": "acct-audit"},
        },
    )

    assert response.status_code == 200, response.text
    assert command_conn.committed is True
    assert len(command_conn.audit_events) == 1
    audit = command_conn.audit_events[0]
    assert audit["event_type"] == "operator_command"
    assert audit["aggregate_type"] == "operator_command"
    assert audit["actor"] == "risk_admin"
    assert audit["payload"]["operation"] == "CLOSE_ALL"
    assert audit["payload"]["reason"] == "operator close_all audit completeness drill"
    assert audit["payload"]["request_id"] == "req-close-all-audit"
    assert audit["payload"]["actor"] == {"actor_id": "risk-admin", "role": "risk_admin"}


def test_acceptance_and_chaos_harnesses_import_list_and_dry_run_offline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    acceptance = _load_module(ROOT / "scripts" / "order_management_acceptance.py", "order_management_acceptance")
    chaos = _load_module(ROOT / "scripts" / "order_management_chaos.py", "order_management_chaos")

    assert [scenario.name for scenario in acceptance.SCENARIOS] == PLAN_15_3_SCENARIOS
    assert len(acceptance.SCENARIOS) == 22
    assert acceptance.main(["--list"]) == 0
    acceptance_list = capsys.readouterr().out
    assert acceptance_list.count("\n") == 22
    assert "settings_publish_ack_rollback" in acceptance_list
    assert acceptance.main(["--dry-run", "--report", str(tmp_path / "testnet.md")]) == 0
    assert "PENDING - run against testnet" in (tmp_path / "testnet.md").read_text(encoding="utf-8")

    assert len(chaos.CHAOS_CASES) >= 7
    assert chaos.main(["--list"]) == 0
    chaos_list = capsys.readouterr().out
    assert "node_restart" in chaos_list
    assert "db_fault" in chaos_list
    assert chaos.main(["--dry-run", "--report", str(tmp_path / "chaos.md")]) == 0
    assert "PENDING - run against chaos infra" in (tmp_path / "chaos.md").read_text(encoding="utf-8")


def _settings_client(monkeypatch: pytest.MonkeyPatch, db_conn: object) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "postgresql:///om8-security")
    monkeypatch.setenv("VIEWER_TOKEN", "viewer-token")
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("RISK_ADMIN_TOKEN", "risk-token")
    monkeypatch.setenv("REVIEWER_TOKEN", "reviewer-token")

    import read_api
    import settings.router as settings_router

    api_app = importlib.reload(read_api)
    monkeypatch.setattr(settings_router, "connect", lambda: db_conn)
    return TestClient(api_app.app)


def _fake_settings_db():
    module = _load_module(ROOT / "tests" / "order_management" / "settings" / "conftest.py", "settings_fake_db")
    return module.FakeSettingsDatabase()


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _iter_descriptor_fields(descriptor: dict):
    for category in descriptor["categories"].values():
        for field in category.values():
            yield None, None, field


def _contains_secret_marker(value: object) -> bool:
    return bool(re.search(r"(secret|token|api[_-]?key|password|credential)", str(value), re.I))


def _migration_columns(sql: str) -> list[tuple[str, str]]:
    columns: list[tuple[str, str]] = []
    for match in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", sql, re.S | re.I):
        table_name = match.group(1)
        for line in match.group(2).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "--")):
                continue
            column_name = stripped.split()[0].strip('" ,')
            columns.append((table_name, column_name))
    return columns


def _audit_row(fake_db, request_id: str) -> dict:
    request_uuid = str(UUID(request_id))
    rows = [row for row in fake_db.audit if row["request_id"] == request_uuid]
    assert len(rows) == 1
    return rows[0]


def _fake_commands_module() -> types.ModuleType:
    module = types.ModuleType("commands")

    def issue_command(conn, *, command_type, requested_by, reason, idempotency_key, target_nodes, scope):
        conn.issued_commands.append(
            {
                "command_type": command_type,
                "requested_by": requested_by,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "target_nodes": target_nodes,
                "scope": scope,
            }
        )
        return {"command_id": "cmd-close-all-audit", "status": "pending", "idempotent": False}

    module.issue_command = issue_command
    return module


class FakeCommandConnection:
    def __init__(self) -> None:
        self.issued_commands: list[dict] = []
        self.audit_events: list[dict] = []
        self.committed = False
        self.closed = False

    def cursor(self):
        return FakeCommandCursor(self)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


class FakeCommandCursor:
    def __init__(self, conn: FakeCommandConnection) -> None:
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        normalized = " ".join(sql.lower().split())
        params = params or ()
        if normalized.startswith("insert into audit_events"):
            self.conn.audit_events.append(
                {
                    "audit_event_id": str(params[0]),
                    "event_type": params[1],
                    "aggregate_type": params[2],
                    "aggregate_id": params[3],
                    "actor": params[4],
                    "trace_id": params[5],
                    "payload": getattr(params[6], "adapted", params[6]),
                }
            )
            return
        raise AssertionError(f"unexpected command audit SQL: {sql}")
