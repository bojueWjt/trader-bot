# Window A Canonical PostgreSQL ERD

PostgreSQL is the control-plane query and audit source of truth. Raw source ingest, Hermes semantic judgments, risk approvals, and approved trade intents are separate tables with FK traceability through the full chain.

```mermaid
erDiagram
    raw_messages {
        uuid id PK
        text source
        text channel_id
        text source_message_id
        text source_version
        timestamptz source_received_at
        timestamptz ingested_at
        text content_hash
        jsonb raw_payload
    }

    media_assets {
        uuid asset_id PK
        uuid raw_message_id FK
        text sha256
        text object_key
        text mime
        text download_status
    }

    message_processing_runs {
        uuid processing_run_id PK
        uuid raw_message_id FK
        text status
        text model_version
        text prompt_version
        text context_version
    }

    context_snapshots {
        uuid context_snapshot_id PK
        uuid raw_message_id FK
        text snapshot_type
        text context_version
        jsonb snapshot
    }

    hermes_decisions {
        uuid decision_id PK
        uuid raw_message_id FK
        uuid processing_run_id FK
        uuid context_snapshot_id FK
        message_type_v1 message_type
        hermes_action_v1 action
        account_scope_v1 account_scope
        position_side_v1 side
        entry_type_v1 entry_type
        text model_version
        text prompt_version
        text context_version
    }

    risk_decisions {
        uuid risk_decision_id PK
        uuid hermes_decision_id FK
        risk_decision_status status
        text account_id
        text instrument_id
        jsonb risk_budget
        jsonb checks
    }

    trade_intents {
        uuid intent_id PK
        uuid hermes_decision_id FK
        uuid risk_decision_id FK
        approved_trade_action_v1 action
        trade_intent_status status
        text account_id
        text instrument_id
        text idempotency_key UK
    }

    execution_commands {
        uuid command_id PK
        uuid intent_id FK
        text command_type
        execution_command_status status
        text idempotency_key UK
    }

    execution_events {
        uuid execution_event_row_id PK
        text event_id UK
        text node_id
        text account_id
        uuid intent_id FK
        text client_order_id
        text venue_order_id
        text event_type
    }

    orders_projection {
        uuid order_projection_id PK
        text account_id
        uuid intent_id FK
        text updated_from_event_id FK
        text client_order_id
        text venue_order_id
        text status
    }

    positions_projection {
        text account_id PK
        text position_id PK
        text updated_from_event_id FK
        text instrument_id
        position_side_v1 side
        numeric quantity
        text status
    }

    accounts_projection {
        text account_id PK
        reconciliation_state_v1 reconciliation_state
        text updated_from_event_id FK
        numeric equity
        numeric margin
        bigint projection_lag_ms
    }

    risk_state {
        uuid risk_state_id PK
        text account_id
        text instrument_id
        text updated_from_event_id FK
        numeric exposure_notional
        numeric open_risk_fraction
    }

    node_heartbeats {
        text node_id PK
        text account_id
        text status
        timestamptz last_seen_at
    }

    audit_events {
        uuid audit_event_id PK
        text aggregate_type
        text aggregate_id
        uuid trace_id
        uuid raw_message_id FK
        uuid hermes_decision_id FK
        uuid risk_decision_id FK
        uuid intent_id FK
    }

    outbox_events {
        uuid outbox_event_id PK
        outbox_status status
        text aggregate_type
        text aggregate_id
        text event_type
        uuid trace_id
    }

    replay_runs {
        uuid replay_run_id PK
        replay_status status
        text requested_by
        text reason
    }

    replay_results {
        uuid replay_result_id PK
        uuid replay_run_id FK
        uuid raw_message_id FK
        uuid hermes_decision_id FK
        uuid risk_decision_id FK
        uuid intent_id FK
        text result_status
    }

    raw_messages ||--o{ media_assets : has
    raw_messages ||--o{ message_processing_runs : processed_by
    raw_messages ||--o{ context_snapshots : informs
    raw_messages ||--o{ hermes_decisions : classified_into
    message_processing_runs ||--o{ hermes_decisions : produced
    context_snapshots ||--o{ hermes_decisions : used_by
    hermes_decisions ||--o{ risk_decisions : reviewed_by_risk
    hermes_decisions ||--o{ trade_intents : authorizes_semantics
    risk_decisions ||--o{ trade_intents : approves_risk
    trade_intents ||--o{ execution_commands : dispatched_as
    trade_intents ||--o{ execution_events : observed_for
    trade_intents ||--o{ orders_projection : projects
    execution_events ||--o{ orders_projection : updates
    execution_events ||--o{ positions_projection : updates
    execution_events ||--o{ accounts_projection : updates
    execution_events ||--o{ risk_state : updates
    raw_messages ||--o{ audit_events : audited
    hermes_decisions ||--o{ audit_events : audited
    risk_decisions ||--o{ audit_events : audited
    trade_intents ||--o{ audit_events : audited
    replay_runs ||--o{ replay_results : produces
    raw_messages ||--o{ replay_results : compares
    hermes_decisions ||--o{ replay_results : compares
    risk_decisions ||--o{ replay_results : compares
    trade_intents ||--o{ replay_results : compares
```

Execution projection write policy:

- `execution_events`, `orders_projection`, `positions_projection`, and `accounts_projection` are writable only by the separate `nautilus_projection_writer` DB role.
- That role receives `INSERT, UPDATE` only. It is intentionally not granted `SELECT` and has no grants on ingest, semantic, risk, intent, audit, or outbox tables.
