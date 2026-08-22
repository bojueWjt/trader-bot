from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2
import pytest
from psycopg2 import errors


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE = REPO_ROOT / "services" / "control-plane"
FOUR_ACCOUNT_ROLLOUT_UP = (
    REPO_ROOT / "db" / "migrations" / "0013_four_account_rollout.up.sql"
)
FOUR_ACCOUNT_ROLLOUT_DOWN = (
    REPO_ROOT / "db" / "migrations" / "0013_four_account_rollout.down.sql"
)

CORE_TABLES = {
    "raw_messages",
    "media_assets",
    "message_processing_runs",
    "hermes_decisions",
    "risk_decisions",
    "trade_intents",
    "execution_commands",
    "execution_events",
    "orders_projection",
    "positions_projection",
    "accounts_projection",
    "risk_state",
    "node_heartbeats",
    "redis_fencing_epochs",
    "context_snapshots",
    "audit_events",
    "outbox_events",
    "replay_runs",
    "replay_results",
    "reviewed_release_manifests",
    "reviewed_release_rollouts",
    "reviewed_release_rollout_events",
    "live_canary_permits",
    "production_incidents",
}

CONTRACT_ENUMS = {
    "message_type_v1": [
        "new_signal",
        "position_update",
        "close_update",
        "analysis",
        "noise",
        "ambiguous",
    ],
    "hermes_action_v1": [
        "open_position",
        "add_position",
        "partial_close",
        "close_position",
        "move_stop_loss",
        "move_stop_to_entry",
        "replace_take_profits",
        "cancel_order",
        "hold",
        "ignore",
        "needs_review",
    ],
    "approved_trade_action_v1": [
        "open_position",
        "add_position",
        "partial_close",
        "close_position",
        "move_stop_loss",
        "move_stop_to_entry",
        "replace_take_profits",
        "cancel_order",
    ],
    "account_scope_v1": ["unassigned", "single", "all"],
    "position_side_v1": ["long", "short"],
    "entry_type_v1": ["market", "limit", "zone", "none"],
    "reconciliation_state_v1": ["healthy", "degraded", "failed"],
}


