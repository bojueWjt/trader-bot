from __future__ import annotations

import inspect
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import Json

import read_api


ACCOUNT_A = "account-a"
NODE_A = "nautilus-node-account-a"
NODE_A_2 = "nautilus-node-account-a-shadow"
ACCOUNT_B = "account-b"
NODE_B = "nautilus-node-account-b"
ACCOUNT_C = "account-c"
NODE_C = "nautilus-node-account-c"
ACCOUNT_D = "account-d"
NODE_D = "nautilus-node-account-d"
SYMBOL = "BTCUSDT"
RELEASE_ID = "release-reviewed-a"
IMAGE_DIGEST = "sha256:" + ("1" * 64)
CONFIG_SHA256 = "2" * 64
LOCK_SHA256 = "3" * 64
SCHEMA_EPOCH = "2026-08-08"
RUNTIME_GENERATION = "runtime-generation-a"
LEASE_FENCING_TOKEN = 41
HEARTBEAT_SEQUENCE = 1
REDIS_FENCING_EPOCH = "11111111-1111-4111-8111-111111111111"
REPLACEMENT_REDIS_FENCING_EPOCH = "22222222-2222-4222-8222-222222222222"
MAX_CUMULATIVE_LOSS_USDT = "1.49"
TESTNET_EMERGENCY_CLOSE_EVIDENCE_SHA256 = "4" * 64
EMPTY_PORTFOLIO_BASELINE_SHA256 = (
    "6ae771d5d317b151109d3933059c0c46ec02368fc826444c9a805bdaa775813f"
)
RISK_TOKEN = "risk-token"
NODE_A_TOKEN = "node-a-token"
NODE_B_TOKEN = "node-b-token"
NODE_C_TOKEN = "node-c-token"
NODE_D_TOKEN = "node-d-token"
@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("RISK_ADMIN_TOKEN", RISK_TOKEN)
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        json.dumps(
            {
                NODE_A: {"account_id": ACCOUNT_A, "token": NODE_A_TOKEN},
                NODE_A_2: {"account_id": ACCOUNT_A, "token": "node-a-2-token"},
                NODE_B: {"account_id": ACCOUNT_B, "token": NODE_B_TOKEN},
                NODE_C: {"account_id": ACCOUNT_C, "token": NODE_C_TOKEN},
                NODE_D: {"account_id": ACCOUNT_D, "token": NODE_D_TOKEN},
            }
        ),
    )
    monkeypatch.setattr(
        read_api,
        "_account_risk_capital_addon",
        lambda _account_id: 0.0,
    )
    _activate_redis_epoch(migrated_db, REDIS_FENCING_EPOCH)
    test_client = TestClient(read_api.app)
    try:
        yield test_client
    finally:
        test_client.close()
        _cleanup_rollout_test_data(migrated_db)


def _risk_headers(request_id: str = "request-1") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {RISK_TOKEN}",
        "X-Request-Id": request_id,
    }


def test_regular_resume_scope_allows_omitted_instruments() -> None:
    assert read_api._resume_scope_symbol({}, required=False) is None


def test_canary_resume_scope_requires_one_explicit_instrument() -> None:
    with pytest.raises(
        read_api.HTTPException,
        match="canary RESUME requires exactly one target symbol",
    ):
        read_api._resume_scope_symbol({}, required=True)
    assert (
        read_api._resume_scope_symbol(
            {"instruments": ["SOLUSDT"]},
            required=True,
        )
        == "SOLUSDT"
    )


def _node_headers(
    node_id: str,
    account_id: str,
    token: str,
    *,
    redis_fencing_epoch: str = REDIS_FENCING_EPOCH,
    runtime_generation: str = RUNTIME_GENERATION,
    lease_fencing_token: int = LEASE_FENCING_TOKEN,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Node-Id": node_id,
        "X-Account-Id": account_id,
        "X-Redis-Fencing-Epoch": redis_fencing_epoch,
        "X-Runtime-Generation": runtime_generation,
        "X-Lease-Fencing-Token": str(lease_fencing_token),
    }


def _connect(url: str):
    return psycopg2.connect(url)


class _CanaryReadinessCursor:
    def __init__(
        self,
        rows: list[tuple],
        *,
        approved_old_release: bool = True,
        registration_event: tuple | None = None,
    ) -> None:
        self._rows = rows
        self._approved_old_release = approved_old_release
        self._registration_event = registration_event
        self._query = ""

    def execute(self, query: str, _params=None) -> None:
        self._query = query

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self):
        if "FROM reviewed_release_rollout_events" in self._query:
            return self._registration_event
        if "FROM reviewed_release_manifests" in self._query:
            if self._approved_old_release:
                return (1,)
            return None
        raise AssertionError(f"unexpected fetchone query: {self._query}")


def _canary_readiness_row(
    account_id: str,
    *,
    status: str,
    upgraded: bool,
) -> tuple:
    release_id = RELEASE_ID
    image_digest = IMAGE_DIGEST
    config_sha256 = CONFIG_SHA256
    lock_sha256 = LOCK_SHA256
    schema_epoch = SCHEMA_EPOCH
    if not upgraded:
        release_id = f"approved-old-{account_id}"
        image_digest = "sha256:" + (account_id[-1] * 64)
        config_sha256 = account_id[-1] * 64
        lock_sha256 = "8" * 64
        schema_epoch = f"approved-old-schema-{account_id}"
    return (
        account_id,
        f"nautilus-node-{account_id}",
        release_id,
        image_digest,
        config_sha256,
        lock_sha256,
        schema_epoch,
        REDIS_FENCING_EPOCH,
        status,
        0.1,
    )


def _stub_canary_readiness_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rollout_phase: str,
    registration_mode: str | None = None,
) -> None:
    rollout = {
        "release_id": RELEASE_ID,
        "redis_fencing_epoch": REDIS_FENCING_EPOCH,
        "image_digest": IMAGE_DIGEST,
        "config_sha256": CONFIG_SHA256,
        "dependency_lock_sha256": LOCK_SHA256,
        "schema_epoch": SCHEMA_EPOCH,
        "phase": rollout_phase,
    }
    if registration_mode:
        rollout["registration_idempotency_key"] = (
            "register-live-safety-release"
        )
        rollout["reviewed_by"] = "reviewer"
    monkeypatch.setattr(
        read_api,
        "_reviewed_rollout_state",
        lambda _cur, _release_id: rollout,
    )
    monkeypatch.setattr(
        read_api,
        "_active_redis_fencing_epoch",
        lambda *_args, **_kwargs: REDIS_FENCING_EPOCH,
    )
    monkeypatch.setattr(
        read_api,
        "_validate_reviewed_fleet_manifests",
        lambda *_args, **_kwargs: None,
    )


def test_canary_readiness_allows_one_active_target_with_halted_peers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_canary_readiness_dependencies(
        monkeypatch,
        rollout_phase="account_b_rollout",
    )
    rows = [
        _canary_readiness_row(
            ACCOUNT_A,
            status="HALTED",
            upgraded=True,
        ),
        _canary_readiness_row(
            ACCOUNT_B,
            status="ACTIVE",
            upgraded=True,
        ),
        _canary_readiness_row(
            ACCOUNT_C,
            status="HALTED",
            upgraded=False,
        ),
        _canary_readiness_row(
            ACCOUNT_D,
            status="HALTED",
            upgraded=False,
        ),
    ]

    read_api._validate_canary_release_ready(
        _CanaryReadinessCursor(rows),
        account_id=ACCOUNT_B,
        required_rollout_phase="account_b_rollout",
        expected_trading_state="ACTIVE",
        release_identity=(
            RELEASE_ID,
            IMAGE_DIGEST,
            CONFIG_SHA256,
            LOCK_SHA256,
            SCHEMA_EPOCH,
        ),
    )


def test_canary_readiness_accepts_migration_all_halted_new_release_peers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_canary_readiness_dependencies(
        monkeypatch,
        rollout_phase="account_b_rollout",
        registration_mode="migration_rebaseline_stopped",
    )
    rows = [
        _canary_readiness_row(
            ACCOUNT_A,
            status="HALTED",
            upgraded=True,
        ),
        _canary_readiness_row(
            ACCOUNT_B,
            status="ACTIVE",
            upgraded=True,
        ),
        _canary_readiness_row(
            ACCOUNT_C,
            status="HALTED",
            upgraded=True,
        ),
        _canary_readiness_row(
            ACCOUNT_D,
            status="HALTED",
            upgraded=True,
        ),
    ]
    registration_event = (
        "registered",
        "account_a_canary",
        1,
        "reviewer",
        {
            "registration_mode": "migration_rebaseline_stopped",
            "bootstrap_all_halted": True,
        },
    )

    read_api._validate_canary_release_ready(
        _CanaryReadinessCursor(
            rows,
            registration_event=registration_event,
        ),
        account_id=ACCOUNT_B,
        required_rollout_phase="account_b_rollout",
        expected_trading_state="ACTIVE",
        release_identity=(
            RELEASE_ID,
            IMAGE_DIGEST,
            CONFIG_SHA256,
            LOCK_SHA256,
            SCHEMA_EPOCH,
        ),
    )


def test_canary_readiness_allows_missing_non_target_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_canary_readiness_dependencies(
        monkeypatch,
        rollout_phase="account_a_canary",
    )
    rows = [
        _canary_readiness_row(
            ACCOUNT_A,
            status="HALTED",
            upgraded=True,
        ),
        _canary_readiness_row(
            ACCOUNT_B,
            status="HALTED",
            upgraded=False,
        ),
        _canary_readiness_row(
            ACCOUNT_C,
            status="HALTED",
            upgraded=False,
        ),
    ]

    read_api._validate_canary_release_ready(
        _CanaryReadinessCursor(rows),
        account_id=ACCOUNT_A,
        required_rollout_phase="account_a_canary",
        expected_trading_state="HALTED",
        release_identity=(
            RELEASE_ID,
            IMAGE_DIGEST,
            CONFIG_SHA256,
            LOCK_SHA256,
            SCHEMA_EPOCH,
        ),
    )


def test_canary_readiness_allows_active_non_target_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_canary_readiness_dependencies(
        monkeypatch,
        rollout_phase="account_a_canary",
    )
    rows = [
        _canary_readiness_row(
            ACCOUNT_A,
            status="HALTED",
            upgraded=True,
        ),
        _canary_readiness_row(
            ACCOUNT_B,
            status="HALTED",
            upgraded=False,
        ),
        _canary_readiness_row(
            ACCOUNT_C,
            status="ACTIVE",
            upgraded=False,
        ),
        _canary_readiness_row(
            ACCOUNT_D,
            status="HALTED",
            upgraded=False,
        ),
    ]

    read_api._validate_canary_release_ready(
        _CanaryReadinessCursor(rows),
        account_id=ACCOUNT_A,
        required_rollout_phase="account_a_canary",
        expected_trading_state="HALTED",
        release_identity=(
            RELEASE_ID,
            IMAGE_DIGEST,
            CONFIG_SHA256,
            LOCK_SHA256,
            SCHEMA_EPOCH,
        ),
    )


def _activate_redis_epoch(
    url: str,
    redis_fencing_epoch: str,
) -> None:
    with _connect(url) as conn, conn.cursor() as cur:
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
                %s, 'trader-v3', 'active', %s, %s, %s, %s,
                'test', now()
            )
            """,
            (
                redis_fencing_epoch,
                "1" * 64,
                "2" * 64,
                "a" * 40,
                f"redis-volume-{redis_fencing_epoch}",
            ),
        )


def _cleanup_rollout_test_data(url: str) -> None:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM live_canary_permits")
        cur.execute("DELETE FROM reviewed_release_rollout_events")
        cur.execute("DELETE FROM reviewed_release_rollouts")


def _seed_heartbeat(
    url: str,
    *,
    node_id: str = NODE_A,
    account_id: str = ACCOUNT_A,
    trading_state: str = "HALTED",
    release_id: str = RELEASE_ID,
    positions: list[dict] | None = None,
    regular_orders: list[dict] | None = None,
    algo_orders: list[dict] | None = None,
    age_seconds: int = 0,
    runtime_generation: str = RUNTIME_GENERATION,
    lease_fencing_token: int = LEASE_FENCING_TOKEN,
    heartbeat_sequence: int = HEARTBEAT_SEQUENCE,
    redis_fencing_epoch: str = REDIS_FENCING_EPOCH,
    health_degraded_reasons: list[str] | None = None,
    projection_lag_ms: int = 0,
    reconciliation_state: str = "healthy",
    evidence_age_seconds: int | None = None,
    margin_age_seconds: int = 0,
    available_balance: int | float = 100,
    equity: int | float = 100,
) -> None:
    now = datetime.now(timezone.utc)
    heartbeat_at = now - timedelta(seconds=age_seconds)
    if evidence_age_seconds is None:
        evidence_age_seconds = age_seconds
    evidence_at = now - timedelta(seconds=evidence_age_seconds)
    margin_at = now - timedelta(seconds=margin_age_seconds)
    account_snapshot_fetched_at = margin_at
    with _connect(url) as conn, conn.cursor() as cur:
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
                positions,
                regular_orders,
                algo_orders,
                positions_snapshot_at,
                regular_orders_snapshot_at,
                algo_orders_snapshot_at,
                reconciliation_completed_at,
                redis_fencing_epoch,
                runtime_generation,
                lease_fencing_token,
                heartbeat_sequence,
                last_seen_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (node_id) DO UPDATE SET
                account_id=EXCLUDED.account_id,
                status=EXCLUDED.status,
                payload=EXCLUDED.payload,
                release_id=EXCLUDED.release_id,
                image_digest=EXCLUDED.image_digest,
                config_sha256=EXCLUDED.config_sha256,
                dependency_lock_sha256=EXCLUDED.dependency_lock_sha256,
                schema_epoch=EXCLUDED.schema_epoch,
                positions=EXCLUDED.positions,
                regular_orders=EXCLUDED.regular_orders,
                algo_orders=EXCLUDED.algo_orders,
                positions_snapshot_at=EXCLUDED.positions_snapshot_at,
                regular_orders_snapshot_at=EXCLUDED.regular_orders_snapshot_at,
                algo_orders_snapshot_at=EXCLUDED.algo_orders_snapshot_at,
                reconciliation_completed_at=EXCLUDED.reconciliation_completed_at,
                redis_fencing_epoch=EXCLUDED.redis_fencing_epoch,
                runtime_generation=EXCLUDED.runtime_generation,
                lease_fencing_token=EXCLUDED.lease_fencing_token,
                heartbeat_sequence=EXCLUDED.heartbeat_sequence,
                last_seen_at=EXCLUDED.last_seen_at
            """,
            (
                node_id,
                account_id,
                trading_state,
                release_id,
                Json(
                    {
                        "readiness": True,
                        "projection_lag_ms": projection_lag_ms,
                        "reconciliation_state": reconciliation_state,
                        "health_degraded_reasons": (
                            health_degraded_reasons or []
                        ),
                        "ts": heartbeat_at.isoformat(),
                    }
                ),
                release_id,
                IMAGE_DIGEST,
                CONFIG_SHA256,
                LOCK_SHA256,
                SCHEMA_EPOCH,
                Json(positions or []),
                Json(regular_orders or []),
                Json(algo_orders or []),
                evidence_at,
                evidence_at,
                evidence_at,
                evidence_at,
                redis_fencing_epoch,
                runtime_generation,
                lease_fencing_token,
                heartbeat_sequence,
                heartbeat_at,
            ),
        )
        cur.execute(
            """
            INSERT INTO accounts_projection (
                account_id,
                currency,
                equity,
                margin,
                available_balance,
                updated_at,
                payload
            )
            VALUES (%s, 'USDT', %s, 0, %s, %s, %s)
            ON CONFLICT (account_id) DO UPDATE SET
                currency=EXCLUDED.currency,
                equity=EXCLUDED.equity,
                margin=EXCLUDED.margin,
                available_balance=EXCLUDED.available_balance,
                updated_at=EXCLUDED.updated_at,
                payload=EXCLUDED.payload
            """,
            (
                account_id,
                equity,
                available_balance,
                margin_at,
                Json(
                    {
                        "account_snapshot_source": (
                            "binance_fapi_account_v3"
                        ),
                        "account_snapshot_fetched_at": (
                            account_snapshot_fetched_at.isoformat()
                        ),
                        "exchange_account": {
                            "currency": "USDT",
                            "equity": equity,
                            "margin": 0,
                            "free": available_balance,
                        },
                    }
                ),
            ),
        )


