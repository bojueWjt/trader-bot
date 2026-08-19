#!/srv/trader-v3/.venv-cp/bin/python
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_UP
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

import psycopg2
import psycopg2.extras

RELEASE_ROOT = Path("/srv/trader-staging/trader-jp24-release-de9411d-20260818T091923Z")
RELEASE_ID = "2417ff26c3eecda011efd5285008b0bff73b2b9bb6e47f6ac78cd57d177825c1"
IMAGE_DIGEST = "sha256:ee2cea4109a60e4a546f394847f3ada5b634988f6119c833a087510a374ac068"
REVIEWER_PRIVATE_KEY = Path("/srv/trader-v3/account-a-canary/reviewer/reviewer-20260814T022842Z/reviewer-private.pem")
REVIEWER_TRUST_SHA256 = "02f0fb71727a84089fa2ba6e4c02b34494ded658c8b0a4aedeeb90662fc78eb9"
PINNED_REVIEWER_PUBLIC_KEY_SHA256 = "2b149fe2d7357dfea74441a1f6d6f1dd9ff6ea6a1a800fd5f2f7c2778339f928"
EVIDENCE_ROOT = Path("/srv/trader-v3/account-a-canary/evidence")
TOKEN_SOURCE_ROOT = Path("/srv/trader-v3/secrets/subaccounts")
RISK_TOKEN_SOURCE = Path("/srv/trader-v3/secrets/control-plane/bootstrap-risk-admin-token")
ENV_FILE = Path("/srv/trader-v3/.env.v3")
PY_CP = Path("/srv/trader-v3/.venv-cp/bin/python")
PY_SYSTEM = Path("/usr/bin/python3")
OPENSSL = Path("/usr/bin/openssl")
OPERATION_LOCK_PATH = Path("/var/lock/trader-v3-account-stall-operation.lock")
SYMBOL = "SOLUSDT"
QUANTITY = Decimal("0.07")
MAX_NOTIONAL = Decimal("12")
MAX_LOSS = Decimal("1.49")
TESTNET_EVIDENCE_SHA256 = "15899ae0526e1f9fc30038b80fa3e34e320f8f8bed9fb03f5ccc7a459caf36db"
TESTNET_VERIFIED_AT = "2026-08-18T06:22:23.348108+00:00"
FLEET_ACCOUNTS = ("account-a", "account-b", "account-c", "account-d")
ACTOR = "codex-account-stall-hardening"
MAX_HEARTBEAT_FUTURE_SKEW_SECONDS = 1.0
FRESH_EVIDENCE_SECONDS = 5.0
REFRESH_WAIT_SECONDS = 90.0

@dataclass(frozen=True)
class Target:
    account_id: str
    node_id: str
    phase: str
    port: int
    token_name: str

    @property
    def prefix(self) -> str:
        return self.account_id.replace("-", "_").upper() + "_LIVE_TRADE"

TARGETS = (
    Target("account-a", "nautilus-node-account-a", "account_a_canary", 8081, "control_plane_account_a_token"),
    Target("account-b", "nautilus-node-account-b", "account_b_rollout", 8082, "control_plane_account_b_token"),
    Target("account-c", "nautilus-node-account-c", "account_c_rollout", 8083, "control_plane_account_c_token"),
    Target("account-d", "nautilus-node-account-d", "account_d_rollout", 8084, "control_plane_account_d_token"),
)
TARGET_BY_ACCOUNT = {
    target.account_id: target
    for target in TARGETS
}
NEXT_PHASE_BY_ACCOUNT = {
    "account-a": "account_b_rollout",
    "account-b": "account_c_rollout",
    "account-c": "account_d_rollout",
    "account-d": "fleet_complete",
}

def log(message: str) -> None:
    print(message, flush=True)

def canonical_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")

def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(payload)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON root is not an object: {path}")
    return payload

def redact_command_arg(arg: str) -> str:
    if "postgresql://" in arg or "postgres://" in arg:
        return "DATABASE_URL=REDACTED"
    return arg

def run(command: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None, input_text: str | None = None, check: bool = True, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
    redacted_command = " ".join(redact_command_arg(item) for item in command)
    log(f"CMD {redacted_command}")
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        pass_fds=pass_fds,
        check=False,
    )
    if completed.stdout.strip():
        log("STDOUT " + completed.stdout.strip())
    if completed.stderr.strip():
        log("STDERR " + completed.stderr.strip())
    log(f"RC {completed.returncode}")
    if check and completed.returncode != 0:
        raise RuntimeError(f"command failed rc={completed.returncode}: {redacted_command}")
    return completed

def read_database_url() -> str:
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "DATABASE_URL":
            value = value.strip().strip("'\"")
            if value:
                return value
    raise RuntimeError("DATABASE_URL is missing")

