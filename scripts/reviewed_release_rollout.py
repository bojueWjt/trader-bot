#!/usr/bin/env python3
"""Register and advance an audited four-account reviewed release rollout."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

import psycopg2
import release_manifest
from psycopg2.extras import Json, RealDictCursor

ACCOUNTS = ("account-a", "account-b", "account-c", "account-d")
EXPECTED_NODE_IDS = {
    "account-a": "nautilus-node-account-a",
    "account-b": "nautilus-node-account-b",
    "account-c": "nautilus-node-account-c",
    "account-d": "nautilus-node-account-d",
}
PHASE_ACCOUNT_A_CANARY = "account_a_canary"
PHASE_ACCOUNT_B_ROLLOUT = "account_b_rollout"
PHASE_ACCOUNT_C_ROLLOUT = "account_c_rollout"
PHASE_ACCOUNT_D_ROLLOUT = "account_d_rollout"
PHASE_FLEET_COMPLETE = "fleet_complete"
PHASE_ABORTED = "aborted"
PHASES = (
    PHASE_ACCOUNT_A_CANARY,
    PHASE_ACCOUNT_B_ROLLOUT,
    PHASE_ACCOUNT_C_ROLLOUT,
    PHASE_ACCOUNT_D_ROLLOUT,
    PHASE_FLEET_COMPLETE,
    PHASE_ABORTED,
)
ACTIVE_ROLLOUT_PHASES = (
    PHASE_ACCOUNT_A_CANARY,
    PHASE_ACCOUNT_B_ROLLOUT,
    PHASE_ACCOUNT_C_ROLLOUT,
    PHASE_ACCOUNT_D_ROLLOUT,
)
MIGRATION_REBASELINE_REGISTRATION_MODE = "migration_rebaseline_stopped"
SAME_EPOCH_HOTFIX_REGISTRATION_MODE = "same_epoch_hotfix"
BOOTSTRAP_STOPPED_REGISTRATION_MODE = "bootstrap_stopped"
ALLOWED_TRANSITIONS = {
    PHASE_ACCOUNT_A_CANARY: {
        PHASE_ACCOUNT_B_ROLLOUT,
        PHASE_ABORTED,
    },
    PHASE_ACCOUNT_B_ROLLOUT: {
        PHASE_ACCOUNT_C_ROLLOUT,
        PHASE_ABORTED,
    },
    PHASE_ACCOUNT_C_ROLLOUT: {
        PHASE_ACCOUNT_D_ROLLOUT,
        PHASE_ABORTED,
    },
    PHASE_ACCOUNT_D_ROLLOUT: {
        PHASE_FLEET_COMPLETE,
        PHASE_ABORTED,
    },
    PHASE_FLEET_COMPLETE: set(),
    PHASE_ABORTED: set(),
}
ROLLOUT_READINESS = {
    PHASE_ACCOUNT_B_ROLLOUT: (
        ("account-a",),
        "account-b",
    ),
    PHASE_ACCOUNT_C_ROLLOUT: (
        ("account-a", "account-b"),
        "account-c",
    ),
    PHASE_ACCOUNT_D_ROLLOUT: (
        ("account-a", "account-b", "account-c"),
        "account-d",
    ),
}
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 5.0
REDIS_FENCING_DOMAIN = "trader-v3"
REDIS_FENCING_EPOCH_KEY = "trader-bot:redis-fencing-epoch"
REDIS_CAPACITY_EVIDENCE_SCHEMA = "trader-v3-redis-capacity-evidence/v3"
LIVE_CANARY_CLOSURE_REPORT_SCHEMA = "trader-v3-live-trade-report/v1"
LIVE_TRADE_SYMBOL = "SOLUSDT"
LIVE_ADAPTER_SOURCE_PATH = "scripts/account_a_live_trade_http_adapter.py"
LIVE_ADAPTER_RELEASE_PATH = "account_a_live_trade_http_adapter.py"
RELEASE_GATE_VALIDITY = timedelta(minutes=15)
PINNED_REVIEWER_PUBLIC_KEY_SHA256 = (
    "2b149fe2d7357dfea74441a1f6d6f1dd"
    "9ff6ea6a1a800fd5f2f7c2778339f928"
)
PINNED_OPENSSL_PATH = Path("/usr/bin/openssl")
CLOSURE_ACCOUNT_BY_TARGET_PHASE = {
    PHASE_ACCOUNT_B_ROLLOUT: "account-a",
    PHASE_ACCOUNT_C_ROLLOUT: "account-b",
    PHASE_ACCOUNT_D_ROLLOUT: "account-c",
    PHASE_FLEET_COMPLETE: "account-d",
}
CLOSURE_PHASE_BY_ACCOUNT = {
    "account-a": PHASE_ACCOUNT_A_CANARY,
    "account-b": PHASE_ACCOUNT_B_ROLLOUT,
    "account-c": PHASE_ACCOUNT_C_ROLLOUT,
    "account-d": PHASE_ACCOUNT_D_ROLLOUT,
}
DEFAULT_OPERATION_LOCK_PATH = Path(
    "/var/lock/trader-v3-account-stall-operation.lock"
)
OPERATION_LOCK_OWNERSHIP_ENV = (
    "ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP"
)
OPERATION_LOCK_FD_ENV = "ACCOUNT_STALL_OPERATION_LOCK_FD"
OPERATION_LOCK_TOKEN_ENV = "ACCOUNT_STALL_OPERATION_LOCK_TOKEN"
TEST_OPERATION_LOCK_PATH_ENV = (
    "TRADER_TEST_ACCOUNT_STALL_OPERATION_LOCK"
)
TEST_MODE_ENV = "TRADER_RELEASE_TEST_MODE"
MAINTENANCE_FENCE_OWNERSHIP_ENV = (
    "ACCOUNT_STALL_MAINTENANCE_FENCE_OWNERSHIP"
)
MAINTENANCE_FENCE_ID_ENV = "ACCOUNT_STALL_MAINTENANCE_FENCE_ID"
MAINTENANCE_FENCE_OWNER_TOKEN_ENV = (
    "ACCOUNT_STALL_MAINTENANCE_FENCE_OWNER_TOKEN"
)
MAINTENANCE_FENCE_OPERATION = "deploy"


class ReleaseRolloutError(RuntimeError):
    pass


class AccountStallOperationLock:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self._fd: int | None = None
        self._inherited = False
        self._token = ""

    def __enter__(self) -> Self:
        if not self._enabled:
            return self
        path = _operation_lock_path()
        ownership = os.environ.get(
            OPERATION_LOCK_OWNERSHIP_ENV,
            "standalone",
        ).strip()
        if ownership == "inherited":
            self._acquire_inherited(path)
            return self
        if ownership != "standalone":
            raise ReleaseRolloutError(
                "invalid account-stall operation lock ownership"
            )
        self._acquire_standalone(path)
        return self

    def __exit__(self, *_args: object) -> None:
        fd = self._fd
        self._fd = None
        self._token = ""
        if fd is None or self._inherited:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _acquire_inherited(self, path: Path) -> None:
        fd_raw = os.environ.get(OPERATION_LOCK_FD_ENV, "").strip()
        token = os.environ.get(OPERATION_LOCK_TOKEN_ENV, "").strip()
        if fd_raw != "9":
            raise ReleaseRolloutError(
                "account-stall inherited lock fd must be 9"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise ReleaseRolloutError(
                "account-stall inherited lock token is invalid"
            )
        fd = int(fd_raw)
        try:
            fd_metadata = os.fstat(fd)
            path_metadata = path.lstat()
        except OSError as exc:
            raise ReleaseRolloutError(
                "account-stall inherited lock is unavailable"
            ) from exc
        if not stat.S_ISREG(fd_metadata.st_mode):
            raise ReleaseRolloutError(
                "account-stall inherited lock fd is not a regular file"
            )
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or fd_metadata.st_nlink != 1
            or path_metadata.st_nlink != 1
        ):
            raise ReleaseRolloutError(
                "account-stall inherited lock path is unsafe"
            )
        if (
            fd_metadata.st_dev != path_metadata.st_dev
            or fd_metadata.st_ino != path_metadata.st_ino
        ):
            raise ReleaseRolloutError(
                "account-stall inherited lock fd does not match lock path"
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseRolloutError(
                f"another account-stall operation holds {path}"
            ) from exc
        try:
            recorded_token = os.pread(fd, 4096, 0).decode(
                "ascii"
            ).strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ReleaseRolloutError(
                "account-stall inherited lock token is unreadable"
            ) from exc
        if recorded_token != token:
            raise ReleaseRolloutError(
                "account-stall inherited lock token mismatch"
            )
        self._fd = fd
        self._inherited = True
        self._token = token

    def _acquire_standalone(self, path: Path) -> None:
        flags = os.O_RDWR | os.O_CREAT
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags | no_follow, 0o600)
        except OSError as exc:
            raise ReleaseRolloutError(
                f"cannot open account-stall operation lock: {path}"
            ) from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ReleaseRolloutError(
                    "account-stall operation lock is not a regular file"
                )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(fd, 0o600)
            token = secrets.token_hex(32)
            os.ftruncate(fd, 0)
            os.pwrite(fd, (token + "\n").encode("ascii"), 0)
            os.fsync(fd)
        except BlockingIOError as exc:
            os.close(fd)
            raise ReleaseRolloutError(
                f"another account-stall operation holds {path}"
            ) from exc
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        self._token = token

    @property
    def owner_token(self) -> str:
        self.require_held()
        return self._token

    def require_held(self) -> None:
        if not self._enabled:
            raise ReleaseRolloutError(
                "mutation requires the account-stall operation lock"
            )
        fd = self._fd
        if fd is None:
            raise ReleaseRolloutError(
                "account-stall operation lock is not held"
            )
        path = _operation_lock_path()
        try:
            fd_metadata = os.fstat(fd)
            path_metadata = path.lstat()
        except OSError as exc:
            raise ReleaseRolloutError(
                "account-stall operation lock identity is unavailable"
            ) from exc
        if (
            not stat.S_ISREG(fd_metadata.st_mode)
            or not stat.S_ISREG(path_metadata.st_mode)
            or fd_metadata.st_nlink != 1
            or path_metadata.st_nlink != 1
        ):
            raise ReleaseRolloutError(
                "account-stall operation lock must remain a regular file"
            )
        if (
            fd_metadata.st_dev != path_metadata.st_dev
            or fd_metadata.st_ino != path_metadata.st_ino
        ):
            raise ReleaseRolloutError(
                "account-stall operation lock inode changed"
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            recorded_token = os.pread(fd, 4096, 0).decode(
                "ascii"
            ).strip()
        except (BlockingIOError, OSError, UnicodeDecodeError) as exc:
            raise ReleaseRolloutError(
                "account-stall operation lock ownership was lost"
            ) from exc
        if (
            not re.fullmatch(r"[0-9a-f]{64}", self._token)
            or recorded_token != self._token
        ):
            raise ReleaseRolloutError(
                "account-stall operation lock owner token mismatch"
            )


def _operation_lock_path() -> Path:
    raw = str(DEFAULT_OPERATION_LOCK_PATH)
    test_path = os.environ.get(TEST_OPERATION_LOCK_PATH_ENV, "").strip()
    if test_path:
        if os.environ.get(TEST_MODE_ENV) != "1":
            raise ReleaseRolloutError(
                "operation lock test override requires test mode"
            )
        raw = test_path
    configured = os.environ.get(
        "ACCOUNT_STALL_OPERATION_LOCK",
        "",
    ).strip()
    if configured and configured != raw:
        raise ReleaseRolloutError(
            "account-stall operation lock path is fixed"
        )
    path = Path(raw)
    if not path.is_absolute():
        raise ReleaseRolloutError(
            "account-stall operation lock path must be absolute"
        )
    if path.is_symlink():
        raise ReleaseRolloutError(
            "account-stall operation lock cannot be a symlink"
        )
    return path


@dataclass(frozen=True)
class ReleaseDocument:
    release_id: str
    image_digest: str
    config_sha256: str
    dependency_lock_sha256: str
    schema_epoch: str
    manifest_sha256: str
    bundle_manifest_sha256: str
    delivery_mode: str
    release_root_path: str
    release_source_manifest_sha256: str
    live_adapter_sha256: str
    node_ids: tuple[tuple[str, str], ...] = ()

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.release_id,
            self.image_digest,
            self.config_sha256,
            self.dependency_lock_sha256,
            self.schema_epoch,
        )


@dataclass(frozen=True)
class RedisFencingEpochEvidence:
    redis_fencing_epoch: str
    marker_sha256: str
    capacity_evidence_sha256: str
    initial_redis_run_id: str
    active_volume: str


@dataclass(frozen=True)
class ReleaseGateTarget:
    account_id: str
    node_id: str
    rollout_phase: str
    schema_version: str
    permit_store_id: str
    permit_store_path: str


@dataclass(frozen=True)
class LiveCanaryClosureReport:
    account_id: str
    node_id: str
    rollout_phase: str
    release_id: str
    image_digest: str
    config_sha256: str
    dependency_lock_sha256: str
    report_sha256: str
    signature_sha256: str
    public_key_sha256: str
    open_filled_quantity: str
    actual_open_notional_usdt: str
    close_quantity: str
    close_filled_quantity: str
    non_target_portfolio_sha256: str

    def evidence(self) -> dict[str, Any]:
        return {
            "schema_version": LIVE_CANARY_CLOSURE_REPORT_SCHEMA,
            "account_id": self.account_id,
            "node_id": self.node_id,
            "rollout_phase": self.rollout_phase,
            "release_id": self.release_id,
            "image_digest": self.image_digest,
            "config_sha256": self.config_sha256,
            "dependency_lock_sha256": self.dependency_lock_sha256,
            "report_sha256": self.report_sha256,
            "signature_sha256": self.signature_sha256,
            "public_key_sha256": self.public_key_sha256,
            "open_order_type": "LIMIT",
            "open_time_in_force": "IOC",
            "open_filled_quantity": self.open_filled_quantity,
            "actual_open_notional_usdt": (
                self.actual_open_notional_usdt
            ),
            "close_order_type": "MARKET",
            "close_reduce_only": True,
            "close_quantity": self.close_quantity,
            "close_filled_quantity": self.close_filled_quantity,
            "target_symbol_flat": True,
            "target_symbol_regular_orders_zero": True,
            "target_symbol_algo_orders_zero": True,
            "finished_halted": True,
            "non_target_portfolio_sha256": (
                self.non_target_portfolio_sha256
            ),
        }


def load_live_canary_closure_report(
    report_path: Path,
    signature_path: Path,
    public_key_path: Path,
) -> LiveCanaryClosureReport:
    report_bytes = _read_evidence_file(report_path, "closure report")
    signature_bytes = _read_evidence_file(
        signature_path,
        "closure report signature",
    )
    public_key_bytes = _read_evidence_file(
        public_key_path,
        "closure report public key",
    )
    public_key_sha256 = hashlib.sha256(public_key_bytes).hexdigest()
    if public_key_sha256 != PINNED_REVIEWER_PUBLIC_KEY_SHA256:
        raise ReleaseRolloutError(
            "closure report public key hash mismatch"
        )
    _verify_closure_report_signature(
        report_bytes=report_bytes,
        signature_bytes=signature_bytes,
        public_key_bytes=public_key_bytes,
    )
    try:
        payload = json.loads(report_bytes)
    except json.JSONDecodeError as exc:
        raise ReleaseRolloutError(
            "closure report is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ReleaseRolloutError(
            "closure report root must be an object"
        )
    return _validated_live_canary_closure_report(
        payload,
        report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        signature_sha256=hashlib.sha256(signature_bytes).hexdigest(),
        public_key_sha256=public_key_sha256,
    )


def _read_evidence_file(path: Path, label: str) -> bytes:
    evidence_path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(evidence_path, flags)
    except OSError as exc:
        raise ReleaseRolloutError(
            f"cannot read {label}: {evidence_path}"
        ) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ReleaseRolloutError(
                f"{label} must be a regular file"
            )
        if metadata.st_size > 2 * 1024 * 1024:
            raise ReleaseRolloutError(f"{label} is too large")
        chunks = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(fd)
    if not payload:
        raise ReleaseRolloutError(f"{label} cannot be empty")
    return payload


def _verify_closure_report_signature(
    *,
    report_bytes: bytes,
    signature_bytes: bytes,
    public_key_bytes: bytes,
) -> None:
    if (
        not PINNED_OPENSSL_PATH.is_absolute()
        or not PINNED_OPENSSL_PATH.is_file()
        or PINNED_OPENSSL_PATH.is_symlink()
        or not os.access(PINNED_OPENSSL_PATH, os.X_OK)
    ):
        raise ReleaseRolloutError(
            "closure report signature verifier is unavailable"
        )
    with tempfile.TemporaryDirectory(
        prefix="reviewed-rollout-closure-"
    ) as temporary_root:
        root = Path(temporary_root)
        report_path = root / "report.json"
        signature_path = root / "report.sig"
        public_key_path = root / "reviewer.pem"
        for path, payload in (
            (report_path, report_bytes),
            (signature_path, signature_bytes),
            (public_key_path, public_key_bytes),
        ):
            path.write_bytes(payload)
            path.chmod(0o400)
        try:
            result = subprocess.run(
                [
                    str(PINNED_OPENSSL_PATH),
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(public_key_path),
                    "-signature",
                    str(signature_path),
                    str(report_path),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseRolloutError(
                "closure report signature verifier failed"
            ) from exc
    if result.returncode != 0:
        raise ReleaseRolloutError(
            "closure report signature verification failed"
        )


def _validated_live_canary_closure_report(
    payload: dict[str, Any],
    *,
    report_sha256: str,
    signature_sha256: str,
    public_key_sha256: str,
) -> LiveCanaryClosureReport:
    if payload.get("schema_version") != LIVE_CANARY_CLOSURE_REPORT_SCHEMA:
        raise ReleaseRolloutError("closure report schema mismatch")
    required_values = {
        "mode": "live",
        "passed": True,
        "failure_reason": "",
        "authorization_signatures_verified": True,
        "round_trip_count": 1,
        "finished_halted": True,
        "target_symbol_flat": True,
        "target_symbol_regular_orders_zero": True,
        "target_symbol_algo_orders_zero": True,
    }
    for field_name, expected in required_values.items():
        if payload.get(field_name) != expected:
            raise ReleaseRolloutError(
                f"closure report mismatch: {field_name}"
            )
    account_id = _required_text(
        payload.get("account_id"),
        "closure report account_id",
    )
    if account_id not in ACCOUNTS:
        raise ReleaseRolloutError(
            "closure report account_id is invalid"
        )
    node_id = _required_text(
        payload.get("node_id"),
        "closure report node_id",
    )
    if node_id != EXPECTED_NODE_IDS[account_id]:
        raise ReleaseRolloutError(
            "closure report node identity mismatch"
        )
    rollout_phase = _required_text(
        payload.get("rollout_phase"),
        "closure report rollout_phase",
    )
    if rollout_phase != CLOSURE_PHASE_BY_ACCOUNT[account_id]:
        raise ReleaseRolloutError(
            "closure report rollout phase mismatch"
        )
    before_sha256 = _required_sha256(
        payload.get("non_target_portfolio_before_sha256"),
        "closure report non_target_portfolio_before_sha256",
    )
    after_sha256 = _required_sha256(
        payload.get("non_target_portfolio_after_sha256"),
        "closure report non_target_portfolio_after_sha256",
    )
    if before_sha256 != after_sha256:
        raise ReleaseRolloutError(
            "closure report changed non-target portfolio"
        )
    mainnet = payload.get("mainnet_round_trip")
    if not isinstance(mainnet, dict):
        raise ReleaseRolloutError(
            "closure report lacks mainnet round trip"
        )
    mainnet_required = {
        "open_order_type": "LIMIT",
        "open_time_in_force": "IOC",
        "close_order_type": "MARKET",
        "close_reduce_only": True,
        "target_symbol_flat": True,
        "target_symbol_regular_orders_zero": True,
        "target_symbol_algo_orders_zero": True,
    }
    for field_name, expected in mainnet_required.items():
        if mainnet.get(field_name) != expected:
            raise ReleaseRolloutError(
                f"closure report mainnet mismatch: {field_name}"
            )
    open_filled = _closure_decimal(
        mainnet,
        "open_filled_quantity",
        positive=True,
    )
    close_quantity = _closure_decimal(
        mainnet,
        "close_quantity",
        positive=True,
    )
    close_filled = _closure_decimal(
        mainnet,
        "close_filled_quantity",
        positive=True,
    )
    if close_quantity != open_filled or close_filled != open_filled:
        raise ReleaseRolloutError(
            "closure report close quantity differs from actual open fill"
        )
    actual_notional = _closure_decimal(
        mainnet,
        "actual_open_notional_usdt",
        positive=True,
    )
    if actual_notional > Decimal("12"):
        raise ReleaseRolloutError(
            "closure report exceeds 12 USDT"
        )
    max_loss = _closure_decimal(
        mainnet,
        "max_cumulative_loss_usdt",
        positive=True,
    )
    cumulative_loss = _closure_decimal(
        mainnet,
        "cumulative_net_loss_usdt",
        non_negative=True,
    )
    if max_loss >= Decimal("1.5") or cumulative_loss >= max_loss:
        raise ReleaseRolloutError(
            "closure report loss limit was reached"
        )
    return LiveCanaryClosureReport(
        account_id=account_id,
        node_id=node_id,
        rollout_phase=rollout_phase,
        release_id=_required_text(
            payload.get("release_id"),
            "closure report release_id",
        ),
        image_digest=_required_text(
            payload.get("image_digest"),
            "closure report image_digest",
        ),
        config_sha256=_required_sha256(
            payload.get("config_sha256"),
            "closure report config_sha256",
        ),
        dependency_lock_sha256=_required_sha256(
            payload.get("dependency_lock_sha256"),
            "closure report dependency_lock_sha256",
        ),
        report_sha256=_required_sha256(
            report_sha256,
            "closure report sha256",
        ),
        signature_sha256=_required_sha256(
            signature_sha256,
            "closure report signature sha256",
        ),
        public_key_sha256=_required_sha256(
            public_key_sha256,
            "closure report public key sha256",
        ),
        open_filled_quantity=format(open_filled, "f"),
        actual_open_notional_usdt=format(actual_notional, "f"),
        close_quantity=format(close_quantity, "f"),
        close_filled_quantity=format(close_filled, "f"),
        non_target_portfolio_sha256=before_sha256,
    )


def _closure_decimal(
    payload: dict[str, Any],
    field_name: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    try:
        value = Decimal(str(payload.get(field_name)))
    except (InvalidOperation, ValueError) as exc:
        raise ReleaseRolloutError(
            f"closure report decimal is invalid: {field_name}"
        ) from exc
    if not value.is_finite():
        raise ReleaseRolloutError(
            f"closure report decimal is invalid: {field_name}"
        )
    if positive and value <= 0:
        raise ReleaseRolloutError(
            f"closure report requires positive {field_name}"
        )
    if non_negative and value < 0:
        raise ReleaseRolloutError(
            f"closure report requires non-negative {field_name}"
        )
    return value


def load_release_document(
    manifest_path: Path,
    bundle_manifest_path: Path,
    *,
    reviewer_trust_sha256: str | None = None,
) -> ReleaseDocument:
    manifest_path = Path(manifest_path)
    bundle_manifest_path = Path(bundle_manifest_path)
    release_root = _canonical_release_root(manifest_path)
    expected_manifest_path = release_root / "release-manifest.json"
    if manifest_path.absolute() != expected_manifest_path:
        raise ReleaseRolloutError(
            "reviewed rollout manifest must use the release root path"
        )
    expected_bundle_path = release_root / "bundle-manifest.json"
    if bundle_manifest_path.absolute() != expected_bundle_path:
        raise ReleaseRolloutError(
            "reviewed rollout bundle manifest must use release payload path"
        )
    _require_canonical_release_file(
        expected_manifest_path,
        "reviewed release manifest",
    )
    _require_canonical_release_file(
        expected_bundle_path,
        "reviewed bundle manifest",
    )
    manifest = _load_json_object(expected_manifest_path)
    validated = release_manifest.validate_release_manifest(manifest)
    if validated["schema_version"] != release_manifest.SCHEMA_VERSION:
        raise ReleaseRolloutError(
            "reviewed rollout requires immutable node config artifacts"
        )
    if (
        validated["release_purpose"]
        != release_manifest.RELEASE_PURPOSE_HARDENING
    ):
        raise ReleaseRolloutError(
            "reviewed rollout rejects emergency rollback manifests"
        )
    if validated["delivery_mode"] != release_manifest.DELIVERY_IMMUTABLE:
        raise ReleaseRolloutError(
            "reviewed rollout requires immutable_image"
        )
    pinned_reviewer = str(
        reviewer_trust_sha256
        or os.environ.get(
            "TRADER_RELEASE_REVIEWER_TRUST_SHA256",
            "",
        )
    ).strip()
    envelope = release_manifest.validate_strict_release_envelope(
        validated,
        payload_root=release_root,
        reviewer_trust_sha256=pinned_reviewer,
        verify_image_labels=True,
    )
    source_manifest_path = (
        release_root / release_manifest.RELEASE_SOURCE_MANIFEST_NAME
    )
    _require_canonical_release_file(
        source_manifest_path,
        "release source manifest",
    )
    actual_source_manifest_sha256 = _sha256_release_file(
        source_manifest_path,
        "release source manifest",
    )
    envelope_source_manifest_sha256 = _required_sha256(
        envelope.get("release_source_manifest_sha256"),
        "release envelope source manifest sha256",
    )
    if actual_source_manifest_sha256 != (
        envelope_source_manifest_sha256
    ):
        raise ReleaseRolloutError(
            "release source manifest hash changed after validation"
        )
    live_adapter_sha256 = _release_live_adapter_sha256(
        release_root,
        envelope,
    )
    release_manifest.require_release_matches_bundle(
        validated,
        expected_bundle_path,
    )
    release_manifest.validate_bundle_payload(
        expected_bundle_path,
        release_root,
        require_transition_runtime=False,
    )
    schema_epochs = validated["schema_epochs"]
    return ReleaseDocument(
        release_id=validated["release_id"],
        image_digest=validated["image_digest"],
        config_sha256=validated["config_sha256"],
        dependency_lock_sha256=validated["dependency_lock_sha256"],
        schema_epoch=schema_epochs["db"],
        manifest_sha256=release_manifest.sha256_file(
            expected_manifest_path
        ),
        bundle_manifest_sha256=release_manifest.sha256_file(
            expected_bundle_path
        ),
        delivery_mode=validated["delivery_mode"],
        release_root_path=str(release_root),
        release_source_manifest_sha256=(
            actual_source_manifest_sha256
        ),
        live_adapter_sha256=live_adapter_sha256,
        node_ids=tuple(
            sorted(
                (
                    str(item["account_id"]),
                    str(item["node_id"]),
                )
                for item in validated["node_configs"]
            )
        ),
    )


def _canonical_release_root(manifest_path: Path) -> Path:
    raw_parent = manifest_path.parent.absolute()
    try:
        release_root = manifest_path.parent.resolve(strict=True)
    except OSError as exc:
        raise ReleaseRolloutError(
            "reviewed release root is unavailable"
        ) from exc
    if raw_parent != release_root:
        raise ReleaseRolloutError(
            "reviewed release root must be canonical and contain no symlinks"
        )
    try:
        metadata = release_root.stat()
    except OSError as exc:
        raise ReleaseRolloutError(
            "reviewed release root is unavailable"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseRolloutError(
            "reviewed release root must be a directory"
        )
    return release_root


def _require_canonical_release_file(path: Path, label: str) -> None:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as exc:
        raise ReleaseRolloutError(f"{label} is unavailable") from exc
    if resolved != path:
        raise ReleaseRolloutError(
            f"{label} must be canonical and contain no symlinks"
        )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ReleaseRolloutError(
            f"{label} must be a single-link regular file"
        )


def _sha256_release_file(path: Path, label: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseRolloutError(
            f"cannot open {label}"
        ) from exc
    try:
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_nlink != 1
        ):
            raise ReleaseRolloutError(
                f"{label} must be a single-link regular file"
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        try:
            current_metadata = path.lstat()
        except OSError as exc:
            raise ReleaseRolloutError(
                f"{label} changed during hashing"
            ) from exc
        if (
            opened_metadata.st_dev != current_metadata.st_dev
            or opened_metadata.st_ino != current_metadata.st_ino
            or opened_metadata.st_size != current_metadata.st_size
            or opened_metadata.st_mtime_ns != current_metadata.st_mtime_ns
        ):
            raise ReleaseRolloutError(
                f"{label} changed during hashing"
            )
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _release_live_adapter_sha256(
    release_root: Path,
    envelope: dict[str, Any],
) -> str:
    source_manifest = envelope.get("source_manifest")
    if not isinstance(source_manifest, dict):
        raise ReleaseRolloutError(
            "release envelope source manifest is missing"
        )
    raw_source_files = source_manifest.get("files")
    if not isinstance(raw_source_files, list):
        raise ReleaseRolloutError(
            "release source manifest files are invalid"
        )
    source_entries = [
        item
        for item in raw_source_files
        if isinstance(item, dict)
        and item.get("release_path") == LIVE_ADAPTER_RELEASE_PATH
    ]
    if len(source_entries) != 1:
        raise ReleaseRolloutError(
            "release source manifest must contain one live adapter"
        )
    source_entry = source_entries[0]
    if source_entry.get("source_path") != LIVE_ADAPTER_SOURCE_PATH:
        raise ReleaseRolloutError(
            "release source manifest live adapter source mismatch"
        )
    if source_entry.get("source_git_mode") != "100755":
        raise ReleaseRolloutError(
            "release source manifest live adapter mode mismatch"
        )
    source_sha256 = _required_sha256(
        source_entry.get("sha256"),
        "release source manifest live adapter sha256",
    )
    raw_payload = envelope.get("release_payload")
    if not isinstance(raw_payload, list):
        raise ReleaseRolloutError(
            "release envelope payload is invalid"
        )
    payload_entries = [
        item
        for item in raw_payload
        if isinstance(item, dict)
        and item.get("path") == LIVE_ADAPTER_RELEASE_PATH
    ]
    if len(payload_entries) != 1:
        raise ReleaseRolloutError(
            "release payload must contain one live adapter"
        )
    payload_sha256 = _required_sha256(
        payload_entries[0].get("sha256"),
        "release payload live adapter sha256",
    )
    adapter_path = release_root / LIVE_ADAPTER_RELEASE_PATH
    _require_canonical_release_file(
        adapter_path,
        "release live adapter",
    )
    actual_sha256 = _sha256_release_file(
        adapter_path,
        "release live adapter",
    )
    if (
        source_sha256 != payload_sha256
        or source_sha256 != actual_sha256
    ):
        raise ReleaseRolloutError(
            "release live adapter hash differs from immutable envelope"
        )
    adapter_metadata = adapter_path.stat()
    if adapter_metadata.st_mode & stat.S_IXUSR == 0:
        raise ReleaseRolloutError(
            "release live adapter must be owner-executable"
        )
    return actual_sha256


def generate_signed_release_gate(
    manifest_path: Path,
    bundle_manifest_path: Path,
    *,
    account_id: str,
    signing_private_key_path: Path,
    output_directory: Path,
    now: datetime | None = None,
) -> dict[str, str]:
    document = load_release_document(
        manifest_path,
        bundle_manifest_path,
    )
    target = _release_gate_target(account_id)
    issued_at = now
    if issued_at is None:
        issued_at = datetime.now(timezone.utc)
    if issued_at.tzinfo is None or issued_at.utcoffset() is None:
        raise ReleaseRolloutError(
            "release gate issuance time must include a timezone"
        )
    issued_at = issued_at.astimezone(timezone.utc).replace(microsecond=0)
    expires_at = issued_at + RELEASE_GATE_VALIDITY
    release_gate = {
        "schema_version": target.schema_version,
        "account_id": target.account_id,
        "node_id": target.node_id,
        "rollout_phase": target.rollout_phase,
        "symbol": LIVE_TRADE_SYMBOL,
        "release_id": document.release_id,
        "image_digest": document.image_digest,
        "config_sha256": document.config_sha256,
        "dependency_lock_sha256": document.dependency_lock_sha256,
        "release_root_path": document.release_root_path,
        "release_source_manifest_sha256": (
            document.release_source_manifest_sha256
        ),
        "live_adapter_sha256": document.live_adapter_sha256,
        "permit_store_id": target.permit_store_id,
        "permit_store_path": target.permit_store_path,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    payload_bytes = (
        json.dumps(release_gate, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    private_key_bytes = _read_signing_private_key(
        signing_private_key_path
    )
    signature_bytes, public_key_bytes = _sign_release_gate(
        payload_bytes,
        private_key_bytes,
    )
    verified_document = load_release_document(
        manifest_path,
        bundle_manifest_path,
    )
    if verified_document != document:
        raise ReleaseRolloutError(
            "reviewed release changed during release gate signing"
        )
    output_root = _publish_signed_release_gate(
        output_directory,
        release_root=Path(document.release_root_path),
        payload_bytes=payload_bytes,
        signature_bytes=signature_bytes,
        public_key_bytes=public_key_bytes,
    )
    return {
        "release_gate": str(output_root / "release-gate.json"),
        "release_gate_sha256": hashlib.sha256(
            payload_bytes
        ).hexdigest(),
        "release_gate_signature": str(
            output_root / "release-gate.sig"
        ),
        "release_gate_signature_sha256": hashlib.sha256(
            signature_bytes
        ).hexdigest(),
        "reviewer_public_key": str(
            output_root / "reviewer-public-key.pem"
        ),
        "reviewer_public_key_sha256": hashlib.sha256(
            public_key_bytes
        ).hexdigest(),
    }


def _release_gate_target(account_id: str) -> ReleaseGateTarget:
    normalized = _required_text(account_id, "account_id")
    if normalized not in ACCOUNTS:
        raise ReleaseRolloutError(
            f"account_id must be one of: {', '.join(ACCOUNTS)}"
        )
    suffix = normalized.removeprefix("account-")
    rollout_phase = f"account_{suffix}_rollout"
    if normalized == "account-a":
        rollout_phase = PHASE_ACCOUNT_A_CANARY
    schema_prefix = f"trader-v3-{normalized}-live"
    return ReleaseGateTarget(
        account_id=normalized,
        node_id=EXPECTED_NODE_IDS[normalized],
        rollout_phase=rollout_phase,
        schema_version=f"{schema_prefix}-release-gate/v1",
        permit_store_id=f"{schema_prefix}-permit-store/v1",
        permit_store_path=(
            f"/var/lib/trader-v3/{normalized}-live-permit-ledger.json"
        ),
    )


def _read_signing_private_key(path: Path) -> bytes:
    private_key_path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(private_key_path, flags)
    except OSError as exc:
        raise ReleaseRolloutError(
            "cannot read release gate signing private key"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise ReleaseRolloutError(
                "release gate signing private key must be a regular file"
            )
        if metadata.st_uid != os.geteuid():
            raise ReleaseRolloutError(
                "release gate signing private key owner mismatch"
            )
        if metadata.st_mode & 0o077:
            raise ReleaseRolloutError(
                "release gate signing private key mode must deny group/world"
            )
        if metadata.st_size <= 0 or metadata.st_size > 64 * 1024:
            raise ReleaseRolloutError(
                "release gate signing private key size is invalid"
            )
        chunks = []
        while True:
            chunk = os.read(descriptor, 16 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _sign_release_gate(
    payload_bytes: bytes,
    private_key_bytes: bytes,
) -> tuple[bytes, bytes]:
    if (
        not PINNED_OPENSSL_PATH.is_absolute()
        or not PINNED_OPENSSL_PATH.is_file()
        or PINNED_OPENSSL_PATH.is_symlink()
        or not os.access(PINNED_OPENSSL_PATH, os.X_OK)
    ):
        raise ReleaseRolloutError(
            "release gate signer is unavailable"
        )
    with tempfile.TemporaryDirectory(
        prefix="reviewed-release-gate-signing-"
    ) as temporary_root:
        root = Path(temporary_root)
        private_key_path = root / "reviewer-private.pem"
        payload_path = root / "release-gate.json"
        signature_path = root / "release-gate.sig"
        public_key_path = root / "reviewer-public-key.pem"
        _write_new_private_file(private_key_path, private_key_bytes)
        _write_new_private_file(payload_path, payload_bytes)
        try:
            public_result = subprocess.run(
                [
                    str(PINNED_OPENSSL_PATH),
                    "pkey",
                    "-in",
                    str(private_key_path),
                    "-pubout",
                    "-out",
                    str(public_key_path),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )
            sign_result = subprocess.run(
                [
                    str(PINNED_OPENSSL_PATH),
                    "dgst",
                    "-sha256",
                    "-sign",
                    str(private_key_path),
                    "-out",
                    str(signature_path),
                    str(payload_path),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseRolloutError(
                "release gate signer failed"
            ) from exc
        if public_result.returncode != 0 or sign_result.returncode != 0:
            raise ReleaseRolloutError(
                "release gate signer rejected the private key"
            )
        try:
            verify_result = subprocess.run(
                [
                    str(PINNED_OPENSSL_PATH),
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(public_key_path),
                    "-signature",
                    str(signature_path),
                    str(payload_path),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseRolloutError(
                "release gate signature verification failed"
            ) from exc
        if verify_result.returncode != 0:
            raise ReleaseRolloutError(
                "release gate signature verification failed"
            )
        try:
            public_key_bytes = public_key_path.read_bytes()
            signature_bytes = signature_path.read_bytes()
        except OSError as exc:
            raise ReleaseRolloutError(
                "release gate signer output is unavailable"
            ) from exc
    public_key_sha256 = hashlib.sha256(public_key_bytes).hexdigest()
    if public_key_sha256 != PINNED_REVIEWER_PUBLIC_KEY_SHA256:
        raise ReleaseRolloutError(
            "release gate signing key differs from pinned reviewer"
        )
    if not signature_bytes:
        raise ReleaseRolloutError(
            "release gate signature is empty"
        )
    return signature_bytes, public_key_bytes


def _publish_signed_release_gate(
    output_directory: Path,
    *,
    release_root: Path,
    payload_bytes: bytes,
    signature_bytes: bytes,
    public_key_bytes: bytes,
) -> Path:
    output = Path(output_directory).absolute()
    if output.exists() or output.is_symlink():
        raise ReleaseRolloutError(
            "release gate output directory must not exist"
        )
    try:
        parent = output.parent.resolve(strict=True)
    except OSError as exc:
        raise ReleaseRolloutError(
            "release gate output parent is unavailable"
        ) from exc
    if output.parent.absolute() != parent:
        raise ReleaseRolloutError(
            "release gate output parent must be canonical"
        )
    try:
        output.relative_to(release_root)
    except ValueError:
        pass
    else:
        raise ReleaseRolloutError(
            "release gate output must remain outside immutable release root"
        )
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.",
            dir=parent,
        )
    )
    published = False
    try:
        staging.chmod(0o700)
        for name, content in (
            ("release-gate.json", payload_bytes),
            ("release-gate.sig", signature_bytes),
            ("reviewer-public-key.pem", public_key_bytes),
        ):
            _write_new_private_file(staging / name, content)
        os.replace(staging, output)
        published = True
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise ReleaseRolloutError(
            "cannot publish signed release gate"
        ) from exc
    finally:
        if not published:
            for path in staging.glob("*"):
                path.unlink()
            staging.rmdir()
    return output


def _write_new_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o400)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_capacity_evidence(path: Path) -> RedisFencingEpochEvidence:
    evidence_path = Path(path)
    if evidence_path.is_symlink():
        raise ReleaseRolloutError(
            "Redis capacity evidence cannot be a symlink"
        )
    try:
        payload_bytes = evidence_path.read_bytes()
        payload = json.loads(payload_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseRolloutError(
            f"cannot read Redis capacity evidence: {evidence_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReleaseRolloutError(
            "Redis capacity evidence root must be an object"
        )
    if payload.get("schema_version") != REDIS_CAPACITY_EVIDENCE_SCHEMA:
        raise ReleaseRolloutError("Redis capacity evidence schema mismatch")
    required_truths = (
        "passed",
        "nodes_stopped",
        "control_keys_reinitialized",
    )
    for field_name in required_truths:
        if payload.get(field_name) is not True:
            raise ReleaseRolloutError(
                f"Redis capacity evidence requires {field_name}=true"
            )
    if payload.get("dataset_mode") != (
        "empty-volume-exchange-first-rebaseline"
    ):
        raise ReleaseRolloutError("Redis capacity evidence dataset mode mismatch")
    if payload.get("active_container") != "trader-v3-redis":
        raise ReleaseRolloutError(
            "Redis capacity evidence active container mismatch"
        )
    active_key_count = payload.get("active_key_count")
    if (
        isinstance(active_key_count, bool)
        or not isinstance(active_key_count, int)
        or active_key_count != 1
    ):
        raise ReleaseRolloutError(
            "Redis capacity evidence must contain only the epoch marker"
        )
    if payload.get("redis_fencing_epoch_key") != REDIS_FENCING_EPOCH_KEY:
        raise ReleaseRolloutError(
            "Redis capacity evidence marker key mismatch"
        )
    redis_fencing_epoch = _required_text(
        payload.get("redis_fencing_epoch"),
        "redis_fencing_epoch",
    )
    try:
        parsed_epoch = UUID(redis_fencing_epoch)
    except ValueError as exc:
        raise ReleaseRolloutError(
            "Redis fencing epoch must be a canonical UUID4"
        ) from exc
    if parsed_epoch.version != 4 or str(parsed_epoch) != redis_fencing_epoch:
        raise ReleaseRolloutError(
            "Redis fencing epoch must be a canonical UUID4"
        )
    marker_sha256 = _required_sha256(
        payload.get("redis_fencing_epoch_sha256"),
        "redis_fencing_epoch_sha256",
    )
    actual_marker_sha256 = hashlib.sha256(
        redis_fencing_epoch.encode("ascii")
    ).hexdigest()
    if marker_sha256 != actual_marker_sha256:
        raise ReleaseRolloutError(
            "Redis capacity evidence marker hash mismatch"
        )
    active_run_id = _required_redis_run_id(
        payload.get("active_redis_run_id"),
        "active_redis_run_id",
    )
    initial_run_id = _required_redis_run_id(
        payload.get("initial_redis_run_id"),
        "initial_redis_run_id",
    )
    if initial_run_id != active_run_id:
        raise ReleaseRolloutError(
            "Redis capacity evidence initial run_id mismatch"
        )
    active_volume = _required_text(
        payload.get("active_volume"),
        "active_volume",
    )
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", active_volume) is None:
        raise ReleaseRolloutError(
            "Redis capacity evidence active volume is invalid"
        )
    _required_text(
        payload.get("active_volume_source"),
        "active_volume_source",
    )
    return RedisFencingEpochEvidence(
        redis_fencing_epoch=redis_fencing_epoch,
        marker_sha256=marker_sha256,
        capacity_evidence_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        initial_redis_run_id=initial_run_id,
        active_volume=active_volume,
    )


def register_reviewed_release(
    conn,
    document: ReleaseDocument,
    capacity_evidence: RedisFencingEpochEvidence,
    *,
    reviewed_by: str,
    idempotency_key: str,
    operation_lock: AccountStallOperationLock,
) -> dict[str, Any]:
    operation_lock.require_held()
    reviewer = _required_text(reviewed_by, "reviewed_by")
    if not isinstance(capacity_evidence, RedisFencingEpochEvidence):
        raise ReleaseRolloutError("capacity_evidence is required")
    operation_key = _required_text(
        idempotency_key,
        "idempotency_key",
    )
    if dict(document.node_ids) != EXPECTED_NODE_IDS:
        raise ReleaseRolloutError(
            "reviewed release node/account exact-set mismatch"
        )
    with conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            operation_lock.require_held()
            maintenance_fence = _require_maintenance_fence(
                cur,
                operation_lock,
                stage="register",
            )
            _lock_redis_fencing_epoch_domain(cur)
            existing_by_key = _rollout_by_registration_key(
                cur,
                operation_key,
            )
            if existing_by_key is not None:
                _require_rollout_matches_document(
                    existing_by_key,
                    document,
                    capacity_evidence,
                    operation_key,
                    reviewer,
                )
                _require_active_epoch_matches_rollout(
                    cur,
                    existing_by_key,
                )
                _require_registered_manifests(cur, document)
                return _rollout_payload(
                    existing_by_key,
                    idempotent=True,
                )

            active_epoch = _active_redis_fencing_epoch(
                cur,
                for_update=True,
            )
            same_epoch_predecessor: dict[str, Any] | None = None
            if active_epoch == capacity_evidence.redis_fencing_epoch:
                same_epoch_predecessor = _lock_same_epoch_hotfix_predecessor(
                    cur,
                    document=document,
                    capacity_evidence=capacity_evidence,
                    active_redis_fencing_epoch=active_epoch,
                )
                _abort_same_epoch_hotfix_predecessor(
                    cur,
                    predecessor=same_epoch_predecessor,
                    successor_document=document,
                    successor_capacity_evidence=capacity_evidence,
                    actor=reviewer,
                )
                registration_heartbeats: list[dict[str, Any]] = []
            else:
                registration_heartbeats = (
                    _lock_registration_heartbeats(
                        cur,
                        document=document,
                        active_redis_fencing_epoch=active_epoch,
                        max_age_seconds=DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
                    )
                )
                operation_lock.require_held()
                _activate_redis_fencing_epoch(
                    cur,
                    capacity_evidence,
                    activated_by=reviewer,
                )
            operation_lock.require_held()
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
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    document.release_id,
                    capacity_evidence.redis_fencing_epoch,
                    document.image_digest,
                    document.config_sha256,
                    document.dependency_lock_sha256,
                    document.schema_epoch,
                    document.manifest_sha256,
                    document.bundle_manifest_sha256,
                    operation_key,
                    reviewer,
                ),
            )
            inserted = cur.fetchone()
            rollout = inserted
            if rollout is None:
                rollout = _rollout_by_release_id(
                    cur,
                    document.release_id,
                )
            if rollout is None:
                conflicting = _rollout_by_registration_key(
                    cur,
                    operation_key,
                )
                if conflicting is not None:
                    raise ReleaseRolloutError(
                        "registration idempotency key belongs to another release"
                    )
                raise ReleaseRolloutError(
                    "reviewed release rollout registration conflicted"
                )

            _require_rollout_matches_document(
                rollout,
                document,
                capacity_evidence,
                operation_key,
                reviewer,
            )
            _register_account_manifests(
                cur,
                document,
                reviewed_by=reviewer,
            )
            _require_registered_manifests(cur, document)
            if inserted is not None:
                evidence = {
                    "accounts": list(ACCOUNTS),
                    "delivery_mode": document.delivery_mode,
                    "manifest_sha256": document.manifest_sha256,
                    "bundle_manifest_sha256": (
                        document.bundle_manifest_sha256
                    ),
                    "redis_fencing_epoch": (
                        capacity_evidence.redis_fencing_epoch
                    ),
                    "redis_fencing_epoch_marker_sha256": (
                        capacity_evidence.marker_sha256
                    ),
                    "redis_capacity_evidence_sha256": (
                        capacity_evidence.capacity_evidence_sha256
                    ),
                    "initial_redis_run_id": (
                        capacity_evidence.initial_redis_run_id
                    ),
                    "active_volume": capacity_evidence.active_volume,
                    "registration_heartbeats": (
                        registration_heartbeats
                    ),
                    "maintenance_fence": maintenance_fence,
                }
                if same_epoch_predecessor is not None:
                    evidence.update(
                        {
                            "registration_mode": (
                                SAME_EPOCH_HOTFIX_REGISTRATION_MODE
                            ),
                            "predecessor_release_id": str(
                                same_epoch_predecessor["release_id"]
                            ),
                            "redis_epoch_reused": True,
                        }
                    )
                _record_rollout_event(
                    cur,
                    release_id=document.release_id,
                    event_type="registered",
                    from_phase=None,
                    to_phase=PHASE_ACCOUNT_A_CANARY,
                    phase_version=1,
                    idempotency_key=operation_key,
                    actor=reviewer,
                    reason=(
                        "reviewed release registered for account-a through "
                        "account-d"
                    ),
                    evidence=evidence,
                )
                _record_global_audit(
                    cur,
                    release_id=document.release_id,
                    event_type="reviewed_release_registered",
                    actor=reviewer,
                    payload={
                        "phase": PHASE_ACCOUNT_A_CANARY,
                        "phase_version": 1,
                        "idempotency_key": operation_key,
                        **evidence,
                    },
                )
            return _rollout_payload(
                rollout,
                idempotent=inserted is None,
            )


def bootstrap_register_reviewed_release(
    conn,
    document: ReleaseDocument,
    capacity_evidence: RedisFencingEpochEvidence,
    *,
    reviewed_by: str,
    idempotency_key: str,
    operation_lock: AccountStallOperationLock,
) -> dict[str, Any]:
    """Register a fenced rollout while the four-account fleet is stopped."""
    operation_lock.require_held()
    reviewer = _required_text(reviewed_by, "reviewed_by")
    if not isinstance(capacity_evidence, RedisFencingEpochEvidence):
        raise ReleaseRolloutError("capacity_evidence is required")
    operation_key = _required_text(
        idempotency_key,
        "idempotency_key",
    )
    if dict(document.node_ids) != EXPECTED_NODE_IDS:
        raise ReleaseRolloutError(
            "reviewed release node/account exact-set mismatch"
        )

    with conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            operation_lock.require_held()
            _lock_redis_fencing_epoch_domain(cur)
            _require_no_active_maintenance_fence_for_bootstrap_stopped(cur)
            existing_by_key = _rollout_by_registration_key(
                cur,
                operation_key,
            )
            history = _bootstrap_registration_history(cur)
            if existing_by_key is not None:
                _require_rollout_matches_document(
                    existing_by_key,
                    document,
                    capacity_evidence,
                    operation_key,
                    reviewer,
                )
                _require_active_epoch_matches_rollout(
                    cur,
                    existing_by_key,
                )
                _require_registered_manifests(cur, document)
                _require_bootstrap_registration_replay(
                    cur,
                    document=document,
                    capacity_evidence=capacity_evidence,
                    history=history,
                    idempotency_key=operation_key,
                    reviewed_by=reviewer,
                )
                return _rollout_payload(
                    existing_by_key,
                    idempotent=True,
                )

            predecessor = None
            registration_mode = "bootstrap"
            if _bootstrap_history_is_empty(history):
                operation_lock.require_held()
                _activate_redis_fencing_epoch(
                    cur,
                    capacity_evidence,
                    activated_by=reviewer,
                )
            else:
                registration_mode = BOOTSTRAP_STOPPED_REGISTRATION_MODE
                predecessor = _lock_bootstrap_stopped_predecessor(
                    cur,
                    document=document,
                    capacity_evidence=capacity_evidence,
                    history=history,
                )
                operation_lock.require_held()
                _abort_bootstrap_stopped_predecessor(
                    cur,
                    predecessor=predecessor,
                    successor_document=document,
                    successor_capacity_evidence=capacity_evidence,
                    actor=reviewer,
                )
            operation_lock.require_held()
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
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING *
                """,
                (
                    document.release_id,
                    capacity_evidence.redis_fencing_epoch,
                    document.image_digest,
                    document.config_sha256,
                    document.dependency_lock_sha256,
                    document.schema_epoch,
                    document.manifest_sha256,
                    document.bundle_manifest_sha256,
                    operation_key,
                    reviewer,
                ),
            )
            rollout = cur.fetchone()
            if rollout is None:
                raise ReleaseRolloutError(
                    "bootstrap reviewed release rollout registration conflicted"
                )

            _require_rollout_matches_document(
                rollout,
                document,
                capacity_evidence,
                operation_key,
                reviewer,
            )
            _register_account_manifests(
                cur,
                document,
                reviewed_by=reviewer,
            )
            _require_registered_manifests(cur, document)
            evidence = {
                "registration_mode": registration_mode,
                "bootstrap_all_halted": True,
                "all_accounts_stopped": True,
                "bootstrap_history": {
                    "redis_fencing_epoch_count": (
                        history["redis_fencing_epoch_count"]
                    ),
                    "reviewed_release_rollout_count": (
                        history["reviewed_release_rollout_count"]
                    ),
                },
                "accounts": list(ACCOUNTS),
                "delivery_mode": document.delivery_mode,
                "manifest_sha256": document.manifest_sha256,
                "bundle_manifest_sha256": document.bundle_manifest_sha256,
                "redis_fencing_epoch": (
                    capacity_evidence.redis_fencing_epoch
                ),
                "redis_fencing_epoch_marker_sha256": (
                    capacity_evidence.marker_sha256
                ),
                "redis_capacity_evidence_sha256": (
                    capacity_evidence.capacity_evidence_sha256
                ),
                "initial_redis_run_id": (
                    capacity_evidence.initial_redis_run_id
                ),
                "active_volume": capacity_evidence.active_volume,
            }
            if predecessor is not None:
                evidence.update(
                    {
                        "predecessor_release_id": str(
                            predecessor["release_id"]
                        ),
                        "predecessor_phase": str(predecessor["phase"]),
                        "predecessor_phase_version": int(
                            predecessor["phase_version"]
                        ),
                        "predecessor_redis_fencing_epoch": str(
                            predecessor["redis_fencing_epoch"]
                        ),
                        "redis_epoch_reused": True,
                    }
                )
            _record_rollout_event(
                cur,
                release_id=document.release_id,
                event_type="registered",
                from_phase=None,
                to_phase=PHASE_ACCOUNT_A_CANARY,
                phase_version=1,
                idempotency_key=operation_key,
                actor=reviewer,
                reason=(
                    "stopped bootstrap release registered for account-a "
                    "through account-d"
                ),
                evidence=evidence,
            )
            _record_global_audit(
                cur,
                release_id=document.release_id,
                event_type="reviewed_release_registered",
                actor=reviewer,
                payload={
                    "phase": PHASE_ACCOUNT_A_CANARY,
                    "phase_version": 1,
                    "idempotency_key": operation_key,
                    **evidence,
                },
            )
            return _rollout_payload(
                rollout,
                idempotent=False,
            )


