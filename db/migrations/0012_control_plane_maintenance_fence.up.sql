DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname='trader_v3_node_control'
    ) THEN
        CREATE ROLE trader_v3_node_control LOGIN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname='trader_v3_event_ingest'
    ) THEN
        CREATE ROLE trader_v3_event_ingest LOGIN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname='trader_v3_operator_query'
    ) THEN
        CREATE ROLE trader_v3_operator_query LOGIN;
    END IF;
END
$$;

ALTER ROLE trader_v3_node_control
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 16;
ALTER ROLE trader_v3_event_ingest
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 24;
ALTER ROLE trader_v3_operator_query
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 16;

DO $$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO %I, %I, %I',
        current_database(),
        'trader_v3_node_control',
        'trader_v3_event_ingest',
        'trader_v3_operator_query'
    );
END
$$;

GRANT USAGE ON SCHEMA public
    TO trader_v3_node_control,
       trader_v3_event_ingest,
       trader_v3_operator_query;

CREATE TABLE control_plane_maintenance_fences (
    fence_id uuid PRIMARY KEY,
    domain text NOT NULL DEFAULT 'trader-v3',
    operation text NOT NULL,
    actor text NOT NULL,
    owner_token_sha256 text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    lease_version bigint NOT NULL DEFAULT 1,
    acquired_at timestamptz NOT NULL DEFAULT now(),
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    released_at timestamptz,
    release_reason text,
    last_stage text NOT NULL DEFAULT 'acquired',
    account_evidence jsonb NOT NULL,
    CONSTRAINT ck_control_plane_maintenance_fence_domain
        CHECK (domain = 'trader-v3'),
    CONSTRAINT ck_control_plane_maintenance_fence_operation
        CHECK (operation ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'),
    CONSTRAINT ck_control_plane_maintenance_fence_actor
        CHECK (btrim(actor) <> ''),
    CONSTRAINT ck_control_plane_maintenance_fence_owner_hash
        CHECK (owner_token_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_control_plane_maintenance_fence_status
        CHECK (status IN ('active', 'released', 'expired')),
    CONSTRAINT ck_control_plane_maintenance_fence_lease_version
        CHECK (lease_version > 0),
    CONSTRAINT ck_control_plane_maintenance_fence_expiry
        CHECK (expires_at > acquired_at),
    CONSTRAINT ck_control_plane_maintenance_fence_release
        CHECK (
            (
                status = 'active'
                AND released_at IS NULL
                AND release_reason IS NULL
            )
            OR (
                status IN ('released', 'expired')
                AND released_at IS NOT NULL
                AND release_reason IS NOT NULL
                AND btrim(release_reason) <> ''
            )
        ),
    CONSTRAINT ck_control_plane_maintenance_fence_evidence
        CHECK (
            jsonb_typeof(account_evidence) = 'array'
            AND jsonb_array_length(account_evidence) = 2
        )
);

CREATE UNIQUE INDEX uq_control_plane_maintenance_fence_active
    ON control_plane_maintenance_fences (domain)
    WHERE status='active';

CREATE INDEX idx_control_plane_maintenance_fence_expiry
    ON control_plane_maintenance_fences (status, expires_at);

CREATE TABLE control_plane_maintenance_fence_events (
    maintenance_event_id bigserial PRIMARY KEY,
    fence_id uuid NOT NULL
        REFERENCES control_plane_maintenance_fences(fence_id),
    event_type text NOT NULL,
    operation text NOT NULL,
    actor text NOT NULL,
    stage text NOT NULL,
    lease_version bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    account_evidence jsonb NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_control_plane_maintenance_fence_event_type
        CHECK (
            event_type IN (
                'acquired',
                'taken_over',
                'stage_verified',
                'released',
                'expired'
            )
        ),
    CONSTRAINT ck_control_plane_maintenance_fence_event_actor
        CHECK (btrim(actor) <> ''),
    CONSTRAINT ck_control_plane_maintenance_fence_event_stage
        CHECK (btrim(stage) <> ''),
    CONSTRAINT ck_control_plane_maintenance_fence_event_lease
        CHECK (lease_version > 0),
    CONSTRAINT ck_control_plane_maintenance_fence_event_evidence
        CHECK (
            jsonb_typeof(account_evidence) = 'array'
            AND jsonb_array_length(account_evidence) = 2
        ),
    CONSTRAINT ck_control_plane_maintenance_fence_event_details
        CHECK (jsonb_typeof(details) = 'object')
);

CREATE INDEX idx_control_plane_maintenance_fence_events_fence
    ON control_plane_maintenance_fence_events (
        fence_id,
        maintenance_event_id
    );

CREATE FUNCTION reject_control_plane_maintenance_fence_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'control-plane maintenance fence audit events are append-only';
END;
$$;

CREATE TRIGGER trg_control_plane_maintenance_fence_events_append_only
    BEFORE UPDATE OR DELETE ON control_plane_maintenance_fence_events
    FOR EACH ROW
    EXECUTE FUNCTION reject_control_plane_maintenance_fence_event_mutation();

CREATE FUNCTION capture_control_plane_maintenance_evidence(
    p_heartbeat_max_age_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    active_redis_fencing_epoch uuid;
    evidence jsonb := '[]'::jsonb;
    heartbeat_record record;
    seen_account_a boolean := false;
    seen_account_b boolean := false;
BEGIN
    IF (
        p_heartbeat_max_age_seconds < 1
        OR p_heartbeat_max_age_seconds > 60
    ) THEN
        RAISE EXCEPTION
            'heartbeat max age must be between 1 and 60 seconds';
    END IF;

    SELECT redis_fencing_epoch
    INTO active_redis_fencing_epoch
    FROM redis_fencing_epochs
    WHERE domain='trader-v3'
      AND status='active'
    FOR SHARE;

    IF active_redis_fencing_epoch IS NULL THEN
        RAISE EXCEPTION
            'active trader-v3 Redis fencing epoch is unavailable';
    END IF;

    FOR heartbeat_record IN
        SELECT node_id,
               account_id,
               status,
               release_id,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               redis_fencing_epoch,
               runtime_generation,
               lease_fencing_token,
               heartbeat_sequence,
               last_seen_at
        FROM node_heartbeats
        WHERE account_id IN ('account-a', 'account-b')
          AND last_seen_at >= (
              clock_timestamp()
              - make_interval(secs => p_heartbeat_max_age_seconds)
          )
        ORDER BY account_id, node_id
        FOR UPDATE
    LOOP
        IF heartbeat_record.account_id = 'account-a' THEN
            IF seen_account_a THEN
                RAISE EXCEPTION
                    'maintenance fence requires exactly one fresh account-a and account-b heartbeat';
            END IF;
            seen_account_a := true;
        ELSIF heartbeat_record.account_id = 'account-b' THEN
            IF seen_account_b THEN
                RAISE EXCEPTION
                    'maintenance fence requires exactly one fresh account-a and account-b heartbeat';
            END IF;
            seen_account_b := true;
        END IF;

        IF upper(COALESCE(heartbeat_record.status, '')) <> 'HALTED' THEN
            RAISE EXCEPTION
                '% must be HALTED before shared maintenance',
                heartbeat_record.account_id;
        END IF;
        IF (
            heartbeat_record.node_id IS NULL
            OR btrim(heartbeat_record.node_id) = ''
            OR heartbeat_record.release_id IS NULL
            OR btrim(heartbeat_record.release_id) = ''
            OR heartbeat_record.image_digest IS NULL
            OR btrim(heartbeat_record.image_digest) = ''
            OR heartbeat_record.config_sha256 IS NULL
            OR btrim(heartbeat_record.config_sha256) = ''
            OR heartbeat_record.dependency_lock_sha256 IS NULL
            OR btrim(heartbeat_record.dependency_lock_sha256) = ''
            OR heartbeat_record.schema_epoch IS NULL
            OR btrim(heartbeat_record.schema_epoch) = ''
            OR heartbeat_record.runtime_generation IS NULL
            OR btrim(heartbeat_record.runtime_generation) = ''
            OR heartbeat_record.lease_fencing_token IS NULL
            OR heartbeat_record.lease_fencing_token <= 0
            OR heartbeat_record.heartbeat_sequence IS NULL
            OR heartbeat_record.heartbeat_sequence <= 0
        ) THEN
            RAISE EXCEPTION
                '% heartbeat identity is incomplete',
                heartbeat_record.account_id;
        END IF;
        IF (
            heartbeat_record.redis_fencing_epoch IS DISTINCT FROM
            active_redis_fencing_epoch
        ) THEN
            RAISE EXCEPTION
                '% heartbeat Redis fencing epoch is stale',
                heartbeat_record.account_id;
        END IF;

        evidence := evidence || jsonb_build_array(
            jsonb_build_object(
                'account_id', heartbeat_record.account_id,
                'node_id', heartbeat_record.node_id,
                'status', upper(heartbeat_record.status),
                'release_id', heartbeat_record.release_id,
                'image_digest', heartbeat_record.image_digest,
                'config_sha256', heartbeat_record.config_sha256,
                'dependency_lock_sha256',
                    heartbeat_record.dependency_lock_sha256,
                'schema_epoch', heartbeat_record.schema_epoch,
                'redis_fencing_epoch',
                    heartbeat_record.redis_fencing_epoch::text,
                'runtime_generation',
                    heartbeat_record.runtime_generation,
                'lease_fencing_token',
                    heartbeat_record.lease_fencing_token,
                'heartbeat_sequence',
                    heartbeat_record.heartbeat_sequence,
                'last_seen_at', heartbeat_record.last_seen_at
            )
        );
    END LOOP;

    IF NOT seen_account_a OR NOT seen_account_b THEN
        RAISE EXCEPTION
            'maintenance fence requires exactly one fresh account-a and account-b heartbeat';
    END IF;
    IF jsonb_array_length(evidence) <> 2 THEN
        RAISE EXCEPTION
            'maintenance fence requires exactly one fresh account-a and account-b heartbeat';
    END IF;
    RETURN evidence;
END;
$$;

CREATE FUNCTION acquire_control_plane_maintenance_fence(
    p_fence_id uuid,
    p_operation text,
    p_actor text,
    p_owner_token text,
    p_lease_seconds integer,
    p_heartbeat_max_age_seconds integer
)
RETURNS TABLE (
    fence_id uuid,
    lease_version bigint,
    expires_at timestamptz,
    account_evidence jsonb
)
LANGUAGE plpgsql
AS $$
DECLARE
    current_fence control_plane_maintenance_fences%ROWTYPE;
    current_evidence jsonb;
    owner_token_sha256 text;
    takeover_fence_id uuid;
    lease_expires_at timestamptz;
BEGIN
    IF p_fence_id IS NULL THEN
        RAISE EXCEPTION 'maintenance fence_id is required';
    END IF;
    IF p_operation !~ '^[a-z0-9][a-z0-9._:-]{0,127}$' THEN
        RAISE EXCEPTION 'maintenance operation is invalid';
    END IF;
    IF p_actor IS NULL OR btrim(p_actor) = '' THEN
        RAISE EXCEPTION 'maintenance actor is required';
    END IF;
    IF p_owner_token !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'maintenance owner token is invalid';
    END IF;
    IF p_lease_seconds < 15 OR p_lease_seconds > 300 THEN
        RAISE EXCEPTION
            'maintenance lease must be between 15 and 300 seconds';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtext('trader-v3-control-plane-maintenance-fence')
    );
    owner_token_sha256 := encode(
        sha256(convert_to(p_owner_token, 'UTF8')),
        'hex'
    );

    SELECT *
    INTO current_fence
    FROM control_plane_maintenance_fences
    WHERE domain='trader-v3'
      AND status='active'
    ORDER BY acquired_at DESC
    LIMIT 1
    FOR UPDATE;

    IF current_fence.fence_id IS NOT NULL THEN
        IF current_fence.expires_at > clock_timestamp() THEN
            RAISE EXCEPTION
                'active maintenance fence % is held by % until %',
                current_fence.fence_id,
                current_fence.actor,
                current_fence.expires_at;
        END IF;
        takeover_fence_id := current_fence.fence_id;
        UPDATE control_plane_maintenance_fences
        SET status='expired',
            released_at=clock_timestamp(),
            release_reason='lease expired before takeover',
            last_stage='expired'
        WHERE control_plane_maintenance_fences.fence_id
            = current_fence.fence_id;
        INSERT INTO control_plane_maintenance_fence_events (
            fence_id,
            event_type,
            operation,
            actor,
            stage,
            lease_version,
            expires_at,
            account_evidence,
            details
        )
        VALUES (
            current_fence.fence_id,
            'expired',
            current_fence.operation,
            p_actor,
            'expired',
            current_fence.lease_version,
            current_fence.expires_at,
            current_fence.account_evidence,
            jsonb_build_object('takeover_fence_id', p_fence_id)
        );
    END IF;

    current_evidence := capture_control_plane_maintenance_evidence(
        p_heartbeat_max_age_seconds
    );
    lease_expires_at := clock_timestamp()
        + make_interval(secs => p_lease_seconds);

    INSERT INTO control_plane_maintenance_fences (
        fence_id,
        operation,
        actor,
        owner_token_sha256,
        expires_at,
        account_evidence
    )
    VALUES (
        p_fence_id,
        p_operation,
        p_actor,
        owner_token_sha256,
        lease_expires_at,
        current_evidence
    );

    INSERT INTO control_plane_maintenance_fence_events (
        fence_id,
        event_type,
        operation,
        actor,
        stage,
        lease_version,
        expires_at,
        account_evidence,
        details
    )
    VALUES (
        p_fence_id,
        CASE
            WHEN takeover_fence_id IS NULL THEN 'acquired'
            ELSE 'taken_over'
        END,
        p_operation,
        p_actor,
        'acquired',
        1,
        lease_expires_at,
        current_evidence,
        CASE
            WHEN takeover_fence_id IS NULL THEN '{}'::jsonb
            ELSE jsonb_build_object(
                'expired_fence_id',
                takeover_fence_id
            )
        END
    );

    RETURN QUERY
    SELECT p_fence_id, 1::bigint, lease_expires_at, current_evidence;
END;
$$;

CREATE FUNCTION verify_control_plane_maintenance_fence(
    p_fence_id uuid,
    p_owner_token text,
    p_stage text,
    p_lease_seconds integer,
    p_heartbeat_max_age_seconds integer
)
RETURNS TABLE (
    fence_id uuid,
    lease_version bigint,
    expires_at timestamptz,
    account_evidence jsonb
)
LANGUAGE plpgsql
AS $$
DECLARE
    current_fence control_plane_maintenance_fences%ROWTYPE;
    current_evidence jsonb;
    current_identity jsonb;
    expected_identity jsonb;
    owner_token_sha256 text;
    lease_expires_at timestamptz;
BEGIN
    IF p_owner_token !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'maintenance owner token is invalid';
    END IF;
    IF p_stage !~ '^[a-z0-9][a-z0-9._:-]{0,127}$' THEN
        RAISE EXCEPTION 'maintenance stage is invalid';
    END IF;
    IF p_lease_seconds < 15 OR p_lease_seconds > 300 THEN
        RAISE EXCEPTION
            'maintenance lease must be between 15 and 300 seconds';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtext('trader-v3-control-plane-maintenance-fence')
    );
    owner_token_sha256 := encode(
        sha256(convert_to(p_owner_token, 'UTF8')),
        'hex'
    );
    SELECT *
    INTO current_fence
    FROM control_plane_maintenance_fences
    WHERE control_plane_maintenance_fences.fence_id=p_fence_id
    FOR UPDATE;

    IF current_fence.fence_id IS NULL THEN
        RAISE EXCEPTION 'maintenance fence is unavailable';
    END IF;
    IF (
        current_fence.status <> 'active'
        OR current_fence.expires_at <= clock_timestamp()
    ) THEN
        RAISE EXCEPTION 'maintenance fence is inactive or expired';
    END IF;
    IF current_fence.owner_token_sha256 <> owner_token_sha256 THEN
        RAISE EXCEPTION 'maintenance fence owner token mismatch';
    END IF;

    current_evidence := capture_control_plane_maintenance_evidence(
        p_heartbeat_max_age_seconds
    );
    SELECT jsonb_agg(
        value - 'heartbeat_sequence' - 'last_seen_at'
        ORDER BY value->>'account_id'
    )
    INTO current_identity
    FROM jsonb_array_elements(current_evidence);
    SELECT jsonb_agg(
        value - 'heartbeat_sequence' - 'last_seen_at'
        ORDER BY value->>'account_id'
    )
    INTO expected_identity
    FROM jsonb_array_elements(current_fence.account_evidence);

    IF current_identity IS DISTINCT FROM expected_identity THEN
        RAISE EXCEPTION
            'maintenance fence A/B node, writer, Redis, or release identity drifted';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(current_evidence) AS current_item
        JOIN jsonb_array_elements(
            current_fence.account_evidence
        ) AS expected_item
          ON current_item->>'account_id'
             = expected_item->>'account_id'
        WHERE (current_item->>'heartbeat_sequence')::bigint
            < (expected_item->>'heartbeat_sequence')::bigint
    ) THEN
        RAISE EXCEPTION
            'maintenance fence heartbeat sequence regressed';
    END IF;

    lease_expires_at := clock_timestamp()
        + make_interval(secs => p_lease_seconds);
    UPDATE control_plane_maintenance_fences
    SET lease_version=current_fence.lease_version + 1,
        refreshed_at=clock_timestamp(),
        expires_at=lease_expires_at,
        last_stage=p_stage,
        account_evidence=current_evidence
    WHERE control_plane_maintenance_fences.fence_id=p_fence_id;

    INSERT INTO control_plane_maintenance_fence_events (
        fence_id,
        event_type,
        operation,
        actor,
        stage,
        lease_version,
        expires_at,
        account_evidence
    )
    VALUES (
        p_fence_id,
        'stage_verified',
        current_fence.operation,
        current_fence.actor,
        p_stage,
        current_fence.lease_version + 1,
        lease_expires_at,
        current_evidence
    );

    RETURN QUERY
    SELECT p_fence_id,
           current_fence.lease_version + 1,
           lease_expires_at,
           current_evidence;
