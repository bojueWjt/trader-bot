# Legacy SQLite History Import Mapping

This import is read-only history capture. It writes only to `legacy_*` tables and does not migrate execution state into canonical control-plane, Nautilus runtime/cache, projection, bridge, engine, dashboard, or contracts tables.

## Source To Target Tables

| Source SQLite table | Target PostgreSQL table | Notes |
| --- | --- | --- |
| `signals` | `legacy_signals` | Historical signal rows. `legacy_id` is the source `signal_id`. |
| `signal_events` | `legacy_signal_events` | Historical signal event rows. `legacy_id` is the source integer `id` converted to text. |
| `signal_operations` | `legacy_signal_operations` | Historical operation rows. `legacy_id` is the source integer `id` converted to text. |

## Column Mapping

### `signals` -> `legacy_signals`

| Source column | Target column | Transform |
| --- | --- | --- |
| `signal_id` | `legacy_id` | Copied as text import key. |
| `signal_id` | `signal_id` | Copied unchanged. |
| `status` | `status` | Copied unchanged. |
| `message_type` | `message_type` | Copied unchanged. |
| `pair` | `pair` | Copied unchanged. |
| `payload` | `payload` | Parsed as JSON, recursively removes sensitive keys, stored as `jsonb`. Invalid JSON is stored as a JSON string. |
| `received_at` | `received_at` | Copied unchanged as legacy text. |
| `approved_at` | `approved_at` | Copied unchanged as legacy text. |
| `expires_at` | `expires_at` | Copied unchanged as legacy text. |
| `created_at` | `created_at` | Copied unchanged as legacy text. |
| `updated_at` | `updated_at` | Copied unchanged as legacy text. |
| n/a | `imported_at` | PostgreSQL import timestamp defaulted by DDL. |
| sanitized mapped row | `source_hash` | SHA256 of the sanitized mapped row. |

### `signal_events` -> `legacy_signal_events`

| Source column | Target column | Transform |
| --- | --- | --- |
| `id` | `legacy_id` | Converted to text import key. |
| `id` | `source_event_id` | Copied unchanged. |
| `event_kind` | `event_kind` | Copied unchanged. |
| `event_type` | `event_type` | Copied unchanged. |
| `signal_id` | `signal_id` | Copied unchanged. |
| `payload` | `payload` | Parsed as JSON, recursively removes sensitive keys, stored as `jsonb`. Invalid JSON is stored as a JSON string. |
| `created_at` | `created_at` | Copied unchanged as legacy text. |
| n/a | `imported_at` | PostgreSQL import timestamp defaulted by DDL. |
| sanitized mapped row | `source_hash` | SHA256 of the sanitized mapped row. |

### `signal_operations` -> `legacy_signal_operations`

| Source column | Target column | Transform |
| --- | --- | --- |
| `id` | `legacy_id` | Converted to text import key. |
| `id` | `source_operation_id` | Copied unchanged. |
| `signal_id` | `signal_id` | Copied unchanged. |
| `operation_type` | `operation_type` | Copied unchanged. |
| `status` | `status` | Copied unchanged. |
| `created_at` | `created_at` | Copied unchanged as legacy text. |
| n/a | `imported_at` | PostgreSQL import timestamp defaulted by DDL. |
| sanitized mapped row | `source_hash` | SHA256 of the sanitized mapped row. |

## Skipped And Redacted Fields

Sensitive fields are removed recursively from JSON payload objects before insert and before `source_hash` calculation. Field names are matched case-insensitively by substring:

- `api_key`
- `secret`
- `token`
- `password`
- `credential`

The legacy source schema does not contain top-level columns with these names. If sensitive names appear inside `signals.payload` or `signal_events.payload`, those key/value pairs are omitted from the stored `jsonb` payload.

## Import Key Strategy

Each target table has an import uniqueness constraint on `(legacy_id, source_hash)`.

- `legacy_id` preserves the source row identity.
- `source_hash` is SHA256 over the sanitized mapped row, excluding `imported_at`.
- Re-running the import with unchanged source data uses `ON CONFLICT (legacy_id, source_hash) DO NOTHING`, so no duplicate rows are created.
- A changed non-sensitive source row with the same legacy identity creates a new historical row with a different `source_hash`.
- A change only to a sensitive field does not affect `source_hash`, because sensitive fields are intentionally excluded from history.

The CLI prints one reconciliation line per table:

```text
legacy_signals row_count=<count> sha256=<checksum>
legacy_signal_events row_count=<count> sha256=<checksum>
legacy_signal_operations row_count=<count> sha256=<checksum>
```

The reconciliation checksum is computed from stable target columns and excludes `import_id` and `imported_at`.