def _seed_reviewed_release_and_permit(
    url: str,
    *,
    permit_id: str | None = None,
    permit_account_id: str = ACCOUNT_A,
    max_notional: str = "12",
    max_cumulative_loss: str = MAX_CUMULATIVE_LOSS_USDT,
    permit_status: str = "issued",
    evidence_age_seconds: int = 0,
    rollout_phase: str = "account_a_canary",
    seed_fleet_peers: bool = True,
    registration_mode: str | None = None,
) -> str:
    permit_id = permit_id or str(uuid4())
    portfolio_baseline_sha256 = None
    if permit_status == "armed":
        portfolio_baseline_sha256 = EMPTY_PORTFOLIO_BASELINE_SHA256
    evidence_verified_at = (
        datetime.now(timezone.utc)
        - timedelta(seconds=evidence_age_seconds)
    )
    with _connect(url) as conn, conn.cursor() as cur:
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
            ON CONFLICT (release_id) DO NOTHING
            """,
            (
                RELEASE_ID,
                REDIS_FENCING_EPOCH,
                IMAGE_DIGEST,
                CONFIG_SHA256,
                LOCK_SHA256,
                SCHEMA_EPOCH,
                "5" * 64,
                "6" * 64,
                "register-live-safety-release",
            ),
        )
        cur.execute(
            """
            SELECT phase, phase_version
            FROM reviewed_release_rollouts
            WHERE release_id=%s
            FOR UPDATE
            """,
            (RELEASE_ID,),
        )
        phase, phase_version = cur.fetchone()
        if (
            phase == "account_a_canary"
            and rollout_phase
            in (
                "account_b_rollout",
                "account_c_rollout",
                "account_d_rollout",
                "fleet_complete",
            )
        ):
            cur.execute(
                """
                UPDATE reviewed_release_rollouts
                SET phase='account_b_rollout',
                    phase_version=%s
                WHERE release_id=%s
                """,
                (int(phase_version) + 1, RELEASE_ID),
            )
            phase = "account_b_rollout"
            phase_version = int(phase_version) + 1
        if (
            phase == "account_b_rollout"
            and rollout_phase
            in (
                "account_c_rollout",
                "account_d_rollout",
                "fleet_complete",
            )
        ):
            cur.execute(
                """
                UPDATE reviewed_release_rollouts
                SET phase='account_c_rollout',
                    phase_version=%s
                WHERE release_id=%s
                """,
                (int(phase_version) + 1, RELEASE_ID),
            )
            phase = "account_c_rollout"
            phase_version = int(phase_version) + 1
        if (
            phase == "account_c_rollout"
            and rollout_phase in ("account_d_rollout", "fleet_complete")
        ):
            cur.execute(
                """
                UPDATE reviewed_release_rollouts
                SET phase='account_d_rollout',
                    phase_version=%s
                WHERE release_id=%s
                """,
                (int(phase_version) + 1, RELEASE_ID),
            )
            phase = "account_d_rollout"
            phase_version = int(phase_version) + 1
        if phase == "account_d_rollout" and rollout_phase == "fleet_complete":
            cur.execute(
                """
                UPDATE reviewed_release_rollouts
                SET phase='fleet_complete',
                    phase_version=%s
                WHERE release_id=%s
                """,
                (int(phase_version) + 1, RELEASE_ID),
            )
        if registration_mode:
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
                    reason,
                    evidence
                )
                VALUES (
                    %s, %s, 'registered', NULL, 'account_a_canary',
                    1, 'register-live-safety-release', 'reviewer',
                    'test stopped all-halted registration', %s
                )
                ON CONFLICT (idempotency_key) DO UPDATE SET
                    evidence=EXCLUDED.evidence
                """,
                (
                    str(uuid4()),
                    RELEASE_ID,
                    Json(
                        {
                            "registration_mode": registration_mode,
                            "bootstrap_all_halted": True,
                        }
                    ),
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
            ON CONFLICT (account_id, release_id) DO NOTHING
            """,
            (
                RELEASE_ID,
                IMAGE_DIGEST,
                CONFIG_SHA256,
                LOCK_SHA256,
                SCHEMA_EPOCH,
                [ACCOUNT_A, ACCOUNT_B, ACCOUNT_C, ACCOUNT_D],
            ),
        )
        cur.execute(
            """
            INSERT INTO live_canary_permits (
                permit_id,
                account_id,
                symbol,
                max_notional_usdt,
                max_cumulative_loss_usdt,
                max_open_count,
                consumed_open_count,
                expires_at,
                release_id,
                testnet_emergency_close_evidence_sha256,
                testnet_emergency_close_verified_at,
                status,
                issued_by,
                portfolio_baseline_sha256
            )
            VALUES (
                %s, %s, %s, %s, %s, 1, 0,
                now() + interval '10 minutes',
                %s, %s, %s, %s, 'reviewer', %s
            )
            """,
            (
                permit_id,
                permit_account_id,
                SYMBOL,
                max_notional,
                max_cumulative_loss,
                RELEASE_ID,
                TESTNET_EMERGENCY_CLOSE_EVIDENCE_SHA256,
                evidence_verified_at,
                permit_status,
                portfolio_baseline_sha256,
            ),
        )
    if seed_fleet_peers:
        phase_account_count = {
            "account_a_canary": 1,
            "account_b_rollout": 2,
            "account_c_rollout": 3,
            "account_d_rollout": 4,
            "fleet_complete": 4,
        }
        upgraded_count = phase_account_count[rollout_phase]
        account_nodes = (
            (ACCOUNT_A, NODE_A),
            (ACCOUNT_B, NODE_B),
            (ACCOUNT_C, NODE_C),
            (ACCOUNT_D, NODE_D),
        )
        for index, (account_id, node_id) in enumerate(
            account_nodes,
            start=1,
        ):
            with _connect(url) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM node_heartbeats
                    WHERE node_id=%s
                    """,
                    (node_id,),
                )
                heartbeat_exists = cur.fetchone() is not None
            if heartbeat_exists:
                continue
            if index <= upgraded_count:
                _seed_heartbeat(
                    url,
                    node_id=node_id,
                    account_id=account_id,
                    runtime_generation=(
                        f"runtime-generation-{account_id}"
                    ),
                    lease_fencing_token=40 + index,
                )
                continue
            old_release_id = f"approved-old-{account_id}"
            old_image_digest = "sha256:" + (account_id[-1] * 64)
            old_config_sha256 = account_id[-1] * 64
            old_lock_sha256 = str(index) * 64
            old_schema_epoch = f"approved-old-schema-{account_id}"
            with _connect(url) as conn, conn.cursor() as cur:
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
                    ON CONFLICT (account_id, release_id) DO NOTHING
                    """,
                    (
                        account_id,
                        old_release_id,
                        old_image_digest,
                        old_config_sha256,
                        old_lock_sha256,
                        old_schema_epoch,
                    ),
                )
            _seed_heartbeat(
                url,
                node_id=node_id,
                account_id=account_id,
                release_id=old_release_id,
                runtime_generation=f"runtime-generation-{account_id}",
                lease_fencing_token=40 + index,
            )
            with _connect(url) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE node_heartbeats
                    SET image_digest=%s,
                        config_sha256=%s,
                        dependency_lock_sha256=%s,
                        schema_epoch=%s,
                        last_seen_at=now()
                    WHERE node_id=%s
                    """,
                    (
                        old_image_digest,
                        old_config_sha256,
                        old_lock_sha256,
                        old_schema_epoch,
                        node_id,
                    ),
                )
    return permit_id


def _seed_execution_fill(
    url: str,
    *,
    account_id: str = ACCOUNT_A,
    node_id: str = NODE_A,
    client_order_id: str = "Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01",
    symbol: str = SYMBOL,
    side: str = "BUY",
    quantity: str = "0.1",
    event_id: str | None = None,
) -> None:
    if event_id is None:
        event_id = str(uuid4())
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO execution_events (
                execution_event_row_id,
                event_id,
                node_id,
                account_id,
                client_order_id,
                event_type,
                ts_event,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, 'OrderFilled', now(), %s)
            """,
            (
                str(uuid4()),
                event_id,
                node_id,
                account_id,
                client_order_id,
                Json(
                    {
                        "instrument_id": f"{symbol}-PERP.BINANCE",
                        "side": side,
                        "last_qty": quantity,
                    }
                ),
            ),
        )


def _seed_ownership_rebaseline(
    url: str,
    *,
    account_id: str = ACCOUNT_A,
    node_id: str = NODE_A,
    symbol: str = SYMBOL,
    baseline_quantity: str,
    exchange_quantity: str,
    manual_quantity: str,
    event_id: str | None = None,
) -> None:
    if event_id is None:
        event_id = f"ownership-rebaseline:{uuid4()}"
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO execution_events (
                execution_event_row_id,
                event_id,
                node_id,
                account_id,
                event_type,
                ts_event,
                payload
            )
            VALUES (%s, %s, %s, %s, 'OwnershipRebaseline', now(), %s)
            """,
            (
                str(uuid4()),
                event_id,
                node_id,
                account_id,
                Json(
                    {
                        "ownership_schema_version": (
                            "ownership-rebaseline/v1"
                        ),
                        "symbol": symbol,
                        "instrument_id": (
                            f"{symbol}-PERP.BINANCE"
                        ),
                        "baseline_quantity": baseline_quantity,
                        "exchange_quantity_at_baseline": (
                            exchange_quantity
                        ),
                        "manual_quantity_at_baseline": manual_quantity,
                        "reason": "test ownership adjudication",
                        "adjudicated_by": "test-user",
                        "request_id": str(uuid4()),
                        "cutoff_semantics": (
                            "fills strictly after marker "
                            "(ts_event,created_at,event_id)"
                        ),
                    }
                ),
            ),
        )


def _resume_body(
    permit_id: str | None,
    *,
    account_id: str = ACCOUNT_A,
    node_id: str = NODE_A,
    target_nodes: list[str] | None = None,
) -> dict:
    scope = {
        "account_id": account_id,
        "instruments": [SYMBOL],
        "release_id": RELEASE_ID,
    }
    if permit_id is not None:
        scope["canary_permit_id"] = permit_id
    return {
        "type": "RESUME",
        "reason": f"audited {account_id} canary",
        "confirm": True,
        "request_id": str(uuid4()),
        "target_nodes": target_nodes or [node_id],
        "scope": scope,
    }


