"""GET /v1/accounts?history_hours=.. (contracts/backend-api.md §8, T1).

Covers: no-param byte-identical baseline, summed totals with correct
accounts_sampled on a bucket missing an account, and 400 on an
out-of-range/non-integer history_hours.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import read_api

ACCOUNTS = ("account-a", "account-b", "account-c", "account-d")
VIEWER = "viewer-token"
AUTH = {"Authorization": f"Bearer {VIEWER}"}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("VIEWER_TOKEN", VIEWER)
    return TestClient(read_api.app)


def _seed_account(conn, account_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts_projection (account_id, currency, equity, margin, available_balance, "
            "reconciliation_state) VALUES (%s,'USDT',1000,100,900,'healthy')",
            (account_id,),
        )
    conn.commit()


def _seed_all_accounts(conn) -> None:
    for account_id in ACCOUNTS:
        _seed_account(conn, account_id)


def _bucket_floor(dt: datetime) -> datetime:
    epoch_seconds = dt.timestamp()
    floored = (epoch_seconds // 1800) * 1800
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _insert_sample(
    conn,
    *,
    account_id: str,
    bucket_at: datetime,
    equity: str,
    available: str,
    margin: str,
    sampled_at: datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO account_equity_samples "
            "(account_id, bucket_at, equity, available, margin, sampled_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (account_id, bucket_at, equity, available, margin, sampled_at),
        )
    conn.commit()


_BASELINE_ENVELOPE_KEYS = {
    "schema_version",
    "data_source",
    "snapshot_id",
    "generated_at",
    "last_execution_event_at",
    "projection_lag_ms",
    "stale",
    "missing_nodes",
    "reconciliation_state",
}


def test_v1_accounts_without_history_hours_is_byte_identical_to_baseline(
    client: TestClient, db_conn
) -> None:
    _seed_all_accounts(db_conn)

    resp = client.get("/v1/accounts", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    # §8 additive fields must never appear unless history_hours is given —
    # the pre-§8 response shape is exactly this key set, nothing more.
    assert set(body.keys()) == _BASELINE_ENVELOPE_KEYS | {"accounts"}
    assert "data" not in body
    assert len(body["accounts"]) == len(ACCOUNTS)
    for account in body["accounts"]:
        assert set(account.keys()) == {
            "account_id",
            "currency",
            "equity",
            "available",
            "margin_used",
            "realized_pnl_today",
            "reconciliation_state",
            "updated_at",
        }


def test_v1_accounts_history_hours_sums_buckets_and_reports_accounts_sampled(
    client: TestClient, db_conn
) -> None:
    _seed_all_accounts(db_conn)

    now = datetime.now(timezone.utc)
    bucket_now = _bucket_floor(now)
    bucket_prev = bucket_now - timedelta(minutes=30)

    # Older bucket: all four accounts sampled.
    for account_id, equity, available in (
        ("account-a", "1000", "900"),
        ("account-b", "1000", "900"),
        ("account-c", "1000", "900"),
        ("account-d", "1000", "900"),
    ):
        _insert_sample(
            db_conn,
            account_id=account_id,
            bucket_at=bucket_prev,
            equity=equity,
            available=available,
            margin="50",
            sampled_at=bucket_prev,
        )

    # Newer bucket: account-d missed this cycle (mirror fetch failure).
    for account_id, equity, available in (
        ("account-a", "2000", "1800"),
        ("account-b", "2000", "1800"),
        ("account-c", "2000", "1800"),
    ):
        _insert_sample(
            db_conn,
            account_id=account_id,
            bucket_at=bucket_now,
            equity=equity,
            available=available,
            margin="50",
            sampled_at=bucket_now,
        )

    resp = client.get("/v1/accounts", params={"history_hours": "2"}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    total = body["data"]["equity_history_total"]
    meta = body["data"]["equity_history_meta"]

    assert len(total) == 2
    assert total[0]["t"] <= total[1]["t"]  # ascending

    prev_point, now_point = total[0], total[1]
    assert prev_point["accounts_sampled"] == 4
    assert prev_point["equity"] == "4000"
    assert prev_point["available"] == "3600"

    assert now_point["accounts_sampled"] == 3
    assert now_point["equity"] == "6000"
    assert now_point["available"] == "5400"

    assert meta == {
        "bucket_seconds": 1800,
        "accounts_expected": 4,
        "since": meta["since"],
        "first_sample_at": meta["first_sample_at"],
    }
    assert meta["accounts_expected"] == len(body["accounts"])


@pytest.mark.parametrize("bad_value", ["0", "999", "abc", "-1", "12.5"])
def test_v1_accounts_history_hours_rejects_invalid_values(
    client: TestClient, db_conn, bad_value: str
) -> None:
    _seed_all_accounts(db_conn)

    resp = client.get("/v1/accounts", params={"history_hours": bad_value}, headers=AUTH)

    assert resp.status_code == 400


def test_empty_history_has_no_fabricated_points(client: TestClient) -> None:
    response = client.get("/v1/accounts?history_hours=24", headers=AUTH)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["equity_history_total"] == []
    assert data["equity_history_meta"]["accounts_expected"] == 4
    assert data["equity_history_meta"]["first_sample_at"] is None


def test_missing_projection_and_unregistered_samples_cannot_fake_complete_bucket(
    client: TestClient, db_conn,
) -> None:
    bucket = _bucket_floor(datetime.now(timezone.utc))
    for account_id in (*ACCOUNTS[:3], "account-retired"):
        _seed_account(db_conn, account_id)
        _insert_sample(
            db_conn, account_id=account_id, bucket_at=bucket,
            equity="1000.00000001", available="900.00000001",
            margin="100", sampled_at=bucket,
        )
    response = client.get("/v1/accounts?history_hours=24", headers=AUTH)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["equity_history_meta"]["accounts_expected"] == 4
    assert data["equity_history_total"] == [{
        "t": bucket.isoformat(), "equity": "3000.00000003",
        "available": "2700.00000003", "accounts_sampled": 3,
    }]


def test_week_history_is_bounded_and_excludes_future_samples(
    client: TestClient, db_conn,
) -> None:
    bucket = _bucket_floor(datetime.now(timezone.utc))
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO account_equity_samples "
            "(account_id, bucket_at, equity, available, margin, sampled_at) "
            "SELECT 'account-a', t, 1000, 900, 100, t "
            "FROM generate_series(%s - interval '8 days', "
            "%s + interval '1 day', interval '30 minutes') t",
            (bucket, bucket),
        )
    db_conn.commit()
    response = client.get("/v1/accounts?history_hours=168", headers=AUTH)
    assert response.status_code == 200
    points = response.json()["data"]["equity_history_total"]
    assert len(points) == 336
    assert datetime.fromisoformat(points[0]["t"]) == bucket - timedelta(minutes=30 * 335)
    assert datetime.fromisoformat(points[-1]["t"]) == bucket


def test_equity_history_requires_reader_auth(client: TestClient) -> None:
    assert client.get("/v1/accounts?history_hours=24").status_code == 401
