CREATE TABLE IF NOT EXISTS legacy_signals (
    import_id BIGSERIAL PRIMARY KEY,
    legacy_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'raw',
    message_type TEXT NOT NULL DEFAULT 'new_signal',
    pair TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_hash TEXT NOT NULL,
    CONSTRAINT uq_legacy_signals_import_key UNIQUE (legacy_id, source_hash),
    CONSTRAINT ck_legacy_signals_source_hash_sha256 CHECK (source_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_legacy_signals_signal_id
    ON legacy_signals (signal_id);

CREATE INDEX IF NOT EXISTS idx_legacy_signals_pair_received
    ON legacy_signals (pair, received_at);

CREATE TABLE IF NOT EXISTS legacy_signal_events (
    import_id BIGSERIAL PRIMARY KEY,
    legacy_id TEXT NOT NULL,
    source_event_id BIGINT NOT NULL,
    event_kind TEXT NOT NULL DEFAULT 'audit',
    event_type TEXT NOT NULL DEFAULT '',
    signal_id TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TEXT NOT NULL DEFAULT '',
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_hash TEXT NOT NULL,
    CONSTRAINT uq_legacy_signal_events_import_key UNIQUE (legacy_id, source_hash),
    CONSTRAINT ck_legacy_signal_events_source_hash_sha256 CHECK (source_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_legacy_signal_events_signal_id
    ON legacy_signal_events (signal_id);

CREATE INDEX IF NOT EXISTS idx_legacy_signal_events_created_at
    ON legacy_signal_events (created_at);

CREATE TABLE IF NOT EXISTS legacy_signal_operations (
    import_id BIGSERIAL PRIMARY KEY,
    legacy_id TEXT NOT NULL,
    source_operation_id BIGINT NOT NULL,
    signal_id TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved',
    created_at TEXT NOT NULL DEFAULT '',
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_hash TEXT NOT NULL,
    CONSTRAINT uq_legacy_signal_operations_import_key UNIQUE (legacy_id, source_hash),
    CONSTRAINT ck_legacy_signal_operations_source_hash_sha256 CHECK (source_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_legacy_signal_operations_signal_id
    ON legacy_signal_operations (signal_id);

CREATE INDEX IF NOT EXISTS idx_legacy_signal_operations_created_at
    ON legacy_signal_operations (created_at);
