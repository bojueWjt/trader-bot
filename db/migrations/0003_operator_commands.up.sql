-- A-08 canonical kill switch / node command state. Operator commands (HALT/REDUCE/
-- RESUME/CANCEL_ALL/CLOSE_ALL) require per-target-node acknowledgement: a command is
-- only complete once every target node acks. Risk mode itself lives in risk_state.state.

CREATE TABLE operator_commands (
    command_id uuid PRIMARY KEY,
    command_type text NOT NULL,
    scope jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending',
    requested_by text NOT NULL,
    reason text NOT NULL,
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT uq_operator_commands_idempotency UNIQUE (idempotency_key),
    CONSTRAINT ck_operator_commands_type CHECK (
        command_type IN (
            'HALT',
            'REDUCE',
            'RESUME',
            'CANCEL_ALL',
            'CLOSE_ALL',
            'REFRESH_EVIDENCE'
        )
    ),
    CONSTRAINT ck_operator_commands_status CHECK (
        status IN ('pending', 'acknowledged', 'partial', 'failed', 'completed')
    )
);

CREATE INDEX idx_operator_commands_status ON operator_commands (status);

CREATE TABLE command_node_acks (
    command_id uuid NOT NULL REFERENCES operator_commands(command_id) ON DELETE CASCADE,
    node_id text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    detail text,
    ack_at timestamptz,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (command_id, node_id),
    CONSTRAINT ck_command_node_acks_status CHECK (status IN ('pending', 'acked', 'failed'))
);

CREATE INDEX idx_command_node_acks_status ON command_node_acks (status);
