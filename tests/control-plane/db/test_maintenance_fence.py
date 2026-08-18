from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extras import Json


REPO_ROOT = Path(__file__).resolve().parents[3]
FOUR_ACCOUNT_ROLLOUT_UP = (
    REPO_ROOT / "db" / "migrations" / "0013_four_account_rollout.up.sql"
)
FOUR_ACCOUNT_ROLLOUT_DOWN = (
    REPO_ROOT / "db" / "migrations" / "0013_four_account_rollout.down.sql"
)
MIGRATE = REPO_ROOT / "services" / "control-plane" / "db" / "migrate.py"
ACCOUNTS = ("account-a", "account-b", "account-c", "account-d")
REDIS_FENCING_EPOCH = "11111111-1111-4111-8111-111111111111"
REPLACEMENT_REDIS_FENCING_EPOCH = "22222222-2222-4222-8222-222222222222"
OWNER_TOKEN = "a" * 64
RELEASE_ID = "release-maintenance-test"
IMAGE_DIGEST = "sha256:" + ("1" * 64)
CONFIG_SHA256 = "2" * 64
DEPENDENCY_LOCK_SHA256 = "3" * 64
SCHEMA_EPOCH = "0015_refresh_evidence_command"


def _load_migrate_module() -> ModuleType:
    module_name = "trader_control_plane_migrate"
    spec = importlib.util.spec_from_file_location(module_name, MIGRATE)
    if spec is None or spec.loader is None:
        raise RuntimeError("control-plane migration module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


MIGRATE_MODULE = _load_migrate_module()


def _canonical_evidence_sha256(evidence: object) -> str:
    encoded = json.dumps(
        evidence,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _seed_fleet(
    database_url: str,
    *,
    account_a_state: str = "HALTED",
    account_b_age_seconds: int = 0,
) -> None:
    now = datetime.now(timezone.utc)
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
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
                'maintenance-test-volume', 'test', now()
            )
            """,
            (
                REDIS_FENCING_EPOCH,
                "4" * 64,
                "5" * 64,
                "6" * 40,
            ),
        )
        for index, account_id in enumerate(ACCOUNTS, start=1):
            state = "HALTED"
            age_seconds = 0
            if account_id == "account-a":
                state = account_a_state
            if account_id == "account-b":
                age_seconds = account_b_age_seconds
            heartbeat_at = now - timedelta(seconds=age_seconds)
            cur.execute(
                """
                INSERT INTO node_heartbeats (
                    node_id,
                    account_id,
                    status,
                    version,
                    payload,
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
                )
                VALUES (
                    %s, %s, %s, 'maintenance-test', %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    f"nautilus-node-{account_id}",
                    account_id,
                    state,
                    Json({"readiness": True}),
                    RELEASE_ID,
                    IMAGE_DIGEST,
                    CONFIG_SHA256,
                    DEPENDENCY_LOCK_SHA256,
                    SCHEMA_EPOCH,
                    REDIS_FENCING_EPOCH,
                    f"runtime-{account_id[-1]}",
                    40 + index,
                    index,
                    heartbeat_at,
                ),
            )


def _acquire(
    cur,
    *,
    fence_id: str | None = None,
    owner_token: str = OWNER_TOKEN,
    operation: str = "test-maintenance",
    lease_seconds: int = 30,
    heartbeat_max_age_seconds: int = 10,
) -> tuple:
    fence_id = fence_id or str(uuid4())
    cur.execute(
        """
        SELECT fence_id,
               lease_version,
               expires_at,
               account_evidence
        FROM acquire_control_plane_maintenance_fence(
            %s, %s, %s, %s, %s, %s
        )
        """,
        (
            fence_id,
            operation,
            "pytest",
            owner_token,
            lease_seconds,
            heartbeat_max_age_seconds,
        ),
    )
    return cur.fetchone()


def _acquire_frozen_evidence(database_url: str) -> tuple[str, object, str]:
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        fence_id, _lease_version, _expires_at, evidence = _acquire(
            cur,
            lease_seconds=30,
        )
    return fence_id, evidence, _canonical_evidence_sha256(evidence)


def _verify_frozen(
    database_url: str,
    *,
    fence_id: str,
    evidence_sha256: str,
    owner_token: str = OWNER_TOKEN,
) -> dict[str, object]:
    conn = psycopg2.connect(database_url)
    try:
        return MIGRATE_MODULE.verify_frozen_maintenance_fence(
            conn,
            fence_id=MIGRATE_MODULE.UUID(fence_id),
            owner_token=owner_token,
            stage="post-stop-test",
            lease_seconds=30,
            account_evidence_sha256=evidence_sha256,
        )
    finally:
        conn.close()


