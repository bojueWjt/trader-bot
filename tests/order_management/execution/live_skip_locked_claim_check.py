from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from uuid import uuid4

import psycopg2

from order_management.execution_jobs import claim_next_execution_job


def main() -> int:
    url = os.environ.get("DATABASE_URL", "postgresql:///om_v3_om4")
    account_id = "acct-om4-live-claim-script"
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    job_ids = [str(uuid4()), str(uuid4())]

    setup = psycopg2.connect(url)
    try:
        setup.autocommit = True
        with setup.cursor() as cur:
            cur.execute("DELETE FROM execution_jobs WHERE account_id=%s", (account_id,))
            for index, job_id in enumerate(job_ids):
                cur.execute(
                    """
                    INSERT INTO execution_jobs (
                        execution_job_id, account_id, instrument_id, venue_symbol,
                        action, status, request_id, idempotency_key, payload
                    )
                    VALUES (%s,%s,'BTCUSDT-PERP.BINANCE','BTCUSDT-PERP.BINANCE',
                            'open_position','pending',%s,%s,'{}'::jsonb)
                    """,
                    (job_id, account_id, f"req-live-{index}", f"live-claim-key-{index}"),
                )
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    claimed: list[tuple[str, str]] = []

    def worker(worker_id: str) -> None:
        conn = psycopg2.connect(url)
        try:
            barrier.wait(timeout=10)
            result = claim_next_execution_job(conn, worker_id=worker_id, now=now)
            if result is not None:
                claimed.append((worker_id, result.execution_job_id))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    cleanup = psycopg2.connect(url)
    try:
        cleanup.autocommit = True
        with cleanup.cursor() as cur:
            cur.execute("DELETE FROM execution_jobs WHERE account_id=%s", (account_id,))
    finally:
        cleanup.close()

    print(f"inserted={sorted(job_ids)}")
    print(f"claimed={sorted(claimed)}")
    print(f"distinct_claims={len({job_id for _, job_id in claimed})}")
    return 0 if len(claimed) == 2 and len({job_id for _, job_id in claimed}) == 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
