"""C-04/C-05 concurrent real-Hermes replay.

N worker threads, each with its own DB connection, claim pending messages via the
A-04 SKIP-LOCKED lease and process them through REAL Hermes — so the Hermes calls run
in parallel. The lease makes concurrent claims safe (no double-processing).

  CONCURRENCY=8 DATABASE_URL=... HERMES_API_URL=... HERMES_API_KEY=... HERMES_MODEL=... \
    python concurrent_replay.py
"""
import os
import sys
import threading
import time
from collections import Counter

import psycopg2

import worker
from connection import database_url
from hermes_client import RealHermesClient
from smoke_replay import _FilesystemMediaLoader, _LiveSnapshotProvider

CONCURRENCY = int(os.environ.get("CONCURRENCY", "8"))
client = RealHermesClient()
if not client.configured:
    print("BLOCKED: HERMES_API_URL/HERMES_API_KEY/HERMES_MODEL not all set")
    raise SystemExit(1)

results: list[str] = []
lock = threading.Lock()


def worker_loop(wid: int) -> None:
    conn = psycopg2.connect(database_url())
    idle = 0
    try:
        while True:
            result = worker.process_one(
                conn,
                worker_id=f"cc-{wid}",
                client=client,
                media_loader=_FilesystemMediaLoader(),
                snapshot_provider=_LiveSnapshotProvider(conn),
            )
            if result.status == "skipped":
                idle += 1
                if idle >= 3:  # nothing left after a few tries -> done
                    break
                time.sleep(0.3)
                continue
            idle = 0
            with lock:
                results.append(result.status)
    finally:
        conn.close()


threads = [threading.Thread(target=worker_loop, args=(i,), daemon=True) for i in range(CONCURRENCY)]
for t in threads:
    t.start()
for t in threads:
    t.join()

print(f"CONCURRENT REPLAY (x{CONCURRENCY}) processed {len(results)} messages:")
for status, n in sorted(Counter(results).items(), key=lambda kv: -kv[1]):
    print(f"  {status}: {n}")
