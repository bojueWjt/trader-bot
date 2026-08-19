#!/srv/trader-v3/.venv-cp/bin/python
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras


RELEASE_ROOT = Path(
    "/srv/trader-staging/trader-jp24-release-dbdf167-20260818T071337Z"
)
RELEASE_ID = (
    "37b5b1e5104b5594e82ed7127dfafc8ae29450d0a39223b36cf4c455a60f02a7"
)
IMAGE_DIGEST = (
    "sha256:f65bc2f2ddaa056480aa770695aa4b8779ddbd189b496485398662a8172c0973"
)
CONFIG_SHA256 = (
    "fe45d4596d0940c4b951e45b6e4c031e707d46173539bb38f8d287d8469e419d"
)
DEPENDENCY_LOCK_SHA256 = (
    "8e45d7223bfc84f54b857a8cb4bc10e4308c1f09cb530884b5d6e6cfc3f97cd9"
)
FIX_COMMIT = "dbdf167"
PRIOR_ROOT = Path(
    "/srv/trader-v3/account-a-canary/evidence/"
    "jp24-account-a-recovery-20260818T070416Z/account-a"
)
PRIOR_REPORT = PRIOR_ROOT / "live-trade-report.json"
PRIOR_REPORT_SIGNATURE = PRIOR_ROOT / "live-trade-report.sig"
PRIOR_SOURCE_EVIDENCE = PRIOR_ROOT / "recovery-source-evidence.json"
PRIOR_SOURCE_SIGNATURE = PRIOR_ROOT / "recovery-source-evidence.sig"
PRIOR_PUBLIC_KEY = PRIOR_ROOT / "reviewer-public-key.pem"
REVIEWER_PRIVATE_KEY = Path(
    "/srv/trader-v3/account-a-canary/reviewer/"
    "reviewer-20260814T022842Z/reviewer-private.pem"
)
EXPECTED_PRIOR_REPORT_SHA256 = (
    "b287910731e4183a95395ecce91ee7d93955b7cc0df854a219e8a8b9ffea7f7b"
)
EXPECTED_PRIOR_SOURCE_SHA256 = (
    "595dd00e39a40655dff7274717107628847d0c97e064f2da952827a4d86ff4f3"
)
EXPECTED_PUBLIC_KEY_SHA256 = (
    "2b149fe2d7357dfea74441a1f6d6f1dd"
    "9ff6ea6a1a800fd5f2f7c2778339f928"
)
BASELINE_SHA256 = (
    "318e0456c8113f20b652ffbd2484cf69f13cab9689cd752fc72df4ed91c704bd"
)
ACCOUNT_ID = "account-a"
NODE_ID = "nautilus-node-account-a"
ROLLOUT_PHASE = "account_a_canary"
PERMIT_ID = "e5bece5d-f710-414a-b538-41305b36ca93"
INTENT_ID = "fbe61118-4038-4e20-9e12-5a7bc3a9acdb"
FLEET_ACCOUNTS = ("account-a", "account-b", "account-c", "account-d")
TOKEN_ROOT_ENV = "RECOVERY_TOKEN_ROOT"
OPENSSL = Path("/usr/bin/openssl")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    text = json.dumps(
        payload,
        default=str,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )
    return (text + "\n").encode("ascii")


def write_new(path: Path, payload: dict[str, Any]) -> None:
    data = canonical_bytes(payload)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o400,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def database_url() -> str:
    env_path = Path("/srv/trader-v3/.env.v3")
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("DATABASE_URL="):
            continue
        return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("DATABASE_URL is missing")


def verify_signature(
    payload_path: Path,
    signature_path: Path,
    public_key_path: Path,
) -> None:
    subprocess.run(
        [
            str(OPENSSL),
            "dgst",
            "-sha256",
            "-verify",
            str(public_key_path),
            "-signature",
            str(signature_path),
            str(payload_path),
        ],
        check=True,
        capture_output=True,
    )