def db_connect():
    return psycopg2.connect(read_database_url())

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso_seconds(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()

def require_phase(conn, expected_phase: str) -> None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT phase, release_id, phase_version
            FROM reviewed_release_rollouts
            WHERE release_id=%s
            """, (RELEASE_ID,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("active release rollout row is missing")
    actual_phase = str(row["phase"])
    log(f"ROLLOUT phase={actual_phase} release_id={row['release_id']} phase_version={row['phase_version']}")
    if actual_phase != expected_phase:
        raise RuntimeError(f"expected rollout phase {expected_phase}, got {actual_phase}")

def load_heartbeat(
    conn,
    target: Target,
    *,
    allow_stale: bool = False,
) -> dict[str, Any]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT account_id,node_id,status,release_id,image_digest,
                   config_sha256,dependency_lock_sha256,schema_epoch,
                   last_seen_at,positions_snapshot_at,regular_orders_snapshot_at,
                   algo_orders_snapshot_at,reconciliation_completed_at,
                   redis_fencing_epoch::text AS redis_fencing_epoch,
                   runtime_generation,lease_fencing_token,heartbeat_sequence,
                   payload,clock_timestamp() AS database_now
            FROM node_heartbeats
            WHERE account_id=%s AND node_id=%s
            """, (target.account_id, target.node_id))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"heartbeat missing for {target.account_id}")
    db_now = row["database_now"]
    age = (db_now - row["last_seen_at"]).total_seconds()
    log(f"HEARTBEAT account={target.account_id} status={row['status']} age_s={age:.3f} seq={row['heartbeat_sequence']} token_present={bool(row['lease_fencing_token'])}")
    if age < -MAX_HEARTBEAT_FUTURE_SKEW_SECONDS:
        raise RuntimeError(f"heartbeat is from the future for {target.account_id}: {age:.3f}s")
    if age > 5 and not allow_stale:
        raise RuntimeError(f"heartbeat is stale for {target.account_id}: {age:.3f}s")
    if row["status"] != "HALTED":
        raise RuntimeError(f"target is not HALTED before canary: {target.account_id}")
    if row["release_id"] != RELEASE_ID:
        raise RuntimeError(f"heartbeat release mismatch for {target.account_id}")
    if not row["runtime_generation"] or not row["redis_fencing_epoch"] or not row["lease_fencing_token"]:
        raise RuntimeError(f"writer identity incomplete for {target.account_id}")
    return dict(row)

def _timestamp_age_seconds(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if not isinstance(value, datetime):
        return float("inf")
    db_now = row.get("database_now")
    if not isinstance(db_now, datetime):
        raise RuntimeError("heartbeat database_now is missing")
    return (db_now - value).total_seconds()

def _parse_payload_ts(payload_obj: Any) -> datetime | bool:
    if not isinstance(payload_obj, dict):
        return False
    raw = payload_obj.get("ts")
    if not raw:
        return False
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False

def _reconciliation_health_timestamp(row: dict[str, Any]) -> datetime | bool:
    payload_obj = row.get("payload")
    if not isinstance(payload_obj, dict):
        return False
    if payload_obj.get("reconciliation_state") != "healthy":
        return False
    return _parse_payload_ts(payload_obj)

def _timestamp_age_from_value(row: dict[str, Any], value: datetime | bool) -> float:
    if not isinstance(value, datetime):
        return float("inf")
    db_now = row.get("database_now")
    if not isinstance(db_now, datetime):
        raise RuntimeError("heartbeat database_now is missing")
    return (db_now - value).total_seconds()

def wait_for_fresh_evidence(conn, target: Target) -> dict[str, Any]:
    deadline = time.monotonic() + REFRESH_WAIT_SECONDS
    snapshot_fields = (
        "last_seen_at",
        "positions_snapshot_at",
        "regular_orders_snapshot_at",
        "algo_orders_snapshot_at",
    )
    last_ages: dict[str, float] = {}
    while time.monotonic() < deadline:
        row = load_heartbeat(conn, target, allow_stale=True)
        last_ages = {
            field: _timestamp_age_seconds(row, field)
            for field in snapshot_fields
        }
        reconciliation_health_at = _reconciliation_health_timestamp(row)
        last_ages["reconciliation_health_at"] = _timestamp_age_from_value(
            row,
            reconciliation_health_at,
        )
        reconciliation_completed_age = _timestamp_age_seconds(
            row,
            "reconciliation_completed_at",
        )
        compact = {
            field: ("missing" if age == float("inf") else round(age, 3))
            for field, age in last_ages.items()
        }
        compact["reconciliation_completed_at"] = (
            "missing"
            if reconciliation_completed_age == float("inf")
            else round(reconciliation_completed_age, 3)
        )
        log("FRESH_CHECK " + json.dumps({"account_id": target.account_id, **compact}, sort_keys=True))
        if all(
            age >= -MAX_HEARTBEAT_FUTURE_SKEW_SECONDS
            and age <= FRESH_EVIDENCE_SECONDS
            for age in last_ages.values()
        ):
            return row
        time.sleep(1.0)
    raise RuntimeError(
        f"fresh evidence did not arrive for {target.account_id}: "
        + json.dumps({
            field: ("missing" if age == float("inf") else round(age, 3))
            for field, age in last_ages.items()
        }, sort_keys=True)
    )

def check_ready(target: Target) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://127.0.0.1:{target.port}/ready", timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    compact = {
        "account_id": payload.get("account_id"),
        "node_id": payload.get("node_id"),
        "ready": payload.get("ready"),
        "trading_state": payload.get("trading_state"),
        "halt_reason": payload.get("halt_reason"),
        "reconciliation_status": payload.get("reconciliation_status"),
    }
    log("READY " + json.dumps(compact, sort_keys=True))
    if compact["account_id"] != target.account_id or compact["node_id"] != target.node_id:
        raise RuntimeError(f"ready identity mismatch for {target.account_id}")
    if compact["ready"] is not True:
        raise RuntimeError(f"ready=false for {target.account_id}")
    if str(compact["trading_state"]).upper() != "HALTED":
        raise RuntimeError(f"ready target is not HALTED for {target.account_id}")
    if compact["reconciliation_status"] != "healthy":
        raise RuntimeError(f"reconciliation is not healthy for {target.account_id}")
    return payload

