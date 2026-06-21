from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg2


ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE = ROOT / "services" / "control-plane"
CONTROL_PLANE_RISK = CONTROL_PLANE / "risk"
EXECUTION_DOMAIN = ROOT / "packages" / "execution-domain"

for _path in (CONTROL_PLANE, CONTROL_PLANE_RISK, EXECUTION_DOMAIN):
    _p = str(_path)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from risk_config import RiskConfig  # noqa: E402
from reservations import ReservationRequest, reserve_risk  # noqa: E402


ACCOUNT_ID = "acct-om3-live-check"
NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "postgresql:///om_v3_om3")
    try:
        setup = psycopg2.connect(database_url)
    except psycopg2.OperationalError as exc:
        print(f"blocked: cannot connect to {database_url}: {exc}")
        return 2

    try:
        _clean(setup)
        _seed_account(setup)
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    results: list[tuple[str, str, Decimal | None]] = []

    def worker(name: str) -> None:
        conn = psycopg2.connect(database_url)
        try:
            barrier.wait(timeout=5)
            result = reserve_risk(
                conn,
                ReservationRequest(
                    account_id=ACCOUNT_ID,
                    instrument_id="BTCUSDT",
                    intent_id=f"live-check-{name}",
                    idempotency_key=f"live-check-{name}",
                    risk_amount=Decimal("700"),
                    notional=Decimal("7000"),
                    ttl_seconds=60,
                    now=NOW,
                ),
                config=RiskConfig(max_total_open_risk_pct=Decimal("0.10")),
            )
            results.append((name, result.status, result.remaining_budget))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    verify = psycopg2.connect(database_url)
    try:
        with verify.cursor() as cur:
            cur.execute(
                """
                SELECT coalesce(sum(risk_amount), 0), count(*)
                FROM risk_reservations
                WHERE account_id=%s AND status='held'
                """,
                (ACCOUNT_ID,),
            )
            held_risk, held_count = cur.fetchone()
        print(f"results={sorted(results)}")
        print(f"held_count={held_count} held_risk={held_risk}")
        ok = sorted(status for _, status, _ in results) == ["held", "rejected"]
        ok = ok and held_count == 1 and held_risk == Decimal("700")
        print(f"over_allocated={not ok}")
        return 0 if ok else 1
    finally:
        _clean(verify)
        verify.close()


def _seed_account(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts_projection (
                account_id, currency, equity, margin, available_balance,
                projection_lag_ms, last_execution_event_at, updated_at
            )
            VALUES (%s, 'USDT', 10000, 0, 9000, 0, now(), now())
            ON CONFLICT (account_id) DO UPDATE SET
                equity=EXCLUDED.equity,
                margin=EXCLUDED.margin,
                available_balance=EXCLUDED.available_balance,
                projection_lag_ms=0,
                last_execution_event_at=now(),
                updated_at=now()
            """,
            (ACCOUNT_ID,),
        )
    conn.commit()


def _clean(conn) -> None:
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_reservations WHERE account_id=%s", (ACCOUNT_ID,))
            cur.execute("DELETE FROM positions_projection WHERE account_id=%s", (ACCOUNT_ID,))
            cur.execute("DELETE FROM accounts_projection WHERE account_id=%s", (ACCOUNT_ID,))
    finally:
        conn.autocommit = False


if __name__ == "__main__":
    raise SystemExit(main())

