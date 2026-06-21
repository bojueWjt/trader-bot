"""Order-management settings application service."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from db.connection import transaction

from audit import record_settings_audit

from .import_export import export_payload, validate_import_payload
from .permissions import assert_can_read_settings, assert_can_write_settings
from .publisher import publish_settings_changed
from .resolver import (
    canonical_scope_key,
    extract_values,
    resolve_effective_settings,
)
from .schema import load_descriptor, validate_settings
from .versioning import (
    diff_settings,
    get_current_settings,
    get_version,
    insert_settings_version,
    list_versions,
)


class SettingsServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.errors = errors or []

    def as_detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "errors": self.errors}


class SettingsService:
    def __init__(self, conn: Any):
        self.conn = conn
        self.descriptor = load_descriptor()

    def get_settings(self, scope: str = "global", scope_key: str | None = None) -> dict[str, Any]:
        scope, scope_key = self._scope(scope, scope_key)
        current = get_current_settings(self.conn, scope, scope_key)
        return {
            "scope": scope,
            "scope_key": scope_key,
            "version": current["version"],
            "settings": current["settings"],
        }

    def get_effective(self, *, account_id: str | None = None, instrument_id: str | None = None) -> dict[str, Any]:
        resolved = resolve_effective_settings(conn=self.conn, account_id=account_id, instrument_id=instrument_id)
        return {
            "account_id": account_id,
            "instrument_id": instrument_id,
            "settings": resolved,
            "values": extract_values(resolved),
        }

    def validate_patch(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        validation = validate_settings(dict(patch or {}), self.descriptor)
        return {
            "valid": validation["valid"],
            "errors": validation["errors"],
            "normalized": validation["normalized"],
        }

    def patch_settings(
        self,
        *,
        scope: str,
        scope_key: str | None,
        patch: Mapping[str, Any],
        expected_version: int | None,
        reason: str,
        request_id: str,
        actor: Mapping[str, Any],
        confirm: bool | None = None,
        operator_signoff: str | None = None,
    ) -> dict[str, Any]:
        if expected_version is None:
            raise SettingsServiceError("expected_version_required", "expected_version is required")
        if not str(reason or "").strip():
            raise SettingsServiceError("reason_required", "reason is required")
        if not str(request_id or "").strip():
            raise SettingsServiceError("request_id_required", "request_id is required")
        scope, scope_key = self._scope(scope, scope_key)
        validation = self.validate_patch(patch)
        if not validation["valid"]:
            raise SettingsServiceError(
                "validation_failed",
                "settings patch failed validation",
                errors=validation["errors"],
            )

        with transaction(self.conn):
            current = get_current_settings(self.conn, scope, scope_key)
            if int(current["version"]) != int(expected_version):
                raise SettingsServiceError(
                    "version_conflict",
                    "settings version conflict",
                    status_code=409,
                    errors=[
                        {
                            "path": "expected_version",
                            "message": f"expected {expected_version}, current {current['version']}",
                        }
                    ],
                )
            before_settings = dict(current["settings"])
            after_settings = apply_patch_to_settings(before_settings, validation["normalized"])
            after_validation = validate_settings(after_settings, self.descriptor)
            if not after_validation["valid"]:
                raise SettingsServiceError(
                    "validation_failed",
                    "resulting settings failed validation",
                    errors=after_validation["errors"],
                )

            user = assert_can_write_settings(
                actor,
                operation="patch",
                before_settings=before_settings,
                after_settings=after_settings,
                confirm=confirm,
                operator_signoff=operator_signoff,
            )
            version = int(current["version"]) + 1
            insert_settings_version(
                self.conn,
                scope=scope,
                scope_key=scope_key,
                version=version,
                previous_version=current["version"] or None,
                settings=after_settings,
                changed_by=user["actor_id"],
                reason=reason,
                request_id=request_id,
            )
            target = f"{scope}:{scope_key}"
            record_settings_audit(
                self.conn,
                actor=user,
                action="settings.patch",
                target=target,
                before_state=before_settings,
                after_state=after_settings,
                reason=reason,
                request_id=request_id,
            )
            outbox_event_id = publish_settings_changed(
                self.conn,
                scope=scope,
                scope_key=scope_key,
                version=version,
                settings=after_settings,
                request_id=request_id,
            )
        return {
            "scope": scope,
            "scope_key": scope_key,
            "version": version,
            "previous_version": current["version"] or None,
            "settings": after_settings,
            "outbox_event_id": outbox_event_id,
        }

    def list_versions(self, scope: str, scope_key: str | None) -> dict[str, Any]:
        scope, scope_key = self._scope(scope, scope_key)
        return {"scope": scope, "scope_key": scope_key, "versions": list_versions(self.conn, scope, scope_key)}

    def get_version(self, scope: str, scope_key: str | None, version: int) -> dict[str, Any]:
        scope, scope_key = self._scope(scope, scope_key)
        try:
            return get_version(self.conn, scope, scope_key, int(version))
        except KeyError as exc:
            raise SettingsServiceError("version_not_found", str(exc), status_code=404) from exc

    def diff_versions(self, scope: str, scope_key: str | None, from_version: int, to_version: int) -> dict[str, Any]:
        before = self.get_version(scope, scope_key, from_version)
        after = self.get_version(scope, scope_key, to_version)
        return {
            "scope": before["scope"],
            "scope_key": before["scope_key"],
            "from_version": from_version,
            "to_version": to_version,
            "changes": diff_settings(before["settings"], after["settings"]),
        }

    def rollback_settings(
        self,
        *,
        scope: str,
        scope_key: str | None,
        target_version: int,
        expected_version: int,
        reason: str,
        request_id: str,
        actor: Mapping[str, Any],
        confirm: bool | None = None,
        operator_signoff: str | None = None,
    ) -> dict[str, Any]:
        if confirm is not True:
            raise SettingsServiceError("confirmation_required", "rollback requires confirm=true")
        if not str(reason or "").strip():
            raise SettingsServiceError("reason_required", "reason is required")
        if not str(request_id or "").strip():
            raise SettingsServiceError("request_id_required", "request_id is required")
        scope, scope_key = self._scope(scope, scope_key)
        try:
            target_version_row = get_version(self.conn, scope, scope_key, int(target_version))
        except KeyError as exc:
            raise SettingsServiceError("version_not_found", str(exc), status_code=404) from exc

        with transaction(self.conn):
            current = get_current_settings(self.conn, scope, scope_key)
            if int(current["version"]) != int(expected_version):
                raise SettingsServiceError("version_conflict", "settings version conflict", status_code=409)
            before_settings = dict(current["settings"])
            after_settings = dict(target_version_row["settings"])
            user = assert_can_write_settings(
                actor,
                operation="rollback",
                before_settings=before_settings,
                after_settings=after_settings,
                confirm=confirm,
                operator_signoff=operator_signoff,
            )
            version = int(current["version"]) + 1
            insert_settings_version(
                self.conn,
                scope=scope,
                scope_key=scope_key,
                version=version,
                previous_version=current["version"] or None,
                settings=after_settings,
                changed_by=user["actor_id"],
                reason=reason,
                request_id=request_id,
            )
            target = f"{scope}:{scope_key}"
            record_settings_audit(
                self.conn,
                actor=user,
                action="settings.rollback",
                target=target,
                before_state=before_settings,
                after_state=after_settings,
                reason=reason,
                request_id=request_id,
            )
            outbox_event_id = publish_settings_changed(
                self.conn,
                scope=scope,
                scope_key=scope_key,
                version=version,
                settings=after_settings,
                request_id=request_id,
            )
        return {
            "scope": scope,
            "scope_key": scope_key,
            "version": version,
            "previous_version": current["version"] or None,
            "rollback_of_version": int(target_version),
            "settings": after_settings,
            "outbox_event_id": outbox_event_id,
        }

    def export_settings(self, *, scope: str, scope_key: str | None, environment: str) -> dict[str, Any]:
        scope, scope_key = self._scope(scope, scope_key)
        current = get_current_settings(self.conn, scope, scope_key)
        return export_payload(
            scope=scope,
            scope_key=scope_key,
            version=current["version"],
            settings=current["settings"],
            environment=environment,
        )

    def validate_import(
        self,
        payload: Mapping[str, Any],
        *,
        current_environment: str,
        scope: str,
        scope_key: str | None,
    ) -> dict[str, Any]:
        scope, scope_key = self._scope(scope, scope_key)
        current = get_current_settings(self.conn, scope, scope_key)
        return validate_import_payload(
            payload,
            current_environment=current_environment,
            before_settings=current["settings"],
        )

    def import_settings(
        self,
        payload: Mapping[str, Any],
        *,
        current_environment: str,
        scope: str,
        scope_key: str | None,
        expected_version: int,
        reason: str,
        request_id: str,
        actor: Mapping[str, Any],
        confirm: bool | None = None,
        operator_signoff: str | None = None,
    ) -> dict[str, Any]:
        validation = self.validate_import(
            payload,
            current_environment=current_environment,
            scope=scope,
            scope_key=scope_key,
        )
        if not validation["valid"]:
            raise SettingsServiceError("validation_failed", "settings import failed validation", errors=validation["errors"])
        assert_can_write_settings(actor, operation="import")
        return self.patch_settings(
            scope=scope,
            scope_key=scope_key,
            patch=validation["settings"],
            expected_version=expected_version,
            reason=reason,
            request_id=request_id,
            actor=actor,
            confirm=confirm,
            operator_signoff=operator_signoff,
        )

    def runtime_status(self, *, scope: str = "global", scope_key: str | None = None) -> dict[str, Any]:
        current = self.get_settings(scope, scope_key)
        return {
            "scope": current["scope"],
            "scope_key": current["scope_key"],
            "desired_version": current["version"],
            "effective_version": None,
            "status": "pending_ack" if current["version"] else "not_published",
        }

    def _scope(self, scope: str, scope_key: str | None) -> tuple[str, str]:
        scope_value = str(scope or "global").strip().lower()
        try:
            key = canonical_scope_key(scope_value, scope_key)
        except ValueError as exc:
            raise SettingsServiceError("invalid_scope", str(exc)) from exc
        return scope_value, key


def apply_patch_to_settings(
    current_settings: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = copy.deepcopy(dict(current_settings or {}))
    for category, fields in (patch or {}).items():
        if not isinstance(fields, Mapping):
            continue
        target = dict(merged.get(category) or {})
        for field_name, value in fields.items():
            if value is None:
                target.pop(field_name, None)
            else:
                target[field_name] = copy.deepcopy(value)
        if target:
            merged[category] = target
        else:
            merged.pop(category, None)
    return merged