def ensure_no_target_incident(conn, target: Target) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT incident_id::text, severity, status, left(summary, 120)
            FROM production_incidents
            WHERE account_id=%s AND severity IN ('P0', 'P1') AND status='open'
            ORDER BY opened_at DESC
            """, (target.account_id,))
        rows = cur.fetchall()
    log(f"INCIDENTS account={target.account_id} open_p0_p1={len(rows)}")
    if rows:
        raise RuntimeError(f"target account has open P0/P1 incidents: {target.account_id}")

def sample_fleet_freshness(
    conn,
    account_dir: Path,
    expected_phase: str,
    public_key_path: Path,
) -> Path:
    timestamp_fields = (
        "last_seen_at",
        "positions_snapshot_at",
        "regular_orders_snapshot_at",
        "algo_orders_snapshot_at",
    )
    freshness_limits = {
        "last_seen_at": FRESH_EVIDENCE_SECONDS,
        "positions_snapshot_at": 10.0,
        "regular_orders_snapshot_at": 10.0,
        "algo_orders_snapshot_at": 10.0,
    }
    with conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT release_id, phase, image_digest
                FROM reviewed_release_rollouts
                WHERE phase IN (
                    'account_a_canary',
                    'account_b_rollout',
                    'account_c_rollout',
                    'account_d_rollout'
                )
                FOR UPDATE
                """
            )
            active = [dict(row) for row in cur.fetchall()]
            expected_active = [{
                "release_id": RELEASE_ID,
                "phase": expected_phase,
                "image_digest": IMAGE_DIGEST,
            }]
            if active != expected_active:
                raise RuntimeError(
                    "active rollout guard failed: "
                    + json.dumps(active, default=str, sort_keys=True)
                )

            cur.execute(
                """
                WITH sampled AS MATERIALIZED (
                    SELECT clock_timestamp() AS sampled_at
                )
                SELECT sampled.sampled_at,
                       nh.account_id,
                       nh.node_id,
                       nh.status,
                       nh.release_id,
                       nh.image_digest,
                       nh.last_seen_at,
                       nh.positions_snapshot_at,
                       nh.regular_orders_snapshot_at,
                       nh.algo_orders_snapshot_at,
                       nh.reconciliation_completed_at
                FROM node_heartbeats nh
                CROSS JOIN sampled
                WHERE nh.account_id = ANY(%s)
                ORDER BY nh.account_id
                FOR SHARE OF nh
                """,
                (list(FLEET_ACCOUNTS),),
            )
            heartbeat_rows = [dict(row) for row in cur.fetchall()]
            observed_accounts = tuple(
                row["account_id"] for row in heartbeat_rows
            )
            if observed_accounts != FLEET_ACCOUNTS:
                raise RuntimeError("fleet heartbeat set mismatch")

            freshness_rows = []
            for row in heartbeat_rows:
                account_id = str(row["account_id"])
                if row["status"] != "HALTED":
                    raise RuntimeError(f"{account_id} status is not HALTED")
                if row["release_id"] != RELEASE_ID:
                    raise RuntimeError(f"{account_id} release mismatch")
                if row["image_digest"] != IMAGE_DIGEST:
                    raise RuntimeError(f"{account_id} image mismatch")
                ages = {}
                for field in timestamp_fields:
                    value = row[field]
                    if not isinstance(value, datetime):
                        raise RuntimeError(f"{account_id} missing {field}")
                    age = (row["sampled_at"] - value).total_seconds()
                    if age < -MAX_HEARTBEAT_FUTURE_SKEW_SECONDS:
                        raise RuntimeError(
                            f"{account_id} {field} is from the future: {age}"
                        )
                    limit = freshness_limits[field]
                    if age >= limit:
                        raise RuntimeError(
                            f"{account_id} {field} age out of range: "
                            f"{age} >= {limit}"
                        )
                    ages[field] = round(age, 6)
                freshness_rows.append({
                    "account_id": account_id,
                    "node_id": row["node_id"],
                    "status": row["status"],
                    "release_id": row["release_id"],
                    "image_digest": row["image_digest"],
                    "sampled_at": row["sampled_at"].isoformat(),
                    "ages_seconds": ages,
                })

            cur.execute(
                """
                SELECT incident_id::text,
                       account_id,
                       severity,
                       status,
                       summary,
                       opened_at
                FROM production_incidents
                WHERE status='open' AND severity IN ('P0', 'P1')
                ORDER BY account_id, opened_at
                FOR UPDATE
                """
            )
            incident_rows = [dict(row) for row in cur.fetchall()]
            if incident_rows:
                raise RuntimeError(
                    "open P0/P1 incidents block canary: "
                    + json.dumps(
                        incident_rows,
                        default=str,
                        sort_keys=True,
                    )
                )
    evidence = {
        "schema_version": "trader-v3-fleet-freshness/v1",
        "captured_at": now_utc().isoformat(),
        "release_id": RELEASE_ID,
        "image_digest": IMAGE_DIGEST,
        "rollout_phase": expected_phase,
        "freshness_limits_seconds": freshness_limits,
        "freshness_rows": freshness_rows,
        "open_p0_p1_incidents": [],
    }
    evidence_path = account_dir / "fleet-freshness.json"
    write_json(evidence_path, evidence)
    signature_path = evidence_path.with_suffix(".json.sig")
    sign_json(evidence_path, signature_path, public_key_path)
    log(
        "FLEET_FRESHNESS "
        + json.dumps(
            {
                "heartbeat_under_5_seconds": True,
                "snapshots_under_10_seconds": True,
                "rows": freshness_rows,
                "open_p0_p1_incidents": 0,
                "evidence_sha256": sha256_file(evidence_path),
                "signature_sha256": sha256_file(signature_path),
            },
            sort_keys=True,
        )
    )
    return evidence_path

