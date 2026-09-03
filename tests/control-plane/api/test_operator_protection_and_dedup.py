"""WP-E tests: operator open protection-completeness gate + semantic dedup.

Spec (docs/plans/2026-08-28-execution-state-arch-migration.md, WP-E 改动点 2/4,
softened to advisory-only per the 2026-08-28 operator directive: owner-operated
account, gates warn but never block), on POST /v1/operator/orders for
open_position:

- take_profits == [] and no stop_loss and no protection_policy -> accepted,
  protection_policy auto-recorded as "deferred", warning in the response.
- take_profits == [] with stop_loss and no protection_policy -> accepted, the
  server auto-fills protection_policy="stop_only" and echoes it back.
- explicit protection_policy is validated against the contract enum (typo
  guard, still 422 — malformed input, not a trading restriction).
- non-empty take_profits are unaffected.
- semantic dedup: an active (status='approved', valid_until in the future)
  open intent for the same account+instrument+action with a Decimal-equal
  entry price -> proceeds with a "duplicate_open_intent" warning naming the
  existing intent_id; a different entry price (the 2026-08-25 case: 1.55 vs
  1.459) stays silent; "allow_duplicate": true silences the warning.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import psycopg2
import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import Json

import read_api


ACCOUNT_ID = "account-b"
NODE_ID = "nautilus-node-account-b"
SYMBOL = "ATOMUSDT"
RISK_TOKEN = "risk-token"
RELEASE_ID = "release-wp-e-protection"
IMAGE_DIGEST = "sha256:" + ("1" * 64)
CONFIG_SHA256 = "2" * 64
LOCK_SHA256 = "3" * 64
SCHEMA_EPOCH = "2026-08-08"
RUNTIME_GENERATION = "runtime-generation-wp-e"
REDIS_FENCING_EPOCH = "11111111-1111-4111-8111-111111111111"

# 2026-08-25 incident prices: two adds at DIFFERENT entry prices are distinct
# operations and must both pass the dedup gate.
ENTRY_PRICE = 1.55
OTHER_ENTRY_PRICE = 1.459


def _connect(url: str):
    return psycopg2.connect(url)


def _activate_redis_epoch(url: str) -> None:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE redis_fencing_epochs
            SET status='retired', retired_at=now()
            WHERE domain='trader-v3' AND status='active'
            """
        )
        cur.execute(
            """
            INSERT INTO redis_fencing_epochs (
                redis_fencing_epoch, domain, status, marker_sha256,
                capacity_evidence_sha256, initial_redis_run_id,
                active_volume, activated_by, activated_at
            )
            VALUES (%s, 'trader-v3', 'active', %s, %s, %s, %s, 'test', now())
            """,
            (
                REDIS_FENCING_EPOCH,
                "1" * 64,
                "2" * 64,
                "a" * 40,
                f"redis-volume-{REDIS_FENCING_EPOCH}",
            ),
        )


