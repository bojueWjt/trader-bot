#!/usr/bin/env python3
"""Fail-closed account-a SOLUSDT canary round-trip executor."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid5

ACCOUNT_ID = "account-a"
SYMBOL = "SOLUSDT"
LIVE_CANARY_QUANTITY = Decimal("0.07")
MAX_ACTUAL_OPEN_NOTIONAL_USDT = Decimal(12)
ABSOLUTE_LOSS_CAP_USDT = Decimal("1.5")
MAX_CLOCK_SKEW = timedelta(seconds=60)
DEFAULT_HEALTH_MAX_AGE_SECONDS = Decimal(5)
MAX_HEALTH_MAX_AGE_SECONDS = Decimal(30)
DEFAULT_EXCHANGE_MAX_AGE_SECONDS = Decimal(120)
MAX_EXCHANGE_MAX_AGE_SECONDS = Decimal(180)
EXCHANGE_EVIDENCE_SOURCE = "exchange"
NODE_STATE_EVIDENCE_SOURCE = "node"
DEFAULT_OPERATION_LOCK_PATH = Path(
    "/var/lock/trader-v3-account-stall-operation.lock"
)
DEFAULT_LIVE_PERMIT_LEDGER_PATH = Path(
    "/var/lib/trader-v3/account-a-live-permit-ledger.json"
)
LIVE_PERMIT_STORE_ID = "trader-v3-account-a-live-permit-store/v1"
PINNED_REVIEWER_PUBLIC_KEY_SHA256 = (
    "010261af290c41cd19d25c233fbb18e0"
    "cbcb23ee36db52eea0053d30e5837ede"
)
PINNED_OPENSSL_PATH = Path("/usr/bin/openssl")

RELEASE_GATE_SCHEMA = "trader-v3-account-a-live-release-gate/v1"
SAFETY_GATE_SCHEMA = "trader-v3-account-a-live-safety-gate/v1"
EMERGENCY_CLOSE_GATE_SCHEMA = (
    "trader-v3-account-a-live-emergency-close-gate/v1"
)
PERMIT_SCHEMA = "trader-v3-account-a-live-permit/v1"
EVIDENCE_RECOVERY_GATE_SCHEMA = (
    "trader-v3-account-a-evidence-recovery-gate/v1"
)
EVIDENCE_RECOVERY_CAPABILITY = (
    "final-snapshot-and-publish-evidence/v1"
)
EVIDENCE_RECOVERY_ALLOWED_ACTIONS = (
    "final-snapshot",
    "publish-evidence",
)
EVIDENCE_SCHEMA = "trader-v3-live-trade-report/v1"
LEDGER_SCHEMA = "trader-v3-account-a-live-permit-ledger/v1"
JOURNAL_STATES = (
    "AUTHORIZED",
    "RESUMED",
    "OPEN_SUBMITTED",
    "OPEN_OBSERVED",
    "CLOSE_PENDING",
    "CLOSE_CONFIRMED",
    "HALTED",
    "RISK_RECOVERY_REQUIRED",
    "EVIDENCE_ENRICHMENT_PENDING",
    "EVIDENCE_COMMITTED",
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CLIENT_ORDER_ID_RE = re.compile(r"^B[0-9a-f]{32}[0-9]{2}$")
TERMINAL_OPEN_STATUSES = {
    "FILLED",
    "EXPIRED",
    "CANCELED",
    "REJECTED",
}
TERMINAL_NO_FILL_OPEN_STATUSES = {
    "EXPIRED",
    "CANCELED",
    "REJECTED",
}
RESULT_PASSED = "PASSED"
RESULT_DEGRADED = "DEGRADED"
RESULT_BLOCKED = "BLOCKED"
RESULT_STATUSES = {
    RESULT_PASSED,
    RESULT_DEGRADED,
    RESULT_BLOCKED,
}
SIGNED_HEALTH_FIELDS = (
    "actor_tick_at",
    "heartbeat_at",
    "projection_at",
    "reconciliation_at",
    "loss_monitor_at",
)
SOFT_ADAPTER_ERROR_RE = re.compile(
    r"(?:"
    r"(?<!\d)(?:408|425|429|5\d\d)(?!\d)|"
    r"timeout|timed out|"
    r"circuit(?:[-_ ]?open)?|"
    r"queue(?:[-_ ]?(?:pressure|full))?|"
    r"memory(?:[-_ ]?queue)?[-_ ]?pressure|"
    r"resource(?:[-_ ]?(?:pressure|exhausted|busy))|"
    r"reconciliation(?:[-_ ]?(?:pressure|backlog|busy))|"
    r"exchange(?:[-_ ]?(?:mirror|state))?[-_ ]?"
    r"(?:lag|stale|pending)|"
    r"rate[-_ ]?limit|too many requests|"
    r"readiness|heartbeat|projection|telemetry|unavailable|"
    r"capacity[-_ ]?(?:pressure|exhausted|full)|"
    r"service unavailable|temporar|overload"
    r")",
    re.IGNORECASE,
)
HARD_ADAPTER_ERROR_RE = re.compile(
    r"(?:"
    r"\bownership(?:[-_ ]identity)?[-_ ]?"
    r"(?:mismatch|conflict)\b|"
    r"\bfenc(?:e|ing)(?:[-_ ](?:token|epoch|lease))?[-_ ]?"
    r"(?:mismatch|conflict|lost|rejected|violation)\b|"
    r"\bfencing[-_ ]lease[-_ ]lost\b|"
    r"\bidentity[-_ ]?(?:mismatch|conflict)\b|"
    r"\bwriter(?:[-_ ]identity)?[-_ ]?"
    r"(?:mismatch|conflict)\b|"
    r"\blease[-_ ]?(?:mismatch|conflict)\b|"
    r"\bidentity[-_ ]?(?:is[-_ ]?)?"
    r"(?:incomplete|invalid)\b|"
    r"\bfenc(?:e|ing)[-_ ]epoch[-_ ]?"
    r"(?:is[-_ ]?)?invalid\b|"
    r"\bnode[-_ ]auth[-_ ]tokens[-_ ]must[-_ ]be[-_ ]unique\b|"
    r"\bfsync\b|"
    r"\bdurab(?:le|ility)[-_ ]?"
    r"(?:write|append|commit|failure|error)\b|"
    r"\bdurab(?:le|ility)[-_ ]?operation[-_ ]?deadline[-_ ]?"
    r"(?:exceeded|expired|failed|timeout)\b|"
    r"\bENOSPC\b|"
    r"\bno[-_ ]space\b|"
    r"\b(?:disk|file[-_ ]?system)[-_ ]?"
    r"(?:is[-_ ]?)?full\b|"
    r"\batomic[-_ ]replace[-_ ]?"
    r"(?:failed|failure|error)\b|"
    r"\b(?:journal|outbox|store|disk)[-_ ]?"
    r"capacity[-_ ]?(?:exhausted|full)\b"
    r")",
    re.IGNORECASE,
)


class LiveTradeExecutionError(RuntimeError):
    pass


class DuplicatePermitError(LiveTradeExecutionError):
    pass


class RecoverableEvidenceError(LiveTradeExecutionError):
    pass


class OperationLockConflict(LiveTradeExecutionError):
    pass


class TerminationRequested(LiveTradeExecutionError):
    pass


class SoftActionFailure(LiveTradeExecutionError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
    ) -> None:
        super().__init__(message)
        self.code = code


class ClassifiedExecutionError(LiveTradeExecutionError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
    ) -> None:
        super().__init__(message)
        self.code = code


class TradeAdapter(Protocol):
    def preflight(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def resume(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        ...

    def submit_open(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def observe(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        ...

    def cancel_open(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def current_position(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def submit_close(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def final_snapshot(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def halt(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


class SignatureVerifier(Protocol):
    def verify(
        self,
        *,
        public_key: bytes,
        payload: bytes,
        signature: bytes,
    ) -> None:
        ...


@dataclass(frozen=True)
class SignedDocumentPaths:
    payload: Path
    signature: Path | None = None


@dataclass(frozen=True)
class AuthorizationPaths:
    release_gate: SignedDocumentPaths
    safety_gate: SignedDocumentPaths
    emergency_close_gate: SignedDocumentPaths
    permit: SignedDocumentPaths
    reviewer_public_key: Path | None = None


@dataclass(frozen=True)
class ReleaseIdentity:
    release_id: str
    node_id: str
    writer_id: str
    lease_id: str
    fencing_epoch: int
    image_digest: str
    config_sha256: str
    dependency_lock_sha256: str
    live_executor_sha256: str
    live_adapter_sha256: str
    permit_store_id: str
    permit_store_path: str


@dataclass(frozen=True)
class CanaryAuthorization:
    permit_id: str
    account_id: str
    symbol: str
    release: ReleaseIdentity
    intent_id: str
    close_intent_id: str
    open_client_order_id: str
    close_client_order_id: str
    open_side: str
    quantity: Decimal
    limit_price_usdt: Decimal
    max_notional_usdt: Decimal
    fee_reserve_usdt: Decimal
    exchange_filters: Mapping[str, Decimal]
    max_cumulative_net_loss_usdt: Decimal
    portfolio_baseline_sha256: str
    expires_at: datetime
    emergency_close_evidence_type: str
    emergency_close_environment: str
    emergency_close_verified_at: datetime
    emergency_close_quantity: Decimal
    emergency_close_evidence_sha256: str
    actor_tick_at: datetime | None
    heartbeat_at: datetime | None
    projection_at: datetime | None
    reconciliation_at: datetime | None
    loss_monitor_at: datetime | None
    health_max_age_seconds: Decimal
    exchange_max_age_seconds: Decimal
    document_sha256: Mapping[str, str]
    authorization_sha256: str
    signatures_verified: bool
    warnings: tuple[str, ...]

    @property
    def requested_open_notional_usdt(self) -> Decimal:
        return self.quantity * self.limit_price_usdt


@dataclass(frozen=True)
class EvidenceRecoveryGate:
    gate_id: str
    permit_id: str
    authorization_sha256: str
    release_id: str
    intent_id: str
    close_intent_id: str
    open_client_order_id: str
    close_client_order_id: str
    permit_store_id: str
    permit_store_path: str
    evidence_path: str
    recovery_executor_sha256: str
    recovery_adapter_sha256: str
    issued_at: datetime
    refresh_after: datetime
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class TradeObservation:
    open_status: str
    exchange_evidence_state: str
    filled_quantity: Decimal
    average_fill_price_usdt: Decimal
    cumulative_net_loss_usdt: Decimal
    observed_at: datetime
    mark_at: datetime
    loss_monitor_at: datetime
    evidence_sha256: str
    warnings: tuple[str, ...]

    @property
    def actual_open_notional_usdt(self) -> Decimal:
        return self.filled_quantity * self.average_fill_price_usdt


@dataclass(frozen=True)
class PositionSnapshot:
    side: str
    quantity: Decimal
    fetched_at: datetime
    evidence_sha256: str


@dataclass(frozen=True)
class LivePreflightEvidence:
    evidence_sha256: str
    fetched_at: datetime
    warnings: tuple[str, ...]
    observed_portfolio_baseline_sha256: str
    portfolio_drifted: bool


@dataclass(frozen=True)
class CleanupResult:
    safe: bool
    close_submitted: bool
    close_quantity: Decimal
    pre_halt_snapshot: Mapping[str, Any] | None
    final_snapshot: Mapping[str, Any] | None
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    failure_reason: str
    evidence_path: Path
    evidence_sha256: str
    finished_halted: bool
    close_submitted: bool
    close_quantity: Decimal
    warnings: tuple[str, ...]
    degraded_reasons: tuple[str, ...]
    retryable: bool
    error_code: str
    retry_counts: Mapping[str, int]

    @property
    def passed(self) -> bool:
        if self.status == RESULT_BLOCKED:
            return False
        if self.retryable:
            return False
        return True


@dataclass(frozen=True)
class PermitClaimOutcome:
    newly_authorized: bool
    recovery_required: bool
    terminal: bool
    state: str
    pending_action: str
    pending_action_payload: Mapping[str, Any] | None
    pending_evidence: Mapping[str, Any] | None


@dataclass(frozen=True)
class ValidatedLiveAdapter:
    source_path: Path
    sha256: str
    payload: bytes


@dataclass(frozen=True)
class ProtectedOutputPath:
    target: Path
    parent_device: int
    parent_inode: int
    owner_uid: int
    label: str


class EvidenceRecoveryOnlyAdapter:
    def __init__(self, delegate: TradeAdapter) -> None:
        self._delegate = delegate

    def final_snapshot(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._delegate.final_snapshot(request)

    def preflight(
        self,
        _request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise LiveTradeExecutionError(
            "evidence recovery capability forbids preflight"
        )

    def resume(
        self,
        _request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise LiveTradeExecutionError(
            "evidence recovery capability forbids RESUME"
        )

    def submit_open(
        self,
        _request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise LiveTradeExecutionError(
            "evidence recovery capability forbids OPEN"
        )

    def observe(
        self,
        _request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise LiveTradeExecutionError(
            "evidence recovery capability forbids observation"
        )

    def cancel_open(
        self,
        _request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise LiveTradeExecutionError(
            "evidence recovery capability forbids CANCEL_OPEN"
        )

    def current_position(
        self,
        _request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise LiveTradeExecutionError(
            "evidence recovery capability forbids position query"
        )

    def submit_close(
        self,
        _request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise LiveTradeExecutionError(
            "evidence recovery capability forbids CLOSE"
        )

    def halt(
        self,
        _request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise LiveTradeExecutionError(
            "evidence recovery capability forbids HALT"
        )


class LiveOperationLock:
    def __init__(
        self,
        path: Path = DEFAULT_OPERATION_LOCK_PATH,
    ) -> None:
        self._path = _required_absolute_path(
            path,
            "operation lock path",
        )
        self._handle = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self._handle is not None:
            raise LiveTradeExecutionError(
                "operation lock is already held"
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = _open_protected_lock_file(self._path)
        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            handle.close()
            raise OperationLockConflict(
                f"account-stall operation lock is busy: {self._path}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def require_held(self) -> None:
        if self._handle is None:
            raise LiveTradeExecutionError(
                "live operation lock is not held"
            )

    def __enter__(self) -> LiveOperationLock:
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()


class TerminationSignalGuard:
    def __init__(self) -> None:
        self._previous: dict[int, Any] = {}
        self._triggered = False

    def __enter__(self) -> TerminationSignalGuard:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            self._previous[signal_number] = signal.getsignal(
                signal_number
            )
            signal.signal(signal_number, self._handle)
        return self

    def __exit__(self, *_args) -> None:
        for signal_number, handler in self._previous.items():
            signal.signal(signal_number, handler)
        self._previous.clear()

    def _handle(self, signal_number: int, _frame: Any) -> None:
        if self._triggered:
            return
        self._triggered = True
        raise TerminationRequested(
            f"termination signal received: {signal_number}"
        )


class AuditTrail:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._clock = clock
        self._events: list[dict[str, Any]] = []
        self._last_sha256 = "0" * 64

    def record(
        self,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> str:
        event = {
            "sequence": len(self._events) + 1,
            "event_type": _required_text(event_type, "event_type"),
            "occurred_at": _utc_now(self._clock).isoformat(),
            "previous_event_sha256": self._last_sha256,
            "payload": _json_safe(dict(payload)),
        }
        event_sha256 = _sha256_bytes(_canonical_json_bytes(event))
        event["event_sha256"] = event_sha256
        self._events.append(event)
        self._last_sha256 = event_sha256
        return event_sha256

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    @property
    def chain_sha256(self) -> str:
        return self._last_sha256


class OpenSSLSignatureVerifier:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._openssl_path = _validate_system_executable(
            PINNED_OPENSSL_PATH,
            "OpenSSL",
        )
        self._timeout_seconds = _positive_float(
            timeout_seconds,
            "timeout_seconds",
        )

    def verify(
        self,
        *,
        public_key: bytes,
        payload: bytes,
        signature: bytes,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="account-a-live-signature-"
        ) as temporary_root:
            root = Path(temporary_root)
            public_key_path = root / "reviewer.pem"
            payload_path = root / "payload.json"
            signature_path = root / "payload.sig"
            _write_private_file(public_key_path, public_key)
            _write_private_file(payload_path, payload)
            _write_private_file(signature_path, signature)
            command = [
                str(self._openssl_path),
                "dgst",
                "-sha256",
                "-verify",
                str(public_key_path),
                "-signature",
                str(signature_path),
                str(payload_path),
            ]
            try:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                    timeout=self._timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise LiveTradeExecutionError(
                    "signed gate verifier could not run"
                ) from exc
            if result.returncode != 0:
                raise LiveTradeExecutionError(
                    "signed gate verification failed"
                )


class JsonCommandAdapter:
    """Invoke a JSON-line command adapter after live authorization succeeds."""

    def __init__(
        self,
        adapter: ValidatedLiveAdapter,
        *,
        timeout_seconds: float = 15.0,
        live_authorized: bool,
        operation_lock: LiveOperationLock,
    ) -> None:
        if live_authorized is not True:
            raise LiveTradeExecutionError(
                "external adapter requires verified live authorization"
            )
        operation_lock.require_held()
        self._operation_lock = operation_lock
        self._temporary_root = tempfile.TemporaryDirectory(
            prefix="account-a-live-adapter-"
        )
        root = Path(self._temporary_root.name)
        root.chmod(0o700)
        staged_path = root / "reviewed-live-adapter"
        _atomic_write_bytes(
            staged_path,
            adapter.payload,
            mode=0o500,
            replace_existing=False,
        )
        if _sha256_bytes(staged_path.read_bytes()) != adapter.sha256:
            self._temporary_root.cleanup()
            raise LiveTradeExecutionError(
                "staged live adapter hash mismatch"
            )
        self._command_path = staged_path
        self._timeout_seconds = _positive_float(
            timeout_seconds,
            "adapter timeout_seconds",
        )

    def close(self) -> None:
        self._temporary_root.cleanup()

    def __enter__(self) -> JsonCommandAdapter:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def preflight(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._invoke("preflight", request)

    def resume(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._invoke("resume", request)

    def submit_open(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._invoke("open", request)

    def observe(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._invoke("observe", request)

    def cancel_open(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._invoke("cancel-open", request)

    def current_position(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._invoke("position", request)

    def submit_close(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._invoke("close", request)

    def final_snapshot(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._invoke("final-snapshot", request)

    def halt(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._invoke("halt", request)

    def _invoke(
        self,
        action: str,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._operation_lock.require_held()
        command = [str(self._command_path), action]
        input_bytes = _canonical_json_bytes(dict(request)) + b"\n"
        timeout_seconds = self._timeout_seconds
        requested_timeout = request.get("hard_timeout_seconds")
        if requested_timeout is not None:
            action_timeout = _positive_float(
                requested_timeout,
                f"{action} hard_timeout_seconds",
            )
            timeout_seconds = min(timeout_seconds, action_timeout)
        try:
            result = subprocess.run(
                command,
                input=input_bytes,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise SoftActionFailure(
                f"adapter action timed out: {action}",
                code="HTTP_TIMEOUT",
            ) from exc
        except OSError as exc:
            raise LiveTradeExecutionError(
                f"adapter action could not run: {action}"
            ) from exc
        if result.returncode != 0:
            raise LiveTradeExecutionError(
                f"adapter action failed: {action}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LiveTradeExecutionError(
                f"adapter action returned invalid JSON: {action}"
            ) from exc
        if not isinstance(payload, dict):
            raise LiveTradeExecutionError(
                f"adapter action returned a non-object: {action}"
            )
        if payload.get("accepted") is False:
            soft_failure = _soft_adapter_rejection(
                payload,
                action=action,
            )
            if soft_failure is not None:
                raise soft_failure
        return payload


class DryRunAdapter:
    """Deterministic in-process adapter used by the default CLI mode."""

    def __init__(self, authorization: CanaryAuthorization) -> None:
        self._authorization = authorization
        self._position_quantity = Decimal(0)
        self._position_side = "FLAT"
        self._trading_state = "HALTED"
        self._evidence_timestamp = (
            _synthetic_authorization_timestamp(authorization)
        )

    def preflight(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = self._identity_payload()
        payload.update(
            {
                "action": "preflight",
                "source": EXCHANGE_EVIDENCE_SOURCE,
                "exchange_authoritative": True,
                "fetched_at": self._evidence_timestamp.isoformat(),
                "mirror_stale": False,
                "target_position_side": "FLAT",
                "target_position_quantity": "0",
                "target_regular_order_count": 0,
                "target_algo_order_count": 0,
                "available_usdt_balance": "100",
                "non_target_portfolio_baseline_sha256": (
                    self._authorization.portfolio_baseline_sha256
                ),
                "node_snapshot": {
                    "account_id": self._authorization.account_id,
                    "node_id": self._authorization.release.node_id,
                    "writer_id": self._authorization.release.writer_id,
                    "lease_id": self._authorization.release.lease_id,
                    "fencing_epoch": (
                        self._authorization.release.fencing_epoch
                    ),
                    "trading_state": self._trading_state,
                    "process_liveness": True,
                    "actor_tick_at": self._evidence_timestamp.isoformat(),
                    "loss_monitor_healthy": True,
                    "loss_monitor_at": self._evidence_timestamp.isoformat(),
                },
                "evidence_sha256": _synthetic_hash(
                    "dry-run-preflight"
                ),
            }
        )
        return payload

    def resume(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self._trading_state = "ACTIVE"
        return self._ack(
            "resume",
            request,
            source=NODE_STATE_EVIDENCE_SOURCE,
            trading_state=self._trading_state,
            observed_at=self._evidence_timestamp.isoformat(),
        )

    def submit_open(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._position_quantity = self._authorization.quantity
        if self._authorization.open_side == "BUY":
            self._position_side = "LONG"
        else:
            self._position_side = "SHORT"
        return self._ack(
            "open",
            request,
            client_order_id=self._authorization.open_client_order_id,
        )

    def observe(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = self._identity_payload()
        payload.update(
            {
                "open_client_order_id": (
                    self._authorization.open_client_order_id
                ),
                "open_status": "FILLED",
                "filled_quantity": _decimal_text(
                    self._authorization.quantity
                ),
                "average_fill_price_usdt": _decimal_text(
                    self._authorization.limit_price_usdt
                ),
                "cumulative_net_loss_usdt": "0",
                "mark_fresh": True,
                "loss_monitor_healthy": True,
                "observed_at": self._evidence_timestamp.isoformat(),
                "mark_at": self._evidence_timestamp.isoformat(),
                "loss_monitor_at": self._evidence_timestamp.isoformat(),
                "loss_monitor_source": "exchange_mirror+node",
                "exchange_evidence_state": "confirmed_executed",
                "evidence_sha256": _synthetic_hash("dry-run-observe"),
            }
        )
        return payload

    def cancel_open(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._ack(
            "cancel-open",
            request,
            client_order_id=self._authorization.open_client_order_id,
        )

    def current_position(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = self._identity_payload()
        payload.update(
            {
                "position_side": self._position_side,
                "position_quantity": _decimal_text(
                    self._position_quantity
                ),
                "source": EXCHANGE_EVIDENCE_SOURCE,
                "fetched_at": self._evidence_timestamp.isoformat(),
                "evidence_sha256": _synthetic_hash("dry-run-position"),
            }
        )
        return payload

    def submit_close(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        filled_quantity = _decimal(
            request.get("quantity"),
            "close quantity",
            positive=True,
        )
        self._position_quantity = Decimal(0)
        self._position_side = "FLAT"
        return self._ack(
            "close",
            request,
            client_order_id=self._authorization.close_client_order_id,
            filled_quantity=_decimal_text(filled_quantity),
            intent_id=self._authorization.close_intent_id,
        )

    def final_snapshot(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = self._identity_payload()
        payload.update(
            {
                "target_symbol_flat": self._position_quantity == 0,
                "target_symbol_regular_orders_zero": True,
                "target_symbol_algo_orders_zero": True,
                "position_quantity": _decimal_text(
                    self._position_quantity
                ),
                "non_target_portfolio_baseline_sha256": (
                    self._authorization.portfolio_baseline_sha256
                ),
                "open_filled_quantity": _decimal_text(
                    self._authorization.quantity
                ),
                "open_average_fill_price_usdt": _decimal_text(
                    self._authorization.limit_price_usdt
                ),
                "gross_pnl_usdt": "0",
                "fees_usdt": "0",
                "net_pnl_usdt": "0",
                "cumulative_net_loss_usdt": "0",
                "enrichment_degraded": False,
                "financial_proof_complete": True,
                "warnings": [],
                "source": EXCHANGE_EVIDENCE_SOURCE,
                "fetched_at": self._evidence_timestamp.isoformat(),
                "evidence_sha256": _synthetic_hash(
                    "dry-run-final-snapshot"
                ),
            }
        )
        return payload

    def halt(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self._trading_state = "HALTED"
        return self._ack(
            "halt",
            request,
            source=NODE_STATE_EVIDENCE_SOURCE,
            trading_state="HALTED",
            observed_at=self._evidence_timestamp.isoformat(),
        )

    def _identity_payload(self) -> dict[str, Any]:
        return _identity_payload(self._authorization)

    def _ack(
        self,
        action: str,
        request: Mapping[str, Any],
        **extra: Any,
    ) -> Mapping[str, Any]:
        payload = self._identity_payload()
        payload.update(
            {
                "accepted": True,
                "action": action,
                "evidence_sha256": _synthetic_hash(
                    f"dry-run-{action}"
                ),
            }
        )
        side_effect_id = request.get("side_effect_id")
        if side_effect_id is not None:
            payload["side_effect_id"] = side_effect_id
        payload.update(extra)
        return payload


class SingleUsePermitStore:
    def __init__(
        self,
        path: Path,
        *,
        store_id: str = LIVE_PERMIT_STORE_ID,
        testing_allow_live_path_override: bool = False,
        clock: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
    ) -> None:
        self._path = _required_absolute_path(
            path,
            "permit ledger path",
        )
        self._lock_path = self._path.with_suffix(
            self._path.suffix + ".lock"
        )
        self._store_id = _required_text(
            store_id,
            "permit store_id",
        )
        self._testing_allow_live_path_override = bool(
            testing_allow_live_path_override
        )
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._path

    @property
    def store_id(self) -> str:
        return self._store_id

    def validate_live_binding(
        self,
        authorization: CanaryAuthorization,
    ) -> None:
        if self._store_id != authorization.release.permit_store_id:
            raise LiveTradeExecutionError(
                "permit store identity differs from signed release"
            )
        signed_path = Path(authorization.release.permit_store_path)
        if signed_path != DEFAULT_LIVE_PERMIT_LEDGER_PATH:
            raise LiveTradeExecutionError(
                "signed permit ledger path differs from fixed live path"
            )
        if self._testing_allow_live_path_override:
            return
        if self._path != DEFAULT_LIVE_PERMIT_LEDGER_PATH:
            raise LiveTradeExecutionError(
                "live permit ledger path is fixed"
            )

    def claim(
        self,
        authorization: CanaryAuthorization,
        *,
        mode: str,
    ) -> None:
        outcome = self.claim_or_recover(
            authorization,
            mode=mode,
        )
        if outcome.newly_authorized is not True:
            raise DuplicatePermitError(
                "single-use permit already has durable state"
            )

    def claim_or_recover(
        self,
        authorization: CanaryAuthorization,
        *,
        mode: str,
    ) -> PermitClaimOutcome:
        normalized_mode = _execution_mode(mode)

        def operation(
            payload: dict[str, Any],
        ) -> PermitClaimOutcome:
            records = payload["records"]
            record_key = self._record_key(
                authorization,
                normalized_mode,
            )
            existing = records.get(record_key)
            if isinstance(existing, dict):
                self._require_record_identity(
                    existing,
                    authorization,
                    normalized_mode,
                )
                state = self._record_state(existing)
                pending_evidence = existing.get("pending_evidence")
                if not isinstance(pending_evidence, dict):
                    pending_evidence = None
                pending_action = str(
                    existing.get("pending_action") or ""
                )
                pending_action_payload = existing.get(
                    "pending_action_payload"
                )
                if not isinstance(pending_action_payload, dict):
                    pending_action_payload = None
                return PermitClaimOutcome(
                    newly_authorized=False,
                    recovery_required=(
                        state != "EVIDENCE_COMMITTED"
                    ),
                    terminal=state == "EVIDENCE_COMMITTED",
                    state=state,
                    pending_action=pending_action,
                    pending_action_payload=pending_action_payload,
                    pending_evidence=pending_evidence,
                )
            live_identity_fields = (
                "permit_id",
                "release_id",
                "intent_id",
                "close_intent_id",
                "open_client_order_id",
                "close_client_order_id",
            )
            for record in records.values():
                if record.get("mode") != normalized_mode:
                    continue
                for field_name in live_identity_fields:
                    expected = getattr(
                        authorization,
                        field_name,
                        False,
                    )
                    if expected is False:
                        expected = getattr(
                            authorization.release,
                            field_name,
                            False,
                        )
                    if record.get(field_name) == expected:
                        raise DuplicatePermitError(
                            f"single-use identity already consumed: {field_name}"
                        )
            record = {
                "mode": normalized_mode,
                "permit_id": authorization.permit_id,
                "release_id": authorization.release.release_id,
                "intent_id": authorization.intent_id,
                "close_intent_id": authorization.close_intent_id,
                "open_client_order_id": (
                    authorization.open_client_order_id
                ),
                "close_client_order_id": (
                    authorization.close_client_order_id
                ),
                "authorization_sha256": (
                    authorization.authorization_sha256
                ),
                "state": "AUTHORIZED",
                "pending_action": "",
                "pending_action_payload": {},
                "claimed_at": _utc_now(self._clock).isoformat(),
                "evidence_sha256": "",
                "history": [],
            }
            self._append_history(
                record,
                event_type="AUTHORIZED",
                state="AUTHORIZED",
                payload={
                    "authorization_sha256": (
                        authorization.authorization_sha256
                    ),
                },
            )
            records[record_key] = record
            return PermitClaimOutcome(
                newly_authorized=True,
                recovery_required=False,
                terminal=False,
                state="AUTHORIZED",
                pending_action="",
                pending_action_payload=None,
                pending_evidence=None,
            )

        return self._update(operation)

    def prepare_action(
        self,
        authorization: CanaryAuthorization,
        *,
        mode: str,
        action: str,
        payload: Mapping[str, Any],
        state: str | None = None,
    ) -> None:
        normalized_mode = _execution_mode(mode)
        normalized_action = _required_text(action, "journal action")
        action_payload = _json_safe(dict(payload))
        next_state = state
        if next_state is not None:
            next_state = _journal_state(next_state)

        def operation(document: dict[str, Any]) -> None:
            record = self._record_for_authorization(
                document,
                authorization,
                normalized_mode,
            )
            current_state = self._record_state(record)
            if current_state == "EVIDENCE_COMMITTED":
                raise LiveTradeExecutionError(
                    "terminal permit journal cannot start an action"
                )
            if next_state is not None:
                record["state"] = next_state
            record["pending_action"] = normalized_action
            record["pending_action_payload"] = action_payload
            self._append_history(
                record,
                event_type="ACTION_PENDING",
                state=self._record_state(record),
                payload={
                    "action": normalized_action,
                    "request": action_payload,
                },
            )

        self._update(operation)

    def complete_action(
        self,
        authorization: CanaryAuthorization,
        *,
        mode: str,
        action: str,
        state: str | None,
        result: Mapping[str, Any],
    ) -> None:
        normalized_mode = _execution_mode(mode)
        normalized_action = _required_text(action, "journal action")
        next_state = state
        if next_state is not None:
            next_state = _journal_state(next_state)

        def operation(payload: dict[str, Any]) -> None:
            record = self._record_for_authorization(
                payload,
                authorization,
                normalized_mode,
            )
            pending_action = str(
                record.get("pending_action") or ""
            )
            if pending_action != normalized_action:
                raise LiveTradeExecutionError(
                    "journal pending action changed before completion"
                )
            current_state = self._record_state(record)
            event_type = "ACTION_CONFIRMED"
            if next_state is not None:
                record["state"] = next_state
                current_state = next_state
                event_type = next_state
            record["pending_action"] = ""
            record["pending_action_payload"] = {}
            record["last_result"] = _json_safe(dict(result))
            self._append_history(
                record,
                event_type=event_type,
                state=current_state,
                payload={
                    "action": normalized_action,
                    "result": dict(result),
                },
            )

        self._update(operation)

    def record_recovery_event(
        self,
        authorization: CanaryAuthorization,
        *,
        mode: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        normalized_mode = _execution_mode(mode)
        normalized_event = _required_text(
            event_type,
            "recovery event_type",
        )

        def operation(document: dict[str, Any]) -> None:
            record = self._record_for_authorization(
                document,
                authorization,
                normalized_mode,
            )
            self._append_history(
                record,
                event_type=normalized_event,
                state=self._record_state(record),
                payload=payload,
            )

        self._update(operation)

    def mark_state(
        self,
        authorization: CanaryAuthorization,
        *,
        mode: str,
        state: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        normalized_mode = _execution_mode(mode)
        next_state = _journal_state(state)

        def operation(document: dict[str, Any]) -> None:
            record = self._record_for_authorization(
                document,
                authorization,
                normalized_mode,
            )
            record["state"] = next_state
            record["pending_action"] = ""
            record["pending_action_payload"] = {}
            self._append_history(
                record,
                event_type=event_type,
                state=next_state,
                payload=payload,
            )

        self._update(operation)

    def prepare_evidence(
        self,
        authorization: CanaryAuthorization,
        *,
        mode: str,
        evidence_path: Path,
        evidence: Mapping[str, Any],
        evidence_sha256: str,
        passed: bool,
        allow_terminal_enrichment: bool = False,
    ) -> None:
        normalized_mode = _execution_mode(mode)
        normalized_hash = _required_sha256(
            evidence_sha256,
            "evidence_sha256",
        )
        absolute_path = _required_absolute_path(
            evidence_path,
            "evidence path",
        )

        def operation(document: dict[str, Any]) -> None:
            record = self._record_for_authorization(
                document,
                authorization,
                normalized_mode,
            )
            current_state = self._record_state(record)
            enrichment_of_sha256 = ""
            if (
                current_state == "EVIDENCE_COMMITTED"
                and allow_terminal_enrichment
            ):
                enrichment_of_sha256 = _required_sha256(
                    record.get("evidence_sha256"),
                    "committed enrichment evidence_sha256",
                )
                record["state"] = "EVIDENCE_ENRICHMENT_PENDING"
                current_state = "EVIDENCE_ENRICHMENT_PENDING"
            elif current_state != "HALTED":
                raise LiveTradeExecutionError(
                    "evidence can only be prepared after HALTED"
                )
            pending = {
                "path": str(absolute_path),
                "sha256": normalized_hash,
                "passed": bool(passed),
                "payload": _json_safe(dict(evidence)),
            }
            if enrichment_of_sha256:
                pending["enrichment_of_sha256"] = (
                    enrichment_of_sha256
                )
            record["pending_evidence"] = pending
            record["pending_action"] = "PUBLISH_EVIDENCE"
            record["pending_action_payload"] = {
                "path": str(absolute_path),
                "sha256": normalized_hash,
            }
            event_type = "EVIDENCE_PREPARED"
            if enrichment_of_sha256:
                event_type = "EVIDENCE_ENRICHMENT_PREPARED"
            self._append_history(
                record,
                event_type=event_type,
                state=current_state,
                payload={
                    "path": str(absolute_path),
                    "sha256": normalized_hash,
                    "passed": bool(passed),
                    "enrichment_of_sha256": (
                        enrichment_of_sha256
                    ),
                },
            )

        self._update(operation)

    def commit_evidence(
        self,
        authorization: CanaryAuthorization,
        *,
        mode: str,
        evidence_sha256: str,
    ) -> None:
        normalized_mode = _execution_mode(mode)
        normalized_hash = _required_sha256(
            evidence_sha256,
            "evidence_sha256",
        )

        def operation(document: dict[str, Any]) -> None:
            record = self._record_for_authorization(
                document,
                authorization,
                normalized_mode,
            )
            pending = record.get("pending_evidence")
            if not isinstance(pending, dict):
                raise LiveTradeExecutionError(
                    "prepared evidence journal is missing"
                )
            if pending.get("sha256") != normalized_hash:
                raise LiveTradeExecutionError(
                    "prepared evidence hash changed"
                )
            record["state"] = "EVIDENCE_COMMITTED"
            record["pending_action"] = ""
            record["pending_action_payload"] = {}
            record["evidence_sha256"] = normalized_hash
            record["evidence_path"] = str(pending.get("path") or "")
            record["completed_at"] = _utc_now(
                self._clock
            ).isoformat()
            self._append_history(
                record,
                event_type="EVIDENCE_COMMITTED",
                state="EVIDENCE_COMMITTED",
                payload={
                    "path": record["evidence_path"],
                    "sha256": normalized_hash,
                    "passed": bool(pending.get("passed")),
                },
            )

        self._update(operation)

    def snapshot(
        self,
        authorization: CanaryAuthorization,
        *,
        mode: str,
    ) -> Mapping[str, Any] | None:
        normalized_mode = _execution_mode(mode)

        def operation(document: dict[str, Any]):
            record_key = self._record_key(
                authorization,
                normalized_mode,
            )
            record = document["records"].get(record_key)
            if not isinstance(record, dict):
                return None
            self._require_record_identity(
                record,
                authorization,
                normalized_mode,
            )
            return json.loads(json.dumps(record))

        return self._read(operation)

    def _update(
        self,
        operation: Callable[[dict[str, Any]], Any],
    ) -> Any:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _open_protected_lock_file(self._lock_path) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            payload = self._load()
            result = operation(payload)
            _atomic_write_json(self._path, payload, mode=0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return result

    def _read(
        self,
        operation: Callable[[dict[str, Any]], Any],
    ) -> Any:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _open_protected_lock_file(self._lock_path) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            payload = self._load()
            result = operation(payload)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return result

    def _load(self) -> dict[str, Any]:
        if not os.path.lexists(self._path):
            return {
                "schema_version": LEDGER_SCHEMA,
                "store_id": self._store_id,
                "canonical_path": str(self._path),
                "records": {},
            }
        try:
            payload = json.loads(
                _read_protected_file(
                    self._path,
                    "single-use permit ledger",
                    live=True,
                )
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveTradeExecutionError(
                "single-use permit ledger is unreadable"
            ) from exc
        if not isinstance(payload, dict):
            raise LiveTradeExecutionError(
                "single-use permit ledger must be an object"
            )
        if payload.get("schema_version") != LEDGER_SCHEMA:
            raise LiveTradeExecutionError(
                "single-use permit ledger schema mismatch"
            )
        if payload.get("store_id") != self._store_id:
            raise LiveTradeExecutionError(
                "single-use permit ledger store identity mismatch"
            )
        if payload.get("canonical_path") != str(self._path):
            raise LiveTradeExecutionError(
                "single-use permit ledger canonical path mismatch"
            )
        records = payload.get("records")
        if not isinstance(records, dict):
            raise LiveTradeExecutionError(
                "single-use permit ledger records must be an object"
            )
        return payload

    def _record_key(
        self,
        authorization: CanaryAuthorization,
        mode: str,
    ) -> str:
        return f"{mode}:{authorization.permit_id}"

    def _record_for_authorization(
        self,
        document: dict[str, Any],
        authorization: CanaryAuthorization,
        mode: str,
    ) -> dict[str, Any]:
        record_key = self._record_key(authorization, mode)
        record = document["records"].get(record_key)
        if not isinstance(record, dict):
            raise LiveTradeExecutionError(
                "single-use permit journal is missing"
            )
        self._require_record_identity(
            record,
            authorization,
            mode,
        )
        return record

    def _require_record_identity(
        self,
        record: Mapping[str, Any],
        authorization: CanaryAuthorization,
        mode: str,
    ) -> None:
        expected = {
            "mode": mode,
            "permit_id": authorization.permit_id,
            "release_id": authorization.release.release_id,
            "intent_id": authorization.intent_id,
            "close_intent_id": authorization.close_intent_id,
            "open_client_order_id": (
                authorization.open_client_order_id
            ),
            "close_client_order_id": (
                authorization.close_client_order_id
            ),
            "authorization_sha256": (
                authorization.authorization_sha256
            ),
        }
        for field_name, expected_value in expected.items():
            if record.get(field_name) != expected_value:
                raise LiveTradeExecutionError(
                    f"permit journal identity mismatch: {field_name}"
                )

    def _record_state(self, record: Mapping[str, Any]) -> str:
        return _journal_state(record.get("state"))

    def _append_history(
        self,
        record: dict[str, Any],
        *,
        event_type: str,
        state: str,
        payload: Mapping[str, Any],
    ) -> None:
        history = record.get("history")
        if not isinstance(history, list):
            raise LiveTradeExecutionError(
                "permit journal history is invalid"
            )
        history.append(
            {
                "sequence": len(history) + 1,
                "recorded_at": _utc_now(self._clock).isoformat(),
                "event_type": _required_text(
                    event_type,
                    "journal event_type",
                ),
                "state": _journal_state(state),
                "payload": _json_safe(dict(payload)),
            }
        )


class AtomicEvidenceWriter:
    def __init__(
        self,
        path: Path,
        *,
        live: bool = False,
    ) -> None:
        self._protected_path: ProtectedOutputPath | None = None
        if live:
            self._protected_path = _bind_protected_output_path(
                path,
                "live evidence path",
                owner_uid=os.geteuid(),
            )
            self._path = self._protected_path.target
        else:
            self._path = _required_absolute_path(
                path,
                "evidence path",
            )

    @property
    def path(self) -> Path:
        return self._path

    def write(self, evidence: Mapping[str, Any]) -> str:
        payload, evidence_sha256 = self.prepare(evidence)
        return self.commit_prepared(
            payload,
            evidence_sha256=evidence_sha256,
            allow_existing=False,
        )

    def prepare(
        self,
        evidence: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        normalized = _json_safe(dict(evidence))
        payload = _canonical_pretty_json_bytes(normalized)
        return normalized, _sha256_bytes(payload)

    def commit_prepared(
        self,
        evidence: Mapping[str, Any],
        *,
        evidence_sha256: str,
        allow_existing: bool,
        replace_existing: bool = False,
    ) -> str:
        normalized_hash = _required_sha256(
            evidence_sha256,
            "prepared evidence_sha256",
        )
        payload = _canonical_pretty_json_bytes(dict(evidence))
        if _sha256_bytes(payload) != normalized_hash:
            raise LiveTradeExecutionError(
                "prepared evidence payload hash mismatch"
            )
        if os.path.lexists(self._path):
            if allow_existing:
                existing = _read_protected_file(
                    self._path,
                    "existing evidence",
                    live=True,
                )
                if _sha256_bytes(existing) == normalized_hash:
                    return normalized_hash
            if not replace_existing:
                raise LiveTradeExecutionError(
                    f"evidence path already exists: {self._path}"
                )
        _atomic_write_bytes(
            self._path,
            payload,
            mode=0o400,
            replace_existing=replace_existing,
            protected_path=self._protected_path,
        )
        return normalized_hash


class AccountALiveTradeExecutor:
    def __init__(
        self,
        *,
        adapter: TradeAdapter,
        permit_store: SingleUsePermitStore,
        evidence_writer: AtomicEvidenceWriter,
        operation_lock: LiveOperationLock | None = None,
        mode: str = "dry-run",
        max_observations: int = 30,
        poll_interval_seconds: float = 0.0,
        recovery_max_attempts: int = 3,
        recovery_deadline_seconds: float = 20.0,
        recovery_retry_interval_seconds: float = 0.0,
        soft_action_max_attempts: int = 3,
        soft_action_deadline_seconds: float = 5.0,
        journal_write_timeout_seconds: float = 1.0,
        evidence_only: bool = False,
        clock: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._adapter = adapter
        self._permit_store = permit_store
        self._evidence_writer = evidence_writer
        self._operation_lock = operation_lock
        self._mode = _execution_mode(mode)
        if isinstance(max_observations, bool) or max_observations < 1:
            raise LiveTradeExecutionError(
                "max_observations must be a positive integer"
            )
        self._max_observations = int(max_observations)
        self._poll_interval_seconds = _non_negative_float(
            poll_interval_seconds,
            "poll_interval_seconds",
        )
        if (
            isinstance(recovery_max_attempts, bool)
            or recovery_max_attempts < 1
        ):
            raise LiveTradeExecutionError(
                "recovery_max_attempts must be a positive integer"
            )
        self._recovery_max_attempts = int(recovery_max_attempts)
        self._recovery_deadline_seconds = _positive_float(
            recovery_deadline_seconds,
            "recovery_deadline_seconds",
        )
        self._recovery_retry_interval_seconds = _non_negative_float(
            recovery_retry_interval_seconds,
            "recovery_retry_interval_seconds",
        )
        if (
            isinstance(soft_action_max_attempts, bool)
            or soft_action_max_attempts < 1
        ):
            raise LiveTradeExecutionError(
                "soft_action_max_attempts must be a positive integer"
            )
        self._soft_action_max_attempts = int(
            soft_action_max_attempts
        )
        self._soft_action_deadline_seconds = _positive_float(
            soft_action_deadline_seconds,
            "soft_action_deadline_seconds",
        )
        self._journal_write_timeout_seconds = _positive_float(
            journal_write_timeout_seconds,
            "journal_write_timeout_seconds",
        )
        self._evidence_only = bool(evidence_only)
        self._clock = clock
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._journal_degraded = False
        self._journal_failures: list[str] = []
        self._warnings: list[str] = []
        self._degraded_reasons: list[str] = []
        self._retry_counts: dict[str, int] = {}

    def execute(
        self,
        authorization: CanaryAuthorization,
    ) -> ExecutionResult:
        self._journal_degraded = False
        self._journal_failures = []
        self._warnings = []
        self._degraded_reasons = []
        self._retry_counts = {}
        self._require_live_operation_lock()
        execution_warnings = _validate_authorization_for_execution(
            authorization,
            mode=self._mode,
            now=_utc_now(self._clock),
            allow_expired=self._evidence_only,
        )
        for warning in authorization.warnings:
            self._add_warning(warning)
        for warning in execution_warnings:
            self._add_warning(warning)
        if self._mode == "live":
            self._permit_store.validate_live_binding(authorization)
        trail = AuditTrail(clock=self._clock)
        journal_enabled = False
        pending_evidence: Mapping[str, Any] | None = None
        recovery_close_request: Mapping[str, Any] | None = None
        recovery_started = False
        cleanup_complete = False
        cleanup_required = False
        cleanup_result = CleanupResult(
            safe=False,
            close_submitted=False,
            close_quantity=Decimal(0),
            pre_halt_snapshot=None,
            final_snapshot=None,
            errors=(),
        )
        actual_open_notional = Decimal(0)
        peak_cumulative_loss = Decimal(0)
        open_filled_quantity = Decimal(0)
        round_trip_complete = False
        open_result_unknown = False
        failure_reason = ""
        error_code = ""
        retryable = False
        finished_halted = False
        halt_attempted = False
        halt_observed_at: datetime | None = None
        open_dispatched_at: datetime | None = None
        recovery_only_active = self._evidence_only

        try:
            claim_outcome = self._permit_store.claim_or_recover(
                authorization,
                mode=self._mode,
            )
            journal_enabled = (
                claim_outcome.newly_authorized
                or claim_outcome.recovery_required
            )
            cleanup_required = claim_outcome.recovery_required
            pending_evidence = claim_outcome.pending_evidence
            recovery_started = claim_outcome.recovery_required
            if claim_outcome.terminal:
                recovery_only_active = True
                cleanup_required = False
                cleanup_complete = True
                finished_halted = True
                halt_attempted = True
                committed_record = self._permit_store.snapshot(
                    authorization,
                    mode=self._mode,
                )
                if (
                    self._mode == "live"
                    and self._evidence_only is False
                    and not os.path.lexists(
                        self._evidence_writer.path
                    )
                ):
                    return self._recovery_gate_required_result(
                        disposition="terminal evidence file restore",
                    )
                committed_result = self._read_committed_evidence_result(
                    authorization,
                    committed_record,
                )
                if (
                    self._evidence_only
                    and _committed_evidence_requires_enrichment(
                        committed_record,
                        authorization,
                        mode=self._mode,
                    )
                ):
                    return self._finalize_halted_recovery(
                        authorization,
                        trail,
                        committed_record,
                        enrich_committed=True,
                    )
                return committed_result
            if claim_outcome.state == "RISK_RECOVERY_REQUIRED":
                recovery_only_active = True
                cleanup_required = False
                cleanup_complete = True
                finished_halted = True
                halt_attempted = True
                return ExecutionResult(
                    status=RESULT_BLOCKED,
                    failure_reason=(
                        "permit journal requires separately authorized "
                        "target risk recovery"
                    ),
                    evidence_path=self._evidence_writer.path,
                    evidence_sha256="",
                    finished_halted=True,
                    close_submitted=False,
                    close_quantity=Decimal(0),
                    warnings=tuple(self._warnings),
                    degraded_reasons=tuple(
                        self._degraded_reasons
                    ),
                    retryable=False,
                    error_code="RISK_RECOVERY_REQUIRED",
                    retry_counts=dict(self._retry_counts),
                )
            if claim_outcome.pending_action == "CLOSE":
                try:
                    recovery_close_request = (
                        _validated_pending_close_request(
                            claim_outcome.pending_action_payload,
                            authorization,
                        )
                    )
                except LiveTradeExecutionError as exc:
                    self._permit_store.mark_state(
                        authorization,
                        mode=self._mode,
                        state="RISK_RECOVERY_REQUIRED",
                        event_type="PENDING_CLOSE_IDENTITY_CONFLICT",
                        payload={
                            "reason": _exception_text(exc),
                        },
                    )
                    recovery_only_active = True
                    cleanup_required = False
                    cleanup_complete = True
                    finished_halted = True
                    halt_attempted = True
                    return ExecutionResult(
                        status=RESULT_BLOCKED,
                        failure_reason=_exception_text(exc),
                        evidence_path=self._evidence_writer.path,
                        evidence_sha256="",
                        finished_halted=True,
                        close_submitted=False,
                        close_quantity=Decimal(0),
                        warnings=tuple(self._warnings),
                        degraded_reasons=tuple(
                            self._degraded_reasons
                        ),
                        retryable=False,
                        error_code="RISK_RECOVERY_REQUIRED",
                        retry_counts=dict(self._retry_counts),
                    )
            if (
                self._mode == "live"
                and self._evidence_only is False
                and claim_outcome.state
                in {
                    "HALTED",
                    "EVIDENCE_ENRICHMENT_PENDING",
                }
                and claim_outcome.pending_action
                in {
                    "",
                    "PUBLISH_EVIDENCE",
                }
            ):
                recovery_only_active = True
                cleanup_required = False
                cleanup_complete = True
                finished_halted = True
                halt_attempted = True
                return self._recovery_gate_required_result(
                    disposition="HALTED evidence recovery",
                )
            if (
                claim_outcome.recovery_required
                and claim_outcome.state == "HALTED"
                and pending_evidence is None
            ):
                recovery_record = self._permit_store.snapshot(
                    authorization,
                    mode=self._mode,
                )
                if _is_recovery_only_record(recovery_record):
                    recovery_only_active = True
                    cleanup_required = False
                    cleanup_complete = True
                    finished_halted = True
                    halt_attempted = True
                    try:
                        return self._finalize_halted_recovery(
                            authorization,
                            trail,
                            recovery_record,
                        )
                    except Exception as exc:
                        retryable_recovery = isinstance(
                            exc,
                            RecoverableEvidenceError,
                        ) or _is_soft_action_failure(exc)
                        failure_code = "EXECUTION_BLOCKED"
                        if isinstance(exc, ClassifiedExecutionError):
                            failure_code = exc.code
                        if retryable_recovery:
                            failure_code = (
                                "RECOVERY_EVIDENCE_INCOMPLETE"
                            )
                        failure_text = _exception_text(exc)
                        trail.record(
                            "recovery_only_finalizer_blocked",
                            {
                                "reason": failure_text,
                                "retryable": retryable_recovery,
                                "error_code": failure_code,
                            },
                        )
                        return ExecutionResult(
                            status=RESULT_BLOCKED,
                            failure_reason=failure_text,
                            evidence_path=self._evidence_writer.path,
                            evidence_sha256="",
                            finished_halted=True,
                            close_submitted=False,
                            close_quantity=Decimal(0),
                            warnings=tuple(self._warnings),
                            degraded_reasons=tuple(
                                self._degraded_reasons
                            ),
                            retryable=retryable_recovery,
                            error_code=failure_code,
                            retry_counts=dict(self._retry_counts),
                        )
            if (
                claim_outcome.recovery_required
                and claim_outcome.state
                in {
                    "HALTED",
                    "EVIDENCE_ENRICHMENT_PENDING",
                }
                and pending_evidence is not None
                and _is_recovery_only_pending_evidence(
                    pending_evidence
                )
            ):
                cleanup_required = False
                cleanup_complete = True
                finished_halted = True
                halt_attempted = True
                recovered_result = self._commit_recovered_evidence(
                    authorization,
                    pending_evidence,
                )
                if recovered_result is None:
                    raise LiveTradeExecutionError(
                        "prepared recovery-only evidence path mismatch"
                    )
                return recovered_result
            if self._evidence_only:
                failure_text = (
                    "evidence-only recovery requires a HALTED permit "
                    "journal with no pending side effect"
                )
                cleanup_required = False
                cleanup_complete = True
                halt_attempted = True
                trail.record(
                    "recovery_only_journal_rejected",
                    {
                        "reason": failure_text,
                        "state": claim_outcome.state,
                        "pending_evidence": pending_evidence is not None,
                    },
                )
                return ExecutionResult(
                    status=RESULT_BLOCKED,
                    failure_reason=failure_text,
                    evidence_path=self._evidence_writer.path,
                    evidence_sha256="",
                    finished_halted=(
                        claim_outcome.state == "HALTED"
                    ),
                    close_submitted=False,
                    close_quantity=Decimal(0),
                    warnings=tuple(self._warnings),
                    degraded_reasons=tuple(
                        self._degraded_reasons
                    ),
                    retryable=False,
                    error_code="RECOVERY_JOURNAL_INELIGIBLE",
                    retry_counts=dict(self._retry_counts),
                )
            if claim_outcome.recovery_required:
                try:
                    self._run_journal_write(
                        lambda: self._permit_store.record_recovery_event(
                            authorization,
                            mode=self._mode,
                            event_type="RECOVERY_REQUIRED",
                            payload={
                                "state": claim_outcome.state,
                                "reason": (
                                    "startup found an incomplete "
                                    "permit journal"
                                ),
                            },
                        )
                    )
                except BaseException as exc:  # noqa: BLE001
                    failure = self._record_journal_failure(
                        phase="record",
                        action="RECOVERY_REQUIRED",
                        exc=exc,
                    )
                    trail.record(
                        "permit_journal_bypassed_for_safety",
                        {
                            "phase": "record",
                            "action": "RECOVERY_REQUIRED",
                            "journal_failure": failure,
                        },
                    )
                raise LiveTradeExecutionError(
                    "incomplete permit journal requires recovery"
                )
            trail.record(
                "permit_claimed",
                {
                    "mode": self._mode,
                    "permit_id": authorization.permit_id,
                    "authorization_sha256": (
                        authorization.authorization_sha256
                    ),
                },
            )
            self._run_live_preflight(
                authorization,
                trail,
                phase="before-resume",
            )
            health_warnings = _validate_health_freshness(
                authorization,
                now=_utc_now(self._clock),
                label="before RESUME",
            )
            for warning in health_warnings:
                self._add_warning(warning)
            resume_request = self._base_request(authorization)
            resume_request.update(
                {
                    "command": "RESUME",
                    "scope": "single-canary-round-trip",
                    "max_round_trips": 1,
                    "side_effect_id": deterministic_side_effect_id(
                        authorization,
                        "RESUME",
                    ),
                }
            )
            self._journal_prepare(
                authorization,
                enabled=journal_enabled,
                action="RESUME",
                request=resume_request,
            )
            resume_dispatched_at = _utc_now(self._clock)
            resume_result = self._run_soft_action(
                action="RESUME",
                trail=trail,
                operation=lambda: _validate_resume_ack(
                    self._adapter.resume(resume_request),
                    authorization,
                    now=_utc_now(self._clock),
                    expected_side_effect_id=resume_request[
                        "side_effect_id"
                    ],
                ),
            )
            open_journal_fallback_state = "AUTHORIZED"
            resume_exchange_not_before = resume_dispatched_at
            if resume_result is False:
                self._journal_continue_after_soft_failure(
                    authorization,
                    enabled=journal_enabled,
                    action="RESUME",
                    state="AUTHORIZED",
                )
                trail.record(
                    "limited_resume_degraded_continue",
                    {
                        "side_effect_id": resume_request[
                            "side_effect_id"
                        ],
                    },
                )
            else:
                (
                    resume_hash,
                    resume_observed_at,
                    resume_warnings,
                ) = resume_result
                for warning in resume_warnings:
                    self._add_warning(warning)
                resume_exchange_not_before = resume_observed_at
                open_journal_fallback_state = "RESUMED"
                self._journal_complete(
                    authorization,
                    enabled=journal_enabled,
                    action="RESUME",
                    state="RESUMED",
                    result={
                        "adapter_evidence_sha256": resume_hash,
                        "observed_at": resume_observed_at.isoformat(),
                    },
                )
                trail.record(
                    "limited_resume_accepted",
                    {
                        "adapter_evidence_sha256": resume_hash,
                        "observed_at": resume_observed_at.isoformat(),
                        "side_effect_id": resume_request[
                            "side_effect_id"
                        ],
                    },
                )

            open_now = _utc_now(self._clock)
            before_open_preflight = self._run_live_preflight(
                authorization,
                trail,
                phase="before-open",
            )
            if (
                before_open_preflight is not False
                and before_open_preflight.fetched_at
                < resume_exchange_not_before
            ):
                causal_warning = (
                    "BEFORE_OPEN_CAUSAL_FRESHNESS_RELAXED: exchange "
                    "snapshot predates RESUME; max-age, target position, "
                    "target orders, and known available balance "
                    "sufficiency remain enforced"
                )
                self._add_warning(causal_warning)
                trail.record(
                    "before_open_exchange_causality_relaxed",
                    {
                        "resume_boundary": (
                            resume_exchange_not_before.isoformat()
                        ),
                        "snapshot_fetched_at": (
                            before_open_preflight.fetched_at.isoformat()
                        ),
                        "retained_hard_checks": [
                            "exchange_max_age",
                            "target_position_flat",
                            "target_regular_orders_zero",
                            "target_algo_orders_zero",
                            "known_available_balance_sufficiency",
                        ],
                    },
                )
            health_warnings = _validate_health_freshness(
                authorization,
                now=open_now,
                label="before OPEN",
            )
            for warning in health_warnings:
                self._add_warning(warning)
            if authorization.expires_at <= open_now:
                raise LiveTradeExecutionError(
                    "permit expired before open"
                )
            open_request = self._base_request(authorization)
            open_request.update(
                {
                    "client_order_id": (
                        authorization.open_client_order_id
                    ),
                    "side": authorization.open_side,
                    "order_type": "LIMIT",
                    "time_in_force": "IOC",
                    "quantity": _decimal_text(authorization.quantity),
                    "limit_price_usdt": _decimal_text(
                        authorization.limit_price_usdt
                    ),
                    "max_actual_open_notional_usdt": (
                        _decimal_text(
                            MAX_ACTUAL_OPEN_NOTIONAL_USDT
                        )
                    ),
                    "side_effect_id": deterministic_side_effect_id(
                        authorization,
                        "OPEN",
                    ),
                }
            )
            open_dispatched_at = _utc_now(self._clock)
            cleanup_required = True
            open_acknowledged = self._dispatch_open(
                authorization,
                trail,
                request=open_request,
                enabled=journal_enabled,
                journal_fallback_state=open_journal_fallback_state,
                attempt=1,
            )
            observation = self._observe_open_after_dispatch(
                authorization,
                trail,
                journal_enabled=journal_enabled,
                dispatch_acknowledged=open_acknowledged,
                journal_fallback_state=open_journal_fallback_state,
                exchange_not_before=open_dispatched_at,
            )
            open_result_unknown = (
                observation is False and not open_acknowledged
            )
            if (
                observation is not False
                and _is_terminal_ioc_zero_fill(observation)
                and not _is_terminal_ioc_no_fill(observation)
            ):
                raise ClassifiedExecutionError(
                    (
                        "terminal IOC open produced no fill without "
                        "definitive exchange absence proof"
                    ),
                    code="OPEN_RESULT_AMBIGUOUS",
                )
            if (
                observation is not False
                and _is_terminal_ioc_no_fill(observation)
            ):
                retryable = True
                self._add_degraded(
                    "DEGRADED_NO_FILL: terminal IOC open produced "
                    "no fill; a new signed permit and intent may retry"
                )
                trail.record(
                    "open_terminal_no_fill",
                    {
                        "open_status": observation.open_status,
                        "client_order_id": (
                            authorization.open_client_order_id
                        ),
                        "intent_id": authorization.intent_id,
                        "side_effect_id": open_request[
                            "side_effect_id"
                        ],
                    },
                )
                raise ClassifiedExecutionError(
                    (
                        "terminal IOC open produced no fill; "
                        "retry requires a new signed permit and intent"
                    ),
                    code="DEGRADED_NO_FILL",
                )
            if observation is not False:
                open_filled_quantity = observation.filled_quantity
                actual_open_notional = (
                    observation.actual_open_notional_usdt
                )
                peak_cumulative_loss = (
                    observation.cumulative_net_loss_usdt
                )
                if observation.filled_quantity <= 0:
                    raise LiveTradeExecutionError(
                        "open order produced no fill"
                    )
                if (
                    actual_open_notional
                    > authorization.max_notional_usdt
                ):
                    raise LiveTradeExecutionError(
                        "actual open notional exceeds permit"
                    )
                if actual_open_notional > MAX_ACTUAL_OPEN_NOTIONAL_USDT:
                    raise LiveTradeExecutionError(
                        "actual open notional exceeds 12 USDT"
                    )
                if observation.cumulative_net_loss_usdt >= (
                    authorization.max_cumulative_net_loss_usdt
                ):
                    raise LiveTradeExecutionError(
                        "cumulative net loss threshold reached"
                    )

            (
                finished_halted,
                halt_error,
                halt_observed_at,
            ) = self._halt_with_recovery(
                authorization,
                trail,
                reason="open-risk-window-closed",
                journal_enabled=journal_enabled,
            )
            halt_attempted = True
            if not finished_halted:
                raise ClassifiedExecutionError(
                    halt_error,
                    code="HALT_UNPROVEN",
                )
            expected_close_quantity = authorization.quantity
            if observation is not False:
                expected_close_quantity = observation.filled_quantity
            cleanup_result = self._flatten_target_risk(
                authorization,
                trail,
                reason="round-trip-close",
                journal_enabled=journal_enabled,
                exchange_not_before=open_dispatched_at,
                expected_close_quantity=expected_close_quantity,
            )
            cleanup_complete = True
            if (
                open_result_unknown
                and cleanup_result.close_submitted is not True
            ):
                if cleanup_result.safe is not True:
                    raise LiveTradeExecutionError(
                        _join_errors(
                            "target risk cleanup failed",
                            cleanup_result.errors,
                        )
                    )
                raise ClassifiedExecutionError(
                    (
                        "OPEN exchange result remained unknown after "
                        "bounded reconciliation"
                    ),
                    code="OPEN_RESULT_AMBIGUOUS",
                )
            if open_result_unknown:
                self._add_degraded(
                    "OPEN confirmed executed by exchange position; "
                    "exact close reconciled the missing response"
                )
                trail.record(
                    "open_confirmed_executed_by_position",
                    {
                        "close_quantity": _decimal_text(
                            cleanup_result.close_quantity
                        ),
                        "client_order_id": (
                            authorization.open_client_order_id
                        ),
                    },
                )
            if cleanup_result.safe is not True:
                raise LiveTradeExecutionError(
                    _join_errors(
                        "target risk cleanup failed",
                        cleanup_result.errors,
                    )
                )
            if (
                observation is not False
                and cleanup_result.close_quantity
                != observation.filled_quantity
            ):
                raise LiveTradeExecutionError(
                    "close quantity differs from open filled quantity"
                )
            if cleanup_result.close_submitted is not True:
                raise LiveTradeExecutionError(
                    "round trip requires an exact reduce-only close"
                )
            if cleanup_result.pre_halt_snapshot is None:
                raise LiveTradeExecutionError(
                    "pre-HALT close proof snapshot is missing"
                )
            final_loss = _decimal(
                cleanup_result.pre_halt_snapshot.get(
                    "cumulative_net_loss_usdt"
                ),
                "final cumulative_net_loss_usdt",
                non_negative=True,
            )
            peak_cumulative_loss = max(
                peak_cumulative_loss,
                final_loss,
            )
            if final_loss >= (
                authorization.max_cumulative_net_loss_usdt
            ):
                raise LiveTradeExecutionError(
                    "final cumulative net loss threshold reached"
                )
            if observation is False:
                open_filled_quantity = cleanup_result.close_quantity
            round_trip_complete = True
        except BaseException as exc:  # noqa: BLE001
            if recovery_only_active:
                if not isinstance(exc, Exception):
                    raise
                cleanup_required = False
                cleanup_complete = True
                halt_attempted = True
                failure_text = _exception_text(exc)
                return ExecutionResult(
                    status=RESULT_BLOCKED,
                    failure_reason=failure_text,
                    evidence_path=self._evidence_writer.path,
                    evidence_sha256="",
                    finished_halted=True,
                    close_submitted=False,
                    close_quantity=Decimal(0),
                    warnings=tuple(self._warnings),
                    degraded_reasons=tuple(
                        self._degraded_reasons
                    ),
                    retryable=_is_soft_action_failure(exc),
                    error_code="RECOVERY_EVIDENCE_INCOMPLETE",
                    retry_counts=dict(self._retry_counts),
                )
            failure_reason = _exception_text(exc)
            error_code = _hard_failure_code(exc)
            trail.record(
                "execution_failed",
                {
                    "reason": failure_reason,
                },
            )
        finally:
            halt_reason = failure_reason
            for journal_failure in self._journal_failures:
                if journal_failure in halt_reason:
                    continue
                halt_reason = _join_errors(
                    halt_reason,
                    (journal_failure,),
                )
            if not halt_reason:
                halt_reason = "canary-round-trip-complete"
            if (
                cleanup_required
                and not finished_halted
                and not halt_attempted
            ):
                (
                    finished_halted,
                    halt_error,
                    halt_observed_at,
                ) = self._halt_with_recovery(
                    authorization,
                    trail,
                    reason=halt_reason,
                    journal_enabled=(
                        journal_enabled
                        and recovery_close_request is None
                    ),
                )
                halt_attempted = True
                if not finished_halted:
                    failure_reason = _join_errors(
                        failure_reason,
                        (halt_error,),
                    )
                    error_code = "HALT_UNPROVEN"
            if (
                failure_reason
                and cleanup_required
                and not cleanup_complete
            ):
                try:
                    emergency_result = self._flatten_target_risk(
                        authorization,
                        trail,
                        reason="failure-cleanup",
                        journal_enabled=journal_enabled,
                        exchange_not_before=open_dispatched_at,
                        expected_close_quantity=(
                            open_filled_quantity
                            if open_filled_quantity > 0
                            else authorization.quantity
                        ),
                        pending_close_request=recovery_close_request,
                    )
                    cleanup_result = emergency_result
                    cleanup_complete = True
                    if emergency_result.errors:
                        cleanup_text = "; ".join(
                            emergency_result.errors
                        )
                        failure_reason = _join_errors(
                            failure_reason,
                            (cleanup_text,),
                        )
                except BaseException as exc:  # noqa: BLE001
                    cleanup_error = (
                        "cleanup orchestration failed: "
                        f"{_exception_text(exc)}"
                    )
                    failure_reason = _join_errors(
                        failure_reason,
                        (cleanup_error,),
                    )
                    cleanup_result = CleanupResult(
                        safe=False,
                        close_submitted=False,
                        close_quantity=Decimal(0),
                        pre_halt_snapshot=None,
                        final_snapshot=None,
                        errors=(cleanup_error,),
                    )
                    trail.record(
                        "cleanup_orchestration_failed",
                        {
                            "reason": cleanup_error,
                        },
                    )
            final_halt_required = cleanup_required
            first_halt_required = not halt_attempted
            recovery_close_resolved = (
                recovery_close_request is None
                or cleanup_result.close_submitted
            )
            if final_halt_required or first_halt_required:
                (
                    finished_halted,
                    halt_error,
                    halt_observed_at,
                ) = self._halt_with_recovery(
                    authorization,
                    trail,
                    reason=halt_reason,
                    journal_enabled=(
                        journal_enabled
                        and recovery_close_resolved
                    ),
                )
                halt_attempted = True
                if not finished_halted:
                    failure_reason = _join_errors(
                        failure_reason,
                        (halt_error,),
                    )
                    error_code = "HALT_UNPROVEN"
            if (
                finished_halted
                and halt_observed_at is not None
            ):
                post_halt_snapshot, snapshot_error = (
                    self._fetch_post_halt_snapshot(
                        authorization,
                        trail,
                        halt_observed_at=halt_observed_at,
                        journal_enabled=(
                            journal_enabled
                            and recovery_close_resolved
                        ),
                    )
                )
                if post_halt_snapshot is False:
                    failure_reason = _join_errors(
                        failure_reason,
                        (snapshot_error,),
                    )
                    error_code = "POST_HALT_SNAPSHOT_UNPROVEN"
                    cleanup_result = replace(
                        cleanup_result,
                        safe=False,
                        errors=tuple(
                            dict.fromkeys(
                                (
                                    *cleanup_result.errors,
                                    snapshot_error,
                                )
                            )
                        ),
                    )
                else:
                    cleanup_result = replace(
                        cleanup_result,
                        final_snapshot=post_halt_snapshot,
                    )

        for journal_failure in self._journal_failures:
            if journal_failure in failure_reason:
                continue
            failure_reason = _join_errors(
                failure_reason,
                (journal_failure,),
            )
            error_code = "DURABLE_WRITE_OR_CAPACITY_FAILURE"

        final_snapshot = cleanup_result.final_snapshot
        if final_snapshot is not None:
            post_halt_loss = _decimal(
                final_snapshot.get("cumulative_net_loss_usdt"),
                "post-HALT cumulative_net_loss_usdt",
                non_negative=True,
            )
            peak_cumulative_loss = max(
                peak_cumulative_loss,
                post_halt_loss,
            )
            if post_halt_loss >= (
                authorization.max_cumulative_net_loss_usdt
            ):
                failure_reason = _join_errors(
                    failure_reason,
                    (
                        "post-HALT cumulative net loss threshold "
                        "reached",
                    ),
                )
                error_code = "LOSS_LIMIT_REACHED"
        if (
            final_snapshot is not None
            and final_snapshot.get("financial_proof_complete") is True
        ):
            final_open_quantity = _decimal(
                final_snapshot.get("open_filled_quantity"),
                "post-HALT open_filled_quantity",
                positive=True,
            )
            final_open_average_price = _decimal(
                final_snapshot.get("open_average_fill_price_usdt"),
                "post-HALT open_average_fill_price_usdt",
                positive=True,
            )
            final_actual_open_notional = (
                final_open_quantity * final_open_average_price
            )
            open_filled_quantity = final_open_quantity
            actual_open_notional = final_actual_open_notional
            if (
                cleanup_result.close_submitted
                and final_open_quantity != cleanup_result.close_quantity
            ):
                failure_reason = _join_errors(
                    failure_reason,
                    (
                        (
                            "final open filled quantity differs from exact "
                            "close quantity"
                        ),
                    ),
                )
                error_code = "EXACT_CLOSE_UNPROVEN"
            if final_actual_open_notional > authorization.max_notional_usdt:
                failure_reason = _join_errors(
                    failure_reason,
                    ("final actual open notional exceeds permit",),
                )
                error_code = "NOTIONAL_LIMIT_REACHED"
            if (
                final_actual_open_notional
                > MAX_ACTUAL_OPEN_NOTIONAL_USDT
            ):
                failure_reason = _join_errors(
                    failure_reason,
                    ("final actual open notional exceeds 12 USDT",),
                )
                error_code = "NOTIONAL_LIMIT_REACHED"

        hard_completion_proved = (
            round_trip_complete
            and finished_halted
            and cleanup_result.safe
            and cleanup_result.final_snapshot is not None
        )
        status = RESULT_BLOCKED
        if hard_completion_proved and not failure_reason:
            status = RESULT_PASSED
            if self._degraded_reasons:
                status = RESULT_DEGRADED
        if status == RESULT_BLOCKED and not error_code:
            error_code = "EXECUTION_BLOCKED"
        if status == RESULT_DEGRADED:
            error_code = self._degraded_error_code()
        passed = status != RESULT_BLOCKED

        if (
            pending_evidence is not None
            and journal_enabled
            and not self._journal_degraded
            and finished_halted
            and cleanup_result.safe
        ):
            recovered = self._commit_recovered_evidence(
                authorization,
                pending_evidence,
            )
            if recovered is not None:
                return recovered

        evidence = self._build_evidence(
            authorization,
            trail,
            status=status,
            passed=passed,
            failure_reason=failure_reason,
            error_code=error_code,
            retryable=retryable,
            finished_halted=finished_halted,
            cleanup_result=cleanup_result,
            actual_open_notional=actual_open_notional,
            open_filled_quantity=open_filled_quantity,
            peak_cumulative_loss=peak_cumulative_loss,
        )
        prepared_evidence, prepared_hash = (
            self._evidence_writer.prepare(evidence)
        )
        self._require_live_operation_lock()
        evidence_can_finalize = (
            journal_enabled
            and not self._journal_degraded
            and finished_halted
            and cleanup_result.safe
        )
        if evidence_can_finalize:
            self._permit_store.prepare_evidence(
                authorization,
                mode=self._mode,
                evidence_path=self._evidence_writer.path,
                evidence=prepared_evidence,
                evidence_sha256=prepared_hash,
                passed=passed,
            )
        replace_recoverable_evidence = (
            evidence_can_finalize
            and recovery_started
            and pending_evidence is None
            and os.path.lexists(self._evidence_writer.path)
        )
        if replace_recoverable_evidence:
            self._validate_recoverable_evidence_for_replacement(
                authorization
            )
        evidence_sha256 = self._evidence_writer.commit_prepared(
            prepared_evidence,
            evidence_sha256=prepared_hash,
            allow_existing=False,
            replace_existing=replace_recoverable_evidence,
        )
        if evidence_can_finalize:
            self._permit_store.commit_evidence(
                authorization,
                mode=self._mode,
                evidence_sha256=evidence_sha256,
            )
        self._require_live_operation_lock()
        return ExecutionResult(
            status=status,
            failure_reason=failure_reason,
            evidence_path=self._evidence_writer.path,
            evidence_sha256=evidence_sha256,
            finished_halted=finished_halted,
            close_submitted=cleanup_result.close_submitted,
            close_quantity=cleanup_result.close_quantity,
            warnings=tuple(self._warnings),
            degraded_reasons=tuple(self._degraded_reasons),
            retryable=retryable,
            error_code=error_code,
            retry_counts=dict(self._retry_counts),
        )

    def _run_live_preflight(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        *,
        phase: str,
        exchange_not_before: datetime | None = None,
    ) -> LivePreflightEvidence | bool:
        preflight_request = self._base_request(authorization)
        preflight_request["phase"] = phase
        preflight_request["quantity"] = _decimal_text(
            authorization.quantity
        )
        preflight_request["limit_price_usdt"] = _decimal_text(
            authorization.limit_price_usdt
        )
        preflight_request["fee_reserve_usdt"] = _decimal_text(
            authorization.fee_reserve_usdt
        )
        if exchange_not_before is not None:
            preflight_request["exchange_not_before"] = (
                exchange_not_before.isoformat()
            )
        preflight_result = self._run_soft_action(
            action="PREFLIGHT",
            trail=trail,
            operation=lambda: _validate_live_preflight(
                self._adapter.preflight(preflight_request),
                authorization,
                now=_utc_now(self._clock),
                phase=phase,
                exchange_not_before=exchange_not_before,
            ),
        )
        if preflight_result is False:
            if phase == "before-open":
                raise ClassifiedExecutionError(
                    (
                        "before-open exchange authority unavailable "
                        "after RESUME"
                    ),
                    code="EXCHANGE_AUTHORITY_UNAVAILABLE",
                )
            trail.record(
                "live_preflight_degraded_continue",
                {
                    "phase": phase,
                    "reason": (
                        "live preflight transport evidence was unavailable"
                    ),
                },
            )
            return False
        for warning in preflight_result.warnings:
            self._add_warning(warning)
        if preflight_result.portfolio_drifted:
            trail.record(
                "non_target_portfolio_drift_observed",
                {
                    "phase": phase,
                    "signed_baseline_sha256": (
                        authorization.portfolio_baseline_sha256
                    ),
                    "observed_baseline_sha256": (
                        preflight_result
                        .observed_portfolio_baseline_sha256
                    ),
                },
            )
        trail.record(
            "live_preflight_confirmed",
            {
                "phase": phase,
                "adapter_evidence_sha256": (
                    preflight_result.evidence_sha256
                ),
                "fetched_at": preflight_result.fetched_at.isoformat(),
                "warning_count": len(preflight_result.warnings),
                "signed_non_target_portfolio_baseline_sha256": (
                    authorization.portfolio_baseline_sha256
                ),
                "observed_non_target_portfolio_baseline_sha256": (
                    preflight_result.observed_portfolio_baseline_sha256
                ),
                "non_target_portfolio_drifted": (
                    preflight_result.portfolio_drifted
                ),
                "quantity": _decimal_text(authorization.quantity),
                "limit_price_usdt": _decimal_text(
                    authorization.limit_price_usdt
                ),
                "fee_reserve_usdt": _decimal_text(
                    authorization.fee_reserve_usdt
                ),
            },
        )
        return preflight_result

    def _dispatch_open(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        *,
        request: Mapping[str, Any],
        enabled: bool,
        journal_fallback_state: str,
        attempt: int,
    ) -> bool:
        self._journal_prepare(
            authorization,
            enabled=enabled,
            action="OPEN",
            request=request,
        )
        try:
            open_ack = self._adapter.submit_open(request)
            open_hash = _validate_ack(
                open_ack,
                authorization,
                action="open",
                expected_client_order_id=(
                    authorization.open_client_order_id
                ),
                expected_side_effect_id=request["side_effect_id"],
            )
        except BaseException as exc:
            if not _is_soft_action_failure(exc):
                raise ClassifiedExecutionError(
                    (
                        "OPEN result is ambiguous after dispatch: "
                        f"{_exception_text(exc)}"
                    ),
                    code="OPEN_RESULT_AMBIGUOUS",
                ) from exc
            error_code = _soft_failure_code(_exception_text(exc))
            if isinstance(exc, SoftActionFailure):
                error_code = exc.code
            self._journal_continue_after_soft_failure(
                authorization,
                enabled=enabled,
                action="OPEN",
                state=journal_fallback_state,
            )
            self._add_degraded(
                "OPEN response unavailable; exchange "
                f"reconciliation required: {error_code}: "
                f"{_exception_text(exc)}"
            )
            trail.record(
                "open_dispatch_degraded_reconcile",
                {
                    "attempt": attempt,
                    "error_code": error_code,
                    "reason": _exception_text(exc),
                    "client_order_id": (
                        authorization.open_client_order_id
                    ),
                    "side_effect_id": request["side_effect_id"],
                },
            )
            return False
        self._journal_complete(
            authorization,
            enabled=enabled,
            action="OPEN",
            state="OPEN_SUBMITTED",
            result={
                "adapter_evidence_sha256": open_hash,
                "client_order_id": (
                    authorization.open_client_order_id
                ),
                "attempt": attempt,
            },
        )
        trail.record(
            "open_submitted",
            {
                "adapter_evidence_sha256": open_hash,
                "attempt": attempt,
                "client_order_id": (
                    authorization.open_client_order_id
                ),
                "requested_notional_usdt": _decimal_text(
                    authorization.requested_open_notional_usdt
                ),
                "side_effect_id": request["side_effect_id"],
            },
        )
        return True

    def _observe_open_after_dispatch(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        *,
        journal_enabled: bool,
        dispatch_acknowledged: bool,
        journal_fallback_state: str,
        exchange_not_before: datetime,
    ) -> TradeObservation | bool:
        soft_failure_state = "OPEN_SUBMITTED"
        if not dispatch_acknowledged:
            soft_failure_state = journal_fallback_state
        try:
            observation = self._wait_for_open_terminal(
                authorization,
                trail,
                journal_enabled=journal_enabled,
                soft_failure_state=soft_failure_state,
                exchange_not_before=exchange_not_before,
            )
        except LiveTradeExecutionError as exc:
            if dispatch_acknowledged:
                raise
            if "did not reach a terminal state" not in str(exc):
                raise
            trail.record(
                "open_exchange_result_unknown",
                {
                    "reason": _exception_text(exc),
                    "source": "terminal-observation-budget",
                },
            )
            return False
        if observation is False and not dispatch_acknowledged:
            trail.record(
                "open_exchange_result_unknown",
                {
                    "reason": (
                        "soft observation recovery was exhausted"
                    ),
                    "source": "soft-observation-budget",
                },
            )
        return observation

    def _wait_for_open_terminal(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        *,
        journal_enabled: bool,
        exchange_not_before: datetime,
        soft_failure_state: str = "OPEN_SUBMITTED",
    ) -> TradeObservation | bool:
        initial_time = _utc_now(self._clock)
        latest = TradeObservation(
            open_status="UNKNOWN",
            exchange_evidence_state="unknown",
            filled_quantity=Decimal(0),
            average_fill_price_usdt=Decimal(0),
            cumulative_net_loss_usdt=Decimal(0),
            observed_at=initial_time,
            mark_at=initial_time,
            loss_monitor_at=initial_time,
            evidence_sha256="0" * 64,
            warnings=(),
        )
        for observation_index in range(1, self._max_observations + 1):
            observe_request = self._base_request(authorization)
            observe_request.update(
                {
                    "open_client_order_id": (
                        authorization.open_client_order_id
                    ),
                    "observation_index": observation_index,
                    "max_cumulative_net_loss_usdt": _decimal_text(
                        authorization.max_cumulative_net_loss_usdt
                    ),
                    "exchange_not_before": (
                        exchange_not_before.isoformat()
                    ),
                }
            )
            self._journal_prepare(
                authorization,
                enabled=journal_enabled,
                action="OBSERVE",
                request=observe_request,
            )
            observed = self._run_soft_action(
                action="OBSERVE",
                trail=trail,
                operation=lambda request=observe_request: _parse_observation(
                    self._adapter.observe(request),
                    authorization,
                    now=_utc_now(self._clock),
                    exchange_not_before=exchange_not_before,
                ),
            )
            if observed is False:
                self._journal_continue_after_soft_failure(
                    authorization,
                    enabled=journal_enabled,
                    action="OBSERVE",
                    state=soft_failure_state,
                )
                trail.record(
                    "observation_degraded_continue_to_close",
                    {
                        "observation_index": observation_index,
                        "open_client_order_id": (
                            authorization.open_client_order_id
                        ),
                    },
                )
                return False
            latest = observed
            for warning in latest.warnings:
                self._add_warning(warning)
            self._journal_complete(
                authorization,
                enabled=journal_enabled,
                action="OBSERVE",
                state="OPEN_OBSERVED",
                result={
                    "adapter_evidence_sha256": (
                        latest.evidence_sha256
                    ),
                    "open_status": latest.open_status,
                    "filled_quantity": _decimal_text(
                        latest.filled_quantity
                    ),
                    "cumulative_net_loss_usdt": _decimal_text(
                        latest.cumulative_net_loss_usdt
                    ),
                    "observed_at": latest.observed_at.isoformat(),
                    "mark_at": latest.mark_at.isoformat(),
                    "loss_monitor_at": (
                        latest.loss_monitor_at.isoformat()
                    ),
                },
            )
            trail.record(
                "trade_observed",
                {
                    "adapter_evidence_sha256": (
                        latest.evidence_sha256
                    ),
                    "open_status": latest.open_status,
                    "filled_quantity": _decimal_text(
                        latest.filled_quantity
                    ),
                    "actual_open_notional_usdt": _decimal_text(
                        latest.actual_open_notional_usdt
                    ),
                    "cumulative_net_loss_usdt": _decimal_text(
                        latest.cumulative_net_loss_usdt
                    ),
                },
            )
            if latest.actual_open_notional_usdt > (
                authorization.max_notional_usdt
            ):
                return latest
            if latest.actual_open_notional_usdt > (
                MAX_ACTUAL_OPEN_NOTIONAL_USDT
            ):
                return latest
            if latest.cumulative_net_loss_usdt >= (
                authorization.max_cumulative_net_loss_usdt
            ):
                return latest
            if latest.open_status in TERMINAL_OPEN_STATUSES:
                return latest
            if observation_index < self._max_observations:
                self._sleeper(self._poll_interval_seconds)
        raise LiveTradeExecutionError(
            "open order did not reach a terminal state"
        )

    def _flatten_target_risk(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        *,
        reason: str,
        journal_enabled: bool,
        exchange_not_before: datetime | None,
        expected_close_quantity: Decimal,
        pending_close_request: Mapping[str, Any] | None = None,
    ) -> CleanupResult:
        cancel_started_at = self._monotonic()
        last_errors: list[str] = []
        close_submitted = False
        close_quantity = Decimal(0)
        final_snapshot: Mapping[str, Any] | None = None
        close_attempted = False
        close_confirmed = False
        exact_pending_close_request: dict[str, Any] | None = None
        quantity_violation = False
        causal_exchange_not_before = exchange_not_before
        if pending_close_request is not None:
            exact_pending_close_request = dict(
                _validated_pending_close_request(
                    pending_close_request,
                    authorization,
                )
            )
            close_attempted = True
            close_quantity = _decimal(
                exact_pending_close_request.get("quantity"),
                "pending CLOSE quantity",
                positive=True,
            )
            raw_exchange_not_before = exact_pending_close_request.get(
                "exchange_not_before"
            )
            if raw_exchange_not_before is not None:
                causal_exchange_not_before = _timestamp(
                    raw_exchange_not_before,
                    "pending CLOSE exchange_not_before",
                )

        cancel_attempts: Sequence[int] = range(
            1,
            self._recovery_max_attempts + 1,
        )
        if exact_pending_close_request is not None:
            cancel_attempts = ()
            trail.record(
                "pending_close_recovery_queries_position_first",
                {
                    "client_order_id": (
                        authorization.close_client_order_id
                    ),
                    "side_effect_id": (
                        exact_pending_close_request[
                            "side_effect_id"
                        ]
                    ),
                    "quantity": _decimal_text(close_quantity),
                },
            )
        for attempt in cancel_attempts:
            if not self._recovery_budget_available(
                cancel_started_at
            ):
                break
            cancel_request = self._base_request(authorization)
            cancel_request.update(
                {
                    "open_client_order_id": (
                        authorization.open_client_order_id
                    ),
                    "reason": reason,
                    "attempt": attempt,
                    "side_effect_id": deterministic_side_effect_id(
                        authorization,
                        "CANCEL_OPEN",
                    ),
                    "hard_timeout_seconds": (
                        self._recovery_seconds_remaining(
                            cancel_started_at
                        )
                    ),
                }
            )
            cancel_journal_prepared = (
                self._recovery_journal_prepare(
                    authorization,
                    trail,
                    enabled=journal_enabled,
                    action="CANCEL_OPEN",
                    request=cancel_request,
                )
            )
            if not self._refresh_hard_timeout(
                cancel_request,
                cancel_started_at,
            ):
                break
            try:
                cancel_ack = self._adapter.cancel_open(cancel_request)
                cancel_hash = _validate_ack(
                    cancel_ack,
                    authorization,
                    action="cancel-open",
                    expected_client_order_id=(
                        authorization.open_client_order_id
                    ),
                    expected_side_effect_id=cancel_request[
                        "side_effect_id"
                    ],
                )
                self._recovery_journal_complete(
                    authorization,
                    trail,
                    enabled=journal_enabled,
                    prepared=cancel_journal_prepared,
                    action="CANCEL_OPEN",
                    state=None,
                    result={
                        "adapter_evidence_sha256": cancel_hash,
                        "attempt": attempt,
                    },
                )
                trail.record(
                    "open_cancel_confirmed",
                    {
                        "adapter_evidence_sha256": cancel_hash,
                        "attempt": attempt,
                        "side_effect_id": cancel_request[
                            "side_effect_id"
                        ],
                    },
                )
                break
            except BaseException as exc:  # noqa: BLE001
                cancel_error = (
                    f"cancel open attempt {attempt} failed: "
                    f"{_exception_text(exc)}"
                )
                last_errors = [cancel_error]
                trail.record(
                    "open_cancel_failed",
                    {
                        "reason": cancel_error,
                        "attempt": attempt,
                        "side_effect_id": cancel_request[
                            "side_effect_id"
                        ],
                    },
                )
                if _is_soft_action_failure(exc):
                    self._add_degraded(
                        (
                            "CANCEL_OPEN transport failure recovered "
                            f"through exchange proof: {_exception_text(exc)}"
                        )
                    )
                self._recovery_sleep(cancel_started_at)

        close_started_at = self._monotonic()
        flat_confirmed = False
        for attempt in range(1, self._recovery_max_attempts + 1):
            if not self._recovery_budget_available(close_started_at):
                break
            position_request = self._base_request(authorization)
            position_request.update(
                {
                    "reason": reason,
                    "attempt": attempt,
                    "hard_timeout_seconds": (
                        self._recovery_seconds_remaining(
                            close_started_at
                        )
                    ),
                }
            )
            if causal_exchange_not_before is not None:
                position_request["exchange_not_before"] = (
                    causal_exchange_not_before.isoformat()
                )
            position_journal_prepared = (
                self._recovery_journal_prepare(
                    authorization,
                    trail,
                    enabled=(
                        journal_enabled
                        and exact_pending_close_request is None
                    ),
                    action="QUERY_POSITION",
                    request=position_request,
                )
            )
            if not self._refresh_hard_timeout(
                position_request,
                close_started_at,
            ):
                break
            try:
                raw_position = self._adapter.current_position(
                    position_request
                )
                position = _parse_position(
                    raw_position,
                    authorization,
                    now=_utc_now(self._clock),
                    exchange_not_before=causal_exchange_not_before,
                )
                self._recovery_journal_complete(
                    authorization,
                    trail,
                    enabled=journal_enabled,
                    prepared=position_journal_prepared,
                    action="QUERY_POSITION",
                    state=None,
                    result={
                        "adapter_evidence_sha256": (
                            position.evidence_sha256
                        ),
                        "position_side": position.side,
                        "position_quantity": _decimal_text(
                            position.quantity
                        ),
                        "source": EXCHANGE_EVIDENCE_SOURCE,
                        "fetched_at": position.fetched_at.isoformat(),
                        "attempt": attempt,
                    },
                )
                trail.record(
                    "position_observed",
                    {
                        "adapter_evidence_sha256": (
                            position.evidence_sha256
                        ),
                        "position_side": position.side,
                        "position_quantity": _decimal_text(
                            position.quantity
                        ),
                        "source": EXCHANGE_EVIDENCE_SOURCE,
                        "fetched_at": position.fetched_at.isoformat(),
                        "attempt": attempt,
                    },
                )
            except BaseException as exc:  # noqa: BLE001
                position_error = (
                    f"position query attempt {attempt} failed: "
                    f"{_exception_text(exc)}"
                )
                last_errors = [position_error]
                trail.record(
                    "position_query_failed",
                    {
                        "reason": position_error,
                        "attempt": attempt,
                    },
                )
                if _is_soft_action_failure(exc):
                    self._add_degraded(
                        (
                            "CURRENT_POSITION transport failure "
                            f"required retry: {_exception_text(exc)}"
                        )
                    )
                self._recovery_sleep(close_started_at)
                continue

            if position.quantity == 0:
                flat_confirmed = True
                last_errors = []
                if close_attempted and not close_confirmed:
                    close_side_effect_id = ""
                    if exact_pending_close_request is not None:
                        close_side_effect_id = str(
                            exact_pending_close_request.get(
                                "side_effect_id",
                                "",
                            )
                        )
                    close_submitted = True
                    self._add_degraded(
                        "CLOSE_ACK_MISSING_RECOVERED_FROM_EXCHANGE_POSITION"
                    )
                    self._recovery_journal_mark_state(
                        authorization,
                        trail,
                        enabled=journal_enabled,
                        state="CLOSE_CONFIRMED",
                        event_type="CLOSE_EFFECT_RECONCILED_FLAT",
                        payload={
                            "quantity": _decimal_text(close_quantity),
                            "attempt": attempt,
                            "side_effect_id": close_side_effect_id,
                        },
                    )
                    trail.record(
                        "reduce_only_close_effect_reconciled_flat",
                        {
                            "client_order_id": (
                                authorization.close_client_order_id
                            ),
                            "quantity": _decimal_text(close_quantity),
                            "reduce_only": True,
                            "attempt": attempt,
                            "side_effect_id": close_side_effect_id,
                        },
                    )
                break

            if close_attempted and not close_confirmed:
                if exact_pending_close_request is None:
                    last_errors = [
                        (
                            "exact close request is unavailable for "
                            "reconciliation"
                        )
                    ]
                    break
                reconcile_request = dict(
                    exact_pending_close_request
                )
                try:
                    close_ack = self._adapter.submit_close(
                        reconcile_request
                    )
                    close_hash, close_warnings = (
                        _validate_exact_close_ack(
                            close_ack,
                            authorization,
                            expected_quantity=close_quantity,
                            expected_side_effect_id=reconcile_request[
                                "side_effect_id"
                            ],
                        )
                    )
                    for warning in close_warnings:
                        self._add_degraded(
                            "CLOSE_ENRICHMENT_DEGRADED: "
                            f"{warning}"
                        )
                    close_confirmed = True
                    close_submitted = True
                    last_errors = []
                    self._recovery_journal_mark_state(
                        authorization,
                        trail,
                        enabled=journal_enabled,
                        state="CLOSE_CONFIRMED",
                        event_type="CLOSE_RECONCILED_EXACT_FILL",
                        payload={
                            "adapter_evidence_sha256": close_hash,
                            "quantity": _decimal_text(
                                close_quantity
                            ),
                            "attempt": attempt,
                            "reconciled": True,
                            "enrichment_warnings": list(
                                close_warnings
                            ),
                        },
                    )
                    trail.record(
                        "reduce_only_close_reconciled",
                        {
                            "adapter_evidence_sha256": close_hash,
                            "client_order_id": (
                                authorization.close_client_order_id
                            ),
                            "quantity": _decimal_text(
                                close_quantity
                            ),
                            "reduce_only": True,
                            "attempt": attempt,
                            "side_effect_id": reconcile_request[
                                "side_effect_id"
                            ],
                            "enrichment_warnings": list(
                                close_warnings
                            ),
                        },
                    )
                except BaseException as exc:  # noqa: BLE001
                    close_error = (
                        "exact close reconciliation attempt "
                        f"{attempt} failed: {_exception_text(exc)}"
                    )
                    last_errors = [close_error]
                    trail.record(
                        "reduce_only_close_reconciliation_failed",
                        {
                            "reason": close_error,
                            "quantity": _decimal_text(
                                close_quantity
                            ),
                            "attempt": attempt,
                            "side_effect_id": reconcile_request[
                                "side_effect_id"
                            ],
                        },
                    )
                    if _is_soft_action_failure(exc):
                        self._add_degraded(
                            (
                                "CLOSE transport failure required "
                                "idempotent exact-fill reconciliation: "
                                f"{_exception_text(exc)}"
                            )
                        )
                self._recovery_sleep(close_started_at)
                continue

            if close_confirmed:
                last_errors = [
                    (
                        "target position remains after the authorized "
                        "close quantity was filled"
                    )
                ]
                break
            close_quantity = min(
                position.quantity,
                expected_close_quantity,
            )
            close_attempted = True
            if position.quantity > expected_close_quantity:
                quantity_violation = True
                trail.record(
                    "position_quantity_exceeded",
                    {
                        "authorized_close_quantity": _decimal_text(
                            expected_close_quantity
                        ),
                        "position_quantity": _decimal_text(
                            position.quantity
                        ),
                    },
                )
            close_request = self._base_request(authorization)
            close_dispatched_at = _utc_now(self._clock)
            causal_exchange_not_before = close_dispatched_at
            close_request.update(
                {
                    "intent_id": authorization.close_intent_id,
                    "open_intent_id": authorization.intent_id,
                    "client_order_id": (
                        authorization.close_client_order_id
                    ),
                    "side": _close_side(position.side),
                    "position_side": position.side,
                    "order_type": "MARKET",
                    "quantity": _decimal_text(close_quantity),
                    "reduce_only": True,
                    "reason": reason,
                    "attempt": attempt,
                    "side_effect_id": deterministic_side_effect_id(
                        authorization,
                        "CLOSE",
                    ),
                    "hard_timeout_seconds": (
                        self._recovery_seconds_remaining(
                            close_started_at
                        )
                    ),
                    "exchange_not_before": (
                        close_dispatched_at.isoformat()
                    ),
                }
            )
            close_journal_prepared = (
                self._recovery_journal_prepare(
                    authorization,
                    trail,
                    enabled=journal_enabled,
                    action="CLOSE",
                    request=close_request,
                    state="CLOSE_PENDING",
                )
            )
            exact_pending_close_request = dict(close_request)
            if not self._refresh_hard_timeout(
                close_request,
                close_started_at,
            ):
                break
            try:
                close_ack = self._adapter.submit_close(close_request)
                close_hash, close_warnings = _validate_exact_close_ack(
                    close_ack,
                    authorization,
                    expected_quantity=close_quantity,
                    expected_side_effect_id=close_request["side_effect_id"],
                )
                for warning in close_warnings:
                    self._add_degraded(
                        f"CLOSE_ENRICHMENT_DEGRADED: {warning}"
                    )
                close_confirmed = True
                close_submitted = True
                self._recovery_journal_complete(
                    authorization,
                    trail,
                    enabled=journal_enabled,
                    prepared=close_journal_prepared,
                    action="CLOSE",
                    state="CLOSE_CONFIRMED",
                    result={
                        "adapter_evidence_sha256": close_hash,
                        "quantity": _decimal_text(close_quantity),
                        "attempt": attempt,
                        "enrichment_warnings": list(close_warnings),
                    },
                )
                trail.record(
                    "reduce_only_close_confirmed",
                    {
                        "adapter_evidence_sha256": close_hash,
                        "client_order_id": (
                            authorization.close_client_order_id
                        ),
                        "side": _close_side(position.side),
                        "quantity": _decimal_text(close_quantity),
                        "reduce_only": True,
                        "attempt": attempt,
                        "side_effect_id": close_request[
                            "side_effect_id"
                        ],
                        "enrichment_warnings": list(close_warnings),
                    },
            )
            except BaseException as exc:  # noqa: BLE001
                close_error = (
                    f"reduce-only close attempt {attempt} failed: "
                    f"{_exception_text(exc)}"
                )
                last_errors = [close_error]
                trail.record(
                    "reduce_only_close_failed",
                    {
                        "reason": close_error,
                        "quantity": _decimal_text(close_quantity),
                        "attempt": attempt,
                        "side_effect_id": close_request[
                            "side_effect_id"
                        ],
                    },
                )
                if _is_soft_action_failure(exc):
                    self._add_degraded(
                        (
                            "CLOSE transport failure required exact "
                            f"exchange reconciliation: {_exception_text(exc)}"
                        )
                    )
            self._recovery_sleep(close_started_at)

        verification_started_at = self._monotonic()
        if flat_confirmed:
            for attempt in range(
                1,
                self._recovery_max_attempts + 1,
            ):
                if not self._recovery_budget_available(
                    verification_started_at
                ):
                    break
                final_request = self._base_request(authorization)
                final_request.update(
                    {
                        "reason": reason,
                        "phase": "pre-halt-close-proof",
                        "attempt": attempt,
                        "quantity": _decimal_text(
                            authorization.quantity
                        ),
                        "open_side": authorization.open_side,
                        "limit_price_usdt": _decimal_text(
                            authorization.limit_price_usdt
                        ),
                        "hard_timeout_seconds": (
                            self._recovery_seconds_remaining(
                                verification_started_at
                            )
                        ),
                    }
                )
                if causal_exchange_not_before is not None:
                    final_request["exchange_not_before"] = (
                        causal_exchange_not_before.isoformat()
                    )
                final_journal_prepared = (
                    self._recovery_journal_prepare(
                        authorization,
                        trail,
                        enabled=journal_enabled,
                        action="FINAL_SNAPSHOT",
                        request=final_request,
                    )
                )
                if not self._refresh_hard_timeout(
                    final_request,
                    verification_started_at,
                ):
                    break
                try:
                    raw_final = self._adapter.final_snapshot(
                        final_request
                    )
                    final_snapshot = _validate_final_snapshot(
                        raw_final,
                        authorization,
                        now=_utc_now(self._clock),
                        exchange_not_before=(
                            causal_exchange_not_before
                        ),
                    )
                    self._record_final_snapshot_degradation(
                        final_snapshot,
                        trail,
                        phase="pre-halt-close-proof",
                    )
                    self._recovery_journal_complete(
                        authorization,
                        trail,
                        enabled=journal_enabled,
                        prepared=final_journal_prepared,
                        action="FINAL_SNAPSHOT",
                        state=None,
                        result={
                            "adapter_evidence_sha256": (
                                final_snapshot["evidence_sha256"]
                            ),
                            "source": EXCHANGE_EVIDENCE_SOURCE,
                            "fetched_at": final_snapshot["fetched_at"],
                            "attempt": attempt,
                        },
                    )
                    trail.record(
                        "final_safety_snapshot_confirmed",
                        {
                            "adapter_evidence_sha256": (
                                final_snapshot["evidence_sha256"]
                            ),
                            "target_symbol_flat": True,
                            "target_symbol_regular_orders_zero": True,
                            "target_symbol_algo_orders_zero": True,
                            "non_target_portfolio_baseline_sha256": (
                                final_snapshot[
                                    "non_target_portfolio_baseline_sha256"
                                ]
                            ),
                            "non_target_portfolio_drifted": (
                                final_snapshot[
                                    "non_target_portfolio_drifted"
                                ]
                            ),
                            "source": EXCHANGE_EVIDENCE_SOURCE,
                            "fetched_at": final_snapshot["fetched_at"],
                            "attempt": attempt,
                        },
                    )
                    last_errors = []
                    break
                except BaseException as exc:  # noqa: BLE001
                    final_error = (
                        f"final snapshot attempt {attempt} failed: "
                        f"{_exception_text(exc)}"
                    )
                    last_errors = [final_error]
                    trail.record(
                        "final_safety_snapshot_failed",
                        {
                            "reason": final_error,
                            "attempt": attempt,
                        },
                    )
                    if _is_soft_action_failure(exc):
                        self._add_degraded(
                            (
                                "PRE_HALT_SNAPSHOT transport failure "
                                f"required retry: {_exception_text(exc)}"
                            )
                        )
                    self._recovery_sleep(verification_started_at)

        if not flat_confirmed:
            last_errors.append(
                "recovery could not prove target position flat"
            )
        if final_snapshot is None:
            last_errors.append(
                "recovery could not prove final safety snapshot"
            )
        if quantity_violation:
            last_errors.append(
                "target position exceeded authorized close quantity"
            )
        safe = (
            flat_confirmed
            and final_snapshot is not None
            and not last_errors
        )
        return CleanupResult(
            safe=safe,
            close_submitted=close_submitted,
            close_quantity=close_quantity,
            pre_halt_snapshot=final_snapshot,
            final_snapshot=None,
            errors=tuple(last_errors),
        )

    def _halt_with_recovery(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        *,
        reason: str,
        journal_enabled: bool,
    ) -> tuple[bool, str, datetime | None]:
        halt_started_at = self._monotonic()
        last_error = "HALT was not attempted"
        for attempt in range(1, self._recovery_max_attempts + 1):
            if not self._recovery_budget_available(
                halt_started_at
            ):
                break
            halt_request = self._base_request(authorization)
            halt_request.update(
                {
                    "command": "HALT",
                    "reason": reason,
                    "attempt": attempt,
                    "side_effect_id": deterministic_side_effect_id(
                        authorization,
                        "HALT",
                    ),
                    "hard_timeout_seconds": (
                        self._recovery_seconds_remaining(
                            halt_started_at
                        )
                    ),
                }
            )
            halt_journal_prepared = (
                self._recovery_journal_prepare(
                    authorization,
                    trail,
                    enabled=journal_enabled,
                    action="HALT",
                    request=halt_request,
                )
            )
            if not self._refresh_hard_timeout(
                halt_request,
                halt_started_at,
            ):
                break
            try:
                halt_ack = self._adapter.halt(halt_request)
                (
                    halt_hash,
                    halt_observed_at,
                    halt_warnings,
                ) = _validate_halt_ack(
                    halt_ack,
                    authorization,
                    now=_utc_now(self._clock),
                    expected_side_effect_id=halt_request[
                        "side_effect_id"
                    ],
                )
                for warning in halt_warnings:
                    self._add_warning(warning)
                self._recovery_journal_complete(
                    authorization,
                    trail,
                    enabled=journal_enabled,
                    prepared=halt_journal_prepared,
                    action="HALT",
                    state="HALTED",
                    result={
                        "adapter_evidence_sha256": halt_hash,
                        "source": NODE_STATE_EVIDENCE_SOURCE,
                        "trading_state": "HALTED",
                        "observed_at": halt_observed_at.isoformat(),
                        "attempt": attempt,
                        "side_effect_id": halt_request[
                            "side_effect_id"
                        ],
                    },
                )
                trail.record(
                    "halt_accepted",
                    {
                        "adapter_evidence_sha256": halt_hash,
                        "source": NODE_STATE_EVIDENCE_SOURCE,
                        "trading_state": "HALTED",
                        "observed_at": halt_observed_at.isoformat(),
                        "attempt": attempt,
                    },
                )
                return True, "", halt_observed_at
            except BaseException as exc:  # noqa: BLE001
                last_error = (
                    f"HALT attempt {attempt} failed: "
                    f"{_exception_text(exc)}"
                )
                trail.record(
                    "halt_failed",
                    {
                        "reason": last_error,
                        "attempt": attempt,
                        "side_effect_id": halt_request[
                            "side_effect_id"
                        ],
                    },
                )
                if _is_soft_action_failure(exc):
                    self._add_degraded(
                        (
                            "HALT transport failure required retry: "
                            f"{_exception_text(exc)}"
                        )
                    )
                self._recovery_sleep(halt_started_at)
        error = (
            f"{last_error}; unable to prove HALTED within recovery budget"
        )
        return False, error, None

    def _fetch_post_halt_snapshot(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        *,
        halt_observed_at: datetime,
        journal_enabled: bool,
    ) -> tuple[Mapping[str, Any] | bool, str]:
        started_at = self._monotonic()
        last_error = "post-HALT final snapshot was not attempted"
        for attempt in range(1, self._recovery_max_attempts + 1):
            if not self._recovery_budget_available(started_at):
                break
            request = self._base_request(authorization)
            request.update(
                {
                    "reason": "post-halt-final-proof",
                    "phase": "post-halt-final",
                    "attempt": attempt,
                    "quantity": _decimal_text(
                        authorization.quantity
                    ),
                    "open_side": authorization.open_side,
                    "limit_price_usdt": _decimal_text(
                        authorization.limit_price_usdt
                    ),
                    "halt_observed_at": halt_observed_at.isoformat(),
                    "exchange_not_before": (
                        halt_observed_at.isoformat()
                    ),
                    "hard_timeout_seconds": (
                        self._recovery_seconds_remaining(started_at)
                    ),
                }
            )
            prepared = self._recovery_journal_prepare(
                authorization,
                trail,
                enabled=journal_enabled,
                action="POST_HALT_FINAL_SNAPSHOT",
                request=request,
            )
            if not self._refresh_hard_timeout(request, started_at):
                break
            try:
                raw_snapshot = self._adapter.final_snapshot(request)
                snapshot = _validate_final_snapshot(
                    raw_snapshot,
                    authorization,
                    now=_utc_now(self._clock),
                    exchange_not_before=halt_observed_at,
                )
                self._record_final_snapshot_degradation(
                    snapshot,
                    trail,
                    phase="post-halt-final",
                )
                self._recovery_journal_complete(
                    authorization,
                    trail,
                    enabled=journal_enabled,
                    prepared=prepared,
                    action="POST_HALT_FINAL_SNAPSHOT",
                    state=None,
                    result={
                        "adapter_evidence_sha256": snapshot[
                            "evidence_sha256"
                        ],
                        "source": EXCHANGE_EVIDENCE_SOURCE,
                        "fetched_at": snapshot["fetched_at"],
                        "halt_observed_at": (
                            halt_observed_at.isoformat()
                        ),
                        "non_target_portfolio_baseline_sha256": (
                            snapshot[
                                "non_target_portfolio_baseline_sha256"
                            ]
                        ),
                        "non_target_portfolio_drifted": snapshot[
                            "non_target_portfolio_drifted"
                        ],
                        "attempt": attempt,
                    },
                )
                trail.record(
                    "post_halt_final_snapshot_confirmed",
                    {
                        "adapter_evidence_sha256": snapshot[
                            "evidence_sha256"
                        ],
                        "source": EXCHANGE_EVIDENCE_SOURCE,
                        "fetched_at": snapshot["fetched_at"],
                        "halt_observed_at": (
                            halt_observed_at.isoformat()
                        ),
                        "attempt": attempt,
                    },
                )
                return snapshot, ""
            except BaseException as exc:  # noqa: BLE001
                last_error = (
                    f"post-HALT final snapshot attempt {attempt} failed: "
                    f"{_exception_text(exc)}"
                )
                trail.record(
                    "post_halt_final_snapshot_failed",
                    {
                        "reason": last_error,
                        "attempt": attempt,
                    },
                )
                if _is_soft_action_failure(exc):
                    self._add_degraded(
                        (
                            "POST_HALT_SNAPSHOT transport failure "
                            f"required retry: {_exception_text(exc)}"
                        )
                    )
                self._recovery_sleep(started_at)
        error = (
            f"{last_error}; unable to prove fresh post-HALT exchange state"
        )
        return False, error

    def _journal_prepare(
        self,
        authorization: CanaryAuthorization,
        *,
        enabled: bool,
        action: str,
        request: Mapping[str, Any],
        state: str | None = None,
    ) -> None:
        if not enabled:
            return
        self._require_live_operation_lock()
        try:
            self._run_journal_write(
                lambda: self._permit_store.prepare_action(
                    authorization,
                    mode=self._mode,
                    action=action,
                    payload=request,
                    state=state,
                )
            )
        except BaseException as exc:
            failure = self._record_journal_failure(
                phase="prepare",
                action=action,
                exc=exc,
            )
            raise LiveTradeExecutionError(failure) from exc

    def _journal_complete(
        self,
        authorization: CanaryAuthorization,
        *,
        enabled: bool,
        action: str,
        state: str | None,
        result: Mapping[str, Any],
    ) -> None:
        if not enabled:
            return
        try:
            self._run_journal_write(
                lambda: self._permit_store.complete_action(
                    authorization,
                    mode=self._mode,
                    action=action,
                    state=state,
                    result=result,
                )
            )
        except BaseException as exc:
            failure = self._record_journal_failure(
                phase="complete",
                action=action,
                exc=exc,
            )
            raise LiveTradeExecutionError(failure) from exc
        self._require_live_operation_lock()

    def _journal_mark_state(
        self,
        authorization: CanaryAuthorization,
        *,
        enabled: bool,
        state: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        if not enabled:
            return
        try:
            self._run_journal_write(
                lambda: self._permit_store.mark_state(
                    authorization,
                    mode=self._mode,
                    state=state,
                    event_type=event_type,
                    payload=payload,
                )
            )
        except BaseException as exc:
            failure = self._record_journal_failure(
                phase="mark",
                action=event_type,
                exc=exc,
            )
            raise LiveTradeExecutionError(failure) from exc

    def _run_journal_write(
        self,
        operation: Callable[[], None],
    ) -> None:
        if self._mode != "live":
            operation()
            return
        self._require_live_operation_lock()
        completed = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                operation()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(
            target=run,
            name="account-a-live-journal-write",
            daemon=True,
        )
        worker.start()
        completed_in_time = completed.wait(
            self._journal_write_timeout_seconds
        )
        if not completed_in_time:
            raise TimeoutError(
                "journal write exceeded live safety timeout "
                f"of {self._journal_write_timeout_seconds:g}s"
            )
        if errors:
            raise errors[0]

    def _recovery_journal_prepare(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        *,
        enabled: bool,
        action: str,
        request: Mapping[str, Any],
        state: str | None = None,
    ) -> bool:
        if not enabled:
            return False
        if self._journal_degraded:
            self._record_journal_bypass(
                trail,
                phase="prepare",
                action=action,
            )
            return False
        try:
            self._journal_prepare(
                authorization,
                enabled=True,
                action=action,
                request=request,
                state=state,
            )
        except BaseException:  # noqa: BLE001
            self._record_journal_bypass(
                trail,
                phase="prepare",
                action=action,
            )
            return False
        return True

    def _recovery_journal_complete(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        *,
        enabled: bool,
        prepared: bool,
        action: str,
        state: str | None,
        result: Mapping[str, Any],
    ) -> None:
        if not enabled:
            return
        if not prepared or self._journal_degraded:
            trail.record(
                "permit_journal_outcome_retained_in_memory",
                {
                    "phase": "complete",
                    "action": action,
                    "result": dict(result),
                },
            )
            return
        try:
            self._journal_complete(
                authorization,
                enabled=True,
                action=action,
                state=state,
                result=result,
            )
        except BaseException:  # noqa: BLE001
            self._record_journal_bypass(
                trail,
                phase="complete",
                action=action,
            )

    def _recovery_journal_mark_state(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        *,
        enabled: bool,
        state: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        if not enabled:
            return
        if self._journal_degraded:
            trail.record(
                "permit_journal_outcome_retained_in_memory",
                {
                    "phase": "mark",
                    "action": event_type,
                    "state": state,
                    "payload": dict(payload),
                },
            )
            return
        try:
            self._journal_mark_state(
                authorization,
                enabled=True,
                state=state,
                event_type=event_type,
                payload=payload,
            )
        except BaseException:  # noqa: BLE001
            self._record_journal_bypass(
                trail,
                phase="mark",
                action=event_type,
            )

    def _record_journal_failure(
        self,
        *,
        phase: str,
        action: str,
        exc: BaseException,
    ) -> str:
        failure = (
            f"permit journal {phase} failed for {action}: "
            f"{_exception_text(exc)}"
        )
        self._journal_degraded = True
        if failure not in self._journal_failures:
            self._journal_failures.append(failure)
        return failure

    def _record_journal_bypass(
        self,
        trail: AuditTrail,
        *,
        phase: str,
        action: str,
    ) -> None:
        failure = ""
        if self._journal_failures:
            failure = self._journal_failures[-1]
        trail.record(
            "permit_journal_bypassed_for_safety",
            {
                "phase": phase,
                "action": action,
                "journal_failure": failure,
            },
        )

    def _run_soft_action(
        self,
        *,
        action: str,
        trail: AuditTrail,
        operation: Callable[[], Any],
    ) -> Any:
        started_at = self._monotonic()
        failures = 0
        last_error = ""
        last_code = "SOFT_TRANSPORT_FAILURE"
        for attempt in range(1, self._soft_action_max_attempts + 1):
            try:
                result = operation()
            except BaseException as exc:
                if not _is_soft_action_failure(exc):
                    raise
                failures += 1
                last_error = _exception_text(exc)
                if isinstance(exc, SoftActionFailure):
                    last_code = exc.code
                else:
                    last_code = _soft_failure_code(last_error)
                self._retry_counts[action] = failures
                trail.record(
                    "soft_action_retry",
                    {
                        "action": action,
                        "attempt": attempt,
                        "error_code": last_code,
                        "reason": last_error,
                    },
                )
                elapsed = self._monotonic() - started_at
                budget_exhausted = (
                    elapsed >= self._soft_action_deadline_seconds
                )
                attempts_exhausted = (
                    attempt >= self._soft_action_max_attempts
                )
                if budget_exhausted or attempts_exhausted:
                    break
                self._sleeper(self._recovery_retry_interval_seconds)
                continue
            if failures:
                retry_word = "retries"
                if failures == 1:
                    retry_word = "retry"
                self._add_degraded(
                    (
                        f"{action} recovered after {failures} "
                        f"{retry_word}: {last_code}: {last_error}"
                    )
                )
            return result
        self._add_degraded(
            (
                f"{action} soft failure budget exhausted after "
                f"{failures} attempts: {last_code}: {last_error}"
            )
        )
        return False

    def _journal_continue_after_soft_failure(
        self,
        authorization: CanaryAuthorization,
        *,
        enabled: bool,
        action: str,
        state: str,
    ) -> None:
        if not enabled:
            return
        self._require_live_operation_lock()
        self._run_journal_write(
            lambda: self._permit_store.mark_state(
                authorization,
                mode=self._mode,
                state=state,
                event_type=f"{action}_DEGRADED_CONTINUE",
                payload={
                    "action": action,
                    "retry_count": self._retry_counts.get(action, 0),
                },
            )
        )

    def _add_warning(self, warning: str) -> None:
        normalized = _required_text(warning, "warning")
        if normalized not in self._warnings:
            self._warnings.append(normalized)
        self._add_degraded(f"soft gate: {normalized}")

    def _record_final_snapshot_degradation(
        self,
        snapshot: Mapping[str, Any],
        trail: AuditTrail,
        *,
        phase: str,
    ) -> None:
        if snapshot.get("non_target_portfolio_drifted") is True:
            signed_baseline = _required_sha256(
                snapshot.get(
                    "signed_non_target_portfolio_baseline_sha256"
                ),
                "signed non-target portfolio baseline",
            )
            observed_baseline = _required_sha256(
                snapshot.get("non_target_portfolio_baseline_sha256"),
                "final non-target portfolio baseline",
            )
            warning = _non_target_portfolio_drift_warning(
                expected=signed_baseline,
                observed=observed_baseline,
            )
            self._add_warning(warning)
            trail.record(
                "non_target_portfolio_drift_observed",
                {
                    "phase": phase,
                    "signed_baseline_sha256": signed_baseline,
                    "observed_baseline_sha256": observed_baseline,
                },
            )
        if snapshot.get("financial_proof_complete") is not False:
            return
        warnings = snapshot.get("warnings")
        warning_text = "financial enrichment unavailable"
        if isinstance(warnings, list) and warnings:
            warning_text = "; ".join(str(item) for item in warnings)
        self._add_degraded(
            f"FINANCIAL_PROOF_DEGRADED: {phase}: {warning_text}"
        )
        trail.record(
            "final_financial_proof_degraded",
            {
                "phase": phase,
                "reason": warning_text,
            },
        )

    def _add_degraded(self, reason: str) -> None:
        normalized = _required_text(reason, "degraded reason")
        if normalized in self._degraded_reasons:
            return
        self._degraded_reasons.append(normalized)

    def _degraded_error_code(self) -> str:
        for reason in self._degraded_reasons:
            if "DEGRADED_NO_FILL" in reason:
                return "DEGRADED_NO_FILL"
        if self._retry_counts:
            return "SOFT_TRANSPORT_DEGRADED"
        for reason in self._degraded_reasons:
            if re.search(
                r"transport|timeout|5\d\d|circuit|queue",
                reason,
                re.IGNORECASE,
            ):
                return "SOFT_TRANSPORT_DEGRADED"
        return "SOFT_GATE_DEGRADED"

    def _require_live_operation_lock(self) -> None:
        if self._mode != "live":
            return
        operation_lock = self._operation_lock
        if operation_lock is None:
            raise LiveTradeExecutionError(
                "live executor requires an operation lock"
            )
        operation_lock.require_held()

    def _recovery_gate_required_result(
        self,
        *,
        disposition: str,
    ) -> ExecutionResult:
        return ExecutionResult(
            status=RESULT_BLOCKED,
            failure_reason=(
                "signed evidence recovery gate is required for "
                f"{disposition}"
            ),
            evidence_path=self._evidence_writer.path,
            evidence_sha256="",
            finished_halted=True,
            close_submitted=False,
            close_quantity=Decimal(0),
            warnings=tuple(self._warnings),
            degraded_reasons=tuple(self._degraded_reasons),
            retryable=False,
            error_code="RECOVERY_GATE_REQUIRED",
            retry_counts=dict(self._retry_counts),
        )

    def _recovery_budget_available(self, started_at: float) -> bool:
        return self._recovery_seconds_remaining(started_at) > 0

    def _recovery_seconds_remaining(
        self,
        started_at: float,
    ) -> float:
        elapsed = self._monotonic() - started_at
        remaining = self._recovery_deadline_seconds - elapsed
        return max(0.0, remaining)

    def _refresh_hard_timeout(
        self,
        request: dict[str, Any],
        started_at: float,
    ) -> bool:
        remaining = self._recovery_seconds_remaining(started_at)
        if remaining <= 0:
            return False
        request["hard_timeout_seconds"] = remaining
        return True

    def _recovery_sleep(self, started_at: float) -> None:
        if not self._recovery_budget_available(started_at):
            return
        self._sleeper(self._recovery_retry_interval_seconds)

    def _commit_recovered_evidence(
        self,
        authorization: CanaryAuthorization,
        pending: Mapping[str, Any],
    ) -> ExecutionResult | None:
        pending_path = str(pending.get("path") or "")
        if pending_path != str(self._evidence_writer.path):
            return None
        payload = pending.get("payload")
        if not isinstance(payload, dict):
            raise LiveTradeExecutionError(
                "prepared recovery evidence payload is invalid"
            )
        _validate_evidence_payload_identity(
            payload,
            authorization,
            mode=self._mode,
        )
        pending_hash = _required_sha256(
            pending.get("sha256"),
            "prepared recovery evidence sha256",
        )
        self._require_live_operation_lock()
        replace_existing = False
        if os.path.lexists(self._evidence_writer.path):
            existing = _read_protected_file(
                self._evidence_writer.path,
                "existing recovery evidence",
                live=self._mode == "live",
            )
            if _sha256_bytes(existing) != pending_hash:
                enrichment_of_sha256 = pending.get(
                    "enrichment_of_sha256"
                )
                if enrichment_of_sha256:
                    self._validate_enrichment_evidence_for_replacement(
                        authorization,
                        expected_sha256=_required_sha256(
                            enrichment_of_sha256,
                            "enrichment source evidence sha256",
                        ),
                    )
                else:
                    self._validate_recoverable_evidence_for_replacement(
                        authorization
                    )
                replace_existing = True
        evidence_sha256 = self._evidence_writer.commit_prepared(
            payload,
            evidence_sha256=pending_hash,
            allow_existing=True,
            replace_existing=replace_existing,
        )
        self._permit_store.commit_evidence(
            authorization,
            mode=self._mode,
            evidence_sha256=evidence_sha256,
        )
        return self._execution_result_from_evidence(
            payload,
            evidence_sha256=evidence_sha256,
        )

    def _read_committed_evidence_result(
        self,
        authorization: CanaryAuthorization,
        record: Mapping[str, Any] | None,
    ) -> ExecutionResult:
        if not isinstance(record, Mapping):
            raise LiveTradeExecutionError(
                "committed permit journal is missing"
            )
        if record.get("state") != "EVIDENCE_COMMITTED":
            raise LiveTradeExecutionError(
                "committed permit journal state changed"
            )
        evidence_path = str(record.get("evidence_path") or "")
        if evidence_path != str(self._evidence_writer.path):
            raise LiveTradeExecutionError(
                "committed evidence path differs from executor"
            )
        evidence_sha256 = _required_sha256(
            record.get("evidence_sha256"),
            "committed evidence_sha256",
        )
        if not os.path.lexists(self._evidence_writer.path):
            pending = record.get("pending_evidence")
            if not isinstance(pending, Mapping):
                raise LiveTradeExecutionError(
                    "committed evidence recovery payload is missing"
                )
            if pending.get("path") != evidence_path:
                raise LiveTradeExecutionError(
                    "committed evidence recovery path mismatch"
                )
            pending_hash = _required_sha256(
                pending.get("sha256"),
                "committed evidence recovery sha256",
            )
            if pending_hash != evidence_sha256:
                raise LiveTradeExecutionError(
                    "committed evidence recovery hash mismatch"
                )
            pending_payload = pending.get("payload")
            if not isinstance(pending_payload, Mapping):
                raise LiveTradeExecutionError(
                    "committed evidence recovery payload is invalid"
                )
            _validate_evidence_payload_identity(
                pending_payload,
                authorization,
                mode=self._mode,
            )
            self._require_live_operation_lock()
            self._evidence_writer.commit_prepared(
                pending_payload,
                evidence_sha256=evidence_sha256,
                allow_existing=False,
            )
        raw_evidence = _read_protected_file(
            self._evidence_writer.path,
            "committed evidence",
            live=self._mode == "live",
        )
        if _sha256_bytes(raw_evidence) != evidence_sha256:
            raise LiveTradeExecutionError(
                "committed evidence hash differs from ledger"
            )
        try:
            payload = json.loads(raw_evidence)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LiveTradeExecutionError(
                "committed evidence is invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise LiveTradeExecutionError(
                "committed evidence must be an object"
            )
        _validate_evidence_payload_identity(
            payload,
            authorization,
            mode=self._mode,
        )
        return self._execution_result_from_evidence(
            payload,
            evidence_sha256=evidence_sha256,
        )

    def _execution_result_from_evidence(
        self,
        payload: Mapping[str, Any],
        *,
        evidence_sha256: str,
    ) -> ExecutionResult:
        details_key = "dry_run_round_trip"
        if self._mode == "live":
            details_key = "mainnet_round_trip"
        details = payload.get(details_key)
        if not isinstance(details, dict):
            raise LiveTradeExecutionError(
                "prepared recovery evidence lacks round trip"
            )
        status = str(
            payload.get("result_status")
            or payload.get("status")
            or RESULT_BLOCKED
        )
        if status not in RESULT_STATUSES:
            raise LiveTradeExecutionError(
                "prepared recovery evidence has invalid status"
            )
        return ExecutionResult(
            status=status,
            failure_reason=str(
                payload.get("failure_reason") or ""
            ),
            evidence_path=self._evidence_writer.path,
            evidence_sha256=evidence_sha256,
            finished_halted=bool(payload.get("finished_halted")),
            close_submitted=(
                int(payload.get("round_trip_count", 0)) == 1
            ),
            close_quantity=_decimal(
                details.get("close_quantity"),
                "prepared recovery close_quantity",
                non_negative=True,
            ),
            warnings=tuple(
                str(value)
                for value in payload.get("warnings", ())
            ),
            degraded_reasons=tuple(
                str(value)
                for value in payload.get("degraded_reasons", ())
            ),
            retryable=bool(payload.get("retryable")),
            error_code=str(payload.get("error_code") or ""),
            retry_counts={
                str(key): int(value)
                for key, value in dict(
                    payload.get("retry_counts") or {}
                ).items()
            },
        )

    def _finalize_halted_recovery(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        recovery_record: Mapping[str, Any],
        *,
        enrich_committed: bool = False,
    ) -> ExecutionResult:
        recovery_history = _recovery_history_evidence(
            recovery_record,
            authorization,
        )
        halt_observed_at = recovery_history["halt_observed_at"]
        final_request = self._base_request(authorization)
        final_request.update(
            {
                "reason": "recovery-only-evidence-finalizer",
                "phase": "recovery-only-final",
                "quantity": _decimal_text(authorization.quantity),
                "open_side": authorization.open_side,
                "limit_price_usdt": _decimal_text(
                    authorization.limit_price_usdt
                ),
                "halt_observed_at": halt_observed_at.isoformat(),
                "exchange_not_before": halt_observed_at.isoformat(),
                "hard_timeout_seconds": self._recovery_deadline_seconds,
            }
        )
        raw_snapshot = self._adapter.final_snapshot(final_request)
        residual_risk = _recovery_snapshot_target_risk(
            raw_snapshot,
            authorization,
            now=_utc_now(self._clock),
            exchange_not_before=halt_observed_at,
        )
        if residual_risk is not False:
            self._require_live_operation_lock()
            self._permit_store.mark_state(
                authorization,
                mode=self._mode,
                state="RISK_RECOVERY_REQUIRED",
                event_type="RISK_RECOVERY_REQUIRED",
                payload=residual_risk,
            )
            trail.record(
                "recovery_only_residual_target_risk",
                residual_risk,
            )
            raise ClassifiedExecutionError(
                "recovery final snapshot contains residual target risk",
                code="RISK_RECOVERY_REQUIRED",
            )
        final_snapshot = _validate_final_snapshot(
            raw_snapshot,
            authorization,
            now=_utc_now(self._clock),
            exchange_not_before=halt_observed_at,
        )
        financial_proof_complete = (
            final_snapshot.get("financial_proof_complete") is True
        )
        open_quantity = _decimal(
            final_snapshot.get("open_filled_quantity"),
            "recovery open_filled_quantity",
            non_negative=True,
        )
        requested_close_quantity = recovery_history[
            "requested_close_quantity"
        ]
        close_quantity = requested_close_quantity
        if open_quantity > authorization.quantity:
            raise LiveTradeExecutionError(
                "recovery open filled quantity exceeds authorization"
            )
        confirmed_close_quantity = recovery_history[
            "confirmed_close_quantity"
        ]
        if (
            confirmed_close_quantity is not False
            and requested_close_quantity != confirmed_close_quantity
        ):
            raise LiveTradeExecutionError(
                "recovery close confirmation differs from journal request"
            )
        if confirmed_close_quantity is False:
            self._add_degraded(
                "CLOSE_ACK_MISSING_RECOVERED_FROM_EXCHANGE_SNAPSHOT"
            )
        open_average_price = _decimal(
            final_snapshot.get("open_average_fill_price_usdt"),
            "recovery open_average_fill_price_usdt",
            non_negative=True,
        )
        actual_open_notional = open_quantity * open_average_price
        if (
            financial_proof_complete
            and open_quantity != requested_close_quantity
        ):
            raise LiveTradeExecutionError(
                "recovery open fill differs from journal close request"
            )
        if actual_open_notional > authorization.max_notional_usdt:
            raise LiveTradeExecutionError(
                "recovery actual open notional exceeds permit"
            )
        if actual_open_notional > MAX_ACTUAL_OPEN_NOTIONAL_USDT:
            raise LiveTradeExecutionError(
                "recovery actual open notional exceeds 12 USDT"
            )
        cumulative_loss = _decimal(
            final_snapshot.get("cumulative_net_loss_usdt"),
            "recovery cumulative_net_loss_usdt",
            non_negative=True,
        )
        if cumulative_loss >= authorization.max_cumulative_net_loss_usdt:
            raise LiveTradeExecutionError(
                "recovery cumulative net loss threshold reached"
            )
        self._record_final_snapshot_degradation(
            final_snapshot,
            trail,
            phase="recovery-only-final",
        )
        enrichment_retryable = not financial_proof_complete
        trail.record(
            "recovery_only_final_snapshot_confirmed",
            {
                "adapter_evidence_sha256": final_snapshot[
                    "evidence_sha256"
                ],
                "fetched_at": final_snapshot["fetched_at"],
                "halt_observed_at": halt_observed_at.isoformat(),
                "open_filled_quantity": _decimal_text(open_quantity),
                "close_filled_quantity": _decimal_text(close_quantity),
                "close_ack_confirmed": (
                    confirmed_close_quantity is not False
                ),
                "financial_proof_complete": financial_proof_complete,
                "enrichment_retryable": enrichment_retryable,
            },
        )
        cleanup_result = CleanupResult(
            safe=True,
            close_submitted=True,
            close_quantity=close_quantity,
            pre_halt_snapshot=final_snapshot,
            final_snapshot=final_snapshot,
            errors=(),
        )
        status = RESULT_PASSED
        if self._degraded_reasons:
            status = RESULT_DEGRADED
        evidence = self._build_evidence(
            authorization,
            trail,
            status=status,
            passed=True,
            failure_reason="",
            error_code=(
                self._degraded_error_code()
                if status == RESULT_DEGRADED
                else ""
            ),
            retryable=False,
            finished_halted=True,
            cleanup_result=cleanup_result,
            actual_open_notional=actual_open_notional,
            open_filled_quantity=open_quantity,
            peak_cumulative_loss=cumulative_loss,
        )
        evidence["financial_enrichment_retryable"] = (
            enrichment_retryable
        )
        prepared_evidence, prepared_hash = self._evidence_writer.prepare(
            evidence
        )
        self._require_live_operation_lock()
        self._permit_store.prepare_evidence(
            authorization,
            mode=self._mode,
            evidence_path=self._evidence_writer.path,
            evidence=prepared_evidence,
            evidence_sha256=prepared_hash,
            passed=True,
            allow_terminal_enrichment=enrich_committed,
        )
        replace_existing = os.path.lexists(self._evidence_writer.path)
        if replace_existing:
            if enrich_committed:
                self._validate_enrichment_evidence_for_replacement(
                    authorization,
                    expected_sha256=_required_sha256(
                        recovery_record.get("evidence_sha256"),
                        "committed enrichment source sha256",
                    ),
                )
            else:
                self._validate_recoverable_evidence_for_replacement(
                    authorization
                )
        evidence_sha256 = self._evidence_writer.commit_prepared(
            prepared_evidence,
            evidence_sha256=prepared_hash,
            allow_existing=False,
            replace_existing=replace_existing,
        )
        self._permit_store.commit_evidence(
            authorization,
            mode=self._mode,
            evidence_sha256=evidence_sha256,
        )
        self._require_live_operation_lock()
        return ExecutionResult(
            status=status,
            failure_reason="",
            evidence_path=self._evidence_writer.path,
            evidence_sha256=evidence_sha256,
            finished_halted=True,
            close_submitted=True,
            close_quantity=close_quantity,
            warnings=tuple(self._warnings),
            degraded_reasons=tuple(self._degraded_reasons),
            retryable=False,
            error_code=(
                self._degraded_error_code()
                if status == RESULT_DEGRADED
                else ""
            ),
            retry_counts=dict(self._retry_counts),
        )

    def _validate_recoverable_evidence_for_replacement(
        self,
        authorization: CanaryAuthorization,
    ) -> None:
        raw = _read_protected_file(
            self._evidence_writer.path,
            "recoverable existing evidence",
            live=self._mode == "live",
        )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LiveTradeExecutionError(
                "recoverable existing evidence is invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise LiveTradeExecutionError(
                "recoverable existing evidence must be an object"
            )
        _validate_evidence_payload_identity(
            payload,
            authorization,
            mode=self._mode,
        )
        result_status = payload.get("result_status")
        if result_status is None:
            result_status = payload.get("status")
        if (
            result_status != RESULT_BLOCKED
            or payload.get("passed") is not False
        ):
            raise LiveTradeExecutionError(
                "recoverable existing evidence is terminal"
            )

    def _validate_enrichment_evidence_for_replacement(
        self,
        authorization: CanaryAuthorization,
        *,
        expected_sha256: str,
    ) -> None:
        raw = _read_protected_file(
            self._evidence_writer.path,
            "committed enrichment source evidence",
            live=self._mode == "live",
        )
        if _sha256_bytes(raw) != expected_sha256:
            raise LiveTradeExecutionError(
                "committed enrichment source hash mismatch"
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LiveTradeExecutionError(
                "committed enrichment source evidence is invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise LiveTradeExecutionError(
                "committed enrichment source evidence must be an object"
            )
        _validate_evidence_payload_identity(
            payload,
            authorization,
            mode=self._mode,
        )
        if payload.get("financial_enrichment_retryable") is not True:
            raise LiveTradeExecutionError(
                "committed evidence does not allow enrichment"
            )

    def _base_request(
        self,
        authorization: CanaryAuthorization,
    ) -> dict[str, Any]:
        payload = _identity_payload(authorization)
        payload.update(
            {
                "mode": self._mode,
                "authorization_sha256": (
                    authorization.authorization_sha256
                ),
                "document_sha256": dict(
                    authorization.document_sha256
                ),
            }
        )
        return payload

    def _build_evidence(
        self,
        authorization: CanaryAuthorization,
        trail: AuditTrail,
        *,
        status: str,
        passed: bool,
        failure_reason: str,
        error_code: str,
        retryable: bool,
        finished_halted: bool,
        cleanup_result: CleanupResult,
        actual_open_notional: Decimal,
        open_filled_quantity: Decimal,
        peak_cumulative_loss: Decimal,
    ) -> dict[str, Any]:
        final_snapshot = cleanup_result.final_snapshot
        target_flat = False
        regular_orders_zero = False
        algo_orders_zero = False
        portfolio_after = ""
        gross_pnl = Decimal(0)
        fees = Decimal(0)
        net_pnl = Decimal(0)
        final_cumulative_loss = peak_cumulative_loss
        if final_snapshot is not None:
            target_flat = bool(
                final_snapshot.get("target_symbol_flat")
            )
            regular_orders_zero = bool(
                final_snapshot.get(
                    "target_symbol_regular_orders_zero"
                )
            )
            algo_orders_zero = bool(
                final_snapshot.get(
                    "target_symbol_algo_orders_zero"
                )
            )
            portfolio_after = str(
                final_snapshot.get(
                    "non_target_portfolio_baseline_sha256",
                    "",
                )
            )
            gross_pnl = _decimal(
                final_snapshot.get("gross_pnl_usdt"),
                "evidence gross_pnl_usdt",
            )
            fees = _decimal(
                final_snapshot.get("fees_usdt"),
                "evidence fees_usdt",
                non_negative=True,
            )
            net_pnl = _decimal(
                final_snapshot.get("net_pnl_usdt"),
                "evidence net_pnl_usdt",
            )
            final_cumulative_loss = _decimal(
                final_snapshot.get("cumulative_net_loss_usdt"),
                "evidence cumulative_net_loss_usdt",
                non_negative=True,
            )
            final_cumulative_loss = max(
                peak_cumulative_loss,
                final_cumulative_loss,
            )
        round_trip_count = 0
        if cleanup_result.close_submitted:
            round_trip_count = 1
        execution_details = {
            "open_order_type": "LIMIT",
            "open_time_in_force": "IOC",
            "open_side": authorization.open_side,
            "requested_quantity": _decimal_text(authorization.quantity),
            "limit_price_usdt": _decimal_text(
                authorization.limit_price_usdt
            ),
            "fee_reserve_usdt": _decimal_text(
                authorization.fee_reserve_usdt
            ),
            "exchange_filters": {
                field_name: _decimal_text(field_value)
                for field_name, field_value in (
                    authorization.exchange_filters.items()
                )
            },
            "open_client_order_id": (
                authorization.open_client_order_id
            ),
            "open_filled_quantity": _decimal_text(
                open_filled_quantity
            ),
            "actual_open_notional_usdt": _decimal_text(
                actual_open_notional
            ),
            "close_order_type": "MARKET",
            "close_reduce_only": True,
            "close_client_order_id": (
                authorization.close_client_order_id
            ),
            "close_quantity": _decimal_text(
                cleanup_result.close_quantity
            ),
            "close_filled_quantity": "0",
            "target_symbol_flat": target_flat,
            "target_symbol_regular_orders_zero": (
                regular_orders_zero
            ),
            "target_symbol_algo_orders_zero": algo_orders_zero,
            "max_cumulative_loss_usdt": _decimal_text(
                authorization.max_cumulative_net_loss_usdt
            ),
            "gross_pnl_usdt": _decimal_text(gross_pnl),
            "fees_usdt": _decimal_text(fees),
            "net_pnl_usdt": _decimal_text(net_pnl),
            "cumulative_net_loss_usdt": _decimal_text(
                final_cumulative_loss
            ),
            "emergency_close_available": True,
        }
        if cleanup_result.close_submitted:
            execution_details["close_filled_quantity"] = _decimal_text(
                cleanup_result.close_quantity
            )
        if final_snapshot is not None:
            execution_details["final_snapshot_phase"] = (
                "post-halt-final"
            )
            execution_details["final_snapshot_fetched_at"] = str(
                final_snapshot.get("fetched_at") or ""
            )
            execution_details["final_snapshot_evidence_sha256"] = str(
                final_snapshot.get("evidence_sha256") or ""
            )
        emergency_close_evidence = {
            "verified": True,
            "evidence_type": authorization.emergency_close_evidence_type,
            "verified_at": (
                authorization.emergency_close_verified_at.isoformat()
            ),
            "environment": authorization.emergency_close_environment,
            "release_id": authorization.release.release_id,
            "image_digest": authorization.release.image_digest,
            "symbol": authorization.symbol,
            "open_order_type": "LIMIT",
            "open_time_in_force": "IOC",
            "open_filled_quantity": _decimal_text(
                authorization.emergency_close_quantity
            ),
            "close_order_type": "MARKET",
            "close_reduce_only": True,
            "close_quantity": _decimal_text(
                authorization.emergency_close_quantity
            ),
            "close_filled_quantity": _decimal_text(
                authorization.emergency_close_quantity
            ),
            "target_symbol_flat": True,
            "target_symbol_regular_orders_zero": True,
            "target_symbol_algo_orders_zero": True,
            "evidence_sha256": (
                authorization.emergency_close_evidence_sha256
            ),
        }
        evidence = {
            "schema_version": EVIDENCE_SCHEMA,
            "generated_at": _utc_now(self._clock).isoformat(),
            "mode": self._mode,
            "status": status,
            "result_status": status,
            "passed": passed,
            "failure_reason": failure_reason,
            "error_code": error_code,
            "retryable": retryable,
            "warnings": list(self._warnings),
            "degraded_reasons": list(self._degraded_reasons),
            "retry_counts": dict(self._retry_counts),
            "rollout_phase": "account_a_canary",
            "account_id": authorization.account_id,
            "symbol": authorization.symbol,
            "release_id": authorization.release.release_id,
            "image_digest": authorization.release.image_digest,
            "config_sha256": authorization.release.config_sha256,
            "dependency_lock_sha256": (
                authorization.release.dependency_lock_sha256
            ),
            "permit_id": authorization.permit_id,
            "intent_id": authorization.intent_id,
            "open_client_order_id": (
                authorization.open_client_order_id
            ),
            "close_client_order_id": (
                authorization.close_client_order_id
            ),
            "side_effect_ids": {
                "resume": deterministic_side_effect_id(
                    authorization,
                    "RESUME",
                ),
                "open": deterministic_side_effect_id(
                    authorization,
                    "OPEN",
                ),
                "cancel_open": deterministic_side_effect_id(
                    authorization,
                    "CANCEL_OPEN",
                ),
                "close": deterministic_side_effect_id(
                    authorization,
                    "CLOSE",
                ),
                "halt": deterministic_side_effect_id(
                    authorization,
                    "HALT",
                ),
            },
            "authorization_sha256": (
                authorization.authorization_sha256
            ),
            "authorization_document_sha256": dict(
                authorization.document_sha256
            ),
            "authorization_signatures_verified": (
                authorization.signatures_verified
            ),
            "permit_journal_degraded": self._journal_degraded,
            "permit_journal_failures": list(
                self._journal_failures
            ),
            "financial_enrichment_retryable": (
                final_snapshot is not None
                and final_snapshot.get(
                    "financial_proof_complete"
                )
                is False
            ),
            "round_trip_count": round_trip_count,
            "finished_halted": finished_halted,
            "target_symbol_flat": target_flat,
            "target_symbol_regular_orders_zero": (
                regular_orders_zero
            ),
            "target_symbol_algo_orders_zero": algo_orders_zero,
            "non_target_portfolio_before_sha256": (
                authorization.portfolio_baseline_sha256
            ),
            "non_target_portfolio_after_sha256": portfolio_after,
            "emergency_close_evidence": emergency_close_evidence,
            "event_chain_sha256": trail.chain_sha256,
            "events": list(trail.events),
        }
        if authorization.emergency_close_evidence_type == "testnet_execution":
            evidence["testnet_emergency_close"] = emergency_close_evidence
        details_key = "dry_run_round_trip"
        if self._mode == "live":
            details_key = "mainnet_round_trip"
        evidence[details_key] = execution_details
        return evidence


def load_authorization(
    paths: AuthorizationPaths,
    *,
    execute_live: bool,
    signature_verifier: SignatureVerifier | None = None,
    operation_lock: LiveOperationLock | None = None,
    now: datetime | None = None,
    allow_expired: bool = False,
) -> CanaryAuthorization:
    current_time = now
    if current_time is None:
        current_time = datetime.now(timezone.utc)
    current_time = _aware_utc(current_time, "now")

    public_key = b""
    signatures_verified = False
    if execute_live:
        if operation_lock is None:
            raise LiveTradeExecutionError(
                "live authorization requires an operation lock"
            )
        operation_lock.require_held()
        if signature_verifier is None:
            raise LiveTradeExecutionError(
                "live execution requires a signature verifier"
            )
        if paths.reviewer_public_key is None:
            raise LiveTradeExecutionError(
                "live execution requires a reviewer public key"
            )
        public_key = _read_protected_file(
            paths.reviewer_public_key,
            "reviewer public key",
            live=True,
        )
        if _sha256_bytes(public_key) != (
            PINNED_REVIEWER_PUBLIC_KEY_SHA256
        ):
            raise LiveTradeExecutionError(
                "reviewer public key hash mismatch"
            )

    payload_bytes: dict[str, bytes] = {}
    payloads: dict[str, dict[str, Any]] = {}
    signature_bytes: dict[str, bytes] = {}
    document_paths = {
        "release_gate": paths.release_gate,
        "safety_gate": paths.safety_gate,
        "emergency_close_gate": paths.emergency_close_gate,
        "permit": paths.permit,
    }
    for document_name, document_path in document_paths.items():
        raw = _read_protected_file(
            document_path.payload,
            document_name,
            live=execute_live,
        )
        payload_bytes[document_name] = raw
        payloads[document_name] = _decode_json_object(
            raw,
            document_name,
        )
        if execute_live:
            signature_path = document_path.signature
            if signature_path is None:
                raise LiveTradeExecutionError(
                    f"{document_name} signature is required"
                )
            signature_bytes[document_name] = _read_protected_file(
                signature_path,
                f"{document_name} signature",
                live=True,
            )
    if execute_live:
        operation_lock.require_held()
        for document_name in document_paths:
            signature_verifier.verify(
                public_key=public_key,
                payload=payload_bytes[document_name],
                signature=signature_bytes[document_name],
            )
        signatures_verified = True

    document_sha256 = {
        name: _sha256_bytes(raw)
        for name, raw in payload_bytes.items()
    }
    return _validate_authorization_documents(
        payloads,
        document_sha256=document_sha256,
        signatures_verified=signatures_verified,
        now=current_time,
        allow_expired=allow_expired,
    )


def load_evidence_recovery_gate(
    paths: SignedDocumentPaths,
    *,
    reviewer_public_key: Path,
    authorization: CanaryAuthorization,
    evidence_path: Path,
    signature_verifier: SignatureVerifier,
    operation_lock: LiveOperationLock,
    now: datetime | None = None,
) -> EvidenceRecoveryGate:
    operation_lock.require_held()
    current_time = now
    if current_time is None:
        current_time = datetime.now(timezone.utc)
    current_time = _aware_utc(current_time, "now")
    public_key = _read_protected_file(
        reviewer_public_key,
        "reviewer public key",
        live=True,
    )
    if _sha256_bytes(public_key) != PINNED_REVIEWER_PUBLIC_KEY_SHA256:
        raise LiveTradeExecutionError(
            "reviewer public key hash mismatch"
        )
    signature_path = paths.signature
    if signature_path is None:
        raise LiveTradeExecutionError(
            "evidence recovery gate signature is required"
        )
    raw_payload = _read_protected_file(
        paths.payload,
        "evidence recovery gate",
        live=True,
    )
    raw_signature = _read_protected_file(
        signature_path,
        "evidence recovery gate signature",
        live=True,
    )
    operation_lock.require_held()
    signature_verifier.verify(
        public_key=public_key,
        payload=raw_payload,
        signature=raw_signature,
    )
    payload = _decode_json_object(
        raw_payload,
        "evidence recovery gate",
    )
    _require_schema(
        payload,
        EVIDENCE_RECOVERY_GATE_SCHEMA,
        "evidence recovery gate",
    )
    if payload.get("capability") != EVIDENCE_RECOVERY_CAPABILITY:
        raise LiveTradeExecutionError(
            "evidence recovery gate capability mismatch"
        )
    allowed_actions = payload.get("allowed_actions")
    if allowed_actions != list(EVIDENCE_RECOVERY_ALLOWED_ACTIONS):
        raise LiveTradeExecutionError(
            "evidence recovery gate allowed actions mismatch"
        )
    requested_evidence_path = _bind_protected_output_path(
        evidence_path,
        "evidence recovery output path",
        owner_uid=os.geteuid(),
    )
    signed_evidence_path = _bind_protected_output_path(
        payload.get("evidence_path"),
        "signed evidence recovery output path",
        owner_uid=os.geteuid(),
    )
    if signed_evidence_path.target != requested_evidence_path.target:
        raise LiveTradeExecutionError(
            "evidence recovery gate identity mismatch: evidence_path"
        )
    expected_identity = {
        "account_id": authorization.account_id,
        "symbol": authorization.symbol,
        "permit_id": authorization.permit_id,
        "authorization_sha256": (
            authorization.authorization_sha256
        ),
        "release_id": authorization.release.release_id,
        "intent_id": authorization.intent_id,
        "close_intent_id": authorization.close_intent_id,
        "open_client_order_id": (
            authorization.open_client_order_id
        ),
        "close_client_order_id": (
            authorization.close_client_order_id
        ),
        "permit_store_id": authorization.release.permit_store_id,
        "permit_store_path": str(DEFAULT_LIVE_PERMIT_LEDGER_PATH),
        "evidence_path": str(requested_evidence_path.target),
    }
    for field_name, expected_value in expected_identity.items():
        actual_value = payload.get(field_name)
        if field_name == "evidence_path":
            actual_value = str(signed_evidence_path.target)
        if actual_value != expected_value:
            raise LiveTradeExecutionError(
                "evidence recovery gate identity mismatch: "
                f"{field_name}"
            )
    if Path(authorization.release.permit_store_path) != (
        DEFAULT_LIVE_PERMIT_LEDGER_PATH
    ):
        raise LiveTradeExecutionError(
            "evidence recovery authorization ledger path mismatch"
        )
    issued_at = _timestamp(
        payload.get("issued_at"),
        "evidence recovery gate issued_at",
    )
    if issued_at - current_time > MAX_CLOCK_SKEW:
        raise LiveTradeExecutionError(
            "evidence recovery gate is issued in the future"
        )
    refresh_after = _timestamp(
        payload.get("refresh_after"),
        "evidence recovery gate refresh_after",
    )
    if refresh_after <= issued_at:
        raise LiveTradeExecutionError(
            "evidence recovery gate refresh_after must follow issued_at"
        )
    warnings: list[str] = []
    if current_time >= refresh_after:
        warnings.append(
            "EVIDENCE_RECOVERY_GATE_REFRESH_RECOMMENDED: "
            "signed recovery capability remains valid"
        )
    return EvidenceRecoveryGate(
        gate_id=_canonical_uuid(
            payload.get("gate_id"),
            "evidence recovery gate_id",
        ),
        permit_id=authorization.permit_id,
        authorization_sha256=authorization.authorization_sha256,
        release_id=authorization.release.release_id,
        intent_id=authorization.intent_id,
        close_intent_id=authorization.close_intent_id,
        open_client_order_id=authorization.open_client_order_id,
        close_client_order_id=authorization.close_client_order_id,
        permit_store_id=authorization.release.permit_store_id,
        permit_store_path=str(DEFAULT_LIVE_PERMIT_LEDGER_PATH),
        evidence_path=expected_identity["evidence_path"],
        recovery_executor_sha256=_required_sha256(
            payload.get("recovery_executor_sha256"),
            "evidence recovery executor sha256",
        ),
        recovery_adapter_sha256=_required_sha256(
            payload.get("recovery_adapter_sha256"),
            "evidence recovery adapter sha256",
        ),
        issued_at=issued_at,
        refresh_after=refresh_after,
        warnings=tuple(warnings),
    )


def _validate_authorization_documents(
    payloads: Mapping[str, Mapping[str, Any]],
    *,
    document_sha256: Mapping[str, str],
    signatures_verified: bool,
    now: datetime,
    allow_expired: bool = False,
) -> CanaryAuthorization:
    release_gate = payloads["release_gate"]
    safety_gate = payloads["safety_gate"]
    emergency_gate = payloads["emergency_close_gate"]
    permit = payloads["permit"]

    _require_schema(
        release_gate,
        RELEASE_GATE_SCHEMA,
        "release gate",
    )
    _require_schema(
        safety_gate,
        SAFETY_GATE_SCHEMA,
        "safety gate",
    )
    _require_schema(
        emergency_gate,
        EMERGENCY_CLOSE_GATE_SCHEMA,
        "emergency close gate",
    )
    _require_schema(permit, PERMIT_SCHEMA, "permit")

    release_identity = ReleaseIdentity(
        release_id=_required_text(
            release_gate.get("release_id"),
            "release gate release_id",
        ),
        node_id=_required_text(
            release_gate.get("node_id"),
            "release gate node_id",
        ),
        writer_id=_required_text(
            release_gate.get("writer_id"),
            "release gate writer_id",
        ),
        lease_id=_required_text(
            release_gate.get("lease_id"),
            "release gate lease_id",
        ),
        fencing_epoch=_positive_integer(
            release_gate.get("fencing_epoch"),
            "release gate fencing_epoch",
        ),
        image_digest=_required_image_digest(
            release_gate.get("image_digest"),
            "release gate image_digest",
        ),
        config_sha256=_required_sha256(
            release_gate.get("config_sha256"),
            "release gate config_sha256",
        ),
        dependency_lock_sha256=_required_sha256(
            release_gate.get("dependency_lock_sha256"),
            "release gate dependency_lock_sha256",
        ),
        live_executor_sha256=_required_sha256(
            release_gate.get("live_executor_sha256"),
            "release gate live_executor_sha256",
        ),
        live_adapter_sha256=_required_sha256(
            release_gate.get("live_adapter_sha256"),
            "release gate live_adapter_sha256",
        ),
        permit_store_id=_required_text(
            release_gate.get("permit_store_id"),
            "release gate permit_store_id",
        ),
        permit_store_path=_required_text(
            release_gate.get("permit_store_path"),
            "release gate permit_store_path",
        ),
    )
    if release_identity.permit_store_id != LIVE_PERMIT_STORE_ID:
        raise LiveTradeExecutionError(
            "release gate permit store identity mismatch"
        )
    if Path(release_identity.permit_store_path) != (
        DEFAULT_LIVE_PERMIT_LEDGER_PATH
    ):
        raise LiveTradeExecutionError(
            "release gate permit ledger path mismatch"
        )
    _require_target_identity(release_gate, "release gate")
    if release_gate.get("rollout_phase") != "account_a_canary":
        raise LiveTradeExecutionError(
            "release gate rollout phase must be account_a_canary"
        )
    _validate_document_window(
        release_gate,
        "release gate",
        now,
        allow_expired=allow_expired,
    )

    _require_target_identity(safety_gate, "safety gate")
    _require_release_identity(
        safety_gate,
        release_identity,
        "safety gate",
    )
    _require_document_hash(
        safety_gate,
        "release_gate_sha256",
        document_sha256["release_gate"],
        "safety gate",
    )
    warnings: list[str] = []
    required_safety_truths = (
        "canary_halted",
        "target_symbol_flat",
        "target_symbol_regular_orders_zero",
        "target_symbol_algo_orders_zero",
    )
    for field_name in required_safety_truths:
        if safety_gate.get(field_name) is not True:
            raise LiveTradeExecutionError(
                f"safety gate requires {field_name}=true"
            )
    signed_live_health = (
        (
            "process_liveness",
            "SAFETY_PROCESS_LIVENESS_MISSING",
        ),
        (
            "loss_monitor_healthy",
            "SAFETY_LOSS_MONITOR_HEALTH_MISSING",
        ),
    )
    for field_name, warning_code in signed_live_health:
        health_value = safety_gate.get(field_name)
        if health_value is False:
            raise LiveTradeExecutionError(
                f"safety gate requires {field_name}=true"
            )
        if health_value is not True:
            warnings.append(
                f"{warning_code}: {field_name} telemetry is missing"
            )
    scoped_safety_health = (
        (
            "ownership_healthy",
            "SAFETY_OWNERSHIP_HEALTH_MISSING",
        ),
        (
            "fencing_healthy",
            "SAFETY_FENCING_HEALTH_MISSING",
        ),
        (
            "durability_healthy",
            "SAFETY_DURABILITY_HEALTH_MISSING",
        ),
    )
    for field_name, warning_code in scoped_safety_health:
        health_value = safety_gate.get(field_name)
        if health_value is False:
            raise LiveTradeExecutionError(
                f"safety gate requires {field_name}=true"
            )
        if health_value is None or health_value == "":
            warnings.append(
                f"{warning_code}: {field_name} telemetry is missing"
            )
            continue
        if health_value is not True:
            raise LiveTradeExecutionError(
                f"safety gate {field_name} must be boolean"
            )
    if safety_gate.get("risk_healthy") is not True:
        warnings.append(
            "SAFETY_RISK_DEGRADED: risk_healthy is missing or false"
        )
    soft_truth_warnings = (
        (
            "readiness_healthy",
            False,
            "SAFETY_READINESS_DEGRADED: readiness_healthy=false",
        ),
        (
            "reconciliation_healthy",
            False,
            (
                "SAFETY_RECONCILIATION_DEGRADED: "
                "reconciliation_healthy=false"
            ),
        ),
        (
            "no_open_p0_p1_incidents",
            False,
            (
                "UNSCOPED_INCIDENT_WARNING: "
                "no_open_p0_p1_incidents=false"
            ),
        ),
        (
            "circuit_open",
            True,
            "CIRCUIT_OPEN_DEGRADED: circuit_open=true",
        ),
        (
            "memory_queue_pressure",
            True,
            (
                "MEMORY_QUEUE_PRESSURE_DEGRADED: "
                "memory_queue_pressure=true"
            ),
        ),
    )
    for field_name, warning_value, warning in soft_truth_warnings:
        if safety_gate.get(field_name) == warning_value:
            warnings.append(warning)
    portfolio_baseline_sha256 = _required_sha256(
        safety_gate.get("non_target_portfolio_baseline_sha256"),
        "safety gate non_target_portfolio_baseline_sha256",
    )
    health_max_age_seconds = _decimal(
        safety_gate.get("health_max_age_seconds"),
        "safety gate health_max_age_seconds",
        positive=True,
    )
    if health_max_age_seconds > MAX_HEALTH_MAX_AGE_SECONDS:
        raise LiveTradeExecutionError(
            "safety gate health max age exceeds 30 seconds"
        )
    exchange_max_age_seconds = _decimal(
        safety_gate.get(
            "exchange_max_age_seconds",
            DEFAULT_EXCHANGE_MAX_AGE_SECONDS,
        ),
        "safety gate exchange_max_age_seconds",
        positive=True,
    )
    if exchange_max_age_seconds > MAX_EXCHANGE_MAX_AGE_SECONDS:
        raise LiveTradeExecutionError(
            "safety gate exchange max age exceeds 180 seconds"
        )
    health_timestamps: dict[str, datetime | None] = {}
    for field_name in SIGNED_HEALTH_FIELDS:
        timestamp = _optional_timestamp(
            safety_gate.get(field_name),
            f"safety gate {field_name}",
        )
        health_timestamps[field_name] = timestamp
        if timestamp is None:
            warnings.append(
                "SAFETY_HEALTH_TELEMETRY_MISSING: "
                f"{field_name} is missing"
            )
            continue
        warning = _freshness_warning(
            timestamp,
            now=now,
            max_age_seconds=health_max_age_seconds,
            label=f"safety gate {field_name}",
        )
        if warning:
            warnings.append(
                f"SOFT_FRESHNESS_DEGRADED: {warning}"
            )
    _validate_document_window(
        safety_gate,
        "safety gate",
        now,
        allow_expired=allow_expired,
    )

    _require_target_identity(
        emergency_gate,
        "emergency close gate",
    )
    _require_release_identity(
        emergency_gate,
        release_identity,
        "emergency close gate",
    )
    _require_document_hash(
        emergency_gate,
        "release_gate_sha256",
        document_sha256["release_gate"],
        "emergency close gate",
    )
    _require_document_hash(
        emergency_gate,
        "safety_gate_sha256",
        document_sha256["safety_gate"],
        "emergency close gate",
    )
    evidence_type = _required_text(
        emergency_gate.get("evidence_type", "testnet_execution"),
        "emergency close gate evidence_type",
    )
    supported_evidence_environments = {
        "testnet_execution": "testnet",
        "contract_replay": "contract_replay",
    }
    expected_environment = supported_evidence_environments.get(evidence_type)
    if expected_environment is None:
        raise LiveTradeExecutionError(
            "emergency close evidence_type is unsupported"
        )
    if emergency_gate.get("environment") != expected_environment:
        raise LiveTradeExecutionError(
            "emergency close gate mismatch: environment"
        )
    if evidence_type == "contract_replay":
        warnings.append(
            (
                "EMERGENCY_CLOSE_CONTRACT_REPLAY_ONLY: "
                "emergency close evidence is a deterministic contract replay"
            )
        )
    expected_emergency_values = {
        "verified": True,
        "open_order_type": "LIMIT",
        "open_time_in_force": "IOC",
        "close_order_type": "MARKET",
        "close_reduce_only": True,
        "target_symbol_flat": True,
        "target_symbol_regular_orders_zero": True,
        "target_symbol_algo_orders_zero": True,
    }
    for field_name, expected_value in expected_emergency_values.items():
        if emergency_gate.get(field_name) != expected_value:
            raise LiveTradeExecutionError(
                f"emergency close gate mismatch: {field_name}"
            )
    emergency_evidence_sha256 = _required_sha256(
        emergency_gate.get(
            "evidence_sha256",
            emergency_gate.get("testnet_evidence_sha256"),
        ),
        "emergency close gate evidence_sha256",
    )
    emergency_verified_at = _timestamp(
        emergency_gate.get("verified_at"),
        "emergency close gate verified_at",
    )
    evidence_age = now - emergency_verified_at
    if evidence_age < -MAX_CLOCK_SKEW:
        raise LiveTradeExecutionError(
            "emergency close verification is in the future"
        )
    if evidence_age > timedelta(days=1):
        warnings.append(
            (
                "EMERGENCY_CLOSE_PROOF_STALE: "
                "emergency close verification exceeds 1 day"
            )
        )
    emergency_quantity = _decimal(
        emergency_gate.get("verified_quantity"),
        "emergency close gate verified_quantity",
        positive=True,
    )
    _validate_document_window(
        emergency_gate,
        "emergency close gate",
        now,
        allow_expired=allow_expired,
    )

    _require_target_identity(permit, "permit")
    _require_release_identity(permit, release_identity, "permit")
    _require_document_hash(
        permit,
        "release_gate_sha256",
        document_sha256["release_gate"],
        "permit",
    )
    _require_document_hash(
        permit,
        "safety_gate_sha256",
        document_sha256["safety_gate"],
        "permit",
    )
    _require_document_hash(
        permit,
        "emergency_close_gate_sha256",
        document_sha256["emergency_close_gate"],
        "permit",
    )
    permit_id = _canonical_uuid(
        permit.get("permit_id"),
        "permit_id",
    )
    intent_id = _canonical_uuid(
        permit.get("intent_id"),
        "intent_id",
    )
    close_intent_id = deterministic_close_intent_id(intent_id)
    open_client_order_id = _required_client_order_id(
        permit.get("open_client_order_id"),
        "open_client_order_id",
    )
    close_client_order_id = _required_client_order_id(
        permit.get("close_client_order_id"),
        "close_client_order_id",
    )
    expected_open_client_order_id = deterministic_open_client_order_id(
        intent_id
    )
    expected_close_client_order_id = deterministic_close_client_order_id(
        intent_id
    )
    if open_client_order_id != expected_open_client_order_id:
        raise LiveTradeExecutionError(
            "open client order ID is not derived from intent"
        )
    if close_client_order_id != expected_close_client_order_id:
        raise LiveTradeExecutionError(
            "close client order ID is not derived from intent"
        )
    open_side = _required_text(
        permit.get("open_side"),
        "open_side",
    ).upper()
    if open_side not in {"BUY", "SELL"}:
        raise LiveTradeExecutionError(
            "open_side must be BUY or SELL"
        )
    quantity = _decimal(
        permit.get("quantity"),
        "quantity",
        positive=True,
    )
    if quantity != LIVE_CANARY_QUANTITY:
        raise LiveTradeExecutionError(
            "permit quantity must equal 0.07 SOL"
        )
    if quantity != emergency_quantity:
        raise LiveTradeExecutionError(
            "permit quantity differs from emergency close evidence"
        )
    limit_price = _decimal(
        permit.get("limit_price_usdt"),
        "limit_price_usdt",
        positive=True,
    )
    max_notional = _decimal(
        permit.get("max_notional_usdt"),
        "max_notional_usdt",
        positive=True,
    )
    if max_notional > MAX_ACTUAL_OPEN_NOTIONAL_USDT:
        raise LiveTradeExecutionError(
            "permit max notional exceeds 12 USDT"
        )
    requested_notional = quantity * limit_price
    if requested_notional > max_notional:
        raise LiveTradeExecutionError(
            "requested open notional exceeds permit"
        )
    if requested_notional > MAX_ACTUAL_OPEN_NOTIONAL_USDT:
        raise LiveTradeExecutionError(
            "requested open notional exceeds 12 USDT"
        )
    fee_reserve = _decimal(
        permit.get("fee_reserve_usdt"),
        "fee_reserve_usdt",
        non_negative=True,
    )
    exchange_filters = _parse_exchange_filters(
        permit.get("exchange_filters"),
        quantity=quantity,
        limit_price=limit_price,
        requested_notional=requested_notional,
        warnings=warnings,
    )
    max_loss = _decimal(
        permit.get("max_cumulative_net_loss_usdt"),
        "max_cumulative_net_loss_usdt",
        positive=True,
    )
    if max_loss >= ABSOLUTE_LOSS_CAP_USDT:
        raise LiveTradeExecutionError(
            "loss threshold must be below 1.5 USDT"
        )
    if permit.get("single_use") is not True:
        raise LiveTradeExecutionError(
            "permit requires single_use=true"
        )
    max_round_trips = permit.get("max_round_trips")
    if isinstance(max_round_trips, bool) or max_round_trips != 1:
        raise LiveTradeExecutionError(
            "permit requires max_round_trips=1"
        )
    permit_baseline = _required_sha256(
        permit.get("portfolio_baseline_sha256"),
        "permit portfolio_baseline_sha256",
    )
    if permit_baseline != portfolio_baseline_sha256:
        raise LiveTradeExecutionError(
            "permit portfolio baseline differs from safety gate"
        )
    expires_at = _validate_document_window(
        permit,
        "permit",
        now,
        allow_expired=allow_expired,
    )
    if allow_expired and expires_at <= now:
        warnings.append(
            "EVIDENCE_RECOVERY_EXPIRED_AUTHORIZATION: "
            "signed authorization is restricted to evidence recovery"
        )

    normalized_hashes = {
        name: _required_sha256(value, f"{name} sha256")
        for name, value in document_sha256.items()
    }
    authorization_payload = {
        "permit_id": permit_id,
        "account_id": ACCOUNT_ID,
        "symbol": SYMBOL,
        "release": asdict(release_identity),
        "intent_id": intent_id,
        "close_intent_id": close_intent_id,
        "open_client_order_id": open_client_order_id,
        "close_client_order_id": close_client_order_id,
        "open_side": open_side,
        "quantity": _decimal_text(quantity),
        "limit_price_usdt": _decimal_text(limit_price),
        "max_notional_usdt": _decimal_text(max_notional),
        "fee_reserve_usdt": _decimal_text(fee_reserve),
        "exchange_filters": {
            field_name: _decimal_text(field_value)
            for field_name, field_value in exchange_filters.items()
        },
        "max_cumulative_net_loss_usdt": _decimal_text(max_loss),
        "portfolio_baseline_sha256": portfolio_baseline_sha256,
        "expires_at": expires_at.isoformat(),
        "emergency_close_evidence_type": evidence_type,
        "emergency_close_environment": expected_environment,
        "emergency_close_verified_at": (
            emergency_verified_at.isoformat()
        ),
        "emergency_close_quantity": _decimal_text(
            emergency_quantity
        ),
        "emergency_close_evidence_sha256": (
            emergency_evidence_sha256
        ),
        "health_max_age_seconds": _decimal_text(
            health_max_age_seconds
        ),
        "exchange_max_age_seconds": _decimal_text(
            exchange_max_age_seconds
        ),
        "document_sha256": normalized_hashes,
        "signatures_verified": signatures_verified,
    }
    for field_name in SIGNED_HEALTH_FIELDS:
        timestamp = health_timestamps[field_name]
        authorization_payload[field_name] = None
        if timestamp is not None:
            authorization_payload[field_name] = timestamp.isoformat()
    authorization_sha256 = _sha256_bytes(
        _canonical_json_bytes(authorization_payload)
    )
    return CanaryAuthorization(
        permit_id=permit_id,
        account_id=ACCOUNT_ID,
        symbol=SYMBOL,
        release=release_identity,
        intent_id=intent_id,
        close_intent_id=close_intent_id,
        open_client_order_id=open_client_order_id,
        close_client_order_id=close_client_order_id,
        open_side=open_side,
        quantity=quantity,
        limit_price_usdt=limit_price,
        max_notional_usdt=max_notional,
        fee_reserve_usdt=fee_reserve,
        exchange_filters=exchange_filters,
        max_cumulative_net_loss_usdt=max_loss,
        portfolio_baseline_sha256=portfolio_baseline_sha256,
        expires_at=expires_at,
        emergency_close_evidence_type=evidence_type,
        emergency_close_environment=expected_environment,
        emergency_close_verified_at=emergency_verified_at,
        emergency_close_quantity=emergency_quantity,
        emergency_close_evidence_sha256=(
            emergency_evidence_sha256
        ),
        actor_tick_at=health_timestamps["actor_tick_at"],
        heartbeat_at=health_timestamps["heartbeat_at"],
        projection_at=health_timestamps["projection_at"],
        reconciliation_at=health_timestamps[
            "reconciliation_at"
        ],
        loss_monitor_at=health_timestamps["loss_monitor_at"],
        health_max_age_seconds=health_max_age_seconds,
        exchange_max_age_seconds=exchange_max_age_seconds,
        document_sha256=normalized_hashes,
        authorization_sha256=authorization_sha256,
        signatures_verified=signatures_verified,
        warnings=tuple(warnings),
    )


def deterministic_open_client_order_id(intent_id: Any) -> str:
    intent_uuid = UUID(str(intent_id))
    return f"B{intent_uuid.hex}01"


def deterministic_close_intent_id(intent_id: Any) -> str:
    intent_uuid = UUID(str(intent_id))
    close_uuid = uuid5(
        intent_uuid,
        "trader-v3/account-a/SOLUSDT/close",
    )
    return str(close_uuid)


def deterministic_close_client_order_id(intent_id: Any) -> str:
    close_intent_id = deterministic_close_intent_id(intent_id)
    close_uuid = UUID(close_intent_id)
    return f"B{close_uuid.hex}01"


def deterministic_side_effect_id(
    authorization: CanaryAuthorization,
    action: Any,
) -> str:
    normalized_action = _required_text(
        action,
        "side effect action",
    ).upper()
    if re.fullmatch(r"[A-Z][A-Z0-9_-]*", normalized_action) is None:
        raise LiveTradeExecutionError(
            "side effect action contains invalid characters"
        )
    identity = (
        f"{authorization.authorization_sha256}:{normalized_action}"
    )
    return _sha256_bytes(identity.encode("ascii"))


def _validate_authorization_for_execution(
    authorization: CanaryAuthorization,
    *,
    mode: str,
    now: datetime,
    allow_expired: bool = False,
) -> tuple[str, ...]:
    if authorization.account_id != ACCOUNT_ID:
        raise LiveTradeExecutionError(
            "execution account must be account-a"
        )
    if authorization.symbol != SYMBOL:
        raise LiveTradeExecutionError(
            "execution symbol must be SOLUSDT"
        )
    if authorization.quantity != LIVE_CANARY_QUANTITY:
        raise LiveTradeExecutionError(
            "execution quantity must equal 0.07 SOL"
        )
    if authorization.requested_open_notional_usdt > (
        MAX_ACTUAL_OPEN_NOTIONAL_USDT
    ):
        raise LiveTradeExecutionError(
            "execution requested notional exceeds 12 USDT"
        )
    if authorization.max_cumulative_net_loss_usdt >= (
        ABSOLUTE_LOSS_CAP_USDT
    ):
        raise LiveTradeExecutionError(
            "execution loss threshold must be below 1.5 USDT"
        )
    if authorization.expires_at <= now and not allow_expired:
        raise LiveTradeExecutionError("permit is expired")
    warnings = _validate_health_freshness(
        authorization,
        now=now,
        label="execution",
    )
    if mode == "live" and authorization.signatures_verified is not True:
        raise LiveTradeExecutionError(
            "live execution requires verified signatures"
        )
    return warnings


def _validate_live_preflight(
    payload: Mapping[str, Any],
    authorization: CanaryAuthorization,
    *,
    now: datetime,
    phase: str = "before-resume",
    exchange_not_before: datetime | None = None,
) -> LivePreflightEvidence:
    _validate_adapter_identity(payload, authorization)
    if payload.get("action") != "preflight":
        raise LiveTradeExecutionError(
            "preflight adapter action identity mismatch"
        )
    _require_evidence_source(
        payload,
        expected=EXCHANGE_EVIDENCE_SOURCE,
        label="preflight",
    )
    warnings = _validated_payload_warnings(
        payload,
        label="preflight",
    )
    if (
        phase == "before-open"
        and payload.get("exchange_authoritative") is not True
    ):
        warnings.append(
            "PREFLIGHT_EXCHANGE_AUTHORITY_UNCONFIRMED"
        )
    fetched_at = _timestamp(
        payload.get("fetched_at"),
        "preflight fetched_at",
    )
    _require_fresh_timestamp(
        fetched_at,
        now=now,
        max_age_seconds=authorization.exchange_max_age_seconds,
        label="preflight fetched_at",
    )
    _require_timestamp_not_before(
        fetched_at,
        not_before=exchange_not_before,
        label="preflight fetched_at",
    )
    if payload.get("mirror_stale") is True:
        raise LiveTradeExecutionError(
            "preflight exchange mirror is stale"
        )
    position_side = _required_text(
        payload.get("target_position_side"),
        "preflight target_position_side",
    ).upper()
    position_quantity = _decimal(
        payload.get("target_position_quantity"),
        "preflight target_position_quantity",
        non_negative=True,
    )
    if position_side != "FLAT" or position_quantity != 0:
        raise LiveTradeExecutionError(
            "preflight target position is not flat"
        )
    for field_name in (
        "target_regular_order_count",
        "target_algo_order_count",
    ):
        value = payload.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise LiveTradeExecutionError(
                f"preflight {field_name} must be an integer"
            )
        if value != 0:
            raise LiveTradeExecutionError(
                f"preflight {field_name} must be zero"
            )
    baseline = _required_sha256(
        payload.get("non_target_portfolio_baseline_sha256"),
        "preflight non-target portfolio baseline",
    )
    if baseline != authorization.portfolio_baseline_sha256:
        warnings.append(
            _non_target_portfolio_drift_warning(
                expected=authorization.portfolio_baseline_sha256,
                observed=baseline,
            )
        )
    node_snapshot = payload.get("node_snapshot")
    if (
        node_snapshot is not None
        and node_snapshot is not False
        and not isinstance(node_snapshot, Mapping)
    ):
        raise LiveTradeExecutionError(
            "preflight node snapshot is invalid"
        )
    if isinstance(node_snapshot, Mapping):
        if node_snapshot.get("account_id") != authorization.account_id:
            raise LiveTradeExecutionError(
                "preflight node account identity mismatch"
            )
        expected_node_identity = {
            "node_id": authorization.release.node_id,
            "writer_id": authorization.release.writer_id,
            "lease_id": authorization.release.lease_id,
            "fencing_epoch": authorization.release.fencing_epoch,
        }
        for field_name, expected_value in expected_node_identity.items():
            actual_value = node_snapshot.get(field_name)
            missing_value = actual_value is None or actual_value == ""
            if field_name != "node_id" and missing_value:
                warnings.append(
                    f"PREFLIGHT_NODE_IDENTITY_MISSING: {field_name}"
                )
                continue
            if actual_value != expected_value:
                raise LiveTradeExecutionError(
                    f"preflight node identity mismatch: {field_name}"
                )
        raw_trading_state = node_snapshot.get("trading_state")
        if raw_trading_state is None or raw_trading_state == "":
            warnings.append(
                "PREFLIGHT_NODE_STATE_MISSING: trading_state"
            )
        else:
            trading_state = _required_text(
                raw_trading_state,
                "preflight trading_state",
            ).upper()
            allowed_trading_states = {"HALTED", "STOPPED"}
            if phase == "before-open":
                allowed_trading_states = {
                    "ACTIVE",
                    "RUNNING",
                    "RESUMED",
                }
            if trading_state not in allowed_trading_states:
                raise LiveTradeExecutionError(
                    f"preflight node state is invalid for {phase}"
                )
        for field_name in (
            "process_liveness",
            "loss_monitor_healthy",
        ):
            health_value = node_snapshot.get(field_name)
            if health_value is False:
                raise LiveTradeExecutionError(
                    f"preflight requires {field_name}=true"
                )
            if health_value is not True:
                warnings.append(
                    f"PREFLIGHT_NODE_HEALTH_MISSING: {field_name}"
                )
        for field_name in (
            "actor_tick_at",
            "loss_monitor_at",
        ):
            raw_timestamp = node_snapshot.get(field_name)
            if raw_timestamp is None or raw_timestamp == "":
                warnings.append(
                    f"preflight {field_name} is required"
                )
                continue
            timestamp = _timestamp(
                raw_timestamp,
                f"preflight {field_name}",
            )
            warning = _freshness_warning(
                timestamp,
                now=now,
                max_age_seconds=authorization.health_max_age_seconds,
                label=f"preflight {field_name}",
            )
            if warning:
                warnings.append(warning)
        if node_snapshot.get("readiness") is False:
            warnings.append("PREFLIGHT_READINESS_DEGRADED")
        reconciliation_state = str(
            node_snapshot.get("reconciliation_state") or ""
        ).lower()
        if reconciliation_state and reconciliation_state not in {
            "healthy",
            "ready",
            "ok",
        }:
            warnings.append(
                "PREFLIGHT_RECONCILIATION_DEGRADED: "
                f"{reconciliation_state}"
            )
    else:
        warnings.append("PREFLIGHT_NODE_TELEMETRY_UNAVAILABLE")
    raw_available_balance = payload.get("available_usdt_balance")
    if raw_available_balance is None or raw_available_balance == "":
        warnings.append("PREFLIGHT_AVAILABLE_BALANCE_MISSING")
    else:
        available_balance = _decimal(
            raw_available_balance,
            "preflight available_usdt_balance",
            non_negative=True,
        )
        required_balance = (
            authorization.requested_open_notional_usdt
            + authorization.fee_reserve_usdt
        )
        if available_balance < required_balance:
            raise LiveTradeExecutionError(
                "available USDT balance is below order plus fee reserve"
            )
    evidence_sha256 = _required_sha256(
        payload.get("evidence_sha256"),
        "preflight evidence_sha256",
    )
    return LivePreflightEvidence(
        evidence_sha256=evidence_sha256,
        fetched_at=fetched_at,
        warnings=tuple(dict.fromkeys(warnings)),
        observed_portfolio_baseline_sha256=baseline,
        portfolio_drifted=(
            baseline != authorization.portfolio_baseline_sha256
        ),
    )


def _validated_payload_warnings(
    payload: Mapping[str, Any],
    *,
    label: str,
) -> list[str]:
    raw_warnings = payload.get("warnings", [])
    warning_code = re.sub(
        r"[^A-Z0-9]+",
        "_",
        label.upper(),
    ).strip("_")
    malformed_warning = f"{warning_code}_WARNINGS_MALFORMED"
    if not isinstance(raw_warnings, list):
        detail = str(raw_warnings)
        if isinstance(raw_warnings, Mapping):
            detail = json.dumps(
                _json_safe(dict(raw_warnings)),
                sort_keys=True,
                separators=(",", ":"),
            )
        return [f"{malformed_warning}: {detail}"[:500]]
    normalized: list[str] = []
    malformed_items = 0
    for item in raw_warnings:
        text = str(item or "").strip()
        if not text:
            malformed_items += 1
            continue
        normalized.append(text[:500])
    if malformed_items:
        normalized.append(
            f"{malformed_warning}: {malformed_items} empty item(s)"
        )
    return list(dict.fromkeys(normalized))


def _non_target_portfolio_drift_warning(
    *,
    expected: str,
    observed: str,
) -> str:
    return (
        "NON_TARGET_PORTFOLIO_DRIFT: unrelated account activity "
        f"changed signed baseline {expected} to {observed}"
    )


def _observation_warnings(
    payload: Mapping[str, Any],
) -> list[str]:
    return _validated_payload_warnings(
        payload,
        label="observation",
    )


def _parse_observation(
    payload: Mapping[str, Any],
    authorization: CanaryAuthorization,
    *,
    now: datetime,
    exchange_not_before: datetime | None = None,
) -> TradeObservation:
    _validate_adapter_identity(payload, authorization)
    if payload.get("open_client_order_id") != (
        authorization.open_client_order_id
    ):
        raise LiveTradeExecutionError(
            "observation open client order ID mismatch"
        )
    if payload.get("mark_fresh") is not True:
        raise LiveTradeExecutionError(
            "observation mark price is stale"
        )
    warnings = _observation_warnings(payload)
    loss_monitor_healthy = payload.get("loss_monitor_healthy")
    if loss_monitor_healthy is False:
        raise LiveTradeExecutionError(
            "loss_monitor_healthy=false requires risk flattening"
        )
    if loss_monitor_healthy is not True:
        warnings.append(
            "observation loss_monitor_healthy telemetry is missing"
        )
    open_status = _required_text(
        payload.get("open_status"),
        "open_status",
    ).upper()
    if re.fullmatch(r"[A-Z_]{3,32}", open_status) is None:
        raise LiveTradeExecutionError(
            "open_status is invalid"
        )
    exchange_evidence_state = _required_text(
        payload.get("exchange_evidence_state", "unknown"),
        "exchange_evidence_state",
    ).lower()
    if exchange_evidence_state not in {
        "confirmed_executed",
        "definitively_absent",
        "unknown",
    }:
        raise LiveTradeExecutionError(
            "exchange_evidence_state is invalid"
        )
    filled_quantity = _decimal(
        payload.get("filled_quantity"),
        "filled_quantity",
        non_negative=True,
    )
    average_fill_price = _decimal(
        payload.get("average_fill_price_usdt"),
        "average_fill_price_usdt",
        non_negative=True,
    )
    if filled_quantity > 0 and average_fill_price <= 0:
        raise LiveTradeExecutionError(
            "filled order requires a positive average price"
        )
    if filled_quantity > authorization.quantity:
        raise LiveTradeExecutionError(
            "filled quantity exceeds authorized quantity"
        )
    cumulative_loss = _decimal(
        payload.get("cumulative_net_loss_usdt"),
        "cumulative_net_loss_usdt",
        non_negative=True,
    )
    observed_at = _timestamp(
        payload.get("observed_at"),
        "observation observed_at",
    )
    mark_at = _timestamp(
        payload.get("mark_at"),
        "observation mark_at",
    )
    _require_fresh_timestamp(
        observed_at,
        now=now,
        max_age_seconds=authorization.health_max_age_seconds,
        label="observation observed_at",
    )
    _require_fresh_timestamp(
        mark_at,
        now=now,
        max_age_seconds=authorization.exchange_max_age_seconds,
        label="observation mark_at",
    )
    loss_monitor_source = str(
        payload.get("loss_monitor_source") or ""
    ).strip().lower()
    if not loss_monitor_source:
        loss_monitor_source = "exchange_mirror"
        warnings.append(
            "observation loss_monitor_source telemetry is missing"
        )
    loss_monitor_max_age = authorization.exchange_max_age_seconds
    if "node" in loss_monitor_source:
        loss_monitor_max_age = authorization.health_max_age_seconds
    raw_loss_monitor_at = payload.get("loss_monitor_at")
    if raw_loss_monitor_at is None or raw_loss_monitor_at == "":
        loss_monitor_at = mark_at
        warnings.append(
            "observation loss_monitor_at telemetry is missing"
        )
    else:
        loss_monitor_at = _timestamp(
            raw_loss_monitor_at,
            "observation loss_monitor_at",
        )
        warning = _freshness_warning(
            loss_monitor_at,
            now=now,
            max_age_seconds=loss_monitor_max_age,
            label="observation loss_monitor_at",
        )
        if warning:
            warnings.append(warning)
    _require_timestamp_not_before(
        mark_at,
        not_before=exchange_not_before,
        label="observation mark_at",
    )
    evidence_sha256 = _required_sha256(
        payload.get("evidence_sha256"),
        "observation evidence_sha256",
    )
    return TradeObservation(
        open_status=open_status,
        exchange_evidence_state=exchange_evidence_state,
        filled_quantity=filled_quantity,
        average_fill_price_usdt=average_fill_price,
        cumulative_net_loss_usdt=cumulative_loss,
        observed_at=observed_at,
        mark_at=mark_at,
        loss_monitor_at=loss_monitor_at,
        evidence_sha256=evidence_sha256,
        warnings=tuple(warnings),
    )


def _is_terminal_ioc_no_fill(
    observation: TradeObservation,
) -> bool:
    if not _is_terminal_ioc_zero_fill(observation):
        return False
    return observation.exchange_evidence_state == "definitively_absent"


def _is_terminal_ioc_zero_fill(
    observation: TradeObservation,
) -> bool:
    if observation.open_status not in TERMINAL_NO_FILL_OPEN_STATUSES:
        return False
    return observation.filled_quantity == 0


def _parse_position(
    payload: Mapping[str, Any],
    authorization: CanaryAuthorization,
    *,
    now: datetime,
    exchange_not_before: datetime | None = None,
) -> PositionSnapshot:
    _validate_adapter_identity(payload, authorization)
    _require_evidence_source(
        payload,
        expected=EXCHANGE_EVIDENCE_SOURCE,
        label="position",
    )
    fetched_at = _timestamp(
        payload.get("fetched_at"),
        "position fetched_at",
    )
    _require_fresh_timestamp(
        fetched_at,
        now=now,
        max_age_seconds=authorization.exchange_max_age_seconds,
        label="position fetched_at",
    )
    _require_timestamp_not_before(
        fetched_at,
        not_before=exchange_not_before,
        label="position fetched_at",
    )
    side = _required_text(
        payload.get("position_side"),
        "position_side",
    ).upper()
    if side not in {"LONG", "SHORT", "FLAT"}:
        raise LiveTradeExecutionError(
            "position_side must be LONG, SHORT, or FLAT"
        )
    quantity = _decimal(
        payload.get("position_quantity"),
        "position_quantity",
        non_negative=True,
    )
    if quantity == 0 and side != "FLAT":
        raise LiveTradeExecutionError(
            "zero position quantity requires FLAT side"
        )
    if quantity > 0 and side == "FLAT":
        raise LiveTradeExecutionError(
            "non-zero position quantity requires LONG or SHORT"
        )
    expected_side = "LONG"
    if authorization.open_side == "SELL":
        expected_side = "SHORT"
    if quantity > 0 and side != expected_side:
        raise LiveTradeExecutionError(
            "position side differs from authorized open side"
        )
    return PositionSnapshot(
        side=side,
        quantity=quantity,
        fetched_at=fetched_at,
        evidence_sha256=_required_sha256(
            payload.get("evidence_sha256"),
            "position evidence_sha256",
        ),
    )


def _validated_pending_close_request(
    payload: Mapping[str, Any] | None,
    authorization: CanaryAuthorization,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise LiveTradeExecutionError(
            "pending CLOSE journal request is missing"
        )
    expected_values = {
        "client_order_id": authorization.close_client_order_id,
        "intent_id": authorization.close_intent_id,
        "open_intent_id": authorization.intent_id,
        "side_effect_id": deterministic_side_effect_id(
            authorization,
            "CLOSE",
        ),
        "order_type": "MARKET",
    }
    for field_name, expected_value in expected_values.items():
        if payload.get(field_name) != expected_value:
            raise LiveTradeExecutionError(
                f"pending CLOSE identity mismatch: {field_name}"
            )
    if payload.get("reduce_only") is not True:
        raise LiveTradeExecutionError(
            "pending CLOSE request must be reduce-only"
        )
    expected_position_side = "LONG"
    expected_close_side = "SELL"
    if authorization.open_side == "SELL":
        expected_position_side = "SHORT"
        expected_close_side = "BUY"
    if payload.get("position_side") != expected_position_side:
        raise LiveTradeExecutionError(
            "pending CLOSE position side mismatch"
        )
    if payload.get("side") != expected_close_side:
        raise LiveTradeExecutionError(
            "pending CLOSE side mismatch"
        )
    quantity = _decimal(
        payload.get("quantity"),
        "pending CLOSE quantity",
        positive=True,
    )
    if quantity > authorization.quantity:
        raise LiveTradeExecutionError(
            "pending CLOSE quantity exceeds authorization"
        )
    raw_mode = payload.get("mode")
    if raw_mode is not None and raw_mode != "live":
        raise LiveTradeExecutionError(
            "pending CLOSE mode mismatch"
        )
    raw_timeout = payload.get("hard_timeout_seconds")
    if raw_timeout is not None:
        _positive_float(
            raw_timeout,
            "pending CLOSE hard_timeout_seconds",
        )
    return _json_safe(dict(payload))


def _recovery_snapshot_target_risk(
    payload: Mapping[str, Any],
    authorization: CanaryAuthorization,
    *,
    now: datetime,
    exchange_not_before: datetime | None,
) -> Mapping[str, Any] | bool:
    _validate_adapter_identity(payload, authorization)
    _require_evidence_source(
        payload,
        expected=EXCHANGE_EVIDENCE_SOURCE,
        label="recovery final snapshot",
    )
    fetched_at = _timestamp(
        payload.get("fetched_at"),
        "recovery final snapshot fetched_at",
    )
    _require_fresh_timestamp(
        fetched_at,
        now=now,
        max_age_seconds=authorization.exchange_max_age_seconds,
        label="recovery final snapshot fetched_at",
    )
    _require_timestamp_not_before(
        fetched_at,
        not_before=exchange_not_before,
        label="recovery final snapshot fetched_at",
    )
    truth_fields = (
        "target_symbol_flat",
        "target_symbol_regular_orders_zero",
        "target_symbol_algo_orders_zero",
    )
    truths: dict[str, bool] = {}
    for field_name in truth_fields:
        field_value = payload.get(field_name)
        if not isinstance(field_value, bool):
            raise LiveTradeExecutionError(
                f"recovery final snapshot {field_name} must be boolean"
            )
        truths[field_name] = field_value
    position_quantity = _decimal(
        payload.get("position_quantity"),
        "recovery final position_quantity",
        non_negative=True,
    )
    evidence_sha256 = _required_sha256(
        payload.get("evidence_sha256"),
        "recovery final snapshot evidence_sha256",
    )
    residual_risk = position_quantity > 0
    if not all(truths.values()):
        residual_risk = True
    if not residual_risk:
        return False
    return {
        **truths,
        "position_quantity": _decimal_text(position_quantity),
        "fetched_at": fetched_at.isoformat(),
        "adapter_evidence_sha256": evidence_sha256,
    }


def _financial_decimal_or_degraded(
    payload: Mapping[str, Any],
    field_name: str,
    *,
    warnings: list[str],
    non_negative: bool = False,
) -> Decimal:
    try:
        return _decimal(
            payload.get(field_name),
            f"final {field_name}",
            non_negative=non_negative,
        )
    except LiveTradeExecutionError as exc:
        warnings.append(
            "FINANCIAL_FIELD_INVALID: "
            f"{field_name}: {_exception_text(exc)}"
        )
        return Decimal(0)


def _validate_final_snapshot(
    payload: Mapping[str, Any],
    authorization: CanaryAuthorization,
    *,
    now: datetime,
    exchange_not_before: datetime | None = None,
) -> Mapping[str, Any]:
    _validate_adapter_identity(payload, authorization)
    _require_evidence_source(
        payload,
        expected=EXCHANGE_EVIDENCE_SOURCE,
        label="final snapshot",
    )
    fetched_at = _timestamp(
        payload.get("fetched_at"),
        "final snapshot fetched_at",
    )
    _require_fresh_timestamp(
        fetched_at,
        now=now,
        max_age_seconds=authorization.exchange_max_age_seconds,
        label="final snapshot fetched_at",
    )
    _require_timestamp_not_before(
        fetched_at,
        not_before=exchange_not_before,
        label="final snapshot fetched_at",
    )
    required_truths = (
        "target_symbol_flat",
        "target_symbol_regular_orders_zero",
        "target_symbol_algo_orders_zero",
    )
    for field_name in required_truths:
        if payload.get(field_name) is not True:
            raise LiveTradeExecutionError(
                f"final snapshot requires {field_name}=true"
            )
    position_quantity = _decimal(
        payload.get("position_quantity"),
        "final position_quantity",
        non_negative=True,
    )
    if position_quantity != 0:
        raise LiveTradeExecutionError(
            "final target position is not flat"
        )
    baseline = _required_sha256(
        payload.get("non_target_portfolio_baseline_sha256"),
        "final non-target portfolio baseline",
    )
    portfolio_drifted = (
        baseline != authorization.portfolio_baseline_sha256
    )
    _required_sha256(
        payload.get("evidence_sha256"),
        "final snapshot evidence_sha256",
    )
    normalized_warnings = _validated_payload_warnings(
        payload,
        label="final snapshot",
    )
    open_filled_quantity = _financial_decimal_or_degraded(
        payload,
        "open_filled_quantity",
        warnings=normalized_warnings,
        non_negative=True,
    )
    open_average_fill_price = _financial_decimal_or_degraded(
        payload,
        "open_average_fill_price_usdt",
        warnings=normalized_warnings,
        non_negative=True,
    )
    gross_pnl = _financial_decimal_or_degraded(
        payload,
        "gross_pnl_usdt",
        warnings=normalized_warnings,
    )
    fees = _financial_decimal_or_degraded(
        payload,
        "fees_usdt",
        warnings=normalized_warnings,
        non_negative=True,
    )
    net_pnl = _financial_decimal_or_degraded(
        payload,
        "net_pnl_usdt",
        warnings=normalized_warnings,
    )
    cumulative_loss = _financial_decimal_or_degraded(
        payload,
        "cumulative_net_loss_usdt",
        warnings=normalized_warnings,
        non_negative=True,
    )
    enrichment_degraded = payload.get("enrichment_degraded")
    if not isinstance(enrichment_degraded, bool):
        normalized_warnings.append(
            "FINANCIAL_METADATA_INVALID: "
            "enrichment_degraded must be boolean"
        )
    financial_proof_complete = payload.get(
        "financial_proof_complete"
    )
    if not isinstance(financial_proof_complete, bool):
        normalized_warnings.append(
            "FINANCIAL_METADATA_INVALID: "
            "financial_proof_complete must be boolean"
        )
    initial_warning_state = bool(
        [
            warning
            for warning in normalized_warnings
            if "FINANCIAL_METADATA_" not in warning
        ]
    )
    if (
        isinstance(enrichment_degraded, bool)
        and enrichment_degraded != initial_warning_state
    ):
        normalized_warnings.append(
            "FINANCIAL_METADATA_INCONSISTENT: "
            "enrichment state differs from warnings"
        )
    if (
        isinstance(financial_proof_complete, bool)
        and financial_proof_complete == initial_warning_state
    ):
        normalized_warnings.append(
            "FINANCIAL_METADATA_INCONSISTENT: "
            "financial proof state differs from warnings"
        )
    if open_filled_quantity > authorization.quantity:
        raise LiveTradeExecutionError(
            "final open filled quantity exceeds authorization"
        )
    if open_filled_quantity <= 0:
        normalized_warnings.append(
            "FINANCIAL_PROOF_INCOMPLETE: "
            "open filled quantity is unavailable"
        )
    if open_average_fill_price <= 0:
        normalized_warnings.append(
            "FINANCIAL_PROOF_INCOMPLETE: "
            "open average fill price is unavailable"
        )
    if net_pnl != gross_pnl - fees:
        normalized_warnings.append(
            "FINANCIAL_PNL_INCONSISTENT: "
            "net PnL differs from gross PnL minus fees"
        )
    expected_loss = max(Decimal(0), -net_pnl)
    if cumulative_loss != expected_loss:
        normalized_warnings.append(
            "FINANCIAL_PNL_INCONSISTENT: "
            "cumulative net loss differs from net PnL"
        )
    normalized_warnings = list(dict.fromkeys(normalized_warnings))
    financial_proof_complete = not normalized_warnings
    enrichment_degraded = bool(normalized_warnings)
    normalized = dict(payload)
    normalized["gross_pnl_usdt"] = _decimal_text(gross_pnl)
    normalized["fees_usdt"] = _decimal_text(fees)
    normalized["net_pnl_usdt"] = _decimal_text(net_pnl)
    normalized["cumulative_net_loss_usdt"] = _decimal_text(
        cumulative_loss
    )
    normalized["open_filled_quantity"] = _decimal_text(
        open_filled_quantity
    )
    normalized["open_average_fill_price_usdt"] = _decimal_text(
        open_average_fill_price
    )
    normalized["financial_proof_complete"] = financial_proof_complete
    normalized["enrichment_degraded"] = enrichment_degraded
    normalized["warnings"] = normalized_warnings
    normalized["non_target_portfolio_drifted"] = portfolio_drifted
    normalized["signed_non_target_portfolio_baseline_sha256"] = (
        authorization.portfolio_baseline_sha256
    )
    normalized["source"] = EXCHANGE_EVIDENCE_SOURCE
    normalized["fetched_at"] = fetched_at.isoformat()
    return normalized


def _validate_evidence_payload_identity(
    payload: Mapping[str, Any],
    authorization: CanaryAuthorization,
    *,
    mode: str,
) -> None:
    expected_identity = {
        "schema_version": EVIDENCE_SCHEMA,
        "mode": mode,
        "rollout_phase": "account_a_canary",
        "account_id": authorization.account_id,
        "symbol": authorization.symbol,
        "release_id": authorization.release.release_id,
        "permit_id": authorization.permit_id,
        "intent_id": authorization.intent_id,
        "open_client_order_id": authorization.open_client_order_id,
        "close_client_order_id": authorization.close_client_order_id,
        "authorization_sha256": authorization.authorization_sha256,
    }
    for field_name, expected_value in expected_identity.items():
        if payload.get(field_name) != expected_value:
            raise LiveTradeExecutionError(
                f"evidence identity mismatch: {field_name}"
            )
    status = payload.get("status")
    result_status = payload.get("result_status")
    if status not in RESULT_STATUSES:
        raise LiveTradeExecutionError(
            "evidence result status is invalid"
        )
    if result_status != status:
        raise LiveTradeExecutionError(
            "evidence result status fields differ"
        )
    passed = payload.get("passed")
    if not isinstance(passed, bool):
        raise LiveTradeExecutionError(
            "evidence passed must be boolean"
        )
    expected_passed = status != RESULT_BLOCKED
    if passed != expected_passed:
        raise LiveTradeExecutionError(
            "evidence passed differs from result status"
        )
    details_key = "dry_run_round_trip"
    if mode == "live":
        details_key = "mainnet_round_trip"
    if not isinstance(payload.get(details_key), Mapping):
        raise LiveTradeExecutionError(
            f"evidence {details_key} must be an object"
        )


def _is_recovery_only_record(
    record: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(record, Mapping):
        return False
    if record.get("state") != "HALTED":
        return False
    if str(record.get("pending_action") or ""):
        return False
    if str(record.get("evidence_sha256") or ""):
        return False
    return True


def _is_recovery_only_pending_evidence(
    pending: Mapping[str, Any],
) -> bool:
    payload = pending.get("payload")
    if not isinstance(payload, Mapping):
        return False
    if not isinstance(pending.get("path"), str):
        return False
    if SHA256_RE.fullmatch(str(pending.get("sha256") or "")) is None:
        return False
    return isinstance(pending.get("passed"), bool)


def _is_committed_evidence_record(
    record: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(record, Mapping):
        return False
    if record.get("state") != "EVIDENCE_COMMITTED":
        return False
    if str(record.get("pending_action") or ""):
        return False
    return bool(str(record.get("evidence_sha256") or ""))


def _committed_evidence_requires_enrichment(
    record: Mapping[str, Any] | None,
    authorization: CanaryAuthorization,
    *,
    mode: str,
) -> bool:
    if not _is_committed_evidence_record(record):
        return False
    pending = record.get("pending_evidence")
    if not isinstance(pending, Mapping):
        return False
    pending_hash = _required_sha256(
        pending.get("sha256"),
        "committed pending evidence sha256",
    )
    if pending_hash != record.get("evidence_sha256"):
        raise LiveTradeExecutionError(
            "committed pending evidence hash mismatch"
        )
    payload = pending.get("payload")
    if not isinstance(payload, Mapping):
        raise LiveTradeExecutionError(
            "committed pending evidence payload is invalid"
        )
    _validate_evidence_payload_identity(
        payload,
        authorization,
        mode=mode,
    )
    return payload.get("financial_enrichment_retryable") is True


def _has_recovery_only_pending_evidence(
    record: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(record, Mapping):
        return False
    if record.get("state") not in {
        "HALTED",
        "EVIDENCE_ENRICHMENT_PENDING",
    }:
        return False
    if record.get("pending_action") != "PUBLISH_EVIDENCE":
        return False
    pending = record.get("pending_evidence")
    if not isinstance(pending, Mapping):
        return False
    return _is_recovery_only_pending_evidence(pending)


def _recovery_history_evidence(
    record: Mapping[str, Any],
    authorization: CanaryAuthorization,
) -> Mapping[str, Any]:
    raw_history = record.get("history")
    if not isinstance(raw_history, list):
        raise LiveTradeExecutionError(
            "recovery permit journal history is invalid"
        )
    requested_close_quantity: Decimal | bool = False
    confirmed_close_quantity: Decimal | bool = False
    halt_observed_at: datetime | bool = False
    for expected_sequence, raw_entry in enumerate(raw_history, start=1):
        if not isinstance(raw_entry, Mapping):
            raise LiveTradeExecutionError(
                "recovery permit journal history entry is invalid"
            )
        if raw_entry.get("sequence") != expected_sequence:
            raise LiveTradeExecutionError(
                "recovery permit journal history sequence is invalid"
            )
        payload = raw_entry.get("payload")
        if not isinstance(payload, Mapping):
            raise LiveTradeExecutionError(
                "recovery permit journal history payload is invalid"
            )
        event_type = str(raw_entry.get("event_type") or "")
        action = str(payload.get("action") or "")
        if event_type == "ACTION_PENDING" and action == "CLOSE":
            request = payload.get("request")
            if not isinstance(request, Mapping):
                raise LiveTradeExecutionError(
                    "recovery CLOSE journal request is invalid"
                )
            if (
                request.get("client_order_id")
                != authorization.close_client_order_id
            ):
                raise LiveTradeExecutionError(
                    "recovery CLOSE journal identity mismatch"
                )
            if (
                request.get("side_effect_id")
                != deterministic_side_effect_id(
                    authorization,
                    "CLOSE",
                )
            ):
                raise LiveTradeExecutionError(
                    "recovery CLOSE side effect identity mismatch"
                )
            if request.get("reduce_only") is not True:
                raise LiveTradeExecutionError(
                    "recovery CLOSE journal is not reduce-only"
                )
            requested_close_quantity = _decimal(
                request.get("quantity"),
                "recovery requested close quantity",
                positive=True,
            )
        close_confirmation_event = event_type in {
            "ACTION_CONFIRMED",
            "CLOSE_CONFIRMED",
        }
        if close_confirmation_event and action == "CLOSE":
            result = payload.get("result")
            if not isinstance(result, Mapping):
                raise LiveTradeExecutionError(
                    "recovery CLOSE confirmation is invalid"
                )
            confirmed_close_quantity = (
                _recovery_confirmed_close_quantity(result)
            )
        if raw_entry.get("state") != "HALTED":
            continue
        raw_observed_at = payload.get("observed_at")
        result = payload.get("result")
        if (
            (raw_observed_at is None or raw_observed_at == "")
            and isinstance(result, Mapping)
        ):
            raw_observed_at = result.get("observed_at")
        if raw_observed_at is None or raw_observed_at == "":
            continue
        halt_observed_at = _timestamp(
            raw_observed_at,
            "recovery HALT observed_at",
        )
    if requested_close_quantity is False:
        raise RecoverableEvidenceError(
            "recovery CLOSE journal request is missing"
        )
    if requested_close_quantity > authorization.quantity:
        raise LiveTradeExecutionError(
            "recovery CLOSE journal exceeds authorization"
        )
    if halt_observed_at is False:
        raise RecoverableEvidenceError(
            "recovery HALT audit timestamp is missing"
        )
    return {
        "requested_close_quantity": requested_close_quantity,
        "confirmed_close_quantity": confirmed_close_quantity,
        "halt_observed_at": halt_observed_at,
    }


def _recovery_confirmed_close_quantity(
    result: Mapping[str, Any],
) -> Decimal:
    raw_quantity = result.get("quantity")
    raw_filled_quantity = result.get("filled_quantity")
    quantity: Decimal | bool = False
    filled_quantity: Decimal | bool = False
    if raw_quantity is not None:
        quantity = _decimal(
            raw_quantity,
            "recovery confirmed close quantity",
            positive=True,
        )
    if raw_filled_quantity is not None:
        filled_quantity = _decimal(
            raw_filled_quantity,
            "recovery confirmed close filled_quantity",
            positive=True,
        )
    if quantity is not False and filled_quantity is not False:
        if quantity != filled_quantity:
            raise LiveTradeExecutionError(
                "recovery CLOSE confirmation quantity fields differ"
            )
    if quantity is not False:
        return quantity
    if filled_quantity is not False:
        return filled_quantity
    raise LiveTradeExecutionError(
        "recovery CLOSE confirmation quantity is missing"
    )


def _validate_ack(
    payload: Mapping[str, Any],
    authorization: CanaryAuthorization,
    *,
    action: str,
    expected_client_order_id: str = "",
    expected_side_effect_id: str = "",
    expected_intent_id: str = "",
) -> str:
    _validate_adapter_identity(
        payload,
        authorization,
        expected_intent_id=expected_intent_id,
    )
    if payload.get("accepted") is not True:
        soft_failure = _soft_adapter_rejection(payload, action=action)
        if soft_failure is not None:
            raise soft_failure
        raise LiveTradeExecutionError(
            f"adapter rejected action: {action}"
        )
    if payload.get("action") != action:
        raise LiveTradeExecutionError(
            f"adapter action identity mismatch: {action}"
        )
    if (
        expected_client_order_id
        and payload.get("client_order_id")
        != expected_client_order_id
    ):
        raise LiveTradeExecutionError(
            f"adapter client order ID mismatch: {action}"
        )
    if (
        expected_side_effect_id
        and payload.get("side_effect_id")
        != expected_side_effect_id
    ):
        raise LiveTradeExecutionError(
            f"adapter side effect identity mismatch: {action}"
        )
    return _required_sha256(
        payload.get("evidence_sha256"),
        f"{action} evidence_sha256",
    )


def _validate_resume_ack(
    payload: Mapping[str, Any],
    authorization: CanaryAuthorization,
    *,
    now: datetime,
    expected_side_effect_id: str,
) -> tuple[str, datetime, tuple[str, ...]]:
    evidence_sha256 = _validate_ack(
        payload,
        authorization,
        action="resume",
        expected_side_effect_id=expected_side_effect_id,
    )
    _require_evidence_source(
        payload,
        expected=NODE_STATE_EVIDENCE_SOURCE,
        label="RESUME acknowledgement",
    )
    trading_state = _required_text(
        payload.get("trading_state"),
        "RESUME acknowledgement trading_state",
    ).upper()
    if trading_state not in {"ACTIVE", "RUNNING", "RESUMED"}:
        raise LiveTradeExecutionError(
            "RESUME acknowledgement did not prove an active trading state"
        )
    observed_at = _timestamp(
        payload.get("observed_at"),
        "RESUME acknowledgement observed_at",
    )
    _require_fresh_timestamp(
        observed_at,
        now=now,
        max_age_seconds=authorization.health_max_age_seconds,
        label="RESUME acknowledgement observed_at",
    )
    return (
        evidence_sha256,
        observed_at,
        tuple(
            _validated_payload_warnings(
                payload,
                label="RESUME acknowledgement",
            )
        ),
    )


def _validate_exact_close_ack(
    payload: Mapping[str, Any],
    authorization: CanaryAuthorization,
    *,
    expected_quantity: Decimal,
    expected_side_effect_id: str,
) -> tuple[str, tuple[str, ...]]:
    evidence_sha256 = _validate_ack(
        payload,
        authorization,
        action="close",
        expected_client_order_id=authorization.close_client_order_id,
        expected_side_effect_id=expected_side_effect_id,
        expected_intent_id=authorization.close_intent_id,
    )
    filled_quantity = _decimal(
        payload.get("filled_quantity"),
        "close filled_quantity",
        positive=True,
    )
    if filled_quantity != expected_quantity:
        raise LiveTradeExecutionError(
            "close filled quantity differs from position"
        )
    enrichment_degraded = payload.get(
        "enrichment_degraded",
        False,
    )
    warnings = _validated_payload_warnings(
        payload,
        label="close",
    )
    if not isinstance(enrichment_degraded, bool):
        warnings.append(
            "CLOSE_ENRICHMENT_METADATA_INVALID: "
            "enrichment_degraded must be boolean"
        )
    elif enrichment_degraded != bool(warnings):
        warnings.append(
            "CLOSE_ENRICHMENT_METADATA_INCONSISTENT: "
            "enrichment state differs from warnings"
        )
    return evidence_sha256, tuple(dict.fromkeys(warnings))


def _soft_adapter_rejection(
    payload: Mapping[str, Any],
    *,
    action: str,
) -> SoftActionFailure | None:
    parts = [
        str(payload.get("error_class") or ""),
        str(payload.get("error_code") or ""),
        str(payload.get("code") or ""),
        str(payload.get("reason") or ""),
        str(payload.get("message") or ""),
        str(payload.get("status_code") or ""),
    ]
    description = " ".join(parts).strip()
    if HARD_ADAPTER_ERROR_RE.search(description) is not None:
        return None
    if SOFT_ADAPTER_ERROR_RE.search(description) is None:
        return None
    code = _soft_failure_code(description)
    message = f"{action} soft adapter rejection"
    if description:
        message = f"{message}: {description}"
    return SoftActionFailure(message, code=code)


def _is_soft_action_failure(exc: BaseException) -> bool:
    if HARD_ADAPTER_ERROR_RE.search(_exception_text(exc)) is not None:
        return False
    if isinstance(exc, SoftActionFailure):
        return True
    if isinstance(
        exc,
        (TimeoutError, subprocess.TimeoutExpired),
    ):
        return True
    return SOFT_ADAPTER_ERROR_RE.search(_exception_text(exc)) is not None


def _soft_failure_code(value: Any) -> str:
    text = str(value)
    if re.search(
        r"timeout|timed out|(?<!\d)408(?!\d)",
        text,
        re.IGNORECASE,
    ):
        return "HTTP_TIMEOUT"
    if re.search(r"(?<!\d)425(?!\d)", text):
        return "HTTP_425"
    if re.search(
        r"(?<!\d)429(?!\d)|rate[-_ ]?limit|too many requests",
        text,
        re.IGNORECASE,
    ):
        return "HTTP_429"
    if re.search(
        r"(?<!\d)5\d\d(?!\d)|service unavailable",
        text,
        re.IGNORECASE,
    ):
        return "HTTP_5XX"
    if re.search(r"circuit", text, re.IGNORECASE):
        return "CIRCUIT_OPEN"
    if re.search(
        r"reconciliation(?:[-_ ]?(?:pressure|backlog|busy))",
        text,
        re.IGNORECASE,
    ):
        return "RECONCILIATION_PRESSURE"
    if re.search(
        (
            r"queue|memory(?:[-_ ]?queue)?[-_ ]?pressure|"
            r"resource(?:[-_ ]?(?:pressure|exhausted|busy))|"
            r"overload"
        ),
        text,
        re.IGNORECASE,
    ):
        return "RESOURCE_PRESSURE"
    if re.search(r"readiness", text, re.IGNORECASE):
        return "READINESS_DEGRADED"
    if re.search(r"heartbeat", text, re.IGNORECASE):
        return "HEARTBEAT_DEGRADED"
    if re.search(r"projection", text, re.IGNORECASE):
        return "PROJECTION_DEGRADED"
    return "SOFT_TRANSPORT_FAILURE"


def _hard_failure_code(exc: BaseException) -> str:
    if isinstance(exc, ClassifiedExecutionError):
        return exc.code
    text = _exception_text(exc)
    if re.search(r"actor_tick_at.*stale", text, re.IGNORECASE):
        return "ACTOR_PROGRESS_FROZEN"
    if re.search(r"loss_monitor_at.*stale", text, re.IGNORECASE):
        return "LOSS_MONITOR_FROZEN"
    if re.search(
        r"operation lock|owner|fenc",
        text,
        re.IGNORECASE,
    ):
        return "OWNERSHIP_FENCING_CONFLICT"
    if re.search(
        (
            r"journal|fsync|no space|capacity|durab|"
            r"disk full|filesystem full|file system full|"
            r"atomic replace"
        ),
        text,
        re.IGNORECASE,
    ):
        return "DURABLE_WRITE_OR_CAPACITY_FAILURE"
    if re.search(
        r"HALT|HALTED",
        text,
        re.IGNORECASE,
    ):
        return "HALT_UNPROVEN"
    if re.search(
        r"flat|close|position|final snapshot",
        text,
        re.IGNORECASE,
    ):
        return "EXACT_CLOSE_UNPROVEN"
    return "EXECUTION_BLOCKED"


def _validate_halt_ack(
    payload: Mapping[str, Any],
    authorization: CanaryAuthorization,
    *,
    now: datetime,
    expected_side_effect_id: str,
) -> tuple[str, datetime, tuple[str, ...]]:
    evidence_sha256 = _validate_ack(
        payload,
        authorization,
        action="halt",
        expected_side_effect_id=expected_side_effect_id,
    )
    _require_evidence_source(
        payload,
        expected=NODE_STATE_EVIDENCE_SOURCE,
        label="HALT acknowledgement",
    )
    trading_state = _required_text(
        payload.get("trading_state"),
        "HALT acknowledgement trading_state",
    ).upper()
    if trading_state not in {"HALTED", "STOPPED"}:
        raise LiveTradeExecutionError(
            "HALT acknowledgement did not prove HALTED or STOPPED"
        )
    observed_at = _timestamp(
        payload.get("observed_at"),
        "HALT acknowledgement observed_at",
    )
    _require_fresh_timestamp(
        observed_at,
        now=now,
        max_age_seconds=authorization.health_max_age_seconds,
        label="HALT acknowledgement observed_at",
    )
    return (
        evidence_sha256,
        observed_at,
        tuple(
            _validated_payload_warnings(
                payload,
                label="HALT acknowledgement",
            )
        ),
    )


def _validate_adapter_identity(
    payload: Mapping[str, Any],
    authorization: CanaryAuthorization,
    *,
    expected_intent_id: str = "",
) -> None:
    expected = _identity_payload(
        authorization,
        intent_id=expected_intent_id,
    )
    for field_name, expected_value in expected.items():
        if payload.get(field_name) != expected_value:
            raise LiveTradeExecutionError(
                f"adapter identity mismatch: {field_name}"
            )


def _require_evidence_source(
    payload: Mapping[str, Any],
    *,
    expected: str,
    label: str,
) -> None:
    source = _required_text(
        payload.get("source"),
        f"{label} source",
    ).lower()
    if source != expected:
        raise LiveTradeExecutionError(
            f"{label} source must be {expected}"
        )


def _identity_payload(
    authorization: CanaryAuthorization,
    *,
    intent_id: str = "",
) -> dict[str, Any]:
    selected_intent_id = intent_id
    if not selected_intent_id:
        selected_intent_id = authorization.intent_id
    return {
        "account_id": authorization.account_id,
        "symbol": authorization.symbol,
        "release_id": authorization.release.release_id,
        "node_id": authorization.release.node_id,
        "writer_id": authorization.release.writer_id,
        "lease_id": authorization.release.lease_id,
        "fencing_epoch": authorization.release.fencing_epoch,
        "image_digest": authorization.release.image_digest,
        "config_sha256": authorization.release.config_sha256,
        "dependency_lock_sha256": (
            authorization.release.dependency_lock_sha256
        ),
        "permit_id": authorization.permit_id,
        "intent_id": selected_intent_id,
    }


def _close_side(position_side: str) -> str:
    if position_side == "LONG":
        return "SELL"
    if position_side == "SHORT":
        return "BUY"
    raise LiveTradeExecutionError(
        "cannot close a FLAT position"
    )


def _require_target_identity(
    payload: Mapping[str, Any],
    label: str,
) -> None:
    if payload.get("account_id") != ACCOUNT_ID:
        raise LiveTradeExecutionError(
            f"{label} account must be account-a"
        )
    if payload.get("symbol") != SYMBOL:
        raise LiveTradeExecutionError(
            f"{label} symbol must be SOLUSDT"
        )


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LiveTradeExecutionError(
            f"{label} must be a positive integer"
        )
    if value < 1:
        raise LiveTradeExecutionError(
            f"{label} must be a positive integer"
        )
    return value


def _parse_exchange_filters(
    value: Any,
    *,
    quantity: Decimal,
    limit_price: Decimal,
    requested_notional: Decimal,
    warnings: list[str],
) -> dict[str, Decimal]:
    if value is None:
        warnings.append(
            "EXCHANGE_FILTERS_MISSING: signed exchange metadata unavailable"
        )
        return {}
    if not isinstance(value, Mapping):
        raise LiveTradeExecutionError(
            "exchange_filters must be an object"
        )
    filter_values: dict[str, Decimal] = {}
    filter_fields = (
        "quantity_step",
        "min_quantity",
        "price_tick",
        "min_notional",
    )
    for field_name in filter_fields:
        raw_value = value.get(field_name)
        if raw_value is None or raw_value == "":
            warnings.append(
                f"EXCHANGE_FILTER_MISSING: {field_name}"
            )
            continue
        filter_values[field_name] = _decimal(
            raw_value,
            f"exchange_filters.{field_name}",
            positive=True,
        )

    quantity_step = filter_values.get("quantity_step")
    if quantity_step is not None and quantity % quantity_step != 0:
        raise LiveTradeExecutionError(
            "exchange filter rejected: quantity_step"
        )
    min_quantity = filter_values.get("min_quantity")
    if min_quantity is not None and quantity < min_quantity:
        raise LiveTradeExecutionError(
            "exchange filter rejected: min_quantity"
        )
    price_tick = filter_values.get("price_tick")
    if price_tick is not None and limit_price % price_tick != 0:
        raise LiveTradeExecutionError(
            "exchange filter rejected: price_tick"
        )
    min_notional = filter_values.get("min_notional")
    if min_notional is not None and requested_notional < min_notional:
        raise LiveTradeExecutionError(
            "exchange filter rejected: min_notional"
        )
    return filter_values


def _require_release_identity(
    payload: Mapping[str, Any],
    release: ReleaseIdentity,
    label: str,
) -> None:
    expected = asdict(release)
    for field_name, expected_value in expected.items():
        if payload.get(field_name) != expected_value:
            raise LiveTradeExecutionError(
                f"{label} release identity mismatch: {field_name}"
            )


def _require_document_hash(
    payload: Mapping[str, Any],
    field_name: str,
    expected_value: str,
    label: str,
) -> None:
    actual = _required_sha256(
        payload.get(field_name),
        f"{label} {field_name}",
    )
    if actual != expected_value:
        raise LiveTradeExecutionError(
            f"{label} document hash mismatch: {field_name}"
        )


def _validate_document_window(
    payload: Mapping[str, Any],
    label: str,
    now: datetime,
    *,
    allow_expired: bool = False,
) -> datetime:
    issued_at = _timestamp(
        payload.get("issued_at"),
        f"{label} issued_at",
    )
    expires_at = _timestamp(
        payload.get("expires_at"),
        f"{label} expires_at",
    )
    if issued_at > now + MAX_CLOCK_SKEW:
        raise LiveTradeExecutionError(
            f"{label} issued_at is in the future"
        )
    if expires_at <= now and not allow_expired:
        raise LiveTradeExecutionError(
            f"{label} is expired"
        )
    if expires_at <= issued_at:
        raise LiveTradeExecutionError(
            f"{label} expiry must follow issuance"
        )
    return expires_at


def _require_schema(
    payload: Mapping[str, Any],
    expected: str,
    label: str,
) -> None:
    if payload.get("schema_version") != expected:
        raise LiveTradeExecutionError(
            f"{label} schema mismatch"
        )


def validate_live_adapter(
    path: Path,
    *,
    expected_sha256: str,
) -> ValidatedLiveAdapter:
    target = _required_absolute_path(path, "live adapter path")
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise LiveTradeExecutionError(
            f"cannot resolve live adapter path: {target}"
        ) from exc
    if resolved != target:
        raise LiveTradeExecutionError(
            "live adapter path must be canonical and contain no symlinks"
        )
    payload = _read_protected_file(
        target,
        "live adapter",
        live=True,
    )
    file_stat = target.stat()
    if file_stat.st_mode & stat.S_IXUSR == 0:
        raise LiveTradeExecutionError(
            "live adapter must be owner-executable"
        )
    normalized_hash = _required_sha256(
        expected_sha256,
        "signed live adapter sha256",
    )
    actual_hash = _sha256_bytes(payload)
    if actual_hash != normalized_hash:
        raise LiveTradeExecutionError(
            "live adapter hash differs from signed release"
        )
    return ValidatedLiveAdapter(
        source_path=target,
        sha256=actual_hash,
        payload=payload,
    )


def validate_live_executor(
    path: Path,
    *,
    expected_sha256: str,
) -> str:
    target = _required_absolute_path(path, "live executor path")
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise LiveTradeExecutionError(
            f"cannot resolve live executor path: {target}"
        ) from exc
    if resolved != target:
        raise LiveTradeExecutionError(
            "live executor path must be canonical and contain no symlinks"
        )
    payload = _read_protected_file(
        target,
        "live executor",
        live=True,
    )
    normalized_hash = _required_sha256(
        expected_sha256,
        "signed live executor sha256",
    )
    actual_hash = _sha256_bytes(payload)
    if actual_hash != normalized_hash:
        raise LiveTradeExecutionError(
            "live executor hash differs from signed release"
        )
    return actual_hash


def _validate_system_executable(path: Path, label: str) -> Path:
    target = _required_absolute_path(path, f"{label} path")
    try:
        resolved = target.resolve(strict=True)
        file_stat = target.stat()
    except OSError as exc:
        raise LiveTradeExecutionError(
            f"cannot inspect protected {label}: {target}"
        ) from exc
    if resolved != target:
        raise LiveTradeExecutionError(
            f"{label} path must be canonical"
        )
    if not stat.S_ISREG(file_stat.st_mode):
        raise LiveTradeExecutionError(
            f"{label} must be a regular file"
        )
    if file_stat.st_uid != 0:
        raise LiveTradeExecutionError(
            f"{label} must be root-owned"
        )
    if file_stat.st_mode & 0o022:
        raise LiveTradeExecutionError(
            f"{label} is group/world writable"
        )
    if file_stat.st_mode & 0o111 == 0:
        raise LiveTradeExecutionError(
            f"{label} is not executable"
        )
    return target


def _validate_health_freshness(
    authorization: CanaryAuthorization,
    *,
    now: datetime,
    label: str,
) -> tuple[str, ...]:
    timestamps = {
        "actor_tick_at": authorization.actor_tick_at,
        "heartbeat_at": authorization.heartbeat_at,
        "projection_at": authorization.projection_at,
        "reconciliation_at": authorization.reconciliation_at,
        "loss_monitor_at": authorization.loss_monitor_at,
    }
    warnings = []
    for field_name in SIGNED_HEALTH_FIELDS:
        timestamp = timestamps[field_name]
        if timestamp is None:
            warnings.append(
                "SOFT_HEALTH_TELEMETRY_MISSING: "
                f"{label} {field_name} is missing"
            )
            continue
        warning = _freshness_warning(
            timestamp,
            now=now,
            max_age_seconds=authorization.health_max_age_seconds,
            label=f"{label} {field_name}",
        )
        if warning:
            warnings.append(
                f"SOFT_FRESHNESS_DEGRADED: {warning}"
            )
    return tuple(warnings)


def _freshness_warning(
    timestamp: datetime,
    *,
    now: datetime,
    max_age_seconds: Decimal,
    label: str,
) -> str:
    try:
        _require_fresh_timestamp(
            timestamp,
            now=now,
            max_age_seconds=max_age_seconds,
            label=label,
        )
    except LiveTradeExecutionError as exc:
        return _exception_text(exc)
    return ""


def _require_timestamp_not_before(
    timestamp: datetime,
    *,
    not_before: datetime | None,
    label: str,
) -> None:
    if not_before is None:
        return
    if timestamp < not_before:
        raise LiveTradeExecutionError(
            f"{label} predates required exchange progress"
        )


def _require_fresh_timestamp(
    timestamp: datetime,
    *,
    now: datetime,
    max_age_seconds: Decimal,
    label: str,
) -> None:
    current = _aware_utc(now, "freshness now")
    observed = _aware_utc(timestamp, label)
    age = Decimal(str((current - observed).total_seconds()))
    if age < Decimal(0):
        if abs(age) > Decimal(str(MAX_CLOCK_SKEW.total_seconds())):
            raise LiveTradeExecutionError(
                f"{label} is in the future"
            )
        return
    if age > max_age_seconds:
        raise LiveTradeExecutionError(
            f"{label} is stale"
        )


def _journal_state(value: Any) -> str:
    state = _required_text(value, "journal state").upper()
    if state not in JOURNAL_STATES:
        raise LiveTradeExecutionError(
            f"unsupported permit journal state: {state}"
        )
    return state


def _required_absolute_path(value: Any, label: str) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        raise LiveTradeExecutionError(
            f"{label} must be absolute"
        )
    if ".." in path.parts:
        raise LiveTradeExecutionError(
            f"{label} cannot contain parent traversal"
        )
    return Path(os.path.abspath(path))


def _bind_protected_output_path(
    value: Any,
    label: str,
    *,
    owner_uid: int,
) -> ProtectedOutputPath:
    target = _required_absolute_path(value, label)
    canonical_parent, parent_stat = _validate_protected_parent_chain(
        target.parent,
        label,
        owner_uid=owner_uid,
    )
    canonical_target = canonical_parent / target.name
    try:
        target_stat = os.lstat(canonical_target)
    except FileNotFoundError:
        target_stat = None
    except OSError as exc:
        raise LiveTradeExecutionError(
            f"cannot inspect protected {label}: {canonical_target}"
        ) from exc
    if target_stat is not None:
        if stat.S_ISLNK(target_stat.st_mode):
            raise LiveTradeExecutionError(
                f"{label} must not be a symlink"
            )
        if not stat.S_ISREG(target_stat.st_mode):
            raise LiveTradeExecutionError(
                f"{label} must be a regular file path"
            )
    return ProtectedOutputPath(
        target=canonical_target,
        parent_device=parent_stat.st_dev,
        parent_inode=parent_stat.st_ino,
        owner_uid=owner_uid,
        label=label,
    )


def _validate_protected_parent_chain(
    parent: Path,
    label: str,
    *,
    owner_uid: int,
) -> tuple[Path, os.stat_result]:
    absolute_parent = _required_absolute_path(
        parent,
        f"{label} parent",
    )
    current = Path(absolute_parent.anchor)
    allowed_owner_uids = {0, owner_uid}
    parent_stat: os.stat_result | None = None
    for part in absolute_parent.parts[1:]:
        current = current / part
        try:
            current_stat = os.lstat(current)
        except OSError as exc:
            raise LiveTradeExecutionError(
                f"{label} parent must already exist: {current}"
            ) from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise LiveTradeExecutionError(
                f"{label} parent chain contains a symlink: {current}"
            )
        if not stat.S_ISDIR(current_stat.st_mode):
            raise LiveTradeExecutionError(
                f"{label} parent chain contains a non-directory: "
                f"{current}"
            )
        if current_stat.st_uid not in allowed_owner_uids:
            raise LiveTradeExecutionError(
                f"{label} parent owner is untrusted: {current}"
            )
        writable_by_others = current_stat.st_mode & 0o022
        sticky_root_directory = (
            current_stat.st_uid == 0
            and current_stat.st_mode & stat.S_ISVTX
        )
        if writable_by_others and not sticky_root_directory:
            raise LiveTradeExecutionError(
                f"{label} parent is group/world writable: {current}"
            )
        parent_stat = current_stat
    if parent_stat is None:
        try:
            parent_stat = os.lstat(current)
        except OSError as exc:
            raise LiveTradeExecutionError(
                f"{label} parent must already exist: {current}"
            ) from exc
    return current, parent_stat


def _revalidate_protected_output_path(
    binding: ProtectedOutputPath,
) -> None:
    rebound = _bind_protected_output_path(
        binding.target,
        binding.label,
        owner_uid=binding.owner_uid,
    )
    if rebound.target != binding.target:
        raise LiveTradeExecutionError(
            f"protected {binding.label} path changed"
        )
    if (
        rebound.parent_device != binding.parent_device
        or rebound.parent_inode != binding.parent_inode
    ):
        raise LiveTradeExecutionError(
            f"protected {binding.label} parent changed"
        )


def _read_protected_file(
    path: Path,
    label: str,
    *,
    live: bool,
) -> bytes:
    target = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise LiveTradeExecutionError(
            f"cannot open protected {label}: {target}"
        ) from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise LiveTradeExecutionError(
                f"{label} must be a regular file"
            )
        if live:
            if file_stat.st_uid != os.geteuid():
                raise LiveTradeExecutionError(
                    f"{label} owner differs from executor"
                )
            if file_stat.st_mode & 0o022:
                raise LiveTradeExecutionError(
                    f"{label} is group/world writable"
                )
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _decode_json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise LiveTradeExecutionError(
            f"{label} contains invalid JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise LiveTradeExecutionError(
            f"{label} root must be an object"
        )
    return decoded


def _atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    mode: int,
) -> None:
    _atomic_write_bytes(
        path,
        _canonical_pretty_json_bytes(dict(payload)),
        mode=mode,
        replace_existing=True,
    )


def _atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    replace_existing: bool,
    protected_path: ProtectedOutputPath | None = None,
) -> None:
    target = Path(path)
    if protected_path is None:
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        if target != protected_path.target:
            raise LiveTradeExecutionError(
                "protected output target differs from binding"
            )
        _revalidate_protected_output_path(protected_path)
    if not replace_existing and target.exists():
        raise LiveTradeExecutionError(
            f"target path already exists: {target}"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if protected_path is not None:
            _revalidate_protected_output_path(protected_path)
        if not replace_existing and target.exists():
            raise LiveTradeExecutionError(
                f"target path appeared concurrently: {target}"
            )
        if replace_existing:
            os.replace(temporary_name, target)
        else:
            try:
                os.link(temporary_name, target)
            except FileExistsError as exc:
                raise LiveTradeExecutionError(
                    f"target path appeared concurrently: {target}"
                ) from exc
            os.unlink(temporary_name)
        if protected_path is not None:
            _revalidate_protected_output_path(protected_path)
        _fsync_directory(target.parent)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _write_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _open_protected_lock_file(path: Path):
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LiveTradeExecutionError(
            f"cannot open protected lock file: {path}"
        ) from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise LiveTradeExecutionError(
                "permit ledger lock must be a regular file"
            )
        if file_stat.st_uid != os.geteuid():
            raise LiveTradeExecutionError(
                "permit ledger lock owner differs from executor"
            )
        if file_stat.st_mode & 0o022:
            raise LiveTradeExecutionError(
                "permit ledger lock is group/world writable"
            )
        return os.fdopen(descriptor, "a+", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _json_safe(dict(payload)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _canonical_pretty_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            _json_safe(dict(payload)),
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        return _aware_utc(value, "datetime").isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _synthetic_hash(label: str) -> str:
    return _sha256_bytes(label.encode("ascii"))


def _required_text(value: Any, label: str) -> str:
    if value is None:
        raise LiveTradeExecutionError(f"{label} is required")
    text = str(value).strip()
    if not text:
        raise LiveTradeExecutionError(f"{label} is required")
    return text


def _required_sha256(value: Any, label: str) -> str:
    text = _required_text(value, label).lower()
    if SHA256_RE.fullmatch(text) is None:
        raise LiveTradeExecutionError(
            f"{label} must be a lowercase SHA-256"
        )
    return text


def _required_image_digest(value: Any, label: str) -> str:
    text = _required_text(value, label)
    if IMAGE_DIGEST_RE.fullmatch(text) is None:
        raise LiveTradeExecutionError(
            f"{label} must be a sha256 image digest"
        )
    return text


def _required_client_order_id(value: Any, label: str) -> str:
    text = _required_text(value, label)
    if CLIENT_ORDER_ID_RE.fullmatch(text) is None:
        raise LiveTradeExecutionError(
            f"{label} is invalid"
        )
    return text


def _canonical_uuid(value: Any, label: str) -> str:
    try:
        parsed = UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LiveTradeExecutionError(
            f"{label} must be a UUID"
        ) from exc
    if str(parsed) != str(value):
        raise LiveTradeExecutionError(
            f"{label} must be a canonical UUID"
        )
    return str(parsed)


def _decimal(
    value: Any,
    label: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiveTradeExecutionError(
            f"{label} must be a decimal"
        ) from exc
    if not number.is_finite():
        raise LiveTradeExecutionError(
            f"{label} must be finite"
        )
    if positive and number <= 0:
        raise LiveTradeExecutionError(
            f"{label} must be positive"
        )
    if non_negative and number < 0:
        raise LiveTradeExecutionError(
            f"{label} must be non-negative"
        )
    return number


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _timestamp(value: Any, label: str) -> datetime:
    text = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveTradeExecutionError(
            f"{label} must be an ISO-8601 timestamp"
        ) from exc
    return _aware_utc(parsed, label)


def _optional_timestamp(
    value: Any,
    label: str,
) -> datetime | None:
    if value is None or value == "":
        return None
    return _timestamp(value, label)


def _synthetic_authorization_timestamp(
    authorization: CanaryAuthorization,
) -> datetime:
    candidates = (
        authorization.actor_tick_at,
        authorization.loss_monitor_at,
        authorization.heartbeat_at,
        authorization.projection_at,
        authorization.reconciliation_at,
    )
    for timestamp in candidates:
        if timestamp is not None:
            return timestamp
    return authorization.emergency_close_verified_at


def _aware_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        raise LiveTradeExecutionError(
            f"{label} must include a timezone"
        )
    return value.astimezone(timezone.utc)


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    return _aware_utc(clock(), "clock")


def _execution_mode(value: str) -> str:
    mode = _required_text(value, "mode").lower()
    if mode not in {"dry-run", "live"}:
        raise LiveTradeExecutionError(
            "mode must be dry-run or live"
        )
    return mode


def _positive_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LiveTradeExecutionError(
            f"{label} must be numeric"
        ) from exc
    if number <= 0:
        raise LiveTradeExecutionError(
            f"{label} must be positive"
        )
    return number


def _non_negative_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LiveTradeExecutionError(
            f"{label} must be numeric"
        ) from exc
    if number < 0:
        raise LiveTradeExecutionError(
            f"{label} must be non-negative"
        )
    return number


def _exception_text(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return text
    return exc.__class__.__name__


def _join_errors(
    primary: str,
    additional: Sequence[str],
) -> str:
    parts = []
    if primary:
        parts.append(primary)
    parts.extend(item for item in additional if item)
    return "; ".join(parts)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Execute one fail-closed account-a SOLUSDT canary round trip."
        )
    )
    parser.add_argument("--release-gate", type=Path, required=True)
    parser.add_argument("--release-gate-signature", type=Path)
    parser.add_argument("--safety-gate", type=Path, required=True)
    parser.add_argument("--safety-gate-signature", type=Path)
    parser.add_argument(
        "--emergency-close-gate",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--emergency-close-gate-signature",
        type=Path,
    )
    parser.add_argument("--permit", type=Path, required=True)
    parser.add_argument("--permit-signature", type=Path)
    parser.add_argument("--reviewer-public-key", type=Path)
    parser.add_argument("--permit-ledger", type=Path)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument(
        "--recover-evidence-only",
        action="store_true",
    )
    parser.add_argument(
        "--evidence-recovery-gate",
        type=Path,
    )
    parser.add_argument(
        "--evidence-recovery-gate-signature",
        type=Path,
    )
    parser.add_argument(
        "--live-adapter",
        type=Path,
    )
    parser.add_argument(
        "--adapter-timeout-seconds",
        type=float,
        default=130.0,
    )
    parser.add_argument(
        "--max-observations",
        type=int,
        default=30,
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=1.0,
    )
    parser.add_argument(
        "--recovery-max-attempts",
        type=int,
        default=3,
    )
    parser.add_argument(
        "--recovery-deadline-seconds",
        type=float,
        default=150.0,
    )
    parser.add_argument(
        "--recovery-retry-interval-seconds",
        type=float,
        default=0.5,
    )
    parser.add_argument(
        "--journal-write-timeout-seconds",
        type=float,
        default=1.0,
    )
    return parser


def _authorization_paths_from_args(
    args: argparse.Namespace,
) -> AuthorizationPaths:
    return AuthorizationPaths(
        release_gate=SignedDocumentPaths(
            args.release_gate,
            args.release_gate_signature,
        ),
        safety_gate=SignedDocumentPaths(
            args.safety_gate,
            args.safety_gate_signature,
        ),
        emergency_close_gate=SignedDocumentPaths(
            args.emergency_close_gate,
            args.emergency_close_gate_signature,
        ),
        permit=SignedDocumentPaths(
            args.permit,
            args.permit_signature,
        ),
        reviewer_public_key=args.reviewer_public_key,
    )


def _build_executor(
    args: argparse.Namespace,
    *,
    adapter: TradeAdapter,
    permit_store: SingleUsePermitStore,
    operation_lock: LiveOperationLock | None,
    mode: str,
) -> AccountALiveTradeExecutor:
    return AccountALiveTradeExecutor(
        adapter=adapter,
        permit_store=permit_store,
        evidence_writer=AtomicEvidenceWriter(
            args.evidence_output,
            live=mode == "live",
        ),
        operation_lock=operation_lock,
        mode=mode,
        max_observations=args.max_observations,
        poll_interval_seconds=args.poll_interval_seconds,
        recovery_max_attempts=args.recovery_max_attempts,
        recovery_deadline_seconds=args.recovery_deadline_seconds,
        recovery_retry_interval_seconds=(
            args.recovery_retry_interval_seconds
        ),
        journal_write_timeout_seconds=(
            args.journal_write_timeout_seconds
        ),
        evidence_only=bool(args.recover_evidence_only),
    )


def _execute_dry_run_from_args(
    args: argparse.Namespace,
    paths: AuthorizationPaths,
) -> ExecutionResult:
    ledger_path = args.permit_ledger
    if ledger_path is None:
        raise LiveTradeExecutionError(
            "dry-run execution requires --permit-ledger"
        )
    authorization = load_authorization(
        paths,
        execute_live=False,
    )
    adapter = DryRunAdapter(authorization)
    executor = _build_executor(
        args,
        adapter=adapter,
        permit_store=SingleUsePermitStore(ledger_path),
        operation_lock=None,
        mode="dry-run",
    )
    return executor.execute(authorization)


def _execute_live_from_args(
    args: argparse.Namespace,
    paths: AuthorizationPaths,
) -> ExecutionResult:
    evidence_only = bool(args.recover_evidence_only)
    live_adapter_path = args.live_adapter
    if live_adapter_path is None:
        raise LiveTradeExecutionError(
            "live execution requires --live-adapter"
        )
    if paths.reviewer_public_key is None:
        raise LiveTradeExecutionError(
            "live execution requires --reviewer-public-key"
        )
    if evidence_only and args.evidence_recovery_gate is None:
        raise LiveTradeExecutionError(
            "evidence-only recovery requires "
            "--evidence-recovery-gate"
        )
    if (
        evidence_only
        and args.evidence_recovery_gate_signature is None
    ):
        raise LiveTradeExecutionError(
            "evidence-only recovery requires "
            "--evidence-recovery-gate-signature"
        )
    if args.permit_ledger is not None:
        requested_ledger = _required_absolute_path(
            args.permit_ledger,
            "permit ledger path",
        )
        if requested_ledger != DEFAULT_LIVE_PERMIT_LEDGER_PATH:
            raise LiveTradeExecutionError(
                "live permit ledger path is fixed"
            )

    operation_lock = LiveOperationLock()
    with operation_lock:
        with TerminationSignalGuard():
            verifier = OpenSSLSignatureVerifier()
            authorization = load_authorization(
                paths,
                execute_live=True,
                signature_verifier=verifier,
                operation_lock=operation_lock,
                allow_expired=evidence_only,
            )
            permit_store = SingleUsePermitStore(
                DEFAULT_LIVE_PERMIT_LEDGER_PATH
            )
            if evidence_only:
                recovery_record = permit_store.snapshot(
                    authorization,
                    mode="live",
                )
                if (
                    not _is_recovery_only_record(recovery_record)
                    and not _is_committed_evidence_record(
                        recovery_record
                    )
                    and not _has_recovery_only_pending_evidence(
                        recovery_record
                    )
                ):
                    raise LiveTradeExecutionError(
                        "evidence-only recovery requires an eligible "
                        "permit journal"
                    )
                recovery_gate = load_evidence_recovery_gate(
                    SignedDocumentPaths(
                        args.evidence_recovery_gate,
                        args.evidence_recovery_gate_signature,
                    ),
                    reviewer_public_key=paths.reviewer_public_key,
                    authorization=authorization,
                    evidence_path=args.evidence_output,
                    signature_verifier=verifier,
                    operation_lock=operation_lock,
                )
                authorization = replace(
                    authorization,
                    warnings=(
                        *authorization.warnings,
                        *recovery_gate.warnings,
                    ),
                )
                executor_path = Path(__file__).resolve()
                validate_live_executor(
                    executor_path,
                    expected_sha256=(
                        recovery_gate.recovery_executor_sha256
                    ),
                )
                validated_adapter = validate_live_adapter(
                    live_adapter_path,
                    expected_sha256=(
                        recovery_gate.recovery_adapter_sha256
                    ),
                )
            else:
                validate_live_executor(
                    Path(__file__).resolve(),
                    expected_sha256=(
                        authorization.release.live_executor_sha256
                    ),
                )
                validated_adapter = validate_live_adapter(
                    live_adapter_path,
                    expected_sha256=(
                        authorization.release.live_adapter_sha256
                    ),
                )
            with JsonCommandAdapter(
                validated_adapter,
                timeout_seconds=args.adapter_timeout_seconds,
                live_authorized=authorization.signatures_verified,
                operation_lock=operation_lock,
            ) as adapter:
                selected_adapter: TradeAdapter = adapter
                if evidence_only:
                    selected_adapter = EvidenceRecoveryOnlyAdapter(
                        adapter
                    )
                executor = _build_executor(
                    args,
                    adapter=selected_adapter,
                    permit_store=permit_store,
                    operation_lock=operation_lock,
                    mode="live",
                )
                return executor.execute(authorization)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    execute_live = bool(args.execute_live)
    if execute_live is False and args.live_adapter is not None:
        parser.error("--live-adapter requires --execute-live")
    if args.recover_evidence_only and execute_live is False:
        parser.error(
            "--recover-evidence-only requires --execute-live"
        )

    paths = _authorization_paths_from_args(args)
    mode = "live"
    if execute_live is False:
        mode = "dry-run"
    try:
        if execute_live:
            result = _execute_live_from_args(args, paths)
        else:
            result = _execute_dry_run_from_args(args, paths)
    except LiveTradeExecutionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    summary = {
        "passed": result.passed,
        "mode": mode,
        "evidence_path": str(result.evidence_path),
        "evidence_sha256": result.evidence_sha256,
        "finished_halted": result.finished_halted,
        "close_submitted": result.close_submitted,
        "close_quantity": _decimal_text(result.close_quantity),
    }
    print(json.dumps(summary, sort_keys=True))
    if result.passed:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
