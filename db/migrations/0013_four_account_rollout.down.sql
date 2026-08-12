DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM reviewed_release_rollouts
        WHERE phase IN ('account_c_rollout', 'account_d_rollout')
    ) THEN
        RAISE EXCEPTION
            'cannot roll back four-account rollout while C/D rollout is active';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM reviewed_release_rollout_events
        WHERE from_phase IN ('account_c_rollout', 'account_d_rollout')
           OR to_phase IN ('account_c_rollout', 'account_d_rollout')
    ) THEN
        RAISE EXCEPTION
            'cannot roll back four-account rollout while C/D events exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM control_plane_maintenance_fences
        WHERE account_evidence @> '[{"account_id": "account-c"}]'::jsonb
           OR account_evidence @> '[{"account_id": "account-d"}]'::jsonb
    ) THEN
        RAISE EXCEPTION
            'cannot roll back while four-account maintenance evidence exists';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM control_plane_maintenance_fence_events
        WHERE account_evidence @> '[{"account_id": "account-c"}]'::jsonb
           OR account_evidence @> '[{"account_id": "account-d"}]'::jsonb
    ) THEN
        RAISE EXCEPTION
            'cannot roll back while four-account maintenance events exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM live_canary_permits
        WHERE account_id IN ('account-b', 'account-c', 'account-d')
    ) THEN
        RAISE EXCEPTION
            'cannot roll back while B-D live canary permits exist';
    END IF;
END
$$;

ALTER TABLE live_canary_permits
    DROP CONSTRAINT ck_live_canary_permits_account;

ALTER TABLE live_canary_permits
    ADD CONSTRAINT ck_live_canary_permits_account
        CHECK (account_id = 'account-a');

ALTER TABLE reviewed_release_rollouts
    DROP CONSTRAINT ck_reviewed_release_rollouts_phase;

ALTER TABLE reviewed_release_rollouts
    ADD CONSTRAINT ck_reviewed_release_rollouts_phase
        CHECK (
            phase IN (
                'account_a_canary',
                'account_b_rollout',
                'fleet_complete',
                'aborted'
            )
        );

DROP INDEX uq_reviewed_release_rollouts_active;

CREATE UNIQUE INDEX uq_reviewed_release_rollouts_active
    ON reviewed_release_rollouts ((true))
    WHERE phase IN ('account_a_canary', 'account_b_rollout');

ALTER TABLE reviewed_release_rollout_events
    DROP CONSTRAINT ck_reviewed_release_rollout_events_from_phase,
    DROP CONSTRAINT ck_reviewed_release_rollout_events_to_phase,
    DROP CONSTRAINT ck_reviewed_release_rollout_events_shape;

ALTER TABLE reviewed_release_rollout_events
    ADD CONSTRAINT ck_reviewed_release_rollout_events_from_phase
        CHECK (
            from_phase IS NULL
            OR from_phase IN (
                'account_a_canary',
                'account_b_rollout',
                'fleet_complete',
                'aborted'
            )
        ),
    ADD CONSTRAINT ck_reviewed_release_rollout_events_to_phase
        CHECK (
            to_phase IN (
                'account_a_canary',
                'account_b_rollout',
                'fleet_complete',
                'aborted'
            )
        ),
    ADD CONSTRAINT ck_reviewed_release_rollout_events_shape
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
        );

CREATE OR REPLACE FUNCTION validate_reviewed_release_rollout_transition()
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

ALTER TABLE control_plane_maintenance_fences
    DROP CONSTRAINT ck_control_plane_maintenance_fence_evidence;

ALTER TABLE control_plane_maintenance_fences
    ADD CONSTRAINT ck_control_plane_maintenance_fence_evidence
        CHECK (
            jsonb_typeof(account_evidence) = 'array'
            AND jsonb_array_length(account_evidence) = 2
        );

ALTER TABLE control_plane_maintenance_fence_events
    DROP CONSTRAINT ck_control_plane_maintenance_fence_event_evidence;

ALTER TABLE control_plane_maintenance_fence_events
    ADD CONSTRAINT ck_control_plane_maintenance_fence_event_evidence
        CHECK (
            jsonb_typeof(account_evidence) = 'array'
            AND jsonb_array_length(account_evidence) = 2
        );

CREATE OR REPLACE FUNCTION capture_control_plane_maintenance_evidence(
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

CREATE OR REPLACE FUNCTION verify_control_plane_maintenance_fence(
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
