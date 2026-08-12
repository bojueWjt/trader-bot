UPDATE command_node_acks
SET status = 'acked'
WHERE status IN ('accepted', 'running', 'completed');

UPDATE operator_commands
SET status = 'acknowledged'
WHERE status IN ('accepted', 'running');

ALTER TABLE operator_commands
    DROP CONSTRAINT IF EXISTS ck_operator_commands_status;

ALTER TABLE operator_commands
    ADD CONSTRAINT ck_operator_commands_status
        CHECK (
            status IN (
                'pending',
                'acknowledged',
                'partial',
                'failed',
                'completed'
            )
        );

ALTER TABLE command_node_acks
    DROP CONSTRAINT IF EXISTS ck_command_node_acks_status;

ALTER TABLE command_node_acks
    ADD CONSTRAINT ck_command_node_acks_status
        CHECK (status IN ('pending', 'acked', 'failed'));

ALTER TABLE command_node_acks
    DROP CONSTRAINT IF EXISTS ck_command_node_acks_result_object,
    DROP COLUMN IF EXISTS result;

ALTER TABLE risk_state
    DROP COLUMN IF EXISTS version;

DROP TABLE IF EXISTS production_incidents;
DROP TABLE IF EXISTS live_canary_permits;
DROP TRIGGER IF EXISTS trg_reviewed_release_rollout_transition
    ON reviewed_release_rollouts;
DROP FUNCTION IF EXISTS validate_reviewed_release_rollout_transition();
DROP TABLE IF EXISTS reviewed_release_rollout_events;
DROP TABLE IF EXISTS reviewed_release_rollouts;
DROP TABLE IF EXISTS reviewed_release_manifests;

DROP INDEX IF EXISTS idx_node_heartbeats_account_freshness;

ALTER TABLE node_heartbeats
    DROP CONSTRAINT IF EXISTS fk_node_heartbeats_redis_fencing_epoch;

DROP TABLE IF EXISTS redis_fencing_epochs;

ALTER TABLE node_heartbeats
    DROP CONSTRAINT IF EXISTS ck_node_heartbeats_writer_identity_complete,
    DROP CONSTRAINT IF EXISTS ck_node_heartbeats_algo_orders_array,
    DROP CONSTRAINT IF EXISTS ck_node_heartbeats_regular_orders_array,
    DROP CONSTRAINT IF EXISTS ck_node_heartbeats_positions_array,
    DROP COLUMN IF EXISTS reconciliation_completed_at,
    DROP COLUMN IF EXISTS algo_orders_snapshot_at,
    DROP COLUMN IF EXISTS regular_orders_snapshot_at,
    DROP COLUMN IF EXISTS positions_snapshot_at,
    DROP COLUMN IF EXISTS algo_orders,
    DROP COLUMN IF EXISTS regular_orders,
    DROP COLUMN IF EXISTS positions,
    DROP COLUMN IF EXISTS heartbeat_sequence,
    DROP COLUMN IF EXISTS lease_fencing_token,
    DROP COLUMN IF EXISTS runtime_generation,
    DROP COLUMN IF EXISTS redis_fencing_epoch,
    DROP COLUMN IF EXISTS schema_epoch,
    DROP COLUMN IF EXISTS dependency_lock_sha256,
    DROP COLUMN IF EXISTS config_sha256,
    DROP COLUMN IF EXISTS image_digest,
    DROP COLUMN IF EXISTS release_id;
