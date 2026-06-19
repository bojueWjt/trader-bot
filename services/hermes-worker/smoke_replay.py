"""Real-Hermes 10-message smoke replay.

This drives the worker against the REAL Hermes endpoint over a set of real raw
messages. It is the data/infra-dependent acceptance step. If the endpoint is not
configured (HERMES_API_URL/HERMES_API_KEY/HERMES_MODEL) or fewer than 10 real
messages are available, it reports BLOCKED instead of faking a pass.

Usage:
  DATABASE_URL=... HERMES_API_URL=... HERMES_API_KEY=... HERMES_MODEL=... \
    python services/hermes-worker/smoke_replay.py --min 10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE / "queue"), str(_HERE.parents[1] / "services" / "control-plane" / "db")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Real-Hermes smoke replay")
    parser.add_argument("--min", type=int, default=10, help="minimum real messages required")
    args = parser.parse_args(argv)

    from hermes_client import RealHermesClient

    client = RealHermesClient()
    blockers = []
    if not client.configured:
        blockers.append("HERMES_API_URL/HERMES_API_KEY/HERMES_MODEL not all set")

    pending = _count_pending_raw_messages()
    if pending is None:
        blockers.append("DATABASE_URL not set / database unreachable")
    elif pending < args.min:
        blockers.append(f"only {pending} pending real messages (need >= {args.min})")

    if blockers:
        print("SMOKE STATUS: BLOCKED")
        for blocker in blockers:
            print(f"  - {blocker}")
        print(
            "To run: provide a real Hermes endpoint + >= "
            f"{args.min} real raw messages, then re-run this script."
        )
        return 0

    # configured + enough data: run the real replay (executed only in a real env)
    return _run_real_replay(client, args.min)


def _count_pending_raw_messages():
    import os

    if not os.environ.get("DATABASE_URL"):
        return None
    try:
        import psycopg2

        from connection import database_url

        with psycopg2.connect(database_url()) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM outbox_events
                WHERE aggregate_type = 'raw_message' AND status = 'pending'
                """
            )
            return cur.fetchone()[0]
    except Exception:
        return None


def _run_real_replay(client, minimum):  # pragma: no cover - requires real Hermes
    import psycopg2

    import worker
    from connection import database_url

    processed = 0
    results = []
    conn = psycopg2.connect(database_url())
    try:
        while processed < minimum:
            result = worker.process_one(
                conn,
                worker_id="smoke-replay",
                client=client,
                media_loader=_FilesystemMediaLoader(),
                snapshot_provider=_LiveSnapshotProvider(conn),
            )
            if result.status == "skipped":
                break
            processed += 1
            results.append(result.status)
        print(f"SMOKE STATUS: RAN {processed} messages -> {results}")
        return 0 if processed >= minimum else 1
    finally:
        conn.close()


class _FilesystemMediaLoader:  # pragma: no cover - real env only
    def load(self, object_key):
        import base64
        import mimetypes

        data = Path(object_key).read_bytes()
        mime = mimetypes.guess_type(object_key)[0] or "application/octet-stream"
        return mime, base64.b64encode(data).decode("ascii")


class _LiveSnapshotProvider:  # pragma: no cover - real env only
    def __init__(self, conn):
        self._conn = conn

    def current(self):
        raise RuntimeError("live SystemSnapshotV1 provider is wired by A-09 control-plane API")


if __name__ == "__main__":
    raise SystemExit(main())