def _replace_active_redis_fencing_epoch(cur) -> None:
    cur.execute(
        """
        UPDATE redis_fencing_epochs
        SET status='retired',
            retired_at=now()
        WHERE domain='trader-v3'
          AND status='active'
        """
    )
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
            'replacement-maintenance-test-volume', 'pytest', now()
        )
        """,
        (
            REPLACEMENT_REDIS_FENCING_EPOCH,
            "7" * 64,
            "8" * 64,
            "9" * 40,
        ),
    )


@pytest.fixture()
def frozen_fence_db(migrated_db: str):
    try:
        yield migrated_db
    finally:
        with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
            cur.execute(
                """
                ALTER TABLE control_plane_maintenance_fence_events
                DISABLE TRIGGER
                    trg_control_plane_maintenance_fence_events_append_only
                """
            )
            cur.execute(
                "DELETE FROM control_plane_maintenance_fence_events"
            )
            cur.execute("DELETE FROM control_plane_maintenance_fences")
            cur.execute(
                """
                ALTER TABLE control_plane_maintenance_fence_events
                ENABLE TRIGGER
                    trg_control_plane_maintenance_fence_events_append_only
                """
            )


def test_fence_acquisition_captures_exact_halted_fleet_identity(
    migrated_db: str,
) -> None:
    _seed_fleet(migrated_db)

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        fence_id, lease_version, _expires_at, evidence = _acquire(cur)

        assert lease_version == 1
        assert {item["account_id"] for item in evidence} == set(ACCOUNTS)
        assert {item["status"] for item in evidence} == {"HALTED"}
        assert {
            item["redis_fencing_epoch"] for item in evidence
        } == {REDIS_FENCING_EPOCH}
        assert {item["release_id"] for item in evidence} == {RELEASE_ID}
        cur.execute(
            """
            SELECT event_type
            FROM control_plane_maintenance_fence_events
            WHERE fence_id=%s
            ORDER BY maintenance_event_id
            """,
            (fence_id,),
        )
        assert cur.fetchall() == [("acquired",)]
        conn.rollback()


@pytest.mark.parametrize(
    ("account_a_state", "account_b_age_seconds", "message"),
    (
        ("ACTIVE", 0, "must be HALTED"),
        ("HALTED", 120, "account-a through account-d"),
    ),
)
def test_fence_acquisition_rejects_non_quiesced_fleet(
    migrated_db: str,
    account_a_state: str,
    account_b_age_seconds: int,
    message: str,
) -> None:
    _seed_fleet(
        migrated_db,
        account_a_state=account_a_state,
        account_b_age_seconds=account_b_age_seconds,
    )

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg2.Error, match=message):
            _acquire(cur, heartbeat_max_age_seconds=10)
        conn.rollback()


def test_fence_acquisition_requires_all_four_accounts(
    migrated_db: str,
) -> None:
    _seed_fleet(migrated_db)

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM node_heartbeats WHERE account_id='account-c'"
        )
        with pytest.raises(
            psycopg2.Error,
            match="account-a through account-d",
        ):
            _acquire(cur)
        conn.rollback()


def test_fence_acquisition_rejects_account_node_identity_drift(
    migrated_db: str,
) -> None:
    _seed_fleet(migrated_db)

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE node_heartbeats
            SET node_id='nautilus-node-account-x'
            WHERE account_id='account-c'
            """
        )
        with pytest.raises(
            psycopg2.Error,
            match=(
                "account-c heartbeat node_id must equal "
                "nautilus-node-account-c"
            ),
        ):
            _acquire(cur)
        conn.rollback()


def test_fence_evidence_constraints_require_exact_account_set(
    migrated_db: str,
) -> None:
    _seed_fleet(migrated_db)

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        fence_id, _lease_version, expires_at, _evidence = _acquire(cur)
        invalid_evidence = Json(
            [
                {"account_id": "account-a"},
                {"account_id": "account-b"},
                {"account_id": "account-c"},
                {"account_id": "account-c"},
            ]
        )

        cur.execute("SAVEPOINT invalid_fence_evidence")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
                UPDATE control_plane_maintenance_fences
                SET account_evidence=%s
                WHERE fence_id=%s
                """,
                (invalid_evidence, fence_id),
            )
        cur.execute("ROLLBACK TO SAVEPOINT invalid_fence_evidence")

        cur.execute("SAVEPOINT invalid_fence_event_evidence")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
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
                    %s,
                    'stage_verified',
                    'test-maintenance',
                    'pytest',
                    'invalid-evidence',
                    2,
                    %s,
                    %s
                )
                """,
                (fence_id, expires_at, invalid_evidence),
            )
        cur.execute("ROLLBACK TO SAVEPOINT invalid_fence_event_evidence")
        conn.rollback()