def migration_rebaseline_register_reviewed_release(
    conn,
    document: ReleaseDocument,
    capacity_evidence: RedisFencingEpochEvidence,
    *,
    reviewed_by: str,
    idempotency_key: str,
    operation_lock: AccountStallOperationLock,
) -> dict[str, Any]:
    """Register a fresh-host release while preserving migrated rollout history."""
    operation_lock.require_held()
    reviewer = _required_text(reviewed_by, "reviewed_by")
    if not isinstance(capacity_evidence, RedisFencingEpochEvidence):
        raise ReleaseRolloutError("capacity_evidence is required")
    operation_key = _required_text(
        idempotency_key,
        "idempotency_key",
    )
    if dict(document.node_ids) != EXPECTED_NODE_IDS:
        raise ReleaseRolloutError(
            "reviewed release node/account exact-set mismatch"
        )

    with conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            operation_lock.require_held()
            _lock_redis_fencing_epoch_domain(cur)
            _require_no_active_maintenance_fence_for_migration(cur)
            existing_by_key = _rollout_by_registration_key(
                cur,
                operation_key,
            )
            history = _bootstrap_registration_history(cur)
            if existing_by_key is not None:
                _require_rollout_matches_document(
                    existing_by_key,
                    document,
                    capacity_evidence,
                    operation_key,
                    reviewer,
                )
                _require_migration_rebaseline_replay(
                    cur,
                    document=document,
                    capacity_evidence=capacity_evidence,
                    reviewed_by=reviewer,
                    idempotency_key=operation_key,
                )
                _require_registered_manifests(cur, document)
                return _rollout_payload(
                    existing_by_key,
                    idempotent=True,
                )

            predecessor = _lock_migration_rebaseline_predecessor(
                cur,
                document=document,
                capacity_evidence=capacity_evidence,
                history=history,
            )
            operation_lock.require_held()
            _abort_migration_rebaseline_predecessor(
                cur,
                predecessor=predecessor,
                successor_document=document,
                successor_capacity_evidence=capacity_evidence,
                actor=reviewer,
            )
            operation_lock.require_held()
            _activate_redis_fencing_epoch(
                cur,
                capacity_evidence,
                activated_by=reviewer,
            )
            operation_lock.require_held()
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
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING *
                """,
                (
                    document.release_id,
                    capacity_evidence.redis_fencing_epoch,
                    document.image_digest,
                    document.config_sha256,
                    document.dependency_lock_sha256,
                    document.schema_epoch,
                    document.manifest_sha256,
                    document.bundle_manifest_sha256,
                    operation_key,
                    reviewer,
                ),
            )
            rollout = cur.fetchone()
            if rollout is None:
                raise ReleaseRolloutError(
                    "migration rebaseline rollout registration conflicted"
                )

            _require_rollout_matches_document(
                rollout,
                document,
                capacity_evidence,
                operation_key,
                reviewer,
            )
            _register_account_manifests(
                cur,
                document,
                reviewed_by=reviewer,
            )
            _require_registered_manifests(cur, document)
            evidence = {
                "registration_mode": (
                    MIGRATION_REBASELINE_REGISTRATION_MODE
                ),
                "bootstrap_all_halted": True,
                "all_accounts_stopped": True,
                "predecessor_release_id": predecessor["release_id"],
                "predecessor_phase": predecessor["phase"],
                "predecessor_phase_version": int(
                    predecessor["phase_version"]
                ),
                "predecessor_redis_fencing_epoch": str(
                    predecessor["redis_fencing_epoch"]
                ),
                "migration_history": {
                    "redis_fencing_epoch_count": (
                        history["redis_fencing_epoch_count"]
                    ),
                    "reviewed_release_rollout_count": (
                        history["reviewed_release_rollout_count"]
                    ),
                },
                "accounts": list(ACCOUNTS),
                "delivery_mode": document.delivery_mode,
                "manifest_sha256": document.manifest_sha256,
                "bundle_manifest_sha256": document.bundle_manifest_sha256,
                "redis_fencing_epoch": (
                    capacity_evidence.redis_fencing_epoch
                ),
                "redis_fencing_epoch_marker_sha256": (
                    capacity_evidence.marker_sha256
                ),
                "redis_capacity_evidence_sha256": (
                    capacity_evidence.capacity_evidence_sha256
                ),
                "initial_redis_run_id": (
                    capacity_evidence.initial_redis_run_id
                ),
                "active_volume": capacity_evidence.active_volume,
            }
            _record_rollout_event(
                cur,
                release_id=document.release_id,
                event_type="registered",
                from_phase=None,
                to_phase=PHASE_ACCOUNT_A_CANARY,
                phase_version=1,
                idempotency_key=operation_key,
                actor=reviewer,
                reason=(
                    "stopped migration rebaseline release registered for "
                    "account-a through account-d"
                ),
                evidence=evidence,
            )
            _record_global_audit(
                cur,
                release_id=document.release_id,
                event_type="reviewed_release_registered",
                actor=reviewer,
                payload={
                    "phase": PHASE_ACCOUNT_A_CANARY,
                    "phase_version": 1,
                    "idempotency_key": operation_key,
                    **evidence,
                },
            )
            return _rollout_payload(
                rollout,
                idempotent=False,
            )


def advance_rollout(
    conn,
    release_id: str,
    *,
    to_phase: str,
    actor: str,
    reason: str,
    idempotency_key: str,
    heartbeat_max_age_seconds: float = DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    closure_report_path: Path | None = None,
    closure_signature_path: Path | None = None,
    closure_public_key_path: Path | None = None,
    operation_lock: AccountStallOperationLock,
) -> dict[str, Any]:
    operation_lock.require_held()
    normalized_release_id = _required_text(release_id, "release_id")
    target_phase = _required_phase(to_phase)
    normalized_actor = _required_text(actor, "actor")
    normalized_reason = _required_text(reason, "reason")
    operation_key = _required_text(
        idempotency_key,
        "idempotency_key",
    )
    max_age_seconds = _heartbeat_max_age(heartbeat_max_age_seconds)

    with conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            operation_lock.require_held()
            maintenance_fence = _require_maintenance_fence(
                cur,
                operation_lock,
                stage=f"advance:{target_phase}",
            )
            existing_event = _event_by_idempotency_key(
                cur,
                operation_key,
            )
            if existing_event is not None:
                if (
                    existing_event["release_id"] != normalized_release_id
                    or existing_event["to_phase"] != target_phase
                    or existing_event["actor"] != normalized_actor
                    or existing_event["reason"] != normalized_reason
                ):
                    raise ReleaseRolloutError(
                        "transition idempotency key payload mismatch"
                    )
                rollout = _rollout_by_release_id(
                    cur,
                    normalized_release_id,
                    for_update=True,
                )
                if rollout is None:
                    raise ReleaseRolloutError(
                        "reviewed release rollout is missing"
                    )
                closure_report = _closure_report_for_transition(
                    rollout,
                    target_phase=target_phase,
                    report_path=closure_report_path,
                    signature_path=closure_signature_path,
                    public_key_path=closure_public_key_path,
                )
                _require_idempotent_closure_report(
                    existing_event,
                    closure_report,
                )
                if target_phase != PHASE_ABORTED:
                    _require_active_epoch_matches_rollout(cur, rollout)
                return _rollout_payload(rollout, idempotent=True)

            rollout = _rollout_by_release_id(
                cur,
                normalized_release_id,
                for_update=True,
            )
            if rollout is None:
                raise ReleaseRolloutError(
                    "reviewed release rollout is missing"
                )
            if target_phase != PHASE_ABORTED:
                _require_active_epoch_matches_rollout(cur, rollout)
            current_phase = str(rollout["phase"])
            if current_phase == target_phase:
                return _rollout_payload(rollout, idempotent=True)
            allowed = ALLOWED_TRANSITIONS.get(current_phase, set())
            if target_phase not in allowed:
                raise ReleaseRolloutError(
                    "invalid reviewed release rollout transition: "
                    f"{current_phase} -> {target_phase}"
                )
            closure_report = _closure_report_for_transition(
                rollout,
                target_phase=target_phase,
                report_path=closure_report_path,
                signature_path=closure_signature_path,
                public_key_path=closure_public_key_path,
            )

            evidence: dict[str, Any] = {
                "maintenance_fence": maintenance_fence,
            }
            if closure_report is not None:
                evidence["canary_closure_report"] = (
                    closure_report.evidence()
                )
            readiness = ROLLOUT_READINESS.get(target_phase)
            if readiness is not None:
                upgraded_accounts, next_account = readiness
                evidence["heartbeats"] = _require_account_rollout_readiness(
                    cur,
                    rollout,
                    upgraded_accounts=upgraded_accounts,
                    next_account=next_account,
                    max_age_seconds=max_age_seconds,
                )
            if target_phase == PHASE_FLEET_COMPLETE:
                evidence["heartbeats"] = _require_matching_heartbeats(
                    cur,
                    rollout,
                    accounts=ACCOUNTS,
                    max_age_seconds=max_age_seconds,
                )

            next_version = int(rollout["phase_version"]) + 1
            operation_lock.require_held()
            cur.execute(
                """
                UPDATE reviewed_release_rollouts
                SET phase=%s,
                    phase_version=%s
                WHERE release_id=%s
                  AND phase=%s
                  AND phase_version=%s
                RETURNING *
                """,
                (
                    target_phase,
                    next_version,
                    normalized_release_id,
                    current_phase,
                    rollout["phase_version"],
                ),
            )
            updated = cur.fetchone()
            if updated is None:
                raise ReleaseRolloutError(
                    "reviewed release rollout changed concurrently"
                )
            _record_rollout_event(
                cur,
                release_id=normalized_release_id,
                event_type="phase_transition",
                from_phase=current_phase,
                to_phase=target_phase,
                phase_version=next_version,
                idempotency_key=operation_key,
                actor=normalized_actor,
                reason=normalized_reason,
                evidence=evidence,
            )
            _record_global_audit(
                cur,
                release_id=normalized_release_id,
                event_type="reviewed_release_phase_transition",
                actor=normalized_actor,
                payload={
                    "from_phase": current_phase,
                    "to_phase": target_phase,
                    "phase_version": next_version,
                    "idempotency_key": operation_key,
                    "reason": normalized_reason,
                    **evidence,
                },
            )
            return _rollout_payload(updated, idempotent=False)


def finalize_fleet_rollout(
    conn,
    release_id: str,
    *,
    actor: str,
    reason: str,
    idempotency_key: str,
    heartbeat_max_age_seconds: float = DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    closure_report_path: Path | None = None,
    closure_signature_path: Path | None = None,
    closure_public_key_path: Path | None = None,
    operation_lock: AccountStallOperationLock,
) -> dict[str, Any]:
    operation_lock.require_held()
    normalized_actor = _required_text(actor, "actor")
    max_age_seconds = _heartbeat_max_age(heartbeat_max_age_seconds)
    fence_id = str(uuid4())
    owner_token = operation_lock.owner_token
    _acquire_finalize_maintenance_fence(
        conn,
        fence_id=fence_id,
        actor=normalized_actor,
        owner_token=owner_token,
        heartbeat_max_age_seconds=max_age_seconds,
    )

    previous_environment = {
        MAINTENANCE_FENCE_OWNERSHIP_ENV: os.environ.get(
            MAINTENANCE_FENCE_OWNERSHIP_ENV
        ),
        MAINTENANCE_FENCE_ID_ENV: os.environ.get(
            MAINTENANCE_FENCE_ID_ENV
        ),
        MAINTENANCE_FENCE_OWNER_TOKEN_ENV: os.environ.get(
            MAINTENANCE_FENCE_OWNER_TOKEN_ENV
        ),
    }
    os.environ[MAINTENANCE_FENCE_OWNERSHIP_ENV] = "inherited"
    os.environ[MAINTENANCE_FENCE_ID_ENV] = fence_id
    os.environ[MAINTENANCE_FENCE_OWNER_TOKEN_ENV] = owner_token
    primary_error: BaseException | None = None
    try:
        return advance_rollout(
            conn,
            release_id,
            to_phase=PHASE_FLEET_COMPLETE,
            actor=normalized_actor,
            reason=reason,
            idempotency_key=idempotency_key,
            heartbeat_max_age_seconds=max_age_seconds,
            closure_report_path=closure_report_path,
            closure_signature_path=closure_signature_path,
            closure_public_key_path=closure_public_key_path,
            operation_lock=operation_lock,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        _restore_maintenance_fence_environment(previous_environment)
        try:
            _release_finalize_maintenance_fence(
                conn,
                fence_id=fence_id,
                owner_token=owner_token,
                actor=normalized_actor,
                reason="fleet finalization complete",
            )
        except Exception:
            if primary_error is None:
                raise


def _acquire_finalize_maintenance_fence(
    conn,
    *,
    fence_id: str,
    actor: str,
    owner_token: str,
    heartbeat_max_age_seconds: float,
) -> None:
    heartbeat_max_age = int(heartbeat_max_age_seconds)
    if heartbeat_max_age != heartbeat_max_age_seconds:
        raise ReleaseRolloutError(
            "fleet finalization heartbeat max age must be an integer"
        )
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fence_id::text
                FROM acquire_control_plane_maintenance_fence(
                    %s, 'deploy', %s, %s, %s, %s
                )
                """,
                (
                    fence_id,
                    actor,
                    owner_token,
                    120,
                    heartbeat_max_age,
                ),
            )
            row = cur.fetchone()
    if row is None or str(row[0]) != fence_id:
        raise ReleaseRolloutError(
            "fleet finalization maintenance fence acquisition failed"
        )