def _insert_raw_message(conn, *, raw_id: UUID | None = None, source_message_id: str = "msg-1"):
    raw_id = raw_id or uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_messages (
                id, source, channel_id, source_message_id, source_version,
                source_received_at, content_hash, raw_payload
            )
            VALUES (%s, 'telegram', 'channel-a', %s, 'v1',
                    '2026-06-19T12:00:00Z', %s, '{}'::jsonb)
            RETURNING id
            """,
            (str(raw_id), source_message_id, f"sha-{source_message_id}"),
        )
        return UUID(cur.fetchone()[0])


def _insert_rollout_test_identity(
    cur,
    *,
    redis_fencing_epoch: str,
    release_id: str,
) -> None:
    cur.execute(
        """
        INSERT INTO redis_fencing_epochs (
            redis_fencing_epoch,
            domain,
            status,
            marker_sha256,
            capacity_evidence_sha256,
            initial_redis_run_id,
            active_volume,
            activated_by,
            activated_at
        )
        VALUES (
            %s, 'trader-v3', 'active', %s, %s, %s,
            %s, 'schema-reviewer', now()
        )
        """,
        (
            redis_fencing_epoch,
            "a" * 64,
            "b" * 64,
            "c" * 40,
            f"{release_id}-volume",
        ),
    )
    cur.execute(
        """
        INSERT INTO reviewed_release_rollouts (
            release_id,
            redis_fencing_epoch,
            image_digest,
            config_sha256,
            dependency_lock_sha256,
            schema_epoch,
            manifest_sha256,
            bundle_manifest_sha256,
            registration_idempotency_key,
            reviewed_by
        )
        VALUES (
            %s, %s, 'sha256:test-rollout-image',
            'test-rollout-config', 'test-rollout-lock',
            '0010_live_safety', %s, %s, %s, 'schema-reviewer'
        )
        """,
        (
            release_id,
            redis_fencing_epoch,
            "d" * 64,
            "e" * 64,
            f"{release_id}-registration",
        ),
    )


def _delete_rollout_test_identity(
    cur,
    *,
    redis_fencing_epoch: str,
    release_id: str,
) -> None:
    cur.execute(
        "DELETE FROM reviewed_release_rollout_events WHERE release_id=%s",
        (release_id,),
    )
    cur.execute(
        "DELETE FROM reviewed_release_rollouts WHERE release_id=%s",
        (release_id,),
    )
    cur.execute(
        "DELETE FROM redis_fencing_epochs WHERE redis_fencing_epoch=%s",
        (redis_fencing_epoch,),
    )


def test_fresh_migration_creates_required_tables_constraints_indexes_and_projection_role(
    run_migration, db_url
):
    run_migration("down")
    run_migration("up")

    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        tables = {row[0] for row in cur.fetchall()}
        assert CORE_TABLES.issubset(tables)

        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name='node_heartbeats'
            """
        )
        heartbeat_columns = {row[0] for row in cur.fetchall()}
        assert {
            "release_id",
            "image_digest",
            "config_sha256",
            "dependency_lock_sha256",
            "schema_epoch",
            "positions",
            "regular_orders",
            "algo_orders",
            "positions_snapshot_at",
            "regular_orders_snapshot_at",
            "algo_orders_snapshot_at",
            "reconciliation_completed_at",
            "redis_fencing_epoch",
            "runtime_generation",
            "lease_fencing_token",
            "heartbeat_sequence",
        }.issubset(heartbeat_columns)

        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name='live_canary_permits'
            """
        )
        permit_columns = {row[0] for row in cur.fetchall()}
        assert {
            "portfolio_baseline_sha256",
            "max_cumulative_loss_usdt",
            "testnet_emergency_close_evidence_sha256",
            "testnet_emergency_close_verified_at",
        }.issubset(permit_columns)

        cur.execute(
            """
            SELECT is_nullable, data_type, column_default
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name='risk_state'
              AND column_name='version'
            """
        )
        assert cur.fetchone() == ("NO", "bigint", "1")

        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name='reviewed_release_rollouts'
            """
        )
        rollout_columns = {row[0] for row in cur.fetchall()}
        assert {
            "release_id",
            "redis_fencing_epoch",
            "manifest_sha256",
            "bundle_manifest_sha256",
            "registration_idempotency_key",
            "phase",
            "phase_version",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        }.issubset(rollout_columns)

        cur.execute(
            """
            SELECT is_nullable, data_type, column_default
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name='command_node_acks'
              AND column_name='result'
            """
        )
        assert cur.fetchone() == ("NO", "jsonb", "'{}'::jsonb")

        cur.execute(
            """
            SELECT t.typname, e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE t.typname = ANY(%s)
            ORDER BY t.typname, e.enumsortorder
            """,
            (list(CONTRACT_ENUMS.keys()),),
        )
        enum_values: dict[str, list[str]] = {}
        for enum_name, enum_value in cur.fetchall():
            enum_values.setdefault(enum_name, []).append(enum_value)
        assert enum_values == CONTRACT_ENUMS

        cur.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conname = ANY(%s)
            """,
            (
                [
                    "uq_raw_messages_source_identity",
                    "ck_trade_intents_approved_chain",
                    "uq_trade_intents_idempotency_key",
                    "uq_execution_events_event_id",
                    "uq_hermes_decisions_raw_message_id",
                    "ck_command_node_acks_result_object",
                    "ck_live_canary_permits_cumulative_loss",
                    "ck_live_canary_permits_emergency_close_evidence",
                    "ck_live_canary_permits_emergency_close_verified_at",
                    "ck_reviewed_release_rollouts_phase",
                    "ck_reviewed_release_rollout_events_shape",
                    "ck_redis_fencing_epochs_lifecycle",
                    "ck_control_plane_maintenance_fence_evidence",
                    "ck_control_plane_maintenance_fence_event_evidence",
                ],
            ),
        )
        constraints = {row[0] for row in cur.fetchall()}
        assert constraints == {
            "uq_raw_messages_source_identity",
            "ck_trade_intents_approved_chain",
            "uq_trade_intents_idempotency_key",
            "uq_execution_events_event_id",
            "uq_hermes_decisions_raw_message_id",
            "ck_command_node_acks_result_object",
            "ck_live_canary_permits_cumulative_loss",
            "ck_live_canary_permits_emergency_close_evidence",
            "ck_live_canary_permits_emergency_close_verified_at",
            "ck_reviewed_release_rollouts_phase",
            "ck_reviewed_release_rollout_events_shape",
            "ck_redis_fencing_epochs_lifecycle",
            "ck_control_plane_maintenance_fence_evidence",
            "ck_control_plane_maintenance_fence_event_evidence",
        }

        cur.execute(
            """
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conname IN (
                'ck_control_plane_maintenance_fence_evidence',
                'ck_control_plane_maintenance_fence_event_evidence'
            )
            """
        )
        maintenance_evidence_constraints = dict(cur.fetchall())
        assert set(maintenance_evidence_constraints) == {
            "ck_control_plane_maintenance_fence_evidence",
            "ck_control_plane_maintenance_fence_event_evidence",
        }
        for definition in maintenance_evidence_constraints.values():
            assert "jsonb_array_length(account_evidence) = 4" in definition
            for account_id in (
                "account-a",
                "account-b",
                "account-c",
                "account-d",
            ):
                assert account_id in definition

        cur.execute(
            """
            SELECT pg_get_functiondef(
                'capture_control_plane_maintenance_evidence(integer)'
                ::regprocedure
            )
            """
        )
        capture_function = cur.fetchone()[0]
        assert "account-c" in capture_function
        assert "account-d" in capture_function
        assert "nautilus-node-" in capture_function

        cur.execute(
            """
            SELECT pg_get_functiondef(
                'verify_control_plane_maintenance_fence('
                'uuid,text,text,integer,integer'
                ')'::regprocedure
            )
            """
        )
        assert "maintenance fence A-D" in cur.fetchone()[0]

        cur.execute(
            """
            SELECT pg_get_expr(indexprs, indrelid),
                   pg_get_expr(indpred, indrelid)
            FROM pg_index
            WHERE indexrelid = (
                'uq_reviewed_release_rollouts_active'::regclass
            )
            """
        )
        active_index_expression, active_index_predicate = cur.fetchone()
        assert active_index_expression == "true"
        for phase in (
            "account_a_canary",
            "account_b_rollout",
            "account_c_rollout",
            "account_d_rollout",
        ):
            assert phase in active_index_predicate

        redis_fencing_epoch = "11111111-1111-4111-8111-111111111111"
        cur.execute(
            """
            INSERT INTO redis_fencing_epochs (
                redis_fencing_epoch,
                domain,
                status,
                marker_sha256,
                capacity_evidence_sha256,
                initial_redis_run_id,
                active_volume,
                activated_by,
                activated_at
            )
            VALUES (
                %s, 'trader-v3', 'active', %s, %s, %s,
                'schema-redis-volume', 'schema-reviewer', now()
            )
            """,
            (
                redis_fencing_epoch,
                "7" * 64,
                "8" * 64,
                "a" * 40,
            ),
        )

        cur.execute(
            """
            INSERT INTO reviewed_release_rollouts (
                release_id,
                redis_fencing_epoch,
                image_digest,
                config_sha256,
                dependency_lock_sha256,
                schema_epoch,
                manifest_sha256,
                bundle_manifest_sha256,
                registration_idempotency_key,
                reviewed_by
            )
            VALUES (
                'schema-rollout-release',
                %s,
                'sha256:schema-rollout-image',
                'schema-rollout-config',
                'schema-rollout-lock',
                'schema-rollout-epoch',
                %s,
                %s,
                'schema-rollout-registration',
                'schema-reviewer'
            )
            """,
            (redis_fencing_epoch, "5" * 64, "6" * 64),
        )
        cur.execute(
            """
            UPDATE reviewed_release_rollouts
            SET phase='account_b_rollout',
                phase_version=2
            WHERE release_id='schema-rollout-release'
            """
        )
        cur.execute("SAVEPOINT invalid_b_to_fleet_rollout")
        with pytest.raises(errors.RaiseException):
            cur.execute(
                """
                UPDATE reviewed_release_rollouts
                SET phase='fleet_complete',
                    phase_version=3
                WHERE release_id='schema-rollout-release'
                """
            )
        cur.execute("ROLLBACK TO SAVEPOINT invalid_b_to_fleet_rollout")

        cur.execute(
            """
            INSERT INTO reviewed_release_rollout_events (
                rollout_event_id,
                release_id,
                event_type,
                from_phase,
                to_phase,
                phase_version,
                idempotency_key,
                actor,
                reason
            )
            VALUES (
                %s,
                'schema-rollout-release',
                'registered',
                NULL,
                'account_a_canary',
                1,
                'schema-rollout-event-1',
                'schema-reviewer',
                'schema rollout registered'
            )
            """,
            (str(uuid4()),),
        )
        cur.execute("SAVEPOINT invalid_b_to_fleet_event")
        with pytest.raises(errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO reviewed_release_rollout_events (
                    rollout_event_id,
                    release_id,
                    event_type,
                    from_phase,
                    to_phase,
                    phase_version,
                    idempotency_key,
                    actor,
                    reason
                )
                VALUES (
                    %s,
                    'schema-rollout-release',
                    'phase_transition',
                    'account_b_rollout',
                    'fleet_complete',
                    3,
                    'schema-invalid-rollout-event',
                    'schema-reviewer',
                    'invalid skipped rollout phases'
                )
                """,
                (str(uuid4()),),
            )
        cur.execute("ROLLBACK TO SAVEPOINT invalid_b_to_fleet_event")

        cur.execute(
            """
            UPDATE reviewed_release_rollouts
            SET phase='account_c_rollout',
                phase_version=3
            WHERE release_id='schema-rollout-release'
            """
        )
        cur.execute(
            """
            UPDATE reviewed_release_rollouts
            SET phase='account_d_rollout',
                phase_version=4
            WHERE release_id='schema-rollout-release'
            """
        )
        cur.execute(
            """
            UPDATE reviewed_release_rollouts
            SET phase='fleet_complete',
                phase_version=5
            WHERE release_id='schema-rollout-release'
            """
        )
        cur.executemany(
            """
            INSERT INTO reviewed_release_rollout_events (
                rollout_event_id,
                release_id,
                event_type,
                from_phase,
                to_phase,
                phase_version,
                idempotency_key,
                actor,
                reason
            )
            VALUES (
                %s,
                'schema-rollout-release',
                'phase_transition',
                %s,
                %s,
                %s,
                %s,
                'schema-reviewer',
                'schema rollout transition'
            )
            """,
            [
                (
                    str(uuid4()),
                    "account_a_canary",
                    "account_b_rollout",
                    2,
                    "schema-rollout-event-2",
                ),
                (
                    str(uuid4()),
                    "account_b_rollout",
                    "account_c_rollout",
                    3,
                    "schema-rollout-event-3",
                ),
                (
                    str(uuid4()),
                    "account_c_rollout",
                    "account_d_rollout",
                    4,
                    "schema-rollout-event-4",
                ),
                (
                    str(uuid4()),
                    "account_d_rollout",
                    "fleet_complete",
                    5,
                    "schema-rollout-event-5",
                ),
            ],
        )
        cur.execute("SAVEPOINT terminal_rollout_transition")
        with pytest.raises(errors.RaiseException):
            cur.execute(
                """
                UPDATE reviewed_release_rollouts
                SET phase='aborted',
                    phase_version=6
                WHERE release_id='schema-rollout-release'
                """
            )
        cur.execute("ROLLBACK TO SAVEPOINT terminal_rollout_transition")
        cur.execute(
            """
            DELETE FROM reviewed_release_rollout_events
            WHERE release_id='schema-rollout-release'
            """
        )
        cur.execute(
            """
            DELETE FROM reviewed_release_rollouts
            WHERE release_id='schema-rollout-release'
            """
        )

        command_id = uuid4()
        cur.execute(
            """
            INSERT INTO operator_commands (
                command_id,
                command_type,
                scope,
                requested_by,
                reason,
                idempotency_key
            )
            VALUES (%s, 'CLOSE_ALL', '{}'::jsonb, 'test', 'test', %s)
            """,
            (str(command_id), str(uuid4())),
        )
        cur.execute(
            """
            INSERT INTO command_node_acks (command_id, node_id)
            VALUES (%s, 'schema-test-node')
            RETURNING result
            """,
            (str(command_id),),
        )
        assert cur.fetchone()[0] == {}
        cur.execute("SAVEPOINT command_ack_result_object")
        with pytest.raises(errors.CheckViolation):
            cur.execute(
                """
                UPDATE command_node_acks
                SET result='[]'::jsonb
                WHERE command_id=%s
                  AND node_id='schema-test-node'
                """,
                (str(command_id),),
            )
        cur.execute("ROLLBACK TO SAVEPOINT command_ack_result_object")

        cur.execute(
            """
            INSERT INTO reviewed_release_manifests (
                account_id,
                release_id,
                image_digest,
                config_sha256,
                dependency_lock_sha256,
                schema_epoch,
                review_status,
                reviewed_by
            )
            VALUES (
                'account-a',
                'schema-canary-release',
                'sha256:schema-image',
                'schema-config',
                'schema-lock',
                'schema-epoch',
                'reviewed',
                'schema-reviewer'
            )
            """
        )
        permit_id = uuid4()
        cur.execute(
            """
            INSERT INTO live_canary_permits (
                permit_id,
                account_id,
                symbol,
                max_notional_usdt,
                max_cumulative_loss_usdt,
                expires_at,
                release_id,
                testnet_emergency_close_evidence_sha256,
                testnet_emergency_close_verified_at,
                issued_by
            )
            VALUES (
                %s,
                'account-a',
                'SOLUSDT',
                12,
                1.49,
                now() + interval '10 minutes',
                'schema-canary-release',
                %s,
                now(),
                'schema-reviewer'
            )
            """,
            (str(permit_id), "4" * 64),
        )
        cur.execute("SAVEPOINT canary_cumulative_loss_limit")
        with pytest.raises(errors.CheckViolation):
            cur.execute(
                """
                UPDATE live_canary_permits
                SET max_cumulative_loss_usdt=1.5
                WHERE permit_id=%s
                """,
                (str(permit_id),),
            )
        cur.execute("ROLLBACK TO SAVEPOINT canary_cumulative_loss_limit")

        cur.execute("SAVEPOINT canary_emergency_close_hash")
        with pytest.raises(errors.CheckViolation):
            cur.execute(
                """
                UPDATE live_canary_permits
                SET testnet_emergency_close_evidence_sha256='invalid'
                WHERE permit_id=%s
                """,
                (str(permit_id),),
            )
        cur.execute("ROLLBACK TO SAVEPOINT canary_emergency_close_hash")

        cur.execute("SAVEPOINT canary_emergency_close_verified_at")
        with pytest.raises(errors.CheckViolation):
            cur.execute(
                """
                UPDATE live_canary_permits
                SET testnet_emergency_close_verified_at=issued_at + interval '2 minutes'
                WHERE permit_id=%s
                """,
                (str(permit_id),),
            )
        cur.execute(
            "ROLLBACK TO SAVEPOINT canary_emergency_close_verified_at"
        )

        cur.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = ANY(%s)
            """,
            (
                [
                    "idx_raw_messages_channel_received",
                    "idx_outbox_events_status",
                    "idx_orders_projection_account_id",
                    "idx_positions_projection_account_id",
                    "idx_accounts_projection_account_id",
                ],
            ),
        )
        indexes = {row[0] for row in cur.fetchall()}
        assert indexes == {
            "idx_raw_messages_channel_received",
            "idx_outbox_events_status",
            "idx_orders_projection_account_id",
            "idx_positions_projection_account_id",
            "idx_accounts_projection_account_id",
        }

        cur.execute(
            """
            SELECT tgname
            FROM pg_trigger
            WHERE tgname = 'trg_raw_messages_source_received_at_insert_only'
            """
        )
        assert cur.fetchone()[0] == "trg_raw_messages_source_received_at_insert_only"

        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'nautilus_projection_writer'")
        assert cur.fetchone() == (1,)
        cur.execute(
            """
            SELECT
                has_table_privilege('nautilus_projection_writer', 'orders_projection', 'INSERT'),
                has_table_privilege('nautilus_projection_writer', 'orders_projection', 'UPDATE'),
                has_table_privilege('nautilus_projection_writer', 'orders_projection', 'SELECT'),
                has_table_privilege('nautilus_projection_writer', 'raw_messages', 'INSERT')
            """
        )
        assert cur.fetchone() == (True, True, False, False)


def test_control_plane_lock_privileges_are_migrated_and_isolated(
    migrated_db,
) -> None:
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                has_column_privilege(
                    'trader_v3_node_control',
                    'redis_fencing_epochs',
                    'created_at',
                    'UPDATE'
                ),
                has_column_privilege(
                    'trader_v3_event_ingest',
                    'redis_fencing_epochs',
                    'created_at',
                    'UPDATE'
                ),
                has_column_privilege(
                    'trader_v3_event_ingest',
                    'node_heartbeats',
                    'created_at',
                    'UPDATE'
                ),
                has_column_privilege(
                    'trader_v3_operator_query',
                    'node_heartbeats',
                    'created_at',
                    'UPDATE'
                ),
                has_column_privilege(
                    'trader_v3_operator_query',
                    'control_plane_maintenance_fences',
                    'acquired_at',
                    'UPDATE'
                )
            """
        )
        assert cur.fetchone() == (True, True, True, True, True)

        cur.execute(
            """
            SELECT
                has_column_privilege(
                    'trader_v3_event_ingest',
                    'node_heartbeats',
                    'status',
                    'UPDATE'
                ),
                has_column_privilege(
                    'trader_v3_operator_query',
                    'node_heartbeats',
                    'status',
                    'UPDATE'
                ),
                has_table_privilege(
                    'trader_v3_operator_query',
                    'orders_projection',
                    'UPDATE'
                ),
                has_table_privilege(
                    'trader_v3_operator_query',
                    'accounts_projection',
                    'UPDATE'
                ),
                has_table_privilege(
                    'trader_v3_operator_query',
                    'execution_events',
                    'UPDATE'
                )
            """
        )
        assert cur.fetchone() == (False, False, False, False, False)

        cur.execute(
            """
            SELECT
                has_table_privilege(
                    'trader_v3_event_ingest',
                    'orders_projection',
                    'UPDATE'
                ),
                has_table_privilege(
                    'trader_v3_event_ingest',
                    'accounts_projection',
                    'UPDATE'
                ),
                has_table_privilege(
                    'trader_v3_event_ingest',
                    'execution_events',
                    'INSERT'
                )
            """
        )
        assert cur.fetchone() == (True, True, True)


