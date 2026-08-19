#!/srv/trader-v3/.venv-cp/bin/python
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg2.extras


RUNNER_PATH = Path(
    "/srv/trader-v3/account-a-canary/tools/"
    "jp24_live_four_account_20260818.py"
)
LEDGER_PATH = Path(
    "/var/lib/trader-v3/account-b-live-permit-ledger.json"
)
ARCHIVE_ROOT = Path(
    "/srv/trader-staging/"
    "jp24-evidence-refresh-hotfix-20260818T0800Z/"
    "account-b-ledger-rotation"
)
RELEASE_ID = (
    "37b5b1e5104b5594e82ed7127dfafc8ae29450d0a39223b36cf4c455a60f02a7"
)
IMAGE_DIGEST = (
    "sha256:"
    "f65bc2f2ddaa056480aa770695aa4b8779ddbd189b496485398662a8172c0973"
)
EXPECTED_SCHEMA = "trader-v3-account-b-live-permit-ledger/v1"
EXPECTED_STORE_ID = "trader-v3-account-b-live-permit-store/v1"
EXPECTED_FAILED_PERMIT_ID = "bda85655-74f3-4309-857f-c7b429ff5679"
FAILED_REPORTS = (
    Path(
        "/srv/trader-v3/account-a-canary/evidence/"
        "jp24-live-account-b-20260818T074629Z/"
        "account-b/live-trade-report.json"
    ),
    Path(
        "/srv/trader-v3/account-a-canary/evidence/"
        "jp24-live-account-b-20260818T080214Z/"
        "account-b/live-trade-report.json"
    ),
)


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "jp24_runner_for_ledger_rotation",
        RUNNER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load jp24 rollout runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def write_exclusive(path: Path, payload: bytes, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_replace_json(path: Path, payload: dict) -> None:
    data = (
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    ).encode("ascii")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def require_file(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"required regular file is missing: {path}")
    return path.read_bytes()


def wait_for_fleet_fresh(conn, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    expected_accounts = (
        "account-a",
        "account-b",
        "account-c",
        "account-d",
    )
    last_state = []
    while True:
        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor,
        ) as cursor:
            cursor.execute(
                """
                SELECT account_id,
                       status,
                       release_id,
                       image_digest,
                       extract(
                           epoch FROM clock_timestamp() - last_seen_at
                       ) AS heartbeat_age_seconds
                FROM node_heartbeats
                WHERE account_id = ANY(%s)
                ORDER BY account_id
                """,
                (list(expected_accounts),),
            )
            last_state = [dict(row) for row in cursor.fetchall()]
        accounts_match = tuple(
            row["account_id"] for row in last_state
        ) == expected_accounts
        state_is_fresh = accounts_match
        for row in last_state:
            age = float(row["heartbeat_age_seconds"])
            if row["status"] != "HALTED":
                state_is_fresh = False
            if row["release_id"] != RELEASE_ID:
                state_is_fresh = False
            if row["image_digest"] != IMAGE_DIGEST:
                state_is_fresh = False
            if age < -1 or age > 2:
                state_is_fresh = False
        if state_is_fresh:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "fleet did not become fresh before fence acquisition: "
                + json.dumps(json_safe(last_state), sort_keys=True)
            )
        time.sleep(0.25)


