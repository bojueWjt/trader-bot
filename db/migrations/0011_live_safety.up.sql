ALTER TABLE node_heartbeats
    ADD COLUMN IF NOT EXISTS release_id text,
    ADD COLUMN IF NOT EXISTS image_digest text,
    ADD COLUMN IF NOT EXISTS config_sha256 text,
    ADD COLUMN IF NOT EXISTS dependency_lock_sha256 text,
    ADD COLUMN IF NOT EXISTS schema_epoch text,
    ADD COLUMN IF NOT EXISTS positions jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS regular_orders jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS algo_orders jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS positions_snapshot_at timestamptz,
    ADD COLUMN IF NOT EXISTS regular_orders_snapshot_at timestamptz,
    ADD COLUMN IF NOT EXISTS algo_orders_snapshot_at timestamptz,
    ADD COLUMN IF NOT EXISTS reconciliation_completed_at timestamptz,
    ADD COLUMN IF NOT EXISTS redis_fencing_epoch uuid,
    ADD COLUMN IF NOT EXISTS runtime_generation text,
    ADD COLUMN IF NOT EXISTS lease_fencing_token bigint,
    ADD COLUMN IF NOT EXISTS heartbeat_sequence bigint;

ALTER TABLE risk_state
    ADD COLUMN IF NOT EXISTS version bigint NOT NULL DEFAULT 1;

ALTER TABLE node_heartbeats
    ADD CONSTRAINT ck_node_heartbeats_positions_array
        CHECK (jsonb_typeof(positions) = 'array'),
    ADD CONSTRAINT ck_node_heartbeats_regular_orders_array
        CHECK (jsonb_typeof(regular_orders) = 'array'),
    ADD CONSTRAINT ck_node_heartbeats_algo_orders_array
        CHECK (jsonb_typeof(algo_orders) = 'array'),
    ADD CONSTRAINT ck_node_heartbeats_writer_identity_complete
        CHECK (
            (
                redis_fencing_epoch IS NULL
                AND runtime_generation IS NULL
                AND lease_fencing_token IS NULL
                AND heartbeat_sequence IS NULL
            )
            OR (
                redis_fencing_epoch IS NOT NULL
                AND runtime_generation IS NOT NULL
                AND btrim(runtime_generation) <> ''
                AND lease_fencing_token > 0
                AND heartbeat_sequence > 0
            )
        );

