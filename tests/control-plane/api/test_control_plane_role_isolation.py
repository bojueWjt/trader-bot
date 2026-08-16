from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg2
import pytest
from fastapi.testclient import TestClient
from psycopg2.extensions import make_dsn

import pools
import read_api
from app_roles import AppRole
from pools import (
    BoundedPostgresPool,
    PoolCheckoutTimeout,
    PoolConfigurationError,
    PostgresPoolConfig,
    pool_config_for_role,
)


NODE_A = "node-a"
NODE_B = "node-b"
ACCOUNT_A = "account-a"
ACCOUNT_B = "account-b"
NODE_A_TOKEN = "node-a-token"
NODE_B_TOKEN = "node-b-token"
RELEASE_ID = "release-reviewed-a"
IMAGE_DIGEST = "sha256:" + ("1" * 64)
CONFIG_SHA256 = "2" * 64
DEPENDENCY_SHA256 = "3" * 64
SCHEMA_EPOCH = "0015_refresh_evidence_command"
REDIS_FENCING_EPOCH = "11111111-1111-4111-8111-111111111111"


def _insert_active_redis_fencing_epoch(cur) -> None:
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
            'test-redis-volume', 'test', now()
        )
        """,
        (
            REDIS_FENCING_EPOCH,
            "6" * 64,
            "7" * 64,
            "a" * 40,
        ),
    )


def _application_paths(app) -> set[str]:
    return {
        route.path
        for route in app.routes
        if route.path.startswith(("/v1/", "/api/", "/health/"))
    }


def _route_methods(app, path: str) -> set[str]:
    methods: set[str] = set()
    for route in app.routes:
        if route.path != path:
            continue
        methods.update(route.methods or set())
    return methods


def test_role_apps_expose_only_their_owned_routes() -> None:
    node_control = _application_paths(read_api.create_app(AppRole.NODE_CONTROL))
    event_ingest = _application_paths(read_api.create_app(AppRole.EVENT_INGEST))
    operator_query = _application_paths(read_api.create_app(AppRole.OPERATOR_QUERY))

    assert node_control == {
        "/health/role",
        "/v1/accounts/{account_id}",
        "/v1/nodes/{node_id}/commands",
        "/v1/nodes/{node_id}/commands/{command_id}/ack",
        "/v1/nodes/{node_id}/exchange-state",
        "/v1/nodes/{node_id}/heartbeat",
        "/v1/nodes/{node_id}/incidents",
        "/v1/nodes/{node_id}/intents",
        "/v1/nodes/{node_id}/intents/{intent_id}/ack",
    }
    assert event_ingest == {
        "/health/role",
        "/v1/nodes/{node_id}/events",
        "/v1/nodes/{node_id}/execution-events",
    }
    assert "/health/role" in operator_query
    assert "/api/system/snapshot" in operator_query
    assert "/v1/operator/orders" in operator_query
    assert "/v1/nodes/{node_id}/heartbeat" not in operator_query
    assert "/v1/nodes/{node_id}/execution-events" not in operator_query


def test_settings_admin_routes_belong_only_to_operator_query() -> None:
    node_control = read_api.create_app(AppRole.NODE_CONTROL)
    event_ingest = read_api.create_app(AppRole.EVENT_INGEST)
    operator_query = read_api.create_app(AppRole.OPERATOR_QUERY)
    settings_path = "/v1/order-management/settings"

    assert _route_methods(node_control, settings_path) == set()
    assert _route_methods(event_ingest, settings_path) == set()
    assert _route_methods(operator_query, settings_path) == {
        "GET",
        "PATCH",
    }
    assert _route_methods(
        operator_query,
        f"{settings_path}/rollback",
    ) == {"POST"}
    assert _route_methods(
        operator_query,
        f"{settings_path}/import",
    ) == {"POST"}


def test_default_all_role_preserves_the_existing_route_surface() -> None:
    role_app = read_api.create_app(AppRole.ALL)

    assert _application_paths(role_app) == _application_paths(read_api.all_role_app)


@pytest.mark.parametrize(
    ("app_role", "database_role"),
    (
        (AppRole.NODE_CONTROL, "trader_v3_node_control"),
        (AppRole.EVENT_INGEST, "trader_v3_event_ingest"),
        (AppRole.OPERATOR_QUERY, "trader_v3_operator_query"),
    ),
)
def test_role_health_checks_out_real_role_connection_and_echoes_identity(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
    app_role: AppRole,
    database_role: str,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        make_dsn(migrated_db, user=database_role),
    )
    monkeypatch.setenv(
        "CONTROL_PLANE_EXPECT_DATABASE_ROLE",
        database_role,
    )

    with TestClient(read_api.create_app(app_role)) as client:
        response = client.get("/health/role")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "app_role": app_role.value,
        "expected_database_role": database_role,
        "session_user": database_role,
        "current_user": database_role,
        "rollback_only_permission_probe": "pass",
    }


def test_each_role_has_independent_bounded_pool_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROL_PLANE_NODE_CONTROL_DB_POOL_SIZE", "3")
    monkeypatch.setenv(
        "CONTROL_PLANE_NODE_CONTROL_DB_CHECKOUT_TIMEOUT_SECONDS",
        "0.25",
    )
    monkeypatch.setenv(
        "CONTROL_PLANE_NODE_CONTROL_DB_CONNECT_TIMEOUT_SECONDS",
        "0.2",
    )
    monkeypatch.setenv(
        "CONTROL_PLANE_NODE_CONTROL_DB_STATEMENT_TIMEOUT_MS",
        "1200",
    )
    monkeypatch.setenv("CONTROL_PLANE_OPERATOR_QUERY_DB_POOL_SIZE", "9")

    node_config = pool_config_for_role(AppRole.NODE_CONTROL)
    query_config = pool_config_for_role(AppRole.OPERATOR_QUERY)

    assert node_config == PostgresPoolConfig(
        role=AppRole.NODE_CONTROL.value,
        max_size=3,
        checkout_timeout_seconds=0.25,
        connect_timeout_seconds=0.2,
        statement_timeout_ms=1200,
    )
    assert query_config.role == AppRole.OPERATOR_QUERY.value
    assert query_config.max_size == 9
    assert query_config != node_config


@pytest.mark.parametrize(
    ("connect_timeout_seconds", "checkout_timeout_seconds"),
    (
        (0.0, 1.0),
        (-0.1, 1.0),
        (1.01, 1.0),
        (float("inf"), 1.0),
    ),
)
def test_pool_rejects_connect_timeout_outside_total_checkout_budget(
    connect_timeout_seconds: float,
    checkout_timeout_seconds: float,
) -> None:
    with pytest.raises(PoolConfigurationError, match="connect timeout"):
        PostgresPoolConfig(
            role=AppRole.NODE_CONTROL.value,
            max_size=1,
            checkout_timeout_seconds=checkout_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
            statement_timeout_ms=1000,
        )


def test_pool_rejects_environment_connect_timeout_above_checkout_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CONTROL_PLANE_NODE_CONTROL_DB_CHECKOUT_TIMEOUT_SECONDS",
        "0.5",
    )
    monkeypatch.setenv(
        "CONTROL_PLANE_NODE_CONTROL_DB_CONNECT_TIMEOUT_SECONDS",
        "0.51",
    )

    with pytest.raises(PoolConfigurationError, match="must be between"):
        pool_config_for_role(AppRole.NODE_CONTROL)


@dataclass
class _FakeConnection:
    name: str
    closed: int = 0
    commit_calls: int = 0
    rollback_calls: int = 0

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.closed = 1


def test_query_pool_exhaustion_cannot_consume_node_control_capacity() -> None:
    created: list[_FakeConnection] = []

    def connect(database_url: str, **kwargs) -> _FakeConnection:
        assert database_url == "postgresql://control-plane"
        assert kwargs["options"].startswith("-c statement_timeout=")
        connection = _FakeConnection(name=kwargs["application_name"])
        created.append(connection)
        return connection

    query_pool = BoundedPostgresPool(
        "postgresql://control-plane",
        PostgresPoolConfig(
            role=AppRole.OPERATOR_QUERY.value,
            max_size=1,
            checkout_timeout_seconds=0.01,
            connect_timeout_seconds=0.01,
            statement_timeout_ms=4000,
        ),
        connect=connect,
    )
    node_pool = BoundedPostgresPool(
        "postgresql://control-plane",
        PostgresPoolConfig(
            role=AppRole.NODE_CONTROL.value,
            max_size=1,
            checkout_timeout_seconds=0.01,
            connect_timeout_seconds=0.01,
            statement_timeout_ms=1000,
        ),
        connect=connect,
    )

    query_lease = query_pool.checkout()
    try:
        with pytest.raises(PoolCheckoutTimeout):
            query_pool.checkout()
        node_lease = node_pool.checkout()
        node_lease.close()
    finally:
        query_lease.close()
        query_pool.close()
        node_pool.close()

    assert [connection.name for connection in created] == [
        "trader-control-plane:operator-query",
        "trader-control-plane:node-control",
    ]
    assert len(created) == 2


def test_pool_context_manager_commits_and_returns_connection() -> None:
    connection = _FakeConnection(name="node-control")
    pool = BoundedPostgresPool(
        "postgresql://control-plane",
        PostgresPoolConfig(
            role=AppRole.NODE_CONTROL.value,
            max_size=1,
            checkout_timeout_seconds=0.01,
            connect_timeout_seconds=0.01,
            statement_timeout_ms=1000,
        ),
        connect=lambda *_args, **_kwargs: connection,
    )

    with pool.checkout():
        assert pool.checked_out == 1

    assert connection.commit_calls == 1
    assert connection.rollback_calls == 1
    assert pool.checked_out == 0
    pool.close()


def test_pool_context_manager_rolls_back_and_returns_connection() -> None:
    connection = _FakeConnection(name="event-ingest")
    pool = BoundedPostgresPool(
        "postgresql://control-plane",
        PostgresPoolConfig(
            role=AppRole.EVENT_INGEST.value,
            max_size=1,
            checkout_timeout_seconds=0.01,
            connect_timeout_seconds=0.01,
            statement_timeout_ms=1000,
        ),
        connect=lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(RuntimeError, match="boom"):
        with pool.checkout():
            raise RuntimeError("boom")

    assert connection.commit_calls == 0
    assert connection.rollback_calls == 2
    assert pool.checked_out == 0
    pool.close()


def test_pool_context_manager_close_is_idempotent() -> None:
    connection = _FakeConnection(name="operator-query")
    pool = BoundedPostgresPool(
        "postgresql://control-plane",
        PostgresPoolConfig(
            role=AppRole.OPERATOR_QUERY.value,
            max_size=1,
            checkout_timeout_seconds=0.01,
            connect_timeout_seconds=0.01,
            statement_timeout_ms=1000,
        ),
        connect=lambda *_args, **_kwargs: connection,
    )
    lease = pool.checkout()

    lease.close()
    lease.close()

    assert connection.rollback_calls == 1
    assert pool.checked_out == 0
    pool.close()


def test_pool_passes_rounded_up_connect_timeout_to_psycopg2() -> None:
    connection = _FakeConnection(name="node-control")
    connect_kwargs: dict[str, object] = {}

    def connect(_database_url: str, **kwargs) -> _FakeConnection:
        connect_kwargs.update(kwargs)
        return connection

    pool = BoundedPostgresPool(
        "postgresql://control-plane",
        PostgresPoolConfig(
            role=AppRole.NODE_CONTROL.value,
            max_size=1,
            checkout_timeout_seconds=2.0,
            connect_timeout_seconds=1.01,
            statement_timeout_ms=1000,
        ),
        connect=connect,
    )

    lease = pool.checkout()
    lease.close()

    assert connect_kwargs["connect_timeout"] == 2
    pool.close()


def test_pool_connect_timeout_uses_remaining_checkout_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="node-control")
    connect_kwargs: dict[str, object] = {}
    timestamps = iter((10.0, 11.2, 11.25, 11.3))
    monkeypatch.setattr(pools.time, "monotonic", lambda: next(timestamps))

    def connect(_database_url: str, **kwargs) -> _FakeConnection:
        connect_kwargs.update(kwargs)
        return connection

    pool = BoundedPostgresPool(
        "postgresql://control-plane",
        PostgresPoolConfig(
            role=AppRole.NODE_CONTROL.value,
            max_size=1,
            checkout_timeout_seconds=3.0,
            connect_timeout_seconds=2.6,
            statement_timeout_ms=1000,
        ),
        connect=connect,
    )

    lease = pool.checkout()
    lease.close()

    assert connect_kwargs["connect_timeout"] == 2
    pool.close()


def test_pool_enforces_total_deadline_and_reclaims_late_connection() -> None:
    connection = _FakeConnection(name="node-control")
    connect_started = threading.Event()
    allow_connect_to_finish = threading.Event()
    connect_calls = 0

    def connect(*_args, **_kwargs) -> _FakeConnection:
        nonlocal connect_calls
        connect_calls += 1
        connect_started.set()
        allow_connect_to_finish.wait(timeout=1.0)
        return connection

    pool = BoundedPostgresPool(
        "postgresql://control-plane",
        PostgresPoolConfig(
            role=AppRole.NODE_CONTROL.value,
            max_size=1,
            checkout_timeout_seconds=0.05,
            connect_timeout_seconds=0.05,
            statement_timeout_ms=1000,
        ),
        connect=connect,
    )

    started_at = time.monotonic()
    with pytest.raises(PoolCheckoutTimeout, match="establishing"):
        pool.checkout()
    elapsed_seconds = time.monotonic() - started_at

    assert connect_started.is_set()
    assert elapsed_seconds < 0.5
    assert pool.checked_out == 1

    with pytest.raises(PoolCheckoutTimeout):
        pool.checkout()
    assert connect_calls == 1

    allow_connect_to_finish.set()
    deadline = time.monotonic() + 1.0
    while pool.checked_out != 0 and time.monotonic() < deadline:
        time.sleep(0.005)

    assert pool.checked_out == 0
    assert connection.closed == 1
    pool.close()


def test_pool_releases_capacity_when_connection_worker_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = BoundedPostgresPool(
        "postgresql://control-plane",
        PostgresPoolConfig(
            role=AppRole.NODE_CONTROL.value,
            max_size=1,
            checkout_timeout_seconds=0.1,
            connect_timeout_seconds=0.1,
            statement_timeout_ms=1000,
        ),
        connect=lambda *_args, **_kwargs: _FakeConnection("node-control"),
    )

    def fail_start(_thread) -> None:
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(pools.threading.Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="thread start failed"):
        pool.checkout()

    assert pool.checked_out == 0
    pool.close()


def test_pool_registry_separates_connect_timeout_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_configs: list[PostgresPoolConfig] = []

    class _RegistryPool:
        def __init__(self, _database_url, config) -> None:
            created_configs.append(config)

        def checkout(self):
            return object()

    monkeypatch.setattr(pools, "_POOL_REGISTRY", {})
    monkeypatch.setattr(pools, "BoundedPostgresPool", _RegistryPool)
    monkeypatch.setenv(
        "CONTROL_PLANE_NODE_CONTROL_DB_CHECKOUT_TIMEOUT_SECONDS",
        "2",
    )
    monkeypatch.setenv(
        "CONTROL_PLANE_NODE_CONTROL_DB_CONNECT_TIMEOUT_SECONDS",
        "1",
    )
    pools.checkout_role_connection(
        "postgresql://control-plane",
        AppRole.NODE_CONTROL,
    )
    monkeypatch.setenv(
        "CONTROL_PLANE_NODE_CONTROL_DB_CONNECT_TIMEOUT_SECONDS",
        "1.5",
    )
    pools.checkout_role_connection(
        "postgresql://control-plane",
        AppRole.NODE_CONTROL,
    )

    assert [config.connect_timeout_seconds for config in created_configs] == [
        1.0,
        1.5,
    ]


def test_halted_account_a_canary_reports_account_b_rollout_pending(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        json.dumps(
            {
                NODE_A: {"account_id": ACCOUNT_A, "token": NODE_A_TOKEN},
                NODE_B: {"account_id": ACCOUNT_B, "token": NODE_B_TOKEN},
            }
        ),
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _insert_active_redis_fencing_epoch(cur)
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'reviewer')
            """,
            (
                RELEASE_ID,
                REDIS_FENCING_EPOCH,
                IMAGE_DIGEST,
                CONFIG_SHA256,
                DEPENDENCY_SHA256,
                SCHEMA_EPOCH,
                "4" * 64,
                "5" * 64,
                "register-release-reviewed-a",
            ),
        )
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
            SELECT account_id,
                   %s,
                   %s,
                   %s,
                   %s,
                   %s,
                   'reviewed',
                   'reviewer'
            FROM unnest(%s::text[]) AS account_id
            """,
            (
                RELEASE_ID,
                IMAGE_DIGEST,
                CONFIG_SHA256,
                DEPENDENCY_SHA256,
                SCHEMA_EPOCH,
                [ACCOUNT_A, ACCOUNT_B],
            ),
        )
        cur.execute(
            """
            INSERT INTO node_heartbeats (
                node_id,
                account_id,
                status,
                release_id,
                image_digest,
                config_sha256,
                dependency_lock_sha256,
                schema_epoch,
                last_seen_at
            )
            VALUES (%s, %s, 'HALTED', %s, %s, %s, %s, %s, now())
            """,
            (
                NODE_B,
                ACCOUNT_B,
                "release-drifted-b",
                "sha256:" + ("9" * 64),
                CONFIG_SHA256,
                DEPENDENCY_SHA256,
                SCHEMA_EPOCH,
            ),
        )

    heartbeat_body = {
        "account_id": ACCOUNT_A,
        "ts": now.isoformat(),
        "trading_state": "HALTED",
        "readiness": True,
        "projection_lag_ms": 0,
        "reconciliation_state": "healthy",
        "release_id": RELEASE_ID,
        "image_digest": IMAGE_DIGEST,
        "config_sha256": CONFIG_SHA256,
        "dependency_lock_sha256": DEPENDENCY_SHA256,
        "schema_epoch": SCHEMA_EPOCH,
        "positions": [],
        "regular_orders": [],
        "algo_orders": [],
        "positions_snapshot_at": now.isoformat(),
        "regular_orders_snapshot_at": now.isoformat(),
        "algo_orders_snapshot_at": now.isoformat(),
        "reconciliation_completed_at": now.isoformat(),
        "redis_fencing_epoch": REDIS_FENCING_EPOCH,
        "runtime_generation": "runtime-a",
        "lease_fencing_token": 41,
        "heartbeat_sequence": 1,
    }
    with TestClient(read_api.create_app(AppRole.NODE_CONTROL)) as client:
        response = client.post(
            f"/v1/nodes/{NODE_A}/heartbeat",
            headers={
                "Authorization": f"Bearer {NODE_A_TOKEN}",
                "X-Node-Id": NODE_A,
                "X-Account-Id": ACCOUNT_A,
            },
            json=heartbeat_body,
        )
        active_body = dict(heartbeat_body)
        active_body["trading_state"] = "ACTIVE"
        active_body["heartbeat_sequence"] = 2
        active_response = client.post(
            f"/v1/nodes/{NODE_A}/heartbeat",
            headers={
                "Authorization": f"Bearer {NODE_A_TOKEN}",
                "X-Node-Id": NODE_A,
                "X-Account-Id": ACCOUNT_A,
            },
            json=active_body,
        )

    assert response.status_code == 200
    receipt = response.json()
    assert receipt["release_gate"]["status"] == "pass"
    assert (
        receipt["release_gate"]["rollout_phase"]
        == "account_a_canary"
    )
    assert receipt["release_gate"]["reviewed_manifest"] == {
        "release_id": RELEASE_ID,
        "image_digest": IMAGE_DIGEST,
        "config_sha256": CONFIG_SHA256,
        "dependency_lock_sha256": DEPENDENCY_SHA256,
        "schema_epoch": SCHEMA_EPOCH,
    }
    assert receipt["peers"] == [
        {
            "node_id": NODE_B,
            "account_id": ACCOUNT_B,
            "release_id": "release-drifted-b",
            "image_digest": "sha256:" + ("9" * 64),
            "config_sha256": CONFIG_SHA256,
            "dependency_lock_sha256": DEPENDENCY_SHA256,
            "schema_epoch": SCHEMA_EPOCH,
            "redis_fencing_epoch": False,
            "freshness_age_seconds": pytest.approx(0.0, abs=2.0),
            "fresh": True,
            "identity_matches": False,
            "status": "rollout_pending",
        }
    ]
    assert active_response.status_code == 200
    assert (
        active_response.json()["peers"][0]["status"]
        == "identity_drift"
    )


def test_account_b_old_release_accepts_fresh_halted_account_a_canary(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        json.dumps(
            {
                NODE_A: {"account_id": ACCOUNT_A, "token": NODE_A_TOKEN},
                NODE_B: {"account_id": ACCOUNT_B, "token": NODE_B_TOKEN},
            }
        ),
    )
    old_release_id = "release-reviewed-b-previous"
    old_image_digest = "sha256:" + ("8" * 64)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _insert_active_redis_fencing_epoch(cur)
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'reviewer')
            """,
            (
                RELEASE_ID,
                REDIS_FENCING_EPOCH,
                IMAGE_DIGEST,
                CONFIG_SHA256,
                DEPENDENCY_SHA256,
                SCHEMA_EPOCH,
                "4" * 64,
                "5" * 64,
                "register-release-reviewed-a",
            ),
        )
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
                %s, %s, %s, %s, %s, %s, 'reviewed', 'reviewer'
            )
            """,
            (
                ACCOUNT_B,
                old_release_id,
                old_image_digest,
                CONFIG_SHA256,
                DEPENDENCY_SHA256,
                SCHEMA_EPOCH,
            ),
        )
        cur.execute(
            """
            INSERT INTO node_heartbeats (
                node_id,
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
            )
            VALUES (
                %s, %s, 'HALTED', %s, %s, %s, %s, %s,
                %s, 'runtime-a', 41, 1, now()
            )
            """,
            (
                NODE_A,
                ACCOUNT_A,
                RELEASE_ID,
                IMAGE_DIGEST,
                CONFIG_SHA256,
                DEPENDENCY_SHA256,
                SCHEMA_EPOCH,
                REDIS_FENCING_EPOCH,
            ),
        )

    with TestClient(read_api.create_app(AppRole.NODE_CONTROL)) as client:
        response = client.post(
            f"/v1/nodes/{NODE_B}/heartbeat",
            headers={
                "Authorization": f"Bearer {NODE_B_TOKEN}",
                "X-Node-Id": NODE_B,
                "X-Account-Id": ACCOUNT_B,
            },
            json={
                "account_id": ACCOUNT_B,
                "ts": now.isoformat(),
                "trading_state": "ACTIVE",
                "readiness": True,
                "projection_lag_ms": 0,
                "reconciliation_state": "healthy",
                "release_id": old_release_id,
                "image_digest": old_image_digest,
                "config_sha256": CONFIG_SHA256,
                "dependency_lock_sha256": DEPENDENCY_SHA256,
                "schema_epoch": SCHEMA_EPOCH,
                "positions": [],
                "regular_orders": [],
                "algo_orders": [],
                "positions_snapshot_at": now.isoformat(),
                "regular_orders_snapshot_at": now.isoformat(),
                "algo_orders_snapshot_at": now.isoformat(),
                "reconciliation_completed_at": now.isoformat(),
                "redis_fencing_epoch": REDIS_FENCING_EPOCH,
                "runtime_generation": "runtime-b",
                "lease_fencing_token": 42,
                "heartbeat_sequence": 1,
            },
        )

    assert response.status_code == 200
    receipt = response.json()
    assert receipt["release_gate"]["status"] == "pass"
    assert receipt["peers"][0]["node_id"] == NODE_A
    assert receipt["peers"][0]["status"] == "rollout_pending"