END;
$$;

CREATE FUNCTION release_control_plane_maintenance_fence(
    p_fence_id uuid,
    p_owner_token text,
    p_actor text,
    p_reason text
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    current_fence control_plane_maintenance_fences%ROWTYPE;
    owner_token_sha256 text;
BEGIN
    IF p_owner_token !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'maintenance owner token is invalid';
    END IF;
    IF p_actor IS NULL OR btrim(p_actor) = '' THEN
        RAISE EXCEPTION 'maintenance release actor is required';
    END IF;
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE EXCEPTION 'maintenance release reason is required';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtext('trader-v3-control-plane-maintenance-fence')
    );
    owner_token_sha256 := encode(
        sha256(convert_to(p_owner_token, 'UTF8')),
        'hex'
    );
    SELECT *
    INTO current_fence
    FROM control_plane_maintenance_fences
    WHERE control_plane_maintenance_fences.fence_id=p_fence_id
    FOR UPDATE;

    IF current_fence.fence_id IS NULL THEN
        RETURN false;
    END IF;
    IF current_fence.owner_token_sha256 <> owner_token_sha256 THEN
        RAISE EXCEPTION 'maintenance fence owner token mismatch';
    END IF;
    IF current_fence.status <> 'active' THEN
        RETURN false;
    END IF;

    UPDATE control_plane_maintenance_fences
    SET status='released',
        released_at=clock_timestamp(),
        release_reason=p_reason,
        last_stage='released'
    WHERE control_plane_maintenance_fences.fence_id=p_fence_id;

    INSERT INTO control_plane_maintenance_fence_events (
        fence_id,
        event_type,
        operation,
        actor,
        stage,
        lease_version,
        expires_at,
        account_evidence,
        details
    )
    VALUES (
        p_fence_id,
        'released',
        current_fence.operation,
        p_actor,
        'released',
        current_fence.lease_version,
        current_fence.expires_at,
        current_fence.account_evidence,
        jsonb_build_object('reason', p_reason)
    );
    RETURN true;