def test_transaction_rollback_removes_raw_message(db_conn, db_url):
    raw_id = uuid4()
    _insert_raw_message(db_conn, raw_id=raw_id, source_message_id="rollback")
    db_conn.rollback()

    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_messages WHERE id = %s", (str(raw_id),))
        assert cur.fetchone() == (0,)


def test_unique_raw_message_key_rejects_concurrent_duplicate(db_conn, db_url):
    _insert_raw_message(db_conn, source_message_id="duplicate")
    results: queue.Queue[str] = queue.Queue()

    def insert_duplicate():
        try:
            with psycopg2.connect(db_url) as conn2, conn2.cursor() as cur2:
                cur2.execute("SET lock_timeout = '5s'")
                _insert_raw_message(conn2, source_message_id="duplicate")
                conn2.commit()
                results.put("inserted")
        except errors.UniqueViolation:
            results.put("unique_violation")

    thread = threading.Thread(target=insert_duplicate)
    thread.start()
    time.sleep(0.2)
    db_conn.commit()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert results.get_nowait() == "unique_violation"


def test_repository_commits_raw_message_and_outbox_in_same_transaction(migrated_db):
    sys.path.insert(0, str(CONTROL_PLANE))
    from db.connection import connect, transaction
    from db.repository import ingest_raw_message_with_outbox

    raw_id = uuid4()
    with connect(migrated_db) as conn:
        with transaction(conn):
            inserted_raw_id, outbox_event_id = ingest_raw_message_with_outbox(
                conn,
                {
                    "id": raw_id,
                    "source": "telegram",
                    "channel_id": "channel-a",
                    "source_message_id": "repo-commit",
                    "source_version": "v1",
                    "source_received_at": "2026-06-19T12:00:00Z",
                    "content_hash": "sha-repo-commit",
                    "message_text": "BTC long",
                    "raw_payload": {"message": "BTC long"},
                },
                {
                    "event_type": "raw_message.ingested",
                    "payload": {"raw_message_id": str(raw_id)},
                    "trace_id": raw_id,
                },
            )

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_messages WHERE id = %s", (str(raw_id),))
        assert cur.fetchone() == (1,)
        cur.execute(
            """
            SELECT aggregate_type, aggregate_id, event_type, status
            FROM outbox_events
            WHERE outbox_event_id = %s
            """,
            (str(outbox_event_id),),
        )
        assert cur.fetchone() == (
            "raw_message",
            str(inserted_raw_id),
            "raw_message.ingested",
            "pending",
        )