def _seed_exchange_accepted_durable_entry_ladder(
    client: TestClient,
    migrated_db: str,
) -> tuple[str, list[dict]]:
    _seed_heartbeat(migrated_db, trading_state="ACTIVE")
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM live_canary_permits WHERE permit_id=%s",
            (permit_id,),
        )
    open_response = client.post(
        "/v1/operator/orders",
        headers=_risk_headers(f"durable-ladder-{uuid4()}"),
        json={
            "action": "open_position",
            "account_id": ACCOUNT_A,
            "symbol": SYMBOL,
            "side": "long",
            "entry": {
                "type": "zone",
                "price_min": 99,
                "price_max": 101,
            },
            "stop_loss": 95,
            "quantity": 0.12,
            "notional_usdt": 12,
            "reason": "durable ladder resume gate test",
            "client_ref": f"durable-ladder-{uuid4()}",
        },
    )
    assert open_response.status_code == 200
    payload = open_response.json()
    preview = payload["execution_preview"]
    assert preview["type"] == "zone_ladder"
    intent_id = str(UUID(payload["intent_id"]))
    intent_uuid = UUID(intent_id)
    orders = []
    for tranche in preview["tranches"]:
        sequence = int(tranche["seq"])
        client_order_id = f"B{intent_uuid.hex}{sequence:02d}"
        order = {
            "symbol": SYMBOL,
            "client_order_id": client_order_id,
            "order_type": "LIMIT",
            "order_kind": "regular",
            "side": "BUY",
            "quantity": str(tranche["quantity"]),
            "price": str(tranche["price"]),
            "time_in_force": "GTC",
            "reduce_only": False,
        }
        orders.append(order)
        with _connect(migrated_db) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders_projection (
                    order_projection_id,
                    account_id,
                    instrument_id,
                    intent_id,
                    client_order_id,
                    status,
                    side,
                    order_type,
                    quantity,
                    price,
                    reduce_only,
                    updated_at,
                    payload
                )
                VALUES (
                    %s, %s, %s, %s, %s, 'working', 'long',
                    'LIMIT', %s, %s, false, now(), %s
                )
                """,
                (
                    str(uuid4()),
                    ACCOUNT_A,
                    "BTCUSDT-PERP.BINANCE",
                    intent_id,
                    client_order_id,
                    order["quantity"],
                    order["price"],
                    Json(
                        {
                            "side": "BUY",
                            "reduce_only": False,
                        }
                    ),
                ),
            )
    return intent_id, orders


@pytest.mark.parametrize("command_type", ("HALT", "REDUCE", "RESUME"))
def test_state_commands_require_scope_account_id(
    client: TestClient,
    command_type: str,
) -> None:
    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json={
            "type": command_type,
            "reason": "test",
            "confirm": True,
            "request_id": str(uuid4()),
            "target_nodes": [NODE_A],
            "scope": {},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "scope.account_id is required"


def test_command_targets_must_match_fresh_node_account(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, node_id=NODE_B, account_id=ACCOUNT_B)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json={
            "type": "HALT",
            "reason": "test",
            "confirm": True,
            "request_id": str(uuid4()),
            "target_nodes": [NODE_B],
            "scope": {"account_id": ACCOUNT_A},
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "target_nodes do not match fresh account binding"


def test_refresh_evidence_command_allows_stale_account_binding(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, age_seconds=120)
    request_id = str(uuid4())

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(request_id),
        json={
            "type": "REFRESH_EVIDENCE",
            "reason": "refresh stale live evidence",
            "confirm": True,
            "request_id": request_id,
            "target_nodes": [NODE_A],
            "scope": {
                "account_id": ACCOUNT_A,
                "release_id": RELEASE_ID,
            },
        },
    )

    assert response.status_code == 200
    command_id = response.json()["command_id"]
    commands = client.get(
        f"/v1/nodes/{NODE_A}/commands",
        params={"account_id": ACCOUNT_A},
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
    )
    assert commands.status_code == 200
    command = next(
        item
        for item in commands.json()["commands"]
        if item["command_id"] == command_id
    )
    assert command["type"] == "refresh_evidence"
    assert command["args"]["account_id"] == ACCOUNT_A

    status = client.get(
        f"/v1/commands/{command_id}",
        headers=_risk_headers(str(uuid4())),
    )
    assert status.status_code == 200
    assert status.json()["status"] == "pending"

    ack = client.post(
        f"/v1/nodes/{NODE_A}/commands/{command_id}/ack",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "status": "completed",
        },
    )
    assert ack.status_code == 200

    completed = client.get(
        f"/v1/commands/{command_id}",
        headers=_risk_headers(str(uuid4())),
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["acks"] == [
        {
            "node_id": NODE_A,
            "status": "completed",
        }
    ]

    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT state->>'mode'
            FROM risk_state
            WHERE account_id=%s
            """,
            (ACCOUNT_A,),
        )
        assert cur.fetchall() == []
        cur.execute(
            "DELETE FROM operator_commands WHERE command_id=%s",
            (command_id,),
        )