END;
$$;

REVOKE ALL ON control_plane_maintenance_fences
    FROM trader_v3_node_control,
         trader_v3_event_ingest,
         trader_v3_operator_query;
REVOKE ALL ON control_plane_maintenance_fence_events
    FROM trader_v3_node_control,
         trader_v3_event_ingest,
         trader_v3_operator_query;
REVOKE EXECUTE ON FUNCTION
    capture_control_plane_maintenance_evidence(integer)
    FROM PUBLIC,
         trader_v3_node_control,
         trader_v3_event_ingest,
         trader_v3_operator_query;
REVOKE EXECUTE ON FUNCTION
    acquire_control_plane_maintenance_fence(
        uuid, text, text, text, integer, integer
    )
    FROM PUBLIC,
         trader_v3_node_control,
         trader_v3_event_ingest,
         trader_v3_operator_query;
REVOKE EXECUTE ON FUNCTION
    verify_control_plane_maintenance_fence(
        uuid, text, text, integer, integer
    )
    FROM PUBLIC,
         trader_v3_node_control,
         trader_v3_event_ingest,
         trader_v3_operator_query;
REVOKE EXECUTE ON FUNCTION
    release_control_plane_maintenance_fence(uuid, text, text, text)
    FROM PUBLIC,
         trader_v3_node_control,
         trader_v3_event_ingest,
         trader_v3_operator_query;

