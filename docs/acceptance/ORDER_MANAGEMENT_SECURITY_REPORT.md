# Order Management Security Report (OM8-08)

Status: implemented and covered by `tests/order_management/security/test_security_audit.py`.

## Controls

| Control | Enforcement point | Proof |
|---|---|---|
| No anonymous settings writes | `services/control-plane/settings/router.py::require_settings_actor` requires `Authorization: Bearer ...` and maps env tokens to `viewer`, `operator`, or `risk_admin`. | `test_no_anonymous_or_viewer_settings_and_command_write` checks anonymous and invalid tokens are rejected for settings patch, rollback, and import. |
| Viewer cannot write settings | `services/control-plane/settings/permissions.py::SETTINGS_ROLE_PERMISSIONS` allows viewer only `read`, `validate`, and `export`; writes flow through `SettingsService` and `assert_can_write_settings`. | `test_no_anonymous_or_viewer_settings_and_command_write` checks viewer PATCH is 403 and operator PATCH succeeds. |
| No anonymous command writes | `services/control-plane/api/read_api.py::issue_operator_command` calls `require_reader`, then requires `risk_admin` for `POST /v1/commands`. | `test_no_anonymous_or_viewer_settings_and_command_write` checks anonymous command write is 401 and viewer command write is 403. |
| Request ID on dangerous commands | `POST /v1/commands` requires `request_id`/`X-Request-Id`, `reason`, and `confirm=true`; `SettingsService.patch_settings` and `rollback_settings` reject empty request IDs. | `test_no_anonymous_or_viewer_settings_and_command_write` and `test_settings_write_commands_require_request_id`. |
| Live-risk relaxation audit | `SettingsService.patch_settings` records `settings.patch` with actor, before/after state, reason, and request_id through `audit_events`. | `test_dangerous_settings_writes_audit_actor_reason_and_request_id`. |
| Rollback audit | `SettingsService.rollback_settings` writes a new immutable settings version and records `settings.rollback` with actor, before/after state, reason, and request_id. | `test_dangerous_settings_writes_audit_actor_reason_and_request_id`. |
| close_all audit | `POST /v1/commands` records an `operator_command` audit event for `CLOSE_ALL` with actor, reason, request_id, and operation in the redacted payload. | `test_close_all_command_records_durable_audit_with_reason_and_request_id`. |
| Settings export excludes secrets | `services/control-plane/settings/import_export.py::sanitize_export` removes secret-like names and descriptor-marked secrets. | `test_no_secret_echo_store_export_or_business_table_secret_columns`. |
| Settings descriptor has no secret business fields | `services/control-plane/settings/schema.py::validate_descriptor` rejects descriptor fields with `secret=true`; unknown fields such as `api_key` fail validation. | `test_no_secret_echo_store_export_or_business_table_secret_columns`. |
| Logs redact secrets | `services/control-plane/security/audit.py::redact_payload` and `order_management.metrics.structured_log_line` redact secret-like keys and values. | `test_no_secret_echo_store_export_or_business_table_secret_columns` reuses the OM8-01 structured log redaction path. |
| Business tables do not store secret columns | `db/migrations/0005_order_management.up.sql` adds order-management tables with operational IDs, state, numeric values, JSON payloads, and audit metadata only. | `test_no_secret_echo_store_export_or_business_table_secret_columns` scans 0005 business-table column names for secret-like columns. |
| Settings API does not echo secret values | `POST /v1/order-management/settings/validate` returns validation errors for unknown secret-like fields without returning the submitted secret value. | `test_no_secret_echo_store_export_or_business_table_secret_columns`. |

## Audit Completeness

Dangerous operations covered by this audit:

| Operation | Required metadata | Storage |
|---|---|---|
| Live-risk relaxation | `actor`, `reason`, `request_id`, `before_state`, `after_state` | `audit_events` typed columns via `record_settings_audit` |
| Settings rollback | `actor`, `reason`, `request_id`, `before_state`, `after_state` | `audit_events` typed columns via `record_settings_audit` |
| `close_all` command | `actor`, `reason`, `request_id`, `operation` | `audit_events` actor column plus redacted payload via `security.audit.record_audit_event` |

## Residual Execution Gate

This report verifies code and offline harness behavior only. Testnet order execution, chaos injection, and rollout remain operator/infra-gated and are not claimed as executed here.