def test_four_account_evidence_blocks_down_migration(
    migrated_db: str,
) -> None:
    _seed_fleet(migrated_db)

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _acquire(cur)
        cur.execute("SAVEPOINT four_account_down_guard")
        with pytest.raises(
            psycopg2.errors.RaiseException,
            match="four-account maintenance evidence",
        ):
            cur.execute(
                FOUR_ACCOUNT_ROLLOUT_DOWN.read_text(encoding="utf-8")
            )
        cur.execute("ROLLBACK TO SAVEPOINT four_account_down_guard")
        conn.rollback()


def test_forward_migration_preserves_released_two_account_evidence(
    migrated_db: str,
) -> None:
    _seed_fleet(migrated_db)

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            FOUR_ACCOUNT_ROLLOUT_DOWN.read_text(encoding="utf-8")
        )
        fence_id, _lease_version, _expires_at, evidence = _acquire(cur)
        assert {item["account_id"] for item in evidence} == {
            "account-a",
            "account-b",
        }
        cur.execute(
            """
            SELECT release_control_plane_maintenance_fence(
                %s, %s, 'pytest', 'legacy maintenance complete'
            )
            """,
            (fence_id, OWNER_TOKEN),
        )
        assert cur.fetchone() == (True,)

        cur.execute(FOUR_ACCOUNT_ROLLOUT_UP.read_text(encoding="utf-8"))
        cur.execute(
            """
            SELECT jsonb_array_length(account_evidence)
            FROM control_plane_maintenance_fences
            WHERE fence_id=%s
            """,
            (fence_id,),
        )
        assert cur.fetchone() == (2,)
        cur.execute(
            """
            SELECT array_agg(
                jsonb_array_length(account_evidence)
                ORDER BY maintenance_event_id
            )
            FROM control_plane_maintenance_fence_events
            WHERE fence_id=%s
            """,
            (fence_id,),
        )
        assert cur.fetchone() == ([2, 2],)
        conn.rollback()


def test_frozen_stage_verification_accepts_stale_heartbeats(
    frozen_fence_db: str,
) -> None:
    migrated_db = frozen_fence_db
    _seed_fleet(migrated_db)
    fence_id, evidence, evidence_sha256 = _acquire_frozen_evidence(
        migrated_db
    )
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE node_heartbeats
            SET last_seen_at=now() - interval '5 minutes'
            """
        )

    receipt = _verify_frozen(
        migrated_db,
        fence_id=fence_id,
        evidence_sha256=evidence_sha256,
    )

    assert receipt["lease_version"] == 2
    assert receipt["account_evidence"] == evidence
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_type, details
            FROM control_plane_maintenance_fence_events
            WHERE fence_id=%s
            ORDER BY maintenance_event_id DESC
            LIMIT 1
            """,
            (fence_id,),
        )
        event_type, details = cur.fetchone()
    assert event_type == "stage_verified"
    assert details == {
        "account_evidence_sha256": evidence_sha256,
        "redis_fencing_epoch": REDIS_FENCING_EPOCH,
        "verification_mode": "frozen_pre_stop_evidence",
    }


def test_frozen_stage_verification_rejects_evidence_hash_drift(
    frozen_fence_db: str,
) -> None:
    migrated_db = frozen_fence_db
    _seed_fleet(migrated_db)
    fence_id, _evidence, _evidence_sha256 = _acquire_frozen_evidence(
        migrated_db
    )

    with pytest.raises(RuntimeError, match="evidence hash mismatch"):
        _verify_frozen(
            migrated_db,
            fence_id=fence_id,
            evidence_sha256="0" * 64,
        )


def test_frozen_stage_verification_rejects_owner_token_drift(
    frozen_fence_db: str,
) -> None:
    migrated_db = frozen_fence_db
    _seed_fleet(migrated_db)
    fence_id, _evidence, evidence_sha256 = _acquire_frozen_evidence(
        migrated_db
    )

    with pytest.raises(RuntimeError, match="owner token mismatch"):
        _verify_frozen(
            migrated_db,
            fence_id=fence_id,
            evidence_sha256=evidence_sha256,
            owner_token="b" * 64,
        )