GRANT SELECT ON control_plane_maintenance_fences
    TO trader_v3_operator_query;

GRANT SELECT ON
    accounts_projection,
    command_node_acks,
    exchange_state_mirror,
    live_canary_permits,
    node_heartbeats,
    operator_commands,
    orders_projection,
    positions_projection,
    production_incidents,
    redis_fencing_epochs,
    reviewed_release_manifests,
    reviewed_release_rollouts,
    risk_state,
    trade_intents
    TO trader_v3_node_control;
GRANT INSERT, UPDATE ON node_heartbeats
    TO trader_v3_node_control;
GRANT INSERT, UPDATE ON production_incidents
    TO trader_v3_node_control;
GRANT UPDATE ON command_node_acks, operator_commands, trade_intents
    TO trader_v3_node_control;
GRANT UPDATE ON live_canary_permits
    TO trader_v3_node_control;
GRANT INSERT ON audit_events
    TO trader_v3_node_control;

GRANT SELECT ON
    node_heartbeats,
    redis_fencing_epochs,
    reviewed_release_manifests,
    reviewed_release_rollouts,
    trade_intents
    TO trader_v3_event_ingest;
GRANT SELECT, INSERT ON execution_events
    TO trader_v3_event_ingest;
GRANT SELECT, INSERT, UPDATE ON
    accounts_projection,
    orders_projection,
    positions_projection,
    protective_orders_projection
    TO trader_v3_event_ingest;