def test_account_a_state_command_requires_single_target(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, node_id=NODE_A)
    _seed_heartbeat(migrated_db, node_id=NODE_A_2)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json={
            "type": "HALT",
            "reason": "test",
            "confirm": True,
            "request_id": str(uuid4()),
            "target_nodes": [NODE_A, NODE_A_2],
            "scope": {"account_id": ACCOUNT_A},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "account-a commands require exactly one target node"


def test_node_poll_does_not_receive_cross_account_stored_command(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(
        migrated_db,
        node_id=NODE_B,
        account_id=ACCOUNT_B,
    )
    command_id = str(uuid4())
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO operator_commands (
                command_id, command_type, scope, status, requested_by,
                reason, idempotency_key
            )
            VALUES (%s, 'HALT', %s, 'pending', 'risk_admin', 'test', %s)
            """,
            (command_id, Json({"account_id": ACCOUNT_A}), str(uuid4())),
        )
        cur.execute(
            """
            INSERT INTO command_node_acks (command_id, node_id, status)
            VALUES (%s, %s, 'pending')
            """,
            (command_id, NODE_B),
        )

    response = client.get(
        f"/v1/nodes/{NODE_B}/commands",
        params={"account_id": ACCOUNT_B},
        headers=_node_headers(NODE_B, ACCOUNT_B, NODE_B_TOKEN),
    )

    assert response.status_code == 200
    assert response.json() == {"commands": []}


def test_heartbeat_persists_release_and_exchange_evidence(
    client: TestClient,
    migrated_db: str,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    response = client.post(
        f"/v1/nodes/{NODE_A}/heartbeat",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "ts": now.isoformat(),
            "trading_state": "HALTED",
            "readiness": True,
            "projection_lag_ms": 0,
            "reconciliation_state": "healthy",
            "release_id": RELEASE_ID,
            "image_digest": IMAGE_DIGEST,
            "config_sha256": CONFIG_SHA256,
            "dependency_lock_sha256": LOCK_SHA256,
            "schema_epoch": SCHEMA_EPOCH,
            "positions": [],
            "regular_orders": [{"symbol": SYMBOL, "client_order_id": "order-1"}],
            "algo_orders": [],
            "positions_snapshot_at": now.isoformat(),
            "regular_orders_snapshot_at": now.isoformat(),
            "algo_orders_snapshot_at": now.isoformat(),
            "reconciliation_completed_at": now.isoformat(),
            "redis_fencing_epoch": REDIS_FENCING_EPOCH,
            "runtime_generation": RUNTIME_GENERATION,
            "lease_fencing_token": LEASE_FENCING_TOKEN,
            "heartbeat_sequence": HEARTBEAT_SEQUENCE,
        },
    )

    assert response.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT release_id, image_digest, config_sha256,
                   dependency_lock_sha256, schema_epoch,
                   positions, regular_orders, algo_orders,
                   positions_snapshot_at, regular_orders_snapshot_at,
                   algo_orders_snapshot_at, reconciliation_completed_at,
                   redis_fencing_epoch, runtime_generation,
                   lease_fencing_token, heartbeat_sequence
            FROM node_heartbeats
            WHERE node_id=%s
            """,
            (NODE_A,),
        )
        row = cur.fetchone()
    assert row[0:5] == (
        RELEASE_ID,
        IMAGE_DIGEST,
        CONFIG_SHA256,
        LOCK_SHA256,
        SCHEMA_EPOCH,
    )
    assert row[5] == []
    assert row[6] == [{"symbol": SYMBOL, "client_order_id": "order-1"}]
    assert row[7] == []
    assert all(value == now for value in row[8:12])
    assert str(row[12]) == REDIS_FENCING_EPOCH
    assert row[13:16] == (
        RUNTIME_GENERATION,
        LEASE_FENCING_TOKEN,
        HEARTBEAT_SEQUENCE,
    )


@pytest.mark.parametrize(
    ("rollout_phase", "expected_mode", "expected_version"),
    (
        ("account_a_canary", "normal", 1),
        ("fleet_complete", "normal", 5),
    ),
)
def test_heartbeat_receipt_includes_trusted_live_open_gate(
    client: TestClient,
    migrated_db: str,
    rollout_phase: str,
    expected_mode: str,
    expected_version: int,
) -> None:
    _seed_reviewed_release_and_permit(
        migrated_db,
        rollout_phase=rollout_phase,
        seed_fleet_peers=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    response = client.post(
        f"/v1/nodes/{NODE_A}/heartbeat",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "ts": now.isoformat(),
            "trading_state": "HALTED",
            "readiness": True,
            "projection_lag_ms": 0,
            "reconciliation_state": "healthy",
            "release_id": RELEASE_ID,
            "image_digest": IMAGE_DIGEST,
            "config_sha256": CONFIG_SHA256,
            "dependency_lock_sha256": LOCK_SHA256,
            "schema_epoch": SCHEMA_EPOCH,
            "positions": [],
            "regular_orders": [],
            "algo_orders": [],
            "positions_snapshot_at": now.isoformat(),
            "regular_orders_snapshot_at": now.isoformat(),
            "algo_orders_snapshot_at": now.isoformat(),
            "reconciliation_completed_at": now.isoformat(),
            "redis_fencing_epoch": REDIS_FENCING_EPOCH,
            "runtime_generation": RUNTIME_GENERATION,
            "lease_fencing_token": LEASE_FENCING_TOKEN,
            "heartbeat_sequence": HEARTBEAT_SEQUENCE + 1,
        },
    )

    assert response.status_code == 200
    release_gate = response.json()["release_gate"]
    assert release_gate["status"] == "pass"
    assert release_gate["release_id"] == RELEASE_ID
    assert release_gate["rollout_phase"] == rollout_phase
    assert release_gate["live_open_mode"] == expected_mode
    assert release_gate["phase_version"] == expected_version


def test_heartbeat_old_writer_cannot_overwrite_new_runtime_snapshot(
    client: TestClient,
    migrated_db: str,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def heartbeat(
        *,
        runtime_generation: str,
        lease_fencing_token: int,
        heartbeat_sequence: int,
        symbol: str,
    ):
        return client.post(
            f"/v1/nodes/{NODE_A}/heartbeat",
            headers=_node_headers(
                NODE_A,
                ACCOUNT_A,
                NODE_A_TOKEN,
                runtime_generation=runtime_generation,
                lease_fencing_token=lease_fencing_token,
            ),
            json={
                "account_id": ACCOUNT_A,
                "ts": now.isoformat(),
                "trading_state": "HALTED",
                "readiness": True,
                "projection_lag_ms": 0,
                "reconciliation_state": "healthy",
                "release_id": RELEASE_ID,
                "image_digest": IMAGE_DIGEST,
                "config_sha256": CONFIG_SHA256,
                "dependency_lock_sha256": LOCK_SHA256,
                "schema_epoch": SCHEMA_EPOCH,
                "positions": [],
                "regular_orders": [{"symbol": symbol, "client_order_id": "order-1"}],
                "algo_orders": [],
                "positions_snapshot_at": now.isoformat(),
                "regular_orders_snapshot_at": now.isoformat(),
                "algo_orders_snapshot_at": now.isoformat(),
                "reconciliation_completed_at": now.isoformat(),
                "redis_fencing_epoch": REDIS_FENCING_EPOCH,
                "runtime_generation": runtime_generation,
                "lease_fencing_token": lease_fencing_token,
                "heartbeat_sequence": heartbeat_sequence,
            },
        )

    first = heartbeat(
        runtime_generation="runtime-old",
        lease_fencing_token=40,
        heartbeat_sequence=100,
        symbol="BTCUSDT",
    )
    same_token_runtime = heartbeat(
        runtime_generation="runtime-impostor",
        lease_fencing_token=40,
        heartbeat_sequence=1,
        symbol="SOLUSDT",
    )
    takeover = heartbeat(
        runtime_generation="runtime-new",
        lease_fencing_token=41,
        heartbeat_sequence=1,
        symbol="ETHUSDT",
    )
    stale = heartbeat(
        runtime_generation="runtime-old",
        lease_fencing_token=40,
        heartbeat_sequence=101,
        symbol="XRPUSDT",
    )

    assert first.status_code == 200
    assert same_token_runtime.status_code == 409
    assert takeover.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["detail"] == "stale heartbeat writer"
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT runtime_generation,
                   lease_fencing_token,
                   heartbeat_sequence,
                   regular_orders
            FROM node_heartbeats
            WHERE node_id=%s
            """,
            (NODE_A,),
        )
        row = cur.fetchone()
    assert row == (
        "runtime-new",
        41,
        1,
        [{"symbol": "ETHUSDT", "client_order_id": "order-1"}],
    )


def test_new_redis_epoch_can_take_over_with_restarted_fencing_counter(
    client: TestClient,
    migrated_db: str,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def heartbeat(
        *,
        redis_fencing_epoch: str,
        runtime_generation: str,
        lease_fencing_token: int,
        heartbeat_sequence: int,
    ):
        return client.post(
            f"/v1/nodes/{NODE_A}/heartbeat",
            headers=_node_headers(
                NODE_A,
                ACCOUNT_A,
                NODE_A_TOKEN,
                redis_fencing_epoch=redis_fencing_epoch,
                runtime_generation=runtime_generation,
                lease_fencing_token=lease_fencing_token,
            ),
            json={
                "account_id": ACCOUNT_A,
                "ts": now.isoformat(),
                "trading_state": "HALTED",
                "readiness": True,
                "projection_lag_ms": 0,
                "reconciliation_state": "healthy",
                "release_id": RELEASE_ID,
                "image_digest": IMAGE_DIGEST,
                "config_sha256": CONFIG_SHA256,
                "dependency_lock_sha256": LOCK_SHA256,
                "schema_epoch": SCHEMA_EPOCH,
                "positions": [],
                "regular_orders": [],
                "algo_orders": [],
                "positions_snapshot_at": now.isoformat(),
                "regular_orders_snapshot_at": now.isoformat(),
                "algo_orders_snapshot_at": now.isoformat(),
                "reconciliation_completed_at": now.isoformat(),
                "redis_fencing_epoch": redis_fencing_epoch,
                "runtime_generation": runtime_generation,
                "lease_fencing_token": lease_fencing_token,
                "heartbeat_sequence": heartbeat_sequence,
            },
        )

    old_writer = heartbeat(
        redis_fencing_epoch=REDIS_FENCING_EPOCH,
        runtime_generation="runtime-old-epoch",
        lease_fencing_token=40,
        heartbeat_sequence=100,
    )

    _activate_redis_epoch(
        migrated_db,
        REPLACEMENT_REDIS_FENCING_EPOCH,
    )
    replacement = heartbeat(
        redis_fencing_epoch=REPLACEMENT_REDIS_FENCING_EPOCH,
        runtime_generation="runtime-new-epoch",
        lease_fencing_token=1,
        heartbeat_sequence=1,
    )
    stale_old_epoch = heartbeat(
        redis_fencing_epoch=REDIS_FENCING_EPOCH,
        runtime_generation="runtime-old-epoch",
        lease_fencing_token=999,
        heartbeat_sequence=101,
    )

    assert old_writer.status_code == 200
    assert replacement.status_code == 200
    assert stale_old_epoch.status_code == 409
    assert stale_old_epoch.json()["detail"] == "redis fencing epoch mismatch"


def test_live_heartbeat_fails_closed_without_active_redis_epoch(
    client: TestClient,
    migrated_db: str,
) -> None:
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE redis_fencing_epochs
            SET status='retired',
                retired_at=now()
            WHERE domain='trader-v3'
              AND status='active'
            """
        )
    now = datetime.now(timezone.utc).replace(microsecond=0)

    response = client.post(
        f"/v1/nodes/{NODE_A}/heartbeat",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "ts": now.isoformat(),
            "trading_state": "HALTED",
            "readiness": True,
            "projection_lag_ms": 0,
            "reconciliation_state": "healthy",
            "release_id": RELEASE_ID,
            "image_digest": IMAGE_DIGEST,
            "config_sha256": CONFIG_SHA256,
            "dependency_lock_sha256": LOCK_SHA256,
            "schema_epoch": SCHEMA_EPOCH,
            "positions": [],
            "regular_orders": [],
            "algo_orders": [],
            "positions_snapshot_at": now.isoformat(),
            "regular_orders_snapshot_at": now.isoformat(),
            "algo_orders_snapshot_at": now.isoformat(),
            "reconciliation_completed_at": now.isoformat(),
            "redis_fencing_epoch": REDIS_FENCING_EPOCH,
            "runtime_generation": RUNTIME_GENERATION,
            "lease_fencing_token": LEASE_FENCING_TOKEN,
            "heartbeat_sequence": HEARTBEAT_SEQUENCE,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "active redis fencing epoch is unavailable"
    )


def test_live_heartbeat_requires_writer_identity(
    client: TestClient,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    response = client.post(
        f"/v1/nodes/{NODE_A}/heartbeat",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "ts": now.isoformat(),
            "trading_state": "HALTED",
            "readiness": True,
            "projection_lag_ms": 0,
            "reconciliation_state": "healthy",
            "release_id": RELEASE_ID,
            "image_digest": IMAGE_DIGEST,
            "config_sha256": CONFIG_SHA256,
            "dependency_lock_sha256": LOCK_SHA256,
            "schema_epoch": SCHEMA_EPOCH,
            "positions": [],
            "regular_orders": [],
            "algo_orders": [],
            "positions_snapshot_at": now.isoformat(),
            "regular_orders_snapshot_at": now.isoformat(),
            "algo_orders_snapshot_at": now.isoformat(),
            "reconciliation_completed_at": now.isoformat(),
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "live heartbeat requires redis_fencing_epoch, "
        "runtime_generation, lease_fencing_token, "
        "and heartbeat_sequence"
    )


def test_legacy_heartbeat_writer_identity_can_be_fully_absent() -> None:
    assert read_api._heartbeat_writer_identity({}, required=False) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    (
        "order",
        "expected",
    ),
    (
        (
            {
                "client_order_id": "B" + ("a" * 32) + "01",
                "order_type": "LIMIT",
                "reduce_only": True,
                "order_kind": "regular",
            },
            False,
        ),
        (
            {
                "clientOrderId": "B" + ("b" * 32) + "02",
                "type": "STOP_MARKET",
                "order_kind": "algo",
                "reduceOnly": True,
            },
            True,
        ),
        (
            {
                "clientAlgoId": "B" + ("c" * 32) + "03",
                "type": "TAKE_PROFIT_MARKET",
                "order_kind": "algo",
                "reduceOnly": False,
            },
            False,
        ),
        (
            {
                "client_order_id": "B" + ("d" * 32) + "04",
                "order_type": "TAKE_PROFIT_MARKET",
                "order_kind": "algo",
            },
            False,
        ),
        (
            {
                "client_order_id": "manual-order",
                "order_type": "STOP_MARKET",
                "order_kind": "algo",
                "reduce_only": True,
            },
            False,
        ),
    ),
)
def test_robot_order_resume_exemption_is_fail_closed(
    order: dict,
    expected: bool,
) -> None:
    assert read_api._robot_order_is_resume_exempt(order) is expected


def test_resume_projection_reads_use_plain_selects() -> None:
    terminal_source = inspect.getsource(
        read_api._validate_owned_orders_terminal
    )
    footprint_source = inspect.getsource(
        read_api._robot_owned_symbol_footprint
    )
    margin_source = inspect.getsource(
        read_api._validate_margin_ratio_guard
    )
    resume_source = inspect.getsource(read_api._validate_and_arm_resume)

    assert "orders_projection" in terminal_source
    assert "FOR SHARE" not in terminal_source
    assert "load_robot_owned_balance" in footprint_source
    assert "FOR SHARE" not in footprint_source
    assert "accounts_projection" in margin_source
    assert "FOR SHARE" not in margin_source
    assert "_validate_owned_orders_terminal" in resume_source
    assert "_validate_margin_ratio_guard" in resume_source


def test_heartbeat_concurrent_sequences_keep_highest_snapshot(
    client: TestClient,
    migrated_db: str,
) -> None:
    del client
    now = datetime.now(timezone.utc).replace(microsecond=0)
    barrier = threading.Barrier(2)

    def submit(sequence: int):
        barrier.wait()
        with TestClient(read_api.app) as worker_client:
            return worker_client.post(
                f"/v1/nodes/{NODE_A}/heartbeat",
                headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
                json={
                    "account_id": ACCOUNT_A,
                    "ts": now.isoformat(),
                    "trading_state": "HALTED",
                    "readiness": True,
                    "projection_lag_ms": 0,
                    "reconciliation_state": "healthy",
                    "release_id": RELEASE_ID,
                    "image_digest": IMAGE_DIGEST,
                    "config_sha256": CONFIG_SHA256,
                    "dependency_lock_sha256": LOCK_SHA256,
                    "schema_epoch": SCHEMA_EPOCH,
                    "positions": [],
                    "regular_orders": [
                        {
                            "symbol": "BTCUSDT",
                            "client_order_id": f"order-{sequence}",
                        }
                    ],
                    "algo_orders": [],
                    "positions_snapshot_at": now.isoformat(),
                    "regular_orders_snapshot_at": now.isoformat(),
                    "algo_orders_snapshot_at": now.isoformat(),
                    "reconciliation_completed_at": now.isoformat(),
                    "redis_fencing_epoch": REDIS_FENCING_EPOCH,
                    "runtime_generation": RUNTIME_GENERATION,
                    "lease_fencing_token": LEASE_FENCING_TOKEN,
                    "heartbeat_sequence": sequence,
                },
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, (1, 2)))

    assert all(response.status_code in (200, 409) for response in responses)
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT heartbeat_sequence, regular_orders
            FROM node_heartbeats
            WHERE node_id=%s
            """,
            (NODE_A,),
        )
        sequence, regular_orders = cur.fetchone()
    assert sequence == 2
    assert regular_orders == [
        {"symbol": "BTCUSDT", "client_order_id": "order-2"}
    ]


def test_resume_arms_reviewed_flat_fresh_canary(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, armed_at FROM live_canary_permits WHERE permit_id=%s",
            (permit_id,),
        )
        status, armed_at = cur.fetchone()
    assert status == "armed"
    assert armed_at is not None
    command_id = response.json()["command_id"]
    commands = client.get(
        f"/v1/nodes/{NODE_A}/commands",
        params={"account_id": ACCOUNT_A},
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
    )
    assert commands.status_code == 200
    command = next(
        item
        for item in commands.json()["commands"]
        if item["command_id"] == command_id
    )
    permit_evidence = command["args"]["canary_permit"]
    assert command["args"]["live_open_gate"] == {
        "mode": "normal",
        "release_id": RELEASE_ID,
        "rollout_phase": "account_a_canary",
        "phase_version": 1,
    }
    assert permit_evidence["permit_id"] == permit_id
    assert permit_evidence["release_id"] == RELEASE_ID
    assert permit_evidence["max_notional_usdt"] == "12"
    assert permit_evidence["max_cumulative_loss_usdt"] == "1.49"
    assert (
        permit_evidence["testnet_emergency_close_evidence_sha256"]
        == TESTNET_EMERGENCY_CLOSE_EVIDENCE_SHA256
    )
    assert datetime.fromisoformat(
        permit_evidence["testnet_emergency_close_verified_at"]
    ).tzinfo is not None
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload->'payload'->'scope'->'canary_permit'
            FROM audit_events
            WHERE aggregate_id=%s
              AND event_type='operator_command'
            """,
            (command_id,),
        )
        audit_permit_evidence = cur.fetchone()[0]
    assert audit_permit_evidence == permit_evidence


def test_resume_rejects_active_maintenance_fence(
    client: TestClient,
    migrated_db: str,
) -> None:
    for node_id, account_id in (
        (NODE_A, ACCOUNT_A),
        (NODE_B, ACCOUNT_B),
        (NODE_C, ACCOUNT_C),
        (NODE_D, ACCOUNT_D),
    ):
        _seed_heartbeat(
            migrated_db,
            node_id=node_id,
            account_id=account_id,
        )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT fence_id
            FROM acquire_control_plane_maintenance_fence(
                %s, 'resume-gate-test', 'pytest', %s, 30, 10
            )
            """,
            (str(uuid4()), "a" * 64),
        )
        fence_id = cur.fetchone()[0]

    try:
        response = client.post(
            "/v1/commands",
            headers=_risk_headers(str(uuid4())),
            json=_resume_body(permit_id),
        )

        assert response.status_code == 409
        assert response.json()["detail"] == (
            "RESUME is blocked by active maintenance fence"
        )
    finally:
        with _connect(migrated_db) as conn, conn.cursor() as cur:
            cur.execute(
                """
                ALTER TABLE control_plane_maintenance_fence_events
                DISABLE TRIGGER
                    trg_control_plane_maintenance_fence_events_append_only
                """
            )
            cur.execute(
                """
                DELETE FROM control_plane_maintenance_fence_events
                WHERE fence_id=%s
                """,
                (fence_id,),
            )
            cur.execute(
                """
                ALTER TABLE control_plane_maintenance_fence_events
                ENABLE TRIGGER
                    trg_control_plane_maintenance_fence_events_append_only
                """
            )
            cur.execute(
                """
                DELETE FROM control_plane_maintenance_fences
                WHERE fence_id=%s
                """,
                (fence_id,),
            )


def test_account_a_canary_rejects_wrong_rollout_phase(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        rollout_phase="account_b_rollout",
    )

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "account-a canary requires rollout phase account_a_canary"
    )


def test_resume_allows_fleet_complete_peer_identity_drift(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    _seed_reviewed_release_and_permit(
        migrated_db,
        rollout_phase="fleet_complete",
        seed_fleet_peers=True,
    )
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE node_heartbeats
            SET image_digest=%s,
                last_seen_at=now()
            WHERE node_id=%s
            """,
            ("sha256:" + ("9" * 64), NODE_C),
        )

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(
            None,
            account_id=ACCOUNT_B,
            node_id=NODE_B,
        ),
    )

    assert response.status_code == 200


def test_plain_account_b_resume_accepts_active_rollout_phase(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    _seed_heartbeat(
        migrated_db,
        node_id=NODE_B,
        account_id=ACCOUNT_B,
        runtime_generation="runtime-generation-b",
        lease_fencing_token=42,
    )
    _seed_reviewed_release_and_permit(
        migrated_db,
        rollout_phase="account_b_rollout",
    )

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(
            None,
            account_id=ACCOUNT_B,
            node_id=NODE_B,
        ),
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("account_id", "node_id", "rollout_phase"),
    (
        (ACCOUNT_A, NODE_A, "account_a_canary"),
        (ACCOUNT_B, NODE_B, "account_b_rollout"),
        (ACCOUNT_C, NODE_C, "account_c_rollout"),
        (ACCOUNT_D, NODE_D, "account_d_rollout"),
    ),
)
def test_each_account_can_arm_its_own_rollout_canary(
    client: TestClient,
    migrated_db: str,
    account_id: str,
    node_id: str,
    rollout_phase: str,
) -> None:
    account_index = (ACCOUNT_A, ACCOUNT_B, ACCOUNT_C, ACCOUNT_D).index(
        account_id
    )
    account_nodes = (
        (ACCOUNT_A, NODE_A),
        (ACCOUNT_B, NODE_B),
        (ACCOUNT_C, NODE_C),
        (ACCOUNT_D, NODE_D),
    )
    for index, (seed_account_id, seed_node_id) in enumerate(
        account_nodes[: account_index + 1],
        start=1,
    ):
        _seed_heartbeat(
            migrated_db,
            account_id=seed_account_id,
            node_id=seed_node_id,
            runtime_generation=f"runtime-generation-{seed_account_id}",
            lease_fencing_token=40 + index,
        )
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_account_id=account_id,
        rollout_phase=rollout_phase,
    )

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(
            permit_id,
            account_id=account_id,
            node_id=node_id,
        ),
    )

    assert response.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_id, status, armed_node_id
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        assert cur.fetchone() == (account_id, "armed", node_id)


