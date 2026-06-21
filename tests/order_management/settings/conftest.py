from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE = ROOT / "services" / "control-plane"
CONTROL_PLANE_API = CONTROL_PLANE / "api"
EXECUTION_DOMAIN = ROOT / "packages" / "execution-domain"
NAUTILUS_NODE = ROOT / "services" / "nautilus-node"

for _path in (CONTROL_PLANE, CONTROL_PLANE_API, EXECUTION_DOMAIN, NAUTILUS_NODE):
    _p = str(_path)
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture()
def database_url(monkeypatch: pytest.MonkeyPatch) -> str:
    url = "postgresql:///om_v3_om1"
    monkeypatch.setenv("DATABASE_URL", url)
    return url


@pytest.fixture()
def fake_db() -> "FakeSettingsDatabase":
    return FakeSettingsDatabase()


@pytest.fixture()
def db_conn(fake_db: "FakeSettingsDatabase"):
    return fake_db.connect()


class FakeSettingsDatabase:
    def __init__(self) -> None:
        self.settings: list[dict[str, Any]] = []
        self.versions: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []

    def connect(self) -> "FakeConnection":
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, db: FakeSettingsDatabase) -> None:
        self.db = db

    def cursor(self, *args: Any, **kwargs: Any) -> "FakeCursor":
        return FakeCursor(self.db, real_dict=bool(kwargs.get("cursor_factory")))

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeCursor:
    def __init__(self, db: FakeSettingsDatabase, *, real_dict: bool = False) -> None:
        self.db = db
        self.real_dict = real_dict
        self._rows: list[Any] = []
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> None:
        params = tuple(params or ())
        normalized = " ".join(sql.lower().split())
        self._rows = []
        self.rowcount = 0

        if normalized.startswith("select count(*) from order_management_settings"):
            scope, scope_key = params
            count = sum(1 for row in self.db.settings if row["scope"] == scope and row["scope_key"] == scope_key)
            self._rows = [(count,)]
            return

        if "from order_management_settings" in normalized and "order by version desc limit 1" in normalized:
            scope, scope_key = params
            rows = [
                row for row in self.db.settings
                if row["scope"] == scope and row["scope_key"] == scope_key
            ]
            rows.sort(key=lambda row: row["version"], reverse=True)
            if rows:
                self._rows = [self._shape(rows[0], [
                    "scope", "scope_key", "version", "settings", "created_by",
                    "reason", "request_id", "created_at",
                ])]
            return

        if normalized.startswith("insert into order_management_settings"):
            (
                setting_id,
                scope,
                scope_key,
                version,
                settings,
                created_by,
                reason,
                request_id,
            ) = params
            self.db.settings.append(
                {
                    "order_management_setting_id": setting_id,
                    "scope": scope,
                    "scope_key": scope_key,
                    "version": int(version),
                    "settings": _unwrap_json(settings),
                    "created_by": created_by,
                    "reason": reason,
                    "request_id": request_id,
                    "created_at": None,
                }
            )
            self.rowcount = 1
            return

        if normalized.startswith("insert into order_management_setting_versions"):
            (
                setting_version_id,
                scope,
                scope_key,
                version,
                previous_version,
                settings,
                changed_by,
                reason,
                request_id,
            ) = params
            self.db.versions.append(
                {
                    "setting_version_id": setting_version_id,
                    "scope": scope,
                    "scope_key": scope_key,
                    "version": int(version),
                    "previous_version": previous_version,
                    "settings": _unwrap_json(settings),
                    "changed_by": changed_by,
                    "reason": reason,
                    "request_id": request_id,
                    "created_at": None,
                }
            )
            self.rowcount = 1
            return

        if normalized.startswith("select settings from order_management_setting_versions"):
            if "scope='account'" in normalized:
                scope = "account"
                scope_key, version = params
            else:
                scope, scope_key, version = params
            for row in self.db.versions:
                if row["scope"] == scope and row["scope_key"] == scope_key and row["version"] == int(version):
                    self._rows = [(row["settings"],)]
                    return
            return

        if "from order_management_setting_versions" in normalized and "and version=%s" in normalized:
            scope, scope_key, version = params
            for row in self.db.versions:
                if row["scope"] == scope and row["scope_key"] == scope_key and row["version"] == int(version):
                    self._rows = [self._shape(row, [
                        "setting_version_id", "scope", "scope_key", "version", "previous_version",
                        "settings", "changed_by", "reason", "request_id", "created_at",
                    ])]
                    return
            return

        if "from order_management_setting_versions" in normalized:
            scope, scope_key = params
            rows = [
                row for row in self.db.versions
                if row["scope"] == scope and row["scope_key"] == scope_key
            ]
            rows.sort(key=lambda row: row["version"], reverse=True)
            self._rows = [
                self._shape(row, [
                    "setting_version_id", "scope", "scope_key", "version", "previous_version",
                    "settings", "changed_by", "reason", "request_id", "created_at",
                ])
                for row in rows
            ]
            return

        if normalized.startswith("insert into outbox_events"):
            outbox_event_id, aggregate_type, aggregate_id, event_type, payload = params
            if any(row["outbox_event_id"] == outbox_event_id for row in self.db.outbox):
                self._rows = []
                return
            self.db.outbox.append(
                {
                    "outbox_event_id": outbox_event_id,
                    "status": "pending",
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "event_type": event_type,
                    "payload": _unwrap_json(payload),
                }
            )
            self._rows = [(outbox_event_id,)]
            self.rowcount = 1
            return

        if "from outbox_events" in normalized and "payload->>'request_id'" in normalized:
            (request_id,) = params
            for row in self.db.outbox:
                if row["payload"].get("request_id") == request_id:
                    self._rows = [(row["event_type"], row["payload"])]
                    return
            return

        if normalized.startswith("insert into audit_events"):
            (
                audit_event_id,
                event_type,
                target_for_aggregate,
                actor,
                trace_id,
                payload,
                action,
                target,
                before_state,
                after_state,
                reason,
                request_id,
            ) = params
            self.db.audit.append(
                {
                    "audit_event_id": audit_event_id,
                    "event_type": event_type,
                    "aggregate_id": target_for_aggregate,
                    "actor": actor,
                    "trace_id": trace_id,
                    "payload": _unwrap_json(payload),
                    "action": action,
                    "target": target,
                    "before_state": _unwrap_json(before_state),
                    "after_state": _unwrap_json(after_state),
                    "reason": reason,
                    "request_id": request_id,
                }
            )
            self.rowcount = 1
            return

        if "from audit_events" in normalized and "where request_id=%s" in normalized:
            (request_id,) = params
            for row in self.db.audit:
                if row["request_id"] == request_id:
                    self._rows = [(
                        row["actor"],
                        row["action"],
                        row["target"],
                        row["before_state"],
                        row["after_state"],
                        row["reason"],
                        row["request_id"],
                    )]
                    return
            return

        raise AssertionError(f"Fake cursor does not support SQL: {sql}")

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def _shape(self, row: dict[str, Any], fields: list[str]) -> Any:
        if self.real_dict:
            return {field: row.get(field) for field in fields}
        return tuple(row.get(field) for field in fields)


def _unwrap_json(value: Any) -> Any:
    return getattr(value, "adapted", value)