def test_frozen_stage_verification_rejects_expired_lease(
    frozen_fence_db: str,
) -> None:
    migrated_db = frozen_fence_db
    _seed_fleet(migrated_db)
    fence_id, _evidence, evidence_sha256 = _acquire_frozen_evidence(
        migrated_db
    )
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE control_plane_maintenance_fences
            SET acquired_at=now() - interval '2 minutes',
                refreshed_at=now() - interval '1 minute',
                expires_at=now() - interval '1 second'
            WHERE fence_id=%s
            """,
            (fence_id,),
        )

    with pytest.raises(RuntimeError, match="inactive or expired"):
        _verify_frozen(
            migrated_db,
            fence_id=fence_id,
            evidence_sha256=evidence_sha256,
        )


def test_frozen_stage_verification_rejects_redis_epoch_drift(
    frozen_fence_db: str,
) -> None:
    migrated_db = frozen_fence_db
    _seed_fleet(migrated_db)
    fence_id, _evidence, evidence_sha256 = _acquire_frozen_evidence(
        migrated_db
    )
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _replace_active_redis_fencing_epoch(cur)

    with pytest.raises(RuntimeError, match="Redis fencing epoch drifted"):
        _verify_frozen(
            migrated_db,
            fence_id=fence_id,
            evidence_sha256=evidence_sha256,
        )


def test_stage_verification_rejects_writer_and_release_drift(
    migrated_db: str,
) -> None:
    _seed_fleet(migrated_db)
    fence_id = str(uuid4())
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _acquire(cur, fence_id=fence_id)
        cur.execute(
            """
            UPDATE node_heartbeats
            SET runtime_generation='replacement-runtime',
                release_id='replacement-release',
                heartbeat_sequence=heartbeat_sequence + 1,
                last_seen_at=now()
            WHERE account_id='account-b'
            """
        )
        with pytest.raises(psycopg2.Error, match="identity drifted"):
            cur.execute(
                """
                SELECT *
                FROM verify_control_plane_maintenance_fence(
                    %s, %s, 'before-test-mutation', 30, 10
                )
                """,
                (fence_id, OWNER_TOKEN),
            )
        conn.rollback()


def test_expired_fence_takeover_preserves_audit(
    migrated_db: str,
) -> None:
    _seed_fleet(migrated_db)
    first_fence_id = str(uuid4())
    second_fence_id = str(uuid4())
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _acquire(cur, fence_id=first_fence_id)
        cur.execute(
            """
            UPDATE control_plane_maintenance_fences
            SET acquired_at=now() - interval '1 minute',
                refreshed_at=now() - interval '1 minute',
                expires_at=now() - interval '1 second'
            WHERE fence_id=%s
            """,
            (first_fence_id,),
        )
        _acquire(
            cur,
            fence_id=second_fence_id,
            owner_token="b" * 64,
            operation="takeover-test",
        )
        cur.execute(
            """
            SELECT fence_id::text, event_type
            FROM control_plane_maintenance_fence_events
            ORDER BY maintenance_event_id
            """
        )
        assert cur.fetchall() == [
            (first_fence_id, "acquired"),
            (first_fence_id, "expired"),
            (second_fence_id, "taken_over"),
        ]
        conn.rollback()


@pytest.mark.parametrize(
    ("database_role", "forbidden_sql"),
    (
        (
            "trader_v3_node_control",
            "UPDATE execution_events SET payload=payload WHERE false",
        ),
        (
            "trader_v3_event_ingest",
            "UPDATE node_heartbeats SET status=status WHERE false",
        ),
        (
            "trader_v3_operator_query",
            "UPDATE node_heartbeats SET status=status WHERE false",
        ),
    ),
)
def test_runtime_database_roles_are_login_bound_and_fail_forbidden_probe(
    migrated_db: str,
    database_role: str,
    forbidden_sql: str,
) -> None:
    with psycopg2.connect(migrated_db, user=database_role) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT session_user, current_user")
            assert cur.fetchone() == (database_role, database_role)
            cur.execute("SAVEPOINT role_probe")
            with pytest.raises(
                psycopg2.errors.InsufficientPrivilege,
            ):
                cur.execute(forbidden_sql)
            cur.execute("ROLLBACK TO SAVEPOINT role_probe")