def _seed_account_state(url: str) -> None:
    """Fresh node heartbeat + accounts_projection equity basis (sizing gate)."""
    now = datetime.now(timezone.utc)
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO node_heartbeats (
                node_id, account_id, status, version, payload,
                release_id, image_digest, config_sha256,
                dependency_lock_sha256, schema_epoch,
                positions, regular_orders, algo_orders,
                positions_snapshot_at, regular_orders_snapshot_at,
                algo_orders_snapshot_at, reconciliation_completed_at,
                redis_fencing_epoch, runtime_generation,
                lease_fencing_token, heartbeat_sequence, last_seen_at
            )
            VALUES (
                %s, %s, 'HALTED', %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, 41, 1, %s
            )
            ON CONFLICT (node_id) DO UPDATE SET
                payload=EXCLUDED.payload,
                last_seen_at=EXCLUDED.last_seen_at
            """,
            (
                NODE_ID,
                ACCOUNT_ID,
                RELEASE_ID,
                Json(
                    {
                        "readiness": True,
                        "projection_lag_ms": 0,
                        "reconciliation_state": "healthy",
                        "health_degraded_reasons": [],
                        "ts": now.isoformat(),
                    }
                ),
                RELEASE_ID,
                IMAGE_DIGEST,
                CONFIG_SHA256,
                LOCK_SHA256,
                SCHEMA_EPOCH,
                Json([]),
                Json([]),
                Json([]),
                now,
                now,
                now,
                now,
                REDIS_FENCING_EPOCH,
                RUNTIME_GENERATION,
                now,
            ),
        )
        cur.execute(
            """
            INSERT INTO accounts_projection (
                account_id, currency, equity, margin,
                available_balance, updated_at, payload
            )
            VALUES (%s, 'USDT', 100, 0, 100, %s, %s)
            ON CONFLICT (account_id) DO UPDATE SET
                equity=EXCLUDED.equity,
                available_balance=EXCLUDED.available_balance,
                updated_at=EXCLUDED.updated_at,
                payload=EXCLUDED.payload
            """,
            (
                ACCOUNT_ID,
                now,
                Json(
                    {
                        "account_snapshot_source": "binance_fapi_account_v3",
                        "account_snapshot_fetched_at": now.isoformat(),
                        "exchange_account": {
                            "currency": "USDT",
                            "equity": 100,
                            "margin": 0,
                            "free": 100,
                        },
                    }
                ),
            ),
        )


def _seed_reviewed_rollout(url: str) -> None:
    """A reviewed release rollout row => live open gate mode 'normal'."""
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reviewed_release_rollouts (
                release_id, redis_fencing_epoch, image_digest,
                config_sha256, dependency_lock_sha256, schema_epoch,
                manifest_sha256, bundle_manifest_sha256,
                registration_idempotency_key, reviewed_by
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
                "register-wp-e-protection",
            ),
        )


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("RISK_ADMIN_TOKEN", RISK_TOKEN)
    monkeypatch.setattr(
        read_api,
        "_account_risk_capital_addon",
        lambda _account_id: 0.0,
    )
    # Deterministic sizing: never read the operator's watcher sqlite config.
    monkeypatch.setattr(read_api, "_symbol_risk_ratio", lambda _symbol: 0.01)
    _activate_redis_epoch(migrated_db)
    _seed_account_state(migrated_db)
    _seed_reviewed_rollout(migrated_db)
    test_client = TestClient(read_api.app)
    try:
        yield test_client
    finally:
        test_client.close()


def _headers(request_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {RISK_TOKEN}",
        "X-Request-Id": request_id,
    }


def _open_body(
    client_ref: str,
    *,
    price: float = ENTRY_PRICE,
    take_profits: list | None = None,
    stop_loss: float | None = None,
    protection_policy: str | None = None,
    allow_duplicate: bool | None = None,
) -> dict:
    body = {
        "action": "open_position",
        "account_id": ACCOUNT_ID,
        "symbol": SYMBOL,
        "side": "long",
        "entry": {"type": "limit", "price": price, "time_in_force": "GTC"},
        "notional_usdt": 12,
        "reason": "wp-e protection/dedup test",
        "client_ref": client_ref,
    }
    if take_profits is not None:
        body["take_profits"] = take_profits
    if stop_loss is not None:
        body["stop_loss"] = stop_loss
    if protection_policy is not None:
        body["protection_policy"] = protection_policy
    if allow_duplicate is not None:
        body["allow_duplicate"] = allow_duplicate
    return body


def _post_open(client: TestClient, body: dict):
    return client.post(
        "/v1/operator/orders",
        headers=_headers(body["client_ref"]),
        json=body,
    )


def _intent_count(url: str) -> int:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM trade_intents WHERE account_id=%s",
            (ACCOUNT_ID,),
        )
        return cur.fetchone()[0]


def _intent_order_plan(url: str, intent_id: str) -> dict:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT order_plan FROM trade_intents WHERE intent_id=%s",
            (intent_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


# --- 改动点 2: protection completeness gate ---------------------------------


def test_open_with_empty_take_profits_and_no_protection_warns_and_proceeds(
    client: TestClient,
    migrated_db: str,
) -> None:
    """Owner-operated account (2026-08-28 directive): advisory, never blocking."""
    response = _post_open(
        client,
        _open_body("wp-e-unprotected", take_profits=[]),
    )

    assert response.status_code == 200
    payload = response.json()
    # The intent proceeds with the factual policy recorded and a warning.
    assert payload["order_plan"]["protection_policy"] == "deferred"
    assert any("protection" in w for w in payload["warnings"])
    persisted = _intent_order_plan(migrated_db, payload["intent_id"])
    assert persisted["protection_policy"] == "deferred"


def test_open_with_stop_loss_autofills_protection_policy_stop_only(
    client: TestClient,
    migrated_db: str,
) -> None:
    response = _post_open(
        client,
        _open_body("wp-e-stop-only", take_profits=[], stop_loss=1.50),
    )

    assert response.status_code == 200
    payload = response.json()
    # Echoed in the response (operations continuity: not rejected)...
    assert payload["order_plan"]["protection_policy"] == "stop_only"
    # ...and persisted on the approved intent the node will consume.
    persisted = _intent_order_plan(migrated_db, payload["intent_id"])
    assert persisted["protection_policy"] == "stop_only"


@pytest.mark.parametrize("policy", ("waived", "deferred"))
def test_open_with_explicit_protection_policy_accepted(
    client: TestClient,
    migrated_db: str,
    policy: str,
) -> None:
    response = _post_open(
        client,
        _open_body(
            f"wp-e-explicit-{policy}",
            take_profits=[],
            protection_policy=policy,
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["order_plan"]["protection_policy"] == policy
    persisted = _intent_order_plan(migrated_db, payload["intent_id"])
    assert persisted["protection_policy"] == policy


def test_open_with_invalid_protection_policy_rejected(
    client: TestClient,
    migrated_db: str,
) -> None:
    response = _post_open(
        client,
        _open_body(
            "wp-e-bad-policy",
            take_profits=[],
            protection_policy="everything",
        ),
    )

    assert response.status_code in (400, 422)
    assert "protection_policy" in json.dumps(response.json())
    assert _intent_count(migrated_db) == 0


def test_open_with_take_profits_is_unaffected_by_the_gate(
    client: TestClient,
) -> None:
    response = _post_open(
        client,
        _open_body("wp-e-with-tp", take_profits=[1.7]),
    )

    assert response.status_code == 200


# --- 改动点 4: semantic dedup on entry price --------------------------------


def test_same_entry_price_warns_and_different_price_stays_silent(
    client: TestClient,
) -> None:
    """Owner-operated account (2026-08-28 directive): dedup is advisory —
    the duplicate proceeds but the response names the existing intent."""
    first = _post_open(
        client,
        _open_body("wp-e-dup-1", take_profits=[1.7], price=ENTRY_PRICE),
    )
    assert first.status_code == 200
    first_intent_id = first.json()["intent_id"]

    duplicate = _post_open(
        client,
        _open_body("wp-e-dup-2", take_profits=[1.7], price=ENTRY_PRICE),
    )
    assert duplicate.status_code == 200
    dup_payload = duplicate.json()
    assert dup_payload["intent_id"] != first_intent_id
    warnings = dup_payload["warnings"]
    assert any("duplicate_open_intent" in w for w in warnings)
    # The warning must carry the already-existing intent_id.
    assert any(first_intent_id in w for w in warnings)

    # 2026-08-25 regression: 1.55 and 1.459 are different operations —
    # no duplicate warning for a different entry price.
    different_price = _post_open(
        client,
        _open_body("wp-e-dup-3", take_profits=[1.7], price=OTHER_ENTRY_PRICE),
    )
    assert different_price.status_code == 200
    assert different_price.json()["intent_id"] != first_intent_id
    assert not any(
        "duplicate_open_intent" in w
        for w in different_price.json().get("warnings", [])
    )


def test_allow_duplicate_true_silences_price_dedup_warning(
    client: TestClient,
) -> None:
    first = _post_open(
        client,
        _open_body("wp-e-allowdup-1", take_profits=[1.7], price=ENTRY_PRICE),
    )
    assert first.status_code == 200

    bypass = _post_open(
        client,
        _open_body(
            "wp-e-allowdup-2",
            take_profits=[1.7],
            price=ENTRY_PRICE,
            allow_duplicate=True,
        ),
    )
    assert bypass.status_code == 200
    assert bypass.json()["intent_id"] != first.json()["intent_id"]
    assert bypass.json()["replay"] is False
    assert not any(
        "duplicate_open_intent" in w
        for w in bypass.json().get("warnings", [])
    )


def test_expired_approved_intent_does_not_block_a_new_open(
    client: TestClient,
    migrated_db: str,
) -> None:
    first = _post_open(
        client,
        _open_body("wp-e-expired-1", take_profits=[1.7], price=ENTRY_PRICE),
    )
    assert first.status_code == 200
    first_intent_id = first.json()["intent_id"]

    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trade_intents "
            "SET valid_until = now() - interval '1 minute' "
            "WHERE intent_id=%s",
            (first_intent_id,),
        )

    retry = _post_open(
        client,
        _open_body("wp-e-expired-2", take_profits=[1.7], price=ENTRY_PRICE),
    )
    assert retry.status_code == 200
    assert retry.json()["intent_id"] != first_intent_id