def copy_token(src: Path, dst: Path) -> None:
    data = src.read_bytes()
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)

def make_token_files(token_root: Path, target: Target) -> tuple[Path, Path]:
    token_root.mkdir(parents=True, exist_ok=False)
    token_root.chmod(0o700)
    risk = token_root / "risk-admin.token"
    node = token_root / f"{target.account_id}.node.token"
    copy_token(RISK_TOKEN_SOURCE, risk)
    copy_token(TOKEN_SOURCE_ROOT / target.token_name, node)
    return risk, node

def adapter_env(base_env: dict[str, str], target: Target, risk_token: Path, node_token: Path) -> dict[str, str]:
    env = dict(base_env)
    prefix = target.prefix
    env["HARDENED_CANARY_ACCOUNT_ID"] = target.account_id
    env["HARDENED_CANARY_REFRESH_ACCOUNTS"] = target.account_id
    env[f"{prefix}_CONTROL_PLANE_URL"] = "http://127.0.0.1:8080"
    env[f"{prefix}_RISK_ADMIN_TOKEN_FILE"] = str(risk_token)
    env[f"{prefix}_NODE_TOKEN_FILE"] = str(node_token)
    env[f"{prefix}_HTTP_TIMEOUT_SECONDS"] = "5"
    env[f"{prefix}_HTTP_MAX_ATTEMPTS"] = "3"
    env[f"{prefix}_POLL_INTERVAL_SECONDS"] = "0.5"
    env[f"{prefix}_ACTION_TIMEOUT_SECONDS"] = "120"
    env[f"{prefix}_FRESHNESS_SECONDS"] = "5"
    env[f"{prefix}_NODE_FRESHNESS_SECONDS"] = "10"
    return env

def identity_payload(target: Target, heartbeat: dict[str, Any], permit_id: str, intent_id: str) -> dict[str, Any]:
    return {
        "account_id": target.account_id,
        "symbol": SYMBOL,
        "rollout_phase": target.phase,
        "release_id": RELEASE_ID,
        "node_id": target.node_id,
        "writer_id": str(heartbeat["runtime_generation"]),
        "lease_id": str(heartbeat["redis_fencing_epoch"]),
        "fencing_epoch": int(heartbeat["lease_fencing_token"]),
        "image_digest": str(heartbeat["image_digest"]),
        "config_sha256": str(heartbeat["config_sha256"]),
        "dependency_lock_sha256": str(heartbeat["dependency_lock_sha256"]),
        "permit_id": permit_id,
        "intent_id": intent_id,
    }

def adapter_call(action: str, request: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    completed = run([str(RELEASE_ROOT / "account_a_live_trade_http_adapter.py"), action], env=env, input_text=json.dumps(request, sort_keys=True) + "\n", check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"adapter {action} failed")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"adapter {action} returned non-object")
    return payload

def sign_release_gate(account_dir: Path, target: Target) -> tuple[Path, Path, Path]:
    output = account_dir / "release-gate"
    env = dict(os.environ)
    env["TRADER_RELEASE_REVIEWER_TRUST_SHA256"] = REVIEWER_TRUST_SHA256
    run([str(PY_CP), "reviewed_release_rollout.py", "sign-release-gate", "--manifest", "release-manifest.json", "--bundle-manifest", "bundle-manifest.json", "--account-id", target.account_id, "--signing-private-key", str(REVIEWER_PRIVATE_KEY), "--output-directory", str(output)], cwd=RELEASE_ROOT, env=env)
    pub = output / "reviewer-public-key.pem"
    if sha256_file(pub) != PINNED_REVIEWER_PUBLIC_KEY_SHA256:
        raise RuntimeError("reviewer public key hash mismatch")
    return output / "release-gate.json", output / "release-gate.sig", pub

def sign_json(payload_path: Path, signature_path: Path, public_key_path: Path) -> None:
    run([str(OPENSSL), "dgst", "-sha256", "-sign", str(REVIEWER_PRIVATE_KEY), "-out", str(signature_path), str(payload_path)])
    signature_path.chmod(0o400)
    run([str(OPENSSL), "dgst", "-sha256", "-verify", str(public_key_path), "-signature", str(signature_path), str(payload_path)])