def test_account_a_canary_resume_allows_missing_non_target_heartbeat(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM node_heartbeats WHERE node_id=%s",
            (NODE_D,),
        )

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, armed_node_id
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        assert cur.fetchone() == ("armed", NODE_A)


def test_account_a_canary_resume_allows_active_non_target_account(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE node_heartbeats
            SET status='ACTIVE',
                last_seen_at=now()
            WHERE node_id=%s
            """,
            (NODE_C,),
        )

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, armed_node_id
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        assert cur.fetchone() == ("armed", NODE_A)


@pytest.mark.parametrize(
    ("permit_change", "expected_detail"),
    (
        (
            "missing_evidence",
            "canary testnet emergency close evidence is invalid",
        ),
        (
            "stale_evidence",
            "canary testnet emergency close evidence is stale",
        ),
        ("loss_over_limit", "canary permit limits are invalid"),
        ("permit_expired", "canary permit has expired"),
    ),
)
def test_resume_rejects_invalid_canary_machine_gate(
    client: TestClient,
    migrated_db: str,
    permit_change: str,
    expected_detail: str,
) -> None:
    _seed_heartbeat(migrated_db)
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        if permit_change == "missing_evidence":
            cur.execute(
                """
                ALTER TABLE live_canary_permits
                DROP CONSTRAINT ck_live_canary_permits_emergency_close_evidence
                """
            )
            cur.execute(
                """
                ALTER TABLE live_canary_permits
                ALTER COLUMN testnet_emergency_close_evidence_sha256
                DROP NOT NULL
                """
            )
            cur.execute(
                """
                UPDATE live_canary_permits
                SET testnet_emergency_close_evidence_sha256=NULL
                WHERE permit_id=%s
                """,
                (permit_id,),
            )
        if permit_change == "stale_evidence":
            cur.execute(
                """
                UPDATE live_canary_permits
                SET testnet_emergency_close_verified_at=now() - interval '2 days'
                WHERE permit_id=%s
                """,
                (permit_id,),
            )
        if permit_change == "loss_over_limit":
            cur.execute(
                """
                ALTER TABLE live_canary_permits
                DROP CONSTRAINT ck_live_canary_permits_cumulative_loss
                """
            )
            cur.execute(
                """
                UPDATE live_canary_permits
                SET max_cumulative_loss_usdt=1.5
                WHERE permit_id=%s
                """,
                (permit_id,),
            )
        if permit_change == "permit_expired":
            cur.execute(
                """
                UPDATE live_canary_permits
                SET expires_at=now() - interval '1 second'
                WHERE permit_id=%s
                """,
                (permit_id,),
            )

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == expected_detail


def test_resume_allows_existing_non_target_risk_and_binds_portfolio_baseline(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(
        migrated_db,
        positions=[{"symbol": "XAUUSDT", "quantity": "1", "mark_price": "2400"}],
        regular_orders=[{"symbol": "ETHUSDT", "client_order_id": "eth-order"}],
        algo_orders=[{"symbol": "SOLUSDT", "client_order_id": "sol-stop"}],
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, portfolio_baseline_sha256
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        status, baseline = cur.fetchone()
    assert status == "armed"
    assert len(baseline) == 64


def test_resume_ignores_manual_orders_and_positions(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(
        migrated_db,
        positions=[{"symbol": SYMBOL, "quantity": "0.25"}],
        regular_orders=[
            {"symbol": SYMBOL, "client_order_id": "manual-target-order"}
        ],
        algo_orders=[
            {"symbol": SYMBOL, "client_order_id": "manual-target-stop"}
        ],
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 200


def test_resume_ignores_manual_fills(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    _seed_execution_fill(
        migrated_db,
        client_order_id="manual-sol-fill",
        quantity="0.25",
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 200


def test_resume_rejects_robot_owned_target_position_footprint(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    _seed_execution_fill(
        migrated_db,
        client_order_id="Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01",
        side="BUY",
        quantity="0.25",
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "robot-owned target symbol position is not flat"
    )


def test_resume_allows_robot_owned_target_position_round_trip_flat(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    _seed_execution_fill(
        migrated_db,
        client_order_id="Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01",
        side="BUY",
        quantity="0.25",
        event_id="robot-open-fill",
    )
    _seed_execution_fill(
        migrated_db,
        client_order_id="Bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb02",
        side="SELL",
        quantity="0.25",
        event_id="robot-close-fill",
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 200


def test_resume_uses_latest_ownership_rebaseline(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    _seed_execution_fill(
        migrated_db,
        client_order_id="Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01",
        side="BUY",
        quantity="0.25",
    )
    _seed_ownership_rebaseline(
        migrated_db,
        baseline_quantity="0",
        exchange_quantity="1.5",
        manual_quantity="1.5",
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        footprint = read_api._robot_owned_symbol_footprint(
            cur,
            account_id=ACCOUNT_A,
            symbol=SYMBOL,
        )
    assert footprint == 0


def test_ownership_rebaseline_counts_later_robot_fills(
    migrated_db: str,
) -> None:
    _seed_execution_fill(
        migrated_db,
        client_order_id="Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01",
        side="BUY",
        quantity="0.25",
    )
    _seed_ownership_rebaseline(
        migrated_db,
        baseline_quantity="0",
        exchange_quantity="1.5",
        manual_quantity="1.5",
    )
    _seed_execution_fill(
        migrated_db,
        client_order_id="Bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb02",
        side="SELL",
        quantity="0.1",
    )

    with _connect(migrated_db) as conn, conn.cursor() as cur:
        footprint = read_api._robot_owned_symbol_footprint(
            cur,
            account_id=ACCOUNT_A,
            symbol=SYMBOL,
        )

    assert footprint == Decimal("-0.1")


@pytest.mark.parametrize("source", ("projection", "heartbeat"))
def test_resume_rejects_robot_owned_non_terminal_orders(
    client: TestClient,
    migrated_db: str,
    source: str,
) -> None:
    robot_client_order_id = "B" + ("a" * 32) + "01"
    heartbeat_args = {}
    if source == "heartbeat":
        heartbeat_args["regular_orders"] = [
            {
                "symbol": SYMBOL,
                "client_order_id": robot_client_order_id,
                "order_type": "LIMIT",
            }
        ]
    _seed_heartbeat(migrated_db, **heartbeat_args)
    if source == "projection":
        with _connect(migrated_db) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders_projection (
                    order_projection_id, account_id, instrument_id,
                    client_order_id, status, side, order_type, quantity,
                    updated_at, payload
                )
                VALUES (
                    %s, %s, %s, %s, 'working', 'long', 'LIMIT', 1,
                    now(), %s
                )
                """,
                (
                    str(uuid4()),
                    ACCOUNT_A,
                    "BTCUSDT-PERP.BINANCE",
                    robot_client_order_id,
                    Json({}),
                ),
            )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "robot-owned orders are not terminal"
    )


@pytest.mark.parametrize("source", ("projection", "heartbeat"))
def test_resume_allows_robot_owned_reduce_only_protection_orders(
    client: TestClient,
    migrated_db: str,
    source: str,
) -> None:
    robot_client_order_id = "B" + ("b" * 32) + "02"
    heartbeat_args = {}
    if source == "heartbeat":
        heartbeat_args["algo_orders"] = [
            {
                "symbol": SYMBOL,
                "clientOrderId": robot_client_order_id,
                "type": "STOP_MARKET",
                "order_kind": "algo",
                "reduceOnly": True,
            }
        ]
    _seed_heartbeat(migrated_db, **heartbeat_args)
    if source == "projection":
        with _connect(migrated_db) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders_projection (
                    order_projection_id, account_id, instrument_id,
                    client_order_id, status, side, order_type, quantity,
                    updated_at, payload
                )
                VALUES (
                    %s, %s, %s, %s, 'working', 'short',
                    'STOP_MARKET', 1, now(), %s
                )
                """,
                (
                    str(uuid4()),
                    ACCOUNT_A,
                    "BTCUSDT-PERP.BINANCE",
                    robot_client_order_id,
                    Json(
                        {
                            "order_kind": "algo",
                            "reduce_only": True,
                        }
                    ),
                ),
            )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 200


def test_resume_allows_exchange_accepted_unexpired_durable_entry_ladder(
    client: TestClient,
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_binance_mark_price",
        lambda _symbol: 102,
    )
    intent_id, orders = _seed_exchange_accepted_durable_entry_ladder(
        client,
        migrated_db,
    )
    _seed_heartbeat(migrated_db, regular_orders=orders)
    _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(None),
    )

    assert response.status_code == 200
    command_id = response.json()["command_id"]
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT scope FROM operator_commands WHERE command_id=%s",
            (command_id,),
        )
        scope = cur.fetchone()[0]
    preserved = scope["durable_entry_order_exemptions"]
    assert [item["client_order_id"] for item in preserved] == [
        order["client_order_id"] for order in orders
    ]
    assert {item["intent_id"] for item in preserved} == {intent_id}
    assert [item["price"] for item in preserved] == [
        order["price"] for order in orders
    ]
    assert [item["quantity"] for item in preserved] == [
        order["quantity"] for order in orders
    ]


def test_resume_allows_exchange_accepted_expired_durable_entry_ladder(
    client: TestClient,
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_binance_mark_price",
        lambda _symbol: 102,
    )
    intent_id, orders = _seed_exchange_accepted_durable_entry_ladder(
        client,
        migrated_db,
    )
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trade_intents
            SET valid_until=now() - interval '1 second'
            WHERE intent_id=%s
            """,
            (intent_id,),
        )
    _seed_heartbeat(migrated_db, regular_orders=orders)
    _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(None),
    )

    assert response.status_code == 200
    command_id = response.json()["command_id"]
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT scope FROM operator_commands WHERE command_id=%s",
            (command_id,),
        )
        scope = cur.fetchone()[0]
    preserved = scope["durable_entry_order_exemptions"]
    assert [item["client_order_id"] for item in preserved] == [
        order["client_order_id"] for order in orders
    ]
    assert {item["intent_id"] for item in preserved} == {intent_id}


def test_resume_allows_durable_entry_reduce_only_from_projection_payload(
    client: TestClient,
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_binance_mark_price",
        lambda _symbol: 102,
    )
    intent_id, orders = _seed_exchange_accepted_durable_entry_ladder(
        client,
        migrated_db,
    )
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE orders_projection
            SET reduce_only=NULL
            WHERE intent_id=%s
            """,
            (intent_id,),
        )
    _seed_heartbeat(migrated_db, regular_orders=orders)
    _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(None),
    )

    assert response.status_code == 200
    command_id = response.json()["command_id"]
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT scope FROM operator_commands WHERE command_id=%s",
            (command_id,),
        )
        scope = cur.fetchone()[0]
    assert len(scope["durable_entry_order_exemptions"]) == len(orders)


@pytest.mark.parametrize(
    "reduce_only_fields",
    (
        {"reduce_only": None},
        {"reduce_only": "false"},
        {"reduce_only": False, "reduceOnly": True},
    ),
)
def test_resume_rejects_durable_entry_without_explicit_reduce_only_false(
    client: TestClient,
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    reduce_only_fields: dict,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_binance_mark_price",
        lambda _symbol: 102,
    )
    _intent_id, orders = _seed_exchange_accepted_durable_entry_ladder(
        client,
        migrated_db,
    )
    orders[0].pop("reduce_only")
    orders[0].update(reduce_only_fields)
    _seed_heartbeat(migrated_db, regular_orders=orders)
    _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(None),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "robot-owned orders are not terminal"
    )


def test_resume_rejects_durable_entry_with_exchange_quantity_drift(
    client: TestClient,
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_binance_mark_price",
        lambda _symbol: 102,
    )
    _intent_id, orders = _seed_exchange_accepted_durable_entry_ladder(
        client,
        migrated_db,
    )
    orders[1]["quantity"] = "999"
    _seed_heartbeat(migrated_db, regular_orders=orders)
    _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(None),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "robot-owned orders are not terminal"
    )


def test_resume_rejects_durable_entry_with_intent_instrument_drift(
    client: TestClient,
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_binance_mark_price",
        lambda _symbol: 102,
    )
    _intent_id, orders = _seed_exchange_accepted_durable_entry_ladder(
        client,
        migrated_db,
    )
    drifted_client_order_id = orders[0]["client_order_id"]
    orders[0]["symbol"] = "ETHUSDT"
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE orders_projection
            SET instrument_id='ETHUSDT-PERP.BINANCE'
            WHERE client_order_id=%s
            """,
            (drifted_client_order_id,),
        )
    _seed_heartbeat(migrated_db, regular_orders=orders)
    _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(None),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "robot-owned orders are not terminal"
    )


def test_regular_resume_allows_owned_position_with_protection_in_place(
    client: TestClient,
    migrated_db: str,
) -> None:
    robot_client_order_id = "B" + ("e" * 32) + "05"
    _seed_heartbeat(
        migrated_db,
        positions=[{"symbol": SYMBOL, "quantity": "0.25"}],
        algo_orders=[
            {
                "symbol": SYMBOL,
                "client_order_id": robot_client_order_id,
                "order_type": "STOP_MARKET",
                "order_kind": "algo",
                "reduce_only": True,
            }
        ],
    )
    _seed_execution_fill(
        migrated_db,
        client_order_id="B" + ("f" * 32) + "06",
        side="BUY",
        quantity="0.25",
    )
    _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(None),
    )

    assert response.status_code == 200