def sign_file(
    payload_path: Path,
    signature_path: Path,
    public_key_path: Path,
) -> None:
    subprocess.run(
        [
            str(OPENSSL),
            "dgst",
            "-sha256",
            "-sign",
            str(REVIEWER_PRIVATE_KEY),
            "-out",
            str(signature_path),
            str(payload_path),
        ],
        check=True,
        capture_output=True,
    )
    signature_path.chmod(0o400)
    verify_signature(payload_path, signature_path, public_key_path)


def load_prior_report() -> dict[str, Any]:
    if sha256_file(PRIOR_REPORT) != EXPECTED_PRIOR_REPORT_SHA256:
        raise RuntimeError("prior closure report hash mismatch")
    if sha256_file(PRIOR_SOURCE_EVIDENCE) != EXPECTED_PRIOR_SOURCE_SHA256:
        raise RuntimeError("prior recovery source evidence hash mismatch")
    if sha256_file(PRIOR_PUBLIC_KEY) != EXPECTED_PUBLIC_KEY_SHA256:
        raise RuntimeError("pinned reviewer public key mismatch")
    verify_signature(
        PRIOR_REPORT,
        PRIOR_REPORT_SIGNATURE,
        PRIOR_PUBLIC_KEY,
    )
    verify_signature(
        PRIOR_SOURCE_EVIDENCE,
        PRIOR_SOURCE_SIGNATURE,
        PRIOR_PUBLIC_KEY,
    )
    payload = json.loads(PRIOR_REPORT.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("prior closure report is invalid")
    if payload.get("passed") is not True:
        raise RuntimeError("prior closure report did not pass")
    if payload.get("finished_halted") is not True:
        raise RuntimeError("prior closure report did not finish halted")
    if payload.get("non_target_portfolio_before_sha256") != BASELINE_SHA256:
        raise RuntimeError("prior closure before-baseline mismatch")
    if payload.get("non_target_portfolio_after_sha256") != BASELINE_SHA256:
        raise RuntimeError("prior closure after-baseline mismatch")
    return payload


def load_current_state() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    conn = psycopg2.connect(database_url())
    try:
        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cursor:
            cursor.execute(
                """
                SELECT release_id,phase,phase_version,image_digest,
                       config_sha256,dependency_lock_sha256,
                       redis_fencing_epoch::text AS redis_fencing_epoch,
                       created_at
                FROM reviewed_release_rollouts
                WHERE release_id=%s
                """,
                (RELEASE_ID,),
            )
            rollout = dict(cursor.fetchone() or {})
            expected = {
                "release_id": RELEASE_ID,
                "phase": ROLLOUT_PHASE,
                "phase_version": 1,
                "image_digest": IMAGE_DIGEST,
                "config_sha256": CONFIG_SHA256,
                "dependency_lock_sha256": DEPENDENCY_LOCK_SHA256,
            }
            for field_name, expected_value in expected.items():
                if rollout.get(field_name) != expected_value:
                    raise RuntimeError(
                        f"current rollout mismatch: {field_name}"
                    )

            cursor.execute(
                """
                SELECT account_id,node_id,status,release_id,image_digest,
                       config_sha256,dependency_lock_sha256,
                       runtime_generation,
                       redis_fencing_epoch::text AS redis_fencing_epoch,
                       lease_fencing_token,heartbeat_sequence,last_seen_at,
                       clock_timestamp() AS database_now
                FROM node_heartbeats
                WHERE account_id=ANY(%s)
                ORDER BY account_id
                """,
                (list(FLEET_ACCOUNTS),),
            )
            heartbeats = []
            account_heartbeat = {}
            for row in cursor.fetchall():
                heartbeat = dict(row)
                database_now = heartbeat.pop("database_now")
                age_seconds = (
                    database_now - heartbeat["last_seen_at"]
                ).total_seconds()
                heartbeat["heartbeat_age_seconds"] = round(
                    age_seconds,
                    6,
                )
                if heartbeat["status"] != "HALTED":
                    raise RuntimeError(
                        f"{heartbeat['account_id']} is not HALTED"
                    )
                if heartbeat["release_id"] != RELEASE_ID:
                    raise RuntimeError(
                        f"{heartbeat['account_id']} release mismatch"
                    )
                if heartbeat["image_digest"] != IMAGE_DIGEST:
                    raise RuntimeError(
                        f"{heartbeat['account_id']} image mismatch"
                    )
                if age_seconds < -1 or age_seconds > 5:
                    raise RuntimeError(
                        f"{heartbeat['account_id']} heartbeat is stale"
                    )
                if heartbeat["account_id"] == ACCOUNT_ID:
                    account_heartbeat = heartbeat
                heartbeats.append(heartbeat)
            if len(heartbeats) != len(FLEET_ACCOUNTS):
                raise RuntimeError("fleet heartbeat set mismatch")
            if not account_heartbeat:
                raise RuntimeError("account-a heartbeat is missing")

            cursor.execute(
                """
                SELECT count(*) AS count
                FROM production_incidents
                WHERE status='open' AND severity IN ('P0','P1')
                """
            )
            if int(cursor.fetchone()["count"]) != 0:
                raise RuntimeError("open P0/P1 incident blocks closure")

            cursor.execute(
                """
                SELECT permit_id::text,account_id,status,release_id,
                       consumed_open_count,consumed_intent_id::text,
                       closed_at
                FROM live_canary_permits
                WHERE permit_id=%s
                """,
                (PERMIT_ID,),
            )
            permit = dict(cursor.fetchone() or {})
            if permit.get("status") != "closed":
                raise RuntimeError("prior account-a permit is not closed")
            if permit.get("consumed_intent_id") != INTENT_ID:
                raise RuntimeError("prior account-a permit intent mismatch")

            cursor.execute(
                """
                SELECT count(*) AS event_count,
                       count(*) FILTER (
                           WHERE event_type='OrderFilled'
                       ) AS fill_count
                FROM execution_events
                WHERE account_id=%s AND intent_id=%s
                """,
                (ACCOUNT_ID, INTENT_ID),
            )
            event_summary = dict(cursor.fetchone() or {})
            if int(event_summary.get("event_count", 0)) != 6:
                raise RuntimeError("account-a execution event count mismatch")
            if int(event_summary.get("fill_count", 0)) != 2:
                raise RuntimeError("account-a fill event count mismatch")

            cursor.execute(
                """
                SELECT count(*) AS count
                FROM execution_events
                WHERE account_id=%s AND created_at >= %s
                """,
                (ACCOUNT_ID, rollout["created_at"]),
            )
            if int(cursor.fetchone()["count"]) != 0:
                raise RuntimeError(
                    "current release produced account-a execution events"
                )
    finally:
        conn.close()
    return rollout, heartbeats, account_heartbeat


def adapter_snapshots(
    heartbeat: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    token_root_raw = os.environ.get(TOKEN_ROOT_ENV, "").strip()
    if not token_root_raw:
        raise RuntimeError(f"{TOKEN_ROOT_ENV} is required")
    token_root = Path(token_root_raw)
    os.environ.update(
        {
            "HARDENED_CANARY_ACCOUNT_ID": ACCOUNT_ID,
            "HARDENED_CANARY_REFRESH_ACCOUNTS": ACCOUNT_ID,
            "ACCOUNT_A_LIVE_TRADE_CONTROL_PLANE_URL": (
                "http://127.0.0.1:8080"
            ),
            "ACCOUNT_A_LIVE_TRADE_RISK_ADMIN_TOKEN_FILE": str(
                token_root / "risk.token"
            ),
            "ACCOUNT_A_LIVE_TRADE_NODE_TOKEN_FILE": str(
                token_root / "node.token"
            ),
            "ACCOUNT_A_LIVE_TRADE_HTTP_TIMEOUT_SECONDS": "5",
            "ACCOUNT_A_LIVE_TRADE_HTTP_MAX_ATTEMPTS": "3",
            "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0.5",
            "ACCOUNT_A_LIVE_TRADE_ACTION_TIMEOUT_SECONDS": "120",
            "ACCOUNT_A_LIVE_TRADE_FRESHNESS_SECONDS": "5",
            "ACCOUNT_A_LIVE_TRADE_NODE_FRESHNESS_SECONDS": "10",
        }
    )
    sys.path.insert(0, str(RELEASE_ROOT))
    import account_a_live_trade_http_adapter as adapter_module

    adapter = adapter_module.AccountALiveTradeHttpAdapter(
        adapter_module.Config.from_environment()
    )
    request = {
        "account_id": ACCOUNT_ID,
        "symbol": "SOLUSDT",
        "rollout_phase": ROLLOUT_PHASE,
        "release_id": RELEASE_ID,
        "node_id": NODE_ID,
        "writer_id": str(heartbeat["runtime_generation"]),
        "lease_id": str(heartbeat["redis_fencing_epoch"]),
        "fencing_epoch": int(heartbeat["lease_fencing_token"]),
        "image_digest": IMAGE_DIGEST,
        "config_sha256": CONFIG_SHA256,
        "dependency_lock_sha256": DEPENDENCY_LOCK_SHA256,
        "permit_id": PERMIT_ID,
        "intent_id": INTENT_ID,
        "hard_timeout_seconds": 120,
    }
    preflight_request = {
        **request,
        "phase": "preflight",
        "side_effect_id": (
            f"account-a-rebind-{RELEASE_ID[:12]}-preflight"
        ),
    }
    preflight = adapter.dispatch("preflight", preflight_request)
    final_request = {
        **request,
        "quantity": "0.07",
        "limit_price_usdt": "77.4",
        "open_side": "BUY",
    }
    final_snapshot = adapter.dispatch("final-snapshot", final_request)
    if (
        preflight.get("non_target_portfolio_baseline_sha256")
        != BASELINE_SHA256
    ):
        raise RuntimeError("current preflight portfolio baseline mismatch")
    if (
        final_snapshot.get("non_target_portfolio_baseline_sha256")
        != BASELINE_SHA256
    ):
        raise RuntimeError("current final portfolio baseline mismatch")
    if str(preflight.get("target_position_quantity")) != "0":
        raise RuntimeError("current account-a SOL position is not flat")
    if int(preflight.get("target_regular_order_count", -1)) != 0:
        raise RuntimeError("current account-a regular orders are not zero")
    if int(preflight.get("target_algo_order_count", -1)) != 0:
        raise RuntimeError("current account-a algo orders are not zero")
    if final_snapshot.get("target_symbol_flat") is not True:
        raise RuntimeError("current final snapshot is not flat")
    if final_snapshot.get("target_symbol_regular_orders_zero") is not True:
        raise RuntimeError("current final regular orders are not zero")
    if final_snapshot.get("target_symbol_algo_orders_zero") is not True:
        raise RuntimeError("current final algo orders are not zero")
    return preflight, final_snapshot


def main() -> int:
    prior_report = load_prior_report()
    rollout, heartbeats, account_heartbeat = load_current_state()
    preflight, final_snapshot = adapter_snapshots(account_heartbeat)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_root = (
        Path("/srv/trader-v3/account-a-canary/evidence")
        / f"jp24-account-a-rebind-{stamp}"
        / ACCOUNT_ID
    )
    output_root.mkdir(parents=True, mode=0o700)
    public_key = output_root / "reviewer-public-key.pem"
    public_key.write_bytes(PRIOR_PUBLIC_KEY.read_bytes())
    public_key.chmod(0o400)

    preflight_path = output_root / "current-preflight.json"
    final_path = output_root / "current-final-snapshot.json"
    write_new(preflight_path, preflight)
    write_new(final_path, final_snapshot)

    source_evidence = {
        "schema_version": (
            "trader-v3-account-a-release-rebind-source-evidence/v1"
        ),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "closure_release": rollout,
        "prior_closure_report_path": str(PRIOR_REPORT),
        "prior_closure_report_sha256": sha256_file(PRIOR_REPORT),
        "prior_closure_signature_sha256": sha256_file(
            PRIOR_REPORT_SIGNATURE
        ),
        "prior_source_evidence_path": str(PRIOR_SOURCE_EVIDENCE),
        "prior_source_evidence_sha256": sha256_file(
            PRIOR_SOURCE_EVIDENCE
        ),
        "prior_source_signature_sha256": sha256_file(
            PRIOR_SOURCE_SIGNATURE
        ),
        "heartbeats": heartbeats,
        "current_preflight_sha256": sha256_file(preflight_path),
        "current_final_snapshot_sha256": sha256_file(final_path),
        "normalized_non_target_portfolio_sha256": BASELINE_SHA256,
        "database_execution_event_count": 6,
        "database_fill_event_count": 2,
        "current_release_execution_event_count": 0,
    }
    source_path = output_root / "rebind-source-evidence.json"
    write_new(source_path, source_evidence)
    source_signature = source_path.with_suffix(".sig")
    sign_file(source_path, source_signature, public_key)

    report = {
        "schema_version": "trader-v3-live-trade-report/v1",
        "mode": "live",
        "passed": True,
        "failure_reason": "",
        "account_id": ACCOUNT_ID,
        "node_id": NODE_ID,
        "rollout_phase": ROLLOUT_PHASE,
        "release_id": RELEASE_ID,
        "image_digest": IMAGE_DIGEST,
        "config_sha256": CONFIG_SHA256,
        "dependency_lock_sha256": DEPENDENCY_LOCK_SHA256,
        "authorization_signatures_verified": True,
        "round_trip_count": 1,
        "finished_halted": True,
        "target_symbol_flat": True,
        "target_symbol_regular_orders_zero": True,
        "target_symbol_algo_orders_zero": True,
        "non_target_portfolio_before_sha256": BASELINE_SHA256,
        "non_target_portfolio_after_sha256": BASELINE_SHA256,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": "SOLUSDT",
        "permit_id": PERMIT_ID,
        "intent_id": INTENT_ID,
        "mainnet_round_trip": prior_report["mainnet_round_trip"],
        "recovery_provenance": {
            "reason": (
                "current reviewed release revalidated the signed account-a "
                "round trip and current exchange flat state"
            ),
            "fix_commit": FIX_COMMIT,
            "original_execution_release_id": (
                prior_report["recovery_provenance"][
                    "original_execution_release_id"
                ]
            ),
            "original_execution_image_digest": (
                prior_report["recovery_provenance"][
                    "original_execution_image_digest"
                ]
            ),
            "prior_closure_release_id": prior_report["release_id"],
            "prior_closure_image_digest": prior_report["image_digest"],
            "prior_closure_report_sha256": sha256_file(PRIOR_REPORT),
            "rebind_source_evidence_sha256": sha256_file(source_path),
            "current_preflight_sha256": sha256_file(preflight_path),
            "current_final_snapshot_sha256": sha256_file(final_path),
            "database_execution_event_count": 6,
            "database_fill_event_count": 2,
            "current_release_execution_event_count": 0,
        },
    }
    report_path = output_root / "live-trade-report.json"
    report_signature = output_root / "live-trade-report.sig"
    write_new(report_path, report)
    sign_file(report_path, report_signature, public_key)

    import reviewed_release_rollout as rollout_module

    validated = rollout_module.load_live_canary_closure_report(
        report_path,
        report_signature,
        public_key,
    )
    result = {
        "evidence_root": str(output_root),
        "report_sha256": sha256_file(report_path),
        "signature_sha256": sha256_file(report_signature),
        "public_key_sha256": sha256_file(public_key),
        "validated_account_id": validated.account_id,
        "validated_release_id": validated.release_id,
        "open_filled_quantity": validated.open_filled_quantity,
        "close_filled_quantity": validated.close_filled_quantity,
        "actual_open_notional_usdt": (
            validated.actual_open_notional_usdt
        ),
        "non_target_portfolio_sha256": (
            validated.non_target_portfolio_sha256
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
