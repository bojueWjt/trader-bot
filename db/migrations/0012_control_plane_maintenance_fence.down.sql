DROP TRIGGER IF EXISTS
    trg_control_plane_maintenance_fence_events_append_only
    ON control_plane_maintenance_fence_events;
DROP FUNCTION IF EXISTS
    reject_control_plane_maintenance_fence_event_mutation();
DROP FUNCTION IF EXISTS
    release_control_plane_maintenance_fence(uuid, text, text, text);
DROP FUNCTION IF EXISTS
    verify_control_plane_maintenance_fence(
        uuid, text, text, integer, integer
    );
DROP FUNCTION IF EXISTS
    acquire_control_plane_maintenance_fence(
        uuid, text, text, text, integer, integer
    );
DROP FUNCTION IF EXISTS
    capture_control_plane_maintenance_evidence(integer);
DROP TABLE IF EXISTS control_plane_maintenance_fence_events;
DROP TABLE IF EXISTS control_plane_maintenance_fences;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public
    FROM trader_v3_node_control,
         trader_v3_event_ingest,
         trader_v3_operator_query;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public
    FROM trader_v3_node_control,
         trader_v3_event_ingest,
         trader_v3_operator_query;
REVOKE USAGE ON SCHEMA public
    FROM trader_v3_node_control,
         trader_v3_event_ingest,
         trader_v3_operator_query;

DO $$
BEGIN
    EXECUTE format(
        'REVOKE CONNECT ON DATABASE %I FROM %I, %I, %I',
        current_database(),
        'trader_v3_node_control',
        'trader_v3_event_ingest',
        'trader_v3_operator_query'
    );
END
$$;