def _release_finalize_maintenance_fence(
    conn,
    *,
    fence_id: str,
    owner_token: str,
    actor: str,
    reason: str,
) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT release_control_plane_maintenance_fence(
                    %s, %s, %s, %s
                )
                """,
                (fence_id, owner_token, actor, reason),
            )
            row = cur.fetchone()
    if row is None or row[0] is not True:
        raise ReleaseRolloutError(
            "fleet finalization maintenance fence release failed"
        )


def _restore_maintenance_fence_environment(
    previous_environment: dict[str, str | None],
) -> None:
    for key, value in previous_environment.items():
        if value is None:
            os.environ.pop(key, None)
            continue
        os.environ[key] = value


def _closure_report_for_transition(
    rollout: dict[str, Any],
    *,
    target_phase: str,
    report_path: Path | None,
    signature_path: Path | None,
    public_key_path: Path | None,
) -> LiveCanaryClosureReport | None:
    expected_account = CLOSURE_ACCOUNT_BY_TARGET_PHASE.get(target_phase)
    if expected_account is None:
        return None
    if (
        report_path is None
        or signature_path is None
        or public_key_path is None
    ):
        raise ReleaseRolloutError(
            f"{expected_account} signed closure report is required"
        )
    report = load_live_canary_closure_report(
        report_path,
        signature_path,
        public_key_path,
    )
    if report.account_id != expected_account:
        raise ReleaseRolloutError(
            f"{target_phase} requires {expected_account} closure report"
        )
    expected_identity = (
        str(rollout["release_id"]),
        str(rollout["image_digest"]),
        str(rollout["config_sha256"]),
        str(rollout["dependency_lock_sha256"]),
    )
    report_identity = (
        report.release_id,
        report.image_digest,
        report.config_sha256,
        report.dependency_lock_sha256,
    )
    if report_identity != expected_identity:
        raise ReleaseRolloutError(
            "closure report release identity mismatch"
        )
    return report


def _require_idempotent_closure_report(
    existing_event: dict[str, Any],
    closure_report: LiveCanaryClosureReport | None,
) -> None:
    evidence = existing_event.get("evidence")
    stored_report = None
    if isinstance(evidence, dict):
        stored_report = evidence.get("canary_closure_report")
    if closure_report is None:
        if stored_report is not None:
            raise ReleaseRolloutError(
                "transition idempotency closure report mismatch"
            )
        return
    if stored_report != closure_report.evidence():
        raise ReleaseRolloutError(
            "transition idempotency closure report mismatch"
        )


def _lock_same_epoch_hotfix_predecessor(
    cur,
    *,
    document: ReleaseDocument,
    capacity_evidence: RedisFencingEpochEvidence,
    active_redis_fencing_epoch: str,
) -> dict[str, Any]:
    if active_redis_fencing_epoch != capacity_evidence.redis_fencing_epoch:
        raise ReleaseRolloutError(
            "same-epoch hotfix requires the active Redis fencing epoch"
        )
    cur.execute(
        """
        SELECT release_id,
               redis_fencing_epoch::text AS redis_fencing_epoch,
               phase,
               phase_version
        FROM reviewed_release_rollouts
        WHERE phase = ANY(%s)
        FOR UPDATE
        """,
        (list(ACTIVE_ROLLOUT_PHASES),),
    )
    active_rollouts = cur.fetchall()
    if len(active_rollouts) != 1:
        raise ReleaseRolloutError(
            "same-epoch hotfix requires exactly one active predecessor rollout"
        )
    predecessor = active_rollouts[0]
    if predecessor["release_id"] == document.release_id:
        raise ReleaseRolloutError(
            "same-epoch hotfix predecessor must differ from successor"
        )
    if str(predecessor["redis_fencing_epoch"]) != active_redis_fencing_epoch:
        raise ReleaseRolloutError(
            "same-epoch hotfix predecessor Redis epoch is not active"
        )
    return predecessor


def _abort_same_epoch_hotfix_predecessor(
    cur,
    *,
    predecessor: dict[str, Any],
    successor_document: ReleaseDocument,
    successor_capacity_evidence: RedisFencingEpochEvidence,
    actor: str,
) -> None:
    predecessor_release_id = str(predecessor["release_id"])
    predecessor_phase = str(predecessor["phase"])
    predecessor_version = int(predecessor["phase_version"])
    next_version = predecessor_version + 1
    abort_key = f"same-epoch-hotfix-abort:{successor_document.release_id}"
    reason = "superseded by same-epoch reviewed hotfix"
    evidence = {
        "registration_mode": SAME_EPOCH_HOTFIX_REGISTRATION_MODE,
        "redis_epoch_reused": True,
        "successor_release_id": successor_document.release_id,
        "predecessor_redis_fencing_epoch": str(
            predecessor["redis_fencing_epoch"]
        ),
        "successor_redis_fencing_epoch": (
            successor_capacity_evidence.redis_fencing_epoch
        ),
    }
    cur.execute(
        """
        UPDATE reviewed_release_rollouts
        SET phase=%s,
            phase_version=%s
        WHERE release_id=%s
          AND phase=%s
          AND phase_version=%s
        RETURNING release_id
        """,
        (
            PHASE_ABORTED,
            next_version,
            predecessor_release_id,
            predecessor_phase,
            predecessor_version,
        ),
    )
    if cur.fetchone() is None:
        raise ReleaseRolloutError(
            "same-epoch hotfix predecessor changed concurrently"
        )
    _record_rollout_event(
        cur,
        release_id=predecessor_release_id,
        event_type="phase_transition",
        from_phase=predecessor_phase,
        to_phase=PHASE_ABORTED,
        phase_version=next_version,
        idempotency_key=abort_key,
        actor=actor,
        reason=reason,
        evidence=evidence,
    )
    _record_global_audit(
        cur,
        release_id=predecessor_release_id,
        event_type="reviewed_release_phase_transition",
        actor=actor,
        payload={
            "from_phase": predecessor_phase,
            "to_phase": PHASE_ABORTED,
            "phase_version": next_version,
            "idempotency_key": abort_key,
            "reason": reason,
            **evidence,
        },
    )


def get_rollout(conn, release_id: str) -> dict[str, Any]:
    normalized_release_id = _required_text(release_id, "release_id")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        rollout = _rollout_by_release_id(cur, normalized_release_id)
        if rollout is None:
            raise ReleaseRolloutError(
                "reviewed release rollout is missing"
            )
        cur.execute(
            """
            SELECT event_type,
                   from_phase,
                   to_phase,
                   phase_version,
                   idempotency_key,
                   actor,
                   reason,
                   evidence,
                   created_at
            FROM reviewed_release_rollout_events
            WHERE release_id=%s
            ORDER BY phase_version
            """,
            (normalized_release_id,),
        )
        events = [_jsonable_row(row) for row in cur.fetchall()]
    payload = _rollout_payload(rollout, idempotent=False)
    payload["events"] = events
    return payload


def _lock_redis_fencing_epoch_domain(cur) -> None:
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        ("trader-v3-redis-fencing-epoch",),
    )


def _bootstrap_registration_history(cur) -> dict[str, int]:
    cur.execute(
        """
        SELECT (
            SELECT count(*)
            FROM redis_fencing_epochs
        ) AS redis_fencing_epoch_count,
        (
            SELECT count(*)
            FROM reviewed_release_rollouts
        ) AS reviewed_release_rollout_count
        """
    )
    row = cur.fetchone()
    if row is None:
        raise ReleaseRolloutError(
            "bootstrap registration history is unavailable"
        )
    return {
        "redis_fencing_epoch_count": int(
            row["redis_fencing_epoch_count"]
        ),
        "reviewed_release_rollout_count": int(
            row["reviewed_release_rollout_count"]
        ),
    }


def _require_empty_bootstrap_history(history: dict[str, int]) -> None:
    if history["redis_fencing_epoch_count"] != 0:
        raise ReleaseRolloutError(
            "bootstrap registration requires no Redis fencing epoch history"
        )
    if history["reviewed_release_rollout_count"] != 0:
        raise ReleaseRolloutError(
            "bootstrap registration requires no reviewed release rollout history"
        )


def _bootstrap_history_is_empty(history: dict[str, int]) -> bool:
    redis_count = history["redis_fencing_epoch_count"]
    rollout_count = history["reviewed_release_rollout_count"]
    if redis_count == 0 and rollout_count == 0:
        return True
    if redis_count < 1 or rollout_count < 1:
        raise ReleaseRolloutError(
            "bootstrap stopped recovery detected partial rollout history"
        )
    return False


def _require_bootstrap_replay_history(history: dict[str, int]) -> None:
    if history["redis_fencing_epoch_count"] != 1:
        raise ReleaseRolloutError(
            "bootstrap replay requires exactly one Redis fencing epoch"
        )
    if history["reviewed_release_rollout_count"] != 1:
        raise ReleaseRolloutError(
            "bootstrap replay requires exactly one reviewed release rollout"
        )


def _bootstrap_stopped_abort_key(successor_release_id: str) -> str:
    return f"bootstrap-stopped-abort:{successor_release_id}"


def _require_no_active_maintenance_fence_for_bootstrap_stopped(cur) -> None:
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        ("trader-v3-control-plane-maintenance-fence",),
    )
    cur.execute(
        """
        SELECT fence_id::text AS fence_id
        FROM control_plane_maintenance_fences
        WHERE domain=%s
          AND status='active'
        FOR UPDATE
        """,
        (REDIS_FENCING_DOMAIN,),
    )
    if cur.fetchall():
        raise ReleaseRolloutError(
            "bootstrap stopped recovery requires no active maintenance fence"
        )


def _lock_bootstrap_stopped_predecessor(
    cur,
    *,
    document: ReleaseDocument,
    capacity_evidence: RedisFencingEpochEvidence,
    history: dict[str, int],
) -> dict[str, Any]:
    _bootstrap_history_is_empty(history)
    active_epoch = _active_redis_fencing_epoch(
        cur,
        for_update=True,
    )
    if active_epoch != capacity_evidence.redis_fencing_epoch:
        raise ReleaseRolloutError(
            "bootstrap stopped recovery requires the active Redis fencing epoch"
        )
    cur.execute(
        """
        SELECT release_id,
               redis_fencing_epoch::text AS redis_fencing_epoch,
               phase,
               phase_version
        FROM reviewed_release_rollouts
        WHERE phase = ANY(%s)
        FOR UPDATE
        """,
        (list(ACTIVE_ROLLOUT_PHASES),),
    )
    active_rollouts = cur.fetchall()
    if len(active_rollouts) != 1:
        raise ReleaseRolloutError(
            "bootstrap stopped recovery requires exactly one active "
            "predecessor rollout"
        )
    predecessor = active_rollouts[0]
    if predecessor["release_id"] == document.release_id:
        raise ReleaseRolloutError(
            "bootstrap stopped predecessor must differ from successor"
        )
    if str(predecessor["redis_fencing_epoch"]) != active_epoch:
        raise ReleaseRolloutError(
            "bootstrap stopped predecessor Redis epoch is not active"
        )
    return predecessor


def _abort_bootstrap_stopped_predecessor(
    cur,
    *,
    predecessor: dict[str, Any],
    successor_document: ReleaseDocument,
    successor_capacity_evidence: RedisFencingEpochEvidence,
    actor: str,
) -> None:
    predecessor_release_id = str(predecessor["release_id"])
    predecessor_phase = str(predecessor["phase"])
    predecessor_version = int(predecessor["phase_version"])
    next_version = predecessor_version + 1
    abort_key = _bootstrap_stopped_abort_key(
        successor_document.release_id
    )
    reason = "superseded by stopped bootstrap recovery"
    evidence = {
        "registration_mode": BOOTSTRAP_STOPPED_REGISTRATION_MODE,
        "all_accounts_stopped": True,
        "redis_epoch_reused": True,
        "successor_release_id": successor_document.release_id,
        "predecessor_redis_fencing_epoch": str(
            predecessor["redis_fencing_epoch"]
        ),
        "successor_redis_fencing_epoch": (
            successor_capacity_evidence.redis_fencing_epoch
        ),
    }
    cur.execute(
        """
        UPDATE reviewed_release_rollouts
        SET phase=%s,
            phase_version=%s
        WHERE release_id=%s
          AND phase=%s
          AND phase_version=%s
        RETURNING release_id
        """,
        (
            PHASE_ABORTED,
            next_version,
            predecessor_release_id,
            predecessor_phase,
            predecessor_version,
        ),
    )
    if cur.fetchone() is None:
        raise ReleaseRolloutError(
            "bootstrap stopped predecessor changed concurrently"
        )
    _record_rollout_event(
        cur,
        release_id=predecessor_release_id,
        event_type="phase_transition",
        from_phase=predecessor_phase,
        to_phase=PHASE_ABORTED,
        phase_version=next_version,
        idempotency_key=abort_key,
        actor=actor,
        reason=reason,
        evidence=evidence,
    )
    _record_global_audit(
        cur,
        release_id=predecessor_release_id,
        event_type="reviewed_release_phase_transition",
        actor=actor,
        payload={
            "from_phase": predecessor_phase,
            "to_phase": PHASE_ABORTED,
            "phase_version": next_version,
            "idempotency_key": abort_key,
            "reason": reason,
            **evidence,
        },
    )


def _require_bootstrap_registration_replay(
    cur,
    *,
    document: ReleaseDocument,
    capacity_evidence: RedisFencingEpochEvidence,
    history: dict[str, int],
    idempotency_key: str,
    reviewed_by: str,
) -> None:
    cur.execute(
        """
        SELECT evidence
        FROM reviewed_release_rollout_events
        WHERE idempotency_key=%s
        FOR SHARE
        """,
        (idempotency_key,),
    )
    event = cur.fetchone()
    if event is None or not isinstance(event["evidence"], dict):
        raise ReleaseRolloutError(
            "bootstrap replay registration evidence is missing"
        )
    registration_mode = event["evidence"].get("registration_mode")
    if registration_mode == "bootstrap":
        _require_bootstrap_replay_history(history)
        _require_bootstrap_registration_event(
            cur,
            release_id=document.release_id,
            idempotency_key=idempotency_key,
            reviewed_by=reviewed_by,
        )
        _require_bootstrap_registration_audit(
            cur,
            release_id=document.release_id,
            idempotency_key=idempotency_key,
            reviewed_by=reviewed_by,
        )
        return
    if registration_mode != BOOTSTRAP_STOPPED_REGISTRATION_MODE:
        raise ReleaseRolloutError(
            "bootstrap replay registration mode conflicts"
        )
    predecessor_release_id = _require_bootstrap_stopped_abort_event(
        cur,
        successor_release_id=document.release_id,
        successor_redis_fencing_epoch=(
            capacity_evidence.redis_fencing_epoch
        ),
        reviewed_by=reviewed_by,
    )
    _require_bootstrap_stopped_abort_audit(
        cur,
        predecessor_release_id=predecessor_release_id,
        successor_release_id=document.release_id,
        successor_redis_fencing_epoch=(
            capacity_evidence.redis_fencing_epoch
        ),
        reviewed_by=reviewed_by,
    )
    _require_stopped_registration_event(
        cur,
        release_id=document.release_id,
        idempotency_key=idempotency_key,
        reviewed_by=reviewed_by,
        registration_mode=BOOTSTRAP_STOPPED_REGISTRATION_MODE,
        context="bootstrap stopped replay",
        predecessor_release_id=predecessor_release_id,
    )
    _require_stopped_registration_audit(
        cur,
        release_id=document.release_id,
        idempotency_key=idempotency_key,
        reviewed_by=reviewed_by,
        registration_mode=BOOTSTRAP_STOPPED_REGISTRATION_MODE,
        context="bootstrap stopped replay",
        predecessor_release_id=predecessor_release_id,
    )


def _require_bootstrap_stopped_abort_event(
    cur,
    *,
    successor_release_id: str,
    successor_redis_fencing_epoch: str,
    reviewed_by: str,
) -> str:
    abort_key = _bootstrap_stopped_abort_key(successor_release_id)
    cur.execute(
        """
        SELECT release_id,
               event_type,
               from_phase,
               to_phase,
               phase_version,
               actor,
               reason,
               evidence
        FROM reviewed_release_rollout_events
        WHERE idempotency_key=%s
        FOR SHARE
        """,
        (abort_key,),
    )
    row = cur.fetchone()
    if row is None:
        raise ReleaseRolloutError(
            "bootstrap stopped predecessor abort event is missing"
        )
    evidence = row["evidence"]
    if not isinstance(evidence, dict):
        raise ReleaseRolloutError(
            "bootstrap stopped predecessor abort evidence is invalid"
        )
    expected = (
        "phase_transition",
        PHASE_ABORTED,
        reviewed_by,
        "superseded by stopped bootstrap recovery",
        BOOTSTRAP_STOPPED_REGISTRATION_MODE,
        True,
        True,
        successor_release_id,
        successor_redis_fencing_epoch,
    )
    actual = (
        row["event_type"],
        row["to_phase"],
        row["actor"],
        row["reason"],
        evidence.get("registration_mode"),
        evidence.get("all_accounts_stopped"),
        evidence.get("redis_epoch_reused"),
        evidence.get("successor_release_id"),
        evidence.get("successor_redis_fencing_epoch"),
    )
    if (
        row["from_phase"] not in ACTIVE_ROLLOUT_PHASES
        or int(row["phase_version"]) <= 1
        or actual != expected
    ):
        raise ReleaseRolloutError(
            "bootstrap stopped predecessor abort event conflicts"
        )
    predecessor_release_id = str(row["release_id"] or "").strip()
    if not predecessor_release_id:
        raise ReleaseRolloutError(
            "bootstrap stopped predecessor release id is invalid"
        )
    return predecessor_release_id


def _require_bootstrap_stopped_abort_audit(
    cur,
    *,
    predecessor_release_id: str,
    successor_release_id: str,
    successor_redis_fencing_epoch: str,
    reviewed_by: str,
) -> None:
    abort_key = _bootstrap_stopped_abort_key(successor_release_id)
    cur.execute(
        """
        SELECT actor,
               payload
        FROM audit_events
        WHERE event_type='reviewed_release_phase_transition'
          AND aggregate_type='reviewed_release_rollout'
          AND aggregate_id=%s
        FOR SHARE
        """,
        (predecessor_release_id,),
    )
    matching = []
    for row in cur.fetchall():
        payload = row["payload"]
        if not isinstance(payload, dict):
            continue
        if payload.get("idempotency_key") != abort_key:
            continue
        matching.append((row["actor"], payload))
    if len(matching) != 1:
        raise ReleaseRolloutError(
            "bootstrap stopped predecessor abort audit is missing or ambiguous"
        )
    actor, payload = matching[0]
    expected = (
        reviewed_by,
        PHASE_ABORTED,
        BOOTSTRAP_STOPPED_REGISTRATION_MODE,
        True,
        True,
        successor_release_id,
        successor_redis_fencing_epoch,
    )
    actual = (
        actor,
        payload.get("to_phase"),
        payload.get("registration_mode"),
        payload.get("all_accounts_stopped"),
        payload.get("redis_epoch_reused"),
        payload.get("successor_release_id"),
        payload.get("successor_redis_fencing_epoch"),
    )
    if actual != expected:
        raise ReleaseRolloutError(
            "bootstrap stopped predecessor abort audit conflicts"
        )


def _migration_rebaseline_abort_key(successor_release_id: str) -> str:
    return f"migration-rebaseline-abort:{successor_release_id}"


def _require_no_active_maintenance_fence_for_migration(cur) -> None:
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        ("trader-v3-control-plane-maintenance-fence",),
    )
    cur.execute(
        """
        SELECT fence_id::text AS fence_id
        FROM control_plane_maintenance_fences
        WHERE domain=%s
          AND status='active'
        FOR UPDATE
        """,
        (REDIS_FENCING_DOMAIN,),
    )
    if cur.fetchall():
        raise ReleaseRolloutError(
            "migration rebaseline requires no active maintenance fence"
        )


def _lock_migration_rebaseline_predecessor(
    cur,
    *,
    document: ReleaseDocument,
    capacity_evidence: RedisFencingEpochEvidence,
    history: dict[str, int],
) -> dict[str, Any]:
    if history["redis_fencing_epoch_count"] < 1:
        raise ReleaseRolloutError(
            "migration rebaseline requires Redis fencing epoch history"
        )
    if history["reviewed_release_rollout_count"] < 1:
        raise ReleaseRolloutError(
            "migration rebaseline requires reviewed rollout history"
        )
    cur.execute(
        """
        SELECT release_id,
               redis_fencing_epoch::text AS redis_fencing_epoch,
               phase,
               phase_version
        FROM reviewed_release_rollouts
        WHERE phase = ANY(%s)
        FOR UPDATE
        """,
        (list(ACTIVE_ROLLOUT_PHASES),),
    )
    active_rollouts = cur.fetchall()
    if len(active_rollouts) != 1:
        raise ReleaseRolloutError(
            "migration rebaseline requires exactly one active predecessor rollout"
        )
    predecessor = active_rollouts[0]
    if predecessor["release_id"] == document.release_id:
        raise ReleaseRolloutError(
            "migration rebaseline predecessor must differ from successor"
        )
    active_epoch = _active_redis_fencing_epoch(
        cur,
        for_update=True,
    )
    if str(predecessor["redis_fencing_epoch"]) != active_epoch:
        raise ReleaseRolloutError(
            "migration rebaseline predecessor Redis epoch is not active"
        )
    if active_epoch == capacity_evidence.redis_fencing_epoch:
        raise ReleaseRolloutError(
            "migration rebaseline requires a fresh Redis fencing epoch"
        )
    return predecessor


def _abort_migration_rebaseline_predecessor(
    cur,
    *,
    predecessor: dict[str, Any],
    successor_document: ReleaseDocument,
    successor_capacity_evidence: RedisFencingEpochEvidence,
    actor: str,
) -> None:
    predecessor_release_id = str(predecessor["release_id"])
    predecessor_phase = str(predecessor["phase"])
    predecessor_version = int(predecessor["phase_version"])
    next_version = predecessor_version + 1
    abort_key = _migration_rebaseline_abort_key(
        successor_document.release_id
    )
    reason = "superseded by stopped migration rebaseline"
    evidence = {
        "registration_mode": MIGRATION_REBASELINE_REGISTRATION_MODE,
        "all_accounts_stopped": True,
        "successor_release_id": successor_document.release_id,
        "predecessor_redis_fencing_epoch": str(
            predecessor["redis_fencing_epoch"]
        ),
        "successor_redis_fencing_epoch": (
            successor_capacity_evidence.redis_fencing_epoch
        ),
    }
    cur.execute(
        """
        UPDATE reviewed_release_rollouts
        SET phase=%s,
            phase_version=%s
        WHERE release_id=%s
          AND phase=%s
          AND phase_version=%s
        RETURNING release_id
        """,
        (
            PHASE_ABORTED,
            next_version,
            predecessor_release_id,
            predecessor_phase,
            predecessor_version,
        ),
    )
    if cur.fetchone() is None:
        raise ReleaseRolloutError(
            "migration rebaseline predecessor changed concurrently"
        )
    _record_rollout_event(
        cur,
        release_id=predecessor_release_id,
        event_type="phase_transition",
        from_phase=predecessor_phase,
        to_phase=PHASE_ABORTED,
        phase_version=next_version,
        idempotency_key=abort_key,
        actor=actor,
        reason=reason,
        evidence=evidence,
    )
    _record_global_audit(
        cur,
        release_id=predecessor_release_id,
        event_type="reviewed_release_phase_transition",
        actor=actor,
        payload={
            "from_phase": predecessor_phase,
            "to_phase": PHASE_ABORTED,
            "phase_version": next_version,
            "idempotency_key": abort_key,
            "reason": reason,
            **evidence,
        },
    )


def _require_migration_rebaseline_replay(
    cur,
    *,
    document: ReleaseDocument,
    capacity_evidence: RedisFencingEpochEvidence,
    reviewed_by: str,
    idempotency_key: str,
) -> None:
    _require_active_epoch_matches_rollout(
        cur,
        {
            "redis_fencing_epoch": capacity_evidence.redis_fencing_epoch,
        },
    )
    cur.execute(
        """
        SELECT release_id
        FROM reviewed_release_rollouts
        WHERE phase = ANY(%s)
        FOR SHARE
        """,
        (list(ACTIVE_ROLLOUT_PHASES),),
    )
    active_release_ids = [
        str(row["release_id"])
        for row in cur.fetchall()
    ]
    if active_release_ids != [document.release_id]:
        raise ReleaseRolloutError(
            "migration rebaseline replay active rollout differs"
        )
    predecessor_release_id = _require_migration_rebaseline_abort_event(
        cur,
        successor_release_id=document.release_id,
        successor_redis_fencing_epoch=(
            capacity_evidence.redis_fencing_epoch
        ),
        reviewed_by=reviewed_by,
    )
    _require_migration_rebaseline_abort_audit(
        cur,
        predecessor_release_id=predecessor_release_id,
        successor_release_id=document.release_id,
        successor_redis_fencing_epoch=(
            capacity_evidence.redis_fencing_epoch
        ),
        reviewed_by=reviewed_by,
    )
    _require_stopped_registration_event(
        cur,
        release_id=document.release_id,
        idempotency_key=idempotency_key,
        reviewed_by=reviewed_by,
        registration_mode=MIGRATION_REBASELINE_REGISTRATION_MODE,
        context="migration rebaseline replay",
        predecessor_release_id=predecessor_release_id,
    )
    _require_stopped_registration_audit(
        cur,
        release_id=document.release_id,
        idempotency_key=idempotency_key,
        reviewed_by=reviewed_by,
        registration_mode=MIGRATION_REBASELINE_REGISTRATION_MODE,
        context="migration rebaseline replay",
        predecessor_release_id=predecessor_release_id,
    )


def _require_migration_rebaseline_abort_event(
    cur,
    *,
    successor_release_id: str,
    successor_redis_fencing_epoch: str,
    reviewed_by: str,
) -> str:
    abort_key = _migration_rebaseline_abort_key(successor_release_id)
    cur.execute(
        """
        SELECT release_id,
               event_type,
               from_phase,
               to_phase,
               phase_version,
               actor,
               reason,
               evidence
        FROM reviewed_release_rollout_events
        WHERE idempotency_key=%s
        FOR SHARE
        """,
        (abort_key,),
    )
    row = cur.fetchone()
    if row is None:
        raise ReleaseRolloutError(
            "migration rebaseline predecessor abort event is missing"
        )
    if (
        row["event_type"] != "phase_transition"
        or row["from_phase"] not in ACTIVE_ROLLOUT_PHASES
        or row["to_phase"] != PHASE_ABORTED
        or int(row["phase_version"]) <= 1
        or row["actor"] != reviewed_by
        or row["reason"] != "superseded by stopped migration rebaseline"
    ):
        raise ReleaseRolloutError(
            "migration rebaseline predecessor abort event conflicts"
        )
    evidence = row["evidence"]
    if not isinstance(evidence, dict):
        raise ReleaseRolloutError(
            "migration rebaseline predecessor abort evidence is invalid"
        )
    expected = (
        MIGRATION_REBASELINE_REGISTRATION_MODE,
        True,
        successor_release_id,
        successor_redis_fencing_epoch,
    )
    actual = (
        evidence.get("registration_mode"),
        evidence.get("all_accounts_stopped"),
        evidence.get("successor_release_id"),
        evidence.get("successor_redis_fencing_epoch"),
    )
    if actual != expected:
        raise ReleaseRolloutError(
            "migration rebaseline predecessor abort evidence conflicts"
        )
    predecessor_release_id = str(row["release_id"] or "").strip()
    if not predecessor_release_id:
        raise ReleaseRolloutError(
            "migration rebaseline predecessor release id is invalid"
        )
    return predecessor_release_id


def _require_migration_rebaseline_abort_audit(
    cur,
    *,
    predecessor_release_id: str,
    successor_release_id: str,
    successor_redis_fencing_epoch: str,
    reviewed_by: str,
) -> None:
    abort_key = _migration_rebaseline_abort_key(successor_release_id)
    cur.execute(
        """
        SELECT actor,
               payload
        FROM audit_events
        WHERE event_type='reviewed_release_phase_transition'
          AND aggregate_type='reviewed_release_rollout'
          AND aggregate_id=%s
        FOR SHARE
        """,
        (predecessor_release_id,),
    )
    matching = []
    for row in cur.fetchall():
        payload = row["payload"]
        if not isinstance(payload, dict):
            continue
        if payload.get("idempotency_key") != abort_key:
            continue
        matching.append((row["actor"], payload))
    if len(matching) != 1:
        raise ReleaseRolloutError(
            "migration rebaseline predecessor abort audit is missing or ambiguous"
        )
    actor, payload = matching[0]
    expected = (
        reviewed_by,
        PHASE_ABORTED,
        MIGRATION_REBASELINE_REGISTRATION_MODE,
        True,
        successor_release_id,
        successor_redis_fencing_epoch,
    )
    actual = (
        actor,
        payload.get("to_phase"),
        payload.get("registration_mode"),
        payload.get("all_accounts_stopped"),
        payload.get("successor_release_id"),
        payload.get("successor_redis_fencing_epoch"),
    )
    if actual != expected:
        raise ReleaseRolloutError(
            "migration rebaseline predecessor abort audit conflicts"
        )


def _require_bootstrap_registration_event(
    cur,
    *,
    release_id: str,
    idempotency_key: str,
    reviewed_by: str,
) -> None:
    _require_stopped_registration_event(
        cur,
        release_id=release_id,
        idempotency_key=idempotency_key,
        reviewed_by=reviewed_by,
        registration_mode="bootstrap",
        context="bootstrap replay",
    )


def _require_stopped_registration_event(
    cur,
    *,
    release_id: str,
    idempotency_key: str,
    reviewed_by: str,
    registration_mode: str,
    context: str,
    predecessor_release_id: str | None = None,
) -> None:
    cur.execute(
        """
        SELECT release_id,
               event_type,
               from_phase,
               to_phase,
               phase_version,
               actor,
               evidence
        FROM reviewed_release_rollout_events
        WHERE idempotency_key=%s
        FOR SHARE
        """,
        (idempotency_key,),
    )
    row = cur.fetchone()
    if row is None:
        raise ReleaseRolloutError(
            f"{context} registration event is missing"
        )
    identity = (
        row["release_id"],
        row["event_type"],
        row["from_phase"],
        row["to_phase"],
        int(row["phase_version"]),
        row["actor"],
    )
    expected = (
        release_id,
        "registered",
        None,
        PHASE_ACCOUNT_A_CANARY,
        1,
        reviewed_by,
    )
    if identity != expected:
        raise ReleaseRolloutError(
            f"{context} registration event conflicts"
        )
    evidence = row["evidence"]
    if not isinstance(evidence, dict):
        raise ReleaseRolloutError(
            f"{context} registration evidence is invalid"
        )
    if evidence.get("registration_mode") != registration_mode:
        raise ReleaseRolloutError(
            f"{context} registration mode conflicts"
        )
    if evidence.get("bootstrap_all_halted") is not True:
        raise ReleaseRolloutError(
            f"{context} halted state conflicts"
        )
    if predecessor_release_id is None:
        return
    if evidence.get("all_accounts_stopped") is not True:
        raise ReleaseRolloutError(
            f"{context} stopped state conflicts"
        )
    if evidence.get("predecessor_release_id") != predecessor_release_id:
        raise ReleaseRolloutError(
            f"{context} predecessor release conflicts"
        )


def _require_bootstrap_registration_audit(
    cur,
    *,
    release_id: str,
    idempotency_key: str,
    reviewed_by: str,
) -> None:
    _require_stopped_registration_audit(
        cur,
        release_id=release_id,
        idempotency_key=idempotency_key,
        reviewed_by=reviewed_by,
        registration_mode="bootstrap",
        context="bootstrap replay",
    )


def _require_stopped_registration_audit(
    cur,
    *,
    release_id: str,
    idempotency_key: str,
    reviewed_by: str,
    registration_mode: str,
    context: str,
    predecessor_release_id: str | None = None,
) -> None:
    cur.execute(
        """
        SELECT actor,
               payload
        FROM audit_events
        WHERE event_type='reviewed_release_registered'
          AND aggregate_type='reviewed_release_rollout'
          AND aggregate_id=%s
        FOR SHARE
        """,
        (release_id,),
    )
    rows = cur.fetchall()
    matching = []
    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, dict):
            continue
        if payload.get("idempotency_key") != idempotency_key:
            continue
        matching.append((row["actor"], payload))
    if len(matching) != 1:
        raise ReleaseRolloutError(
            f"{context} registration audit is missing or ambiguous"
        )
    actor, payload = matching[0]
    if actor != reviewed_by:
        raise ReleaseRolloutError(
            f"{context} registration audit actor conflicts"
        )
    if payload.get("registration_mode") != registration_mode:
        raise ReleaseRolloutError(
            f"{context} registration audit mode conflicts"
        )
    if payload.get("bootstrap_all_halted") is not True:
        raise ReleaseRolloutError(
            f"{context} registration audit halted state conflicts"
        )
    if predecessor_release_id is None:
        return
    if payload.get("all_accounts_stopped") is not True:
        raise ReleaseRolloutError(
            f"{context} registration audit stopped state conflicts"
        )
    if payload.get("predecessor_release_id") != predecessor_release_id:
        raise ReleaseRolloutError(
            f"{context} registration audit predecessor release conflicts"
        )


def _has_bootstrap_all_halted_registration(
    cur,
    rollout: dict[str, Any],
) -> bool:
    """Return whether a rollout has complete bootstrap registration evidence."""
    registration_key = str(
        rollout["registration_idempotency_key"] or ""
    ).strip()
    reviewed_by = str(rollout["reviewed_by"] or "").strip()
    if not registration_key or not reviewed_by:
        raise ReleaseRolloutError(
            "rollout registration identity is invalid"
        )
    cur.execute(
        """
        SELECT evidence
        FROM reviewed_release_rollout_events
        WHERE idempotency_key=%s
        FOR SHARE
        """,
        (registration_key,),
    )
    event = cur.fetchone()
    if event is None:
        return False
    evidence = event["evidence"]
    if not isinstance(evidence, dict):
        return False
    registration_mode = evidence.get("registration_mode")
    all_halted = evidence.get("bootstrap_all_halted")
    stopped_registration_modes = {
        "bootstrap",
        BOOTSTRAP_STOPPED_REGISTRATION_MODE,
        MIGRATION_REBASELINE_REGISTRATION_MODE,
    }
    bootstrap_marked = (
        registration_mode in stopped_registration_modes
        or all_halted is not None
    )
    if not bootstrap_marked:
        return False
    if (
        registration_mode not in stopped_registration_modes
        or all_halted is not True
    ):
        raise ReleaseRolloutError(
            "bootstrap registration evidence is incomplete"
        )
    if registration_mode == "bootstrap":
        _require_bootstrap_registration_event(
            cur,
            release_id=str(rollout["release_id"]),
            idempotency_key=registration_key,
            reviewed_by=reviewed_by,
        )
        _require_bootstrap_registration_audit(
            cur,
            release_id=str(rollout["release_id"]),
            idempotency_key=registration_key,
            reviewed_by=reviewed_by,
        )
    else:
        predecessor_release_id = str(
            evidence.get("predecessor_release_id") or ""
        ).strip()
        if not predecessor_release_id:
            raise ReleaseRolloutError(
                "migration rebaseline predecessor evidence is incomplete"
            )
        audited_predecessor_release_id = (
            _require_migration_rebaseline_abort_event(
                cur,
                successor_release_id=str(rollout["release_id"]),
                successor_redis_fencing_epoch=str(
                    rollout["redis_fencing_epoch"]
                ),
                reviewed_by=reviewed_by,
            )
        )
        if audited_predecessor_release_id != predecessor_release_id:
            raise ReleaseRolloutError(
                "migration rebaseline predecessor audit differs"
            )
        _require_migration_rebaseline_abort_audit(
            cur,
            predecessor_release_id=predecessor_release_id,
            successor_release_id=str(rollout["release_id"]),
            successor_redis_fencing_epoch=str(
                rollout["redis_fencing_epoch"]
            ),
            reviewed_by=reviewed_by,
        )
        _require_stopped_registration_event(
            cur,
            release_id=str(rollout["release_id"]),
            idempotency_key=registration_key,
            reviewed_by=reviewed_by,
            registration_mode=MIGRATION_REBASELINE_REGISTRATION_MODE,
            context="migration rebaseline readiness",
            predecessor_release_id=predecessor_release_id,
        )
        _require_stopped_registration_audit(
            cur,
            release_id=str(rollout["release_id"]),
            idempotency_key=registration_key,
            reviewed_by=reviewed_by,
            registration_mode=MIGRATION_REBASELINE_REGISTRATION_MODE,
            context="migration rebaseline readiness",
            predecessor_release_id=predecessor_release_id,
        )
    return True


def _require_maintenance_fence(
    cur,
    operation_lock: AccountStallOperationLock,
    *,
    stage: str,
) -> dict[str, Any]:
    operation_lock.require_held()
    ownership = os.environ.get(
        MAINTENANCE_FENCE_OWNERSHIP_ENV,
        "",
    ).strip()
    if ownership != "inherited":
        raise ReleaseRolloutError(
            "mutation requires an inherited maintenance fence"
        )
    fence_id_raw = os.environ.get(
        MAINTENANCE_FENCE_ID_ENV,
        "",
    ).strip()
    try:
        fence_id = UUID(fence_id_raw)
    except ValueError as exc:
        raise ReleaseRolloutError(
            "maintenance fence id is invalid"
        ) from exc
    if str(fence_id) != fence_id_raw:
        raise ReleaseRolloutError(
            "maintenance fence id must be canonical"
        )
    owner_token = operation_lock.owner_token
    inherited_owner_token = os.environ.get(
        MAINTENANCE_FENCE_OWNER_TOKEN_ENV,
        "",
    ).strip()
    if inherited_owner_token != owner_token:
        raise ReleaseRolloutError(
            "maintenance fence owner differs from operation lock"
        )
    owner_token_sha256 = hashlib.sha256(
        owner_token.encode("ascii")
    ).hexdigest()
    cur.execute(
        """
        SELECT fence_id::text AS fence_id,
               operation,
               actor,
               owner_token_sha256,
               status,
               lease_version,
               expires_at,
               last_stage,
               expires_at > clock_timestamp() AS lease_is_fresh
        FROM control_plane_maintenance_fences
        WHERE fence_id=%s
          AND domain=%s
        FOR UPDATE
        """,
        (str(fence_id), REDIS_FENCING_DOMAIN),
    )
    row = cur.fetchone()
    if row is None:
        raise ReleaseRolloutError(
            "maintenance fence is unavailable"
        )
    if row["operation"] != MAINTENANCE_FENCE_OPERATION:
        raise ReleaseRolloutError(
            "maintenance fence operation mismatch"
        )
    if (
        row["status"] != "active"
        or row["lease_is_fresh"] is not True
    ):
        raise ReleaseRolloutError(
            "maintenance fence is inactive or expired"
        )
    if row["owner_token_sha256"] != owner_token_sha256:
        raise ReleaseRolloutError(
            "maintenance fence owner token mismatch"
        )
    operation_lock.require_held()
    return {
        "fence_id": str(row["fence_id"]),
        "operation": str(row["operation"]),
        "actor": str(row["actor"]),
        "lease_version": int(row["lease_version"]),
        "expires_at": row["expires_at"].isoformat(),
        "last_stage": str(row["last_stage"]),
        "rollout_stage": stage,
    }


def _active_redis_fencing_epoch(
    cur,
    *,
    for_update: bool,
) -> str:
    lock_clause = "FOR SHARE"
    if for_update:
        lock_clause = "FOR UPDATE"
    cur.execute(
        """
        SELECT redis_fencing_epoch::text AS redis_fencing_epoch
        FROM redis_fencing_epochs
        WHERE domain=%s
          AND status='active'
        """
        + lock_clause,
        (REDIS_FENCING_DOMAIN,),
    )
    row = cur.fetchone()
    if row is None:
        raise ReleaseRolloutError(
            "active Redis fencing epoch is missing"
        )
    return str(row["redis_fencing_epoch"])


def _lock_registration_heartbeats(
    cur,
    *,
    document: ReleaseDocument,
    active_redis_fencing_epoch: str,
    max_age_seconds: float,
) -> list[dict[str, Any]]:
    del document
    rows_by_account = _fresh_heartbeat_rows(
        cur,
        accounts=ACCOUNTS,
        max_age_seconds=max_age_seconds,
        for_update=True,
    )
    evidence = []
    for account_id in ACCOUNTS:
        row = _single_expected_heartbeat(
            rows_by_account,
            account_id,
            context="registration",
        )
        _require_halted_writer(row, account_id)
        if str(row["redis_fencing_epoch"]) != (
            active_redis_fencing_epoch
        ):
            raise ReleaseRolloutError(
                f"{account_id} registration heartbeat Redis fencing "
                "epoch mismatch"
            )
        evidence.append(_heartbeat_evidence(row))
    return evidence


def _activate_redis_fencing_epoch(
    cur,
    evidence: RedisFencingEpochEvidence,
    *,
    activated_by: str,
) -> None:
    cur.execute(
        """
        SELECT redis_fencing_epoch::text AS redis_fencing_epoch,
               domain,
               status,
               marker_sha256,
               capacity_evidence_sha256,
               initial_redis_run_id,
               active_volume
        FROM redis_fencing_epochs
        WHERE domain=%s
          AND status='active'
        FOR UPDATE
        """,
        (REDIS_FENCING_DOMAIN,),
    )
    active = cur.fetchone()
    if (
        active is not None
        and active["redis_fencing_epoch"] == evidence.redis_fencing_epoch
    ):
        _require_epoch_record_matches_evidence(active, evidence)
        return

    if active is not None:
        cur.execute(
            """
            UPDATE redis_fencing_epochs
            SET status='retired',
                retired_at=now()
            WHERE redis_fencing_epoch=%s
              AND status='active'
            """,
            (active["redis_fencing_epoch"],),
        )

    cur.execute(
        """
        SELECT redis_fencing_epoch::text AS redis_fencing_epoch,
               domain,
               status,
               marker_sha256,
               capacity_evidence_sha256,
               initial_redis_run_id,
               active_volume
        FROM redis_fencing_epochs
        WHERE redis_fencing_epoch=%s
        FOR UPDATE
        """,
        (evidence.redis_fencing_epoch,),
    )
    existing = cur.fetchone()
    if existing is not None:
        _require_epoch_record_matches_evidence(existing, evidence)
        raise ReleaseRolloutError(
            "retired Redis fencing epoch cannot be reactivated"
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
        VALUES (%s, %s, 'active', %s, %s, %s, %s, %s, now())
        """,
        (
            evidence.redis_fencing_epoch,
            REDIS_FENCING_DOMAIN,
            evidence.marker_sha256,
            evidence.capacity_evidence_sha256,
            evidence.initial_redis_run_id,
            evidence.active_volume,
            activated_by,
        ),
    )


