REVOKE UPDATE (created_at) ON redis_fencing_epochs
    FROM trader_v3_node_control;

REVOKE UPDATE (created_at) ON redis_fencing_epochs
    FROM trader_v3_event_ingest;
REVOKE UPDATE (created_at) ON node_heartbeats
    FROM trader_v3_event_ingest;

REVOKE UPDATE (created_at) ON node_heartbeats
    FROM trader_v3_operator_query;
REVOKE UPDATE (acquired_at) ON control_plane_maintenance_fences
    FROM trader_v3_operator_query;