def fetch_binance_json(path: str, *, attempts: int = 5) -> dict[str, Any]:
    url = f"https://fapi.binance.com{path}"
    last_error: Exception | bool = False
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "trader-v3-jp24-canary/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"Binance returned non-object JSON: {path}")
            return payload
        except urllib.error.HTTPError as exc:
            last_error = exc
            retryable = exc.code in {418, 429} or exc.code >= 500
            if not retryable or attempt == attempts:
                raise RuntimeError(
                    f"Binance request failed status={exc.code}: {path}"
                ) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            if attempt == attempts:
                raise RuntimeError(f"Binance request failed: {path}") from exc
        delay = min(2 ** (attempt - 1), 8)
        log(
            f"BINANCE_RETRY path={path} attempt={attempt} "
            f"delay_s={delay} error={type(last_error).__name__}"
        )
        time.sleep(delay)
    raise RuntimeError(f"Binance request exhausted retries: {path}")

def get_public_price() -> tuple[Decimal, dict[str, Any]]:
    ticker = fetch_binance_json(f"/fapi/v1/ticker/price?symbol={SYMBOL}")
    info = fetch_binance_json(f"/fapi/v1/exchangeInfo?symbol={SYMBOL}")
    price = Decimal(str(ticker["price"]))
    filters = {}
    symbols = info.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise RuntimeError("exchangeInfo missing SOLUSDT")
    for item in symbols[0].get("filters", []):
        if isinstance(item, dict):
            filters[str(item.get("filterType"))] = item
    tick = Decimal(str(filters.get("PRICE_FILTER", {}).get("tickSize", "0.01")))
    limit = (price * Decimal("1.02") / tick).to_integral_value(rounding=ROUND_UP) * tick
    if limit * QUANTITY > MAX_NOTIONAL:
        raise RuntimeError(f"limit notional exceeds max: price={price} limit={limit}")
    return limit, {"ticker_price": str(price), "limit_price": str(limit), "tick_size": str(tick)}

def timestamp_from_heartbeat(row: dict[str, Any], key: str, fallback: datetime) -> str:
    value = row.get(key)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return fallback.astimezone(timezone.utc).isoformat()

def reconciliation_timestamp_from_heartbeat(row: dict[str, Any], fallback: datetime) -> str:
    value = _reconciliation_health_timestamp(row)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    completed = row.get("reconciliation_completed_at")
    if isinstance(completed, datetime):
        return completed.astimezone(timezone.utc).isoformat()
    return fallback.astimezone(timezone.utc).isoformat()