def _require_epoch_record_matches_evidence(
    row: dict[str, Any],
    evidence: RedisFencingEpochEvidence,
) -> None:
    stored = (
        str(row["redis_fencing_epoch"]),
        str(row["domain"]),
        str(row["marker_sha256"]),
        str(row["capacity_evidence_sha256"]),
        str(row["initial_redis_run_id"]),
        str(row["active_volume"]),
    )
    expected = (
        evidence.redis_fencing_epoch,
        REDIS_FENCING_DOMAIN,
        evidence.marker_sha256,
        evidence.capacity_evidence_sha256,
        evidence.initial_redis_run_id,
        evidence.active_volume,
    )
    if stored != expected:
        raise ReleaseRolloutError(
            "Redis fencing epoch evidence conflicts with audit history"
        )


def _require_active_epoch_matches_rollout(
    cur,
    rollout: dict[str, Any],
) -> None:
    cur.execute(
        """
        SELECT redis_fencing_epoch::text AS redis_fencing_epoch
        FROM redis_fencing_epochs
        WHERE domain=%s
          AND status='active'
        FOR SHARE
        """,
        (REDIS_FENCING_DOMAIN,),
    )
    active = cur.fetchone()
    if active is None:
        raise ReleaseRolloutError("active Redis fencing epoch is missing")
    if str(active["redis_fencing_epoch"]) != str(
        rollout["redis_fencing_epoch"]
    ):
        raise ReleaseRolloutError(
            "rollout Redis fencing epoch is not active"
        )


