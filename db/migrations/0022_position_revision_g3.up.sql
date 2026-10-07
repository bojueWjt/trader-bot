-- Monotonic semantic generations survive flat/reopen and process restarts.
CREATE TABLE position_revisions (
    account_id text NOT NULL,
    instrument_id text NOT NULL,
    position_side text NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, instrument_id, position_side),
    CHECK (
        (instrument_id = '*' AND position_side = '*') OR
        (instrument_id ~ '^[A-Z0-9_]+$' AND position_side IN ('LONG', 'SHORT'))
    )
);

CREATE TABLE position_revision_invalidations (
    account_id text NOT NULL,
    operation_id text NOT NULL CHECK (length(btrim(operation_id)) > 0),
    instrument_id text NOT NULL,
    position_side text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, operation_id),
    CHECK (
        (instrument_id = '*' AND position_side = '*') OR
        (instrument_id ~ '^[A-Z0-9_]+$' AND position_side IN ('LONG', 'SHORT'))
    )
);

GRANT SELECT, INSERT, UPDATE ON position_revisions TO trader_v3_operator_query;
GRANT SELECT, INSERT ON position_revision_invalidations TO trader_v3_operator_query;
GRANT SELECT ON position_revisions, position_revision_invalidations
    TO trader_v3_node_control, trader_v3_event_ingest;