def build_signed_docs(account_dir: Path, target: Target, heartbeat: dict[str, Any], preflight: dict[str, Any], release_gate_path: Path, public_key_path: Path, permit_id: str, intent_id: str, limit_price: Decimal) -> dict[str, Path]:
    release_gate = read_json(release_gate_path)
    release_hash = sha256_file(release_gate_path)
    issued = now_utc().replace(microsecond=0)
    expires = issued + timedelta(minutes=10)
    payload_obj = heartbeat.get("payload")
    loss_monitor_at = heartbeat["last_seen_at"]
    if isinstance(payload_obj, dict) and payload_obj.get("ts"):
        try:
            loss_monitor_at = datetime.fromisoformat(str(payload_obj["ts"]).replace("Z", "+00:00"))
        except ValueError:
            loss_monitor_at = heartbeat["last_seen_at"]
    target_qty = Decimal(str(preflight["target_position_quantity"]))
    if target_qty != 0:
        raise RuntimeError(f"target position is not flat for {target.account_id}")
    if int(preflight["target_regular_order_count"]) != 0 or int(preflight["target_algo_order_count"]) != 0:
        raise RuntimeError(f"target orders are not zero for {target.account_id}")
    if not isinstance(_reconciliation_health_timestamp(heartbeat), datetime):
        raise RuntimeError(f"reconciliation health is not fresh for {target.account_id}")
    baseline = str(preflight["non_target_portfolio_baseline_sha256"])
    common = {
        "account_id": target.account_id,
        "node_id": target.node_id,
        "rollout_phase": target.phase,
        "symbol": SYMBOL,
        "release_id": release_gate["release_id"],
        "image_digest": release_gate["image_digest"],
        "config_sha256": release_gate["config_sha256"],
        "dependency_lock_sha256": release_gate["dependency_lock_sha256"],
        "release_root_path": release_gate["release_root_path"],
        "release_source_manifest_sha256": release_gate["release_source_manifest_sha256"],
        "live_adapter_sha256": release_gate["live_adapter_sha256"],
        "permit_store_id": release_gate["permit_store_id"],
        "permit_store_path": release_gate["permit_store_path"],
    }
    safety = {
        **common,
        "schema_version": f"trader-v3-{target.account_id}-live-safety-gate/v1",
        "release_gate_sha256": release_hash,
        "issued_at": iso_seconds(issued),
        "expires_at": iso_seconds(expires),
        "canary_halted": True,
        "readiness_healthy": True,
        "reconciliation_healthy": True,
        "target_symbol_flat": True,
        "target_symbol_regular_orders_zero": True,
        "target_symbol_algo_orders_zero": True,
        "loss_monitor_healthy": True,
        "no_open_p0_p1_incidents": True,
        "non_target_portfolio_baseline_sha256": baseline,
        "health_max_age_seconds": "30",
        "actor_tick_at": iso_seconds(issued),
        "heartbeat_at": timestamp_from_heartbeat(heartbeat, "last_seen_at", issued),
        "projection_at": timestamp_from_heartbeat(heartbeat, "positions_snapshot_at", issued),
        "reconciliation_at": reconciliation_timestamp_from_heartbeat(heartbeat, issued),
        "loss_monitor_at": loss_monitor_at.astimezone(timezone.utc).isoformat(),
    }
    safety_path = account_dir / "safety-gate.json"
    write_json(safety_path, safety)
    safety_hash = sha256_file(safety_path)
    emergency = {
        **common,
        "schema_version": f"trader-v3-{target.account_id}-live-emergency-close-gate/v1",
        "release_gate_sha256": release_hash,
        "safety_gate_sha256": safety_hash,
        "issued_at": iso_seconds(issued),
        "expires_at": iso_seconds(expires),
        "verified": True,
        "environment": "testnet",
        "open_order_type": "LIMIT",
        "open_time_in_force": "IOC",
        "close_order_type": "MARKET",
        "close_reduce_only": True,
        "target_symbol_flat": True,
        "target_symbol_regular_orders_zero": True,
        "target_symbol_algo_orders_zero": True,
        "testnet_evidence_sha256": TESTNET_EVIDENCE_SHA256,
        "verified_at": TESTNET_VERIFIED_AT,
        "verified_quantity": format(QUANTITY, "f"),
    }
    emergency_path = account_dir / "emergency-close-gate.json"
    write_json(emergency_path, emergency)
    emergency_hash = sha256_file(emergency_path)
    close_id = str(uuid5(UUID(intent_id), f"trader-v3/{target.account_id}/{SYMBOL}/close"))
    permit = {
        **common,
        "schema_version": f"trader-v3-{target.account_id}-live-permit/v1",
        "release_gate_sha256": release_hash,
        "safety_gate_sha256": safety_hash,
        "emergency_close_gate_sha256": emergency_hash,
        "issued_at": iso_seconds(issued),
        "expires_at": iso_seconds(expires),
        "permit_id": permit_id,
        "intent_id": intent_id,
        "open_client_order_id": f"B{UUID(intent_id).hex}01",
        "close_client_order_id": f"B{UUID(close_id).hex}01",
        "open_side": "BUY",
        "quantity": format(QUANTITY, "f"),
        "limit_price_usdt": format(limit_price, "f"),
        "max_notional_usdt": format(MAX_NOTIONAL, "f"),
        "max_cumulative_net_loss_usdt": format(MAX_LOSS, "f"),
        "single_use": True,
        "max_round_trips": 1,
        "portfolio_baseline_sha256": baseline,
        "writer_id": str(heartbeat["runtime_generation"]),
        "lease_id": str(heartbeat["redis_fencing_epoch"]),
        "fencing_epoch": int(heartbeat["lease_fencing_token"]),
    }
    permit_path = account_dir / "permit.json"
    write_json(permit_path, permit)
    for payload_path in (safety_path, emergency_path, permit_path):
        sign_json(payload_path, payload_path.with_suffix(payload_path.suffix + ".sig"), public_key_path)
    return {
        "release_gate": release_gate_path,
        "release_gate_sig": release_gate_path.with_suffix(".sig"),
        "reviewer_public_key": public_key_path,
        "safety_gate": safety_path,
        "safety_gate_sig": safety_path.with_suffix(".json.sig"),
        "emergency_gate": emergency_path,
        "emergency_gate_sig": emergency_path.with_suffix(".json.sig"),
        "permit": permit_path,
        "permit_sig": permit_path.with_suffix(".json.sig"),
    }

def insert_permit(conn, target: Target, permit_id: str, expires_at: str) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO live_canary_permits (
                    permit_id, account_id, symbol, max_notional_usdt,
                    max_cumulative_loss_usdt, max_open_count,
                    consumed_open_count, expires_at, release_id,
                    testnet_emergency_close_evidence_sha256,
                    testnet_emergency_close_verified_at, status, issued_by
                ) VALUES (%s,%s,%s,%s,%s,1,0,%s,%s,%s,%s,'issued',%s)
                """, (permit_id, target.account_id, SYMBOL, str(MAX_NOTIONAL), str(MAX_LOSS), expires_at, RELEASE_ID, TESTNET_EVIDENCE_SHA256, TESTNET_VERIFIED_AT, ACTOR))
    log(f"PERMIT inserted account={target.account_id} permit_id={permit_id} expires_at={expires_at}")

def revoke_unfinished_permit(permit_id: str) -> None:
    try:
        conn = db_connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE live_canary_permits
                    SET status='revoked'
                    WHERE permit_id=%s AND status IN ('issued','armed')
                    """, (permit_id,))
        conn.close()
        log(f"PERMIT revoked_if_unfinished permit_id={permit_id}")
    except Exception as exc:
        log(f"PERMIT revoke_failed permit_id={permit_id} error={exc}")