def test_resume_rejects_robot_owned_algo_protection_without_reduce_only(
    client: TestClient,
    migrated_db: str,
) -> None:
    robot_client_order_id = "B" + ("c" * 32) + "03"
    _seed_heartbeat(
        migrated_db,
        algo_orders=[
            {
                "symbol": SYMBOL,
                "client_algo_id": robot_client_order_id,
                "order_type": "TAKE_PROFIT_MARKET",
                "order_kind": "algo",
            }
        ],
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "robot-owned orders are not terminal"
    )


@pytest.mark.parametrize("source", ("projection", "heartbeat"))
def test_resume_rejects_robot_owned_explicit_non_reduce_only_orders(
    client: TestClient,
    migrated_db: str,
    source: str,
) -> None:
    robot_client_order_id = "B" + ("d" * 32) + "04"
    heartbeat_args = {}
    if source == "heartbeat":
        heartbeat_args["regular_orders"] = [
            {
                "symbol": SYMBOL,
                "client_order_id": robot_client_order_id,
                "type": "LIMIT",
                "order_kind": "regular",
                "reduce_only": False,
            }
        ]
    _seed_heartbeat(migrated_db, **heartbeat_args)
    if source == "projection":
        with _connect(migrated_db) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders_projection (
                    order_projection_id, account_id, instrument_id,
                    client_order_id, status, side, order_type, quantity,
                    updated_at, payload
                )
                VALUES (
                    %s, %s, %s, %s, 'working', 'long',
                    'LIMIT', 1, now(), %s
                )
                """,
                (
                    str(uuid4()),
                    ACCOUNT_A,
                    "BTCUSDT-PERP.BINANCE",
                    robot_client_order_id,
                    Json(
                        {
                            "order_kind": "regular",
                            "reduceOnly": False,
                        }
                    ),
                ),
            )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "robot-owned orders are not terminal"
    )


def test_resume_keeps_five_second_heartbeat_freshness_window(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, age_seconds=6)
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "node heartbeat evidence is stale"


@pytest.mark.parametrize(
    ("margin_age_seconds", "expected_status"),
    (
        (6, 200),
        (29, 200),
        (31, 409),
    ),
)
def test_resume_uses_thirty_second_margin_freshness_window(
    client: TestClient,
    migrated_db: str,
    margin_age_seconds: int,
    expected_status: int,
) -> None:
    _seed_heartbeat(
        migrated_db,
        margin_age_seconds=margin_age_seconds,
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == expected_status
    if expected_status == 409:
        detail = response.json()["detail"]
        assert detail.startswith(
            "account margin evidence is stale: age_seconds="
        )
        assert "max_age_seconds=30.000" in detail
        age_text = detail.split("age_seconds=", 1)[1].split(" ", 1)[0]
        assert float(age_text) > 30


def test_resume_rejects_low_margin_ratio(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, equity=100, available_balance=3)
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "account margin ratio is below threshold"
    )


def test_resume_uses_payload_timestamp_for_reconciliation_health(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE node_heartbeats
            SET reconciliation_completed_at=clock_timestamp() - interval '5 minutes'
            WHERE node_id=%s
            """,
            (NODE_A,),
        )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, armed_node_id
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        status, armed_node_id = cur.fetchone()
    assert status == "armed"
    assert armed_node_id == NODE_A


def test_migration_rebaseline_all_halted_allows_later_account_canary_resume(
    client: TestClient,
    migrated_db: str,
) -> None:
    for index, (account_id, node_id) in enumerate(
        (
            (ACCOUNT_A, NODE_A),
            (ACCOUNT_B, NODE_B),
            (ACCOUNT_C, NODE_C),
            (ACCOUNT_D, NODE_D),
        ),
        start=1,
    ):
        _seed_heartbeat(
            migrated_db,
            node_id=node_id,
            account_id=account_id,
            runtime_generation=f"runtime-generation-{account_id}",
            lease_fencing_token=40 + index,
        )
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_account_id=ACCOUNT_B,
        rollout_phase="account_b_rollout",
        seed_fleet_peers=False,
        registration_mode="migration_rebaseline_stopped",
    )

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(
            permit_id,
            account_id=ACCOUNT_B,
            node_id=NODE_B,
        ),
    )

    assert response.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, armed_node_id, portfolio_baseline_sha256
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        status, armed_node_id, baseline = cur.fetchone()
    assert status == "armed"
    assert armed_node_id == NODE_B
    assert len(baseline) == 64


@pytest.mark.parametrize("target_risk", ("position", "regular_order", "algo_order"))
def test_resume_allows_existing_target_symbol_risk(
    client: TestClient,
    migrated_db: str,
    target_risk: str,
) -> None:
    heartbeat_args = {
        "positions": [{"symbol": "XAUUSDT", "quantity": "1"}],
        "regular_orders": [{"symbol": "ETHUSDT", "client_order_id": "eth-order"}],
        "algo_orders": [{"symbol": "SOLUSDT", "client_order_id": "sol-stop"}],
    }
    if target_risk == "position":
        heartbeat_args["positions"].append(
            {"symbol": SYMBOL, "quantity": "0.001"}
        )
    if target_risk == "regular_order":
        heartbeat_args["regular_orders"].append(
            {"symbol": SYMBOL, "client_order_id": "target-order"}
        )
    if target_risk == "algo_order":
        heartbeat_args["algo_orders"].append(
            {"symbol": SYMBOL, "client_order_id": "target-stop"}
        )
    _seed_heartbeat(migrated_db, **heartbeat_args)
    permit_id = _seed_reviewed_release_and_permit(migrated_db)

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("evidence_change", "expected_status", "expected_detail"),
    (
        ("incident", 409, "account has an open P0/P1 incident"),
        ("position", 200, ""),
        ("regular_order", 200, ""),
        ("algo_order", 200, ""),
        ("invalid_order", 200, ""),
        ("identity", 409, "node release identity does not match reviewed manifest"),
        ("stale", 200, ""),
        (
            "degraded",
            200,
            "",
        ),
        (
            "projection_lag",
            200,
            "",
        ),
        (
            "unhealthy_reconciliation",
            200,
            "",
        ),
    ),
)
def test_resume_allows_reconciliation_and_portfolio_warnings(
    client: TestClient,
    migrated_db: str,
    evidence_change: str,
    expected_status: int,
    expected_detail: str,
) -> None:
    heartbeat_args = {}
    if evidence_change == "position":
        heartbeat_args["positions"] = [{"symbol": SYMBOL, "quantity": "0.001"}]
    if evidence_change == "regular_order":
        heartbeat_args["regular_orders"] = [{"symbol": SYMBOL, "order_id": "1"}]
    if evidence_change == "algo_order":
        heartbeat_args["algo_orders"] = [{"symbol": SYMBOL, "algo_id": "1"}]
    if evidence_change == "invalid_order":
        heartbeat_args["regular_orders"] = [{"order_id": "missing-symbol"}]
    if evidence_change == "identity":
        heartbeat_args["release_id"] = "different-release"
    if evidence_change == "stale":
        heartbeat_args["evidence_age_seconds"] = 20
    if evidence_change == "degraded":
        heartbeat_args["health_degraded_reasons"] = [
            "execution projection filtered subscribed event: "
            "OrderInitialized"
        ]
    if evidence_change == "projection_lag":
        heartbeat_args["projection_lag_ms"] = 60_000
    if evidence_change == "unhealthy_reconciliation":
        heartbeat_args["reconciliation_state"] = "degraded"
    _seed_heartbeat(migrated_db, **heartbeat_args)
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    if evidence_change == "incident":
        with _connect(migrated_db) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO production_incidents (
                    incident_id, account_id, severity, status, summary
                )
                VALUES (%s, %s, 'P1', 'open', 'test incident')
                """,
                (str(uuid4()), ACCOUNT_A),
            )

    response = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )

    assert response.status_code == expected_status
    if expected_detail:
        assert response.json()["detail"] == expected_detail


def test_account_a_canary_permit_is_atomic_single_use_and_capped(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, trading_state="ACTIVE")
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_status="armed",
    )
    supplied_intent_id = str(uuid4())
    base = {
        "action": "open_position",
        "intent_id": supplied_intent_id,
        "account_id": ACCOUNT_A,
        "symbol": SYMBOL,
        "side": "long",
        "entry": {"type": "limit", "price": 100, "time_in_force": "IOC"},
        "quantity": 0.12,
        "notional_usdt": 12,
        "reason": "account-a canary",
        "protection_policy": "waived",
        "canary_permit_id": permit_id,
    }

    first = client.post(
        "/v1/operator/orders",
        headers=_risk_headers("canary-open-1"),
        json={**base, "client_ref": "canary-open-1"},
    )
    replay = client.post(
        "/v1/operator/orders",
        headers=_risk_headers("canary-open-1"),
        json={**base, "client_ref": "canary-open-1"},
    )
    second = client.post(
        "/v1/operator/orders",
        headers=_risk_headers("canary-open-2"),
        json={**base, "client_ref": "canary-open-2"},
    )

    assert first.status_code == 200
    assert first.json()["intent_id"] == supplied_intent_id
    assert replay.status_code == 200
    assert replay.json()["replay"] is True
    assert replay.json()["intent_id"] == first.json()["intent_id"]
    assert second.status_code == 409
    assert second.json()["detail"] == "canary permit has already been consumed"
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, consumed_open_count, consumed_intent_id::text
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        status, count, consumed_intent_id = cur.fetchone()
    assert status == "consumed"
    assert count == 1
    assert consumed_intent_id == first.json()["intent_id"]
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT order_plan FROM trade_intents WHERE intent_id=%s",
            (first.json()["intent_id"],),
        )
        order_plan = cur.fetchone()[0]
        canary = order_plan["canary_permit"]
        equity = order_plan["equity"]
        live_open_gate = order_plan["live_open_gate"]
        cur.execute(
            """
            SELECT currency, equity, available_balance, payload
            FROM accounts_projection
            WHERE account_id=%s
            """,
            (ACCOUNT_A,),
        )
        currency, real_equity, available_balance, account_payload = (
            cur.fetchone()
        )
    assert equity == {
        "real_equity": 100.0,
        "available_balance": 100.0,
        "risk_capital_addon": 0.0,
        "risk_capital_multiplier": 1.0,
        "effective_equity": 100.0,
    }
    assert currency == "USDT"
    assert float(real_equity) == 100.0
    assert float(available_balance) == 100.0
    assert (
        account_payload["account_snapshot_source"]
        == "binance_fapi_account_v3"
    )
    assert datetime.fromisoformat(
        account_payload["account_snapshot_fetched_at"]
    ).tzinfo is not None
    assert canary["target_symbol"] == SYMBOL
    assert live_open_gate == {
        "mode": "normal",
        "release_id": RELEASE_ID,
        "rollout_phase": "account_a_canary",
        "phase_version": 1,
    }
    assert len(canary["portfolio_baseline_sha256"]) == 64
    assert canary["max_cumulative_loss_usdt"] == "1.49"
    assert (
        canary["testnet_emergency_close_evidence_sha256"]
        == TESTNET_EMERGENCY_CLOSE_EVIDENCE_SHA256
    )
    assert datetime.fromisoformat(
        canary["testnet_emergency_close_verified_at"]
    ).tzinfo is not None


def test_canary_open_rejects_robot_owned_target_position_footprint(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, trading_state="ACTIVE")
    _seed_execution_fill(
        migrated_db,
        client_order_id="Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01",
        side="BUY",
        quantity="0.25",
    )
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_status="armed",
    )

    response = client.post(
        "/v1/operator/orders",
        headers=_risk_headers("canary-open-owned-position"),
        json={
            "action": "open_position",
            "account_id": ACCOUNT_A,
            "symbol": SYMBOL,
            "side": "long",
            "entry": {
                "type": "limit",
                "price": 100,
                "time_in_force": "IOC",
            },
            "quantity": 0.12,
            "notional_usdt": 12,
            "reason": "account-a canary",
            "canary_permit_id": permit_id,
            "protection_policy": "waived",
            "client_ref": "canary-open-owned-position",
        },
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "robot-owned target symbol position is not flat"
    )


def test_canary_open_uses_latest_ownership_rebaseline(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, trading_state="ACTIVE")
    _seed_execution_fill(
        migrated_db,
        client_order_id="Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01",
        side="BUY",
        quantity="0.25",
    )
    _seed_ownership_rebaseline(
        migrated_db,
        baseline_quantity="0",
        exchange_quantity="1.5",
        manual_quantity="1.5",
    )
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_status="armed",
    )

    response = client.post(
        "/v1/operator/orders",
        headers=_risk_headers("canary-open-rebaseline"),
        json={
            "action": "open_position",
            "account_id": ACCOUNT_A,
            "symbol": SYMBOL,
            "side": "long",
            "entry": {
                "type": "limit",
                "price": 100,
                "time_in_force": "IOC",
            },
            "quantity": 0.12,
            "notional_usdt": 12,
            "reason": "account-a canary",
            "canary_permit_id": permit_id,
            "protection_policy": "waived",
            "client_ref": "canary-open-rebaseline",
        },
    )

    assert response.status_code == 200


def test_account_a_canary_rejects_invalid_supplied_intent_id(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, trading_state="ACTIVE")
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_status="armed",
    )

    response = client.post(
        "/v1/operator/orders",
        headers=_risk_headers("canary-invalid-intent-id"),
        json={
            "action": "open_position",
            "intent_id": "not-a-uuid",
            "account_id": ACCOUNT_A,
            "symbol": SYMBOL,
            "side": "long",
            "entry": {
                "type": "limit",
                "price": 100,
                "time_in_force": "IOC",
            },
            "quantity": 0.12,
            "notional_usdt": 12,
            "reason": "account-a invalid canary intent",
            "client_ref": "canary-invalid-intent-id",
            "protection_policy": "waived",
            "canary_permit_id": permit_id,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "intent_id must be a uuid for canary open"
    )
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, consumed_open_count
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        status, consumed_open_count = cur.fetchone()
    assert status == "armed"
    assert consumed_open_count == 0


def test_canary_open_replay_preserves_first_gate_and_budget(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, trading_state="ACTIVE")
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_status="armed",
    )
    body = {
        "action": "open_position",
        "account_id": ACCOUNT_A,
        "symbol": SYMBOL,
        "side": "long",
        "entry": {
            "type": "limit",
            "price": 100,
            "time_in_force": "IOC",
        },
        "quantity": 0.12,
        "notional_usdt": 12,
        "reason": "stable replay proof",
        "client_ref": "stable-canary-replay",
        "protection_policy": "waived",
        "canary_permit_id": permit_id,
    }
    first = client.post(
        "/v1/operator/orders",
        headers=_risk_headers("stable-canary-replay"),
        json=body,
    )
    assert first.status_code == 200
    first_payload = first.json()
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reviewed_release_rollouts
            SET phase='account_b_rollout',
                phase_version=phase_version + 1
            WHERE release_id=%s
            """,
            (RELEASE_ID,),
        )
        cur.execute(
            """
            UPDATE accounts_projection
            SET equity=1,
                available_balance=1,
                updated_at=now()
            WHERE account_id=%s
            """,
            (ACCOUNT_A,),
        )

    replay = client.post(
        "/v1/operator/orders",
        headers=_risk_headers("stable-canary-replay"),
        json=body,
    )

    assert replay.status_code == 200
    replay_payload = replay.json()
    assert replay_payload["replay"] is True
    assert replay_payload["intent_id"] == first_payload["intent_id"]
    assert replay_payload["order_plan"] == first_payload["order_plan"]
    assert replay_payload["risk_budget"] == first_payload["risk_budget"]
    assert replay_payload["order_plan"]["live_open_gate"] == {
        "mode": "normal",
        "release_id": RELEASE_ID,
        "rollout_phase": "account_a_canary",
        "phase_version": 1,
    }