def main() -> int:
    runner = load_runner()
    lock_fd = -1
    owner_token = ""
    fence_acquired = False
    fence_id = str(uuid4())
    conn = runner.db_connect()
    try:
        lock_fd, owner_token = runner.acquire_operation_lock()
        wait_for_fleet_fresh(conn)
        runner.acquire_maintenance_fence(conn, fence_id, owner_token)
        fence_acquired = True
        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor,
        ) as cursor:
            cursor.execute(
                """
                SELECT phase, phase_version
                FROM reviewed_release_rollouts
                WHERE release_id=%s
                FOR SHARE
                """,
                (RELEASE_ID,),
            )
            rollout = cursor.fetchone()
            if rollout is None or rollout["phase"] != "account_b_rollout":
                raise RuntimeError("rollout is not at account_b_rollout")

            cursor.execute(
                """
                SELECT account_id,
                       status,
                       release_id,
                       image_digest,
                       heartbeat_sequence,
                       positions,
                       regular_orders,
                       algo_orders,
                       extract(
                           epoch FROM clock_timestamp() - last_seen_at
                       ) AS heartbeat_age_seconds
                FROM node_heartbeats
                WHERE account_id = ANY(%s)
                ORDER BY account_id
                FOR SHARE
                """,
                (list(runner.FLEET_ACCOUNTS),),
            )
            heartbeats = [dict(row) for row in cursor.fetchall()]
            if tuple(
                row["account_id"] for row in heartbeats
            ) != runner.FLEET_ACCOUNTS:
                raise RuntimeError("fleet heartbeat set mismatch")
            for heartbeat in heartbeats:
                account_id = str(heartbeat["account_id"])
                if heartbeat["status"] != "HALTED":
                    raise RuntimeError(f"{account_id} is not HALTED")
                if heartbeat["release_id"] != RELEASE_ID:
                    raise RuntimeError(f"{account_id} release mismatch")
                if heartbeat["image_digest"] != IMAGE_DIGEST:
                    raise RuntimeError(f"{account_id} image mismatch")
                age = float(heartbeat["heartbeat_age_seconds"])
                if age < -1 or age > 5:
                    raise RuntimeError(
                        f"{account_id} heartbeat is stale: {age}"
                    )
            account_b = heartbeats[1]
            for field_name in (
                "positions",
                "regular_orders",
                "algo_orders",
            ):
                if account_b[field_name]:
                    raise RuntimeError(
                        f"account-b {field_name} is not empty"
                    )

            cursor.execute(
                """
                SELECT incident_id::text,
                       account_id,
                       severity,
                       status
                FROM production_incidents
                WHERE status='open'
                  AND severity IN ('P0', 'P1')
                FOR SHARE
                """
            )
            incidents = [dict(row) for row in cursor.fetchall()]
            if incidents:
                raise RuntimeError("open P0/P1 incident blocks rotation")

            cursor.execute(
                """
                SELECT permit_id::text,
                       status,
                       consumed_open_count,
                       issued_at,
                       armed_at,
                       consumed_at,
                       closed_at
                FROM live_canary_permits
                WHERE account_id='account-b'
                  AND release_id=%s
                ORDER BY issued_at
                FOR SHARE
                """,
                (RELEASE_ID,),
            )
            permits = [dict(row) for row in cursor.fetchall()]
            active = [
                row for row in permits
                if row["status"] in {"issued", "armed"}
            ]
            if active:
                raise RuntimeError("active account-b permit blocks rotation")

        current_ledger_bytes = require_file(LEDGER_PATH)
        current_ledger = json.loads(current_ledger_bytes)
        if current_ledger.get("schema_version") != EXPECTED_SCHEMA:
            raise RuntimeError("account-b ledger schema mismatch")
        if current_ledger.get("store_id") != EXPECTED_STORE_ID:
            raise RuntimeError("account-b ledger store mismatch")
        if current_ledger.get("canonical_path") != str(LEDGER_PATH):
            raise RuntimeError("account-b ledger path mismatch")
        current_records = current_ledger.get("records")
        if not isinstance(current_records, dict):
            raise RuntimeError("account-b ledger records are invalid")

        archived_ledger = ARCHIVE_ROOT / "account-b-ledger.before.json"
        audit_path = ARCHIVE_ROOT / "rotation-audit.json"
        recovering_partial_rotation = ARCHIVE_ROOT.is_dir()
        if recovering_partial_rotation:
            if audit_path.exists():
                raise RuntimeError("account-b rotation audit already exists")
            ledger_bytes = require_file(archived_ledger)
            ledger = json.loads(ledger_bytes)
            if current_records:
                raise RuntimeError(
                    "partial rotation has a non-empty current ledger"
                )
        else:
            ledger_bytes = current_ledger_bytes
            ledger = current_ledger

        if ledger.get("schema_version") != EXPECTED_SCHEMA:
            raise RuntimeError("archived account-b ledger schema mismatch")
        if ledger.get("store_id") != EXPECTED_STORE_ID:
            raise RuntimeError("archived account-b ledger store mismatch")
        if ledger.get("canonical_path") != str(LEDGER_PATH):
            raise RuntimeError("archived account-b ledger path mismatch")
        records = ledger.get("records")
        if not isinstance(records, dict) or len(records) != 1:
            raise RuntimeError("account-b ledger record set is unexpected")
        record = next(iter(records.values()))
        expected_record = (
            record.get("permit_id"),
            record.get("release_id"),
            record.get("state"),
        )
        if expected_record != (
            EXPECTED_FAILED_PERMIT_ID,
            RELEASE_ID,
            "HALTED",
        ):
            raise RuntimeError("account-b failed record differs")

        reports = []
        for path in FAILED_REPORTS:
            report_bytes = require_file(path)
            report = json.loads(report_bytes)
            reports.append(
                {
                    "path": str(path),
                    "sha256": sha256_bytes(report_bytes),
                    "passed": report.get("passed"),
                    "failure_reason": report.get("failure_reason"),
                    "finished_halted": report.get("finished_halted"),
                    "target_symbol_flat": report.get(
                        "target_symbol_flat"
                    ),
                    "target_symbol_regular_orders_zero": report.get(
                        "target_symbol_regular_orders_zero"
                    ),
                    "target_symbol_algo_orders_zero": report.get(
                        "target_symbol_algo_orders_zero"
                    ),
                }
            )

        fresh_ledger = {
            "schema_version": EXPECTED_SCHEMA,
            "store_id": EXPECTED_STORE_ID,
            "canonical_path": str(LEDGER_PATH),
            "records": {},
        }
        fresh_bytes = (
            json.dumps(fresh_ledger, indent=2, sort_keys=True) + "\n"
        ).encode("ascii")
        expected_fresh_sha256 = sha256_bytes(fresh_bytes)
        if recovering_partial_rotation:
            fresh_sha256 = sha256_file(LEDGER_PATH)
            if fresh_sha256 != expected_fresh_sha256:
                raise RuntimeError(
                    "partial rotation current ledger hash mismatch"
                )
        else:
            ARCHIVE_ROOT.mkdir(parents=True, exist_ok=False)
            ARCHIVE_ROOT.chmod(0o700)
            write_exclusive(archived_ledger, ledger_bytes, 0o400)
            atomic_replace_json(LEDGER_PATH, fresh_ledger)
            fresh_sha256 = sha256_file(LEDGER_PATH)
            if fresh_sha256 != expected_fresh_sha256:
                raise RuntimeError("fresh account-b ledger hash mismatch")
        audit = {
            "schema_version": (
                "trader-v3-account-b-ledger-rotation-audit/v1"
            ),
            "rotated_at": datetime.now(timezone.utc).isoformat(),
            "recovered_partial_rotation": recovering_partial_rotation,
            "release_id": RELEASE_ID,
            "rollout_phase": rollout["phase"],
            "phase_version": rollout["phase_version"],
            "ledger_path": str(LEDGER_PATH),
            "archived_ledger_path": str(archived_ledger),
            "archived_ledger_sha256": sha256_bytes(ledger_bytes),
            "fresh_ledger_sha256": fresh_sha256,
            "archived_record": {
                "permit_id": record.get("permit_id"),
                "release_id": record.get("release_id"),
                "state": record.get("state"),
                "evidence_sha256": record.get("evidence_sha256"),
            },
            "permits": json_safe(permits),
            "failed_reports": reports,
            "fleet_heartbeats": [
                {
                    "account_id": row["account_id"],
                    "status": row["status"],
                    "heartbeat_sequence": row["heartbeat_sequence"],
                    "heartbeat_age_seconds": round(
                        float(row["heartbeat_age_seconds"]),
                        6,
                    ),
                }
                for row in heartbeats
            ],
            "open_p0_p1_incidents": [],
            "reason": (
                "archive failed HALTED account-b journal so a new "
                "single-use permit can retry the same reviewed release"
            ),
        }
        runner.write_json(audit_path, audit)
        print(
            json.dumps(
                {
                    "archive_root": str(ARCHIVE_ROOT),
                    "archived_ledger_sha256": (
                        audit["archived_ledger_sha256"]
                    ),
                    "fresh_ledger_sha256": fresh_sha256,
                    "audit_sha256": sha256_file(audit_path),
                    "permit_count": len(permits),
                    "heartbeat_sequences": {
                        row["account_id"]: row["heartbeat_sequence"]
                        for row in heartbeats
                    },
                },
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        if fence_acquired:
            try:
                runner.release_maintenance_fence(
                    conn,
                    fence_id,
                    owner_token,
                    "account-b failed ledger archived",
                )
            except Exception as exc:
                print(
                    f"FENCE release_failed fence_id={fence_id} "
                    f"error={exc}",
                    flush=True,
                )
        conn.close()
        if lock_fd >= 0:
            runner.release_operation_lock(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