GRANT SELECT ON ALL TABLES IN SCHEMA public
    TO trader_v3_operator_query;
GRANT INSERT, UPDATE, DELETE ON
    audit_events,
    command_node_acks,
    context_snapshots,
    execution_commands,
    execution_jobs,
    hermes_decisions,
    live_canary_permits,
    media_assets,
    message_processing_runs,
    node_command_runs,
    operator_commands,
    order_events,
    order_links,
    order_management_setting_versions,
    order_management_settings,
    outbox_events,
    price_feed_status,
    production_incidents,
    raw_messages,
    reconciliation_findings,
    reconciliation_runs,
    replay_results,
    replay_runs,
    reviewed_release_manifests,
    reviewed_release_rollout_events,
    reviewed_release_rollouts,
    risk_decisions,
    risk_reservations,
    risk_state,
    trade_intents,
    trade_outcome_job_runs,
    trade_outcomes
    TO trader_v3_operator_query;

REVOKE UPDATE, DELETE ON execution_events
    FROM trader_v3_node_control,
         trader_v3_operator_query;
REVOKE INSERT, UPDATE, DELETE ON node_heartbeats
    FROM trader_v3_event_ingest,
         trader_v3_operator_query;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
    TO trader_v3_node_control,
       trader_v3_event_ingest,
       trader_v3_operator_query;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON TABLES
    FROM trader_v3_node_control,
         trader_v3_event_ingest,
         trader_v3_operator_query;