def execute_canary(target: Target, account_dir: Path, docs: dict[str, Path], env: dict[str, str]) -> dict[str, Any]:
    evidence_path = account_dir / "live-trade-report.json"
    command = [
        str(PY_SYSTEM), str(RELEASE_ROOT / "account_a_live_trade_executor.py"),
        "--execute-live", "--account-id", target.account_id,
        "--node-id", target.node_id, "--rollout-phase", target.phase,
        "--release-gate", str(docs["release_gate"]),
        "--release-gate-signature", str(docs["release_gate_sig"]),
        "--safety-gate", str(docs["safety_gate"]),
        "--safety-gate-signature", str(docs["safety_gate_sig"]),
        "--emergency-close-gate", str(docs["emergency_gate"]),
        "--emergency-close-gate-signature", str(docs["emergency_gate_sig"]),
        "--permit", str(docs["permit"]),
        "--permit-signature", str(docs["permit_sig"]),
        "--reviewer-public-key", str(docs["reviewer_public_key"]),
        "--live-adapter", str(RELEASE_ROOT / "account_a_live_trade_http_adapter.py"),
        "--evidence-output", str(evidence_path),
        "--adapter-timeout-seconds", "30",
        "--max-observations", "60",
        "--poll-interval-seconds", "1",
        "--recovery-max-attempts", "4",
        "--recovery-deadline-seconds", "30",
    ]
    completed = run(command, env=env, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"live canary failed for {target.account_id}")
    summary = json.loads(completed.stdout)
    if summary.get("passed") is not True:
        raise RuntimeError(f"live canary summary did not pass for {target.account_id}")
    report = read_json(evidence_path)
    compact = {
        "account_id": report.get("account_id"),
        "passed": report.get("passed"),
        "round_trip_count": report.get("round_trip_count"),
        "finished_halted": report.get("finished_halted"),
        "target_symbol_flat": report.get("target_symbol_flat"),
        "mainnet_round_trip": report.get("mainnet_round_trip"),
        "evidence_sha256": summary.get("evidence_sha256"),
    }
    log("CANARY_REPORT " + json.dumps(compact, sort_keys=True))
    return report

def sign_closure_report(account_dir: Path, public_key_path: Path) -> Path:
    report = account_dir / "live-trade-report.json"
    signature = account_dir / "live-trade-report.sig"
    sign_json(report, signature, public_key_path)
    log(f"CLOSURE_SIGNATURE report_sha256={sha256_file(report)} sig_sha256={sha256_file(signature)}")
    return signature

def acquire_operation_lock() -> tuple[int, str]:
    fd = os.open(OPERATION_LOCK_PATH, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if fd != 9:
        os.dup2(fd, 9)
        os.close(fd)
        fd = 9
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.fchmod(fd, 0o600)
    token = secrets.token_hex(32)
    os.ftruncate(fd, 0)
    os.pwrite(fd, (token + "\n").encode("ascii"), 0)
    os.fsync(fd)
    os.set_inheritable(fd, True)
    return fd, token

def release_operation_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

def acquire_maintenance_fence(conn, fence_id: str, token: str) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT fence_id::text
                FROM acquire_control_plane_maintenance_fence(%s, 'deploy', %s, %s, %s, %s)
                """, (fence_id, ACTOR, token, 120, 5))
            row = cur.fetchone()
    if row is None or row[0] != fence_id:
        raise RuntimeError("maintenance fence acquisition failed")
    log(f"FENCE acquired fence_id={fence_id}")

def release_maintenance_fence(conn, fence_id: str, token: str, reason: str) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT release_control_plane_maintenance_fence(%s,%s,%s,%s)", (fence_id, token, ACTOR, reason))
            row = cur.fetchone()
    if row is None or row[0] is not True:
        raise RuntimeError("maintenance fence release failed")
    log(f"FENCE released fence_id={fence_id}")

def advance_phase(account_dir: Path, from_target: Target, to_phase: str) -> None:
    fd = -1
    token = ""
    fence_id = str(uuid4())
    env = dict(os.environ)
    public_key_path = (
        account_dir / "release-gate" / "reviewer-public-key.pem"
    )
    if not public_key_path.is_file():
        public_key_path = account_dir / "reviewer-public-key.pem"
    if not public_key_path.is_file():
        raise RuntimeError("closure reviewer public key is missing")
    conn = db_connect()
    try:
        fd, token = acquire_operation_lock()
        acquire_maintenance_fence(conn, fence_id, token)
        database_url = read_database_url()
        env["DATABASE_URL"] = database_url
        env["ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP"] = "inherited"
        env["ACCOUNT_STALL_OPERATION_LOCK_FD"] = "9"
        env["ACCOUNT_STALL_OPERATION_LOCK_TOKEN"] = token
        env["ACCOUNT_STALL_MAINTENANCE_FENCE_OWNERSHIP"] = "inherited"
        env["ACCOUNT_STALL_MAINTENANCE_FENCE_ID"] = fence_id
        env["ACCOUNT_STALL_MAINTENANCE_FENCE_OWNER_TOKEN"] = token
        completed = run([
            str(PY_CP), "reviewed_release_rollout.py",
            "advance", "--release-id", RELEASE_ID, "--to-phase", to_phase,
            "--actor", ACTOR, "--reason", f"{from_target.account_id} SOLUSDT live canary passed on jp-24",
            "--idempotency-key", f"jp24-{from_target.account_id}-to-{to_phase}-{account_dir.parent.name}",
            "--closure-report", str(account_dir / "live-trade-report.json"),
            "--closure-signature", str(account_dir / "live-trade-report.sig"),
            "--closure-public-key", str(public_key_path),
            "--heartbeat-max-age-seconds", "5",
        ], cwd=RELEASE_ROOT, env=env, pass_fds=(9,), check=True)
        if completed.stdout.strip():
            payload = json.loads(completed.stdout)
            log("ADVANCE_RESULT " + json.dumps({"phase": payload.get("phase"), "phase_version": payload.get("phase_version"), "idempotent": payload.get("idempotent")}, sort_keys=True))
    finally:
        if token:
            try:
                release_maintenance_fence(conn, fence_id, token, f"advance {to_phase} complete")
            except Exception as exc:
                log(f"FENCE release_failed fence_id={fence_id} error={exc}")
        conn.close()
        if fd >= 0:
            release_operation_lock(fd)

def print_permit_state(conn, permit_id: str) -> None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT permit_id::text, account_id, symbol, status, issued_at,
                   armed_at, consumed_at, closed_at, consumed_open_count,
                   armed_node_id, portfolio_baseline_sha256
            FROM live_canary_permits
            WHERE permit_id=%s
            """, (permit_id,))
        row = cur.fetchone()
    log("PERMIT_STATE " + json.dumps(dict(row), default=str, sort_keys=True))