def _register_account_manifests(
    cur,
    document: ReleaseDocument,
    *,
    reviewed_by: str,
) -> None:
    for account_id in ACCOUNTS:
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
                reviewed_by,
                reviewed_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, 'reviewed', %s, now()
            )
            ON CONFLICT (account_id, release_id) DO NOTHING
            """,
            (
                account_id,
                document.release_id,
                document.image_digest,
                document.config_sha256,
                document.dependency_lock_sha256,
                document.schema_epoch,
                reviewed_by,
            ),
        )


def _require_registered_manifests(
    cur,
    document: ReleaseDocument,
) -> None:
    cur.execute(
        """
        SELECT account_id,
               release_id,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               review_status
        FROM reviewed_release_manifests
        WHERE release_id=%s
          AND account_id = ANY(%s)
        ORDER BY account_id
        """,
        (document.release_id, list(ACCOUNTS)),
    )
    rows = cur.fetchall()
    if [row["account_id"] for row in rows] != list(ACCOUNTS):
        raise ReleaseRolloutError(
            "reviewed release registration must include account-a through account-d"
        )
    expected_identity = document.identity
    for row in rows:
        identity = (
            row["release_id"],
            row["image_digest"],
            row["config_sha256"],
            row["dependency_lock_sha256"],
            row["schema_epoch"],
        )
        if identity != expected_identity or row["review_status"] != "reviewed":
            raise ReleaseRolloutError(
                f"{row['account_id']} reviewed release manifest conflicts"
            )


def _require_matching_heartbeats(
    cur,
    rollout: dict[str, Any],
    *,
    accounts: tuple[str, ...],
    max_age_seconds: float,
) -> list[dict[str, Any]]:
    rows_by_account = _fresh_heartbeat_rows(
        cur,
        accounts=accounts,
        max_age_seconds=max_age_seconds,
        for_update=False,
    )

    expected_identity = (
        rollout["release_id"],
        rollout["image_digest"],
        rollout["config_sha256"],
        rollout["dependency_lock_sha256"],
        rollout["schema_epoch"],
        str(rollout["redis_fencing_epoch"]),
    )
    evidence = []
    for account_id in accounts:
        row = _single_expected_heartbeat(
            rows_by_account,
            account_id,
            context="rollout",
        )
        identity = (
            row["release_id"],
            row["image_digest"],
            row["config_sha256"],
            row["dependency_lock_sha256"],
            row["schema_epoch"],
            str(row["redis_fencing_epoch"]),
        )
        if identity != expected_identity:
            if str(row["redis_fencing_epoch"]) != str(
                rollout["redis_fencing_epoch"]
            ):
                raise ReleaseRolloutError(
                    f"{account_id} heartbeat Redis fencing epoch mismatch"
                )
            raise ReleaseRolloutError(
                f"{account_id} heartbeat release identity mismatch"
            )
        _require_halted_writer(row, account_id)
        evidence.append(_heartbeat_evidence(row))
    return evidence


def _require_account_rollout_readiness(
    cur,
    rollout: dict[str, Any],
    *,
    upgraded_accounts: tuple[str, ...],
    next_account: str,
    max_age_seconds: float,
) -> list[dict[str, Any]]:
    accounts = (*upgraded_accounts, next_account)
    context = f"{next_account} rollout"
    rows_by_account = _fresh_heartbeat_rows(
        cur,
        accounts=accounts,
        max_age_seconds=max_age_seconds,
        for_update=True,
    )
    expected_new_identity = (
        rollout["release_id"],
        rollout["image_digest"],
        rollout["config_sha256"],
        rollout["dependency_lock_sha256"],
        rollout["schema_epoch"],
        str(rollout["redis_fencing_epoch"]),
    )
    evidence = []
    for account_id in upgraded_accounts:
        heartbeat = _single_expected_heartbeat(
            rows_by_account,
            account_id,
            context=context,
        )
        _require_halted_writer(heartbeat, account_id)
        if str(heartbeat["redis_fencing_epoch"]) != str(
            rollout["redis_fencing_epoch"]
        ):
            raise ReleaseRolloutError(
                f"{account_id} heartbeat Redis fencing epoch mismatch"
            )
        if _heartbeat_release_identity(heartbeat) != expected_new_identity:
            raise ReleaseRolloutError(
                f"{account_id} must run the new reviewed release before "
                f"{next_account} rollout"
            )
        evidence.append(_heartbeat_evidence(heartbeat))
    next_heartbeat = _single_expected_heartbeat(
        rows_by_account,
        next_account,
        context=context,
    )
    _require_halted_writer(next_heartbeat, next_account)
    if str(next_heartbeat["redis_fencing_epoch"]) != str(
        rollout["redis_fencing_epoch"]
    ):
        raise ReleaseRolloutError(
            f"{next_account} heartbeat Redis fencing epoch mismatch"
        )
    if _has_bootstrap_all_halted_registration(cur, rollout):
        if _heartbeat_release_identity(
            next_heartbeat
        ) != expected_new_identity:
            raise ReleaseRolloutError(
                f"{next_account} must run the bootstrap reviewed release"
            )
        evidence.append(_heartbeat_evidence(next_heartbeat))
        return evidence
    old_release_id = str(
        next_heartbeat["release_id"] or ""
    ).strip()
    if not old_release_id or old_release_id == rollout["release_id"]:
        raise ReleaseRolloutError(
            f"{next_account} must remain on an approved old release"
        )
    cur.execute(
        """
        SELECT release_id,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               review_status
        FROM reviewed_release_manifests
        WHERE account_id=%s
          AND release_id=%s
        FOR SHARE
        """,
        (next_account, old_release_id),
    )
    approved_old = cur.fetchone()
    if approved_old is None or approved_old["review_status"] != "reviewed":
        raise ReleaseRolloutError(
            f"{next_account} old release is not approved"
        )
    approved_identity = (
        approved_old["release_id"],
        approved_old["image_digest"],
        approved_old["config_sha256"],
        approved_old["dependency_lock_sha256"],
        approved_old["schema_epoch"],
    )
    heartbeat_old_identity = _heartbeat_release_identity(
        next_heartbeat,
        include_redis_epoch=False,
    )
    if heartbeat_old_identity != approved_identity:
        raise ReleaseRolloutError(
            f"{next_account} heartbeat differs from its approved old release"
        )
    evidence.append(_heartbeat_evidence(next_heartbeat))
    return evidence


def _fresh_heartbeat_rows(
    cur,
    *,
    accounts: tuple[str, ...],
    max_age_seconds: float,
    for_update: bool,
) -> dict[str, list[dict[str, Any]]]:
    lock_clause = "FOR SHARE"
    if for_update:
        lock_clause = "FOR UPDATE"
    cur.execute(
        """
        SELECT node_id,
               account_id,
               status,
               release_id,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               redis_fencing_epoch::text AS redis_fencing_epoch,
               runtime_generation,
               lease_fencing_token,
               heartbeat_sequence,
               last_seen_at
        FROM node_heartbeats
        WHERE account_id = ANY(%s)
          AND last_seen_at >= (
              now() - make_interval(secs => %s)
          )
        ORDER BY account_id, node_id
        """
        + lock_clause,
        (list(accounts), max_age_seconds),
    )
    rows_by_account: dict[str, list[dict[str, Any]]] = {
        account_id: []
        for account_id in accounts
    }
    for row in cur.fetchall():
        account_id = str(row["account_id"])
        rows_by_account.setdefault(account_id, []).append(row)
    return rows_by_account


def _single_expected_heartbeat(
    rows_by_account: dict[str, list[dict[str, Any]]],
    account_id: str,
    *,
    context: str,
) -> dict[str, Any]:
    rows = rows_by_account.get(account_id, [])
    if len(rows) != 1:
        raise ReleaseRolloutError(
            f"{account_id} requires exactly one fresh {context} heartbeat"
        )
    row = rows[0]
    expected_node_id = EXPECTED_NODE_IDS[account_id]
    if row["node_id"] != expected_node_id:
        raise ReleaseRolloutError(
            f"{account_id} heartbeat node identity mismatch"
        )
    return row


def _require_halted_writer(
    row: dict[str, Any],
    account_id: str,
) -> None:
    if str(row["status"] or "").upper() != "HALTED":
        raise ReleaseRolloutError(
            f"{account_id} must remain HALTED during rollout"
        )
    runtime_generation = str(
        row["runtime_generation"] or ""
    ).strip()
    lease_fencing_token = row["lease_fencing_token"]
    heartbeat_sequence = row["heartbeat_sequence"]
    if (
        not runtime_generation
        or not isinstance(lease_fencing_token, int)
        or lease_fencing_token <= 0
        or not isinstance(heartbeat_sequence, int)
        or heartbeat_sequence <= 0
    ):
        raise ReleaseRolloutError(
            f"{account_id} heartbeat writer identity is invalid"
        )


def _heartbeat_release_identity(
    row: dict[str, Any],
    *,
    include_redis_epoch: bool = True,
) -> tuple[str, ...]:
    identity = (
        str(row["release_id"]),
        str(row["image_digest"]),
        str(row["config_sha256"]),
        str(row["dependency_lock_sha256"]),
        str(row["schema_epoch"]),
    )
    if include_redis_epoch:
        return (*identity, str(row["redis_fencing_epoch"]))
    return identity


def _heartbeat_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": str(row["account_id"]),
        "node_id": str(row["node_id"]),
        "status": str(row["status"]).upper(),
        "release_id": str(row["release_id"]),
        "image_digest": str(row["image_digest"]),
        "config_sha256": str(row["config_sha256"]),
        "dependency_lock_sha256": str(
            row["dependency_lock_sha256"]
        ),
        "schema_epoch": str(row["schema_epoch"]),
        "redis_fencing_epoch": str(row["redis_fencing_epoch"]),
        "runtime_generation": str(row["runtime_generation"]),
        "lease_fencing_token": int(row["lease_fencing_token"]),
        "heartbeat_sequence": int(row["heartbeat_sequence"]),
        "last_seen_at": row["last_seen_at"].isoformat(),
    }


def _record_rollout_event(
    cur,
    *,
    release_id: str,
    event_type: str,
    from_phase: str | None,
    to_phase: str,
    phase_version: int,
    idempotency_key: str,
    actor: str,
    reason: str,
    evidence: dict[str, Any],
) -> None:
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
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid4()),
            release_id,
            event_type,
            from_phase,
            to_phase,
            phase_version,
            idempotency_key,
            actor,
            reason,
            Json(evidence),
        ),
    )


def _record_global_audit(
    cur,
    *,
    release_id: str,
    event_type: str,
    actor: str,
    payload: dict[str, Any],
) -> None:
    cur.execute(
        """
        INSERT INTO audit_events (
            audit_event_id,
            event_type,
            aggregate_type,
            aggregate_id,
            actor,
            payload
        )
        VALUES (%s, %s, 'reviewed_release_rollout', %s, %s, %s)
        """,
        (
            str(uuid4()),
            event_type,
            release_id,
            actor,
            Json(payload),
        ),
    )


def _rollout_by_release_id(
    cur,
    release_id: str,
    *,
    for_update: bool = False,
):
    lock_clause = ""
    if for_update:
        lock_clause = " FOR UPDATE"
    cur.execute(
        """
        SELECT release_id,
               redis_fencing_epoch::text AS redis_fencing_epoch,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               manifest_sha256,
               bundle_manifest_sha256,
               registration_idempotency_key,
               phase,
               phase_version,
               reviewed_by,
               reviewed_at,
               created_at,
               updated_at
        FROM reviewed_release_rollouts
        WHERE release_id=%s
        """
        + lock_clause,
        (release_id,),
    )
    return cur.fetchone()


def _rollout_by_registration_key(cur, idempotency_key: str):
    cur.execute(
        """
        SELECT release_id,
               redis_fencing_epoch::text AS redis_fencing_epoch,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               manifest_sha256,
               bundle_manifest_sha256,
               registration_idempotency_key,
               phase,
               phase_version,
               reviewed_by,
               reviewed_at,
               created_at,
               updated_at
        FROM reviewed_release_rollouts
        WHERE registration_idempotency_key=%s
        FOR UPDATE
        """,
        (idempotency_key,),
    )
    return cur.fetchone()


def _event_by_idempotency_key(cur, idempotency_key: str):
    cur.execute(
        """
        SELECT release_id,
               to_phase,
               actor,
               reason,
               evidence
        FROM reviewed_release_rollout_events
        WHERE idempotency_key=%s
        """,
        (idempotency_key,),
    )
    return cur.fetchone()


def _require_rollout_matches_document(
    rollout: dict[str, Any],
    document: ReleaseDocument,
    capacity_evidence: RedisFencingEpochEvidence,
    registration_idempotency_key: str,
    reviewed_by: str,
) -> None:
    stored_identity = (
        rollout["release_id"],
        rollout["image_digest"],
        rollout["config_sha256"],
        rollout["dependency_lock_sha256"],
        rollout["schema_epoch"],
    )
    if stored_identity != document.identity:
        raise ReleaseRolloutError(
            "reviewed release rollout identity conflicts"
        )
    if str(rollout["redis_fencing_epoch"]) != (
        capacity_evidence.redis_fencing_epoch
    ):
        raise ReleaseRolloutError(
            "reviewed release rollout Redis fencing epoch conflicts"
        )
    if (
        rollout["manifest_sha256"] != document.manifest_sha256
        or rollout["bundle_manifest_sha256"]
        != document.bundle_manifest_sha256
    ):
        raise ReleaseRolloutError(
            "reviewed release rollout document hashes conflict"
        )
    if (
        rollout["registration_idempotency_key"]
        != registration_idempotency_key
    ):
        raise ReleaseRolloutError(
            "release was registered with another idempotency key"
        )
    if rollout["reviewed_by"] != reviewed_by:
        raise ReleaseRolloutError(
            "registration idempotency key reviewer mismatch"
        )


def _rollout_payload(
    rollout: dict[str, Any],
    *,
    idempotent: bool,
) -> dict[str, Any]:
    payload = _jsonable_row(rollout)
    payload["accounts"] = list(ACCOUNTS)
    payload["idempotent"] = idempotent
    return payload


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = {}
    for key, value in dict(row).items():
        if isinstance(value, UUID):
            payload[key] = str(value)
        elif hasattr(value, "isoformat"):
            payload[key] = value.isoformat()
        else:
            payload[key] = value
    return payload


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseRolloutError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise ReleaseRolloutError(f"JSON root must be an object: {path}")
    return value


def _required_text(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ReleaseRolloutError(f"{field_name} is required")
    if len(normalized) > 500:
        raise ReleaseRolloutError(f"{field_name} is too long")
    return normalized


def _required_sha256(value: Any, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
        raise ReleaseRolloutError(f"{field_name} must be a SHA256 digest")
    return normalized


def _required_redis_run_id(value: Any, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if re.fullmatch(r"[0-9a-f]{40}", normalized) is None:
        raise ReleaseRolloutError(f"{field_name} must be a Redis run_id")
    return normalized


def _required_phase(value: str) -> str:
    normalized = _required_text(value, "to_phase")
    if normalized not in PHASES:
        raise ReleaseRolloutError(
            f"to_phase must be one of: {', '.join(PHASES)}"
        )
    return normalized


def _heartbeat_max_age(value: float) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ReleaseRolloutError(
            "heartbeat_max_age_seconds is invalid"
        ) from exc
    if normalized <= 0 or normalized > 60:
        raise ReleaseRolloutError(
            "heartbeat_max_age_seconds must be within (0, 60]"
        )
    return normalized


def _database_url(argument: str | None) -> str:
    database_url = str(argument or os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise ReleaseRolloutError(
            "--database-url or DATABASE_URL is required"
        )
    return database_url


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Register and advance the audited account-a through account-d "
            "reviewed release rollout."
        )
    )
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL; defaults to DATABASE_URL",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_parser = subparsers.add_parser(
        "register",
        help="validate release material and atomically register both accounts",
    )
    register_parser.add_argument("--manifest", type=Path, required=True)
    register_parser.add_argument(
        "--bundle-manifest",
        type=Path,
        required=True,
    )
    register_parser.add_argument(
        "--capacity-evidence",
        type=Path,
        required=True,
    )
    register_parser.add_argument("--reviewed-by", required=True)
    register_parser.add_argument("--idempotency-key", required=True)

    bootstrap_register_parser = subparsers.add_parser(
        "bootstrap-register",
        help=(
            "one-time first Redis epoch and reviewed rollout registration"
        ),
    )
    bootstrap_register_parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
    )
    bootstrap_register_parser.add_argument(
        "--bundle-manifest",
        type=Path,
        required=True,
    )
    bootstrap_register_parser.add_argument(
        "--capacity-evidence",
        type=Path,
        required=True,
    )
    bootstrap_register_parser.add_argument("--reviewed-by", required=True)
    bootstrap_register_parser.add_argument(
        "--idempotency-key",
        required=True,
    )

    migration_rebaseline_parser = subparsers.add_parser(
        "migration-rebaseline-register",
        help=(
            "atomically supersede migrated rollout history and register a "
            "fresh-host release"
        ),
    )
    migration_rebaseline_parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
    )
    migration_rebaseline_parser.add_argument(
        "--bundle-manifest",
        type=Path,
        required=True,
    )
    migration_rebaseline_parser.add_argument(
        "--capacity-evidence",
        type=Path,
        required=True,
    )
    migration_rebaseline_parser.add_argument(
        "--reviewed-by",
        required=True,
    )
    migration_rebaseline_parser.add_argument(
        "--idempotency-key",
        required=True,
    )

    sign_gate_parser = subparsers.add_parser(
        "sign-release-gate",
        help=(
            "derive and sign an executor release gate from immutable "
            "release material"
        ),
    )
    sign_gate_parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
    )
    sign_gate_parser.add_argument(
        "--bundle-manifest",
        type=Path,
        required=True,
    )
    sign_gate_parser.add_argument(
        "--account-id",
        choices=ACCOUNTS,
        required=True,
    )
    sign_gate_parser.add_argument(
        "--signing-private-key",
        type=Path,
        required=True,
    )
    sign_gate_parser.add_argument(
        "--output-directory",
        type=Path,
        required=True,
    )

    advance_parser = subparsers.add_parser(
        "advance",
        help="advance or abort a registered rollout",
    )
    advance_parser.add_argument("--release-id", required=True)
    advance_parser.add_argument("--to-phase", choices=PHASES, required=True)
    advance_parser.add_argument("--actor", required=True)
    advance_parser.add_argument("--reason", required=True)
    advance_parser.add_argument("--idempotency-key", required=True)
    advance_parser.add_argument(
        "--closure-report",
        type=Path,
        help="signed live canary closure report for the prior account",
    )
    advance_parser.add_argument(
        "--closure-signature",
        type=Path,
        help="detached SHA-256 signature for --closure-report",
    )
    advance_parser.add_argument(
        "--closure-public-key",
        type=Path,
        help="pinned reviewer public key for closure verification",
    )
    advance_parser.add_argument(
        "--heartbeat-max-age-seconds",
        type=float,
        default=DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    )

    finalize_parser = subparsers.add_parser(
        "finalize",
        help=(
            "advance account_d_rollout to fleet_complete using the signed "
            "account-d closure report"
        ),
    )
    finalize_parser.add_argument("--release-id", required=True)
    finalize_parser.add_argument("--actor", required=True)
    finalize_parser.add_argument("--reason", required=True)
    finalize_parser.add_argument("--idempotency-key", required=True)
    finalize_parser.add_argument(
        "--closure-report",
        type=Path,
        required=True,
        help="signed account-d live canary closure report",
    )
    finalize_parser.add_argument(
        "--closure-signature",
        type=Path,
        required=True,
        help="detached SHA-256 signature for --closure-report",
    )
    finalize_parser.add_argument(
        "--closure-public-key",
        type=Path,
        required=True,
        help="pinned reviewer public key for closure verification",
    )
    finalize_parser.add_argument(
        "--heartbeat-max-age-seconds",
        type=float,
        default=DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    )

    status_parser = subparsers.add_parser(
        "status",
        help="show rollout state and ordered audit events",
    )
    status_parser.add_argument("--release-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "sign-release-gate":
            result = generate_signed_release_gate(
                args.manifest,
                args.bundle_manifest,
                account_id=args.account_id,
                signing_private_key_path=args.signing_private_key,
                output_directory=args.output_directory,
            )
        else:
            mutation = args.command in {
                "register",
                "bootstrap-register",
                "migration-rebaseline-register",
                "advance",
                "finalize",
            }
            with AccountStallOperationLock(
                enabled=mutation
            ) as operation_lock:
                database_url = _database_url(args.database_url)
                conn = psycopg2.connect(database_url)
                try:
                    if args.command == "register":
                        document = load_release_document(
                            args.manifest,
                            args.bundle_manifest,
                        )
                        capacity_evidence = load_capacity_evidence(
                            args.capacity_evidence
                        )
                        result = register_reviewed_release(
                            conn,
                            document,
                            capacity_evidence,
                            reviewed_by=args.reviewed_by,
                            idempotency_key=args.idempotency_key,
                            operation_lock=operation_lock,
                        )
                    elif args.command == "bootstrap-register":
                        document = load_release_document(
                            args.manifest,
                            args.bundle_manifest,
                        )
                        capacity_evidence = load_capacity_evidence(
                            args.capacity_evidence
                        )
                        result = bootstrap_register_reviewed_release(
                            conn,
                            document,
                            capacity_evidence,
                            reviewed_by=args.reviewed_by,
                            idempotency_key=args.idempotency_key,
                            operation_lock=operation_lock,
                        )
                    elif args.command == "migration-rebaseline-register":
                        document = load_release_document(
                            args.manifest,
                            args.bundle_manifest,
                        )
                        capacity_evidence = load_capacity_evidence(
                            args.capacity_evidence
                        )
                        result = (
                            migration_rebaseline_register_reviewed_release(
                                conn,
                                document,
                                capacity_evidence,
                                reviewed_by=args.reviewed_by,
                                idempotency_key=args.idempotency_key,
                                operation_lock=operation_lock,
                            )
                        )
                    elif args.command == "advance":
                        result = advance_rollout(
                            conn,
                            args.release_id,
                            to_phase=args.to_phase,
                            actor=args.actor,
                            reason=args.reason,
                            idempotency_key=args.idempotency_key,
                            heartbeat_max_age_seconds=(
                                args.heartbeat_max_age_seconds
                            ),
                            closure_report_path=args.closure_report,
                            closure_signature_path=(
                                args.closure_signature
                            ),
                            closure_public_key_path=(
                                args.closure_public_key
                            ),
                            operation_lock=operation_lock,
                        )
                    elif args.command == "finalize":
                        result = finalize_fleet_rollout(
                            conn,
                            args.release_id,
                            actor=args.actor,
                            reason=args.reason,
                            idempotency_key=args.idempotency_key,
                            heartbeat_max_age_seconds=(
                                args.heartbeat_max_age_seconds
                            ),
                            closure_report_path=args.closure_report,
                            closure_signature_path=(
                                args.closure_signature
                            ),
                            closure_public_key_path=(
                                args.closure_public_key
                            ),
                            operation_lock=operation_lock,
                        )
                    else:
                        result = get_rollout(conn, args.release_id)
                finally:
                    conn.close()
    except (
        ReleaseRolloutError,
        release_manifest.ReleaseManifestError,
        psycopg2.Error,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
