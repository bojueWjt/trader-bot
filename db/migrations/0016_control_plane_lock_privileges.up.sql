GRANT UPDATE (created_at) ON redis_fencing_epochs
    TO trader_v3_node_control;

GRANT UPDATE (created_at) ON redis_fencing_epochs
    TO trader_v3_event_ingest;
GRANT UPDATE (created_at) ON node_heartbeats
    TO trader_v3_event_ingest;

GRANT UPDATE (created_at) ON node_heartbeats
    TO trader_v3_operator_query;
GRANT UPDATE (acquired_at) ON control_plane_maintenance_fences
    TO trader_v3_operator_query;