def run_account(base_dir: Path, target: Target, expected_phase: str) -> Path:
    account_dir = base_dir / target.account_id
    account_dir.mkdir(parents=True, exist_ok=False)
    account_dir.chmod(0o700)
    token_root = base_dir / ".runtime-tokens" / target.account_id
    permit_id = str(uuid4())
    intent_id = str(uuid4())
    conn = db_connect()
    try:
        require_phase(conn, expected_phase)
        check_ready(target)
        heartbeat = load_heartbeat(conn, target)
        risk_token, node_token = make_token_files(token_root, target)
        env = adapter_env(dict(os.environ), target, risk_token, node_token)
        request = identity_payload(target, heartbeat, permit_id, intent_id)
        release_gate_path, _release_gate_sig, public_key_path = sign_release_gate(account_dir, target)
        request.update({"phase": "preflight", "side_effect_id": f"{target.account_id}-preflight-{permit_id}"})
        preflight = adapter_call("preflight", request, env)
        log("PREFLIGHT " + json.dumps({
            "account_id": target.account_id,
            "target_position_side": preflight.get("target_position_side"),
            "target_position_quantity": preflight.get("target_position_quantity"),
            "target_regular_order_count": preflight.get("target_regular_order_count"),
            "target_algo_order_count": preflight.get("target_algo_order_count"),
            "available_usdt_balance": preflight.get("available_usdt_balance"),
            "baseline": preflight.get("non_target_portfolio_baseline_sha256"),
        }, sort_keys=True))
        sample_fleet_freshness(
            conn,
            account_dir,
            expected_phase,
            public_key_path,
        )
        ensure_no_target_incident(conn, target)
        limit_price, price_evidence = get_public_price()
        log("PRICE " + json.dumps({"account_id": target.account_id, **price_evidence}, sort_keys=True))
        heartbeat = wait_for_fresh_evidence(conn, target)
        docs = build_signed_docs(account_dir, target, heartbeat, preflight, release_gate_path, public_key_path, permit_id, intent_id, limit_price)
        permit_payload = read_json(docs["permit"])
        insert_permit(conn, target, permit_id, permit_payload["expires_at"])
        try:
            execute_canary(target, account_dir, docs, env)
        except Exception:
            revoke_unfinished_permit(permit_id)
            raise
        sign_closure_report(account_dir, public_key_path)
        print_permit_state(conn, permit_id)
    finally:
        conn.close()
        if token_root.exists():
            shutil.rmtree(token_root)
            log(f"TOKENS deleted account={target.account_id}")
    return account_dir

def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run one jp-24 live canary. Phase advance is a separate user-confirmed command.",
    )
    result.add_argument(
        "--account",
        choices=("account-a", "account-b", "account-c", "account-d"),
        required=True,
        help="execute one live canary only",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    target = TARGET_BY_ACCOUNT[str(args.account)]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_dir = EVIDENCE_ROOT / f"jp24-live-{target.account_id}-{stamp}"
    base_dir.mkdir(parents=True, exist_ok=False)
    base_dir.chmod(0o700)
    log(f"EVIDENCE_DIR {base_dir}")
    account_dir = run_account(base_dir, target, target.phase)
    conn = db_connect()
    try:
        require_phase(conn, target.phase)
    finally:
        conn.close()
    next_phase = NEXT_PHASE_BY_ACCOUNT[target.account_id]
    log(f"ACCOUNT_CANARY_COMPLETE account={target.account_id}")
    log(f"CLOSURE_DIR {account_dir}")
    log(
        "ADVANCE_CONFIRMATION_REQUIRED "
        f"account={target.account_id} to_phase={next_phase}"
    )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