@pytest.mark.parametrize(
    "account_id",
    (ACCOUNT_A, ACCOUNT_B, ACCOUNT_C, ACCOUNT_D),
)
def test_rollout_accounts_accept_regular_zone_open(
    client: TestClient,
    migrated_db: str,
    account_id: str,
) -> None:
    _seed_heartbeat(
        migrated_db,
        node_id=f"nautilus-node-{account_id}",
        account_id=account_id,
        runtime_generation=f"runtime-generation-{account_id}",
    )
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_account_id=account_id,
        rollout_phase="account_a_canary",
    )
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM live_canary_permits WHERE permit_id=%s",
            (permit_id,),
        )
    response = client.post(
        "/v1/operator/orders",
        headers=_risk_headers(f"{account_id}-regular-zone"),
        json={
            "action": "open_position",
            "account_id": account_id,
            "symbol": SYMBOL,
            "side": "long",
            "entry": {
                "type": "zone",
                "price_min": 99,
                "price_max": 101,
            },
            "quantity": 0.12,
            "notional_usdt": 12,
            "reason": f"{account_id} regular zone signal",
            "protection_policy": "waived",
            "client_ref": f"{account_id}-regular-zone",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["order_plan"]["entry"]["type"] == "zone"
    assert payload["order_plan"]["live_open_gate"] == {
        "mode": "normal",
        "release_id": RELEASE_ID,
        "rollout_phase": "account_a_canary",
        "phase_version": 1,
    }
    assert "canary_permit" not in payload["order_plan"]


def test_account_a_canary_rejects_notional_above_permit(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, trading_state="ACTIVE")
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_status="armed",
    )

    response = client.post(
        "/v1/operator/orders",
        headers=_risk_headers("canary-over-limit"),
        json={
            "action": "open_position",
            "account_id": ACCOUNT_A,
            "symbol": SYMBOL,
            "side": "long",
            "entry": {"type": "limit", "price": 100, "time_in_force": "IOC"},
            "quantity": 0.1201,
            "notional_usdt": 12.01,
            "reason": "account-a canary",
            "client_ref": "canary-over-limit",
            "protection_policy": "waived",
            "canary_permit_id": permit_id,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "canary notional exceeds permit limit"


def test_account_a_canary_rejects_decimal_notional_that_float_rounds_to_cap(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db, trading_state="ACTIVE")
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_status="armed",
    )

    response = client.post(
        "/v1/operator/orders",
        headers=_risk_headers("canary-decimal-over-limit"),
        json={
            "action": "open_position",
            "account_id": ACCOUNT_A,
            "symbol": SYMBOL,
            "side": "long",
            "entry": {
                "type": "limit",
                "price": "12.0000000000000001",
                "time_in_force": "IOC",
            },
            "quantity": "1",
            "notional_usdt": "12.0000000000000001",
            "reason": "account-a decimal canary",
            "client_ref": "canary-decimal-over-limit",
            "protection_policy": "waived",
            "canary_permit_id": permit_id,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "canary notional exceeds permit limit"
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, consumed_open_count
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        assert cur.fetchone() == ("armed", 0)


def test_account_a_canary_permit_has_one_concurrent_open_winner(
    client: TestClient,
    migrated_db: str,
) -> None:
    del client
    _seed_heartbeat(migrated_db, trading_state="ACTIVE")
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_status="armed",
    )
    barrier = threading.Barrier(2)

    def submit(client_ref: str):
        barrier.wait()
        with TestClient(read_api.app) as worker_client:
            return worker_client.post(
                "/v1/operator/orders",
                headers=_risk_headers(client_ref),
                json={
                    "action": "open_position",
                    "account_id": ACCOUNT_A,
                    "symbol": SYMBOL,
                    "side": "long",
                    "entry": {
                        "type": "limit",
                        "price": 100,
                        "time_in_force": "IOC",
                    },
                    "quantity": 0.12,
                    "notional_usdt": 12,
                    "reason": "concurrent account-a canary",
                    "protection_policy": "waived",
                    "client_ref": client_ref,
                    "canary_permit_id": permit_id,
                },
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                submit,
                ("canary-concurrent-1", "canary-concurrent-2"),
            )
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    rejected = next(
        response for response in responses if response.status_code == 409
    )
    assert rejected.json()["detail"] == "canary permit has already been consumed"
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)
            FROM trade_intents
            WHERE account_id=%s
              AND instrument_id=%s
              AND action='open_position'
            """,
            (ACCOUNT_A, SYMBOL),
        )
        assert cur.fetchone()[0] == 1


@pytest.mark.parametrize(
    ("entry", "quantity", "expected_detail"),
    (
        (
            {"type": "market", "time_in_force": "IOC"},
            0.12,
            "account-a canary entry.type must be limit",
        ),
        (
            {"type": "zone", "price_min": 99, "price_max": 101},
            0.12,
            "account-a canary entry.type must be limit",
        ),
        (
            {"type": "limit", "price": 100, "time_in_force": "GTC"},
            0.12,
            "account-a canary time_in_force must be IOC",
        ),
        (
            {"type": "limit", "price": 100, "time_in_force": "IOC"},
            None,
            "account-a canary requires explicit quantity",
        ),
    ),
)
def test_account_a_canary_rejects_unsafe_order_shape_before_permit_lock(
    client: TestClient,
    migrated_db: str,
    entry: dict,
    quantity: float | None,
    expected_detail: str,
) -> None:
    _seed_heartbeat(migrated_db, trading_state="ACTIVE")
    permit_id = _seed_reviewed_release_and_permit(
        migrated_db,
        permit_status="armed",
    )
    body = {
        "action": "open_position",
        "account_id": ACCOUNT_A,
        "symbol": SYMBOL,
        "side": "long",
        "entry": entry,
        "notional_usdt": 12,
        "reason": "unsafe account-a canary",
        "client_ref": str(uuid4()),
        "canary_permit_id": permit_id,
    }
    if quantity is not None:
        body["quantity"] = quantity

    response = client.post(
        "/v1/operator/orders",
        headers=_risk_headers(str(uuid4())),
        json=body,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == expected_detail
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, consumed_open_count
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        assert cur.fetchone() == ("armed", 0)


def test_non_owned_portfolio_change_keeps_armed_canary(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(
        migrated_db,
        positions=[{"symbol": "XAUUSDT", "quantity": "1", "mark_price": "2400"}],
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    resume = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )
    assert resume.status_code == 200

    now = datetime.now(timezone.utc).replace(microsecond=0)
    changed = client.post(
        f"/v1/nodes/{NODE_A}/heartbeat",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "ts": now.isoformat(),
            "trading_state": "ACTIVE",
            "readiness": True,
            "projection_lag_ms": 0,
            "reconciliation_state": "healthy",
            "release_id": RELEASE_ID,
            "image_digest": IMAGE_DIGEST,
            "config_sha256": CONFIG_SHA256,
            "dependency_lock_sha256": LOCK_SHA256,
            "schema_epoch": SCHEMA_EPOCH,
            "positions": [{"symbol": "XAUUSDT", "quantity": "2", "mark_price": "2500"}],
            "regular_orders": [],
            "algo_orders": [],
            "positions_snapshot_at": now.isoformat(),
            "regular_orders_snapshot_at": now.isoformat(),
            "algo_orders_snapshot_at": now.isoformat(),
            "reconciliation_completed_at": now.isoformat(),
            "redis_fencing_epoch": REDIS_FENCING_EPOCH,
            "runtime_generation": RUNTIME_GENERATION,
            "lease_fencing_token": LEASE_FENCING_TOKEN,
            "heartbeat_sequence": HEARTBEAT_SEQUENCE + 1,
        },
    )

    assert changed.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM live_canary_permits WHERE permit_id=%s",
            (permit_id,),
        )
        assert cur.fetchone()[0] == "armed"


def test_missing_exchange_evidence_cannot_revoke_armed_canary(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(
        migrated_db,
        positions=[
            {
                "symbol": "XAUUSDT",
                "quantity": "1",
                "mark_price": "2400",
            }
        ],
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    resume = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )
    assert resume.status_code == 200

    now = datetime.now(timezone.utc).replace(microsecond=0)
    missing = client.post(
        f"/v1/nodes/{NODE_A}/heartbeat",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "ts": now.isoformat(),
            "trading_state": "ACTIVE",
            "readiness": True,
            "projection_lag_ms": 0,
            "reconciliation_state": "healthy",
            "release_id": RELEASE_ID,
            "image_digest": IMAGE_DIGEST,
            "config_sha256": CONFIG_SHA256,
            "dependency_lock_sha256": LOCK_SHA256,
            "schema_epoch": SCHEMA_EPOCH,
            "reconciliation_completed_at": now.isoformat(),
            "redis_fencing_epoch": REDIS_FENCING_EPOCH,
            "runtime_generation": RUNTIME_GENERATION,
            "lease_fencing_token": LEASE_FENCING_TOKEN,
            "heartbeat_sequence": HEARTBEAT_SEQUENCE + 1,
        },
    )

    assert missing.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        assert cur.fetchone()[0] == "armed"
        cur.execute(
            """
            SELECT heartbeat_sequence, positions, payload
            FROM node_heartbeats
            WHERE node_id=%s
            """,
            (NODE_A,),
        )
        heartbeat_sequence, positions, payload = cur.fetchone()
    assert heartbeat_sequence == HEARTBEAT_SEQUENCE + 1
    assert positions == [
        {
            "symbol": "XAUUSDT",
            "quantity": "1",
            "mark_price": "2400",
        }
    ]
    assert "node_exchange_evidence_missing" in (
        payload["health_degraded_reasons"]
    )


def test_explicit_empty_non_owned_exchange_evidence_keeps_armed_canary(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(
        migrated_db,
        positions=[
            {
                "symbol": "XAUUSDT",
                "quantity": "1",
            }
        ],
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    resume = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )
    assert resume.status_code == 200

    now = datetime.now(timezone.utc).replace(microsecond=0)
    empty = client.post(
        f"/v1/nodes/{NODE_A}/heartbeat",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "ts": now.isoformat(),
            "trading_state": "ACTIVE",
            "readiness": True,
            "projection_lag_ms": 0,
            "reconciliation_state": "healthy",
            "release_id": RELEASE_ID,
            "image_digest": IMAGE_DIGEST,
            "config_sha256": CONFIG_SHA256,
            "dependency_lock_sha256": LOCK_SHA256,
            "schema_epoch": SCHEMA_EPOCH,
            "positions": [],
            "regular_orders": [],
            "algo_orders": [],
            "positions_snapshot_at": now.isoformat(),
            "regular_orders_snapshot_at": now.isoformat(),
            "algo_orders_snapshot_at": now.isoformat(),
            "reconciliation_completed_at": now.isoformat(),
            "redis_fencing_epoch": REDIS_FENCING_EPOCH,
            "runtime_generation": RUNTIME_GENERATION,
            "lease_fencing_token": LEASE_FENCING_TOKEN,
            "heartbeat_sequence": HEARTBEAT_SEQUENCE + 1,
        },
    )

    assert empty.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status
            FROM live_canary_permits
            WHERE permit_id=%s
            """,
            (permit_id,),
        )
        assert cur.fetchone()[0] == "armed"


def test_stale_exchange_evidence_cannot_replace_fresh_heartbeat(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(
        migrated_db,
        positions=[
            {
                "symbol": "XAUUSDT",
                "quantity": "1",
            }
        ],
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stale_at = now - timedelta(seconds=20)

    stale = client.post(
        f"/v1/nodes/{NODE_A}/heartbeat",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "ts": now.isoformat(),
            "trading_state": "HALTED",
            "readiness": True,
            "projection_lag_ms": 0,
            "reconciliation_state": "healthy",
            "release_id": RELEASE_ID,
            "image_digest": IMAGE_DIGEST,
            "config_sha256": CONFIG_SHA256,
            "dependency_lock_sha256": LOCK_SHA256,
            "schema_epoch": SCHEMA_EPOCH,
            "positions": [],
            "regular_orders": [],
            "algo_orders": [],
            "positions_snapshot_at": stale_at.isoformat(),
            "regular_orders_snapshot_at": stale_at.isoformat(),
            "algo_orders_snapshot_at": stale_at.isoformat(),
            "reconciliation_completed_at": now.isoformat(),
            "redis_fencing_epoch": REDIS_FENCING_EPOCH,
            "runtime_generation": RUNTIME_GENERATION,
            "lease_fencing_token": LEASE_FENCING_TOKEN,
            "heartbeat_sequence": HEARTBEAT_SEQUENCE + 1,
        },
    )

    assert stale.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT heartbeat_sequence, positions, payload
            FROM node_heartbeats
            WHERE node_id=%s
            """,
            (NODE_A,),
        )
        heartbeat_sequence, positions, payload = cur.fetchone()
    assert heartbeat_sequence == HEARTBEAT_SEQUENCE + 1
    assert positions == [
        {
            "symbol": "XAUUSDT",
            "quantity": "1",
        }
    ]
    assert "node_exchange_evidence_is_stale" in (
        payload["health_degraded_reasons"]
    )


def test_non_target_market_data_and_row_order_keep_portfolio_baseline(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(
        migrated_db,
        positions=[
            {"symbol": "XAUUSDT", "quantity": "1", "mark_price": "2400"},
            {"symbol": "MUUSDT", "quantity": "2", "mark_price": "120"},
        ],
        regular_orders=[
            {"symbol": "ETHUSDT", "client_order_id": "eth-order"},
            {"symbol": "SOLUSDT", "client_order_id": "sol-order"},
        ],
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    resume = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )
    assert resume.status_code == 200

    now = datetime.now(timezone.utc).replace(microsecond=0)
    refreshed = client.post(
        f"/v1/nodes/{NODE_A}/heartbeat",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "ts": now.isoformat(),
            "trading_state": "ACTIVE",
            "readiness": True,
            "projection_lag_ms": 0,
            "reconciliation_state": "healthy",
            "release_id": RELEASE_ID,
            "image_digest": IMAGE_DIGEST,
            "config_sha256": CONFIG_SHA256,
            "dependency_lock_sha256": LOCK_SHA256,
            "schema_epoch": SCHEMA_EPOCH,
            "positions": [
                {"symbol": "MUUSDT", "quantity": "2.0", "mark_price": "121"},
                {"symbol": "XAUUSDT", "quantity": "1.00", "mark_price": "2500"},
            ],
            "regular_orders": [
                {"symbol": "SOLUSDT", "client_order_id": "sol-order"},
                {"symbol": "ETHUSDT", "client_order_id": "eth-order"},
            ],
            "algo_orders": [],
            "positions_snapshot_at": now.isoformat(),
            "regular_orders_snapshot_at": now.isoformat(),
            "algo_orders_snapshot_at": now.isoformat(),
            "reconciliation_completed_at": now.isoformat(),
            "redis_fencing_epoch": REDIS_FENCING_EPOCH,
            "runtime_generation": RUNTIME_GENERATION,
            "lease_fencing_token": LEASE_FENCING_TOKEN,
            "heartbeat_sequence": HEARTBEAT_SEQUENCE + 1,
        },
    )

    assert refreshed.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM live_canary_permits WHERE permit_id=%s",
            (permit_id,),
        )
        assert cur.fetchone()[0] == "armed"


def test_non_owned_order_price_change_keeps_portfolio_baseline(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(
        migrated_db,
        regular_orders=[
            {
                "symbol": "ETHUSDT",
                "client_order_id": "eth-order",
                "quantity": "0.1",
                "price": "3000",
            }
        ],
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    resume = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )
    assert resume.status_code == 200

    now = datetime.now(timezone.utc).replace(microsecond=0)
    changed = client.post(
        f"/v1/nodes/{NODE_A}/heartbeat",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "ts": now.isoformat(),
            "trading_state": "ACTIVE",
            "readiness": True,
            "projection_lag_ms": 0,
            "reconciliation_state": "healthy",
            "release_id": RELEASE_ID,
            "image_digest": IMAGE_DIGEST,
            "config_sha256": CONFIG_SHA256,
            "dependency_lock_sha256": LOCK_SHA256,
            "schema_epoch": SCHEMA_EPOCH,
            "positions": [],
            "regular_orders": [
                {
                    "symbol": "ETHUSDT",
                    "client_order_id": "eth-order",
                    "quantity": "0.1",
                    "price": "3001",
                }
            ],
            "algo_orders": [],
            "positions_snapshot_at": now.isoformat(),
            "regular_orders_snapshot_at": now.isoformat(),
            "algo_orders_snapshot_at": now.isoformat(),
            "reconciliation_completed_at": now.isoformat(),
            "redis_fencing_epoch": REDIS_FENCING_EPOCH,
            "runtime_generation": RUNTIME_GENERATION,
            "lease_fencing_token": LEASE_FENCING_TOKEN,
            "heartbeat_sequence": HEARTBEAT_SEQUENCE + 1,
        },
    )

    assert changed.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM live_canary_permits WHERE permit_id=%s",
            (permit_id,),
        )
        assert cur.fetchone()[0] == "armed"


def test_permit_consumption_allows_non_owned_baseline_change(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(
        migrated_db,
        positions=[{"symbol": "XAUUSDT", "quantity": "1"}],
    )
    permit_id = _seed_reviewed_release_and_permit(migrated_db)
    resume = client.post(
        "/v1/commands",
        headers=_risk_headers(str(uuid4())),
        json=_resume_body(permit_id),
    )
    assert resume.status_code == 200

    _seed_heartbeat(
        migrated_db,
        trading_state="ACTIVE",
        positions=[{"symbol": "XAUUSDT", "quantity": "2"}],
        heartbeat_sequence=HEARTBEAT_SEQUENCE + 1,
    )
    response = client.post(
        "/v1/operator/orders",
        headers=_risk_headers("baseline-changed"),
        json={
            "action": "open_position",
            "account_id": ACCOUNT_A,
            "symbol": SYMBOL,
            "side": "long",
            "entry": {
                "type": "limit",
                "price": 100,
                "time_in_force": "IOC",
            },
            "quantity": 0.12,
            "notional_usdt": 12,
            "reason": "baseline changed",
            "protection_policy": "waived",
            "client_ref": "baseline-changed",
            "canary_permit_id": permit_id,
        },
    )

    assert response.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM live_canary_permits WHERE permit_id=%s",
            (permit_id,),
        )
        assert cur.fetchone()[0] == "consumed"


def test_node_command_ack_preserves_progress_states_and_rejects_unknown(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_heartbeat(migrated_db)
    command_id = str(uuid4())
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO operator_commands (
                command_id, command_type, scope, status, requested_by,
                reason, idempotency_key
            )
            VALUES (%s, 'CLOSE_ALL', %s, 'pending', 'risk_admin', 'test', %s)
            """,
            (command_id, Json({"account_id": ACCOUNT_A}), str(uuid4())),
        )
        cur.execute(
            """
            INSERT INTO command_node_acks (command_id, node_id, status)
            VALUES (%s, %s, 'pending')
            """,
            (command_id, NODE_A),
        )

    ack_payloads = (
        ("accepted", {}),
        (
            "running",
            {
                "result": {
                    "phase": "dispatching",
                    "attempt": 1,
                },
                "detail": "exchange requests submitted",
            },
        ),
        (
            "completed",
            {
                "result": {
                    "terminal_snapshot": {
                        "regular_orders": 0,
                        "algo_orders": 0,
                        "positions": 0,
                    },
                },
            },
        ),
    )
    for status, evidence in ack_payloads:
        payload = {
            "account_id": ACCOUNT_A,
            "status": status,
        }
        payload.update(evidence)
        response = client.post(
            f"/v1/nodes/{NODE_A}/commands/{command_id}/ack",
            headers=_node_headers(
                NODE_A,
                ACCOUNT_A,
                NODE_A_TOKEN,
                redis_fencing_epoch=REDIS_FENCING_EPOCH,
                runtime_generation=RUNTIME_GENERATION,
                lease_fencing_token=LEASE_FENCING_TOKEN,
            ),
            json=payload,
        )
        assert response.status_code == 200
        with _connect(migrated_db) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, result, detail
                FROM command_node_acks
                WHERE command_id=%s
                  AND node_id=%s
                """,
                (command_id, NODE_A),
            )
            ack_status, ack_result, ack_detail = cur.fetchone()
            assert ack_status == status
            if status == "running":
                assert ack_result == {
                    "phase": "dispatching",
                    "attempt": 1,
                }
                assert ack_detail == "exchange requests submitted"
                repeated_running = client.post(
                    (
                        f"/v1/nodes/{NODE_A}/commands/"
                        f"{command_id}/ack"
                    ),
                    headers=_node_headers(
                        NODE_A,
                        ACCOUNT_A,
                        NODE_A_TOKEN,
                    ),
                    json={
                        "account_id": ACCOUNT_A,
                        "status": "running",
                        "result": {
                            "attempt": 2,
                            "exchange_request_ids": ["request-1"],
                        },
                    },
                )
                assert repeated_running.status_code == 200
                cur.execute(
                    """
                    SELECT status, result, detail
                    FROM command_node_acks
                    WHERE command_id=%s
                      AND node_id=%s
                    """,
                    (command_id, NODE_A),
                )
                (
                    repeated_status,
                    repeated_result,
                    repeated_detail,
                ) = cur.fetchone()
                assert repeated_status == "running"
                assert repeated_result == {
                    "phase": "dispatching",
                    "attempt": 2,
                    "exchange_request_ids": ["request-1"],
                }
                assert repeated_detail == "exchange requests submitted"
            if status == "completed":
                assert ack_result == {
                    "phase": "dispatching",
                    "attempt": 2,
                    "exchange_request_ids": ["request-1"],
                    "terminal_snapshot": {
                        "regular_orders": 0,
                        "algo_orders": 0,
                        "positions": 0,
                    },
                }
            cur.execute(
                "SELECT status FROM operator_commands WHERE command_id=%s",
                (command_id,),
            )
            assert cur.fetchone()[0] == status

    repeated_terminal = client.post(
        f"/v1/nodes/{NODE_A}/commands/{command_id}/ack",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "status": "completed",
            "result": {
                "verification_rounds": 2,
                "terminal_snapshot": {
                    "regular_orders": 0,
                    "algo_orders": 0,
                    "positions": 0,
                    "snapshot_sequence": 2,
                },
            },
            "error": "terminal proof refreshed",
        },
    )
    assert repeated_terminal.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, result, detail
            FROM command_node_acks
            WHERE command_id=%s
              AND node_id=%s
            """,
            (command_id, NODE_A),
        )
        ack_status, ack_result, ack_detail = cur.fetchone()
        assert ack_status == "completed"
        assert ack_result == {
            "phase": "dispatching",
            "attempt": 2,
            "exchange_request_ids": ["request-1"],
            "verification_rounds": 2,
            "terminal_snapshot": {
                "regular_orders": 0,
                "algo_orders": 0,
                "positions": 0,
                "snapshot_sequence": 2,
            },
        }
        assert ack_detail == "terminal proof refreshed"
        cur.execute(
            "SELECT status FROM operator_commands WHERE command_id=%s",
            (command_id,),
        )
        assert cur.fetchone()[0] == "completed"

    unknown = client.post(
        f"/v1/nodes/{NODE_A}/commands/{command_id}/ack",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={"account_id": ACCOUNT_A, "status": "mystery"},
    )
    assert unknown.status_code == 400
    assert unknown.json()["detail"] == "invalid command ack status"

    regression = client.post(
        f"/v1/nodes/{NODE_A}/commands/{command_id}/ack",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={"account_id": ACCOUNT_A, "status": "running"},
    )
    assert regression.status_code == 409
    assert regression.json()["detail"] == "invalid command ack transition"

    terminal_change = client.post(
        f"/v1/nodes/{NODE_A}/commands/{command_id}/ack",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "status": "failed",
            "result": {"residual_positions": 1},
        },
    )
    assert terminal_change.status_code == 409
    assert terminal_change.json()["detail"] == (
        "invalid command ack transition"
    )

    invalid_result = client.post(
        f"/v1/nodes/{NODE_A}/commands/{command_id}/ack",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={
            "account_id": ACCOUNT_A,
            "status": "completed",
            "result": ["not", "an", "object"],
        },
    )
    assert invalid_result.status_code == 400
    assert invalid_result.json()["detail"] == (
        "command ack result must be an object"
    )

    failed_command_id = str(uuid4())
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO operator_commands (
                command_id, command_type, scope, status, requested_by,
                reason, idempotency_key
            )
            VALUES (%s, 'CLOSE_ALL', %s, 'pending', 'risk_admin', 'test', %s)
            """,
            (
                failed_command_id,
                Json({"account_id": ACCOUNT_A}),
                str(uuid4()),
            ),
        )
        cur.execute(
            """
            INSERT INTO command_node_acks (command_id, node_id, status)
            VALUES (%s, %s, 'pending')
            """,
            (failed_command_id, NODE_A),
        )

    failed = client.post(
        f"/v1/nodes/{NODE_A}/commands/{failed_command_id}/ack",
        headers=_node_headers(NODE_A, ACCOUNT_A, NODE_A_TOKEN),
        json={"account_id": ACCOUNT_A, "status": "failed"},
    )
    assert failed.status_code == 200
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM command_node_acks WHERE command_id=%s",
            (failed_command_id,),
        )
        assert cur.fetchone()[0] == "failed"
        cur.execute(
            "SELECT status FROM operator_commands WHERE command_id=%s",
            (failed_command_id,),
        )
        assert cur.fetchone()[0] == "failed"