def test_four_account_rollout_forward_migration_preserves_legacy_events(
    migrated_db,
):
    redis_fencing_epoch = "22222222-2222-4222-8222-222222222222"
    release_id = "legacy-two-account-rollout"
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            FOUR_ACCOUNT_ROLLOUT_DOWN.read_text(encoding="utf-8")
        )
        _insert_rollout_test_identity(
            cur,
            redis_fencing_epoch=redis_fencing_epoch,
            release_id=release_id,
        )
        cur.execute(
            """
            UPDATE reviewed_release_rollouts
            SET phase='account_b_rollout', phase_version=2
            WHERE release_id=%s
            """,
            (release_id,),
        )
        cur.execute(
            """
            UPDATE reviewed_release_rollouts
            SET phase='fleet_complete', phase_version=3
            WHERE release_id=%s
            """,
            (release_id,),
        )
        cur.execute(
            """
            INSERT INTO reviewed_release_rollout_events (
                rollout_event_id,
                release_id,
                event_type,
                from_phase,
                to_phase,
                phase_version,
                idempotency_key,
                actor,
                reason
            )
            VALUES (
                %s, %s, 'phase_transition', 'account_b_rollout',
                'fleet_complete', 3, %s, 'schema-reviewer',
                'legacy two-account completion'
            )
            """,
            (
                str(uuid4()),
                release_id,
                "legacy-rollout-event",
            ),
        )

        cur.execute(FOUR_ACCOUNT_ROLLOUT_UP.read_text(encoding="utf-8"))
        cur.execute(
            """
            SELECT convalidated
            FROM pg_constraint
            WHERE conname='ck_reviewed_release_rollout_events_shape'
            """
        )
        assert cur.fetchone() == (False,)

        cur.execute("SAVEPOINT reject_new_legacy_transition_event")
        with pytest.raises(errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO reviewed_release_rollout_events (
                    rollout_event_id,
                    release_id,
                    event_type,
                    from_phase,
                    to_phase,
                    phase_version,
                    idempotency_key,
                    actor,
                    reason
                )
                VALUES (
                    %s, %s, 'phase_transition', 'account_b_rollout',
                    'fleet_complete', 4, %s, 'schema-reviewer',
                    'new skipped rollout phases'
                )
                """,
                (
                    str(uuid4()),
                    release_id,
                    "new-invalid-legacy-rollout-event",
                ),
            )
        cur.execute(
            "ROLLBACK TO SAVEPOINT reject_new_legacy_transition_event"
        )
        _delete_rollout_test_identity(
            cur,
            redis_fencing_epoch=redis_fencing_epoch,
            release_id=release_id,
        )


def test_four_account_rollout_down_is_fail_closed(migrated_db):
    redis_fencing_epoch = "33333333-3333-4333-8333-333333333333"
    release_id = "four-account-down-guard"
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _insert_rollout_test_identity(
            cur,
            redis_fencing_epoch=redis_fencing_epoch,
            release_id=release_id,
        )
        cur.execute(
            """
            UPDATE reviewed_release_rollouts
            SET phase='account_b_rollout', phase_version=2
            WHERE release_id=%s
            """,
            (release_id,),
        )
        cur.execute(
            """
            UPDATE reviewed_release_rollouts
            SET phase='account_c_rollout', phase_version=3
            WHERE release_id=%s
            """,
            (release_id,),
        )

        cur.execute("SAVEPOINT active_cd_rollout_guard")
        with pytest.raises(
            errors.RaiseException,
            match="C/D rollout is active",
        ):
            cur.execute(
                FOUR_ACCOUNT_ROLLOUT_DOWN.read_text(encoding="utf-8")
            )
        cur.execute("ROLLBACK TO SAVEPOINT active_cd_rollout_guard")

        cur.execute(
            """
            UPDATE reviewed_release_rollouts
            SET phase='account_d_rollout', phase_version=4
            WHERE release_id=%s
            """,
            (release_id,),
        )
        cur.execute(
            """
            UPDATE reviewed_release_rollouts
            SET phase='fleet_complete', phase_version=5
            WHERE release_id=%s
            """,
            (release_id,),
        )
        cur.execute(
            """
            INSERT INTO reviewed_release_rollout_events (
                rollout_event_id,
                release_id,
                event_type,
                from_phase,
                to_phase,
                phase_version,
                idempotency_key,
                actor,
                reason
            )
            VALUES (
                %s, %s, 'phase_transition', 'account_b_rollout',
                'account_c_rollout', 3, %s, 'schema-reviewer',
                'four-account rollout evidence'
            )
            """,
            (
                str(uuid4()),
                release_id,
                "down-guard-rollout-event",
            ),
        )

        cur.execute("SAVEPOINT cd_rollout_event_guard")
        with pytest.raises(
            errors.RaiseException,
            match="C/D events exist",
        ):
            cur.execute(
                FOUR_ACCOUNT_ROLLOUT_DOWN.read_text(encoding="utf-8")
            )
        cur.execute("ROLLBACK TO SAVEPOINT cd_rollout_event_guard")
        _delete_rollout_test_identity(
            cur,
            redis_fencing_epoch=redis_fencing_epoch,
            release_id=release_id,
        )


def test_down_migration_removes_schema_objects(run_migration, db_url):
    run_migration("down")
    run_migration("up")
    run_migration("down")

    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        assert cur.fetchall() == []
        cur.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'nautilus_projection_writer'"
        )
        assert cur.fetchone() is None