CREATE TABLE redis_fencing_epochs (
    redis_fencing_epoch uuid PRIMARY KEY,
    domain text NOT NULL,
    status text NOT NULL,
    marker_sha256 text NOT NULL,
    capacity_evidence_sha256 text NOT NULL,
    initial_redis_run_id text NOT NULL,
    active_volume text NOT NULL,
    activated_by text NOT NULL,
    activated_at timestamptz NOT NULL,
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_redis_fencing_epochs_domain
        CHECK (domain = 'trader-v3'),
    CONSTRAINT ck_redis_fencing_epochs_status
        CHECK (status IN ('active', 'retired')),
    CONSTRAINT ck_redis_fencing_epochs_marker_sha256
        CHECK (marker_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_redis_fencing_epochs_evidence_sha256
        CHECK (capacity_evidence_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_redis_fencing_epochs_run_id
        CHECK (initial_redis_run_id ~ '^[0-9a-f]{40}$'),
    CONSTRAINT ck_redis_fencing_epochs_volume
        CHECK (btrim(active_volume) <> ''),
    CONSTRAINT ck_redis_fencing_epochs_activated_by
        CHECK (btrim(activated_by) <> ''),
    CONSTRAINT ck_redis_fencing_epochs_lifecycle
        CHECK (
            (
                status = 'active'
                AND retired_at IS NULL
            )
            OR (
                status = 'retired'
                AND retired_at IS NOT NULL
            )
        )
);

CREATE UNIQUE INDEX uq_redis_fencing_epochs_active
    ON redis_fencing_epochs (domain)
    WHERE status = 'active';

ALTER TABLE node_heartbeats
    ADD CONSTRAINT fk_node_heartbeats_redis_fencing_epoch
        FOREIGN KEY (redis_fencing_epoch)
        REFERENCES redis_fencing_epochs(redis_fencing_epoch);

CREATE INDEX idx_node_heartbeats_account_freshness
    ON node_heartbeats (account_id, last_seen_at DESC);

CREATE TABLE reviewed_release_manifests (
    account_id text NOT NULL,
    release_id text NOT NULL,
    image_digest text NOT NULL,
    config_sha256 text NOT NULL,
    dependency_lock_sha256 text NOT NULL,
    schema_epoch text NOT NULL,
    review_status text NOT NULL DEFAULT 'pending',
    reviewed_by text,
    reviewed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, release_id),
    CONSTRAINT ck_reviewed_release_manifests_status
        CHECK (review_status IN ('pending', 'reviewed', 'rejected', 'revoked')),
    CONSTRAINT ck_reviewed_release_manifests_reviewer
        CHECK (
            review_status <> 'reviewed'
            OR (reviewed_by IS NOT NULL AND btrim(reviewed_by) <> '')
        )
);

CREATE TABLE reviewed_release_rollouts (
    release_id text PRIMARY KEY,
    redis_fencing_epoch uuid NOT NULL
        REFERENCES redis_fencing_epochs(redis_fencing_epoch),
    image_digest text NOT NULL,
    config_sha256 text NOT NULL,
    dependency_lock_sha256 text NOT NULL,
    schema_epoch text NOT NULL,
    manifest_sha256 text NOT NULL,
    bundle_manifest_sha256 text NOT NULL,
    registration_idempotency_key text NOT NULL UNIQUE,
    phase text NOT NULL DEFAULT 'account_a_canary',
    phase_version integer NOT NULL DEFAULT 1,
    reviewed_by text NOT NULL,
    reviewed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_reviewed_release_rollouts_identity
        UNIQUE (
            release_id,
            redis_fencing_epoch,
            image_digest,
            config_sha256,
            dependency_lock_sha256,
            schema_epoch
        ),
    CONSTRAINT ck_reviewed_release_rollouts_manifest_sha256
        CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_reviewed_release_rollouts_bundle_sha256
        CHECK (bundle_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_reviewed_release_rollouts_phase
        CHECK (
            phase IN (
                'account_a_canary',
                'account_b_rollout',
                'fleet_complete',
                'aborted'
            )
        ),
    CONSTRAINT ck_reviewed_release_rollouts_phase_version
        CHECK (phase_version > 0),
    CONSTRAINT ck_reviewed_release_rollouts_reviewer
        CHECK (btrim(reviewed_by) <> ''),
    CONSTRAINT ck_reviewed_release_rollouts_idempotency
        CHECK (btrim(registration_idempotency_key) <> '')
);

CREATE UNIQUE INDEX uq_reviewed_release_rollouts_active
    ON reviewed_release_rollouts ((true))
    WHERE phase IN ('account_a_canary', 'account_b_rollout');

CREATE TABLE reviewed_release_rollout_events (
    rollout_event_id uuid PRIMARY KEY,
    release_id text NOT NULL
        REFERENCES reviewed_release_rollouts(release_id),
    event_type text NOT NULL,
    from_phase text,
    to_phase text NOT NULL,
    phase_version integer NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    actor text NOT NULL,
    reason text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_reviewed_release_rollout_events_version
        UNIQUE (release_id, phase_version),
    CONSTRAINT ck_reviewed_release_rollout_events_type
        CHECK (event_type IN ('registered', 'phase_transition')),
    CONSTRAINT ck_reviewed_release_rollout_events_from_phase
        CHECK (
            from_phase IS NULL
            OR from_phase IN (
                'account_a_canary',
                'account_b_rollout',
                'fleet_complete',
                'aborted'
            )
        ),
    CONSTRAINT ck_reviewed_release_rollout_events_to_phase
        CHECK (
            to_phase IN (
                'account_a_canary',
                'account_b_rollout',
                'fleet_complete',
                'aborted'
            )
        ),
    CONSTRAINT ck_reviewed_release_rollout_events_shape
        CHECK (
            (
                event_type = 'registered'
                AND from_phase IS NULL
                AND to_phase = 'account_a_canary'
                AND phase_version = 1
            )
            OR (
                event_type = 'phase_transition'
                AND from_phase IS NOT NULL
                AND phase_version > 1
            )
        ),
    CONSTRAINT ck_reviewed_release_rollout_events_idempotency
        CHECK (btrim(idempotency_key) <> ''),
    CONSTRAINT ck_reviewed_release_rollout_events_actor
        CHECK (btrim(actor) <> ''),
    CONSTRAINT ck_reviewed_release_rollout_events_reason
        CHECK (btrim(reason) <> ''),
    CONSTRAINT ck_reviewed_release_rollout_events_evidence
        CHECK (jsonb_typeof(evidence) = 'object')
);

CREATE INDEX idx_reviewed_release_rollout_events_release
    ON reviewed_release_rollout_events (release_id, phase_version);

CREATE FUNCTION validate_reviewed_release_rollout_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.phase <> 'account_a_canary' OR NEW.phase_version <> 1 THEN
            RAISE EXCEPTION
                'reviewed release rollout must start at account_a_canary version 1';
        END IF;
        RETURN NEW;
    END IF;

    IF (
        NEW.release_id,
        NEW.redis_fencing_epoch,
        NEW.image_digest,
        NEW.config_sha256,
        NEW.dependency_lock_sha256,
        NEW.schema_epoch,
        NEW.manifest_sha256,
        NEW.bundle_manifest_sha256,
        NEW.registration_idempotency_key,
        NEW.reviewed_by,
        NEW.reviewed_at,
        NEW.created_at
    ) IS DISTINCT FROM (
        OLD.release_id,
        OLD.redis_fencing_epoch,
        OLD.image_digest,
        OLD.config_sha256,
        OLD.dependency_lock_sha256,
        OLD.schema_epoch,
        OLD.manifest_sha256,
        OLD.bundle_manifest_sha256,
        OLD.registration_idempotency_key,
        OLD.reviewed_by,
        OLD.reviewed_at,
        OLD.created_at
    ) THEN
        RAISE EXCEPTION 'reviewed release rollout identity is immutable';
    END IF;

    IF NEW.phase_version <> OLD.phase_version + 1 THEN
        RAISE EXCEPTION 'reviewed release rollout phase_version must increment by one';
    END IF;

    IF NOT (
        (
            OLD.phase = 'account_a_canary'
            AND NEW.phase IN ('account_b_rollout', 'aborted')
        )
        OR (
            OLD.phase = 'account_b_rollout'
            AND NEW.phase IN ('fleet_complete', 'aborted')
        )
    ) THEN
        RAISE EXCEPTION
            'invalid reviewed release rollout transition: % -> %',
            OLD.phase,
            NEW.phase;
    END IF;

    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_reviewed_release_rollout_transition
    BEFORE INSERT OR UPDATE ON reviewed_release_rollouts
    FOR EACH ROW
    EXECUTE FUNCTION validate_reviewed_release_rollout_transition();

CREATE TABLE live_canary_permits (
    permit_id uuid PRIMARY KEY,
    account_id text NOT NULL,
    symbol text NOT NULL,
    max_notional_usdt numeric NOT NULL,
    max_cumulative_loss_usdt numeric NOT NULL,
    max_open_count integer NOT NULL DEFAULT 1,
    consumed_open_count integer NOT NULL DEFAULT 0,
    expires_at timestamptz NOT NULL,
    release_id text NOT NULL,
    testnet_emergency_close_evidence_sha256 text NOT NULL,
    testnet_emergency_close_verified_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'issued',
    issued_by text NOT NULL,
    issued_at timestamptz NOT NULL DEFAULT now(),
    armed_at timestamptz,
    armed_node_id text,
    portfolio_baseline_sha256 text,
    consumed_at timestamptz,
    consumed_intent_id uuid REFERENCES trade_intents(intent_id),
    closed_at timestamptz,
    FOREIGN KEY (account_id, release_id)
        REFERENCES reviewed_release_manifests(account_id, release_id),
    CONSTRAINT ck_live_canary_permits_account
        CHECK (account_id = 'account-a'),
    CONSTRAINT ck_live_canary_permits_symbol
        CHECK (btrim(symbol) <> '' AND symbol = upper(symbol)),
    CONSTRAINT ck_live_canary_permits_notional
        CHECK (max_notional_usdt > 0 AND max_notional_usdt <= 12),
    CONSTRAINT ck_live_canary_permits_cumulative_loss
        CHECK (
            max_cumulative_loss_usdt > 0
            AND max_cumulative_loss_usdt < 1.5
        ),
    CONSTRAINT ck_live_canary_permits_emergency_close_evidence
        CHECK (
            testnet_emergency_close_evidence_sha256
                ~ '^[0-9a-f]{64}$'
        ),
    CONSTRAINT ck_live_canary_permits_emergency_close_verified_at
        CHECK (
            testnet_emergency_close_verified_at
                <= issued_at + interval '1 minute'
        ),
    CONSTRAINT ck_live_canary_permits_open_count
        CHECK (max_open_count = 1),
    CONSTRAINT ck_live_canary_permits_consumed_count
        CHECK (
            consumed_open_count >= 0
            AND consumed_open_count <= max_open_count
        ),
    CONSTRAINT ck_live_canary_permits_status
        CHECK (
            status IN (
                'issued',
                'armed',
                'consumed',
                'closed',
                'revoked',
                'expired'
            )
        ),
    CONSTRAINT ck_live_canary_permits_consumed_identity
        CHECK (
            status NOT IN ('consumed', 'closed')
            OR (
                consumed_open_count = 1
                AND consumed_at IS NOT NULL
                AND consumed_intent_id IS NOT NULL
            )
        ),
    CONSTRAINT ck_live_canary_permits_portfolio_baseline
        CHECK (
            status NOT IN ('armed', 'consumed', 'closed')
            OR (
                portfolio_baseline_sha256 IS NOT NULL
                AND portfolio_baseline_sha256 ~ '^[0-9a-f]{64}$'
            )
        )
);

CREATE INDEX idx_live_canary_permits_account_status_expiry
    ON live_canary_permits (account_id, status, expires_at);

CREATE TABLE production_incidents (
    incident_id uuid PRIMARY KEY,
    account_id text NOT NULL,
    severity text NOT NULL,
    status text NOT NULL DEFAULT 'open',
    summary text NOT NULL,
    opened_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_production_incidents_severity
        CHECK (severity IN ('P0', 'P1', 'P2')),
    CONSTRAINT ck_production_incidents_status
        CHECK (status IN ('open', 'closed')),
    CONSTRAINT ck_production_incidents_closed_at
        CHECK (
            (status = 'open' AND closed_at IS NULL)
            OR (status = 'closed' AND closed_at IS NOT NULL)
        )
);

CREATE INDEX idx_production_incidents_open_account
    ON production_incidents (account_id, severity)
    WHERE status = 'open';

ALTER TABLE command_node_acks
    ADD COLUMN IF NOT EXISTS result jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE command_node_acks
    ADD CONSTRAINT ck_command_node_acks_result_object
        CHECK (jsonb_typeof(result) = 'object');

ALTER TABLE command_node_acks
    DROP CONSTRAINT ck_command_node_acks_status;

ALTER TABLE command_node_acks
    ADD CONSTRAINT ck_command_node_acks_status
        CHECK (
            status IN (
                'pending',
                'acked',
                'accepted',
                'running',
                'completed',
                'failed'
            )
        );

ALTER TABLE operator_commands
    DROP CONSTRAINT ck_operator_commands_status;

ALTER TABLE operator_commands
    ADD CONSTRAINT ck_operator_commands_status
        CHECK (
            status IN (
                'pending',
                'accepted',
                'running',
                'acknowledged',
                'partial',
                'failed',
                'completed'
            )
        );
